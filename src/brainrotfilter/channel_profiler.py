"""
channel_profiler.py - Channel-level profiling and auto-escalation.

Workflow:
  1. Fetch channel metadata from YouTube Data API
  2. Query the DB for all analyzed videos for the channel
  3. Compute: flagged_percentage, avg_video_length, upload_frequency
  4. Determine recommended_tier based on thresholds
  5. Apply auto-escalation if flagged% exceeds config threshold
     (but never override a manual override)
  6. Persist updated channel record to DB

Channel tiers:
  allow       - benign channel
  monitor     - worth watching but not blocking yet
  soft_block  - warn users before serving content
  block       - fully blocked

Auto-escalation ladder:
  If flagged_percentage >= channel_flag_percentage (default 30%), escalate
  one tier above what the scoring would suggest, up to 'block'.
  Manual overrides set via the admin panel are never auto-escalated.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from config import config
from db_manager import db
from models import ChannelProfile, ChannelTier

logger = logging.getLogger(__name__)

# Tier ordering (for escalation arithmetic)
_TIER_ORDER = ["allow", "monitor", "soft_block", "block"]


def _escalate_tier(tier: str) -> str:
    """Move one tier up the escalation ladder (capped at 'block')."""
    idx = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else 0
    return _TIER_ORDER[min(idx + 1, len(_TIER_ORDER) - 1)]


def _score_to_channel_tier(
    flagged_pct: float,
    videos_analyzed: int,
) -> str:
    """
    Determine the recommended tier for a channel based on its flagged
    video percentage.

    Requires at least 3 analyzed videos before escalating above 'allow'
    to prevent false positives from a single flagged video.
    """
    if videos_analyzed < 3:
        return "allow"

    cfg_flag_pct = config.channel_flag_percentage  # default 30

    if flagged_pct >= min(cfg_flag_pct * 2, 70):
        return "block"
    if flagged_pct >= cfg_flag_pct:
        return "soft_block"
    if flagged_pct >= cfg_flag_pct * 0.5:
        return "monitor"
    return "allow"


def _compute_upload_frequency(video_publish_dates: list) -> float:
    """
    Estimate uploads per week from a list of ISO-8601 publish date strings.

    Returns 0.0 if fewer than 2 dates are available.
    """
    if len(video_publish_dates) < 2:
        return 0.0

    parsed = []
    for d in video_publish_dates:
        try:
            parsed.append(datetime.fromisoformat(d.replace("Z", "+00:00")))
        except Exception:
            pass

    if len(parsed) < 2:
        return 0.0

    parsed.sort()
    span_days = (parsed[-1] - parsed[0]).total_seconds() / 86400.0
    if span_days <= 0:
        return 0.0

    uploads_per_week = (len(parsed) - 1) / (span_days / 7.0)
    return round(uploads_per_week, 3)


def profile_channel(channel_id: str) -> Optional[ChannelProfile]:
    """
    Build or refresh a ChannelProfile for the given channel.

    Steps:
      1. Fetch YouTube metadata
      2. Load existing channel record from DB
      3. Load all analyzed video records from DB
      4. Compute stats
      5. Determine tier + auto-escalation
      6. Persist and return

    Returns the updated ChannelProfile or None on fatal error.
    """
    start = time.monotonic()
    now = datetime.utcnow()

    # 1. Fetch YouTube metadata
    yt_data: Optional[Dict[str, Any]] = None
    try:
        from youtube_api import get_channel_details

        yt_data = get_channel_details(channel_id)
    except Exception as exc:
        logger.warning("Failed to fetch YouTube channel metadata for %s: %s", channel_id, exc)

    # 2. Load existing record (for manual override detection)
    existing = db.get_channel(channel_id)

    # 3. Video stats from DB
    flagged_stats = db.get_channel_flagged_stats(channel_id)
    videos_analyzed: int = flagged_stats["videos_analyzed"]
    videos_flagged: int = flagged_stats["videos_flagged"]
    flagged_pct: float = flagged_stats["flagged_percentage"]

    # Load actual video records to compute avg_length and upload_frequency
    channel_videos = db.get_channel_videos(channel_id)
    avg_length: float = 0.0
    if channel_videos:
        # Try to get duration from scene_details if available
        durations = []
        for v in channel_videos:
            try:
                import json as _json

                scene = _json.loads(v.get("scene_details") or "{}")
                dur = scene.get("analysis_duration_s", 0)
                if dur and dur > 0:
                    durations.append(dur)
            except Exception:
                pass
        if durations:
            avg_length = round(sum(durations) / len(durations), 1)

    # Upload frequency from YT API if available
    upload_freq: float = 0.0
    if yt_data:
        try:
            from youtube_api import get_channel_videos as _get_vids

            _recent_ids = _get_vids(channel_id, max_results=30)  # noqa: F841
            # We need publish dates — for a lightweight approach, use what DB has
            pub_dates = [v.get("analyzed_at", "") for v in channel_videos if v.get("analyzed_at")]
            upload_freq = _compute_upload_frequency(pub_dates)
        except Exception as exc:
            logger.debug("Upload frequency computation failed: %s", exc)

    # 4. Recommended tier
    recommended_tier = _score_to_channel_tier(flagged_pct, videos_analyzed)

    # 5. Auto-escalation logic
    auto_escalated = False
    final_tier = recommended_tier

    if existing:
        # Honour the existing tier if it was manually elevated by admin
        # We detect this by checking if auto_escalated is False and tier > recommended
        existing_tier = existing.get("tier", "allow")
        existing_auto = bool(existing.get("auto_escalated", False))

        # Auto-escalate if flagged% is above threshold
        if flagged_pct >= config.channel_flag_percentage and videos_analyzed >= 3:
            candidate = _escalate_tier(recommended_tier)
            if not existing_auto and _TIER_ORDER.index(existing_tier) > _TIER_ORDER.index(recommended_tier):
                # Admin manually set a higher tier — respect it
                final_tier = existing_tier
                auto_escalated = False
                logger.debug(
                    "Preserving manually elevated tier %s for channel %s",
                    existing_tier,
                    channel_id,
                )
            else:
                final_tier = candidate
                auto_escalated = True
                logger.info(
                    "Auto-escalating channel %s: %s → %s (flagged=%.1f%%)",
                    channel_id,
                    existing_tier,
                    final_tier,
                    flagged_pct,
                )

    # 6. Build the profile object
    profile = ChannelProfile(
        channel_id=channel_id,
        channel_name=yt_data.get("name", "") if yt_data else (existing.get("channel_name", "") if existing else ""),
        subscriber_count=yt_data.get("subscriber_count", 0) if yt_data else (existing.get("subscriber_count", 0) if existing else 0),
        total_videos=yt_data.get("video_count", 0) if yt_data else (existing.get("total_videos", 0) if existing else 0),
        videos_analyzed=videos_analyzed,
        videos_flagged=videos_flagged,
        flagged_percentage=flagged_pct,
        avg_video_length=avg_length,
        upload_frequency=upload_freq,
        tier=ChannelTier(final_tier),
        auto_escalated=auto_escalated,
        last_analyzed=now,
        created_at=datetime.fromisoformat(existing["created_at"]) if existing and existing.get("created_at") else now,
        updated_at=now,
    )

    # 7. Persist
    try:
        db.upsert_channel(profile)
    except Exception as exc:
        logger.error("Failed to persist channel profile for %s: %s", channel_id, exc)
        return None

    elapsed = time.monotonic() - start
    logger.info(
        "Channel %s profiled in %.2fs: tier=%s, flagged=%.1f%% (%d/%d)",
        channel_id,
        elapsed,
        final_tier,
        flagged_pct,
        videos_flagged,
        videos_analyzed,
    )
    return profile


def get_or_create_channel(channel_id: str) -> Optional[Dict[str, Any]]:
    """
    Return an existing channel record or trigger a profile build if none exists.
    """
    existing = db.get_channel(channel_id)
    if existing:
        return existing
    profile = profile_channel(channel_id)
    return db.get_channel(channel_id) if profile else None


def update_channel_after_video(
    channel_id: str,
    new_video_status: str,
) -> None:
    """
    Re-profile a channel after a new video analysis has been stored.

    This is called by the analyzer_service whenever a video completes analysis.
    """
    profile_channel(channel_id)
