"""Command-line interface for YT Creator Archive.

Runs the same pipeline as the web UI but synchronously in the foreground,
streaming stage-by-stage progress to stdout. Writes to the same
`data/jobs/<job_id>/` layout, so jobs created via CLI show up in the web
UI automatically.

Usage:
    python -m backend.cli https://www.youtube.com/@CHANNEL
    python -m backend.cli @TomScottGo --latest 3 --popular 0 --asr
    python -m backend.cli URL --retry JOB_ID

Run `python -m backend.cli --help` for all options.
"""
from __future__ import annotations
import argparse
import re
import sys
import time
from datetime import datetime, timezone

from . import storage
from .models import (
    ChannelInfo,
    JobIndexEntry,
    JobManifest,
    JobOptions,
    JobStatusFile,
)


# ─── ANSI colors ─────────────────────────────────────────────────────

class C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    SIENNA = "\033[38;5;130m"
    TAPE = "\033[38;5;160m"
    R = "\033[0m"


def disable_colors() -> None:
    for k in ("BOLD", "DIM", "GREEN", "RED", "YELLOW", "SIENNA", "TAPE", "R"):
        setattr(C, k, "")


# ─── Helpers ─────────────────────────────────────────────────────────

def _normalize_channel_url(s: str) -> str:
    """Allow bare @handle or channel/UC... in addition to full URLs."""
    s = s.strip()
    if s.startswith("http"):
        return s
    if s.startswith("@"):
        return f"https://www.youtube.com/{s}"
    if s.startswith("UC") and len(s) > 20:
        return f"https://www.youtube.com/channel/{s}"
    return s


def _make_job_id(channel_url: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", channel_url.split("/")[-1] or "channel")[:32]
    if not slug:
        slug = "channel"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"job_{ts}_{slug}"


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _make_options(args: argparse.Namespace) -> JobOptions:
    return JobOptions(
        latest_count=args.latest,
        popular_count=args.popular,
        audio_bitrate=args.bitrate,
        max_audio_mib=args.max_audio_mib,
        languages=[s.strip().lower() for s in args.langs.split(",") if s.strip()],
        skip_shorts=not args.include_shorts,
        quality_cap=args.quality,
        metadata_scan_size=args.scan_size,
        asr_fallback=args.asr,
        asr_model=args.asr_model,
    )


# ─── CLI-aware JobContext ────────────────────────────────────────────

class CLIJobContext:
    """Drop-in replacement for jobs.JobContext that prints to stdout.

    Implements just the surface that collector modules call:
      - .log(msg)        — verbose info
      - .error(msg)      — record + show error
      - .job_id          — used by collectors for path resolution
      - .manifest        — collectors mutate this
      - .stage_progress  — no-op (we print per-row instead)
      - .update_status   — no-op
      - .save()          — persist manifest + status to disk
    """

    def __init__(self, job_id: str, manifest: JobManifest, *, verbose: bool):
        self.job_id = job_id
        self.manifest = manifest
        self.verbose = verbose
        self._stage_t = 0.0
        self._row_t = 0.0
        self._row_open = False

    # ── Collector-facing API ─────────────────────────────────────
    def log(self, msg: str) -> None:
        # Always persist to file log
        storage.append_log(self.job_id, msg)
        if self.verbose:
            # If a row line is currently open, push the verbose line below it
            if self._row_open:
                sys.stdout.write("\n")
                self._row_open = False
            print(f"     {C.DIM}{msg}{C.R}")

    def error(self, msg: str) -> None:
        storage.append_log(self.job_id, f"ERROR: {msg}")
        self.manifest.errors.append(msg)
        if self._row_open:
            sys.stdout.write("\n")
            self._row_open = False
        print(f"     {C.RED}× {msg}{C.R}")

    def stage_progress(self, current: int, total: int | None = None, action: str = "") -> None:
        # Mirror to status.json so the web UI (if open) reflects CLI progress
        st = storage.read_status(self.job_id) or JobStatusFile(job_id=self.job_id)
        st.status = "running"
        if total is not None:
            st.stage_progress = {"current": current, "total": total}
        if action:
            st.current_action = action
        storage.write_status(st)

    def update_status(self, **kw) -> None:
        st = storage.read_status(self.job_id) or JobStatusFile(job_id=self.job_id)
        for k, v in kw.items():
            if hasattr(st, k):
                setattr(st, k, v)
        storage.write_status(st)
        storage.sync_index_from_job(self.job_id)

    def save(self) -> None:
        storage.write_manifest(self.manifest)
        storage.sync_index_from_job(self.job_id)

    # ── CLI-only presentation helpers ────────────────────────────
    def header(self, num: str, name: str) -> None:
        self._stage_t = time.time()
        print(f"\n{C.BOLD}{num} ▸ {name}{C.R}")
        self.update_status(stage=name.lower().split()[0].rstrip("…"), current_action=f"{name} starting")

    def ok(self, msg: str = "") -> None:
        dt = time.time() - self._stage_t
        print(f"   {C.GREEN}✓{C.R} {msg}  {C.DIM}({_format_elapsed(dt)}){C.R}")

    def warn(self, msg: str) -> None:
        print(f"   {C.YELLOW}! {msg}{C.R}")

    def row_start(self, i: int, total: int, info: str) -> None:
        self._row_t = time.time()
        if self.verbose:
            sys.stdout.write(f"   [{i:>2}/{total}] {info}")
            sys.stdout.flush()
            self._row_open = True
        else:
            sys.stdout.write(".")
            sys.stdout.flush()

    def row_end(self, ok: bool, info: str = "") -> None:
        dt = time.time() - self._row_t
        if self.verbose:
            mark = f"{C.GREEN}✓{C.R}" if ok else f"{C.RED}×{C.R}"
            # If a log line was printed between row_start and row_end, the prefix is gone.
            # In that case, write a fresh line with the mark.
            if self._row_open:
                sys.stdout.write(f"  {mark} {info}  {C.DIM}({_format_elapsed(dt)}){C.R}\n")
            else:
                sys.stdout.write(f"          {mark} {info}  {C.DIM}({_format_elapsed(dt)}){C.R}\n")
            self._row_open = False

    def row_skip(self, i: int, total: int, reason: str) -> None:
        if self.verbose:
            print(f"   [{i:>2}/{total}] {C.DIM}skipped: {reason}{C.R}")


# ─── The pipeline ────────────────────────────────────────────────────

def run_pipeline(ctx: CLIJobContext) -> None:
    """Run all stages sequentially. Mutates ctx.manifest in place."""
    # Imported lazily so `--help` doesn't pay the import cost
    from .collector import audio, channel, downloader, images, selector, single_video, subtitles

    manifest = ctx.manifest
    opts = manifest.config
    single_mode = single_video.is_single_video_url(manifest.channel.url)

    # ── 01 Resolve channel (or single-video metadata) ───────────
    if single_mode:
        ctx.header("01", "Resolve single video")
        record = single_video.resolve(ctx)
        ctx.ok(f"{manifest.channel.title} — {record.title[:60]}")
    else:
        ctx.header("01", "Resolve channel")
        channel.resolve(ctx)
        ctx.ok(f"{manifest.channel.title} ({manifest.channel.channel_id})")
    ctx.save()

    # ── 02 Select videos (skipped in single-video mode) ─────────
    ctx.header("02", "Select videos")
    if single_mode:
        manifest.videos = [record]
        ctx.ok("single video — selector skipped")
    else:
        selector.select(ctx)
        if not manifest.videos:
            ctx.warn("no videos selected — nothing to do")
            manifest.status = "completed"
            return
        sources = " · ".join(
            f"{tag}={sum(1 for v in manifest.videos if tag in v.source)}"
            for tag in ("latest", "popular")
        )
        ctx.ok(f"{len(manifest.videos)} unique  ({sources})")
    ctx.save()

    total = len(manifest.videos)

    # ── 03 Download media ────────────────────────────────────────
    ctx.header("03", "Download media")
    for i, v in enumerate(manifest.videos):
        title_short = (v.title or v.video_id)[:55]
        ctx.row_start(i + 1, total, f"{v.video_id}  {title_short}")
        downloader.download_one(ctx, v)
        size = ""
        if v.files.video:
            try:
                from .collector.utils import file_size_mib
                size = f"{file_size_mib(storage.job_dir(ctx.job_id) / v.files.video):.1f} MiB"
            except Exception:
                pass
        ctx.row_end(ok=bool(v.files.video), info=size)
        ctx.save()

    # ── 04 Extract audio & split ─────────────────────────────────
    ctx.header("04", "Extract audio & split")
    for i, v in enumerate(manifest.videos):
        if not v.files.video:
            ctx.row_skip(i + 1, total, "no video on disk")
            continue
        ctx.row_start(i + 1, total, v.video_id)
        audio.process_one(ctx, v)
        size_mib = v.files.audio_full_size_mib or 0
        n_parts = len(v.files.audio_segments)
        info = f"{size_mib:.2f} MiB"
        if n_parts:
            info += f"  →  {n_parts} parts"
        ctx.row_end(ok=bool(v.files.audio_full), info=info)
        ctx.save()

    # ── 05 Process subtitles ─────────────────────────────────────
    asr_tag = " (+ ASR)" if opts.asr_fallback else ""
    ctx.header("05", f"Process subtitles{asr_tag}")
    for i, v in enumerate(manifest.videos):
        ctx.row_start(i + 1, total, v.video_id)
        subtitles.process_one(ctx, v)
        avail = []
        for lang in opts.languages:
            t = v.files.transcripts.get(lang)
            if t:
                avail.append(f"{lang}:{t.source}")
            else:
                avail.append(f"{C.DIM}{lang}:—{C.R}")
        ctx.row_end(ok=any(v.files.transcripts.get(l) for l in opts.languages), info="  ".join(avail))
        ctx.save()

    # ── 06 Identify creator photos ───────────────────────────────
    ctx.header("06", "Identify creator photos")
    needs_face = images.collect(ctx)
    ctx.save()
    if needs_face:
        ctx.warn("avatar has no detectable face — open web UI to pick the creator")
        manifest.status = "needs_face_confirmation"
    else:
        ctx.ok(f"{len(manifest.creator_images)} photos")
        manifest.status = "completed"

    manifest.completed_at = storage.now_iso()


# ─── Entry point ─────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli",
        description="YT Creator Archive — collect a YouTube creator's videos, subtitles, audio, and identifying photos.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("channel_url", help='YouTube channel URL / "@handle" / "UC..." ID, or a single video URL (YouTube watch/shorts/live, youtu.be, x.com/<user>/status/<id>)')
    parser.add_argument("--latest",       type=int,   default=5,  help="number of newest videos")
    parser.add_argument("--popular",      type=int,   default=5,  help="number of most-viewed videos")
    parser.add_argument("--langs",        type=str,   default="en,zh,ja,ko", help="comma-separated subtitle languages")
    parser.add_argument("--bitrate",      type=str,   default="64k", help="audio bitrate")
    parser.add_argument("--max-audio-mib", dest="max_audio_mib", type=float, default=10.0, help="max size per audio segment (MiB)")
    parser.add_argument("--quality",      type=int,   default=1080, help="max video resolution (height)")
    parser.add_argument("--scan-size",    type=int,   default=80,   help="how many recent videos to scan for selection")
    parser.add_argument("--include-shorts", action="store_true",   help="don't filter out Shorts (≤60s)")
    parser.add_argument("--asr",          action="store_true",     help="enable Whisper ASR fallback for missing subtitles (requires faster-whisper)")
    parser.add_argument("--asr-model",    type=str, default="small",
                        choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="Whisper model size when --asr is enabled")
    parser.add_argument("--retry",        type=str, default=None, metavar="JOB_ID",
                        help="resume an existing job by id (ignores most other flags)")
    parser.add_argument("-q", "--quiet",  action="store_true",     help="suppress per-step output (one dot per row)")
    parser.add_argument("--no-color",     action="store_true",     help="disable ANSI colors")
    args = parser.parse_args(argv)

    if args.no_color or not sys.stdout.isatty():
        disable_colors()

    # Retry path
    if args.retry:
        return _run_retry(args.retry, verbose=not args.quiet)

    # New job
    channel_url = _normalize_channel_url(args.channel_url)
    options = _make_options(args)
    job_id = _make_job_id(channel_url)

    # Bootstrap manifest
    storage.ensure_job_dir(job_id)
    manifest = JobManifest(
        job_id=job_id,
        channel=ChannelInfo(url=channel_url),
        config=options,
        status="running",
        created_at=storage.now_iso(),
    )
    storage.write_manifest(manifest)
    storage.write_status(JobStatusFile(
        job_id=job_id, status="running", stage="init",
        current_action="cli started", updated_at=storage.now_iso(),
    ))
    storage.upsert_index(JobIndexEntry(
        job_id=job_id, channel_url=channel_url,
        created_at=manifest.created_at, status="running",
    ))
    storage.append_log(job_id, f"=== CLI run started ({channel_url}) ===")

    # Print banner
    _banner(channel_url, job_id, options)
    ctx = CLIJobContext(job_id, manifest, verbose=not args.quiet)

    return _execute(ctx)


def _run_retry(job_id: str, *, verbose: bool) -> int:
    manifest = storage.read_manifest(job_id)
    if not manifest:
        print(f"{C.RED}✗ Job not found: {job_id}{C.R}", file=sys.stderr)
        return 2
    # Reset for retry
    manifest.status = "running"
    manifest.errors = []
    manifest.completed_at = None
    storage.write_manifest(manifest)
    storage.write_status(JobStatusFile(
        job_id=job_id, status="running", stage="init",
        current_action="cli retry", updated_at=storage.now_iso(),
    ))
    storage.append_log(job_id, "=== CLI retry started ===")

    _banner(manifest.channel.url, job_id, manifest.config, retry=True)
    ctx = CLIJobContext(job_id, manifest, verbose=verbose)
    return _execute(ctx)


def _banner(channel_url: str, job_id: str, options: JobOptions, *, retry: bool = False) -> None:
    tag = " (retry)" if retry else ""
    print(f"{C.BOLD}YT Creator Archive{C.R}  ·  {C.DIM}cli{tag}{C.R}")
    print(f"  {C.DIM}Channel:{C.R} {channel_url}")
    print(f"  {C.DIM}Job ID: {C.R} {job_id}")
    flags = f"latest={options.latest_count} popular={options.popular_count} langs={','.join(options.languages)} bitrate={options.audio_bitrate} max={options.max_audio_mib}MiB quality={options.quality_cap}p"
    if options.asr_fallback:
        flags += f" asr={options.asr_model}"
    print(f"  {C.DIM}Options:{C.R} {flags}")


def _execute(ctx: CLIJobContext) -> int:
    t_start = time.time()
    try:
        run_pipeline(ctx)
        ctx.save()
        ctx.update_status(
            status=ctx.manifest.status,
            stage="done",
            overall_progress=1.0,
            current_action=ctx.manifest.status,
        )
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Interrupted — partial manifest saved.{C.R}")
        ctx.manifest.status = "failed"
        ctx.manifest.errors.append("Interrupted by user (Ctrl+C)")
        ctx.save()
        ctx.update_status(status="failed", current_action="interrupted")
        return 130
    except Exception as e:
        import traceback
        print(f"\n{C.RED}{C.BOLD}✗ FAILED{C.R}: {e}", file=sys.stderr)
        traceback.print_exc()
        ctx.manifest.status = "failed"
        ctx.manifest.errors.append(str(e))
        ctx.save()
        ctx.update_status(status="failed", current_action=str(e)[:200])
        return 1

    # Summary
    elapsed = time.time() - t_start
    n_videos = len(ctx.manifest.videos)
    n_subs = sum(1 for v in ctx.manifest.videos for t in v.files.transcripts.values() if t)
    n_audio = sum(1 for v in ctx.manifest.videos if v.files.audio_full)
    n_photos = len(ctx.manifest.creator_images)
    sub_by_lang: dict[str, int] = {}
    for v in ctx.manifest.videos:
        for lang, t in v.files.transcripts.items():
            if t:
                sub_by_lang[lang] = sub_by_lang.get(lang, 0) + 1
    sub_breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(sub_by_lang.items()))

    out_dir = storage.job_dir(ctx.job_id) / "output"
    print()
    if ctx.manifest.status == "completed":
        print(f"{C.GREEN}{C.BOLD}✓ Done{C.R} in {_format_elapsed(elapsed)}")
    elif ctx.manifest.status == "needs_face_confirmation":
        print(f"{C.SIENNA}{C.BOLD}◐ Paused{C.R} — open the web UI to confirm the creator's face.")
    else:
        print(f"{C.YELLOW}{C.BOLD}! Stopped{C.R} ({ctx.manifest.status})")
    print(f"  {C.DIM}Output:{C.R}     {out_dir}")
    print(f"  {C.DIM}Videos:{C.R}     {n_videos}")
    print(f"  {C.DIM}Audio:{C.R}      {n_audio} extracted")
    print(f"  {C.DIM}Subtitles:{C.R}  {n_subs} files  ({sub_breakdown or 'none'})")
    print(f"  {C.DIM}Photos:{C.R}     {n_photos}")
    if ctx.manifest.errors:
        print(f"  {C.YELLOW}Anomalies: {len(ctx.manifest.errors)} (see log.txt or web UI){C.R}")

    return 0 if ctx.manifest.status in ("completed", "needs_face_confirmation") else 1


if __name__ == "__main__":
    sys.exit(main())
