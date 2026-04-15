"""
state_killer.py - Linux connection tracking manipulation.

Kills active TCP connections between a client and the Squid proxy when a
video has been flagged mid-stream.  In a transparent proxy setup, the client
never connects directly to YouTube CDN — Squid proxies all traffic.  The
correct kill target is therefore the client → proxy tunnel, NOT the proxy →
CDN connection.

Strategy
--------
1. Find ESTABLISHED TCP conntrack entries where:
   - source IP  == client IP (the LAN device streaming YouTube)
   - destination port ∈ {SQUID_HTTP_PORT, SQUID_HTTPS_PORT}
2. Delete those entries via ``conntrack -D``.
3. This sends a TCP RST to both sides, forcing the browser/app to reconnect.
4. On reconnect, the blocked-video URL hits the Squid external ACL again and
   is denied — stream terminated.

Safety constraints:
  - Only kills TCP connections on the known Squid intercept ports
  - Never touches SSH (22), DNS (53), or unrelated LAN traffic
  - Logs every kill action for audit purposes
  - Requires CAP_NET_ADMIN (granted via AmbientCapabilities in the service unit)
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Squid intercept ports — match linux_configurator.py defaults
SQUID_HTTP_PORT = 3128
SQUID_HTTPS_PORT = 3129


def _run_conntrack(args: List[str], timeout: int = 10) -> Tuple[int, str, str]:
    """Run conntrack with *args* and return (returncode, stdout, stderr)."""
    cmd = ["conntrack"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        logger.error("conntrack not found; install conntrack-tools package")
        return -1, "", "conntrack not found"
    except subprocess.TimeoutExpired:
        logger.error("conntrack timed out.")
        return -1, "", "timeout"
    except Exception as exc:
        logger.error("conntrack error: %s", exc)
        return -1, "", str(exc)


def _list_proxy_connections(client_ip: str) -> List[Tuple[str, int]]:
    """
    List ESTABLISHED TCP connections from *client_ip* to the Squid proxy ports.

    In conntrack, a transparent-proxy entry looks like:
        tcp 6 431999 ESTABLISHED src=CLIENT dst=PROXY sport=CLIENT_PORT dport=3129 ...

    Returns list of (client_ip, client_port) tuples representing active
    Squid tunnels.
    """
    rc, stdout, stderr = _run_conntrack(["-L", "-s", client_ip, "-p", "tcp"])
    if rc != 0:
        logger.warning("conntrack -L returned %d: %s", rc, stderr[:200])
        return []

    tunnels: List[Tuple[str, int]] = []
    # Pattern: src=CLIENT dst=PROXY sport=CLIENT_PORT dport=SQUID_PORT
    pat = re.compile(
        r"src=([\d.]+)\s+dst=[\d.]+\s+sport=(\d+)\s+dport=(\d+)"
    )
    for line in stdout.splitlines():
        if "ESTABLISHED" not in line and "SYN_SENT" not in line:
            continue
        m = pat.search(line)
        if not m:
            continue
        src_ip, sport, dport = m.group(1), int(m.group(2)), int(m.group(3))
        if src_ip == client_ip and dport in (SQUID_HTTP_PORT, SQUID_HTTPS_PORT):
            tunnels.append((client_ip, sport))

    return tunnels


def kill_states_for_video(
    client_ip: str,
    video_id: Optional[str] = None,
) -> Tuple[bool, int]:
    """
    Terminate active Squid proxy tunnels for *client_ip*.

    Finds all ESTABLISHED TCP connections from *client_ip* to the Squid
    intercept ports and deletes them from conntrack, triggering a TCP RST.
    The browser/app must reconnect, at which point the blocked URL is caught
    by the Squid external ACL.

    Args:
        client_ip:  LAN IP of the client currently streaming
        video_id:   Optional video ID (audit log only)

    Returns:
        (success, connections_killed_count)
    """
    if not client_ip or client_ip in ("-", "unknown"):
        logger.warning("kill_states_for_video called with invalid client_ip=%r", client_ip)
        return False, 0

    tunnels = _list_proxy_connections(client_ip)

    if not tunnels:
        logger.info(
            "No active Squid tunnels found for client %s (video=%s).",
            client_ip, video_id or "N/A",
        )
        return True, 0

    killed = 0
    for src_ip, sport in tunnels:
        logger.info(
            "Killing proxy tunnel: %s:%d -> Squid (client=%s, video=%s)",
            src_ip, sport, client_ip, video_id or "N/A",
        )
        rc, _, stderr = _run_conntrack([
            "-D", "-s", src_ip, "-p", "tcp", "--sport", str(sport),
        ])
        if rc == 0:
            killed += 1
        else:
            logger.warning(
                "conntrack -D failed for %s:%d: %s", src_ip, sport, stderr[:200]
            )

    logger.info(
        "State kill complete: %d/%d tunnels killed for client %s (video=%s).",
        killed, len(tunnels), client_ip, video_id or "N/A",
    )
    return killed > 0 or len(tunnels) == 0, killed


def kill_states_for_channel(
    client_ip: str,
    channel_id: str,
) -> Tuple[bool, int]:
    """Kill connections when an entire channel is blocked."""
    logger.info(
        "Channel block connection kill: client=%s channel=%s",
        client_ip, channel_id,
    )
    return kill_states_for_video(client_ip, video_id=f"channel:{channel_id}")


def is_conntrack_available() -> bool:
    """Return True if conntrack is usable on this system."""
    rc, _, _ = _run_conntrack(["-C"], timeout=3)
    return rc == 0
