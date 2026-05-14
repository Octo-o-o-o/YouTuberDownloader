"""Convert downloaded VTT subtitles into 4 language-specific md files.

For each language in config.languages, write
    output/transcripts/<title_safe>.<lang>.md
selecting the best available variant (manual > auto, primary code > regional).
If no subtitle is available for that language, no md file is created and the
manifest records `None` for that language.
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

import webvtt

from .. import storage
from ..models import VideoRecord, VideoTranscript
from . import transcribe as asr
from .utils import find_by_stem


# Language priority — first match wins.
LANG_VARIANTS: dict[str, list[str]] = {
    "en": ["en", "en-US", "en-GB", "en-orig"],
    "zh": ["zh-Hans", "zh", "zh-CN", "zh-Hant", "zh-TW", "zh-HK"],
    "ja": ["ja", "ja-JP"],
    "ko": ["ko", "ko-KR"],
}


def process_one(ctx, v: VideoRecord) -> None:
    job_dir = storage.job_dir(ctx.job_id)
    raw_dir = job_dir / "output" / "transcripts" / "raw"
    out_dir = job_dir / "output" / "transcripts"
    cfg = ctx.manifest.config
    stem = v.title_safe
    out_dir.mkdir(parents=True, exist_ok=True)

    available = _index_available(raw_dir, stem) if raw_dir.exists() else []

    missing_langs: list[str] = []
    for lang in cfg.languages:
        lang = lang.strip().lower()
        if lang not in LANG_VARIANTS:
            ctx.log(f"Unknown language '{lang}', skipping")
            v.files.transcripts[lang] = None
            continue

        chosen = _choose(available, lang)
        if not chosen:
            v.files.transcripts[lang] = None
            missing_langs.append(lang)
            continue

        variant, kind, vtt_path = chosen
        md_name = f"{stem}.{lang}.md"
        md_path = out_dir / md_name
        try:
            text = _vtt_to_markdown(vtt_path, v=v, lang=lang, source_kind=kind, variant=variant)
            md_path.write_text(text, encoding="utf-8")
            v.files.transcripts[lang] = VideoTranscript(
                path=str(md_path.relative_to(job_dir)),
                source=kind,
                raw_path=str(vtt_path.relative_to(job_dir)),
            )
            ctx.log(f"Subtitles[{lang}] for {v.video_id}: {kind} ({variant})")
        except Exception as e:
            ctx.error(f"Failed to convert subtitle for {v.video_id} [{lang}]: {e}")
            v.files.transcripts[lang] = None
            missing_langs.append(lang)

    # ─── ASR fallback for missing languages ───────────────────────
    if cfg.asr_fallback and missing_langs and v.files.audio_full:
        _run_asr_fallback(ctx, v, missing_langs, out_dir, job_dir)


# ─── ASR fallback ────────────────────────────────────────────────────

def _run_asr_fallback(ctx, v: VideoRecord, missing_langs: list[str], out_dir, job_dir) -> None:
    """If user enabled `asr_fallback`, run Whisper on the extracted audio
    and fill in the missing language ONLY if Whisper's detected language
    matches one of the missing requested languages.

    Whisper cannot translate to arbitrary target languages, so we only
    fill in the *native* language of the audio (e.g. a Korean video with
    no subs gets a `ko.md`, not a `zh.md`).
    """
    if not asr.is_available():
        ctx.log(f"ASR fallback requested but faster-whisper not installed (skipping {v.video_id})")
        return

    cfg = ctx.manifest.config
    audio_full = job_dir / v.files.audio_full
    if not audio_full.exists():
        ctx.log(f"ASR fallback: audio file not found for {v.video_id}")
        return

    ctx.log(f"ASR fallback: transcribing {v.video_id} with model={cfg.asr_model}")
    result = asr.transcribe(audio_full, model_size=cfg.asr_model)
    if not result or not result.get("segments"):
        ctx.error(f"ASR fallback produced no output for {v.video_id}")
        return

    detected_raw = result.get("language") or ""
    canonical = result.get("canonical")
    ctx.log(f"ASR fallback: detected language={detected_raw} (canonical={canonical}, conf={result.get('language_probability', 0):.2f})")

    if not canonical:
        ctx.log(f"ASR fallback: detected language '{detected_raw}' is outside our 4 supported codes")
        return
    if canonical not in missing_langs:
        ctx.log(f"ASR fallback: language '{canonical}' was already provided by YouTube; skipping")
        return

    # Build the markdown
    stem = v.title_safe
    md_path = out_dir / f"{stem}.{canonical}.md"
    lines = asr.segments_to_md_lines(result["segments"])

    header = [
        f"# {v.title}",
        "",
        f"- **Video ID**: {v.video_id}",
        f"- **URL**: {v.url}",
        f"- **Language**: {canonical} (detected: {detected_raw})",
        f"- **Subtitle Source**: asr (faster-whisper {cfg.asr_model})",
    ]
    if v.duration_sec:
        header.append(f"- **Duration**: {_fmt_duration(v.duration_sec)}")
    if v.published_at:
        header.append(f"- **Published**: {_pretty_date(v.published_at)}")
    header += ["", "---", ""]
    body = [f"[{ts}] {text}" for ts, text in lines]
    md_path.write_text("\n".join(header + body) + "\n", encoding="utf-8")

    v.files.transcripts[canonical] = VideoTranscript(
        path=str(md_path.relative_to(job_dir)),
        source="asr",
        raw_path=None,
    )
    ctx.log(f"ASR fallback wrote {canonical}.md for {v.video_id} ({len(lines)} lines)")


# ─── Discovery ───────────────────────────────────────────────────────

def _index_available(raw_dir: Path, stem: str) -> list[tuple[str, str, Path]]:
    """Find all VTT files for this video stem.

    yt-dlp names subtitles like: `<stem>.<lang>.vtt` for both manual and auto.
    Distinguishing manual vs auto isn't reliable from filename alone, so we
    rely on yt-dlp's behavior: it writes auto-subs only when no manual exists
    for the language, OR when both are explicitly requested. Since we requested
    both, the same lang code may have a manual one. Heuristic:
    - YouTube auto-sub filenames usually carry `-orig` or are present alongside
      a non-auto variant in the same language family.

    Practical approach: collect everything, mark `manual` if title doesn't end
    with `-orig`/`-auto`, else `auto`. yt-dlp doesn't add `-auto`, so we treat
    everything as `manual` unless we have positive evidence otherwise.
    Real auto-generated subs in yt-dlp normally have `-orig` suffix when the
    user explicitly requested both. We use `_kind_from_name`.
    """
    results: list[tuple[str, str, Path]] = []
    for p in find_by_stem(raw_dir, stem, suffix_in=(".vtt",)):
        # Extract the language tag — everything between the stem and `.vtt`
        rest = p.name[len(stem) + 1 : -4]  # strip "stem." and ".vtt"
        if not rest:
            continue
        kind = "auto" if rest.endswith("-orig") or rest.endswith("-auto") else "manual"
        variant = rest.removesuffix("-orig").removesuffix("-auto")
        results.append((variant, kind, p))
    return results


def _choose(available: list[tuple[str, str, Path]], lang: str) -> Optional[tuple[str, str, Path]]:
    """Pick the best subtitle for a language.

    Priority:
        1. manual + preferred variant (in order)
        2. auto    + preferred variant (in order)
    """
    if not available:
        return None
    variants = LANG_VARIANTS[lang]
    # Match by case-insensitive prefix
    def matches(variant: str, target: str) -> bool:
        return variant.lower() == target.lower()

    # Pass 1: manual
    for target in variants:
        for var, kind, p in available:
            if kind == "manual" and matches(var, target):
                return var, kind, p
    # Pass 2: auto
    for target in variants:
        for var, kind, p in available:
            if kind == "auto" and matches(var, target):
                return var, kind, p
    # Pass 3: any variant starting with the lang prefix (e.g., "en-orig" requested as "en")
    for var, kind, p in available:
        if var.lower().startswith(lang + "-") or var.lower() == lang:
            return var, kind, p
    return None


# ─── VTT → Markdown ──────────────────────────────────────────────────

def _vtt_to_markdown(
    path: Path,
    *,
    v: VideoRecord,
    lang: str,
    source_kind: str,
    variant: str,
) -> str:
    """Parse VTT and emit a markdown document.

    Handles YouTube auto-sub quirks:
    - Captions often roll: the same line appears in two consecutive cues with
      a shifting time window. We dedupe by keeping only new text from each cue.
    - Inline tags like <c.color5>...</c> and <00:00:01.234> are stripped.
    """
    cues = webvtt.read(str(path))

    lines: list[tuple[str, str]] = []  # (timestamp, text)
    last_full = ""

    for cue in cues:
        raw = cue.text or ""
        # Strip inline timestamp/color tags
        cleaned = _strip_inline_tags(raw)
        # Collapse internal newlines
        cleaned = re.sub(r"\s+\n+", "\n", cleaned).strip()
        if not cleaned:
            continue

        # Dedup: drop lines fully contained in the previous output
        if cleaned == last_full:
            continue
        if last_full and cleaned in last_full:
            continue
        # If previous cue is a prefix of this one (rolling captions), keep only the new tail
        if last_full and cleaned.startswith(last_full):
            tail = cleaned[len(last_full):].strip()
            if tail:
                ts = _fmt_ts(cue.start)
                lines.append((ts, tail))
                last_full = cleaned
            continue

        ts = _fmt_ts(cue.start)
        lines.append((ts, cleaned))
        last_full = cleaned

    # Merge consecutive cues with the same timestamp (cosmetic)
    merged: list[tuple[str, str]] = []
    for ts, text in lines:
        if merged and merged[-1][0] == ts:
            merged[-1] = (ts, merged[-1][1] + " " + text)
        else:
            merged.append((ts, text))

    # Build markdown
    header = [
        f"# {v.title}",
        "",
        f"- **Video ID**: {v.video_id}",
        f"- **URL**: {v.url}",
        f"- **Language**: {lang} ({variant})",
        f"- **Subtitle Source**: {source_kind}",
    ]
    if v.duration_sec:
        header.append(f"- **Duration**: {_fmt_duration(v.duration_sec)}")
    if v.published_at:
        header.append(f"- **Published**: {_pretty_date(v.published_at)}")
    header += ["", "---", ""]

    body = [f"[{ts}] {text}" for ts, text in merged]

    return "\n".join(header + body) + "\n"


# ─── Helpers ─────────────────────────────────────────────────────────

_INLINE_TAG = re.compile(r"<[^>]+>")


def _strip_inline_tags(text: str) -> str:
    return _INLINE_TAG.sub("", text)


def _fmt_ts(ts: str) -> str:
    """webvtt cue.start is `HH:MM:SS.mmm` — trim to `HH:MM:SS`."""
    if not ts:
        return "00:00:00"
    return ts.split(".")[0]


def _fmt_duration(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _pretty_date(yyyymmdd: str) -> str:
    if not yyyymmdd or len(yyyymmdd) < 8:
        return yyyymmdd
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
