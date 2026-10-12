"""Sessioni di staging: aree temporanee in cui l'utente carica file/cartelle
da cui il backend costruirà un'ISO al volo con genisoimage."""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

STAGING_ROOT = Path(os.environ.get("STAGING_DIR", "/data/staging"))
STAGING_ROOT.mkdir(parents=True, exist_ok=True)

# Una sessione abbandonata viene cancellata dopo 24h.
TTL_SECONDS = int(os.environ.get("STAGING_TTL", "86400"))

_lock = threading.Lock()
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._\- ]+")


def _resolve_safe(sid: str) -> Path:
    """Ritorna la directory della sessione, verificando che sia dentro STAGING_ROOT."""
    if not re.fullmatch(r"[A-Za-z0-9]{6,32}", sid):
        raise FileNotFoundError(sid)
    d = (STAGING_ROOT / sid).resolve()
    root_resolved = STAGING_ROOT.resolve()
    try:
        d.relative_to(root_resolved)
    except ValueError as e:
        raise FileNotFoundError(sid) from e
    return d


def create_session() -> str:
    sid = uuid.uuid4().hex[:12]
    d = STAGING_ROOT / sid
    d.mkdir(parents=True, exist_ok=False)
    (d / ".created").write_text(str(int(time.time())))
    return sid


def session_dir(sid: str) -> Path:
    d = _resolve_safe(sid)
    if not d.is_dir():
        raise FileNotFoundError(sid)
    return d


def safe_relpath(name: str) -> str:
    """Sanifica un percorso relativo del client preservando le sottocartelle
    ma bloccando ``..`` e nomi assoluti."""
    parts = re.split(r"[\\/]+", name or "")
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p or p in (".", ".."):
            continue
        p = _SAFE_COMPONENT.sub("_", p)
        if p:
            out.append(p)
    return "/".join(out) if out else "file"


def list_files(sid: str) -> list[dict]:
    d = session_dir(sid)
    out: list[dict] = []
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        if p.name == ".created":
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.relative_to(d).as_posix(),
            "size": stat.st_size,
        })
    return out


def total_size(sid: str) -> int:
    return sum(f["size"] for f in list_files(sid))


def reserve_dest(sid: str, relpath: str) -> Path:
    """Crea le sottocartelle e ritorna un path di destinazione univoco
    (aggiunge ``-N`` in caso di collisione)."""
    d = session_dir(sid)
    safe = safe_relpath(relpath)
    dest = d / safe
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        stem, suf = dest.stem, dest.suffix
        i = 1
        while True:
            cand = dest.parent / f"{stem}-{i}{suf}"
            if not cand.exists():
                dest = cand
                break
            i += 1
    return dest


def remove_file(sid: str, relpath: str) -> bool:
    d = session_dir(sid)
    safe = safe_relpath(relpath)
    target = (d / safe).resolve()
    try:
        target.relative_to(d.resolve())
    except ValueError:
        return False
    if not target.is_file():
        return False
    try:
        target.unlink()
    except OSError:
        return False
    # Rimuove eventuali sottocartelle ormai vuote.
    parent = target.parent
    while parent != d and parent.is_dir():
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return True


def cleanup(sid: str) -> None:
    try:
        d = _resolve_safe(sid)
    except FileNotFoundError:
        return
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def cleanup_stale() -> int:
    """Rimuove le sessioni scadute. Restituisce il numero di sessioni rimosse."""
    now = time.time()
    removed = 0
    with _lock:
        for d in STAGING_ROOT.glob("*"):
            if not d.is_dir():
                continue
            marker = d / ".created"
            if not marker.is_file():
                # senza marker: lascia stare
                continue
            try:
                t = int(marker.read_text().strip())
            except (ValueError, OSError):
                continue
            if now - t > TTL_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    if removed:
        log.info("Cleanup staging: rimosse %d sessioni scadute", removed)
    return removed
