"""FastAPI entry point.

Serves the single-page frontend at `/` and the REST API at `/api/*`.
"""
from __future__ import annotations
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status as http_status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import platform
import subprocess

from pydantic import BaseModel

from . import storage
from .jobs import get_runner
from .models import (
    ConfirmFaceRequest,
    JobCreateRequest,
    JobCreateResponse,
    JobOptions,
)


class RevealRequest(BaseModel):
    path: str = ""  # path relative to the job dir; empty = reveal job dir itself

# ─── Bootstrap ───────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(title="YT Creator Archive", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = PROJECT_ROOT / "frontend"

# Start the job runner eagerly so the first POST doesn't wait
get_runner()


@app.on_event("startup")
def mark_interrupted_on_startup():
    """When the server restarts, any job whose persisted status is still
    'running' or 'pending' is by definition orphaned — its worker thread
    died with the previous process. Mark these as 'failed' with a clear
    reason so the user sees a Retry button immediately.
    """
    from datetime import datetime, timezone
    swept = 0
    for entry in storage.list_jobs_newest_first():
        if entry.status not in ("running", "pending"):
            continue
        m = storage.read_manifest(entry.job_id)
        s = storage.read_status(entry.job_id)
        if not m:
            continue
        msg = "Job interrupted by server restart — click Retry to resume."
        if m.status not in ("completed", "failed", "needs_face_confirmation"):
            m.status = "failed"
            if msg not in m.errors:
                m.errors.append(msg)
            storage.write_manifest(m)
        if s and s.status in ("running", "pending"):
            s.status = "failed"
            s.current_action = "interrupted — retry to resume"
            s.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            storage.write_status(s)
        storage.sync_index_from_job(entry.job_id)
        storage.append_log(entry.job_id, "Job marked as failed: server restarted")
        swept += 1
    if swept:
        logging.getLogger(__name__).info(f"[startup] marked {swept} interrupted job(s) as failed")


# ═══════════════════════════════════════════════════════════════════
#  API routes
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/jobs", response_model=JobCreateResponse)
def create_job(req: JobCreateRequest):
    if not req.channel_url:
        raise HTTPException(400, "channel_url is required")
    options = req.options or _default_options()
    job_id = get_runner().submit(req.channel_url, options)
    return JobCreateResponse(job_id=job_id)


@app.get("/api/jobs")
def list_jobs():
    # Opportunistically refresh the index for any active job so the
    # frontend gets fresh updated_at / stage for stale detection.
    entries = storage.list_jobs_newest_first()
    for e in entries:
        if e.status in ("running", "pending"):
            storage.sync_index_from_job(e.job_id)
    # Re-read after possible sync writes
    return [e.model_dump() for e in storage.list_jobs_newest_first()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    m = storage.read_manifest(job_id)
    if not m:
        raise HTTPException(404, "job not found")
    return m.model_dump()


@app.get("/api/jobs/{job_id}/status")
def get_job_status(job_id: str):
    s = storage.read_status(job_id)
    if not s:
        raise HTTPException(404, "job not found")
    return s.model_dump()


@app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
def get_job_log(job_id: str):
    if not storage.manifest_path(job_id).exists():
        raise HTTPException(404, "job not found")
    return storage.read_log(job_id)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    storage.delete_job(job_id)
    return {"deleted": job_id}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    ok, reason = get_runner().retry(job_id)
    if not ok:
        raise HTTPException(409, reason)
    return {"ok": True, "job_id": job_id}


@app.get("/api/jobs/{job_id}/files/{path:path}")
def get_job_file(job_id: str, path: str):
    """Serve any file under the job's directory."""
    base = storage.job_dir(job_id).resolve()
    target = (base / path).resolve()
    # Path traversal protection
    if not str(target).startswith(str(base)):
        raise HTTPException(400, "invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


@app.post("/api/jobs/{job_id}/reveal")
def reveal_in_finder(job_id: str, req: RevealRequest):
    """Open the OS file manager focused on a file/folder inside the job dir.

    macOS: `open -R <path>` reveals the file in Finder (parent folder, selected).
    Linux: `xdg-open <parent_dir>` opens the parent directory.
    Windows: `explorer /select,<path>` reveals the file.
    """
    base = storage.job_dir(job_id).resolve()
    if not base.exists():
        raise HTTPException(404, "job not found")
    rel = (req.path or "").strip()
    target = (base / rel).resolve() if rel else base
    if not str(target).startswith(str(base)):
        raise HTTPException(400, "invalid path")
    if not target.exists():
        raise HTTPException(404, "file not found")

    system = platform.system()
    try:
        if system == "Darwin":
            # On macOS, `open -R FILE` reveals file in Finder.
            # If target is a directory, `open DIR` opens it.
            if target.is_dir():
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["open", "-R", str(target)])
        elif system == "Windows":
            if target.is_dir():
                subprocess.Popen(["explorer", str(target)])
            else:
                subprocess.Popen(["explorer", "/select,", str(target)])
        else:
            # Linux + others: open parent dir
            parent = target if target.is_dir() else target.parent
            subprocess.Popen(["xdg-open", str(parent)])
        return {"ok": True, "revealed": str(target)}
    except FileNotFoundError as e:
        raise HTTPException(500, f"OS reveal command not found: {e}")
    except Exception as e:
        raise HTTPException(500, f"reveal failed: {e}")


# ─── Face confirmation ───────────────────────────────────────────────

@app.get("/api/jobs/{job_id}/face-candidates")
def face_candidates(job_id: str):
    from .collector import face as face_mod
    if not storage.manifest_path(job_id).exists():
        raise HTTPException(404, "job not found")
    try:
        candidates = face_mod.list_candidates(job_id)
        return [c.model_dump() for c in candidates]
    except Exception as e:
        raise HTTPException(500, f"failed to list candidates: {e}")


@app.post("/api/jobs/{job_id}/confirm-face")
def confirm_face(job_id: str, req: ConfirmFaceRequest):
    from .collector import face as face_mod
    if not storage.manifest_path(job_id).exists():
        raise HTTPException(404, "job not found")
    try:
        face_mod.confirm(job_id, req.face_id)
    except Exception as e:
        raise HTTPException(500, f"confirm failed: {e}")
    # Re-run the image stage
    get_runner().rerun_images(job_id)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
#  Static frontend
# ═══════════════════════════════════════════════════════════════════

if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/jobs/{job_id}")
    def detail_redirect(job_id: str):
        # SPA route — same HTML, hash-based routing client side
        return FileResponse(FRONTEND_DIR / "index.html")


# ═══════════════════════════════════════════════════════════════════
#  Defaults from .env
# ═══════════════════════════════════════════════════════════════════

def _default_options() -> JobOptions:
    def env_int(k, d): return int(os.getenv(k, d))
    def env_float(k, d): return float(os.getenv(k, d))
    return JobOptions(
        latest_count=env_int("DEFAULT_LATEST_COUNT", 5),
        popular_count=env_int("DEFAULT_POPULAR_COUNT", 5),
        audio_bitrate=os.getenv("DEFAULT_AUDIO_BITRATE", "64k"),
        max_audio_mib=env_float("DEFAULT_MAX_AUDIO_MIB", 10),
        languages=os.getenv("DEFAULT_LANGUAGES", "en,zh,ja,ko").split(","),
        skip_shorts=os.getenv("DEFAULT_SKIP_SHORTS", "true").lower() == "true",
    )


# Direct run helper
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
