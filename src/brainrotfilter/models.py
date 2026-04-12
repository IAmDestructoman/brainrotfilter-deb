"""
models.py - Pydantic data models for BrainrotFilter.

All API request/response shapes, database row representations, and internal
data-transfer objects are defined here so that every module shares the same
type vocabulary.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class VideoStatus(str, Enum):
    """Classification tier for a video."""

    ALLOW = "allow"
    MONITOR = "monitor"
    SOFT_BLOCK = "soft_block"
    BLOCK = "block"
    PENDING = "pending"


class ChannelTier(str, Enum):
    """Classification tier for a channel."""

    ALLOW = "allow"
    MONITOR = "monitor"
    SOFT_BLOCK = "soft_block"
    BLOCK = "block"


class WhitelistType(str, Enum):
    VIDEO = "video"
    CHANNEL = "channel"


class ActionTaken(str, Enum):
    ALLOW = "allow"
    MONITOR = "monitor"
    SOFT_BLOCK = "soft_block"
    BLOCK = "block"
    PENDING = "pending"


# ---------------------------------------------------------------------------
# Analysis sub-results
# ---------------------------------------------------------------------------


class KeywordMatch(BaseModel):
    """A single keyword match found during analysis."""

    keyword: str
    weight: float
    context: str = ""  # where found: title/description/tags/caption/ocr
    matched_text: str = ""


class SceneDetails(BaseModel):
    """Details from scene cut detection."""

    total_cuts: int = 0
    cuts_per_minute: float = 0.0
    avg_scene_duration_s: float = 0.0
    analysis_duration_s: float = 0.0
    is_music_video: bool = False
    dampening_applied: bool = False
    dampening_factor: float = 1.0
    full_scan_performed: bool = False


class LoudnessMetrics(BaseModel):
    """Audio loudness measurements."""

    rms_mean: float = 0.0
    rms_std: float = 0.0
    dynamic_range_db: float = 0.0
    peak_to_avg_ratio: float = 0.0
    loudness_score: float = 0.0


class AudioChaosMetrics(BaseModel):
    """Spectral chaos measurements."""

    spectral_flux_mean: float = 0.0
    spectral_flux_std: float = 0.0
    zero_crossing_rate: float = 0.0
    onset_density: float = 0.0
    chaos_score: float = 0.0


class NLPFindings(BaseModel):
    """NLP results from speech analysis."""

    repetitive_phrases: List[str] = Field(default_factory=list)
    nonsense_ratio: float = 0.0
    keyword_hits: List[str] = Field(default_factory=list)
    nlp_score: float = 0.0


class AudioDetails(BaseModel):
    """Full audio analysis details."""

    loudness: LoudnessMetrics = Field(default_factory=LoudnessMetrics)
    chaos: AudioChaosMetrics = Field(default_factory=AudioChaosMetrics)
    nlp: NLPFindings = Field(default_factory=NLPFindings)
    speech_text: str = ""
    audio_duration_s: float = 0.0
    error: Optional[str] = None


class ShortsDetails(BaseModel):
    """Details from YouTube Shorts detection."""

    is_short: bool = False
    detection_method: str = "none"  # none / likely / confirmed
    bonus_applied: int = 0
    matched_patterns: List[str] = Field(default_factory=list)
    duration_seconds: int = 0
    duration_is_short: bool = False
    hashtag_in_title: bool = False
    hashtag_in_description: bool = False
    url_confirmed: bool = False
    has_brainrot_patterns: bool = False
    brainrot_pattern_count: int = 0


class CommentDetails(BaseModel):
    """Details from comment sentiment and brainrot signal analysis."""

    comments_fetched: int = 0
    comments_analyzed: int = 0
    keyword_density: float = 0.0
    top_matched_keywords: List[str] = Field(default_factory=list)
    emoji_ratio: float = 0.0
    avg_comment_length: float = 0.0
    unique_words_ratio: float = 0.0
    caps_ratio: float = 0.0
    repetition_ratio: float = 0.0
    toxicity_count: int = 0
    fetch_error: Optional[str] = None
    score_breakdown: Dict[str, float] = Field(default_factory=dict)


class ThumbnailDetails(BaseModel):
    """Details from visual thumbnail analysis."""

    avg_saturation: float = 0.0
    text_detected: bool = False
    text_content: str = ""
    text_area_ratio: float = 0.0
    red_elements_count: int = 0
    faces_detected: int = 0
    edge_density: float = 0.0
    color_entropy: float = 0.0
    download_error: bool = False
    score_breakdown: Dict[str, float] = Field(default_factory=dict)


class EngagementDetails(BaseModel):
    """Details from engagement pattern analysis."""

    view_velocity: float = 0.0
    days_since_published: float = 0.0
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    engagement_ratio: float = 0.0
    like_to_view: float = 0.0
    comment_to_view: float = 0.0
    duration_seconds: int = 0
    duration_category: str = "unknown"
    channel_metrics: Dict[str, Any] = Field(default_factory=dict)
    bait_patterns_found: List[str] = Field(default_factory=list)
    bait_pattern_count: int = 0
    score_breakdown: Dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------


class VideoAnalysis(BaseModel):
    """Complete analysis record for a YouTube video."""

    id: Optional[int] = None
    video_id: str
    channel_id: str = ""
    title: str = ""
    description: str = ""
    thumbnail_url: str = ""

    # Scores (0-100)
    keyword_score: float = 0.0
    scene_score: float = 0.0
    audio_score: float = 0.0
    combined_score: float = 0.0

    # New analyzer scores (0-100 each; shorts_score is a bonus to be added)
    shorts_score: float = 0.0
    comment_score: float = 0.0
    thumbnail_score: float = 0.0
    engagement_score: float = 0.0

    # Short flag (denormalized for fast DB queries)
    is_short: bool = False

    status: VideoStatus = VideoStatus.PENDING
    matched_keywords: List[KeywordMatch] = Field(default_factory=list)
    scene_details: Optional[SceneDetails] = None
    audio_details: Optional[AudioDetails] = None

    # New analyzer details
    shorts_details: Optional[ShortsDetails] = None
    comment_details: Optional[CommentDetails] = None
    thumbnail_details: Optional[ThumbnailDetails] = None
    engagement_details: Optional[EngagementDetails] = None

    analyzed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    manual_override: bool = False
    override_by: Optional[str] = None

    class Config:
        use_enum_values = True


class ChannelProfile(BaseModel):
    """Channel-level profile with aggregated statistics."""

    id: Optional[int] = None
    channel_id: str
    channel_name: str = ""
    subscriber_count: int = 0
    total_videos: int = 0
    videos_analyzed: int = 0
    videos_flagged: int = 0
    flagged_percentage: float = 0.0
    avg_video_length: float = 0.0  # seconds
    upload_frequency: float = 0.0  # videos per week
    tier: ChannelTier = ChannelTier.ALLOW
    auto_escalated: bool = False
    last_analyzed: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        use_enum_values = True


class RequestLog(BaseModel):
    """A single request log entry from the Squid redirector."""

    id: Optional[int] = None
    client_ip: str
    video_id: str = ""
    channel_id: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    action_taken: ActionTaken = ActionTaken.ALLOW
    scores: Optional[Dict[str, float]] = None
    user_agent: str = ""

    class Config:
        use_enum_values = True


class WhitelistEntry(BaseModel):
    """A whitelisted video or channel."""

    id: Optional[int] = None
    type: WhitelistType
    target_id: str
    added_by: str = "admin"
    reason: str = ""
    created_at: Optional[datetime] = None

    class Config:
        use_enum_values = True


class Settings(BaseModel):
    """Full settings snapshot (used in GET/PUT /api/settings)."""

    keyword_threshold: int = 40
    scene_threshold: int = 50
    audio_threshold: int = 45
    combined_threshold: int = 45

    weight_keyword: float = 0.25
    weight_scene: float = 0.20
    weight_audio: float = 0.15
    weight_comment: float = 0.15
    weight_engagement: float = 0.10
    weight_thumbnail: float = 0.10
    weight_ml: float = 0.05

    monitor_score_min: int = 20
    soft_block_score_min: int = 35
    block_score_min: int = 55

    channel_flag_percentage: int = 30
    auto_escalation: bool = True
    initial_scan_duration: int = 45
    full_scan_time_limit: int = 120

    youtube_api_key: str = ""
    gateway_ip: str = ""
    analyzer_service_url: str = "http://127.0.0.1:8199"
    service_port: int = 8199
    squid_port: int = 3128
    log_all_requests: bool = True
    log_retention_days: int = 30

    @field_validator(
        "weight_keyword", "weight_scene", "weight_audio",
        "weight_comment", "weight_engagement", "weight_thumbnail", "weight_ml",
    )
    @classmethod
    def weight_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("Weight must be between 0.0 and 1.0")
        return round(v, 4)


# ---------------------------------------------------------------------------
# API request/response shapes
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """POST /api/analyze request body."""

    video_id: str
    priority: bool = False  # Jump to front of queue


class AnalyzeResponse(BaseModel):
    """POST /api/analyze response."""

    video_id: str
    queued: bool
    message: str


class CheckRequest(BaseModel):
    """POST /api/check request body (used by Squid helper)."""

    video_id: Optional[str] = None
    channel_id: Optional[str] = None


class CheckResponse(BaseModel):
    """POST /api/check response."""

    action: str  # allow / monitor / soft_block / block
    video_id: Optional[str] = None
    channel_id: Optional[str] = None
    redirect_url: Optional[str] = None
    reason: str = ""


class KillStateRequest(BaseModel):
    """POST /api/kill-state request body."""

    client_ip: str
    video_id: Optional[str] = None


class KillStateResponse(BaseModel):
    """POST /api/kill-state response."""

    success: bool
    states_killed: int
    message: str


class OverrideRequest(BaseModel):
    """POST /api/override request body."""

    video_id: str
    action: VideoStatus
    override_by: str = "admin"


class WhitelistRequest(BaseModel):
    """POST /api/whitelist request body."""

    type: WhitelistType
    target_id: str
    added_by: str = "admin"
    reason: str = ""


class PaginatedResponse(BaseModel):
    """Generic paginated response wrapper."""

    total: int
    page: int
    per_page: int
    pages: int
    items: List[Any]


class DashboardStats(BaseModel):
    """GET /api/stats response."""

    total_videos_analyzed: int = 0
    total_videos_blocked: int = 0
    total_videos_soft_blocked: int = 0
    total_videos_monitored: int = 0
    total_videos_allowed: int = 0
    total_channels_profiled: int = 0
    total_channels_blocked: int = 0
    total_requests_today: int = 0
    total_requests_blocked_today: int = 0
    queue_size: int = 0
    avg_combined_score: float = 0.0
    top_matched_keywords: List[Dict[str, Any]] = Field(default_factory=list)
    recent_blocks: List[Dict[str, Any]] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """
    Internal result returned by each analyzer module to the coordinator.
    All three analyzers return this shape so the coordinator can unify them.
    """

    module: str  # "keyword" / "scene" / "audio"
    score: float  # 0-100
    details: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    duration_s: float = 0.0
