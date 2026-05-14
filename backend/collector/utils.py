"""Shared utilities: filename safety, ffprobe, subprocess helpers."""
from __future__ import annotations
import json
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any


# ─── Filename safety ─────────────────────────────────────────────────

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_stem(title: str, video_id: str, max_len: int = 140) -> str:
    """Build a safe filename stem: `<sanitized title> [<video_id>]`.

    - Strip control chars + filesystem-unsafe characters.
    - Collapse whitespace.
    - Trim to `max_len` bytes (best-effort, on a char boundary).
    """
    t = unicodedata.normalize("NFC", title or "").strip()
    t = _UNSAFE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        t = "untitled"
    # Truncate by bytes (UTF-8) to be safe across filesystems
    encoded = t.encode("utf-8")
    if len(encoded) > max_len:
        # Walk back until we land on a valid utf-8 boundary
        encoded = encoded[:max_len]
        while encoded and (encoded[-1] & 0xC0) == 0x80:
            encoded = encoded[:-1]
        t = encoded.decode("utf-8", errors="ignore").rstrip()
    return f"{t} [{video_id}]"


# ─── Subprocess wrapper ──────────────────────────────────────────────

def run(cmd: list[str], *, check: bool = True, capture: bool = True, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        cwd=cwd,
    )


# ─── ffprobe helpers ─────────────────────────────────────────────────

def ffprobe_duration(path: str | Path) -> float:
    """Return duration in seconds, or 0.0 on failure."""
    try:
        r = run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ])
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0


def ffprobe_json(path: str | Path) -> dict[str, Any]:
    try:
        r = run([
            "ffprobe", "-v", "error",
            "-show_format", "-show_streams",
            "-of", "json",
            str(path),
        ])
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


# ─── Size helpers ────────────────────────────────────────────────────

MIB = 1024 * 1024


def file_size_mib(path: str | Path) -> float:
    try:
        return Path(path).stat().st_size / MIB
    except OSError:
        return 0.0


# ─── Safe globbing for files with brackets in their names ────────────
# `Path.glob` interprets `[abc]` as a character class, so a stem like
# `Title [video_id]` cannot be matched literally with glob. Use iterdir.

def find_by_stem(folder: Path, stem: str, suffix_in: tuple[str, ...] | None = None) -> list[Path]:
    """Return files in `folder` whose name starts with `stem + "."`.

    Optionally filter by file suffix (lowercased, with leading dot,
    e.g. `(".mp4", ".mkv")`).
    """
    if not folder.exists():
        return []
    prefix = stem + "."
    out: list[Path] = []
    for p in folder.iterdir():
        if not p.is_file():
            continue
        if not p.name.startswith(prefix):
            continue
        if suffix_in and p.suffix.lower() not in suffix_in:
            continue
        out.append(p)
    return out


def find_one_by_stem(folder: Path, stem: str, suffix_in: tuple[str, ...] | None = None) -> Path | None:
    matches = find_by_stem(folder, stem, suffix_in)
    return matches[0] if matches else None
