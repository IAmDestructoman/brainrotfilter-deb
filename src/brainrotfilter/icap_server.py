#!/usr/bin/env python3
"""
icap_server.py - Minimal ICAP REQMOD server for BrainrotFilter.

Runs as a standalone daemon on 127.0.0.1:1344. Squid sends REQMOD requests
for matching URLs (configured via icap_service + adaptation_access) and
this server inspects the encapsulated HTTP request. For YouTube's
/youtubei/v1/(player|next) POSTs, it reads the JSON body, extracts
videoId, and heuristically classifies click vs hover using fields like
context.client.clientName. In log-only mode (default) the server logs
a structured snapshot and returns 204 No Content so Squid forwards the
original request unchanged. In enforcement mode it also calls the
brainrotfilter /api/check and /api/analyze endpoints.

Chose a self-contained Python implementation over c-icap + python bindings
because the protocol surface we need is small (OPTIONS + REQMOD) and
avoiding the c-icap dependency keeps packaging + debugging straightforward.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import sys
import threading
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("brainrot-icap")

BRAINROT_API = os.environ.get("BRAINROT_API", "http://127.0.0.1:8199")
ENFORCE = os.environ.get("BRAINROT_SHIM_ENFORCE", "0") == "1"
LISTEN_HOST = os.environ.get("BRAINROT_ICAP_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("BRAINROT_ICAP_PORT", "1344"))

ICAP_VERSION = "ICAP/1.0"


# ---------------------------------------------------------------------------
# Low-level ICAP protocol helpers
# ---------------------------------------------------------------------------

def _read_crlf_line(rfile) -> str:
    line = rfile.readline()
    if not line:
        return ""
    return line.rstrip(b"\r\n").decode("latin-1", errors="replace")


def _read_headers(rfile) -> Dict[str, str]:
    """Read headers until an empty line.  Keys lowercased."""
    headers: Dict[str, str] = {}
    while True:
        line = _read_crlf_line(rfile)
        if line == "":
            break
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return headers


def _parse_encapsulated(header_value: str) -> Dict[str, int]:
    """e.g. 'req-hdr=0, req-body=137' -> {'req-hdr': 0, 'req-body': 137}"""
    out: Dict[str, int] = {}
    for item in header_value.split(","):
        k, _, v = item.strip().partition("=")
        if k and v:
            try:
                out[k] = int(v)
            except ValueError:
                pass
    return out


def _read_chunked_icap_body(rfile) -> bytes:
    """Read an ICAP chunked body up to and including the 0-CRLF terminator."""
    buf = bytearray()
    while True:
        size_line = _read_crlf_line(rfile)
        if size_line == "":
            break
        # Chunk size may be followed by extensions (';ieof' for last chunk).
        size_hex = size_line.split(";", 1)[0].strip()
        try:
            size = int(size_hex, 16)
        except ValueError:
            break
        if size == 0:
            # Trailing CRLF after the zero-size chunk
            _read_crlf_line(rfile)
            break
        data = rfile.read(size)
        buf.extend(data)
        _read_crlf_line(rfile)  # CRLF following the chunk data
    return bytes(buf)


# ---------------------------------------------------------------------------
# Inspection + decision
# ---------------------------------------------------------------------------

def _parse_json_body(body: bytes) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _extract_snapshot(url: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Pull out the fields most useful for click/hover heuristic tuning."""
    ctx = data.get("context") or {}
    client = ctx.get("client") or {}
    pc = data.get("playbackContext") or {}
    cpc = pc.get("contentPlaybackContext") or {}

    return {
        "url": url,
        "videoId": data.get("videoId"),
        "clientName": client.get("clientName"),
        "clientVersion": client.get("clientVersion"),
        "visitorData_len": len(ctx.get("clickTracking", {}).get("clickTrackingParams", "") or ""),
        "contentCheckOk": data.get("contentCheckOk"),
        "racyCheckOk": data.get("racyCheckOk"),
        "has_playbackContext": bool(pc),
        "autoplay": cpc.get("autoplay"),
        "autonavState": cpc.get("autonavState"),
        "currentUrl": cpc.get("currentUrl"),
        "referer": cpc.get("referer"),
        "top_keys": list(data.keys())[:24],
    }


def _guess_intent(snap: Dict[str, Any]) -> str:
    client_name = (snap.get("clientName") or "").upper()
    if "EMBEDDED" in client_name or "PREVIEW" in client_name:
        return "hover"
    # autonavState=STATE_ON indicates autoplay navigation — still a play
    if snap.get("autonavState") == "STATE_ON":
        return "autoplay"
    return "click"


def _post_api(path: str, payload: Dict[str, Any], timeout: float = 2.0) -> None:
    """Best-effort fire-and-forget POST to the brainrotfilter API."""
    url = f"{BRAINROT_API}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=timeout).read(1)
    except Exception as exc:
        logger.debug("api call %s failed: %s", path, exc)


def _register_decision(video_id: str, client_ip: str, intent: str) -> None:
    """Enforcement-mode path: check + optionally queue via the main service."""
    if not video_id or intent == "hover":
        return
    # /api/check fires _record_identify and (if blocked) engages the
    # per-client CDN deny.
    _post_api("/api/check", {"video_id": video_id, "client_ip": client_ip})
    # /api/analyze dedupes internally so a duplicate queue call is cheap.
    _post_api("/api/analyze", {"video_id": video_id, "client_ip": client_ip})


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class ICAPHandler(socketserver.StreamRequestHandler):
    timeout = 30

    def handle(self) -> None:
        try:
            self._handle_once()
        except Exception as exc:
            logger.warning("ICAP handler error: %s", exc, exc_info=False)

    def _handle_once(self) -> None:
        req_line = _read_crlf_line(self.rfile)
        if not req_line:
            return
        parts = req_line.split(" ", 2)
        if len(parts) < 3:
            self._send(400, "Bad Request")
            return
        method, uri, _version = parts
        headers = _read_headers(self.rfile)

        if method == "OPTIONS":
            self._handle_options()
        elif method == "REQMOD":
            self._handle_reqmod(uri, headers)
        else:
            self._send(405, "Method Not Allowed")

    # ---- OPTIONS --------------------------------------------------------
    def _handle_options(self) -> None:
        # Do NOT advertise Preview — keeps handling simple and Squid will
        # send the whole body in a single REQMOD.
        lines = [
            f"{ICAP_VERSION} 200 OK",
            "Methods: REQMOD",
            "Service: BrainrotFilter ICAP 1.0",
            "ISTag: \"brainrot-icap-1\"",
            "Max-Connections: 100",
            "Options-TTL: 3600",
            "Allow: 204",
            "Encapsulated: null-body=0",
            "",
            "",
        ]
        self.wfile.write("\r\n".join(lines).encode("latin-1"))

    # ---- REQMOD ---------------------------------------------------------
    def _handle_reqmod(self, uri: str, headers: Dict[str, str]) -> None:
        enc = _parse_encapsulated(headers.get("encapsulated", ""))
        body_key = "req-body" if "req-body" in enc else "null-body"
        hdr_end = enc.get(body_key, 0)

        # Read the encapsulated HTTP request headers block (bytes up to body)
        http_headers = self.rfile.read(hdr_end) if hdr_end > 0 else b""
        http_url, http_method = _parse_http_request_line(http_headers)

        # Only inspect the two URLs we care about
        is_target = (
            "/youtubei/v1/player" in http_url
            or "/youtubei/v1/next" in http_url
        )

        # Drain the body either way so the connection is left clean
        body = b""
        if body_key == "req-body":
            body = _read_chunked_icap_body(self.rfile)

        if is_target and body:
            client_ip = _extract_client_ip(http_headers, headers)
            data = _parse_json_body(body)
            if data is not None:
                snap = _extract_snapshot(http_url, data)
                intent = _guess_intent(snap)
                mode = "enforce" if ENFORCE else "log-only"
                logger.info(
                    "icap-snapshot [%s] intent=%s client=%s %s",
                    mode, intent, client_ip or "?",
                    json.dumps(snap, default=str),
                )
                if ENFORCE:
                    vid = snap.get("videoId")
                    if isinstance(vid, str) and len(vid) == 11:
                        _register_decision(vid, client_ip or "", intent)

        # Always return 204 — we never modify the request, just observe.
        self._send_204()

    # ---- response helpers ----------------------------------------------
    def _send_204(self) -> None:
        self.wfile.write(
            f"{ICAP_VERSION} 204 No Content\r\nEncapsulated: null-body=0\r\n\r\n"
            .encode("latin-1")
        )

    def _send(self, code: int, reason: str) -> None:
        self.wfile.write(
            f"{ICAP_VERSION} {code} {reason}\r\n\r\n".encode("latin-1")
        )


def _parse_http_request_line(http_headers: bytes) -> Tuple[str, str]:
    """Return (url, method) extracted from the first line of a raw HTTP
    request. The URL may be absolute (GET https://…) or path-relative."""
    text = http_headers.decode("latin-1", errors="replace")
    end = text.find("\r\n")
    first = text[:end] if end >= 0 else text
    parts = first.split(" ")
    method = parts[0] if len(parts) > 0 else ""
    url = parts[1] if len(parts) > 1 else ""
    return url, method


def _extract_client_ip(http_headers_raw: bytes, icap_headers: Dict[str, str]) -> str:
    """Squid optionally sets X-Client-IP in the ICAP request headers
    (icap_client_addr_header) — prefer that. Else try X-Forwarded-For
    from the inner HTTP request."""
    if xci := icap_headers.get("x-client-ip"):
        return xci.strip()
    # Fall through: parse inner HTTP headers for X-Forwarded-For
    text = http_headers_raw.decode("latin-1", errors="replace")
    for line in text.split("\r\n"):
        if line.lower().startswith("x-forwarded-for:"):
            return line.partition(":")[2].strip().split(",", 1)[0].strip()
    return ""


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

class _ThreadedICAPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logger.info(
        "BrainrotFilter ICAP starting on %s:%d (api=%s, enforce=%s)",
        LISTEN_HOST, LISTEN_PORT, BRAINROT_API, ENFORCE,
    )
    with _ThreadedICAPServer((LISTEN_HOST, LISTEN_PORT), ICAPHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
