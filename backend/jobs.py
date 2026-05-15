"""Background job runner. Orchestrates all collector stages."""
from __future__ import annotations
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from .models import (
    JobIndexEntry,
    JobManifest,
    JobOptions,
    JobStatusFile,
    ChannelInfo,
)
from . import storage

logger = logging.getLogger(__name__)


# ─── Stage definitions ───────────────────────────────────────────────
# Each stage has a name + relative weight for overall progress.
STAGES = [
    ("resolve_channel",   0.05),
    ("select_videos",     0.10),
    ("download_media",    0.45),
    ("audio",             0.15),
    ("subtitles",         0.15),   # may include ASR fallback (slow)
    ("creator_images",    0.10),
]


class JobContext:
    """Helper passed to each stage — gives logging + status update access."""

    def __init__(self, job_id: str, manifest: JobManifest):
        self.job_id = job_id
        self.manifest = manifest
        self._stage_idx = 0
        self._stage_name = "init"

    # ─ logging ─
    def log(self, msg: str) -> None:
        logger.info("[%s] %s", self.job_id, msg)
        storage.append_log(self.job_id, msg)

    def error(self, msg: str) -> None:
        logger.error("[%s] %s", self.job_id, msg)
        storage.append_log(self.job_id, f"ERROR: {msg}")
        self.manifest.errors.append(msg)

    # ─ stage tracking ─
    def begin_stage(self, name: str, total: int = 0) -> None:
        idx = next((i for i, (n, _) in enumerate(STAGES) if n == name), self._stage_idx)
        self._stage_idx = idx
        self._stage_name = name
        self.update_status(
            status="running",
            stage=name,
            stage_progress={"current": 0, "total": total},
            current_action=f"{name} starting",
        )

    def stage_progress(self, current: int, total: Optional[int] = None, action: str = "") -> None:
        st = storage.read_status(self.job_id) or JobStatusFile(job_id=self.job_id)
        total = total if total is not None else st.stage_progress.get("total", 0)
        overall = self._compute_overall(self._stage_idx, current, max(total, 1))
        self.update_status(
            status="running",
            stage=self._stage_name,
            stage_progress={"current": current, "total": total},
            overall_progress=overall,
            current_action=action or st.current_action,
        )

    def update_status(self, **kwargs) -> None:
        st = storage.read_status(self.job_id) or JobStatusFile(job_id=self.job_id)
        for k, v in kwargs.items():
            setattr(st, k, v)
        storage.write_status(st)
        # Mirror to index
        storage.sync_index_from_job(self.job_id)

    @staticmethod
    def _compute_overall(stage_idx: int, current: int, total: int) -> float:
        """Compute overall progress as cumulative-prior-weights + partial-current."""
        done = sum(w for _, w in STAGES[:stage_idx])
        cur_w = STAGES[stage_idx][1] if stage_idx < len(STAGES) else 0
        frac = min(1.0, (current / total) if total > 0 else 0)
        return round(min(1.0, done + cur_w * frac), 4)

    # ─ manifest checkpoint ─
    def save(self) -> None:
        storage.write_manifest(self.manifest)
        storage.sync_index_from_job(self.job_id)


# ─── Job runner ──────────────────────────────────────────────────────

class JobRunner:
    def __init__(self, max_workers: int = 2):
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self._running: dict[str, threading.Event] = {}

    def submit(self, channel_url: str, options: JobOptions) -> str:
        """Create job record + enqueue background task. Returns job_id."""
        job_id = _make_job_id(channel_url)
        storage.ensure_job_dir(job_id)

        manifest = JobManifest(
            job_id=job_id,
            channel=ChannelInfo(url=channel_url),
            config=options,
            status="pending",
            created_at=storage.now_iso(),
        )
        storage.write_manifest(manifest)
        storage.write_status(JobStatusFile(job_id=job_id, status="pending", stage="queued"))

        idx = JobIndexEntry(
            job_id=job_id,
            channel_url=channel_url,
            created_at=manifest.created_at,
            status="pending",
        )
        storage.upsert_index(idx)
        storage.append_log(job_id, f"Job created for {channel_url}")

        stop_event = threading.Event()
        self._running[job_id] = stop_event
        self.pool.submit(self._run, job_id, stop_event)
        return job_id

    def _run(self, job_id: str, stop_event: threading.Event) -> None:
        # Imported here to avoid circular imports at module load
        from .collector import channel as ch_mod
        from .collector import selector as sel_mod
        from .collector import single_video as sv_mod
        from .collector import downloader as dl_mod
        from .collector import subtitles as sub_mod
        from .collector import audio as audio_mod
        from .collector import images as img_mod

        manifest = storage.read_manifest(job_id)
        if not manifest:
            return
        ctx = JobContext(job_id, manifest)
        single_mode = sv_mod.is_single_video_url(manifest.channel.url)

        try:
            ctx.log("=== Job start ===")
            if single_mode:
                ctx.log("URL detected as single video — channel-listing stages will be skipped")

            # ─── Stage 1: resolve channel (or single-video metadata) ─
            ctx.begin_stage("resolve_channel", total=1)
            single_record = sv_mod.resolve(ctx) if single_mode else None
            if not single_mode:
                ch_mod.resolve(ctx)
            ctx.save()  # persist channel info before status sync, so jobs.jsonl picks up title
            ctx.stage_progress(1, 1, action=f"channel: {manifest.channel.title or manifest.channel.url}")

            # ─── Stage 2: select videos (skipped in single-video mode) ─
            ctx.begin_stage("select_videos", total=1)
            if single_mode:
                manifest.videos = [single_record]
                ctx.log("Single-video mode: 1 video, skipping selector")
            else:
                sel_mod.select(ctx)
            ctx.save()
            ctx.stage_progress(1, 1, action=f"selected {len(manifest.videos)} videos")

            # ─── Stage 3: download videos + thumbnails + info.json ───
            total = len(manifest.videos)
            ctx.begin_stage("download_media", total=total)
            for i, v in enumerate(manifest.videos):
                ctx.stage_progress(i, total, action=f"downloading {v.title[:40]}")
                dl_mod.download_one(ctx, v)
                ctx.save()
            ctx.stage_progress(total, total, action="download complete")

            # ─── Stage 4: audio extract + smart split (BEFORE subtitles
            #              so ASR fallback can use the full audio) ────
            ctx.begin_stage("audio", total=total)
            for i, v in enumerate(manifest.videos):
                ctx.stage_progress(i, total, action=f"audio for {v.title[:40]}")
                audio_mod.process_one(ctx, v)
                ctx.save()
            ctx.stage_progress(total, total, action="audio complete")

            # ─── Stage 5: subtitles → md per language ───────────────
            ctx.begin_stage("subtitles", total=total)
            for i, v in enumerate(manifest.videos):
                ctx.stage_progress(i, total, action=f"subtitles for {v.title[:40]}")
                sub_mod.process_one(ctx, v)
                ctx.save()
            ctx.stage_progress(total, total, action="subtitles complete")

            # ─── Stage 6: creator images ────────────────────────────
            ctx.begin_stage("creator_images", total=1)
            needs_face = img_mod.collect(ctx)
            ctx.save()

            # ─── Finalize ────────────────────────────────────────────
            manifest.completed_at = storage.now_iso()
            if needs_face:
                manifest.status = "needs_face_confirmation"
                ctx.update_status(
                    status="needs_face_confirmation",
                    stage="creator_images",
                    overall_progress=1.0,
                    current_action="waiting for face confirmation",
                )
            else:
                manifest.status = "completed"
                ctx.update_status(
                    status="completed",
                    stage="done",
                    overall_progress=1.0,
                    current_action="completed",
                )
            ctx.save()
            ctx.log("=== Job done ===")

        except Exception as e:
            tb = traceback.format_exc()
            ctx.error(f"Fatal: {e}\n{tb}")
            manifest.status = "failed"
            ctx.update_status(
                status="failed",
                current_action=str(e)[:200],
            )
            ctx.save()
        finally:
            self._running.pop(job_id, None)

    def retry(self, job_id: str) -> tuple[bool, str]:
        """Re-run a job from wherever it stopped.

        yt-dlp's `overwrites=False` + our `if not file.exists()` checks make
        every stage idempotent — already-downloaded videos and extracted audio
        are skipped on resume.

        Returns (ok, reason).
        """
        from datetime import datetime, timezone

        manifest = storage.read_manifest(job_id)
        if not manifest:
            return False, "job not found"

        status = storage.read_status(job_id)
        if status and status.status == "running":
            # Refuse retry only if it looks genuinely alive (< 90s since last update)
            try:
                if status.updated_at:
                    updated = datetime.fromisoformat(status.updated_at)
                    age = (datetime.now(timezone.utc) - updated).total_seconds()
                    if age < 90:
                        return False, f"job still active ({age:.0f}s since last update)"
            except Exception:
                pass

        # Reset for retry but KEEP videos/files already on disk
        manifest.status = "pending"
        manifest.errors = []
        manifest.completed_at = None
        storage.write_manifest(manifest)
        storage.write_status(JobStatusFile(
            job_id=job_id,
            status="pending",
            stage="queued",
            current_action="retry queued",
        ))
        storage.append_log(job_id, "=== Retry triggered ===")
        storage.sync_index_from_job(job_id)

        stop_event = threading.Event()
        self._running[job_id] = stop_event
        self.pool.submit(self._run, job_id, stop_event)
        return True, "queued"

    def rerun_images(self, job_id: str) -> None:
        """Re-run only the creator_images stage (after user confirms face)."""
        from .collector import images as img_mod
        manifest = storage.read_manifest(job_id)
        if not manifest:
            return
        ctx = JobContext(job_id, manifest)

        def _go():
            try:
                ctx.log("=== Re-running creator_images after face confirmation ===")
                ctx.begin_stage("creator_images", total=1)
                img_mod.collect(ctx, force=True)
                manifest.status = "completed"
                manifest.completed_at = storage.now_iso()
                ctx.update_status(
                    status="completed",
                    stage="done",
                    overall_progress=1.0,
                    current_action="completed",
                )
                ctx.save()
                ctx.log("=== Re-run done ===")
            except Exception as e:
                tb = traceback.format_exc()
                ctx.error(f"Re-run failed: {e}\n{tb}")

        self.pool.submit(_go)


def _make_job_id(channel_url: str) -> str:
    """job_20260514_153022_<slug>"""
    import re
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", channel_url.split("/")[-1] or "channel")[:32]
    if not slug:
        slug = "channel"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"job_{ts}_{slug}"


# ─── Singleton ───────────────────────────────────────────────────────

RUNNER: Optional[JobRunner] = None


def get_runner() -> JobRunner:
    global RUNNER
    if RUNNER is None:
        RUNNER = JobRunner(max_workers=2)
    return RUNNER
