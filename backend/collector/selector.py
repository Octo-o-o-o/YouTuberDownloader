"""Pick latest N + most-viewed N videos from a channel and dedupe."""
from __future__ import annotations
from typing import Any, Dict, List

import yt_dlp

from ..models import VideoRecord
from .utils import safe_stem


def _channel_videos_url(handle_or_id: str) -> str:
    if handle_or_id.startswith("UC"):
        return f"https://www.youtube.com/channel/{handle_or_id}/videos"
    if handle_or_id.startswith("@"):
        return f"https://www.youtube.com/{handle_or_id}/videos"
    return f"https://www.youtube.com/@{handle_or_id}/videos"


def _fetch_flat(url: str, limit: int) -> List[Dict[str, Any]]:
    """Fast metadata-only pass. May miss view_count on some entries."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlist_items": f"1-{limit}",
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    return [e for e in (info.get("entries") or []) if e and e.get("id")]


def _fetch_detailed(url: str, limit: int) -> List[Dict[str, Any]]:
    """Slower pass — fetches per-video metadata. Used when flat pass lacks view_count."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "playlist_items": f"1-{limit}",
        "ignoreerrors": True,
        # IMPORTANT: don't flatten — we want full metadata
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    return [e for e in (info.get("entries") or []) if e and e.get("id")]


def _to_record(entry: Dict[str, Any], sources: list[str]) -> VideoRecord:
    vid = entry["id"]
    title = entry.get("title") or vid
    return VideoRecord(
        video_id=vid,
        title=title,
        title_safe=safe_stem(title, vid),
        url=f"https://www.youtube.com/watch?v={vid}",
        source=sources,
        view_count=entry.get("view_count"),
        published_at=entry.get("upload_date"),  # YYYYMMDD; converted on display
        duration_sec=entry.get("duration"),
    )


def select(ctx) -> None:
    """Populate ctx.manifest.videos with the chosen set (dedup by video_id)."""
    cfg = ctx.manifest.config
    channel = ctx.manifest.channel

    if cfg.latest_count <= 0 and cfg.popular_count <= 0:
        ctx.log("Both counts are zero — nothing to do.")
        return

    handle = channel.handle or channel.channel_id
    list_url = _channel_videos_url(handle)
    scan_size = max(cfg.metadata_scan_size, cfg.latest_count + cfg.popular_count + 5)

    ctx.log(f"Scanning up to {scan_size} videos from {list_url}")
    entries = _fetch_flat(list_url, scan_size)
    ctx.log(f"Flat scan returned {len(entries)} entries")

    # Filter Shorts if requested. Some flat entries report duration; many don't.
    def is_short(e: Dict[str, Any]) -> bool:
        d = e.get("duration")
        return bool(d and d <= 60)

    if cfg.skip_shorts:
        # Only filter ones we have duration for — won't drop unknowns
        entries = [e for e in entries if not is_short(e)]

    # Check view_count availability
    has_views = sum(1 for e in entries if e.get("view_count") is not None)
    if entries and has_views / max(len(entries), 1) < 0.7 and cfg.popular_count > 0:
        ctx.log(f"Only {has_views}/{len(entries)} have view_count; falling back to detailed fetch")
        entries = _fetch_detailed(list_url, scan_size)
        if cfg.skip_shorts:
            entries = [e for e in entries if not is_short(e)]

    # Sort:
    # 'latest' = the playlist's natural order (newest first on /videos)
    # 'popular' = view_count desc
    latest_pool = entries[: cfg.latest_count] if cfg.latest_count > 0 else []
    popular_pool: list[Dict[str, Any]] = []
    if cfg.popular_count > 0:
        with_views = [e for e in entries if e.get("view_count") is not None]
        with_views.sort(key=lambda e: e["view_count"], reverse=True)
        popular_pool = with_views[: cfg.popular_count]

    # Build merged list with source tags
    merged: dict[str, VideoRecord] = {}
    for e in latest_pool:
        merged[e["id"]] = _to_record(e, ["latest"])
    for e in popular_pool:
        if e["id"] in merged:
            if "popular" not in merged[e["id"]].source:
                merged[e["id"]].source.append("popular")
        else:
            merged[e["id"]] = _to_record(e, ["popular"])

    ctx.manifest.videos = list(merged.values())
    ctx.log(f"Selected {len(ctx.manifest.videos)} unique videos "
            f"(latest pool: {len(latest_pool)}, popular pool: {len(popular_pool)})")
