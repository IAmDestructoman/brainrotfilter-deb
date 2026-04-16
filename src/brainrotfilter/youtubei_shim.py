"""
youtubei_shim.py - Body-inspecting reverse proxy for YouTube's
/youtubei/v1/player and /youtubei/v1/next POST endpoints.

Squid's url_rewrite_program sends matching URLs to these shim endpoints
instead of the real YouTube upstream. The shim reads the POST body to
extract videoId and distinguish 'click' (actual playback intent) from
'hover' (home-feed preview metadata), then:
  - On click: consult /api/check + queue for analysis if unknown,
    engage per-client CDN block if already known-blocked
  - On hover: noop
  - Forward the (unmodified) request to www.youtube.com and stream the
    response back so the browser sees a normal YouTube answer

Requires: httpx[http2]
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Request, Response

logger = logging.getLogger(__name__)
router = APIRouter()

# Single shared async client; reuses connections + HTTP/2 streams.
_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
    return _client


# Hop-by-hop headers that must NOT be forwarded.
_HOP_BY_HOP = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding",
    "upgrade", "content-length",
}


def _parse_body(body: bytes) -> Tuple[Optional[str], str]:
    """Return (video_id, intent) where intent is 'click' or 'hover'."""
    video_id: Optional[str] = None
    intent = "click"

    try:
        data: Dict[str, Any] = json.loads(body)
    except Exception:
        return None, "click"  # fail open — still queue

    # Direct videoId field (most common on /player)
    vid = data.get("videoId")
    if isinstance(vid, str) and len(vid) == 11:
        video_id = vid

    # Fallback: derive from playbackContext.currentUrl
    if not video_id:
        pc = data.get("playbackContext", {}) or {}
        cpc = pc.get("contentPlaybackContext", {}) or {}
        url = cpc.get("currentUrl", "") or ""
        if "v=" in url:
            tail = url.split("v=", 1)[1]
            cand = tail.split("&", 1)[0].split("#", 1)[0]
            if len(cand) == 11:
                video_id = cand

    # Client-name heuristic: previews use WEB_EMBEDDED_PLAYER / similar
    client = (data.get("context") or {}).get("client") or {}
    client_name = (client.get("clientName") or "").upper()
    if "EMBEDDED" in client_name or "PREVIEW" in client_name:
        intent = "hover"

    return video_id, intent


def _register_decision(video_id: str, client_ip: str, intent: str) -> None:
    """Plug into the existing block/queue pipeline.

    Imported lazily to avoid circular import with analyzer_service.
    """
    if not video_id or intent != "click":
        return
    try:
        from analyzer_service import (  # type: ignore[import-not-found]
            db,
            analysis_queue,
            _block_client_cdn,
            _mark_client_pending,
            _record_identify,
        )
        _record_identify(client_ip)
        status = db.get_video_status(video_id)
        if status == "block":
            if client_ip:
                _block_client_cdn(client_ip, video_id=video_id)
        elif status in (None, "", "pending"):
            queued = analysis_queue.enqueue(video_id, priority=True)
            if queued and client_ip:
                _mark_client_pending(client_ip, video_id)
            logger.info("shim queued %s (client=%s, via %s)",
                        video_id, client_ip, "player/next")
    except Exception as exc:
        logger.warning("shim decision pipeline failed: %s", exc)


async def _forward(request: Request, body: bytes, upstream_path: str) -> Response:
    """Forward the request to www.youtube.com and stream back."""
    client = await _get_client()
    upstream_url = f"https://www.youtube.com{upstream_path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    fwd_headers["host"] = "www.youtube.com"

    try:
        upstream = await client.request(
            method=request.method,
            url=upstream_url,
            content=body,
            headers=fwd_headers,
        )
    except httpx.TimeoutException:
        logger.warning("shim upstream timeout: %s", upstream_url)
        return Response("Upstream timeout", status_code=504)
    except httpx.HTTPError as exc:
        logger.warning("shim upstream error %s: %s", upstream_url, exc)
        return Response("Upstream error", status_code=502)

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


async def _handle(request: Request, upstream_path: str) -> Response:
    body = await request.body()
    video_id, intent = _parse_body(body)

    # Prefer X-Forwarded-For (Squid will set this if configured), else peer
    client_ip = ""
    if xff := request.headers.get("x-forwarded-for"):
        client_ip = xff.split(",")[0].strip()
    elif request.client:
        client_ip = request.client.host or ""

    logger.info(
        "shim %s: video_id=%s intent=%s client=%s",
        upstream_path, video_id or "?", intent, client_ip or "?",
    )
    _register_decision(video_id or "", client_ip, intent)

    return await _forward(request, body, upstream_path)


@router.post("/shim/youtubei/v1/player")
async def shim_player(request: Request) -> Response:
    return await _handle(request, "/youtubei/v1/player")


@router.post("/shim/youtubei/v1/next")
async def shim_next(request: Request) -> Response:
    return await _handle(request, "/youtubei/v1/next")
