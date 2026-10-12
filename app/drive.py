"""Stato del drive ottico e detection del disco inserito.

Usa principalmente ``dvd+rw-mediainfo`` che riconosce sia CD che DVD e
restituisce informazioni dettagliate sul media. In fallback usa ``wodim``.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Optional

log = logging.getLogger(__name__)

DRIVE_DEVICE = os.environ.get("DRIVE_DEVICE", "/dev/sr0")

# Mappa codici "Mounted Media" di dvd+rw-mediainfo a famiglia/etichetta.
_MEDIA_MAP = {
    "08h": ("CD-ROM", "cd"),
    "09h": ("CD-R", "cd"),
    "0Ah": ("CD-RW", "cd"),
    "10h": ("DVD-ROM", "dvd"),
    "11h": ("DVD-R", "dvd"),
    "12h": ("DVD-RAM", "dvd"),
    "13h": ("DVD-RW restricted", "dvd"),
    "14h": ("DVD-RW", "dvd"),
    "1Ah": ("DVD+RW", "dvd"),
    "1Bh": ("DVD+R", "dvd"),
    "2Ah": ("DVD+RW DL", "dvd"),
    "2Bh": ("DVD+R DL", "dvd"),
}


@dataclass
class DriveStatus:
    """Snapshot dello stato del drive."""

    state: str  # one of: empty, blank, has_data, error, unknown
    message: str  # messaggio italiano amichevole
    media_type: Optional[str] = None  # es. "CD-R", "DVD+RW"
    family: Optional[str] = None  # "cd" | "dvd"
    capacity_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    writable: bool = False
    device: str = DRIVE_DEVICE

    def to_dict(self) -> dict:
        return asdict(self)


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        log.warning("Comando non trovato: %s", cmd[0])
        return 127, "", f"comando non trovato: {cmd[0]}"
    except subprocess.TimeoutExpired:
        log.warning("Timeout eseguendo: %s", " ".join(cmd))
        return 124, "", "timeout"


def _parse_mediainfo(output: str) -> DriveStatus:
    """Parsa l'output di ``dvd+rw-mediainfo``."""
    media_type: Optional[str] = None
    family: Optional[str] = None
    capacity: Optional[int] = None
    free: Optional[int] = None
    status_text = ""

    for line in output.splitlines():
        line = line.strip()

        m = re.match(r"Mounted Media:\s*([0-9A-Fa-f]{2}h)(?:,\s*(.+))?", line)
        if m:
            code = m.group(1).upper().replace("H", "h")
            label_from_tool = (m.group(2) or "").strip()
            mapped = _MEDIA_MAP.get(code)
            if mapped:
                media_type, family = mapped
            elif label_from_tool:
                media_type = label_from_tool
                family = "dvd" if "DVD" in label_from_tool.upper() else "cd"
            continue

        m = re.match(r"Disc status:\s*(.+)", line, re.IGNORECASE)
        if m:
            status_text = m.group(1).strip().lower()
            continue

        # "Free Blocks: 2295104*2KB" o simili.
        m = re.match(r"Free Blocks:\s*(\d+)\*?(\d+)?\s*K?B?", line)
        if m:
            blocks = int(m.group(1))
            block_size = int(m.group(2)) if m.group(2) else 2
            free = blocks * block_size * 1024
            continue

        m = re.match(r"READ CAPACITY:\s*(\d+)\*?(\d+)?", line)
        if m:
            blocks = int(m.group(1))
            block_size = int(m.group(2)) if m.group(2) else 2048
            capacity = blocks * block_size
            continue

    is_blank = status_text in ("blank", "empty") or (free is not None and capacity is None)
    # Un media riscrivibile può essere "complete" e comunque ri-masterizzabile,
    # ma per sicurezza UX consideriamo masterizzabili solo media blank.
    writable = is_blank and media_type is not None

    if media_type is None:
        return DriveStatus(
            state="unknown",
            message="Disco rilevato ma tipo non riconosciuto.",
            device=DRIVE_DEVICE,
        )

    if is_blank:
        return DriveStatus(
            state="blank",
            message=f"Disco vuoto inserito ({media_type})",
            media_type=media_type,
            family=family,
            capacity_bytes=capacity or free,
            free_bytes=free,
            writable=True,
            device=DRIVE_DEVICE,
        )

    return DriveStatus(
        state="has_data",
        message=f"Disco con dati inserito ({media_type})",
        media_type=media_type,
        family=family,
        capacity_bytes=capacity,
        free_bytes=free,
        writable=writable,
        device=DRIVE_DEVICE,
    )


def _no_media_signature(text: str) -> bool:
    text_l = text.lower()
    needles = (
        "no media",
        "no medium",
        "no disc",
        "no disk",
        "tray open",
        "medium not present",
        "nomedium",
        "not ready",
    )
    return any(n in text_l for n in needles)


def _detect_cd_status() -> DriveStatus:
    """Stato di un CD: combina ``wodim -atip`` (presenza media masterizzabile)
    con ``wodim -toc`` (presenza di una TOC = dischetto già scritto).

    - ATIP presente + nessuna TOC → vergine
    - ATIP presente + TOC         → già scritto
    - né ATIP né TOC, "no medium" → drive vuoto
    """
    if not shutil.which("wodim"):
        return DriveStatus(
            state="unknown",
            message="wodim non disponibile.",
            device=DRIVE_DEVICE,
        )

    rc_atip, atip_out, atip_err = _run(["wodim", "-atip", f"dev={DRIVE_DEVICE}"])
    atip_combined = (atip_out or "") + "\n" + (atip_err or "")
    has_atip = "ATIP info" in atip_combined

    if not has_atip and _no_media_signature(atip_combined):
        return DriveStatus(
            state="empty",
            message="Drive vuoto — inserisci un CD o DVD vergine.",
            device=DRIVE_DEVICE,
        )

    # TOC: se la sessione contiene tracce, il CD è già scritto.
    rc_toc, toc_out, toc_err = _run(["wodim", "-toc", f"dev={DRIVE_DEVICE}"])
    toc_combined = (toc_out or "") + "\n" + (toc_err or "")
    # Output positivo tipico: "first: 1 last: 1" + righe "track:  1  lba: ..."
    has_toc = bool(
        re.search(r"track:\s*\d+\s+lba:", toc_combined, re.IGNORECASE)
        or re.search(r"first:\s*\d+\s+last:\s*\d+", toc_combined, re.IGNORECASE)
    )

    if has_atip and not has_toc:
        return DriveStatus(
            state="blank",
            message="Disco vuoto inserito (CD-R/RW)",
            media_type="CD-R",
            family="cd",
            writable=True,
            device=DRIVE_DEVICE,
        )

    if has_toc:
        return DriveStatus(
            state="has_data",
            message="Disco con dati inserito (CD)",
            media_type="CD",
            family="cd",
            writable=False,
            device=DRIVE_DEVICE,
        )

    return DriveStatus(
        state="unknown",
        message="Stato del CD non determinabile.",
        device=DRIVE_DEVICE,
    )


def get_drive_status() -> DriveStatus:
    """Restituisce lo stato corrente del drive."""
    if not os.path.exists(DRIVE_DEVICE):
        return DriveStatus(
            state="error",
            message=f"Il dispositivo {DRIVE_DEVICE} non esiste. "
            "Controlla che il masterizzatore sia collegato e mappato nel container.",
            device=DRIVE_DEVICE,
        )

    if shutil.which("dvd+rw-mediainfo"):
        rc, out, err = _run(["dvd+rw-mediainfo", DRIVE_DEVICE])
        combined = (out or "") + "\n" + (err or "")

        if rc == 0 and "Mounted Media" in out:
            try:
                return _parse_mediainfo(out)
            except Exception:
                log.exception("Errore parsando dvd+rw-mediainfo")

        # dvd+rw-mediainfo non gestisce i CD: in quel caso stampa
        # ":-( non-DVD media mounted, exiting..." → passa al ramo CD.
        if "non-DVD media" in combined:
            return _detect_cd_status()

        if _no_media_signature(combined):
            return DriveStatus(
                state="empty",
                message="Drive vuoto — inserisci un CD o DVD vergine.",
                device=DRIVE_DEVICE,
            )

    # Fallback completo CD se dvd+rw-mediainfo non c'è.
    return _detect_cd_status()


def eject_drive(retries: int = 4, delay: float = 1.0) -> bool:
    """Espelle il disco. Ritenta in caso di "Device or resource busy" che è
    tipicamente causato dal polling dello stato che apre il device per qualche
    decina di millisecondi."""
    import time as _time
    if not shutil.which("eject"):
        log.warning("Comando 'eject' non disponibile nel container")
        return False
    last_err = ""
    for attempt in range(1, retries + 1):
        rc, _, err = _run(["eject", DRIVE_DEVICE], timeout=15)
        if rc == 0:
            if attempt > 1:
                log.info("Eject riuscito al tentativo %d", attempt)
            return True
        last_err = (err or "").strip()
        log.info("Eject tentativo %d/%d fallito: %s", attempt, retries, last_err)
        _time.sleep(delay)
    log.warning("Eject fallito definitivamente: %s", last_err)
    return False
