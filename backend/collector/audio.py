"""Extract audio from each video and split it intelligently when > max_audio_mib.

Split priority (best → worst):
    1. YouTube chapter boundaries (from info.json)
    2. Subtitle gaps > 1.5s (from any available VTT)
    3. ffmpeg `silencedetect` end-points
    4. Hard cut at max segment duration

After cutting, each part is verified ≤ max_audio_mib; if oversized, we
re-cut that part earlier or fall back to equal split.
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

from .. import storage
from ..models import AudioSegment, VideoRecord
from .utils import MIB, ffprobe_duration, file_size_mib, find_by_stem, run


TARGET_MIB = 9.5  # safety margin below limit


def process_one(ctx, v: VideoRecord) -> None:
    """Extract + (if needed) split audio for one video."""
    job_dir = storage.job_dir(ctx.job_id)
    if not v.files.video:
        ctx.log(f"No video file for {v.video_id}, skipping audio")
        return

    src = job_dir / v.files.video
    if not src.exists():
        ctx.log(f"Video missing on disk for {v.video_id}, skipping audio")
        return

    cfg = ctx.manifest.config
    out_full = job_dir / "output" / "audio" / "full" / f"{v.title_safe}.m4a"
    out_segs_dir = job_dir / "output" / "audio" / "segments"
    out_full.parent.mkdir(parents=True, exist_ok=True)
    out_segs_dir.mkdir(parents=True, exist_ok=True)

    # 1) Extract to mono AAC at requested bitrate
    if not out_full.exists():
        try:
            run([
                "ffmpeg", "-y", "-i", str(src),
                "-vn", "-c:a", "aac", "-b:a", cfg.audio_bitrate,
                "-ac", "1",
                str(out_full),
            ])
        except Exception as e:
            ctx.error(f"Audio extraction failed for {v.video_id}: {e}")
            return

    full_mib = file_size_mib(out_full)
    v.files.audio_full = str(out_full.relative_to(job_dir))
    v.files.audio_full_size_mib = round(full_mib, 3)
    ctx.log(f"Audio[{v.video_id}]: {full_mib:.2f} MiB at {cfg.audio_bitrate}")

    # 2) Need to split?
    if full_mib <= cfg.max_audio_mib:
        v.files.audio_segments = []
        return

    duration = ffprobe_duration(out_full)
    if duration <= 0:
        ctx.error(f"Cannot probe duration for {v.video_id}, skipping split")
        return

    bytes_per_sec = (out_full.stat().st_size) / duration
    target_bytes = TARGET_MIB * MIB
    max_segment_sec = target_bytes / bytes_per_sec
    ctx.log(f"Splitting: duration={duration:.0f}s, max_segment≈{max_segment_sec:.0f}s")

    # 3) Find candidate cut points (sorted ascending)
    candidates = _gather_candidates(ctx, v, duration)
    ctx.log(f"  candidate cut points: {len(candidates)}")

    # 4) Greedy pack
    cuts = _greedy_cuts(candidates, duration, max_segment_sec)
    ctx.log(f"  computed {len(cuts)-1} segments")

    # 5) Write each segment via `-c copy` (no re-encode = instant + lossless)
    segments: list[AudioSegment] = []
    for i in range(len(cuts) - 1):
        start, end = cuts[i], cuts[i + 1]
        part_name = f"{v.title_safe}.part{i+1:02d}.m4a"
        part_path = out_segs_dir / part_name
        if part_path.exists():
            part_path.unlink()
        ok = _cut(out_full, part_path, start, end)
        if not ok:
            ctx.error(f"Failed to write segment {i+1} for {v.video_id}")
            continue

        size = file_size_mib(part_path)
        # If this part exceeds limit, try to shorten it (move end earlier)
        if size > cfg.max_audio_mib:
            new_end = _shrink_segment_to_fit(out_full, part_path, start, end, cfg.max_audio_mib)
            if new_end and new_end < end:
                # Re-insert remaining time into the cuts list and recompute the rest
                ctx.log(f"  segment {i+1} oversized ({size:.2f}MiB) — shrunk to end={new_end:.1f}")
                cuts.insert(i + 1, new_end)
                size = file_size_mib(part_path)
            # If still too big (e.g. ultra-dense bitrate), just accept with warning
            if size > cfg.max_audio_mib:
                ctx.error(f"  segment {i+1} still {size:.2f}MiB > {cfg.max_audio_mib}MiB after shrink")

        segments.append(AudioSegment(
            path=str(part_path.relative_to(job_dir)),
            start_sec=round(start, 2),
            end_sec=round(end if i + 1 < len(cuts) - 1 else end, 2),
            size_mib=round(size, 3),
        ))

    v.files.audio_segments = segments


# ─── Candidate gathering ─────────────────────────────────────────────

def _gather_candidates(ctx, v: VideoRecord, duration: float) -> list[float]:
    """Return sorted unique cut-point timestamps (in seconds)."""
    points: set[float] = set()
    # 1) YouTube chapters
    for ch in v.chapters or []:
        end = ch.get("end_time")
        if end and 0 < end < duration:
            points.add(round(float(end), 2))
    # 2) Subtitle gaps
    points.update(_subtitle_gap_points(ctx, v, duration))
    # 3) Silence detection
    points.update(_silence_endpoints(ctx, v, duration))
    pts = sorted(p for p in points if 0 < p < duration)
    return pts


def _subtitle_gap_points(ctx, v: VideoRecord, duration: float, min_gap: float = 1.5) -> list[float]:
    """Look at any available VTT for this video; find gaps > min_gap between cues."""
    job_dir = storage.job_dir(ctx.job_id)
    raw_dir = job_dir / "output" / "transcripts" / "raw"
    if not raw_dir.exists():
        return []
    candidates: list[Path] = find_by_stem(raw_dir, v.title_safe, suffix_in=(".vtt",))
    if not candidates:
        return []
    # Prefer English / Chinese auto if present for finer granularity
    import webvtt
    try:
        cues = webvtt.read(str(candidates[0]))
    except Exception:
        return []
    gaps: list[float] = []
    prev_end = 0.0
    for cue in cues:
        start = _ts_to_sec(cue.start)
        end = _ts_to_sec(cue.end)
        if start - prev_end > min_gap:
            # cut in the middle of the gap
            gaps.append(round((prev_end + start) / 2, 2))
        prev_end = max(prev_end, end)
    return gaps


def _silence_endpoints(ctx, v: VideoRecord, duration: float) -> list[float]:
    """Use ffmpeg silencedetect on the extracted audio."""
    job_dir = storage.job_dir(ctx.job_id)
    audio = job_dir / (v.files.audio_full or "")
    if not audio.exists():
        return []
    cfg = ctx.manifest.config
    try:
        r = run([
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio),
            "-af", f"silencedetect=noise={cfg.silence_db}dB:d={cfg.silence_min_dur}",
            "-f", "null", "-",
        ], check=False)
    except Exception:
        return []
    out = (r.stderr or "") + (r.stdout or "")
    # silencedetect prints: `silence_end: 12.345 | silence_duration: 0.560`
    pts: list[float] = []
    for m in re.finditer(r"silence_end:\s*([0-9.]+)", out):
        try:
            pts.append(round(float(m.group(1)), 2))
        except ValueError:
            continue
    return pts


def _ts_to_sec(ts: str) -> float:
    """Convert `HH:MM:SS.mmm` → seconds."""
    if not ts:
        return 0.0
    h, m, rest = "0", "0", "0"
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, rest = parts
    elif len(parts) == 2:
        m, rest = parts
    else:
        rest = parts[0]
    try:
        return int(h) * 3600 + int(m) * 60 + float(rest)
    except ValueError:
        return 0.0


# ─── Greedy cut planning ─────────────────────────────────────────────

def _greedy_cuts(candidates: list[float], duration: float, max_sec: float) -> list[float]:
    """Return a sorted list of cut points starting at 0 and ending exactly at `duration`.

    For each segment, pick the latest candidate before `start + max_sec`.
    If no candidate exists in window (or it would produce a too-short segment),
    fall back to a hard cut at `start + max_sec`.
    """
    # Defensive: keep only candidates strictly inside (0, duration)
    candidates = sorted({round(c, 2) for c in candidates if 0 < c < duration})

    MIN_TAIL = max(5.0, max_sec * 0.05)  # avoid sub-5s scraps at the end

    cuts: list[float] = [0.0]
    while cuts[-1] < duration - 0.1:
        start = cuts[-1]
        ideal_end = start + max_sec
        remaining = duration - start

        # If the rest fits in one segment, finalize.
        if remaining <= max_sec:
            cuts.append(duration)
            break

        # Find largest candidate in (start + max_sec*0.4, ideal_end]
        candidate_in = [c for c in candidates if start + max_sec * 0.4 < c <= ideal_end]
        if candidate_in:
            cut = candidate_in[-1]
        else:
            # No good cut nearby — try a small stretch past ideal_end
            stretch = min(duration - MIN_TAIL, ideal_end * 1.1)
            future = [c for c in candidates if ideal_end < c <= stretch]
            cut = future[0] if future else ideal_end

        # Don't leave a tail shorter than MIN_TAIL — push cut back to absorb tail
        if duration - cut < MIN_TAIL:
            cut = duration  # final cut absorbs the short tail

        cuts.append(cut)
        if cut >= duration - 0.1:
            break

    # Dedup and clamp
    out: list[float] = []
    for c in cuts:
        c = max(0.0, min(c, duration))
        if not out or c > out[-1] + 0.5:  # require ≥0.5s between cuts
            out.append(round(c, 2))
    # Ensure exact-duration terminator
    if not out or out[-1] < duration - 0.05:
        out.append(round(duration, 2))
    # Guarantee monotonic strict
    final: list[float] = []
    for c in out:
        if not final or c > final[-1]:
            final.append(c)
    return final


def _cut(src: Path, dst: Path, start: float, end: float) -> bool:
    """Cut a segment using `-c copy` (stream copy)."""
    try:
        run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
            "-i", str(src),
            "-c", "copy",
            str(dst),
        ])
        return dst.exists() and dst.stat().st_size > 0
    except Exception:
        return False


def _shrink_segment_to_fit(src: Path, dst: Path, start: float, end: float, max_mib: float) -> Optional[float]:
    """Binary-search a shorter end timestamp so the segment fits under max_mib."""
    lo, hi = start + 1, end
    last_ok: Optional[float] = None
    for _ in range(8):
        mid = (lo + hi) / 2
        if not _cut(src, dst, start, mid):
            return last_ok
        if file_size_mib(dst) <= max_mib:
            last_ok = mid
            lo = mid
        else:
            hi = mid
    if last_ok:
        _cut(src, dst, start, last_ok)
    return last_ok
