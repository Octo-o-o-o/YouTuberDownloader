"""InsightFace wrapper. Lazy-loaded to avoid hard dependency at import time."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional, Any

import numpy as np

from .. import storage
from ..models import FaceCandidate


# Cached model handle (per process)
_APP = None
_INIT_ERROR: Optional[str] = None


def _get_app():
    """Lazy-init the FaceAnalysis app. Returns None if unavailable."""
    global _APP, _INIT_ERROR
    if _APP is not None:
        return _APP
    if _INIT_ERROR is not None:
        return None
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _APP = app
        return app
    except Exception as e:  # pragma: no cover
        _INIT_ERROR = str(e)
        return None


def is_available() -> bool:
    return _get_app() is not None


# ─── Embeddings & detection ──────────────────────────────────────────

def detect_faces(image_path: str | Path) -> list[Any]:
    """Return InsightFace `Face` objects detected in the image. Empty list on failure."""
    import cv2  # noqa
    app = _get_app()
    if app is None:
        return []
    try:
        img = _load_bgr(image_path)
        if img is None:
            return []
        return app.get(img) or []
    except Exception:
        return []


def _load_bgr(path: str | Path):
    """Load image as BGR ndarray. Uses Pillow + numpy to avoid cv2 dependency."""
    try:
        from PIL import Image
        im = Image.open(str(path)).convert("RGB")
        arr = np.array(im)[:, :, ::-1]  # RGB → BGR
        return arr
    except Exception:
        return None


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None:
        return 0.0
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ─── Candidate face cache for confirm flow ───────────────────────────

def list_candidates(job_id: str) -> list[FaceCandidate]:
    """Read previously-saved candidate faces from disk.

    These are written by `images.collect()` when face confirmation is needed.
    """
    cand_dir = storage.job_dir(job_id) / "output" / "creator_images" / "candidates"
    if not cand_dir.exists():
        return []
    items: list[FaceCandidate] = []
    for p in sorted(cand_dir.glob("*.jpg")):
        face_id = p.stem
        meta_path = cand_dir / f"{face_id}.meta.json"
        occ = 1
        quality = 0.0
        if meta_path.exists():
            import json
            try:
                d = json.loads(meta_path.read_text())
                occ = d.get("occurrences", 1)
                quality = d.get("quality", 0.0)
            except Exception:
                pass
        items.append(FaceCandidate(
            face_id=face_id,
            crop=str(p.relative_to(storage.job_dir(job_id))),
            occurrences=occ,
            quality=quality,
        ))
    # Sort by quality desc, then occurrences desc
    items.sort(key=lambda c: (c.quality, c.occurrences), reverse=True)
    return items


def confirm(job_id: str, face_id: str) -> None:
    """Promote a candidate face's embedding to the job's reference face."""
    cand_dir = storage.job_dir(job_id) / "output" / "creator_images" / "candidates"
    emb_path = cand_dir / f"{face_id}.npy"
    if not emb_path.exists():
        raise FileNotFoundError(f"no embedding for face_id={face_id}")
    target_dir = storage.job_dir(job_id) / "output" / "creator_images"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_emb = target_dir / "reference_face.npy"
    target_emb.write_bytes(emb_path.read_bytes())

    manifest = storage.read_manifest(job_id)
    if manifest:
        manifest.face_reference.embedding_file = str(target_emb.relative_to(storage.job_dir(job_id)))
        manifest.face_reference.source = "user_confirmed"
        manifest.face_reference.confirmed_by_user = True
        storage.write_manifest(manifest)
