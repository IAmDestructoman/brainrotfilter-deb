"""
youtube_api.py - YouTube Data API v3 wrapper for BrainrotFilter.

Provides:
  - get_video_details(video_id)      -> full video metadata
  - get_channel_details(channel_id)  -> channel metadata
  - get_channel_videos(channel_id)   -> recent video IDs
  - get_video_captions(video_id)     -> auto-generated caption text via yt-dlp

Includes rate-limit tracking and exponential-backoff retry.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional

import isodate  # pip install isodate (transitive dep of google-api-python-client)
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate-limit tracker (per-process in-memory; resets each day)
# ---------------------------------------------------------------------------


class _QuotaTracker:
    """Tracks YouTube API quota units consumed."""

    # Approximate costs:
    COSTS = {
        "videos.list": 1,
        "channels.list": 1,
        "captions.list": 50,
        "search.list": 100,
    }

    def __init__(self) -> None:
        self._lock = Lock()
        self._used = 0
        self._reset_at = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)

    def consume(self, operation: str, units: int = 1) -> bool:
        """
        Deduct quota. Returns False (and logs a warning) if the daily limit
        has been reached, so callers can skip the API call gracefully.
        """
        with self._lock:
            now = datetime.utcnow()
            if now >= self._reset_at:
                self._used = 0
                self._reset_at = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)

            cost = self.COSTS.get(operation, units)
            limit = config.get_int("youtube_api_quota_per_day")
            if self._used + cost > limit:
                logger.warning(
                    "YouTube API quota exceeded (%d/%d). Skipping %s.",
                    self._used,
                    limit,
                    operation,
                )
                return False
            self._used += cost
            return True

    @property
    def used(self) -> int:
        with self._lock:
            return self._used


_quota = _QuotaTracker()


# ---------------------------------------------------------------------------
# Service builder (cached per API key)
# ---------------------------------------------------------------------------


_service_cache: Dict[str, Any] = {}
_service_lock = Lock()


def _get_service() -> Any:
    """Return a cached YouTube API service object."""
    api_key = config.youtube_api_key
    if not api_key:
        raise ValueError(
            "YouTube API key not configured. Set 'youtube_api_key' in settings."
        )
    with _service_lock:
        if api_key not in _service_cache:
            _service_cache[api_key] = build(
                "youtube", "v3", developerKey=api_key, cache_discovery=False
            )
    return _service_cache[api_key]


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


def _retry(fn, max_attempts: int = 3, base_delay: float = 1.0) -> Any:
    """Call *fn()* up to *max_attempts* times with exponential backoff."""
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except HttpError as exc:
            status = exc.resp.status
            if status in (403, 404):
                raise  # Don't retry quota / not-found errors
            logger.warning("YouTube API error %d (attempt %d): %s", status, attempt + 1, exc)
            last_exc = exc
        except Exception as exc:
            logger.warning("YouTube API call failed (attempt %d): %s", attempt + 1, exc)
            last_exc = exc
        time.sleep(base_delay * (2 ** attempt))
    raise RuntimeError(f"YouTube API call failed after {max_attempts} attempts") from last_exc


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------


def get_video_details(video_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch comprehensive metadata for a single YouTube video.

    Returns a dict with keys:
      video_id, title, description, tags, category_id, channel_id,
      channel_title, duration_seconds, thumbnail_url,
      view_count, like_count, comment_count, published_at

    Returns None if the video does not exist or API is unavailable.
    """
    if not _quota.consume("videos.list"):
        return None

    try:
        service = _get_service()

        def _call():
            return (
                service.videos()
                .list(
                    part="snippet,contentDetails,statistics",
                    id=video_id,
                )
                .execute()
            )

        response = _retry(_call)
    except ValueError as exc:
        logger.error("YouTube API not configured: %s", exc)
        return None
    except HttpError as exc:
        logger.error("YouTube API HTTP error for video %s: %s", video_id, exc)
        return None
    except Exception as exc:
        logger.error("Failed to fetch video details for %s: %s", video_id, exc)
        return None

    items = response.get("items", [])
    if not items:
        logger.debug("Video %s not found on YouTube.", video_id)
        return None

    item = items[0]
    snippet = item.get("snippet", {})
    content = item.get("contentDetails", {})
    stats = item.get("statistics", {})

    # Parse ISO 8601 duration -> seconds
    duration_str = content.get("duration", "PT0S")
    try:
        duration_seconds = int(isodate.parse_duration(duration_str).total_seconds())
    except Exception:
        duration_seconds = 0

    # Best available thumbnail
    thumbs = snippet.get("thumbnails", {})
    thumb_url = (
        thumbs.get("maxres", {}).get("url")
        or thumbs.get("high", {}).get("url")
        or thumbs.get("medium", {}).get("url")
        or thumbs.get("default", {}).get("url")
        or ""
    )

    return {
        "video_id": video_id,
        "title": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "tags": snippet.get("tags", []),
        "category_id": snippet.get("categoryId", ""),
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", ""),
        "duration_seconds": duration_seconds,
        "thumbnail_url": thumb_url,
        "view_count": int(stats.get("viewCount", 0) or 0),
        "like_count": int(stats.get("likeCount", 0) or 0),
        "comment_count": int(stats.get("commentCount", 0) or 0),
        "published_at": snippet.get("publishedAt", ""),
    }


def get_channel_details(channel_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch metadata for a YouTube channel.

    Returns a dict with keys:
      channel_id, name, description, subscriber_count,
      video_count, view_count, published_at, country

    Returns None if not found or API unavailable.
    """
    if not _quota.consume("channels.list"):
        return None

    try:
        service = _get_service()

        def _call():
            return (
                service.channels()
                .list(part="snippet,statistics", id=channel_id)
                .execute()
            )

        response = _retry(_call)
    except ValueError as exc:
        logger.error("YouTube API not configured: %s", exc)
        return None
    except HttpError as exc:
        logger.error("YouTube API HTTP error for channel %s: %s", channel_id, exc)
        return None
    except Exception as exc:
        logger.error("Failed to fetch channel details for %s: %s", channel_id, exc)
        return None

    items = response.get("items", [])
    if not items:
        return None

    item = items[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})

    return {
        "channel_id": channel_id,
        "name": snippet.get("title", ""),
        "description": snippet.get("description", ""),
        "subscriber_count": int(stats.get("subscriberCount", 0) or 0),
        "video_count": int(stats.get("videoCount", 0) or 0),
        "view_count": int(stats.get("viewCount", 0) or 0),
        "published_at": snippet.get("publishedAt", ""),
        "country": snippet.get("country", ""),
    }


def get_channel_videos(
    channel_id: str, max_results: int = 50
) -> List[str]:
    """
    Return a list of recent video IDs published by a channel.

    Uses search.list (costs 100 quota units) so this is used sparingly.
    """
    if not _quota.consume("search.list"):
        return []

    try:
        service = _get_service()

        def _call():
            return (
                service.search()
                .list(
                    part="id",
                    channelId=channel_id,
                    type="video",
                    order="date",
                    maxResults=min(max_results, 50),
                )
                .execute()
            )

        response = _retry(_call)
    except Exception as exc:
        logger.error("Failed to list channel videos for %s: %s", channel_id, exc)
        return []

    video_ids: List[str] = []
    for item in response.get("items", []):
        vid_id = item.get("id", {}).get("videoId")
        if vid_id:
            video_ids.append(vid_id)
    return video_ids


def get_video_captions(video_id: str) -> str:
    """
    Download auto-generated captions for a video using yt-dlp.

    Returns the caption text as a plain string, or empty string on failure.
    """
    with tempfile.TemporaryDirectory(prefix="brainrot_captions_") as tmpdir:
        output_template = os.path.join(tmpdir, "%(id)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--write-auto-sub",
            "--skip-download",
            "--sub-lang", "en",
            "--sub-format", "vtt",
            "--output", output_template,
            "--no-warnings",
            "--quiet",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                logger.debug(
                    "yt-dlp caption download returned %d for %s: %s",
                    result.returncode,
                    video_id,
                    result.stderr[:200],
                )
        except subprocess.TimeoutExpired:
            logger.warning("Caption download timed out for %s", video_id)
            return ""
        except Exception as exc:
            logger.error("Caption download failed for %s: %s", video_id, exc)
            return ""

        # Find the downloaded .vtt file
        import glob as _glob
        vtt_files = _glob.glob(os.path.join(tmpdir, "*.vtt"))
        if not vtt_files:
            logger.debug("No caption file found for %s", video_id)
            return ""

        try:
            with open(vtt_files[0], "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError as exc:
            logger.error("Failed to read caption file for %s: %s", video_id, exc)
            return ""

        return _vtt_to_text(raw)


def _vtt_to_text(vtt_content: str) -> str:
    """
    Strip WebVTT timing/formatting markup and return clean caption text.
    """
    import re
    lines = vtt_content.splitlines()
    text_lines: List[str] = []
    seen: set = set()

    for line in lines:
        line = line.strip()
        # Skip header, timing lines, and empty lines
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if "-->" in line:
            continue
        if line.isdigit():
            continue
        # Remove HTML-like tags from VTT
        clean = re.sub(r"<[^>]+>", "", line)
        clean = re.sub(r"&amp;", "&", clean)
        clean = re.sub(r"&lt;", "<", clean)
        clean = re.sub(r"&gt;", ">", clean)
        clean = clean.strip()
        if clean and clean not in seen:
            seen.add(clean)
            text_lines.append(clean)

    return " ".join(text_lines)


def get_quota_used() -> int:
    """Return how many API quota units have been consumed today."""
    return _quota.used
