"""Resolve a YouTube channel URL → ChannelInfo (id, title, avatar, banner)."""
from __future__ import annotations
from typing import Any, Dict
from urllib.parse import urlparse

import yt_dlp

from ..models import ChannelInfo


def _normalize_channel_url(url: str) -> str:
    """Convert a video URL into a channel URL when possible.

    Accepts:
        https://www.youtube.com/@handle
        https://www.youtube.com/@handle/videos
        https://www.youtube.com/channel/UC...
        https://www.youtube.com/c/name
        https://www.youtube.com/user/name
        https://www.youtube.com/watch?v=...
        https://youtu.be/<id>

    Returns a URL that yt-dlp will treat as a channel listing.
    """
    u = url.strip()
    # Strip query params from non-watch URLs to avoid sort flags confusing yt-dlp
    parsed = urlparse(u)
    if parsed.netloc.endswith("youtu.be"):
        return u  # video URL — yt-dlp will give us channel_id via metadata
    if "/watch" in parsed.path or parsed.query.startswith("v="):
        return u
    # Channel-like URL
    path = parsed.path.rstrip("/")
    # If user gave .../@handle, append /videos for listing
    if path.startswith("/@") or path.startswith("/channel/") or path.startswith("/c/") or path.startswith("/user/"):
        if not path.endswith("/videos"):
            return f"{parsed.scheme}://{parsed.netloc}{path}/videos"
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    return u


def _ydl_extract(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    base = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }
    base.update(opts)
    with yt_dlp.YoutubeDL(base) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _pick_avatar(thumbnails: list[dict]) -> str | None:
    if not thumbnails:
        return None
    # Look for thumbnails that look like an avatar (square, id contains "avatar")
    avatar_likes = [t for t in thumbnails if "avatar" in (t.get("id") or "").lower()]
    pool = avatar_likes or [t for t in thumbnails if t.get("width") and t.get("height") and t["width"] == t["height"]]
    pool = pool or thumbnails
    return max(pool, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)).get("url")


def _pick_banner(thumbnails: list[dict]) -> str | None:
    if not thumbnails:
        return None
    banners = [t for t in thumbnails if "banner" in (t.get("id") or "").lower()]
    if banners:
        return max(banners, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0)).get("url")
    # Heuristic: very wide aspect ratio
    wide = [t for t in thumbnails if t.get("width") and t.get("height") and t["width"] >= 2 * t["height"]]
    if wide:
        return max(wide, key=lambda t: (t.get("width") or 0)).get("url")
    return None


def resolve(ctx) -> None:
    """Mutates ctx.manifest.channel with resolved info."""
    raw_url = ctx.manifest.channel.url
    norm = _normalize_channel_url(raw_url)
    ctx.log(f"Resolving channel: {norm}")

    # Two-step: first try as channel listing. If that fails, try as bare video to learn channel_id.
    info: Dict[str, Any] = _ydl_extract(norm, {"playlist_items": "0"})
    if not info or not info.get("channel_id"):
        ctx.log("First pass empty; trying without /videos suffix")
        info = _ydl_extract(raw_url, {"playlist_items": "0"})

    channel_id = info.get("channel_id") or info.get("uploader_id")
    title = info.get("channel") or info.get("uploader") or info.get("title")
    handle = info.get("uploader_id") or info.get("channel_url", "").rstrip("/").split("/")[-1]
    description = info.get("description")

    thumbs = info.get("thumbnails") or []
    avatar = _pick_avatar(thumbs)
    banner = _pick_banner(thumbs)

    # If still no channel_id, last resort: fetch root channel page without /videos
    if not channel_id:
        try:
            info2 = _ydl_extract(f"https://www.youtube.com/{handle}" if handle else raw_url, {"playlist_items": "0"})
            channel_id = channel_id or info2.get("channel_id")
            title = title or info2.get("channel")
            thumbs = thumbs or info2.get("thumbnails") or []
            avatar = avatar or _pick_avatar(thumbs)
            banner = banner or _pick_banner(thumbs)
        except Exception:
            pass

    if not channel_id:
        raise RuntimeError(f"Could not resolve channel from URL: {raw_url}")

    ctx.manifest.channel = ChannelInfo(
        url=raw_url,
        channel_id=channel_id,
        title=title,
        handle=handle,
        avatar_url=avatar,
        banner_url=banner,
        description=description,
    )
    ctx.log(f"Resolved channel: {title} ({channel_id})")
