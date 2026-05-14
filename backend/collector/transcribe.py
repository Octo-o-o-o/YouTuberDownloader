"""faster-whisper wrapper for ASR fallback when YouTube subtitles are missing.

Whisper transcribes audio in its **native** language. It cannot translate from
one non-English language to another. So this module:
  - Auto-detects audio language
  - Returns segments with timestamps
  - Maps detected language → one of our 4 canonical codes (en/zh/ja/ko)

Models (downloaded on first use; cached under ~/.cache/huggingface):
  - tiny    (~75MB)   — fast, ok for English
  - base    (~150MB)  — fast, decent
  - small   (~500MB)  — DEFAULT — good CJK support, 2-5× realtime CPU
  - medium  (~1.5GB)  — best quality, slower
  - large-v3 (~3GB)   — top tier
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Optional

# Module-level cache (one model per process)
_MODELS: dict[str, Any] = {}
_LOAD_ERROR: Optional[str] = None


def is_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _load_model(size: str):
    """Load (and cache) a Whisper model. Returns (model_or_None, error_str_or_None)."""
    global _LOAD_ERROR
    if size in _MODELS:
        return _MODELS[size], None
    if _LOAD_ERROR is not None:
        return None, _LOAD_ERROR
    try:
        from faster_whisper import WhisperModel
        # int8 quantization on CPU: 2-3× smaller memory + faster, almost no quality loss
        model = WhisperModel(size, device="cpu", compute_type="int8")
        _MODELS[size] = model
        return model, None
    except Exception as e:
        import traceback
        _LOAD_ERROR = f"{type(e).__name__}: {e}"
        # Print full traceback once so the user can copy/paste it
        print(f"[transcribe] failed to load Whisper model '{size}':")
        traceback.print_exc()
        return None, _LOAD_ERROR


# Map Whisper's detected ISO-639-1 codes to our 4 canonical codes
LANG_MAP: dict[str, str] = {
    "en": "en",
    "zh": "zh",
    "ja": "ja",
    "ko": "ko",
    # Whisper sometimes returns specific variants
    "yue": "zh",  # Cantonese → zh
}


def canonical_lang(detected: str) -> Optional[str]:
    if not detected:
        return None
    return LANG_MAP.get(detected.lower())


def transcribe(audio_path: str | Path, *, model_size: str = "small", beam_size: int = 1) -> dict:
    """Run ASR on an audio file. Always returns a dict.

    Success:
        {
          "language":   detected ISO-639-1 (raw Whisper code, e.g. "en", "zh", "yue"),
          "canonical":  canonical code we use (en/zh/ja/ko) or None if unsupported,
          "duration":   audio duration in seconds (float),
          "segments":   list of {start, end, text}
        }

    Failure (so the caller can decide what to do):
        {"error": "<reason>", "phase": "load_model" | "transcribe"}
    """
    model, load_err = _load_model(model_size)
    if model is None:
        return {"error": load_err or "unknown model load failure", "phase": "load_model"}
    try:
        segments_gen, info = model.transcribe(
            str(audio_path),
            beam_size=beam_size,
            vad_filter=True,        # skip silence regions, faster + better
            vad_parameters={"min_silence_duration_ms": 500},
        )
        # Materialize the generator (lazy by default)
        segs: list[dict] = []
        for s in segments_gen:
            segs.append({"start": float(s.start), "end": float(s.end), "text": (s.text or "").strip()})
        return {
            "language": info.language,
            "canonical": canonical_lang(info.language),
            "duration": float(info.duration),
            "language_probability": float(info.language_probability),
            "segments": segs,
        }
    except Exception as e:
        import traceback
        print(f"[transcribe] transcription failed on {audio_path}:")
        traceback.print_exc()
        return {"error": f"{type(e).__name__}: {e}", "phase": "transcribe"}


def segments_to_md_lines(segments: list[dict]) -> list[tuple[str, str]]:
    """Convert Whisper segments → (timestamp, text) lines matching subtitles.py output format."""
    out: list[tuple[str, str]] = []
    last_text = ""
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text or text == last_text:
            continue
        sec = max(0.0, float(s.get("start") or 0.0))
        ts = _fmt_ts(sec)
        out.append((ts, text))
        last_text = text
    return out


def _fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
