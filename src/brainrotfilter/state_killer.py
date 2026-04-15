"""
state_killer.py - Linux connection tracking manipulation.

Kills active YouTube streams for a client by:
  1. Deleting conntrack entries (sends TCP RST to both sides)
  2. Adding a temporary iptables REJECT rule so the player cannot reconnect
     to the Squid proxy while the block is in effect

Without step 2 the YouTube player immediately reconnects to googlevideo.com
(via Squid) and continues buffering.  The temporary INPUT REJECT rule (on the
Squid intercept port) prevents any reconnection.  The rule is removed after
*BLOCK_DURATION_SECONDS* so general browsing is restored once the video buffer
drains and the player gives up.

Transparent-proxy conntrack entry format
-----------------------------------------
iptables REDIRECT (PREROUTING) rewrites dst to the Squid port, so conntrack
records both directions inline:

  tcp  ESTABLISHED
    src=CLIENT   dst=CDN_IP  sport=CLIENT_PORT  dport=443   <- original
    src=PROXY    dst=CLIENT  sport=3129         dport=CLIENT_PORT  [ASSURED]

We identify proxied YouTube connections by:
  dport ∈ {80, 443}  AND  reply sport ∈ {SQUID_HTTP_PORT, SQUID_HTTPS_PORT}

Requires CAP_NET_ADMIN (set via AmbientCapabilities in the service unit).
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Squid transparent-proxy intercept ports (must match squid.conf / linux_configurator)
SQUID_HTTP_PORT  = 3128
SQUID_HTTPS_PORT = 3129

# How long (seconds) the client is blocked from reconnecting after a kill
# Set high enough to last for a full-length video (2 hours = 7200s)
BLOCK_DURATION_SECONDS = 7200

# Active iptables block rules: {client_ip: monotonic expiry time}
_active_blocks: Dict[str, float] = {}
_blocks_lock = threading.Lock()


# ---------------------------------------------------------------------------
# conntrack helpers
# ---------------------------------------------------------------------------

def _run_conntrack(args: List[str], timeout: int = 10) -> Tuple[int, str, str]:
    """Run conntrack with *args*; return (returncode, stdout, stderr)."""
    cmd = ["conntrack"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, check=False)
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        logger.error("conntrack not found — install conntrack-tools")
        return -1, "", "conntrack not found"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as exc:
        return -1, "", str(exc)


_NAT_LINE = re.compile(
    r"src=([\d.]+)\s+dst=([\d.]+)\s+sport=(\d+)\s+dport=(\d+)"   # original dir
    r".*?"
    r"src=[\d.]+\s+dst=[\d.]+\s+sport=(\d+)\s+dport=\d+"          # reply dir
)


def _is_private(ip: str) -> bool:
    return (ip.startswith("10.") or ip.startswith("192.168.")
            or ip.startswith("172.16.") or ip.startswith("172.17.")
            or ip.startswith("172.31.") or ip.startswith("127."))


def _list_proxied_connections(
    client_ip: str,
) -> List[Tuple[str, str, int, int]]:
    """
    Return ESTABLISHED proxy-tunnelled connections from *client_ip*.

    Each entry is (client_ip, cdn_ip, client_port, cdn_port).
    """
    rc, stdout, stderr = _run_conntrack(["-L", "-s", client_ip, "-p", "tcp"])
    if rc != 0:
        logger.warning("conntrack -L: rc=%d %s", rc, stderr[:200])
        return []

    results = []
    for line in stdout.splitlines():
        if "ESTABLISHED" not in line:
            continue
        m = _NAT_LINE.search(line)
        if not m:
            continue
        src_ip, dst_ip = m.group(1), m.group(2)
        sport, dport, reply_sport = int(m.group(3)), int(m.group(4)), int(m.group(5))

        if src_ip != client_ip:
            continue
        if dport not in (80, 443):
            continue
        if reply_sport not in (SQUID_HTTP_PORT, SQUID_HTTPS_PORT):
            continue
        if _is_private(dst_ip):
            continue

        results.append((client_ip, dst_ip, sport, dport))

    return results


# ---------------------------------------------------------------------------
# iptables helpers
# ---------------------------------------------------------------------------

def _ipt(*args: str, check: bool = False) -> int:
    """Run iptables with *args*; return exit code."""
    try:
        r = subprocess.run(["iptables"] + list(args), capture_output=True,
                           text=True, timeout=5, check=False)
        if r.returncode != 0 and check:
            logger.warning("iptables %s: %s", " ".join(args), r.stderr.strip())
        return r.returncode
    except Exception as exc:
        logger.warning("iptables error: %s", exc)
        return -1


def _add_client_block(client_ip: str, duration: int = BLOCK_DURATION_SECONDS) -> None:
    """
    Insert an iptables INPUT REJECT rule so *client_ip* cannot reconnect to
    the Squid proxy ports.  The rule is recorded with an expiry for cleanup.

    Skips adding rules if an active (unexpired) block already exists for this
    client, and also uses `-C` to avoid duplicate kernel rules from concurrent
    calls.  The expiry is always refreshed so repeated blocks extend the window.
    """
    now = time.monotonic()
    with _blocks_lock:
        existing_expiry = _active_blocks.get(client_ip)
    already_blocked = existing_expiry is not None and now < existing_expiry

    if not already_blocked:
        comment = f"brainrotfilter-block-{client_ip}"
        for port in (SQUID_HTTP_PORT, SQUID_HTTPS_PORT):
            check_args = ["-C", "INPUT",
                          "-s", client_ip,
                          "-p", "tcp", "--dport", str(port),
                          "-m", "comment", "--comment", comment,
                          "-j", "REJECT", "--reject-with", "tcp-reset"]
            # -C returns 0 if rule exists; skip add if present
            if _ipt(*check_args) != 0:
                _ipt("-I", "INPUT",
                     "-s", client_ip,
                     "-p", "tcp", "--dport", str(port),
                     "-m", "comment", "--comment", comment,
                     "-j", "REJECT", "--reject-with", "tcp-reset")

    expiry = now + duration
    with _blocks_lock:
        _active_blocks[client_ip] = expiry
    logger.info(
        "iptables block for %s (refreshed, expires in %ds)", client_ip, duration
    )


def _remove_client_block(client_ip: str) -> None:
    """Remove iptables REJECT rules for *client_ip* and clear expiry record."""
    comment = f"brainrotfilter-block-{client_ip}"
    for port in (SQUID_HTTP_PORT, SQUID_HTTPS_PORT):
        # Delete all matching rules (may have duplicates if called twice)
        while _ipt("-D", "INPUT",
                   "-s", client_ip,
                   "-p", "tcp", "--dport", str(port),
                   "-m", "comment", "--comment", comment,
                   "-j", "REJECT", "--reject-with", "tcp-reset") == 0:
            pass  # keep deleting until the rule is gone

    with _blocks_lock:
        _active_blocks.pop(client_ip, None)
    logger.info("Removed iptables block for %s", client_ip)


def cleanup_expired_blocks() -> None:
    """
    Remove iptables rules whose duration has elapsed.
    Call this periodically from a background thread.
    """
    now = time.monotonic()
    with _blocks_lock:
        expired = [ip for ip, exp in _active_blocks.items() if now >= exp]
    for ip in expired:
        logger.info("Block expired for %s — removing iptables rule", ip)
        _remove_client_block(ip)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def kill_states_for_video(
    client_ip: str,
    video_id: Optional[str] = None,
) -> Tuple[bool, int]:
    """
    Stop a client from streaming a blocked video:

    1. Find all ESTABLISHED proxied connections (conntrack).
    2. Delete each entry → kernel sends TCP RST to both ends.
    3. Add a temporary iptables REJECT rule so the player cannot immediately
       reconnect and refill the buffer.

    The iptables rule expires after BLOCK_DURATION_SECONDS (default 7200 s),
    preventing the player from refilling the buffer for a full video session.

    Returns (success, connections_killed_count).
    """
    if not client_ip or client_ip in ("-", "unknown", "localhost"):
        logger.warning("kill_states_for_video: invalid client_ip=%r", client_ip)
        return False, 0

    conns = _list_proxied_connections(client_ip)

    killed = 0
    for src_ip, dst_ip, sport, dport in conns:
        logger.info(
            "RST: %s:%d -> %s:%d [video=%s]",
            src_ip, sport, dst_ip, dport, video_id or "N/A",
        )
        rc, _, stderr = _run_conntrack([
            "-D", "-s", src_ip, "-d", dst_ip,
            "-p", "tcp", "--sport", str(sport), "--dport", str(dport),
        ])
        if rc == 0:
            killed += 1
        else:
            logger.warning("conntrack -D failed %s:%d: %s", src_ip, sport, stderr[:200])

    # Block the client from reconnecting so the buffer cannot refill
    _add_client_block(client_ip)

    logger.info(
        "State kill complete: %d/%d RST, proxy blocked for %ds. client=%s video=%s",
        killed, len(conns), BLOCK_DURATION_SECONDS, client_ip, video_id or "N/A",
    )
    return True, killed


def kill_states_for_channel(client_ip: str, channel_id: str) -> Tuple[bool, int]:
    """Kill connections when an entire channel is blocked."""
    logger.info("Channel block kill: client=%s channel=%s", client_ip, channel_id)
    return kill_states_for_video(client_ip, video_id=f"channel:{channel_id}")


def is_client_blocked(client_ip: str) -> bool:
    """Return True if *client_ip* currently has an active iptables block."""
    now = time.monotonic()
    with _blocks_lock:
        expiry = _active_blocks.get(client_ip)
    return expiry is not None and now < expiry


def is_conntrack_available() -> bool:
    """Return True if conntrack is usable on this system."""
    rc, _, _ = _run_conntrack(["-C"], timeout=3)
    return rc == 0
