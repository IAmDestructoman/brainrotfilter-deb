"""
engagement_analyzer.py - Engagement pattern analysis for brainrot detection.

Brainrot videos exhibit distinctive engagement metrics detectable from
YouTube Data API statistics: viral velocity, abnormally high like/comment
ratios, algorithm-optimized durations, and bait-heavy titles.

Pipeline:
  1. View velocity (views / days since publish)
  2. Engagement ratio ((likes + comments) / views)
  3. Like-to-view ratio
  4. Comment-to-view ratio
  5. Duration category analysis
  6. Channel metrics (subscriber-to-view, upload frequency)
  7. Title engagement-bait pattern detection
  8. Sum sub-scores → engagement_score (0-100)

Sub-score caps:
  view_velocity_score     0-15
  engagement_ratio_score  0-15
  like_ratio_score        0-10
  comment_ratio_score     0-10
  duration_score          0-10
  channel_score           0-20
  bait_score              0-20
"""

from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from models import AnalysisResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Duration category boundaries (seconds)
# ---------------------------------------------------------------------------

DURATION_SHORT_MAX: int = 60       # YouTube Shorts territory
DURATION_AD_OPT_MIN: int = 480     # 8 minutes — ad-revenue threshold
DURATION_AD_OPT_MAX: int = 720     # 12 minutes

# ---------------------------------------------------------------------------
# Title engagement-bait pattern definitions
# ---------------------------------------------------------------------------

_BAIT_PATTERNS: List[Tuple[str, re.Pattern[str], int]] = [
    # (name, pattern, max_contribution_points)
    (
        "question_bait",
        re.compile(r"\b(?:can you believe|did you know|would you|what would|who else|how did)\b.{0,30}\?", re.IGNORECASE),
        3,
    ),
    (
        "listicle",
        re.compile(r"\b(?:top\s+\d+|\d+\s+things|\d+\s+reasons|\d+\s+ways|\d+\s+times)\b", re.IGNORECASE),
        3,
    ),
    (
        "urgency_words",
        re.compile(r"\b(?:right now|immediately|must see|you need to|do this now|before it.s too late|breaking)\b", re.IGNORECASE),
        4,
    ),
    (
        "emotional_triggers",
        re.compile(r"\b(?:destroyed|exposed|insane|shocking|unbelievable|incredible|mind.blown|game.changer|jaw.drop|savage|brutal|roasted|humiliated|triggered)\b", re.IGNORECASE),
        4,
    ),
    (
        "caps_urgency",
        re.compile(r"\b(?:NOW|BREAKING|SHOCKING|WATCH|EXPOSED|INSANE|WAIT|OMG|WTF|INSANE|GONE WRONG|GONE SEXUAL)\b"),
        3,
    ),
    (
        "reaction_bait",
        re.compile(r"\b(?:reacting to|my reaction|reaction video|i tried|i tested|24 hours|last to leave|challenge)\b", re.IGNORECASE),
        2,
    ),
    (
        "algorithm_hooks",
        re.compile(r"\b(?:algorithm|this video will|youtube keep|please watch|watch until the end|like and subscribe|hit the bell)\b", re.IGNORECASE),
        3,
    ),
    (
        "numbers_hype",
        re.compile(r"\b(?:\$[\d,]+|\d+[km]\+?\s+(?:subscribers?|followers?|views?|dollars?)|\d{4,}\s+people)\b", re.IGNORECASE),
        2,
    ),
    (
        "hyperbole",
        re.compile(r"\b(?:ever|all time|greatest|worst|best|ultimate|perfect|literally|actually|honestly|genuinely)\b", re.IGNORECASE),
        1,
    ),
]

# ---------------------------------------------------------------------------
# Helper: parse ISO 8601 datetime string → timezone-aware datetime
# ---------------------------------------------------------------------------


def _parse_published_at(published_at: str) -> Optional[datetime]:
    """
    Parse a YouTube API publishedAt string (ISO 8601) to a timezone-aware datetime.

    Handles both 'Z' suffix and explicit UTC offsets.
    Returns None on parse failure.
    """
    if not published_at:
        return None
    try:
        # Normalize: replace trailing Z with +00:00
        ts = published_at.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except Exception:
        pass
    # Try strptime as fallback
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(published_at, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _days_since_published(published_at: str) -> float:
    """Return number of days since publish date, or 0 on error."""
    dt = _parse_published_at(published_at)
    if dt is None:
        return 0.0
    now = datetime.now(tz=timezone.utc)
    delta = now - dt
    return max(0.0, delta.total_seconds() / 86400.0)


# ---------------------------------------------------------------------------
# Sub-score calculators
# ---------------------------------------------------------------------------


def _view_velocity_score(
    view_count: int, days_since: float
) -> Tuple[float, float]:
    """
    Score based on views per day since publish (0-15).

    Extremely high view velocity on very new videos is a strong signal
    (algorithm-pushed viral brainrot).
    """
    if days_since <= 0 or view_count <= 0:
        return 0.0, 0.0

    velocity = view_count / days_since  # views per day

    # Logarithmic mapping: 1M views/day → ~15, 100K → ~10, 10K → ~6
    if velocity <= 0:
        score = 0.0
    else:
        score = min(15.0, math.log10(max(velocity, 1)) * 3.0)

    return round(score, 2), round(velocity, 2)


def _engagement_ratio_score(
    view_count: int, like_count: int, comment_count: int
) -> Tuple[float, float]:
    """
    Score based on (likes + comments) / views (0-15).

    High engagement relative to views indicates algorithm-optimized content.
    """
    if view_count <= 0:
        return 0.0, 0.0

    ratio = (like_count + comment_count) / view_count
    # Typical good YouTube video: ~5-8% engagement ratio
    # Brainrot: often 10-20%+ due to comment bait
    score = min(15.0, ratio * 100.0)

    return round(score, 2), round(ratio, 5)


def _like_ratio_score(view_count: int, like_count: int) -> Tuple[float, float]:
    """Like-to-view ratio score (0-10)."""
    if view_count <= 0:
        return 0.0, 0.0

    ratio = like_count / view_count
    # Normal range: 2-5%; brainrot often pushes 7-15%+
    score = min(10.0, ratio * 100.0)

    return round(score, 2), round(ratio, 5)


def _comment_ratio_score(view_count: int, comment_count: int) -> Tuple[float, float]:
    """Comment-to-view ratio score (0-10)."""
    if view_count <= 0:
        return 0.0, 0.0

    ratio = comment_count / view_count
    # Normal: ~0.3-1%; brainrot: 2-5%+
    score = min(10.0, ratio * 500.0)

    return round(score, 2), round(ratio, 5)


def _duration_score(duration_seconds: int) -> Tuple[float, str]:
    """
    Duration-based score component (0-10).

    Returns (score, category_label).
    """
    if duration_seconds <= 0:
        return 0.0, "unknown"

    if duration_seconds <= DURATION_SHORT_MAX:
        return 10.0, "short"

    if DURATION_AD_OPT_MIN <= duration_seconds <= DURATION_AD_OPT_MAX:
        # Ad-revenue optimized length: mild signal
        return 4.0, "ad_optimized"

    if duration_seconds < 180:
        # 1-3 min: TikTok-ish vertical content
        return 6.0, "very_short"

    if duration_seconds > 3600:
        # Long-form content: low brainrot signal
        return 1.0, "long_form"

    return 0.0, "standard"


def _channel_metrics_score(channel_data: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """
    Score based on channel-level signals (0-20).

    Expected keys in channel_data:
      subscriber_count (int), video_count (int),
      upload_frequency (float, videos/week), view_count (int, channel total)
    """
    if not channel_data:
        return 0.0, {}

    score = 0.0
    metrics: Dict[str, Any] = {}

    subscriber_count = int(channel_data.get("subscriber_count", 0))
    upload_frequency = float(channel_data.get("upload_frequency", 0.0))
    channel_view_count = int(channel_data.get("view_count", 0))

    # Sub-to-view ratio: very low sub count but high channel views = viral/algorithm
    if subscriber_count > 0 and channel_view_count > 0:
        sub_view_ratio = subscriber_count / channel_view_count
        # Normal: 1 sub per 10-50 views. Low ratio = algorithm-surfaced content
        if sub_view_ratio < 0.01:
            sub_component = 8.0
        elif sub_view_ratio < 0.05:
            sub_component = 5.0
        elif sub_view_ratio < 0.1:
            sub_component = 2.0
        else:
            sub_component = 0.0
        metrics["subscriber_to_view_ratio"] = round(sub_view_ratio, 5)
        metrics["sub_component"] = sub_component
        score += sub_component

    # Upload frequency: channels posting many times per day are content farms
    if upload_frequency > 0:
        if upload_frequency >= 14:  # 2+ videos/day
            freq_component = 12.0
        elif upload_frequency >= 7:  # 1/day
            freq_component = 8.0
        elif upload_frequency >= 3.5:  # every other day
            freq_component = 4.0
        else:
            freq_component = 0.0
        metrics["upload_frequency_per_week"] = round(upload_frequency, 2)
        metrics["freq_component"] = freq_component
        score += freq_component

    metrics["total_channel_score"] = round(min(score, 20.0), 2)
    return round(min(score, 20.0), 2), metrics


def _bait_pattern_score(title: str) -> Tuple[float, List[str]]:
    """
    Scan title for engagement-bait patterns (0-20).

    Returns (score, list_of_matched_pattern_names).
    """
    if not title:
        return 0.0, []

    matched: List[str] = []
    total_points = 0

    for name, pattern, max_points in _BAIT_PATTERNS:
        if pattern.search(title):
            matched.append(name)
            total_points += max_points

    score = min(20.0, float(total_points))
    return round(score, 2), matched


# ---------------------------------------------------------------------------
# Main analyze function
# ---------------------------------------------------------------------------


def analyze(
    video_id: str,
    video_data: Optional[Dict[str, Any]] = None,
    channel_data: Optional[Dict[str, Any]] = None,
) -> AnalysisResult:
    """
    Analyze video and channel engagement patterns for brainrot signals.

    Args:
        video_id:     YouTube video ID (used for logging).
        video_data:   Dict with video metadata from YouTube API. Expected keys:
                        view_count (int), like_count (int), comment_count (int),
                        duration (int, seconds), published_at (ISO 8601 string),
                        title (str).
        channel_data: Dict with channel metadata. Expected keys:
                        subscriber_count (int), video_count (int),
                        upload_frequency (float, videos/week),
                        view_count (int, total channel views).

    Returns:
        AnalysisResult with module="engagement", score (0-100), and details dict
        containing: view_velocity, engagement_ratio, like_to_view, comment_to_view,
        duration_category, channel_metrics, bait_patterns_found, score_breakdown.
    """
    start = time.monotonic()

    video_data = video_data or {}
    channel_data = channel_data or {}

    view_count = int(video_data.get("view_count", 0))
    like_count = int(video_data.get("like_count", 0))
    comment_count = int(video_data.get("comment_count", 0))
    duration_seconds = int(video_data.get("duration", 0))
    published_at = str(video_data.get("published_at", ""))
    title = str(video_data.get("title", ""))

    # ----------------------------------------------------------------
    # Compute sub-scores
    # ----------------------------------------------------------------
    days_since = _days_since_published(published_at)

    vel_score, view_velocity = _view_velocity_score(view_count, days_since)
    eng_score, engagement_ratio = _engagement_ratio_score(view_count, like_count, comment_count)
    like_score, like_ratio = _like_ratio_score(view_count, like_count)
    comment_score_val, comment_ratio = _comment_ratio_score(view_count, comment_count)
    dur_score, duration_category = _duration_score(duration_seconds)
    chan_score, chan_metrics = _channel_metrics_score(channel_data)
    bait_score, bait_patterns = _bait_pattern_score(title)

    total_score = min(
        100.0,
        vel_score + eng_score + like_score + comment_score_val
        + dur_score + chan_score + bait_score,
    )

    elapsed = time.monotonic() - start

    logger.info(
        "Engagement analysis for %s: score=%.1f (vel=%.1f, eng=%.1f, like=%.1f, "
        "comment=%.1f, dur=%.1f, chan=%.1f, bait=%.1f), duration=%.2fs",
        video_id,
        total_score,
        vel_score,
        eng_score,
        like_score,
        comment_score_val,
        dur_score,
        chan_score,
        bait_score,
        elapsed,
    )

    return AnalysisResult(
        module="engagement",
        score=round(total_score, 2),
        details={
            "view_velocity": view_velocity,
            "days_since_published": round(days_since, 1),
            "view_count": view_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "engagement_ratio": engagement_ratio,
            "like_to_view": like_ratio,
            "comment_to_view": comment_ratio,
            "duration_seconds": duration_seconds,
            "duration_category": duration_category,
            "channel_metrics": chan_metrics,
            "bait_patterns_found": bait_patterns,
            "bait_pattern_count": len(bait_patterns),
            "score_breakdown": {
                "view_velocity_score": vel_score,
                "engagement_ratio_score": eng_score,
                "like_ratio_score": like_score,
                "comment_ratio_score": comment_score_val,
                "duration_score": dur_score,
                "channel_score": chan_score,
                "bait_score": bait_score,
            },
        },
        duration_s=round(elapsed, 3),
    )
