"""Pydantic models for API and persistence."""
from __future__ import annotations
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field


# ─── API request/response ────────────────────────────────────────────

class JobOptions(BaseModel):
    latest_count: int = 5
    popular_count: int = 5
    audio_bitrate: str = "64k"
    max_audio_mib: float = 10.0
    languages: List[str] = Field(default_factory=lambda: ["en", "zh", "ja", "ko"])
    skip_shorts: bool = True
    quality_cap: int = 1080
    metadata_scan_size: int = 80  # how many videos to scan for selection
    silence_db: int = -30
    silence_min_dur: float = 0.5
    # ASR fallback for missing subtitles (requires faster-whisper)
    asr_fallback: bool = False
    asr_model: str = "small"  # tiny | base | small | medium | large-v3


class JobCreateRequest(BaseModel):
    channel_url: str
    options: Optional[JobOptions] = None


class JobCreateResponse(BaseModel):
    job_id: str


class ConfirmFaceRequest(BaseModel):
    face_id: str


# ─── Internal data ───────────────────────────────────────────────────

JobStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "needs_face_confirmation",
]


class ChannelInfo(BaseModel):
    url: str
    channel_id: Optional[str] = None
    title: Optional[str] = None
    handle: Optional[str] = None
    avatar_url: Optional[str] = None
    banner_url: Optional[str] = None
    description: Optional[str] = None


class VideoTranscript(BaseModel):
    path: str
    source: str  # "manual" | "auto" | "asr"
    raw_path: Optional[str] = None


class AudioSegment(BaseModel):
    path: str
    start_sec: float
    end_sec: float
    size_mib: float


class VideoFiles(BaseModel):
    video: Optional[str] = None
    thumbnail: Optional[str] = None
    info_json: Optional[str] = None
    transcripts: Dict[str, Optional[VideoTranscript]] = Field(default_factory=dict)
    audio_full: Optional[str] = None
    audio_full_size_mib: Optional[float] = None
    audio_segments: List[AudioSegment] = Field(default_factory=list)


class VideoRecord(BaseModel):
    video_id: str
    title: str
    title_safe: str  # safe stem used in filenames (no extension)
    url: str
    source: List[str] = Field(default_factory=list)  # ["latest","popular"]
    view_count: Optional[int] = None
    published_at: Optional[str] = None
    duration_sec: Optional[float] = None
    chapters: List[Dict[str, Any]] = Field(default_factory=list)
    files: VideoFiles = Field(default_factory=VideoFiles)


class CreatorImage(BaseModel):
    file: str
    source: str  # "channel_avatar" | "channel_banner" | "video_thumbnail" | "video_frame"
    from_video: Optional[str] = None
    score: Optional[float] = None  # face match similarity


class FaceReference(BaseModel):
    embedding_file: Optional[str] = None
    source: Optional[str] = None  # "channel_avatar" | "user_confirmed" | None
    confirmed_by_user: bool = False


class JobManifest(BaseModel):
    job_id: str
    channel: ChannelInfo
    config: JobOptions
    videos: List[VideoRecord] = Field(default_factory=list)
    creator_images: List[CreatorImage] = Field(default_factory=list)
    face_reference: FaceReference = Field(default_factory=FaceReference)
    status: JobStatus = "pending"
    created_at: str
    completed_at: Optional[str] = None
    errors: List[str] = Field(default_factory=list)


class JobStatusFile(BaseModel):
    """Mutable status, updated frequently during job execution."""
    job_id: str
    status: JobStatus = "pending"
    stage: str = "init"
    stage_progress: Dict[str, int] = Field(default_factory=lambda: {"current": 0, "total": 0})
    overall_progress: float = 0.0
    current_action: str = ""
    updated_at: str = ""


class JobIndexEntry(BaseModel):
    """One line in jobs.jsonl — summary for the history list."""
    job_id: str
    channel_url: str
    channel_title: Optional[str] = None
    channel_avatar: Optional[str] = None
    created_at: str
    updated_at: Optional[str] = None
    status: JobStatus = "pending"
    stage: Optional[str] = None
    video_count: int = 0
    image_count: int = 0
    overall_progress: float = 0.0


class FaceCandidate(BaseModel):
    face_id: str
    crop: str  # relative file path under the job dir
    occurrences: int = 1
    quality: float = 0.0  # detection confidence × area heuristic
