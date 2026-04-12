"""
squid_redirector.py - Squid url_rewrite_program daemon.

Reads Squid URL rewrite protocol from stdin, one line per request.

Line format (Squid 3.5+):
  <ID> <URL> <client_ip>/<fqdn> <ident> <method> [kvpairs]\n

Writes to stdout for each input line:
  <ID> OK                                    — pass through unchanged
  <ID> OK rewrite-url=<new_url>              — redirect to block/warning page

Protocol documentation:
  https://wiki.squid-cache.org/Features/Redirectors

Decision logic for YouTube URLs:
  1. Whitelisted video or channel → allow (OK)
  2. Video status is "block"     → redirect to /blocked page
  3. Video status is "soft_block"→ redirect to /warning page
  4. Channel tier is "block"     → redirect to /blocked page (channel block)
  5. Unknown video               → allow through, queue async analysis
  6. All other URLs              → allow (OK)

Non-blocking design:
  - DB lookups happen synchronously (SQLite WAL = fast reads)
  - HTTP analysis requests are fired in a background thread pool (fire-and-forget)
  - We never block Squid waiting for analysis to complete
"""

from __future__ import annotations

import logging
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, Tuple

# Bootstrap path so we can import our modules when run directly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import config  # noqa: E402
from db_manager import db  # noqa: E402
from models import ActionTaken, RequestLog  # noqa: E402

# Configure logging to stderr so stdout remains clean for Squid protocol
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [redirector] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Thread pool for async analysis HTTP requests
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="redirector")

# ---------------------------------------------------------------------------
# YouTube URL parsing
# ---------------------------------------------------------------------------

_YOUTUBE_PATTERNS = [
    # Standard watch URL
    re.compile(r"(?:https?://)?(?:www\.|m\.)?youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})", re.I),
    # youtu.be short links
    re.compile(r"(?:https?://)?youtu\.be/([a-zA-Z0-9_-]{11})", re.I),
    # Shorts
    re.compile(r"(?:https?://)?(?:www\.|m\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})", re.I),
    # Embed
    re.compile(r"(?:https?://)?(?:www\.|m\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})", re.I),
    # YouTube Music
    re.compile(r"(?:https?://)?music\.youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})", re.I),
]


def extract_video_id(url: str) -> Optional[str]:
    """
    Parse a URL and return the 11-character YouTube video ID, or None.
    """
    for pattern in _YOUTUBE_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def is_youtube_url(url: str) -> bool:
    """Return True if URL is a YouTube domain we care about."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower().lstrip("www.").lstrip("m.")
        return host in (
            "youtube.com",
            "youtu.be",
            "music.youtube.com",
            "youtubekids.com",
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Squid line parser
# ---------------------------------------------------------------------------


def parse_squid_line(line: str) -> Tuple[str, str, str, str, str]:
    """
    Parse a Squid url_rewrite_program input line.

    Returns (request_id, url, client_ip, ident, method).
    client_ip may include /fqdn — we strip the fqdn part.
    """
    parts = line.strip().split(" ")
    if len(parts) < 3:
        return ("0", parts[0] if parts else "", "", "", "")

    request_id = parts[0]
    url = parts[1]
    client_part = parts[2]  # e.g. "192.168.1.10/client.local"
    client_ip = client_part.split("/")[0]
    ident = parts[3] if len(parts) > 3 else "-"
    method = parts[4] if len(parts) > 4 else "GET"

    return request_id, url, client_ip, ident, method


# ---------------------------------------------------------------------------
# Async analysis trigger
# ---------------------------------------------------------------------------


def _trigger_analysis(video_id: str, client_ip: str) -> None:
    """
    Fire an HTTP POST to the analyzer service in a background thread.
    Completely non-blocking from the caller's perspective.
    """
    import http.client
    import json as _json

    analyzer_url = config.analyzer_service_url
    parsed = urllib.parse.urlparse(analyzer_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8199

    payload = _json.dumps({"video_id": video_id, "priority": False}).encode()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/api/analyze",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
    except Exception as exc:
        logger.debug("Async analysis trigger failed for %s: %s", video_id, exc)


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------


def _make_decision(video_id: str, client_ip: str, user_agent: str) -> Tuple[str, str]:
    """
    Determine what to do with a request for *video_id*.

    Returns (action, redirect_url).
      action:       "allow" | "soft_block" | "block"
      redirect_url: URL to redirect to, or "" for pass-through
    """
    service_host = config.service_host
    service_port = config.service_port

    # 1. Check whitelist
    if db.is_whitelisted(video_id, "video"):
        return "allow", ""

    # 2. Look up existing video record
    status = db.get_video_status(video_id)

    if status == "block":
        redirect = f"http://{service_host}:{service_port}/blocked?video_id={video_id}"
        return "block", redirect

    if status == "soft_block":
        redirect = f"http://{service_host}:{service_port}/warning?video_id={video_id}"
        return "soft_block", redirect

    # 3. Check channel-level block (we don't know channel_id here, so
    #    we do a quick DB lookup via the videos table)
    video_row = db.get_video(video_id)
    if video_row:
        ch_id = video_row.get("channel_id", "")
        if ch_id:
            if not db.is_whitelisted(ch_id, "channel"):
                ch_tier = db.get_channel_tier(ch_id)
                if ch_tier == "block":
                    redirect = (
                        f"http://{service_host}:{service_port}/blocked"
                        f"?video_id={video_id}&channel_id={ch_id}&reason=channel"
                    )
                    return "block", redirect
                if ch_tier == "soft_block":
                    redirect = (
                        f"http://{service_host}:{service_port}/warning"
                        f"?video_id={video_id}&channel_id={ch_id}&reason=channel"
                    )
                    return "soft_block", redirect

    # 4. Unknown video — allow through and schedule analysis
    if status is None:
        _executor.submit(_trigger_analysis, video_id, client_ip)

    return "allow", ""


# ---------------------------------------------------------------------------
# Main redirector loop
# ---------------------------------------------------------------------------


def _log_request(
    client_ip: str,
    video_id: str,
    action: str,
    user_agent: str = "",
) -> None:
    """Persist a request log entry asynchronously."""
    try:
        log = RequestLog(
            client_ip=client_ip,
            video_id=video_id,
            channel_id="",
            timestamp=datetime.utcnow(),
            action_taken=ActionTaken(action),
            user_agent=user_agent,
        )
        db.log_request(log)
    except Exception as exc:
        logger.debug("Request log failed: %s", exc)


def run() -> None:
    """
    Main daemon loop.

    Reads lines from stdin (Squid url_rewrite_program protocol),
    writes decisions to stdout.  Never returns under normal operation.
    """
    logger.info("BrainrotFilter Squid redirector starting (PID %d).", os.getpid())

    # Ensure DB is initialised before processing requests
    try:
        db.initialize()
    except Exception as exc:
        logger.error("DB init failed: %s", exc)

    # Squid expects stdout to be unbuffered
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1)

    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue

        req_id, url, client_ip, ident, method = parse_squid_line(raw_line)

        try:
            video_id = extract_video_id(url)

            if video_id:
                action, redirect_url = _make_decision(video_id, client_ip, user_agent="")

                # Log asynchronously (fire-and-forget)
                if config.get_bool("log_all_requests"):
                    _executor.submit(_log_request, client_ip, video_id, action)

                if redirect_url:
                    response = f"{req_id} OK rewrite-url={redirect_url}\n"
                else:
                    response = f"{req_id} OK\n"
            else:
                # Non-YouTube URL or non-video YouTube page
                response = f"{req_id} OK\n"

        except Exception as exc:
            logger.error("Error processing request %s (%s): %s", req_id, url, exc)
            response = f"{req_id} OK\n"  # fail open — don't break internet

        sys.stdout.write(response)
        sys.stdout.flush()


if __name__ == "__main__":
    run()
