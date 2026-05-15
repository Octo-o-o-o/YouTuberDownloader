"""Single-video mode: YouTube watch/shorts/live URLs + x.com (Twitter) status URLs.

A single yt-dlp call gives us both the uploader (used as ChannelInfo) and the
video metadata, so we can skip both `channel.resolve()` and `selector.select()`
for these URLs. The rest of the pipeline (download → audio → subtitles →
creator images) runs unchanged because every stage operates on a VideoRecord
list, not on the channel listing.
"""
from __future__ import annotations
import re
from typing import Any, Dict
from urllib.parse import urlparse

import yt_dlp

from ..models import ChannelInfo, VideoRecord
from .utils import safe_stem


# ─── URL detection ───────────────────────────────────────────────────

_YOUTUBE_VIDEO_PATTERNS = (
    re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/watch\?",   re.I),
    re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/shorts/",   re.I),
    re.compile(r"^https?://(?:www\.|m\.)?youtube\.com/live/",     re.I),
    re.compile(r"^https?://(?:www\.)?youtu\.be/[A-Za-z0-9_-]+",   re.I),
)

# `https://x.com/<user>/status/<id>` or twitter.com variant
_X_VIDEO_PATTERNS = (
    re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/]+/status/\d+", re.I),
)


def is_single_video_url(url: str) -> bool:
    """True if the URL identifies one specific video (not a channel/user listing)."""
    u = (url or "").strip()
    if not u:
        return False
    return any(p.match(u) for p in _YOUTUBE_VIDEO_PATTERNS + _X_VIDEO_PATTERNS)


def is_x_url(url: str) -> bool:
    host = urlparse((url or "").strip()).netloc.lower()
    return host.endswith("x.com") or host.endswith("twitter.com")


# ─── Extraction ──────────────────────────────────────────────────────

def _pick_largest(thumbs: list[dict]) -> str | None:
    if not thumbs:
        return None
    return max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)).get("url")


def _pick_avatar(thumbs: list[dict]) -> str | None:
    if not thumbs:
        return None
    avatar_likes = [t for t in thumbs if "avatar" in (t.get("id") or "").lower()]
    if avatar_likes:
        return _pick_largest(avatar_likes)
    return None


def resolve(ctx) -> VideoRecord:
    """Extract single-video metadata; populate manifest.channel; return VideoRecord.

    Raises RuntimeError if yt-dlp cannot extract the video.
    """
    raw_url = ctx.manifest.channel.url
    ctx.log(f"Single-video mode: extracting {raw_url}")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info: Dict[str, Any] = ydl.extract_info(raw_url, download=False) or {}
    except Exception as e:
        raise RuntimeError(f"Could not extract video info from {raw_url}: {e}")

    video_id = info.get("id") or info.get("display_id")
    if not video_id:
        raise RuntimeError(f"yt-dlp could not identify a video at {raw_url}")

    title = info.get("title") or info.get("description") or video_id
    if title and len(title) > 180:
        title = title[:180].rstrip() + "…"

    uploader_id = info.get("uploader_id") or info.get("channel_id") or ""
    uploader = info.get("uploader") or info.get("channel") or uploader_id or "unknown"
    channel_id = info.get("channel_id") or uploader_id or video_id

    handle = uploader_id or ""
    if is_x_url(raw_url) and handle and not handle.startswith("@"):
        handle = f"@{handle}"

    thumbs = info.get("thumbnails") or []
    # For YouTube watch URLs, yt-dlp usually includes channel-avatar entries
    # in `thumbnails`; for x.com it doesn't. avatar_url is best-effort.
    avatar = _pick_avatar(thumbs)

    ctx.manifest.channel = ChannelInfo(
        url=raw_url,
        channel_id=channel_id,
        title=uploader,
        handle=handle,
        avatar_url=avatar,
        banner_url=None,
        description=info.get("description"),
    )

    webpage_url = info.get("webpage_url") or raw_url
    record = VideoRecord(
        video_id=video_id,
        title=title,
        title_safe=safe_stem(title, video_id),
        url=webpage_url,
        source=["single"],
        view_count=info.get("view_count") or info.get("like_count"),
        published_at=info.get("upload_date"),
        duration_sec=info.get("duration"),
    )
    ctx.log(f"Resolved single video: {title!r} ({video_id}) by {uploader}")
    return record
