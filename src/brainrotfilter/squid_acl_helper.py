"""
squid_acl_helper.py - Squid external_acl_type helper for BrainrotFilter.

Protocol (Squid external_acl_type):
  STDIN:  <token>\n          (video_id or channel_id)
  STDOUT: OK\n               → token IS blocked (ACL match)
          ERR\n              → token is NOT blocked (no ACL match)
          BH message\n       → broken helper (error condition)

Squid configuration:
  external_acl_type brainrot_check ttl=60 concurrency=10 \
      %SRC %URI /usr/local/bin/brainrotfilter/squid_acl_helper.py

  acl brainrot_blocked external brainrot_check
  http_access deny brainrot_blocked

The helper checks:
  1. Videos table: status IN ('block', 'soft_block')
  2. Channels table: tier IN ('block', 'soft_block')
  3. In-memory TTL cache to minimise DB load

Note: For Squid external_acl_type with %SRC %URI, the token is the full URI.
      We extract video_id from the URI just like squid_redirector.py.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Dict, Optional, Tuple

# Bootstrap import path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import config  # noqa: E402
from db_manager import db  # noqa: E402

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [acl_helper] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory TTL cache
# ---------------------------------------------------------------------------


class _TTLCache:
    """
    Simple thread-safe TTL cache mapping token → (result, expires_at).

    result is True (blocked) or False (allowed).
    """

    def __init__(self, max_size: int = 10_000) -> None:
        self._store: Dict[str, Tuple[bool, float]] = {}
        self._max_size = max_size

    def get(self, key: str) -> Optional[bool]:
        entry = self._store.get(key)
        if entry is None:
            return None
        result, expires = entry
        if time.monotonic() > expires:
            del self._store[key]
            return None
        return result

    def set(self, key: str, blocked: bool, ttl: float) -> None:
        # Evict oldest quarter if full (simple strategy)
        if len(self._store) >= self._max_size:
            now = time.monotonic()
            expired_keys = [k for k, (_, exp) in self._store.items() if exp <= now]
            for k in expired_keys[:max(1, self._max_size // 4)]:
                del self._store[k]
        self._store[key] = (blocked, time.monotonic() + ttl)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


_cache = _TTLCache()


# ---------------------------------------------------------------------------
# Import video_id parser from squid_redirector (shared logic)
# ---------------------------------------------------------------------------


def _extract_video_id(token: str) -> Optional[str]:
    """Try to extract a YouTube video ID from the token."""
    try:
        from squid_redirector import extract_video_id

        return extract_video_id(token)
    except Exception:
        return None


def _is_valid_channel_id(token: str) -> bool:
    """Basic heuristic: YouTube channel IDs start with UC and are 24 chars."""
    token = token.strip()
    return len(token) == 24 and token.startswith("UC")


# ---------------------------------------------------------------------------
# Block check
# ---------------------------------------------------------------------------


def is_blocked(token: str) -> bool:
    """
    Return True if the token (video ID or channel ID) is blocked.

    Decision order:
    1. Check TTL cache
    2. Check whitelist
    3. Check videos table (if looks like a video ID)
    4. Check channels table (if looks like a channel ID)
    5. If a URL, try to extract video ID first, then look it up
    """
    token = token.strip()
    if not token or token == "-":
        return False

    ttl = float(config.acl_cache_ttl)

    # 1. Cache hit
    cached = _cache.get(token)
    if cached is not None:
        return cached

    result = False
    try:
        # 2. Try as a URL first (Squid often passes full URI)
        video_id = _extract_video_id(token)
        if video_id:
            # Whitelist check
            if db.is_whitelisted(video_id, "video"):
                _cache.set(token, False, ttl)
                return False

            status = db.get_video_status(video_id)
            if status in ("block", "soft_block"):
                result = True
            else:
                # Also check channel block via video record
                vid_row = db.get_video(video_id)
                if vid_row:
                    ch_id = vid_row.get("channel_id", "")
                    if ch_id:
                        if db.is_whitelisted(ch_id, "channel"):
                            result = False
                        else:
                            ch_tier = db.get_channel_tier(ch_id)
                            result = ch_tier in ("block", "soft_block")

        elif _is_valid_channel_id(token):
            # 4. Treat as channel ID
            if db.is_whitelisted(token, "channel"):
                result = False
            else:
                ch_tier = db.get_channel_tier(token)
                result = ch_tier in ("block", "soft_block")

        else:
            # 5. Unknown format — try as raw video ID (11 alphanumeric chars)
            if len(token) == 11 and token.replace("-", "").replace("_", "").isalnum():
                if db.is_whitelisted(token, "video"):
                    result = False
                else:
                    status = db.get_video_status(token)
                    result = status in ("block", "soft_block")

    except Exception as exc:
        logger.error("Error checking block status for %r: %s", token, exc)
        result = False

    _cache.set(token, result, ttl)
    return result


# ---------------------------------------------------------------------------
# Main ACL helper loop
# ---------------------------------------------------------------------------


def run() -> None:
    """
    Main daemon loop. Reads tokens from stdin, writes OK/ERR to stdout.
    """
    logger.info("BrainrotFilter ACL helper starting (PID %d).", os.getpid())

    # Ensure DB is initialised
    try:
        db.initialize()
    except Exception as exc:
        logger.error("DB init failed: %s", exc)

    # Squid requires line-buffered stdout
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1)

    for raw_line in sys.stdin:
        token = raw_line.strip()
        if not token:
            continue

        try:
            blocked = is_blocked(token)
            # Squid external_acl: OK = match (blocked), ERR = no match (allow)
            response = "OK\n" if blocked else "ERR\n"
        except Exception as exc:
            logger.error("ACL helper error for %r: %s", token, exc)
            response = "BH message=internal_error\n"

        sys.stdout.write(response)
        sys.stdout.flush()


def invalidate_cache_entry(token: str) -> None:
    """Remove a cache entry (called by analyzer_service after status change)."""
    _cache.invalidate(token)


def clear_cache() -> None:
    """Clear the entire cache (called after bulk DB updates)."""
    _cache.clear()


if __name__ == "__main__":
    run()
