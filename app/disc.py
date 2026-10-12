"""Esplorazione del contenuto del disco ottico tramite ``isoinfo``.

Legge la struttura del filesystem ISO9660/Joliet direttamente da ``/dev/sr0``
senza dover mountare il device, quindi senza privilegi extra nel container.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Iterator, Optional

from .drive import DRIVE_DEVICE

log = logging.getLogger(__name__)


@dataclass
class DiscEntry:
    path: str         # path assoluto sul disco, es. "/foto/IMG_0001.jpg"
    name: str         # nome solo (basename)
    size: int         # 0 per directory
    is_dir: bool
    parent: str       # "/" oppure "/foto"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "is_dir": self.is_dir,
            "parent": self.parent,
        }


# Riga tipica di isoinfo -l (Joliet o Rock Ridge):
#   ----------   0    0    0         1465117 Jun  6 2026 [     66 00]  IMG_20130802.jpg
# I primi 10 caratteri sono i permessi (d = directory). Saltiamo poi
# proprietario/gruppo/uid/gid e prendiamo size (intero) + nome (tutto dopo la
# parentesi quadra di chiusura).
_ENTRY_RX = re.compile(
    r"^(?P<perm>[d-][rwx\-]{9})\s+"
    r"\d+\s+\d+\s+\d+\s+"
    r"(?P<size>\d+)\s+"
    r"\S+\s+\d+\s+\d+\s+"      # mese giorno anno
    r"\[\s*\d+\s*\d+\]\s+"      # [extent flag]
    r"(?P<name>.+?)\s*$"
)

_DIR_HEADER_RX = re.compile(r"^Directory listing of (.+?)\s*$")


def _norm(parent: str, name: str) -> str:
    if parent == "/" or parent == "":
        return "/" + name
    return parent.rstrip("/") + "/" + name


def _isoinfo_cmd(args: list[str]) -> list[str]:
    """Costruisce un comando isoinfo. Usa Joliet (-J) per default: i nomi
    lunghi e gli accenti restano leggibili."""
    return ["isoinfo", "-J", "-i", DRIVE_DEVICE, *args]


def list_disc(skip_hidden: bool = True) -> list[DiscEntry]:
    """Restituisce tutte le voci (file + directory) del disco."""
    if not shutil.which("isoinfo"):
        raise RuntimeError("isoinfo non installato nel container.")
    if not os.path.exists(DRIVE_DEVICE):
        raise RuntimeError(f"Device {DRIVE_DEVICE} non disponibile.")

    proc = subprocess.run(
        _isoinfo_cmd(["-l"]),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        # isoinfo -l ritorna 0 anche se ci sono warning, quindi se siamo qui
        # è una vera failure.
        err = (proc.stderr or "").strip()
        raise RuntimeError(err or "isoinfo ha restituito un errore.")

    entries: list[DiscEntry] = []
    current_dir = "/"
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue

        m = _DIR_HEADER_RX.match(line)
        if m:
            current_dir = m.group(1).strip() or "/"
            if not current_dir.startswith("/"):
                current_dir = "/" + current_dir
            continue

        m = _ENTRY_RX.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        if name in (".", ".."):
            continue
        if skip_hidden and name.startswith("."):
            continue

        is_dir = m.group("perm").startswith("d")
        size = int(m.group("size"))
        path = _norm(current_dir, name)
        entries.append(DiscEntry(
            path=path,
            name=name,
            size=0 if is_dir else size,
            is_dir=is_dir,
            parent=current_dir,
        ))

    return entries


def get_volume_info() -> dict:
    """Restituisce metadati del volume: ID, dimensione totale, ecc."""
    if not shutil.which("isoinfo"):
        return {}
    try:
        proc = subprocess.run(
            ["isoinfo", "-d", "-i", DRIVE_DEVICE],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {}
    if proc.returncode != 0:
        return {}

    info: dict = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        info[k.strip().lower()] = v.strip()

    out = {
        "volume_id": info.get("volume id") or None,
        "publisher": info.get("publisher id") or None,
        "preparer": info.get("data preparer id") or None,
        "application": info.get("application id") or None,
    }
    try:
        out["volume_size_bytes"] = int(info.get("volume size is", "0")) * int(info.get("logical block size is", "0"))
    except (ValueError, TypeError):
        out["volume_size_bytes"] = None
    return out


def extract_file(disc_path: str) -> Iterator[bytes]:
    """Estrae un file dal disco senza mountare. Restituisce un generatore di
    chunk da streamare. Solleva ``RuntimeError`` se non leggibile."""
    if not shutil.which("isoinfo"):
        raise RuntimeError("isoinfo non installato.")
    if not disc_path.startswith("/") or ".." in disc_path.split("/"):
        raise RuntimeError("Percorso non valido.")

    proc = subprocess.Popen(
        _isoinfo_cmd(["-x", disc_path]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1024 * 64)
            if not chunk:
                break
            yield chunk
        rc = proc.wait(timeout=10)
        if rc != 0:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise RuntimeError(f"Estrazione fallita: {err.strip() or 'codice ' + str(rc)}")
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
