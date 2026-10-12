"""Logica di masterizzazione: wodim per CD, growisofs per DVD.

Espone un singleton ``BurnerManager`` che gestisce un singolo job alla volta,
serializzato da un lock. Il job gira in un thread worker che legge l'output
del processo riga per riga, ne estrae il progresso, aggiorna lo stato e
notifica i subscriber asincroni tramite asyncio.Queue.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import history, staging
from .drive import DRIVE_DEVICE, eject_drive, get_drive_status

log = logging.getLogger(__name__)

LOG_DIR = Path(os.environ.get("BURN_LOG_DIR", "/data/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ---------- Stato job ----------

@dataclass
class BurnJob:
    id: str
    source_path: str
    media_family: str  # "cd" | "dvd"
    media_type: Optional[str]
    speed: str  # "auto" | "4" | "8" | "16" | "max"
    verify: bool
    eject: bool

    status: str = "queued"  # queued | preparing | building | burning | verifying | done | error | cancelled
    progress: float = 0.0  # 0..100
    speed_current: Optional[str] = None  # es. "8.0x"
    eta_seconds: Optional[int] = None
    message: str = ""
    error: Optional[str] = None
    log_path: Optional[str] = None
    history_id: Optional[int] = None

    started_at: Optional[float] = None
    ended_at: Optional[float] = None

    # Costruzione ISO al volo da file caricati (opzionale).
    staging_id: Optional[str] = None
    staging_dir: Optional[str] = None
    iso_label: Optional[str] = None
    source_label: Optional[str] = None  # etichetta umana per UI/storico

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "source_path": self.source_path,
            "media_family": self.media_family,
            "media_type": self.media_type,
            "speed": self.speed,
            "verify": self.verify,
            "eject": self.eject,
            "status": self.status,
            "progress": round(self.progress, 1),
            "speed_current": self.speed_current,
            "eta_seconds": self.eta_seconds,
            "message": self.message,
            "error": self.error,
            "log_path": self.log_path,
            "history_id": self.history_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "iso_label": self.iso_label,
            "source_label": self.source_label,
        }


# ---------- Manager ----------

class BurnerError(Exception):
    """Errore previsto, mostrato all'utente."""


class BurnerManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[BurnJob] = None
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel_requested = False

        self._subscribers: list[asyncio.Queue] = []
        self._sub_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Salva il loop principale per pubblicare eventi da thread esterni."""
        self._loop = loop

    # ----- Subscribers (SSE) -----

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._sub_lock:
            self._subscribers.append(q)
        # Spara subito lo snapshot corrente
        if self._current is not None:
            try:
                q.put_nowait(self._current.snapshot())
            except asyncio.QueueFull:
                pass
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _publish(self, event: dict) -> None:
        if self._loop is None:
            return
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                self._loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:
                pass

    # ----- Snapshot -----

    def snapshot(self) -> Optional[dict]:
        return self._current.snapshot() if self._current else None

    def is_busy(self) -> bool:
        return self._current is not None and self._current.status in (
            "queued",
            "preparing",
            "building",
            "burning",
            "verifying",
        )

    # ----- Helper validazione drive -----

    def _check_drive_ready(self):
        drive = get_drive_status()
        if drive.state != "blank":
            raise BurnerError(
                f"Drive non pronto: {drive.message}. "
                "Inserisci un CD o DVD vergine."
            )
        if drive.family not in ("cd", "dvd"):
            raise BurnerError("Tipo di disco non riconosciuto.")
        return drive

    @staticmethod
    def _sanitize_label(label: str) -> str:
        # Etichetta volume: max 32 char, [A-Z0-9_], niente spazi.
        label = (label or "").strip().upper()
        label = re.sub(r"\s+", "_", label)
        label = re.sub(r"[^A-Z0-9_\-]", "", label)
        label = label[:32]
        return label or "BURNARR"

    # ----- Start -----

    def start(
        self,
        *,
        source_path: str,
        speed: str = "auto",
        verify: bool = False,
        eject: bool = True,
    ) -> dict:
        if self.is_busy():
            raise BurnerError("È già in corso una masterizzazione.")

        if not os.path.isfile(source_path):
            raise BurnerError(f"File sorgente non trovato: {source_path}")

        drive = get_drive_status()
        if drive.state != "blank":
            raise BurnerError(
                f"Drive non pronto: {drive.message}. "
                "Inserisci un CD o DVD vergine."
            )

        if drive.family not in ("cd", "dvd"):
            raise BurnerError("Tipo di disco non riconosciuto.")

        # Sanity check: ISO non più grande della capacità.
        try:
            source_size = os.path.getsize(source_path)
        except OSError as e:
            raise BurnerError(f"Impossibile leggere il file sorgente: {e}") from e

        if drive.capacity_bytes and source_size > drive.capacity_bytes:
            raise BurnerError(
                "Il file è più grande della capacità del disco "
                f"({source_size // 1024 // 1024} MB vs {drive.capacity_bytes // 1024 // 1024} MB)."
            )

        job_id = uuid.uuid4().hex[:8]
        log_path = LOG_DIR / f"burn-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{job_id}.log"

        job = BurnJob(
            id=job_id,
            source_path=source_path,
            media_family=drive.family,
            media_type=drive.media_type,
            speed=speed,
            verify=verify,
            eject=eject,
            status="preparing",
            log_path=str(log_path),
            message="Preparazione in corso...",
            started_at=time.time(),
        )

        with self._lock:
            if self.is_busy():
                raise BurnerError("È già in corso una masterizzazione.")
            self._current = job
            self._cancel_requested = False

        job.history_id = history.add_burn(
            started_at=datetime.now(timezone.utc).isoformat(),
            source_file=source_path,
            media_type=drive.media_type,
            speed=speed,
            verify=verify,
            eject=eject,
            status="preparing",
            log_path=str(log_path),
        )

        self._thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        self._thread.start()

        self._publish(job.snapshot())
        return job.snapshot()

    # ----- Start da staging (ISO al volo) -----

    def start_from_staging(
        self,
        *,
        staging_id: str,
        label: str,
        speed: str = "auto",
        verify: bool = False,
        eject: bool = True,
    ) -> dict:
        if self.is_busy():
            raise BurnerError("È già in corso una masterizzazione.")

        if not shutil.which("genisoimage"):
            raise BurnerError("genisoimage non installato nel container.")

        try:
            staging_dir = staging.session_dir(staging_id)
        except FileNotFoundError as e:
            raise BurnerError("Sessione di staging non trovata.") from e

        files = staging.list_files(staging_id)
        if not files:
            raise BurnerError("Nessun file caricato per la masterizzazione.")
        total = sum(f["size"] for f in files)

        drive = self._check_drive_ready()

        # Stima generosa: l'overhead ISO9660/Joliet/RockRidge è < 5% per archivi
        # normali, ma ci teniamo larghi col 90% della capacità disponibile.
        if drive.capacity_bytes and total > int(drive.capacity_bytes * 0.92):
            raise BurnerError(
                "I file occupano troppo spazio per questo disco "
                f"({total // 1024 // 1024} MB; capacità ~{drive.capacity_bytes // 1024 // 1024} MB). "
                "Rimuovi qualche file o usa un disco più capiente."
            )

        clean_label = self._sanitize_label(label)
        job_id = uuid.uuid4().hex[:8]
        log_path = LOG_DIR / f"burn-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{job_id}.log"

        # source_label per UI/storico, leggibile per i familiari.
        size_mb = total // 1024 // 1024
        source_label = f"Disco \"{clean_label}\" ({len(files)} file, {size_mb} MB)"

        job = BurnJob(
            id=job_id,
            source_path="",  # verrà valorizzato dopo la build
            media_family=drive.family,
            media_type=drive.media_type,
            speed=speed,
            verify=verify,
            eject=eject,
            status="preparing",
            log_path=str(log_path),
            message="Preparazione in corso...",
            started_at=time.time(),
            staging_id=staging_id,
            staging_dir=str(staging_dir),
            iso_label=clean_label,
            source_label=source_label,
        )

        with self._lock:
            if self.is_busy():
                raise BurnerError("È già in corso una masterizzazione.")
            self._current = job
            self._cancel_requested = False

        job.history_id = history.add_burn(
            started_at=datetime.now(timezone.utc).isoformat(),
            source_file=source_label,
            media_type=drive.media_type,
            speed=speed,
            verify=verify,
            eject=eject,
            status="preparing",
            log_path=str(log_path),
        )

        self._thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        self._thread.start()
        self._publish(job.snapshot())
        return job.snapshot()

    # ----- Cancel -----

    def cancel(self) -> bool:
        if not self.is_busy():
            return False
        self._cancel_requested = True
        proc = self._proc
        if proc is None:
            return False
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except Exception:
                pass
        return True

    # ----- Worker -----

    def _build_command(self, job: BurnJob) -> list[str]:
        if job.media_family == "cd":
            if not shutil.which("wodim"):
                raise BurnerError("wodim non installato nel container.")
            cmd = ["wodim", "-v", f"dev={DRIVE_DEVICE}", "driveropts=burnfree", "-dao"]
            if job.speed not in ("auto", "max", ""):
                cmd.append(f"speed={job.speed}")
            cmd.append(job.source_path)
            return cmd

        # DVD via growisofs
        if not shutil.which("growisofs"):
            raise BurnerError("growisofs non installato nel container.")
        cmd = ["growisofs", "-dvd-compat"]
        if job.speed not in ("auto", "max", ""):
            cmd.append(f"-speed={job.speed}")
        cmd += ["-Z", f"{DRIVE_DEVICE}={job.source_path}"]
        return cmd

    def _run(self, job: BurnJob) -> None:
        log_file = open(job.log_path, "w", buffering=1, encoding="utf-8", errors="replace")
        try:
            # ---------- Fase 1: build ISO al volo (se job da staging) ----------
            if job.staging_dir:
                job.status = "building"
                job.message = "Creazione ISO dai file caricati..."
                job.progress = 0.0
                self._publish(job.snapshot())
                ok = self._build_iso(job, log_file)
                if not ok:
                    return  # _build_iso ha già finalizzato l'errore o la cancel
                if self._cancel_requested:
                    self._finalize(job, status="cancelled", error="Annullato dall'utente.")
                    return

            # ---------- Fase 2: masterizzazione ----------
            cmd = self._build_command(job)
            log.info("Avvio masterizzazione: %s", " ".join(cmd))
            log_file.write(f"$ {' '.join(cmd)}\n")

            job.status = "burning"
            job.progress = 0.0
            job.message = "Masterizzazione in corso..."
            self._publish(job.snapshot())

            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=False,  # leggiamo bytes per gestire \r di wodim
                preexec_fn=os.setsid,
            )

            self._read_progress(job, self._proc, log_file)
            rc = self._proc.wait()
            log_file.write(f"\n[exit code: {rc}]\n")

            if self._cancel_requested:
                self._finalize(job, status="cancelled", error="Annullato dall'utente.")
                return
            if rc != 0:
                self._finalize(
                    job,
                    status="error",
                    error=self._friendly_error(job, rc),
                )
                return

            # ---------- Fase 3: verifica opzionale ----------
            if job.verify:
                job.status = "verifying"
                job.message = "Verifica del disco in corso..."
                job.progress = 0.0
                self._publish(job.snapshot())
                ok, vmsg = self._verify(job, log_file)
                if not ok:
                    self._finalize(job, status="error", error=f"Verifica fallita: {vmsg}")
                    return

            if job.eject:
                eject_drive()

            self._finalize(job, status="done")

        except BurnerError as e:
            log_file.write(f"\n[BurnerError] {e}\n")
            self._finalize(job, status="error", error=str(e))
        except Exception as e:
            log.exception("Errore inatteso nel worker")
            log_file.write(f"\n[Exception] {e}\n")
            self._finalize(job, status="error", error=f"Errore interno: {e}")
        finally:
            try:
                log_file.close()
            except Exception:
                pass
            self._proc = None

    # Regex precompilate
    _WODIM_RX = re.compile(
        r"Track\s+\d+:\s+(\d+)\s+of\s+(\d+)\s+MB\s+written.*?([\d.]+)x", re.IGNORECASE
    )
    _GROWISOFS_RX = re.compile(
        r"(\d+)/(\d+)\s*\(\s*([\d.]+)%\)\s*@\s*([\d.]+)x"
        r"(?:,\s*remaining\s+([\d:]+))?", re.IGNORECASE,
    )
    _GENISO_RX = re.compile(rb"(\d+(?:\.\d+)?)\s*%\s*done", re.IGNORECASE)

    # ----- Build ISO con genisoimage -----

    def _build_iso(self, job: BurnJob, log_file) -> bool:
        """Costruisce un'ISO da ``job.staging_dir`` in ``.output.iso``.

        In caso di errore o cancellazione finalizza il job e ritorna False.
        Aggiorna ``job.source_path`` con il path dell'ISO costruito.
        """
        assert job.staging_dir
        staging_path = Path(job.staging_dir)
        output_iso = staging_path / ".output.iso"
        if output_iso.exists():
            try:
                output_iso.unlink()
            except OSError:
                pass

        # Rimuovi i marker interni di staging.py così non finiscono nell'ISO.
        for hidden in (".created", ".output.iso"):
            p = staging_path / hidden
            if p.exists() and p.name == ".created":
                try:
                    p.unlink()
                except OSError:
                    pass

        cmd = [
            "genisoimage",
            "-J",                # Joliet (nomi lunghi + unicode Windows)
            "-r",                # Rock Ridge (POSIX, nomi lunghi Unix)
            "-iso-level", "3",   # singoli file > 4 GB ok (utile per DVD)
            "-V", job.iso_label or "BURNARR",
            "-o", str(output_iso),
            str(job.staging_dir),
        ]
        log.info("Build ISO: %s", " ".join(cmd))
        log_file.write(f"$ {' '.join(cmd)}\n")

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=False,
            preexec_fn=os.setsid,
        )

        buf = bytearray()
        last_publish = 0.0
        try:
            while True:
                chunk = self._proc.stdout.read(256)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    idx = -1
                    for i, b in enumerate(buf):
                        if b in (0x0A, 0x0D):
                            idx = i
                            break
                    if idx == -1:
                        break
                    line_bytes = bytes(buf[:idx])
                    del buf[: idx + 1]
                    text = line_bytes.decode("utf-8", errors="replace").strip()
                    if text:
                        log_file.write(text + "\n")
                    m = self._GENISO_RX.search(line_bytes)
                    if m:
                        try:
                            job.progress = min(100.0, float(m.group(1)))
                        except ValueError:
                            pass
                    now = time.time()
                    if now - last_publish > 0.5:
                        self._publish(job.snapshot())
                        last_publish = now
        finally:
            rc = self._proc.wait()
            log_file.write(f"\n[genisoimage exit code: {rc}]\n")

        if self._cancel_requested:
            try:
                output_iso.unlink(missing_ok=True)
            except OSError:
                pass
            return False

        if rc != 0:
            try:
                output_iso.unlink(missing_ok=True)
            except OSError:
                pass
            self._finalize(
                job,
                status="error",
                error=f"Creazione ISO fallita (codice {rc}). Vedi il log per i dettagli.",
            )
            return False

        job.source_path = str(output_iso)
        return True

    def _read_progress(
        self,
        job: BurnJob,
        proc: subprocess.Popen,
        log_file,
    ) -> None:
        """Legge bytes da stdout, splitta su \\n e \\r, parsa il progresso."""
        assert proc.stdout is not None
        buf = bytearray()
        last_publish = 0.0
        last_progress = -1.0

        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf.extend(chunk)
            # wodim usa \r per aggiornare la riga; spezziamo su entrambi.
            while True:
                idx = -1
                for i, b in enumerate(buf):
                    if b in (0x0A, 0x0D):
                        idx = i
                        break
                if idx == -1:
                    break
                line_bytes = bytes(buf[:idx])
                del buf[: idx + 1]
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                log_file.write(line + "\n")

                self._parse_line(job, line)

                now = time.time()
                if (
                    now - last_publish > 0.5
                    or abs(job.progress - last_progress) >= 0.5
                ):
                    self._publish(job.snapshot())
                    last_publish = now
                    last_progress = job.progress

    def _parse_line(self, job: BurnJob, line: str) -> None:
        if job.media_family == "cd":
            m = self._WODIM_RX.search(line)
            if m:
                written = int(m.group(1))
                total = max(int(m.group(2)), 1)
                job.progress = min(100.0, written * 100.0 / total)
                job.speed_current = f"{m.group(3)}x"
                return
        else:
            m = self._GROWISOFS_RX.search(line)
            if m:
                job.progress = float(m.group(3))
                job.speed_current = f"{m.group(4)}x"
                rem = m.group(5)
                if rem and ":" in rem:
                    parts = rem.split(":")
                    try:
                        if len(parts) == 2:
                            job.eta_seconds = int(parts[0]) * 60 + int(parts[1])
                        elif len(parts) == 3:
                            job.eta_seconds = (
                                int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                            )
                    except ValueError:
                        pass
                return

    def _friendly_error(self, job: BurnJob, rc: int) -> str:
        try:
            tail = Path(job.log_path).read_text(errors="replace").splitlines()[-30:]
            tail_text = "\n".join(tail).lower()
        except Exception:
            tail_text = ""

        if "no medium" in tail_text or "nomedium" in tail_text or "no disc" in tail_text:
            return "Non vedo nessun disco nel masterizzatore. Hai inserito un CD o DVD vuoto?"
        if "medium not blank" in tail_text or "not empty" in tail_text:
            return "Il disco inserito non è vuoto. Inseriscine uno vergine."
        if "permission denied" in tail_text:
            return (
                "Permesso negato sul dispositivo. Controlla i permessi su "
                f"{DRIVE_DEVICE} (potrebbe servire cap_add: SYS_RAWIO o privileged)."
            )
        if "input/output error" in tail_text:
            return "Errore di I/O sul disco. Il disco potrebbe essere difettoso o sporco."
        return f"Masterizzazione fallita (codice {rc}). Vedi il log per i dettagli."

    def _verify(self, job: BurnJob, log_file) -> tuple[bool, str]:
        try:
            source_size = os.path.getsize(job.source_path)
            blocks = source_size // 2048
            if source_size % 2048:
                blocks += 1

            source_hash = hashlib.sha256()
            with open(job.source_path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    source_hash.update(chunk)
            src_hex = source_hash.hexdigest()
            log_file.write(f"\n[verify] source sha256={src_hex}\n")

            disc_hash = hashlib.sha256()
            read_bytes = 0
            with open(DRIVE_DEVICE, "rb") as f:
                while read_bytes < source_size:
                    if self._cancel_requested:
                        return False, "verifica annullata"
                    chunk = f.read(min(1024 * 1024, source_size - read_bytes))
                    if not chunk:
                        break
                    disc_hash.update(chunk)
                    read_bytes += len(chunk)
                    job.progress = min(100.0, read_bytes * 100.0 / source_size)
                    self._publish(job.snapshot())

            disc_hex = disc_hash.hexdigest()
            log_file.write(f"[verify] disc   sha256={disc_hex}\n")

            if src_hex == disc_hex:
                return True, "ok"
            return False, "checksum del disco diverso dal file sorgente"
        except Exception as e:
            return False, str(e)

    def _finalize(
        self,
        job: BurnJob,
        *,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        if job.status in ("done", "error", "cancelled"):
            # Già finalizzato (può capitare se _build_iso ha già chiamato _finalize).
            return

        job.status = status
        job.error = error
        job.ended_at = time.time()
        if status == "done":
            job.progress = 100.0
            job.message = "Masterizzazione completata con successo."
        elif status == "cancelled":
            job.message = "Masterizzazione annullata."
        else:
            job.message = error or "Errore durante la masterizzazione."

        # Cleanup eventuale staging (file caricati + ISO temporanea).
        if job.staging_id:
            try:
                staging.cleanup(job.staging_id)
            except Exception:
                log.exception("Cleanup staging fallita per %s", job.staging_id)

        duration = int((job.ended_at or 0) - (job.started_at or 0))
        if job.history_id is not None:
            try:
                history.finish_burn(
                    job.history_id,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    status=status,
                    duration_sec=duration,
                    error=error,
                )
            except Exception:
                log.exception("Impossibile aggiornare lo storico")

        self._publish(job.snapshot())

        # Lascia il job visibile per qualche secondo, poi libera lo slot.
        def _clear() -> None:
            time.sleep(5)
            with self._lock:
                if self._current is job:
                    self._current = None

        threading.Thread(target=_clear, daemon=True).start()


# Singleton globale
manager = BurnerManager()
