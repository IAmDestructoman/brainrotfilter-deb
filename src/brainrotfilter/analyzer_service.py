"""
analyzer_service.py - Main FastAPI service for BrainrotFilter.

Startup sequence:
  1. Initialize database (create tables, load settings)
  2. Start background thread pool for video analysis jobs
  3. Serve FastAPI on port 8199

API Routes:
  POST /api/analyze             - Queue a video for analysis
  GET  /api/status/{video_id}   - Get analysis status
  POST /api/check               - Quick block check (used by Squid)
  POST /api/kill-state          - Kill connection states for a client IP
  GET  /api/stats               - Dashboard statistics
  GET  /api/videos              - List analyzed videos (pagination + filter)
  GET  /api/channels            - List channel profiles
  POST /api/whitelist           - Add to whitelist
  DELETE /api/whitelist/{id}    - Remove whitelist entry
  POST /api/override            - Manual status override
  GET  /api/settings            - Get current settings
  PUT  /api/settings            - Update settings
  GET  /api/logs                - Request logs

  GET  /                        - Admin panel dashboard
  GET  /videos                  - Admin videos page
  GET  /channels                - Admin channels page
  GET  /logs                    - Admin logs page
  GET  /settings                - Admin settings page
  GET  /whitelist               - Admin whitelist page
  GET  /blocked                 - Block page (shown by Squid redirect)
  GET  /warning                 - Warning page (shown for soft_block)
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Bootstrap path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)

from config import config, BLOCKED_VIDEOS_ACL, BLOCKED_CHANNELS_ACL  # noqa: E402
from db_manager import db  # noqa: E402
from models import (  # noqa: E402
    AnalyzeRequest,
    AnalyzeResponse,
    CheckRequest,
    CheckResponse,
    DashboardStats,
    KillStateRequest,
    KillStateResponse,
    OverrideRequest,
    Settings,
    VideoAnalysis,
    VideoStatus,
    WhitelistRequest,
)

# ---------------------------------------------------------------------------
# Parallel analyzer (drop-in replacement for _run_analysis)
# ---------------------------------------------------------------------------

try:
    from parallel_analyzer import parallel_analyze as _parallel_analyze
    _USE_PARALLEL_PIPELINE = True
    logger.info("Parallel analysis pipeline loaded.")
except ImportError as _pa_err:
    _parallel_analyze = None  # type: ignore[assignment]
    _USE_PARALLEL_PIPELINE = False
    logger.warning(
        "parallel_analyzer not available (%s); falling back to sequential pipeline.",
        _pa_err,
    )

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
# Templates and static assets — search multiple locations
WWW_DIR = Path(os.environ.get("BRAINROT_WWW_DIR", "/usr/share/brainrotfilter/www"))
TEMPLATES_DIR = WWW_DIR / "templates"
STATIC_DIR = WWW_DIR / "static"

# Fallback: check local dev paths (for development outside of package)
if not TEMPLATES_DIR.exists():
    for _try_dir in [
        BASE_DIR.parent.parent / "templates",  # src layout: ../../templates
        BASE_DIR.parent.parent / "www" / "brainrotfilter" / "templates",
        Path("/usr/local/www/brainrotfilter/templates"),
    ]:
        if _try_dir.exists():
            TEMPLATES_DIR = _try_dir
            STATIC_DIR = _try_dir.parent / "static"
            break

app = FastAPI(
    title="BrainrotFilter",
    description="Linux YouTube Brainrot Video Filter",
    version="1.0.0",
)

# Mount static files
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Jinja2 templates
templates: Optional[Jinja2Templates] = None
if TEMPLATES_DIR.exists():
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Analysis worker queue
# ---------------------------------------------------------------------------


class AnalysisQueue:
    """
    Thread-safe priority queue for video analysis jobs.

    Priority jobs (from UI override) jump to the front.
    Workers consume jobs from a thread pool.
    """

    def __init__(self, max_workers: int = 4, maxsize: int = 100) -> None:
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=maxsize)
        self._in_flight: set = set()
        self._job_steps: Dict[str, Dict[str, Any]] = {}  # video_id → step state
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="analyzer",
        )
        self._running = False

    def start(self) -> None:
        self._running = True
        # Start consumer threads
        for _ in range(self._executor._max_workers):
            self._executor.submit(self._worker)
        logger.info("Analysis queue started with %d workers.", self._executor._max_workers)

    def stop(self) -> None:
        self._running = False
        self._executor.shutdown(wait=False)

    def enqueue(self, video_id: str, priority: bool = False) -> bool:
        """
        Add *video_id* to the analysis queue.

        Returns False if the queue is full or the video is already queued.
        """
        with self._lock:
            if video_id in self._in_flight:
                return False

        prio = 0 if priority else 1  # lower = higher priority
        try:
            self._queue.put_nowait((prio, time.monotonic(), video_id))
            with self._lock:
                self._in_flight.add(video_id)
            logger.debug("Queued analysis for %s (priority=%s).", video_id, priority)
            return True
        except queue.Full:
            logger.warning("Analysis queue full; dropping %s.", video_id)
            return False

    def size(self) -> int:
        return self._queue.qsize()

    def set_step(self, video_id: str, step: str, title: str = "") -> None:
        """Update the current processing step for an in-flight job (called from worker thread)."""
        with self._lock:
            job = self._job_steps.get(video_id)
            if job is not None:
                job["step"] = step
                if title and not job.get("title"):
                    job["title"] = title

    def _begin_job(self, video_id: str) -> None:
        with self._lock:
            self._job_steps[video_id] = {
                "video_id": video_id,
                "step": "starting",
                "title": "",
                "started_at": time.monotonic(),
            }

    def _end_job(self, video_id: str) -> None:
        with self._lock:
            self._job_steps.pop(video_id, None)

    def get_in_progress(self) -> List[Dict[str, Any]]:
        """Return snapshot of all actively-running jobs with their current step."""
        with self._lock:
            now = time.monotonic()
            return [
                {**job, "elapsed_s": round(now - job["started_at"], 1)}
                for job in self._job_steps.values()
            ]

    def _worker(self) -> None:
        """Worker thread: consume jobs from the queue forever."""
        while self._running:
            try:
                _, _, video_id = self._queue.get(timeout=2)
            except queue.Empty:
                continue
            self._begin_job(video_id)
            try:
                if _USE_PARALLEL_PIPELINE and _parallel_analyze is not None:
                    _parallel_analyze(video_id)
                else:
                    _run_analysis(video_id)
            except Exception as exc:
                logger.error("Unhandled error analyzing %s: %s", video_id, exc)
            finally:
                self._end_job(video_id)
                with self._lock:
                    self._in_flight.discard(video_id)
                # Release any clients that were waiting on this video so their
                # CDN access is restored (analysis outcome already written to DB).
                _clear_client_pending(video_id)
                self._queue.task_done()


analysis_queue = AnalysisQueue(
    max_workers=config.get_int("analysis_worker_threads"),
    maxsize=config.get_int("analysis_queue_maxsize"),
)


# ---------------------------------------------------------------------------
# Pre-emptive CDN blocking
#
# When a client starts watching a new (unanalyzed) video we deny their
# googlevideo.com requests until analysis completes, so the player can't
# pre-buffer content that might later be blocked.
#
# Key -> client_ip; Value -> set of video_ids still awaiting analysis.
# A client is "pending" while their set is non-empty.  Entries are also
# guarded by a hard timeout so a stuck video doesn't block a client forever.
# ---------------------------------------------------------------------------
_pending_clients: Dict[str, Dict[str, float]] = {}   # client_ip -> {video_id: expiry}
_pending_lock = threading.Lock()
PENDING_TIMEOUT_SECONDS = 180  # hard cap in case analysis is lost

# Defensive fallback: track last time we successfully identified a video_id
# for a client.  If a client makes CDN requests but has no recent identify,
# deny the CDN so the player stalls and triggers a fresh page/thumbnail load.
_last_identify: Dict[str, float] = {}
_identify_lock = threading.Lock()
# Stale window must be longer than typical idle periods between stats URLs
# (~30s during playback, longer when browsing sidebars/paused). Too short
# and the defensive CDN block locks up normal usage; too long and a
# cached-thumbnail autoplay can slip in unnoticed.
IDENTIFY_STALE_SECONDS = 180

# Per-client set of recently-requested BLOCKED video_ids.  A client is
# CDN-blocked while any of these entries are still fresh.  Entries are
# refreshed every time the blocked video is re-identified (stats URLs
# fire ~every 30s during playback) and expire naturally once the client
# stops requesting that video's URLs.  Ads / sidebar thumbnails of
# allowed videos do NOT remove the entries, so mid-block ad breaks no
# longer accidentally unblock the player.
_cdn_blocked_clients: Dict[str, Dict[str, float]] = {}  # ip -> {video_id: expiry}
_cdn_block_lock = threading.Lock()
CDN_BLOCK_ENTRY_TTL = 40  # seconds — slightly > player's ~30s stats interval.
#   Short enough that once a user navigates away, the block evaporates
#   within tens of seconds (no prolonged lockout of unrelated YouTube
#   browsing). While the user is still on the blocked video, every
#   denied stats URL re-hits /api/check and refreshes the entry back
#   to full TTL so playback stays blocked. /api/check returning allow
#   for a DIFFERENT video also clears the set immediately.


def _block_client_cdn(client_ip: str, video_id: str = "", duration: int = CDN_BLOCK_ENTRY_TTL) -> None:
    if not client_ip or client_ip in ("-", "unknown", "localhost"):
        return
    # Use a synthetic key when called without a specific video_id (e.g. from
    # the analysis completion hook with no recent client context).
    key = video_id or "__generic__"
    with _cdn_block_lock:
        _cdn_blocked_clients.setdefault(client_ip, {})[key] = time.monotonic() + duration


def _unblock_client_cdn(client_ip: str) -> None:
    """Remove ALL CDN block entries for a client (explicit override)."""
    with _cdn_block_lock:
        _cdn_blocked_clients.pop(client_ip, None)


def _is_client_cdn_blocked(client_ip: str) -> bool:
    if not client_ip:
        return False
    now = time.monotonic()
    with _cdn_block_lock:
        entries = _cdn_blocked_clients.get(client_ip)
        if not entries:
            return False
        # Drop expired entries
        expired = [vid for vid, exp in entries.items() if now >= exp]
        for vid in expired:
            entries.pop(vid, None)
        if not entries:
            _cdn_blocked_clients.pop(client_ip, None)
            return False
        return True


def _mark_client_pending(client_ip: str, video_id: str) -> None:
    if not client_ip or client_ip in ("-", "unknown", "localhost"):
        return
    expiry = time.monotonic() + PENDING_TIMEOUT_SECONDS
    with _pending_lock:
        _pending_clients.setdefault(client_ip, {})[video_id] = expiry


def _clear_client_pending(video_id: str) -> None:
    """Remove *video_id* from every client's pending set."""
    with _pending_lock:
        for cip in list(_pending_clients.keys()):
            _pending_clients[cip].pop(video_id, None)
            if not _pending_clients[cip]:
                _pending_clients.pop(cip, None)


def _is_client_pending(client_ip: str) -> bool:
    if not client_ip:
        return False
    now = time.monotonic()
    with _pending_lock:
        entries = _pending_clients.get(client_ip)
        if not entries:
            return False
        # Drop expired entries
        expired = [vid for vid, exp in entries.items() if now >= exp]
        for vid in expired:
            entries.pop(vid, None)
        if not entries:
            _pending_clients.pop(client_ip, None)
            return False
        return True


def _record_identify(client_ip: str) -> None:
    """Record that we just identified a video_id for *client_ip* via api/check."""
    if not client_ip or client_ip in ("-", "unknown", "localhost"):
        return
    with _identify_lock:
        _last_identify[client_ip] = time.monotonic()


def _client_identify_stale(client_ip: str) -> bool:
    """Return True if client has NO recent video_id identification.

    Used by the CDN helper: if a client is streaming googlevideo.com but we
    cannot tell which video (cached thumbnails, stats URLs bypassing the VM),
    deny the CDN so the player stalls.  That forces a page reload which in
    turn re-requests identifying URLs through Squid.
    """
    if not client_ip or client_ip in ("-", "unknown", "localhost"):
        return False
    with _identify_lock:
        ts = _last_identify.get(client_ip)
    if ts is None:
        return True
    return (time.monotonic() - ts) > IDENTIFY_STALE_SECONDS


# Background thread: clean up expired iptables block rules every 30 s
def _iptables_cleanup_loop() -> None:
    while True:
        time.sleep(30)
        try:
            from state_killer import cleanup_expired_blocks
            cleanup_expired_blocks()
        except Exception as exc:
            logger.debug("iptables cleanup error: %s", exc)


threading.Thread(target=_iptables_cleanup_loop, daemon=True, name="iptables-cleanup").start()


# ---------------------------------------------------------------------------
# Core analysis orchestrator
# ---------------------------------------------------------------------------


def _run_analysis(video_id: str) -> None:
    """
    Full analysis pipeline for a single video.

    Called by worker threads — must not raise.
    """
    logger.info("Starting analysis for video: %s", video_id)
    start = time.monotonic()

    # 1. Fetch metadata from YouTube API
    analysis_queue.set_step(video_id, "metadata")
    yt_data: Optional[Dict[str, Any]] = None
    try:
        from youtube_api import get_video_details

        yt_data = get_video_details(video_id)
    except Exception as exc:
        logger.warning("Failed to fetch YouTube metadata for %s: %s", video_id, exc)

    title = yt_data.get("title", "") if yt_data else ""
    description = yt_data.get("description", "") if yt_data else ""
    tags = yt_data.get("tags", []) if yt_data else []
    category_id = yt_data.get("category_id", "") if yt_data else ""
    channel_id = yt_data.get("channel_id", "") if yt_data else ""
    thumbnail_url = yt_data.get("thumbnail_url", "") if yt_data else ""
    duration_s = yt_data.get("duration_seconds", 0) if yt_data else 0

    # 2. Keyword analysis (fast — no download)
    analysis_queue.set_step(video_id, "keywords", title=title)
    keyword_result = None
    keyword_score = 0.0
    keyword_matches = []
    try:
        import keyword_analyzer

        keyword_result = keyword_analyzer.analyze(
            video_id=video_id,
            title=title,
            description=description,
            tags=tags,
            thumbnail_url=thumbnail_url,
            category_id=category_id,
        )
        keyword_score = keyword_result.score
        kw_details = keyword_result.details or {}
        keyword_matches = kw_details.get("matched_keywords", [])
    except Exception as exc:
        logger.error("Keyword analysis failed for %s: %s", video_id, exc)

    # 3. Scene analysis (requires video download)
    analysis_queue.set_step(video_id, "scene")
    scene_score = 0.0
    scene_details_raw: Dict[str, Any] = {}
    try:
        import scene_analyzer

        scene_result = scene_analyzer.analyze(
            video_id=video_id,
            category_id=category_id,
            duration_seconds=duration_s,
        )
        scene_score = scene_result.score
        scene_details_raw = scene_result.details or {}
    except Exception as exc:
        logger.error("Scene analysis failed for %s: %s", video_id, exc)

    # 4. Audio analysis (requires the video downloaded by scene analyzer to be re-used
    analysis_queue.set_step(video_id, "audio")
    #    or downloaded fresh — here we do a fresh minimal download for audio)
    audio_score = 0.0
    audio_details_raw: Dict[str, Any] = {}
    try:
        import scene_analyzer as _sa
        import audio_analyzer

        with tempfile.TemporaryDirectory(prefix="brainrot_audio_svc_") as tmpdir:
            video_path = os.path.join(tmpdir, f"{video_id}_audio.mp4")
            scan_dur = config.initial_scan_duration
            downloaded = _sa._download_video_segment(video_id, video_path, scan_dur)
            if downloaded:
                audio_result = audio_analyzer.analyze(
                    video_path=video_path,
                    video_id=video_id,
                )
                audio_score = audio_result.score
                audio_details_raw = audio_result.details or {}
            else:
                logger.warning("Audio download failed for %s; audio_score=0.", video_id)
    except Exception as exc:
        logger.error("Audio analysis failed for %s: %s", video_id, exc)

    # 5. Combined score + tier
    analysis_queue.set_step(video_id, "scoring")
    combined_score = config.compute_combined_score(keyword_score, scene_score, audio_score)
    status_str = config.score_to_status(combined_score)

    # 6. Build VideoAnalysis object
    from models import SceneDetails, AudioDetails

    def _safe_model(cls, data):
        try:
            return cls(**data) if data else None
        except Exception:
            return None

    # Reconstruct keyword matches from details
    from models import KeywordMatch as _KM

    matched_kw_objects = []
    for kw in keyword_matches:
        try:
            matched_kw_objects.append(_KM(**kw))
        except Exception:
            pass

    video = VideoAnalysis(
        video_id=video_id,
        channel_id=channel_id,
        title=title,
        description=description[:500],  # truncate for DB storage
        thumbnail_url=thumbnail_url,
        keyword_score=keyword_score,
        scene_score=scene_score,
        audio_score=audio_score,
        combined_score=combined_score,
        status=VideoStatus(status_str),
        matched_keywords=matched_kw_objects,
        scene_details=_safe_model(SceneDetails, scene_details_raw),
        audio_details=_safe_model(AudioDetails, audio_details_raw),
        analyzed_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    # 7. Persist to DB
    try:
        db.upsert_video(video)
    except Exception as exc:
        logger.error("Failed to persist video record for %s: %s", video_id, exc)
        return

    # 8. Update ACL files
    try:
        _update_acl_files()
    except Exception as exc:
        logger.error("ACL file update failed: %s", exc)

    # 9. Kill active states if video was blocked or soft_blocked
    if status_str in ("block", "soft_block"):
        try:
            from state_killer import kill_states_for_video

            # Find all clients currently watching this video from request logs
            recent_clients = db.get_recent_clients_for_video(video_id, max_age_seconds=300)
            for cip in recent_clients:
                success, count = kill_states_for_video(cip, video_id)
                if count > 0:
                    logger.info(
                        "Killed %d state(s) for client %s watching flagged video %s",
                        count, cip, video_id,
                    )
                # Sustained CDN deny — stops the player from refilling the
                # buffer after the brief iptables RST window expires.
                if status_str == "block":
                    _block_client_cdn(cip, video_id=video_id)
        except Exception as exc:
            logger.error("State kill after flagging failed for %s: %s", video_id, exc)

    # 10. Update channel profile
    if channel_id:
        try:
            from channel_profiler import update_channel_after_video

            update_channel_after_video(channel_id, status_str)
        except Exception as exc:
            logger.error("Channel profile update failed for %s: %s", channel_id, exc)

    elapsed = time.monotonic() - start
    logger.info(
        "Analysis complete for %s: status=%s, combined=%.1f "
        "(kw=%.1f, scene=%.1f, audio=%.1f) in %.1fs",
        video_id,
        status_str,
        combined_score,
        keyword_score,
        scene_score,
        audio_score,
        elapsed,
    )


def _update_acl_files() -> None:
    """
    Regenerate the blocked_videos.acl and blocked_channels.acl files
    that Squid uses for ACL lookups.
    """
    blocked_video_ids = db.get_blocked_video_ids()
    blocked_channel_ids = db.get_blocked_channel_ids()

    _write_acl_file(BLOCKED_VIDEOS_ACL, blocked_video_ids)
    _write_acl_file(BLOCKED_CHANNELS_ACL, blocked_channel_ids)
    logger.debug(
        "ACL files updated: %d videos, %d channels.",
        len(blocked_video_ids),
        len(blocked_channel_ids),
    )


def _write_acl_file(path: str, ids: List[str]) -> None:
    """Atomically write an ACL file."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("\n".join(ids) + ("\n" if ids else ""))
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("Failed to write ACL file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Wizard integration — redirect to setup wizard on first access
# ---------------------------------------------------------------------------

try:
    from wizard_integration import integrate as _integrate_wizard
    _integrate_wizard(app)
    logger.info("Setup wizard integrated.")
except ImportError:
    logger.debug("wizard_integration module not found; wizard disabled.")
except Exception as exc:
    logger.warning("Wizard integration failed: %s", exc)


# ---------------------------------------------------------------------------
# Optional feature routers (profiles, ML, community keywords)
# ---------------------------------------------------------------------------

for _mod_name, _route_prefix in [
    ("profile_routes", "/api/profiles"),
    ("ml_routes", "/api/ml"),
    ("community_routes", "/api/community"),
    ("uninstall_routes", "/uninstall"),
    ("youtubei_shim", "/shim/youtubei/v1"),
]:
    try:
        import importlib as _importlib
        _mod = _importlib.import_module(_mod_name)
        app.include_router(_mod.router)
        logger.info("Optional router '%s' registered at %s.", _mod_name, _route_prefix)
    except ImportError:
        logger.debug("Optional module %s not available; skipping router.", _mod_name)
    except Exception as _reg_exc:
        logger.warning("Failed to register router '%s': %s", _mod_name, _reg_exc)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("BrainrotFilter analyzer service starting up.")
    db.initialize()

    # Run database migrations
    try:
        from db_migrations import run_migrations
        run_migrations()
        logger.info("Database migrations applied successfully.")
    except Exception as mig_exc:
        logger.error("Database migration failed: %s", mig_exc)

    config.load()
    analysis_queue.start()

    # Log mounted routes
    mounted = [r.path for r in app.routes if hasattr(r, "path")]
    logger.info(
        "Service ready on port %d. Mounted %d routes.",
        config.service_port,
        len(mounted),
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("BrainrotFilter analyzer service shutting down.")
    analysis_queue.stop()


# ---------------------------------------------------------------------------
# API: Analysis
# ---------------------------------------------------------------------------


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def api_analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    """Queue a video for analysis. Returns immediately; analysis is async."""
    video_id = req.video_id.strip()
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id is required")

    # Log the client IP so the state killer can find active viewers later
    if req.client_ip:
        try:
            from models import RequestLog, ActionTaken
            db.log_request(RequestLog(
                client_ip=req.client_ip,
                video_id=video_id,
                channel_id="",
                timestamp=datetime.utcnow(),
                action_taken=ActionTaken.PENDING,
            ))
        except Exception:
            pass

    queued = analysis_queue.enqueue(video_id, priority=req.priority)

    # Pre-emptive CDN block: mark the client as pending so their googlevideo.com
    # requests are denied until analysis completes.  Also block if the video is
    # already in-flight (duplicate queue attempt).
    if req.client_ip:
        existing = db.get_video(video_id) if not queued else None
        # Only mark pending when we do not yet have a verdict for this video.
        if not existing or existing.get("status") in (None, "pending", ""):
            _mark_client_pending(req.client_ip, video_id)

    return AnalyzeResponse(
        video_id=video_id,
        queued=queued,
        message="Queued for analysis" if queued else "Already in queue or duplicate",
    )


@app.get("/api/client-pending")
async def api_client_pending(ip: str = Query(...)) -> Dict[str, Any]:
    """Return whether *ip* should have its CDN requests denied.

    Returns pending=true if either:
      (a) client has at least one video awaiting analysis, or
      (b) client has made NO video_id identification in the last
          IDENTIFY_STALE_SECONDS — i.e. we can't tell what they're watching.
    Case (b) catches cached-thumbnail / DNS-bypass situations where video
    chunks stream through but no identifying URL reaches Squid.
    """
    pending = _is_client_pending(ip)
    cdn_blocked = _is_client_cdn_blocked(ip)
    # Note: stale/no_identify defensive check is intentionally disabled.
    # In testing it caused too many false-positive denies for legitimate
    # playback because real-world stats URLs sometimes lack docid and
    # the helper's background heartbeat doesn't refresh reliably from
    # Squid's helper environment. Per-video CDN block (analyzing or
    # known-blocked) still enforces actual blocking decisions.
    reason = "ok"
    if cdn_blocked:
        reason = "cdn_blocked"
    elif pending:
        reason = "analyzing"
    return {
        "pending": pending or cdn_blocked,
        "reason": reason,
        "client_ip": ip,
    }


@app.get("/api/analysis/status")
async def api_analysis_status() -> Dict[str, Any]:
    """Return current analysis queue state including in-progress jobs with their step."""
    return {
        "queue_size": analysis_queue.size(),
        "in_progress": analysis_queue.get_in_progress(),
    }


@app.get("/api/status/{video_id}")
async def api_status(video_id: str) -> Dict[str, Any]:
    """Return the current analysis status for a video."""
    video = db.get_video(video_id)
    if not video:
        return {"video_id": video_id, "status": "unknown", "found": False}
    return {
        "video_id": video_id,
        "status": video.get("status", "unknown"),
        "combined_score": video.get("combined_score", 0),
        "keyword_score": video.get("keyword_score", 0),
        "scene_score": video.get("scene_score", 0),
        "audio_score": video.get("audio_score", 0),
        "analyzed_at": video.get("analyzed_at"),
        "found": True,
    }


# ---------------------------------------------------------------------------
# API: Quick check (used by Squid)
# ---------------------------------------------------------------------------


def _redirect_host() -> str:
    """Return the IP/hostname that remote browsers can use to reach this service.

    service_host is the bind address (often 0.0.0.0), which is not routable
    from a client browser.  When it is 0.0.0.0 we auto-detect the primary
    outbound IP so that block/warning redirect URLs actually work.
    """
    host = config.service_host
    if host and host != "0.0.0.0":
        return host
    import socket
    try:
        # Connecting a UDP socket does not send any data but forces the OS to
        # pick the outbound interface, revealing the primary non-loopback IP.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


@app.post("/api/check", response_model=CheckResponse)
async def api_check(req: CheckRequest) -> CheckResponse:
    """
    Fast block check for Squid.
    Checks both video_id and channel_id against the DB.
    """
    service_host = _redirect_host()
    port = config.service_port

    def _log_client(video_id: str = "", channel_id: str = "", action: str = "allow") -> None:
        """Log the requesting client IP so the state killer can find active viewers."""
        if not req.client_ip or req.client_ip in ("-", "unknown", "localhost"):
            return
        try:
            from models import RequestLog, ActionTaken
            action_map = {
                "block": ActionTaken.BLOCK,
                "soft_block": ActionTaken.SOFT_BLOCK,
                "allow": ActionTaken.ALLOW,
            }
            db.log_request(RequestLog(
                client_ip=req.client_ip,
                video_id=video_id,
                channel_id=channel_id,
                timestamp=datetime.utcnow(),
                action_taken=action_map.get(action, ActionTaken.ALLOW),
            ))
        except Exception:
            pass

    # Any /api/check call from a real client counts as "live" traffic for
    # the defensive CDN fallback — the client is actively using YouTube,
    # we just may not always have a video_id to go with it (session-level
    # telemetry without docid, non-playback URLs, etc.). Refreshing here
    # prevents an active session from sliding into no_identify state.
    _record_identify(req.client_ip or "")

    if req.video_id:
        vid = req.video_id.strip()
        if db.is_whitelisted(vid, "video"):
            _log_client(video_id=vid, action="allow")
            # Explicit whitelist is an admin override -> clear any CDN block
            if req.client_ip:
                _unblock_client_cdn(req.client_ip)
            return CheckResponse(action="allow", video_id=vid, reason="whitelisted")

        status = db.get_video_status(vid)
        if status == "block":
            _log_client(video_id=vid, action="block")
            # Only actual navigation URLs (watch/shorts/embed/youtu.be)
            # or an already-existing block get to set/refresh the CDN
            # block. Stats URLs and storyboards for a blocked video can
            # fire from home-feed previews the user isn't actually on;
            # those should not re-engage the block if we already cleared
            # it.
            src = (req.source or "navigation").lower()
            already_blocked = _is_client_cdn_blocked(req.client_ip or "")
            if req.client_ip and (src == "navigation" or already_blocked):
                _block_client_cdn(req.client_ip, video_id=vid)
            return CheckResponse(
                action="block",
                video_id=vid,
                redirect_url=f"http://{service_host}:{port}/blocked?video_id={vid}",
                reason="blocked",
            )
        if status == "soft_block":
            _log_client(video_id=vid, action="soft_block")
            return CheckResponse(
                action="soft_block",
                video_id=vid,
                redirect_url=f"http://{service_host}:{port}/warning?video_id={vid}",
                reason="soft_blocked",
            )

        # Known-allowed video (not blocked, not soft_blocked, not pending).
        # If the client has block entries for OTHER videos, clear them —
        # they've navigated to allowed content so their CDN should unblock
        # immediately without waiting for the TTL.
        if req.client_ip:
            _unblock_client_cdn(req.client_ip)

    if req.channel_id:
        ch = req.channel_id.strip()
        if db.is_whitelisted(ch, "channel"):
            _log_client(channel_id=ch, action="allow")
            return CheckResponse(action="allow", channel_id=ch, reason="channel_whitelisted")

        tier = db.get_channel_tier(ch)
        if tier == "block":
            _log_client(channel_id=ch, action="block")
            return CheckResponse(
                action="block",
                channel_id=ch,
                redirect_url=f"http://{service_host}:{port}/blocked?channel_id={ch}&reason=channel",
                reason="channel_blocked",
            )
        if tier == "soft_block":
            _log_client(channel_id=ch, action="soft_block")
            return CheckResponse(
                action="soft_block",
                channel_id=ch,
                redirect_url=f"http://{service_host}:{port}/warning?channel_id={ch}&reason=channel",
                reason="channel_soft_blocked",
            )

    # Video/channel not blocked.  We deliberately do NOT unblock the CDN
    # here: the new per-video TTL entries expire on their own when the
    # blocked video stops being requested, and an ad or sidebar thumbnail
    # of an allowed video should not reset that timer.
    return CheckResponse(action="allow", reason="not_blocked")


# ---------------------------------------------------------------------------
# API: State killing
# ---------------------------------------------------------------------------


@app.post("/api/kill-state", response_model=KillStateResponse)
async def api_kill_state(req: KillStateRequest) -> KillStateResponse:
    """Kill connection states for a client IP streaming YouTube."""
    from state_killer import kill_states_for_video

    try:
        success, count = kill_states_for_video(req.client_ip, req.video_id)
        return KillStateResponse(
            success=success,
            states_killed=count,
            message=f"Killed {count} state(s) for {req.client_ip}",
        )
    except Exception as exc:
        logger.error("State kill failed: %s", exc)
        return KillStateResponse(
            success=False,
            states_killed=0,
            message=f"Error: {exc}",
        )


# ---------------------------------------------------------------------------
# API: Statistics
# ---------------------------------------------------------------------------


@app.get("/api/stats", response_model=DashboardStats)
async def api_stats() -> DashboardStats:
    """Return aggregated dashboard statistics."""
    stats = db.get_dashboard_stats()
    stats.queue_size = analysis_queue.size()
    return stats


# ---------------------------------------------------------------------------
# API: Videos
# ---------------------------------------------------------------------------


@app.get("/api/videos")
async def api_videos(
    status: Optional[str] = Query(None),
    channel_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=2000),
    limit: Optional[int] = Query(None, ge=1, le=2000),
    order_by: str = Query("analyzed_at"),
    order_dir: str = Query("DESC"),
) -> Dict[str, Any]:
    """List analyzed videos with optional filtering and pagination."""
    # Accept 'limit' as an alias for 'per_page' (older frontend used this name).
    if limit is not None:
        per_page = limit
    items, total = db.get_videos(
        status=status,
        channel_id=channel_id,
        search=search,
        page=page,
        per_page=per_page,
        order_by=order_by,
        order_dir=order_dir,
    )
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": math.ceil(total / per_page) if total > 0 else 1,
        "items": items,
    }


@app.post("/api/videos/recalculate")
async def api_videos_recalculate() -> Dict[str, Any]:
    """Re-compute combined_score and status for all non-manually-overridden videos.

    Uses current config weights/thresholds against the individual scores already
    stored in the database.  Manual overrides are left untouched.
    """
    rows = db.get_all_videos_for_recalculate()

    updates: List[Dict[str, Any]] = []
    breakdown: Dict[str, int] = {}
    status_changed = 0

    for row in rows:
        new_score = config.compute_combined_score(
            keyword=row.get("keyword_score") or 0,
            scene=row.get("scene_score") or 0,
            audio=row.get("audio_score") or 0,
            comment=row.get("comment_score") or 0,
            engagement=row.get("engagement_score") or 0,
            thumbnail=row.get("thumbnail_score") or 0,
            shorts_bonus=row.get("shorts_score") or 0,
        )
        new_status = config.score_to_status(new_score)
        old_status = row.get("status") or "allow"
        old_score = float(row.get("combined_score") or 0)

        score_changed = abs(new_score - old_score) > 0.005
        if score_changed or new_status != old_status:
            updates.append(
                {
                    "video_id": row["video_id"],
                    "combined_score": new_score,
                    "status": new_status,
                }
            )
            if new_status != old_status:
                key = f"{old_status}\u2192{new_status}"
                breakdown[key] = breakdown.get(key, 0) + 1
                status_changed += 1

    if updates:
        db.update_video_scores_bulk(updates)

    return {
        "total": len(rows),
        "recalculated": len(updates),
        "changed": status_changed,
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# API: Channels
# ---------------------------------------------------------------------------


@app.get("/api/channels")
async def api_channels(
    tier: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """List channel profiles."""
    items, total = db.get_channels(tier=tier, search=search, page=page, per_page=per_page)
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": math.ceil(total / per_page) if total > 0 else 1,
        "items": items,
    }


@app.post("/api/channels/{channel_id}/refresh")
async def api_refresh_channel(channel_id: str) -> Dict[str, Any]:
    """Re-profile a channel (background task)."""
    from channel_profiler import profile_channel

    threading.Thread(
        target=profile_channel,
        args=(channel_id,),
        daemon=True,
        name=f"channel_refresh_{channel_id}",
    ).start()
    return {"status": "refresh_queued", "channel_id": channel_id}


# ---------------------------------------------------------------------------
# API: Whitelist
# ---------------------------------------------------------------------------


@app.post("/api/whitelist")
async def api_add_whitelist(req: WhitelistRequest) -> Dict[str, Any]:
    """Add a video or channel to the whitelist."""
    from models import WhitelistEntry

    entry = WhitelistEntry(
        type=req.type,
        target_id=req.target_id,
        added_by=req.added_by,
        reason=req.reason,
    )
    db.add_whitelist(entry)
    _update_acl_files()
    return {"status": "added", "type": req.type, "target_id": req.target_id}


@app.delete("/api/whitelist/{entry_id}")
async def api_remove_whitelist(entry_id: int) -> Dict[str, Any]:
    """Remove a whitelist entry by ID."""
    found = db.remove_whitelist(entry_id)
    if not found:
        raise HTTPException(status_code=404, detail="Whitelist entry not found")
    _update_acl_files()
    return {"status": "removed", "id": entry_id}


@app.get("/api/whitelist")
async def api_list_whitelist(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """List all whitelist entries."""
    items, total = db.get_whitelist(page=page, per_page=per_page)
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": math.ceil(total / per_page) if total > 0 else 1,
        "items": items,
    }


# ---------------------------------------------------------------------------
# API: Manual override
# ---------------------------------------------------------------------------


@app.post("/api/override")
async def api_override(req: OverrideRequest) -> Dict[str, Any]:
    """Manually set the status of a video (admin override)."""
    status_val = req.action if isinstance(req.action, str) else req.action.value
    db.set_video_status(
        req.video_id,
        status_val,
        manual_override=True,
        override_by=req.override_by,
    )
    _update_acl_files()
    return {
        "status": "updated",
        "video_id": req.video_id,
        "new_status": status_val,
        "override_by": req.override_by,
    }


# ---------------------------------------------------------------------------
# API: Settings
# ---------------------------------------------------------------------------


@app.get("/api/settings")
async def api_get_settings() -> Dict[str, Any]:
    """Return all current settings."""
    return config.all()


@app.put("/api/settings")
async def api_update_settings(new_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Update one or more settings."""
    allowed_keys = set(Settings.model_fields.keys())
    # Also allow any key from defaults dict
    from config import DEFAULTS

    allowed_keys.update(DEFAULTS.keys())

    # Filter to only known keys — silently ignore unknown keys so the
    # frontend can evolve without backend 400 errors.
    accepted = {k: v for k, v in new_settings.items() if k in allowed_keys}

    config.save_many(accepted)
    config.refresh()
    return {"status": "updated", "updated_keys": list(new_settings.keys())}


@app.post("/api/settings/test-youtube-api")
async def settings_test_youtube_api(body: dict) -> dict:
    """Test a YouTube API key — called from the settings page."""
    api_key = body.get("api_key", "")
    if not api_key:
        return {"valid": False, "message": "No API key provided"}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"id": "dQw4w9WgXcQ", "part": "snippet", "key": api_key},
            )
        if resp.status_code == 200 and resp.json().get("items"):
            return {"valid": True, "message": "API key is valid"}
        elif resp.status_code == 403:
            return {
                "valid": False,
                "message": "API key rejected (403) — check YouTube Data API v3 is enabled",
            }
        else:
            return {
                "valid": False,
                "message": f"Unexpected response: HTTP {resp.status_code}",
            }
    except Exception as exc:
        return {"valid": False, "message": f"Connection error: {exc}"}


# ---------------------------------------------------------------------------
# API: Logs
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def api_logs(
    client_ip: Optional[str] = Query(None),
    video_id: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    since_hours: int = Query(24, ge=1, le=720),
) -> Dict[str, Any]:
    """Return request logs with optional filtering."""
    items, total = db.get_logs(
        client_ip=client_ip,
        video_id=video_id,
        action=action,
        page=page,
        per_page=per_page,
        since_hours=since_hours,
    )
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": math.ceil(total / per_page) if total > 0 else 1,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Admin panel routes
# ---------------------------------------------------------------------------


def _template_response(request: Request, template_name: str, context: Dict = None):
    """Render a Jinja2 template or return a fallback JSON if templates missing."""
    if templates is None:
        return JSONResponse({"error": "Templates not installed", "page": template_name})
    ctx = context or {}
    return templates.TemplateResponse(request, template_name, ctx)


@app.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    try:
        stats = db.get_dashboard_stats()
        stats.queue_size = analysis_queue.size()
        return _template_response(request, "dashboard.html", {"stats": stats})
    except Exception:
        tb = traceback.format_exc()
        logger.error("Admin panel render error: %s", tb)
        return HTMLResponse(
            f"<h1>Admin Panel Error</h1><pre>{tb}</pre>"
            f"<p><a href='/wizard'>Go to Setup Wizard</a></p>",
            status_code=500,
        )


@app.get("/videos", response_class=HTMLResponse)
async def admin_videos(
    request: Request,
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
):
    items, total = db.get_videos(status=status, page=page, per_page=50)
    return _template_response(
        request,
        "videos.html",
        {
            "videos": items,
            "total": total,
            "page": page,
            "pages": math.ceil(total / 50) if total else 1,
            "status_filter": status,
        },
    )


@app.get("/channels", response_class=HTMLResponse)
async def admin_channels(
    request: Request,
    tier: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
):
    items, total = db.get_channels(tier=tier, page=page, per_page=50)
    return _template_response(
        request,
        "channels.html",
        {
            "channels": items,
            "total": total,
            "page": page,
            "pages": math.ceil(total / 50) if total else 1,
            "tier_filter": tier,
        },
    )


@app.get("/logs", response_class=HTMLResponse)
async def admin_logs(request: Request, page: int = Query(1, ge=1)):
    items, total = db.get_logs(page=page, per_page=100)
    return _template_response(
        request,
        "logs.html",
        {
            "logs": items,
            "total": total,
            "page": page,
            "pages": math.ceil(total / 100) if total else 1,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def admin_settings(request: Request):
    return _template_response(request, "settings.html", {"settings": config.all()})


@app.get("/whitelist", response_class=HTMLResponse)
async def admin_whitelist(request: Request, page: int = Query(1, ge=1)):
    items, total = db.get_whitelist(page=page, per_page=50)
    return _template_response(
        request,
        "whitelist.html",
        {
            "entries": items,
            "total": total,
            "page": page,
            "pages": math.ceil(total / 50) if total else 1,
        },
    )


@app.get("/profiles", response_class=HTMLResponse)
async def admin_profiles(request: Request):
    """Admin page for video/channel analysis profiles (optional feature)."""
    return _template_response(request, "profiles.html", {"active_page": "profiles"})


@app.get("/ml", response_class=HTMLResponse)
async def admin_ml(request: Request):
    """Admin page for ML model management (optional feature)."""
    return _template_response(request, "ml.html", {"active_page": "ml"})


@app.get("/community", response_class=HTMLResponse)
async def admin_community(request: Request):
    """Admin page for community keyword synchronisation."""
    return _template_response(request, "community.html", {"active_page": "community"})


@app.get("/processing", response_class=HTMLResponse)
async def admin_processing(request: Request):
    """Admin page showing the live analysis queue and in-progress jobs."""
    return _template_response(request, "processing.html", {"active_page": "processing"})


# ---------------------------------------------------------------------------
# Block / warning pages (served to end users via Squid redirect)
# ---------------------------------------------------------------------------


@app.get("/blocked", response_class=HTMLResponse)
async def block_page(
    request: Request,
    video_id: Optional[str] = Query(None),
    channel_id: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
    client_ip: Optional[str] = Query(None),
):
    """Block page shown when Squid redirects a blocked video request."""
    video = db.get_video(video_id) if video_id else None
    ctx: Dict[str, Any] = {
        "video_id": video_id,
        "channel_id": channel_id,
        "reason": reason or "brainrot_detected",
        "client_ip": client_ip,
    }
    if video:
        ctx.update({
            "video_title":    video.get("title") or "",
            "channel_name":   video.get("channel_name") or video.get("channel_id") or "",
            "thumbnail_url":  video.get("thumbnail_url") or "",
            "combined_score": video.get("combined_score") or 0,
            "keyword_score":  video.get("keyword_score") or 0,
            "scene_score":    video.get("scene_score") or 0,
            "audio_score":    video.get("audio_score") or 0,
            "matched_keywords": _extract_keywords(video.get("matched_keywords")),
        })
    return _template_response(request, "block_page.html", ctx)


@app.get("/warning", response_class=HTMLResponse)
async def warning_page(
    request: Request,
    video_id: Optional[str] = Query(None),
    channel_id: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
):
    """Warning page shown for soft-blocked videos."""
    video = db.get_video(video_id) if video_id else None
    ctx: Dict[str, Any] = {
        "video_id": video_id,
        "channel_id": channel_id,
        "reason": reason or "potential_brainrot",
        "bypass_url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
        "origin_url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
    }
    if video:
        ctx.update({
            "video_title":    video.get("title") or "",
            "channel_name":   video.get("channel_name") or video.get("channel_id") or "",
            "thumbnail_url":  video.get("thumbnail_url") or "",
            "combined_score": video.get("combined_score") or 0,
            "keyword_score":  video.get("keyword_score") or 0,
            "scene_score":    video.get("scene_score") or 0,
            "audio_score":    video.get("audio_score") or 0,
            "matched_keywords": _extract_keywords(video.get("matched_keywords")),
        })
    return _template_response(request, "warning_page.html", ctx)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> Dict[str, Any]:
    """Simple health check endpoint."""
    return {
        "status": "ok",
        "queue_size": analysis_queue.size(),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/system/status")
async def api_system_status() -> Dict[str, Any]:
    """Return the watchdog's latest status snapshot (read from disk)."""
    path = "/var/lib/brainrotfilter/system_status.json"
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"error": "watchdog_not_running", "services": []}
    except Exception as exc:
        return {"error": str(exc), "services": []}


@app.get("/status", response_class=HTMLResponse)
async def system_status_page(request: Request) -> HTMLResponse:
    """Render the system status admin page."""
    return _template_response(request, "system_status.html",
                              {"active_page": "status"})


@app.get("/version")
async def version_info() -> Dict[str, Any]:
    """Return version and build info."""
    try:
        from version import get_build_info
        return get_build_info()
    except ImportError:
        return {"version": "1.0.0", "release_date": "2026-04-12"}


@app.get("/api/gpu")
async def gpu_info() -> Dict[str, Any]:
    """Return GPU detection info."""
    try:
        from gpu_utils import detect_gpu
        return detect_gpu()
    except ImportError:
        return {"has_cuda": False, "reason": "gpu_utils not available"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )

    host = config.get_str("service_host") or "0.0.0.0"
    port = config.service_port

    logger.info("Starting BrainrotFilter analyzer service on %s:%d", host, port)

    uvicorn.run(
        "analyzer_service:app",
        host=host,
        port=port,
        log_level="info",
        access_log=True,
        reload=False,
        workers=1,  # Single process — we manage our own thread pool
    )


if __name__ == "__main__":
    main()
