"""Burnarr — entrypoint FastAPI."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import disc, history, staging
from .auth import require_auth
from .burner import BurnerError, manager
from .drive import eject_drive, get_drive_status

# ---------- Logging ----------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("burnarr")

# ---------- Costanti / paths ----------

ISO_LIBRARY = Path(os.environ.get("ISO_LIBRARY", "/data/iso"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
ALLOWED_EXT = {".iso", ".img"}
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "9000"))

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------- App ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    history.init_db()
    staging.cleanup_stale()
    manager.attach_loop(asyncio.get_running_loop())
    log.info("Burnarr avviato (drive=%s)", os.environ.get("DRIVE_DEVICE", "/dev/sr0"))
    yield


app = FastAPI(title="Burnarr", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------- Helpers ----------

_SAFE_FILENAME_RX = re.compile(r"[^A-Za-z0-9._\- ]+")


def _safe_filename(name: str) -> str:
    name = os.path.basename(name).strip()
    name = _SAFE_FILENAME_RX.sub("_", name)
    return name or "upload.iso"


def _resolve_source(rel_path: str) -> Path:
    """Risolve un percorso relativo contro ISO_LIBRARY o UPLOAD_DIR,
    bloccando i tentativi di path traversal."""
    if rel_path.startswith("uploads/"):
        base = UPLOAD_DIR
        sub = rel_path[len("uploads/"):]
    elif rel_path.startswith("iso/"):
        base = ISO_LIBRARY
        sub = rel_path[len("iso/"):]
    else:
        # Per compatibilità: tutto il resto va contro ISO_LIBRARY
        base = ISO_LIBRARY
        sub = rel_path

    candidate = (base / sub).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Percorso non valido.")
    return candidate


def _list_iso_files(root: Path, prefix: str) -> list[dict]:
    out: list[dict] = []
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in ALLOWED_EXT:
            continue
        try:
            rel = p.relative_to(root)
            stat = p.stat()
        except (ValueError, OSError):
            continue
        out.append({
            "path": f"{prefix}/{rel.as_posix()}",
            "name": p.name,
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "source": prefix,
        })
    return out


# ---------- Routes UI ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _user: str = Depends(require_auth)):
    return templates.TemplateResponse("index.html", {"request": request})


# ---------- API ----------

@app.get("/api/status")
async def api_status(_user: str = Depends(require_auth)):
    job = manager.snapshot()
    # Durante un job attivo NON interrogare il device: wodim/growisofs lo
    # tengono aperto e ogni open() concorrente (dvd+rw-mediainfo o wodim -toc)
    # può causare "Device or resource busy" e race con eject finale.
    if manager.is_busy():
        drive_dict = {
            "state": "busy",
            "message": "Drive in uso dalla masterizzazione in corso.",
            "media_type": job.get("media_type") if job else None,
            "family": job.get("media_family") if job else None,
            "capacity_bytes": None,
            "free_bytes": None,
            "writable": False,
            "device": os.environ.get("DRIVE_DEVICE", "/dev/sr0"),
        }
    else:
        drive_dict = get_drive_status().to_dict()
    return {"drive": drive_dict, "job": job}


@app.get("/api/files")
async def api_files(_user: str = Depends(require_auth)):
    files = _list_iso_files(ISO_LIBRARY, "iso") + _list_iso_files(UPLOAD_DIR, "uploads")
    return {"files": files}


@app.post("/api/upload")
async def api_upload(
    file: UploadFile = File(...),
    _user: str = Depends(require_auth),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Nessun file ricevuto.")
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Estensione non supportata. Ammesse: {', '.join(sorted(ALLOWED_EXT))}",
        )

    safe = _safe_filename(file.filename)
    dest = UPLOAD_DIR / safe
    # Evita di sovrascrivere file con lo stesso nome
    if dest.exists():
        stem = dest.stem
        i = 1
        while True:
            cand = UPLOAD_DIR / f"{stem}-{i}{ext}"
            if not cand.exists():
                dest = cand
                break
            i += 1

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File troppo grande (limite {MAX_UPLOAD_MB} MB).",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload fallito: {e}") from e
    finally:
        await file.close()

    return {
        "path": f"uploads/{dest.name}",
        "name": dest.name,
        "size": written,
    }


@app.post("/api/burn")
async def api_burn(
    payload: dict,
    _user: str = Depends(require_auth),
):
    source_rel = payload.get("source_path")
    if not isinstance(source_rel, str) or not source_rel:
        raise HTTPException(status_code=400, detail="source_path mancante.")
    speed = str(payload.get("speed", "auto"))
    verify = bool(payload.get("verify", False))
    eject = bool(payload.get("eject", True))

    if speed not in ("auto", "4", "8", "16", "max"):
        raise HTTPException(status_code=400, detail="Velocità non valida.")

    source_abs = _resolve_source(source_rel)
    if not source_abs.is_file():
        raise HTTPException(status_code=404, detail="File sorgente non trovato.")

    try:
        snap = manager.start(
            source_path=str(source_abs),
            speed=speed,
            verify=verify,
            eject=eject,
        )
    except BurnerError as e:
        if "già in corso" in str(e):
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"job": snap}


@app.post("/api/burn/cancel")
async def api_burn_cancel(_user: str = Depends(require_auth)):
    ok = manager.cancel()
    if not ok:
        raise HTTPException(status_code=409, detail="Nessuna masterizzazione in corso.")
    return {"ok": True}


@app.get("/api/burn/progress")
async def api_progress(_user: str = Depends(require_auth)):
    """SSE: invia eventi JSON con lo snapshot del job."""
    q = manager.subscribe()

    async def event_source():
        try:
            # Heartbeat ogni 15s per tenere viva la connessione dietro reverse proxy.
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/history")
async def api_history(_user: str = Depends(require_auth)):
    return {"burns": history.list_burns(limit=20)}


@app.get("/api/history/{burn_id}/log")
async def api_history_log(burn_id: int, _user: str = Depends(require_auth)):
    burn = history.get_burn(burn_id)
    if not burn:
        raise HTTPException(status_code=404, detail="Record non trovato.")
    log_path = burn.get("log_path")
    if not log_path or not os.path.isfile(log_path):
        raise HTTPException(status_code=404, detail="Log non disponibile.")
    return FileResponse(log_path, media_type="text/plain", filename=f"burn-{burn_id}.log")


# ---------- Sfoglia il disco ----------

@app.get("/api/disc/browse")
async def api_disc_browse(_user: str = Depends(require_auth)):
    if manager.is_busy():
        raise HTTPException(status_code=409, detail="Drive occupato dalla masterizzazione.")
    drive = get_drive_status()
    if drive.state != "has_data":
        raise HTTPException(
            status_code=400,
            detail="Nessun disco con dati nel drive.",
        )
    try:
        entries = disc.list_disc()
        volume = disc.get_volume_info()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Impossibile leggere il disco: {e}") from e

    files = [e.to_dict() for e in entries if not e.is_dir]
    dirs = sorted({e.parent for e in entries})
    total_size = sum(f["size"] for f in files)
    return {
        "volume": volume,
        "files": files,
        "directories": dirs,
        "total_files": len(files),
        "total_size": total_size,
    }


@app.post("/api/disc/eject")
async def api_disc_eject(_user: str = Depends(require_auth)):
    if manager.is_busy():
        raise HTTPException(status_code=409, detail="Drive occupato dalla masterizzazione.")
    ok = eject_drive()
    if not ok:
        raise HTTPException(status_code=500, detail="Espulsione fallita. Riprova.")
    return {"ok": True}


@app.get("/api/disc/file")
async def api_disc_file(path: str, _user: str = Depends(require_auth)):
    if manager.is_busy():
        raise HTTPException(status_code=409, detail="Drive occupato dalla masterizzazione.")
    if not path or not path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="Percorso non valido.")
    drive = get_drive_status()
    if drive.state != "has_data":
        raise HTTPException(status_code=400, detail="Nessun disco nel drive.")

    filename = os.path.basename(path) or "file"
    safe_filename = filename.replace('"', "_")

    try:
        stream = disc.extract_file(path)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    def gen():
        try:
            yield from stream
        except RuntimeError:
            # Errore mid-stream: già loggato in disc.extract_file
            return

    # MIME type best-effort: lasciamo octet-stream per evitare di indovinare male
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_filename}"',
        "Cache-Control": "no-store",
    }
    return StreamingResponse(gen(), media_type="application/octet-stream", headers=headers)


# ---------- Staging (build ISO al volo dai file caricati) ----------

@app.post("/api/staging/new")
async def staging_new(_user: str = Depends(require_auth)):
    sid = staging.create_session()
    return {"staging_id": sid, "files": [], "total_size": 0}


@app.get("/api/staging/{sid}")
async def staging_info(sid: str, _user: str = Depends(require_auth)):
    try:
        files = staging.list_files(sid)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessione non trovata.")
    return {"staging_id": sid, "files": files, "total_size": sum(f["size"] for f in files)}


@app.post("/api/staging/{sid}/file")
async def staging_upload(
    sid: str,
    file: UploadFile = File(...),
    relpath: str = Form(""),
    _user: str = Depends(require_auth),
):
    try:
        staging.session_dir(sid)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessione non trovata.")

    name = relpath or file.filename or "file"
    session_root = staging.session_dir(sid)
    dest = staging.reserve_dest(sid, name)

    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File troppo grande (limite {MAX_UPLOAD_MB} MB).",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload fallito: {e}") from e
    finally:
        await file.close()

    saved_rel = dest.relative_to(session_root).as_posix()
    files = staging.list_files(sid)
    return {
        "staging_id": sid,
        "saved": {"name": saved_rel, "size": written},
        "files": files,
        "total_size": sum(f["size"] for f in files),
    }


@app.delete("/api/staging/{sid}/file/{name:path}")
async def staging_delete_file(sid: str, name: str, _user: str = Depends(require_auth)):
    try:
        staging.session_dir(sid)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Sessione non trovata.")
    if not staging.remove_file(sid, name):
        raise HTTPException(status_code=404, detail="File non trovato.")
    files = staging.list_files(sid)
    return {"files": files, "total_size": sum(f["size"] for f in files)}


@app.delete("/api/staging/{sid}")
async def staging_delete(sid: str, _user: str = Depends(require_auth)):
    staging.cleanup(sid)
    return {"ok": True}


@app.post("/api/burn-from-files")
async def api_burn_from_files(payload: dict, _user: str = Depends(require_auth)):
    sid = payload.get("staging_id")
    if not isinstance(sid, str) or not sid:
        raise HTTPException(status_code=400, detail="staging_id mancante.")
    label = str(payload.get("label", "BURNARR")).strip() or "BURNARR"
    speed = str(payload.get("speed", "auto"))
    verify = bool(payload.get("verify", False))
    eject = bool(payload.get("eject", True))

    if speed not in ("auto", "4", "8", "16", "max"):
        raise HTTPException(status_code=400, detail="Velocità non valida.")

    try:
        snap = manager.start_from_staging(
            staging_id=sid,
            label=label,
            speed=speed,
            verify=verify,
            eject=eject,
        )
    except BurnerError as e:
        if "già in corso" in str(e):
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"job": snap}


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail},
        headers=getattr(exc, "headers", None) or {},
    )
