"""Collect 3-5 photos of the creator.

Strategy:
1. Always save channel avatar + banner if present.
2. If InsightFace available AND avatar has a face → automatic mode:
   - Use avatar face as reference embedding
   - Score every video thumbnail (downloaded ones + extra from channel)
   - Pick top 3-5 by similarity (threshold 0.4)
3. If avatar has no recognizable face → write candidate faces from thumbnails
   to disk, mark job as `needs_face_confirmation` and stop. After user
   confirms, the orchestrator re-runs this with the saved reference face.
4. If InsightFace not installed → fall back to "unverified": avatar + banner
   + 3 highest-view thumbnails, flagged `unverified_`.
"""
from __future__ import annotations
import io
import json
import time
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
from PIL import Image
import yt_dlp

from .. import storage
from ..models import CreatorImage, FaceReference
from . import face as face_mod


SIM_THRESHOLD = 0.40
TARGET_MIN, TARGET_MAX = 3, 5
EXTRA_THUMBS_TO_SCAN = 30


def collect(ctx, force: bool = False) -> bool:
    """Returns True if the job is paused waiting for face confirmation."""
    job_dir = storage.job_dir(ctx.job_id)
    out_dir = job_dir / "output" / "creator_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    cand_dir = out_dir / "candidates"

    images: list[CreatorImage] = []
    channel = ctx.manifest.channel

    # 1) Avatar + banner
    avatar_path = _download_url(channel.avatar_url, out_dir / "01_avatar.jpg") if channel.avatar_url else None
    if avatar_path:
        images.append(CreatorImage(
            file=str(avatar_path.relative_to(job_dir)),
            source="channel_avatar",
            score=None,
        ))
        ctx.log("Saved channel avatar")
    banner_path = _download_url(channel.banner_url, out_dir / "02_banner.jpg") if channel.banner_url else None
    if banner_path:
        images.append(CreatorImage(
            file=str(banner_path.relative_to(job_dir)),
            source="channel_banner",
            score=None,
        ))
        ctx.log("Saved channel banner")

    # If face module unavailable → fall back to unverified mode
    if not face_mod.is_available():
        ctx.log("InsightFace not available, falling back to unverified mode")
        _fallback_unverified(ctx, images, out_dir)
        ctx.manifest.creator_images = images
        ctx.manifest.face_reference = FaceReference()
        return False

    # 2) Get reference embedding
    ref_emb = _load_reference_embedding(ctx, out_dir, avatar_path, force=force)
    if ref_emb is None:
        # Build candidate set for user confirmation
        ctx.log("No reference face — preparing candidates for user confirmation")
        cand_dir.mkdir(parents=True, exist_ok=True)
        _prepare_candidates(ctx, cand_dir)
        ctx.manifest.creator_images = images  # only avatar+banner so far
        ctx.manifest.face_reference = FaceReference(confirmed_by_user=False)
        return True  # needs confirmation

    # 3) Score thumbnails
    scored = _score_thumbnails(ctx, ref_emb)
    ctx.log(f"Face-scored {len(scored)} thumbnails")
    # Take top ones above threshold
    scored.sort(key=lambda x: x[2], reverse=True)
    picked = [s for s in scored if s[2] >= SIM_THRESHOLD][:TARGET_MAX]

    for i, (vid, thumb_path, score) in enumerate(picked, start=len(images) + 1):
        out_name = f"{i:02d}_thumb_score{score:.2f}.jpg"
        target = out_dir / out_name
        try:
            target.write_bytes(thumb_path.read_bytes())
            images.append(CreatorImage(
                file=str(target.relative_to(job_dir)),
                source="video_thumbnail",
                from_video=vid,
                score=round(score, 3),
            ))
        except Exception as e:
            ctx.log(f"Failed to copy thumb for {vid}: {e}")

    # If we still don't have TARGET_MIN, take best thumbnails regardless of threshold
    if len(images) < TARGET_MIN and scored:
        for vid, thumb_path, score in scored:
            if any(img.from_video == vid for img in images):
                continue
            out_name = f"{len(images)+1:02d}_thumb_score{score:.2f}.jpg"
            target = out_dir / out_name
            try:
                target.write_bytes(thumb_path.read_bytes())
                images.append(CreatorImage(
                    file=str(target.relative_to(job_dir)),
                    source="video_thumbnail",
                    from_video=vid,
                    score=round(score, 3),
                ))
            except Exception:
                pass
            if len(images) >= TARGET_MIN:
                break

    ctx.manifest.creator_images = images
    return False


# ─── Reference embedding ─────────────────────────────────────────────

def _load_reference_embedding(ctx, out_dir: Path, avatar_path: Optional[Path], *, force: bool) -> Optional[np.ndarray]:
    job_dir = storage.job_dir(ctx.job_id)
    ref_path = out_dir / "reference_face.npy"

    # If user already confirmed (or last run cached), use it.
    if ref_path.exists() and not force:
        try:
            return np.load(ref_path)
        except Exception:
            pass
    if ctx.manifest.face_reference.confirmed_by_user and ref_path.exists():
        try:
            return np.load(ref_path)
        except Exception:
            pass

    # Try avatar
    if avatar_path and avatar_path.exists():
        faces = face_mod.detect_faces(avatar_path)
        if faces:
            # Pick the largest face
            faces.sort(key=_face_area, reverse=True)
            emb = faces[0].embedding
            np.save(ref_path, emb)
            ctx.manifest.face_reference = FaceReference(
                embedding_file=str(ref_path.relative_to(job_dir)),
                source="channel_avatar",
                confirmed_by_user=False,
            )
            return emb
        else:
            ctx.log("Avatar has no detectable face")

    return None


def _face_area(f) -> float:
    """InsightFace `Face` exposes `bbox` (xyxy). Return pixel area."""
    bbox = getattr(f, "bbox", None)
    if bbox is None:
        return 0.0
    try:
        x1, y1, x2, y2 = bbox
        return max(0.0, (x2 - x1) * (y2 - y1))
    except Exception:
        return 0.0


# ─── Thumbnail scoring ───────────────────────────────────────────────

def _score_thumbnails(ctx, ref_emb: np.ndarray) -> list[tuple[str, Path, float]]:
    """Return (video_id, thumbnail_path, max_face_similarity) for every thumb we have.

    Uses the thumbnails we already downloaded; if fewer than 8 videos, also
    fetches additional thumbnail URLs from the channel.
    """
    job_dir = storage.job_dir(ctx.job_id)
    thumbs_dir = job_dir / "output" / "thumbnails"

    pairs: list[tuple[str, Path]] = []  # (video_id, thumb path)
    for v in ctx.manifest.videos:
        if v.files.thumbnail:
            tp = job_dir / v.files.thumbnail
            if tp.exists():
                pairs.append((v.video_id, tp))

    # Augment with extra thumbnails from the channel listing
    extra_pairs = _fetch_extra_thumbnails(ctx, exclude_ids={p[0] for p in pairs}, limit=EXTRA_THUMBS_TO_SCAN)
    pairs.extend(extra_pairs)

    results: list[tuple[str, Path, float]] = []
    for vid, path in pairs:
        try:
            faces = face_mod.detect_faces(path)
            if not faces:
                continue
            best = max(face_mod.cosine_sim(f.embedding, ref_emb) for f in faces)
            results.append((vid, path, best))
        except Exception as e:
            ctx.log(f"Score failed for {path.name}: {e}")
    return results


def _fetch_extra_thumbnails(ctx, exclude_ids: set[str], limit: int) -> list[tuple[str, Path]]:
    """Pull additional thumbnails (image only) for popular videos on the channel."""
    job_dir = storage.job_dir(ctx.job_id)
    extra_dir = job_dir / "output" / "thumbnails" / "extra"
    extra_dir.mkdir(parents=True, exist_ok=True)

    channel = ctx.manifest.channel
    if not channel.handle and not channel.channel_id:
        return []
    # Single-video / non-YouTube channels have nothing to enumerate.
    url_host = (channel.url or "").lower()
    is_youtube = "youtube.com" in url_host or "youtu.be" in url_host
    if not is_youtube:
        return []
    list_url = (
        f"https://www.youtube.com/channel/{channel.channel_id}/videos"
        if channel.channel_id and channel.channel_id.startswith("UC")
        else f"https://www.youtube.com/{channel.handle}/videos"
    )

    try:
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist",
            "playlist_items": f"1-{limit + len(exclude_ids)}",
            "ignoreerrors": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(list_url, download=False) or {}
        entries = info.get("entries") or []
    except Exception as e:
        ctx.log(f"Failed to fetch extra video list: {e}")
        return []

    out: list[tuple[str, Path]] = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        vid = e["id"]
        if vid in exclude_ids:
            continue
        # YouTube thumbnail URL convention
        url = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
        target = extra_dir / f"{vid}.jpg"
        if not target.exists():
            ok = _download_url(url, target)
            if not ok:
                # Fall back to default thumbnail
                _download_url(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", target)
        if target.exists():
            out.append((vid, target))
        if len(out) >= limit:
            break
    return out


# ─── Candidate prep (when avatar has no face) ────────────────────────

def _prepare_candidates(ctx, cand_dir: Path) -> None:
    """Detect faces across thumbnails, cluster, save up to 18 candidate crops + embeddings."""
    job_dir = storage.job_dir(ctx.job_id)
    cand_dir.mkdir(parents=True, exist_ok=True)
    # Clear stale candidates
    for old in cand_dir.glob("*"):
        try:
            old.unlink()
        except Exception:
            pass

    pairs: list[tuple[str, Path]] = []
    for v in ctx.manifest.videos:
        if v.files.thumbnail:
            tp = job_dir / v.files.thumbnail
            if tp.exists():
                pairs.append((v.video_id, tp))
    pairs.extend(_fetch_extra_thumbnails(ctx, exclude_ids={p[0] for p in pairs}, limit=EXTRA_THUMBS_TO_SCAN))

    # Detect all faces with embeddings
    all_faces: list[tuple[np.ndarray, np.ndarray, str]] = []  # (crop, embedding, source_video)
    for vid, path in pairs:
        try:
            img = Image.open(path).convert("RGB")
        except Exception:
            continue
        faces = face_mod.detect_faces(path)
        for f in faces:
            if _face_area(f) < 40 * 40:  # too small
                continue
            crop = _crop_face(img, f.bbox)
            if crop is None:
                continue
            all_faces.append((np.array(crop), f.embedding, vid))

    if not all_faces:
        ctx.log("No faces detected for candidate prep")
        return

    # Greedy clustering by cosine similarity
    clusters: list[dict] = []  # each: {"emb": avg, "members": [(crop,emb,vid)], "best_area": float}
    for crop, emb, vid in all_faces:
        placed = False
        for cl in clusters:
            if face_mod.cosine_sim(cl["emb"], emb) > 0.55:
                cl["members"].append((crop, emb, vid))
                # update average embedding
                n = len(cl["members"])
                cl["emb"] = (cl["emb"] * (n - 1) + emb) / n
                placed = True
                break
        if not placed:
            clusters.append({"emb": emb.copy(), "members": [(crop, emb, vid)]})

    # Rank by cluster size (occurrences)
    clusters.sort(key=lambda c: len(c["members"]), reverse=True)
    saved = 0
    for ci, cl in enumerate(clusters[:18]):
        # Pick the largest crop in cluster
        crop, emb, vid = max(cl["members"], key=lambda m: m[0].shape[0] * m[0].shape[1])
        face_id = f"f{ci:02d}_{int(time.time()*1000) % 1000000:06d}"
        img_pil = Image.fromarray(crop)
        # Make square for nicer display
        img_pil.thumbnail((256, 256))
        img_pil.save(cand_dir / f"{face_id}.jpg", quality=88)
        np.save(cand_dir / f"{face_id}.npy", emb)
        (cand_dir / f"{face_id}.meta.json").write_text(json.dumps({
            "occurrences": len(cl["members"]),
            "quality": float(crop.shape[0] * crop.shape[1]),
            "source_video": vid,
        }), encoding="utf-8")
        saved += 1
    ctx.log(f"Saved {saved} face candidates for confirmation")


def _crop_face(pil_image: Image.Image, bbox) -> Optional[Image.Image]:
    try:
        x1, y1, x2, y2 = [int(v) for v in bbox]
        w, h = pil_image.size
        # Expand bbox 30%
        pad_x = int((x2 - x1) * 0.3)
        pad_y = int((y2 - y1) * 0.3)
        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(w, x2 + pad_x)
        y2 = min(h, y2 + pad_y)
        return pil_image.crop((x1, y1, x2, y2))
    except Exception:
        return None


# ─── Fallback (no insightface) ───────────────────────────────────────

def _fallback_unverified(ctx, images: list[CreatorImage], out_dir: Path) -> None:
    """Just grab top 3 thumbnails by view count as 'unverified'."""
    job_dir = storage.job_dir(ctx.job_id)
    videos_by_views = sorted(
        [v for v in ctx.manifest.videos if v.files.thumbnail],
        key=lambda v: v.view_count or 0,
        reverse=True,
    )
    for i, v in enumerate(videos_by_views[:3], start=len(images) + 1):
        src = job_dir / v.files.thumbnail
        if not src.exists():
            continue
        target = out_dir / f"{i:02d}_unverified_{v.video_id}.jpg"
        target.write_bytes(src.read_bytes())
        images.append(CreatorImage(
            file=str(target.relative_to(job_dir)),
            source="video_thumbnail",
            from_video=v.video_id,
            score=None,
        ))


# ─── HTTP helpers ────────────────────────────────────────────────────

def _download_url(url: str, target: Path) -> Optional[Path]:
    if not url:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            r = c.get(url)
            if r.status_code != 200 or not r.content:
                return None
            target.write_bytes(r.content)
            # Re-encode to JPG to normalize
            try:
                im = Image.open(io.BytesIO(r.content)).convert("RGB")
                im.save(target, format="JPEG", quality=90)
            except Exception:
                pass
            return target
    except Exception:
        return None
