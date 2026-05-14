"""JSON/JSONL persistence layer. No database — just files."""
from __future__ import annotations
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Iterator, Optional

from .models import JobIndexEntry, JobManifest, JobStatusFile


# ─── Paths ───────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
JOBS_INDEX = DATA_DIR / "jobs.jsonl"

DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
if not JOBS_INDEX.exists():
    JOBS_INDEX.touch()


# ─── Locks for write contention (single-process FastAPI but multi-thread jobs) ──

_index_lock = Lock()
_job_locks: dict[str, Lock] = {}


def _job_lock(job_id: str) -> Lock:
    if job_id not in _job_locks:
        _job_locks[job_id] = Lock()
    return _job_locks[job_id]


# ─── Helpers ─────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def ensure_job_dir(job_id: str) -> Path:
    d = job_dir(job_id)
    (d / "output").mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(job_id: str) -> Path:
    return job_dir(job_id) / "manifest.json"


def status_path(job_id: str) -> Path:
    return job_dir(job_id) / "status.json"


def log_path(job_id: str) -> Path:
    return job_dir(job_id) / "log.txt"


# ─── Manifest ────────────────────────────────────────────────────────

def write_manifest(manifest: JobManifest) -> None:
    ensure_job_dir(manifest.job_id)
    with _job_lock(manifest.job_id):
        manifest_path(manifest.job_id).write_text(
            manifest.model_dump_json(indent=2, exclude_none=False),
            encoding="utf-8",
        )


def read_manifest(job_id: str) -> Optional[JobManifest]:
    p = manifest_path(job_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return JobManifest.model_validate(data)
    except Exception:
        return None


# ─── Status ──────────────────────────────────────────────────────────

def write_status(status: JobStatusFile) -> None:
    ensure_job_dir(status.job_id)
    status.updated_at = now_iso()
    with _job_lock(status.job_id):
        status_path(status.job_id).write_text(
            status.model_dump_json(indent=2),
            encoding="utf-8",
        )


def read_status(job_id: str) -> Optional[JobStatusFile]:
    p = status_path(job_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return JobStatusFile.model_validate(data)
    except Exception:
        return None


# ─── Log ─────────────────────────────────────────────────────────────

def append_log(job_id: str, message: str) -> None:
    ensure_job_dir(job_id)
    ts = now_iso()
    line = f"[{ts}] {message}\n"
    with _job_lock(job_id), log_path(job_id).open("a", encoding="utf-8") as f:
        f.write(line)


def read_log(job_id: str) -> str:
    p = log_path(job_id)
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ─── Index (jobs.jsonl) ──────────────────────────────────────────────

def iter_index() -> Iterator[JobIndexEntry]:
    if not JOBS_INDEX.exists():
        return
    with JOBS_INDEX.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield JobIndexEntry.model_validate_json(line)
            except Exception:
                continue


def list_jobs_newest_first() -> list[JobIndexEntry]:
    """Return all jobs, newest first. Each job_id keeps only its latest entry."""
    latest: dict[str, JobIndexEntry] = {}
    for entry in iter_index():
        latest[entry.job_id] = entry
    items = list(latest.values())
    items.sort(key=lambda e: e.created_at, reverse=True)
    return items


def upsert_index(entry: JobIndexEntry) -> None:
    """Append a new line. Reads are deduplicated by job_id (latest wins)."""
    with _index_lock:
        with JOBS_INDEX.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")


def compact_index() -> None:
    """Rewrite the index keeping only the latest entry per job_id."""
    latest = list_jobs_newest_first()
    with _index_lock:
        with JOBS_INDEX.open("w", encoding="utf-8") as f:
            for entry in sorted(latest, key=lambda e: e.created_at):
                f.write(entry.model_dump_json() + "\n")


def delete_job(job_id: str) -> bool:
    """Remove job directory and mark it as deleted in the index."""
    d = job_dir(job_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    # Compact + filter out
    with _index_lock:
        entries = [e for e in list_jobs_newest_first() if e.job_id != job_id]
        with JOBS_INDEX.open("w", encoding="utf-8") as f:
            for e in sorted(entries, key=lambda x: x.created_at):
                f.write(e.model_dump_json() + "\n")
    return True


# ─── Sync helper: keep index in sync with manifest+status ────────────

def sync_index_from_job(job_id: str) -> None:
    """Refresh the index entry for a job based on its manifest+status."""
    m = read_manifest(job_id)
    s = read_status(job_id)
    if not m:
        return
    entry = JobIndexEntry(
        job_id=job_id,
        channel_url=m.channel.url,
        channel_title=m.channel.title,
        channel_avatar=m.channel.avatar_url,
        created_at=m.created_at,
        updated_at=(s.updated_at if s and s.updated_at else None),
        status=(s.status if s else m.status),
        stage=(s.stage if s else None),
        video_count=len(m.videos),
        image_count=len(m.creator_images),
        overall_progress=(s.overall_progress if s else 0.0),
    )
    upsert_index(entry)
