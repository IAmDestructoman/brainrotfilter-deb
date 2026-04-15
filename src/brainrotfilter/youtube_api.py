"""
youtube_api.py - YouTube metadata fetcher for BrainrotFilter.

Primary path: yt-dlp (no API key, no quota).
Fallback path: YouTube Data API v3 (requires key, 10 000 units/day quota).

yt-dlp uses the same internal APIs YouTube's own clients use, so it works
for any video a logged-out user can see — no developer key required.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _ytdlp_bin() -> str:
    """
    Return the path to the yt-dlp executable.

    Prefers the binary co-located with the running Python interpreter (i.e.
    inside the same virtualenv).  Falls back to a plain ``yt-dlp`` PATH lookup
    so development environments still work.
    """
    candidate = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
    return candidate if os.path.isfile(candidate) else "yt-dlp"


# ---------------------------------------------------------------------------
# yt-dlp helpers (primary, no API key)
# ---------------------------------------------------------------------------


def _run_ytdlp(*args: str, timeout: int = 30) -> Optional[str]:
    """
    Run yt-dlp with *args and return stdout, or None on failure.
    Common flags (--no-warnings, --quiet) are always prepended.
    """
    cmd = [_ytdlp_bin(), "--no-warnings", "--quiet"] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.debug("yt-dlp returned rc=%d: %s", result.returncode, result.stderr[:300])
            return None
        return result.stdout
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timed out (%ds) for: %s", timeout, args[-1] if args else "?")
        return None
    except FileNotFoundError:
        logger.error("yt-dlp not found — install with: pip install yt-dlp")
        return None
    except Exception as exc:
        logger.debug("yt-dlp error: %s", exc)
        return None


def _ytdlp_video_meta(video_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch full video metadata via yt-dlp.  No API key or quota needed.
    Returns a normalised dict matching the shape of get_video_details(),
    or None on failure.
    """
    out = _run_ytdlp(
        "--dump-json",
        "--no-download",
        f"https://www.youtube.com/watch?v={video_id}",
        timeout=30,
    )
    if not out:
        return None

    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        logger.debug("yt-dlp JSON parse error for %s: %s", video_id, exc)
        return None

    # Best thumbnail URL
    thumb_url: str = data.get("thumbnail") or ""
    if not thumb_url:
        thumbs = data.get("thumbnails") or []
        if thumbs:
            best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), default={})
            thumb_url = best.get("url", "")

    # Category: yt-dlp returns string names; store the first one
    categories: List[str] = data.get("categories") or []
    category_id = categories[0] if categories else ""

    # upload_date: YYYYMMDD → ISO 8601
    upload_date: str = data.get("upload_date", "") or ""
    published_at = ""
    if len(upload_date) == 8:
        published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}T00:00:00Z"

    return {
        "video_id": video_id,
        "title": data.get("title") or "",
        "description": data.get("description") or "",
        "tags": data.get("tags") or [],
        "category_id": category_id,
        "channel_id": data.get("channel_id") or "",
        "channel_title": data.get("channel") or data.get("uploader") or "",
        "duration_seconds": int(data.get("duration") or 0),
        "thumbnail_url": thumb_url,
        "view_count": int(data.get("view_count") or 0),
        "like_count": int(data.get("like_count") or 0),
        "comment_count": int(data.get("comment_count") or 0),
        "published_at": published_at,
    }


def _ytdlp_channel_videos(channel_id: str, max_results: int = 50) -> List[str]:
    """
    Return a list of recent video IDs for a channel via yt-dlp --flat-playlist.
    Each JSON line from yt-dlp is a playlist entry with an "id" field.
    """
    out = _run_ytdlp(
        "--flat-playlist",
        "--dump-json",
        "--playlist-items", f"1-{max_results}",
        f"https://www.youtube.com/channel/{channel_id}/videos",
        timeout=60,
    )
    if not out:
        return []

    video_ids: List[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            vid_id = entry.get("id") or entry.get("video_id")
            if vid_id and isinstance(vid_id, str):
                video_ids.append(vid_id)
        except json.JSONDecodeError:
            continue
    return video_ids


def _ytdlp_channel_meta(channel_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch basic channel info via yt-dlp.
    Subscriber count / video count are not available without the Data API;
    they are returned as 0.
    """
    # Fetch one flat-playlist entry to get the channel name
    out = _run_ytdlp(
        "--flat-playlist",
        "--dump-json",
        "--playlist-items", "1",
        f"https://www.youtube.com/channel/{channel_id}/videos",
        timeout=30,
    )
    channel_name = ""
    if out:
        for line in out.splitlines():
            try:
                entry = json.loads(line)
                channel_name = entry.get("channel") or entry.get("uploader") or ""
                if channel_name:
                    break
            except json.JSONDecodeError:
                continue

    if not channel_name:
        return None

    return {
        "channel_id": channel_id,
        "name": channel_name,
        "description": "",
        "subscriber_count": 0,
        "video_count": 0,
        "view_count": 0,
        "published_at": "",
        "country": "",
    }


# ---------------------------------------------------------------------------
# YouTube Data API v3 fallback (requires API key; 10 000 units/day quota)
# ---------------------------------------------------------------------------


class _QuotaTracker:
    """Tracks YouTube API quota units consumed today."""

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

    def consume(self, operation: str) -> bool:
        with self._lock:
            now = datetime.utcnow()
            if now >= self._reset_at:
                self._used = 0
                self._reset_at = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) + timedelta(days=1)
            cost = self.COSTS.get(operation, 1)
            from config import config as _cfg
            limit = _cfg.get_int("youtube_api_quota_per_day")
            if self._used + cost > limit:
                logger.warning(
                    "YouTube Data API quota limit reached (%d/%d). Skipping %s.",
                    self._used, limit, operation,
                )
                return False
            self._used += cost
            return True

    @property
    def used(self) -> int:
        with self._lock:
            return self._used


_quota = _QuotaTracker()

_service_cache: Dict[str, Any] = {}
_service_lock = Lock()


def _get_service() -> Any:
    from config import config as _cfg
    api_key = _cfg.youtube_api_key
    if not api_key:
        raise ValueError("YouTube API key not configured.")
    with _service_lock:
        if api_key not in _service_cache:
            from googleapiclient.discovery import build
            _service_cache[api_key] = build(
                "youtube", "v3", developerKey=api_key, cache_discovery=False
            )
    return _service_cache[api_key]


def _retry(fn, max_attempts: int = 3, base_delay: float = 1.0) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            try:
                from googleapiclient.errors import HttpError
                if isinstance(exc, HttpError) and exc.resp.status in (403, 404):
                    raise
            except ImportError:
                pass
            logger.warning("YouTube API call failed (attempt %d): %s", attempt + 1, exc)
            last_exc = exc
        time.sleep(base_delay * (2 ** attempt))
    raise RuntimeError(f"YouTube API call failed after {max_attempts} attempts") from last_exc


def _data_api_video_details(video_id: str) -> Optional[Dict[str, Any]]:
    """Fetch video metadata via YouTube Data API v3 (requires key + quota)."""
    if not _quota.consume("videos.list"):
        return None
    try:
        service = _get_service()

        def _call():
            return service.videos().list(
                part="snippet,contentDetails,statistics", id=video_id,
            ).execute()

        response = _retry(_call)
    except ValueError as exc:
        logger.debug("YouTube Data API not configured: %s", exc)
        return None
    except Exception as exc:
        logger.error("YouTube Data API HTTP error for video %s: %s", video_id, exc)
        return None

    items = response.get("items", [])
    if not items:
        return None

    item = items[0]
    snippet = item.get("snippet", {})
    content = item.get("contentDetails", {})
    stats = item.get("statistics", {})

    try:
        import isodate
        duration_s = int(isodate.parse_duration(content.get("duration", "PT0S")).total_seconds())
    except Exception:
        duration_s = 0

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
        "duration_seconds": duration_s,
        "thumbnail_url": thumb_url,
        "view_count": int(stats.get("viewCount", 0) or 0),
        "like_count": int(stats.get("likeCount", 0) or 0),
        "comment_count": int(stats.get("commentCount", 0) or 0),
        "published_at": snippet.get("publishedAt", ""),
    }


def _data_api_channel_details(channel_id: str) -> Optional[Dict[str, Any]]:
    if not _quota.consume("channels.list"):
        return None
    try:
        service = _get_service()

        def _call():
            return service.channels().list(
                part="snippet,statistics", id=channel_id,
            ).execute()

        response = _retry(_call)
    except Exception as exc:
        logger.error("YouTube Data API error for channel %s: %s", channel_id, exc)
        return None

    items = response.get("items", [])
    if not items:
        return None
    snippet = items[0].get("snippet", {})
    stats = items[0].get("statistics", {})
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


def _data_api_channel_videos(channel_id: str, max_results: int = 50) -> List[str]:
    if not _quota.consume("search.list"):
        return []
    try:
        service = _get_service()

        def _call():
            return service.search().list(
                part="id",
                channelId=channel_id,
                type="video",
                order="date",
                maxResults=min(max_results, 50),
            ).execute()

        response = _retry(_call)
    except Exception as exc:
        logger.error("YouTube Data API channel videos failed for %s: %s", channel_id, exc)
        return []
    return [
        item["id"]["videoId"]
        for item in response.get("items", [])
        if item.get("id", {}).get("videoId")
    ]


# ---------------------------------------------------------------------------
# Public API — yt-dlp primary, Data API fallback
# ---------------------------------------------------------------------------


def get_video_details(video_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch comprehensive metadata for a YouTube video.

    Tries yt-dlp first (no API key, no quota).
    Falls back to YouTube Data API v3 if yt-dlp fails and a key is configured.

    Returns a dict with keys:
      video_id, title, description, tags, category_id, channel_id,
      channel_title, duration_seconds, thumbnail_url,
      view_count, like_count, comment_count, published_at
    """
    result = _ytdlp_video_meta(video_id)
    if result:
        return result

    logger.debug("yt-dlp failed for %s, trying Data API fallback.", video_id)
    return _data_api_video_details(video_id)


def get_channel_details(channel_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch metadata for a YouTube channel.

    Tries yt-dlp first; falls back to Data API.
    Note: subscriber/video/view counts are only available via the Data API.
    """
    result = _ytdlp_channel_meta(channel_id)
    if result:
        return result

    logger.debug("yt-dlp channel meta failed for %s, trying Data API fallback.", channel_id)
    return _data_api_channel_details(channel_id)


def get_channel_videos(channel_id: str, max_results: int = 50) -> List[str]:
    """
    Return a list of recent video IDs published by a channel.

    Tries yt-dlp first (free); falls back to Data API search.list
    (costs 100 quota units per call).
    """
    result = _ytdlp_channel_videos(channel_id, max_results)
    if result:
        return result

    logger.debug("yt-dlp channel videos failed for %s, trying Data API fallback.", channel_id)
    return _data_api_channel_videos(channel_id, max_results)


def get_video_captions(video_id: str) -> str:
    """
    Download auto-generated captions for a video using yt-dlp.

    Returns the caption text as a plain string, or empty string on failure.
    """
    with tempfile.TemporaryDirectory(prefix="brainrot_captions_") as tmpdir:
        output_template = os.path.join(tmpdir, "%(id)s.%(ext)s")
        out = _run_ytdlp(
            "--write-auto-sub",
            "--skip-download",
            "--sub-lang", "en",
            "--sub-format", "vtt",
            "--output", output_template,
            f"https://www.youtube.com/watch?v={video_id}",
            timeout=60,
        )

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
    """Strip WebVTT timing/formatting markup and return clean caption text."""
    import re
    lines = vtt_content.splitlines()
    text_lines: List[str] = []
    seen: set = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if "-->" in line:
            continue
        if line.isdigit():
            continue
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
    """Return how many YouTube Data API quota units have been consumed today."""
    return _quota.used
