"""Download video + thumbnail + info.json + subtitles via yt-dlp.

Two passes:
  1. Video + info + thumbnail (must succeed)
  2. Subtitles in selected languages (may fail without aborting)

This avoids YouTube 429 rate-limiting that occurs when requesting many
subtitle variants in the same call as the video stream.
"""
from __future__ import annotations
import shutil
from pathlib import Path

import yt_dlp

from .. import storage
from ..models import VideoRecord, VideoFiles
from .utils import ffprobe_duration, find_by_stem, find_one_by_stem


def _output_dirs(ctx) -> dict[str, Path]:
    base = storage.job_dir(ctx.job_id) / "output"
    dirs = {
        "videos":      base / "videos",
        "thumbnails":  base / "thumbnails",
        "subs_raw":    base / "transcripts" / "raw",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def _rel(p: Path, job_dir: Path) -> str:
    return str(p.relative_to(job_dir))


# ─── Pass 1: video + info + thumbnail ────────────────────────────────

def download_one(ctx, v: VideoRecord) -> None:
    """Download one video + its subtitles. Mutates v.files."""
    job_dir = storage.job_dir(ctx.job_id)
    dirs = _output_dirs(ctx)
    stem = v.title_safe
    cfg = ctx.manifest.config

    video_outtmpl = str(dirs["videos"] / f"{stem}.%(ext)s")

    # ── Pass 1: video + info + thumbnail ────────────────────────────
    opts = {
        "outtmpl": {
            "default":   video_outtmpl,
            "thumbnail": str(dirs["thumbnails"] / f"{stem}.%(ext)s"),
            "infojson":  video_outtmpl,
        },
        "format": f"bestvideo[height<={cfg.quality_cap}]+bestaudio/best[height<={cfg.quality_cap}]/best",
        "merge_output_format": "mp4",
        "writeinfojson": True,
        "writethumbnail": True,
        # NO subtitles in this pass
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 4,
        "overwrites": False,
        "continuedl": True,
        "ignoreerrors": False,
        "retries": 3,
        "fragment_retries": 3,
    }

    info: dict = {}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(v.url, download=True) or {}
        ctx.log(f"Video downloaded: {v.video_id}")
    except Exception as e:
        ctx.error(f"Video download failed for {v.video_id}: {e}")
        return

    # Resolve files written
    video_path = _find_media(dirs["videos"], stem)
    info_path = dirs["videos"] / f"{stem}.info.json"
    if not info_path.exists():
        cand = [p for p in find_by_stem(dirs["videos"], stem) if p.name.endswith(".info.json")]
        info_path = cand[0] if cand else None
    thumb_path = _find_thumb(dirs["thumbnails"], stem)

    v.files = VideoFiles(
        video=_rel(video_path, job_dir) if video_path else None,
        thumbnail=_rel(thumb_path, job_dir) if thumb_path else None,
        info_json=_rel(info_path, job_dir) if info_path and info_path.exists() else None,
    )

    # Backfill metadata
    if info:
        v.view_count = v.view_count or info.get("view_count")
        v.duration_sec = v.duration_sec or info.get("duration")
        v.chapters = info.get("chapters") or []
        if not v.published_at and info.get("upload_date"):
            v.published_at = info["upload_date"]
    if not v.duration_sec and video_path:
        v.duration_sec = ffprobe_duration(video_path)

    # ── Pass 2: subtitles (best-effort) ─────────────────────────────
    _download_subtitles(ctx, v, dirs["subs_raw"])


def _download_subtitles(ctx, v: VideoRecord, subs_dir: Path) -> None:
    """Download subtitles for the requested languages. Failures are non-fatal."""
    cfg = ctx.manifest.config
    sub_langs = _compact_sub_langs(cfg.languages)
    if not sub_langs:
        return

    subs_outtmpl = str(subs_dir / f"{v.title_safe}.%(ext)s")

    opts = {
        "outtmpl": {"default": subs_outtmpl},
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": sub_langs,
        "subtitlesformat": "vtt/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,        # don't crash on 429 / missing langs
        "retries": 2,
        "sleep_interval_subtitles": 1,  # polite delay between sub requests
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(v.url, download=True)
        found = find_by_stem(subs_dir, v.title_safe, suffix_in=(".vtt",))
        ctx.log(f"Subtitles fetched for {v.video_id}: {len(found)} file(s)")
    except Exception as e:
        ctx.log(f"Subtitle fetch had issues for {v.video_id}: {e}")


# ─── Helpers ─────────────────────────────────────────────────────────

def _find_media(folder: Path, stem: str) -> Path | None:
    """Locate the actual video file written by yt-dlp."""
    for ext in ("mp4", "mkv", "webm", "m4v"):
        p = folder / f"{stem}.{ext}"
        if p.exists():
            return p
    return find_one_by_stem(folder, stem, suffix_in=(".mp4", ".mkv", ".webm", ".m4v"))


def _find_thumb(folder: Path, stem: str) -> Path | None:
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = folder / f"{stem}.{ext}"
        if p.exists():
            return p
    return find_one_by_stem(folder, stem, suffix_in=(".jpg", ".jpeg", ".png", ".webp"))


def _compact_sub_langs(langs: list[str]) -> list[str]:
    """Map our 4 standard codes to a minimal yt-dlp list.

    Keep the list short to avoid YouTube rate-limiting. We let yt-dlp's
    `subtitleslangs` matcher handle variants per language.
    """
    out: list[str] = []
    seen: set[str] = set()
    def add(code: str):
        c = code.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)

    for lang in langs:
        lang = lang.strip().lower()
        if lang == "en":
            add("en"); add("en-US")
        elif lang == "zh":
            add("zh-Hans"); add("zh")
        elif lang == "ja":
            add("ja")
        elif lang == "ko":
            add("ko")
        else:
            add(lang)
    return out
