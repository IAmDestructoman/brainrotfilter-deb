"""
state_killer.py - Linux connection tracking manipulation.

Kills active TCP/UDP connections for a client IP that is currently streaming
a YouTube/Google Video CDN connection, forcing the video to stop immediately
after a block decision has been made mid-stream.

Uses conntrack (netfilter connection tracking) instead of pfctl.

Safety constraints:
  - Only kills connections where the DESTINATION is a known YouTube/Google CDN IP range
  - Never kills LAN-to-LAN connections
  - Logs every kill action for audit purposes
  - Requires root (or CAP_NET_ADMIN) to run conntrack
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Known YouTube/Google CDN destination patterns (CIDR prefixes)
_GOOGLE_CDN_PREFIXES = [
    "142.250.",
    "172.217.",
    "216.58.",
    "74.125.",
    "64.233.",
    "209.85.",
    "173.194.",
    "108.177.",
    "35.190.",
    "34.64.",
    "34.96.",
    "34.104.",
    "34.128.",
    "35.186.",
    "35.191.",
    "35.234.",
    "35.235.",
    "23.236.",
    "23.251.",
    "130.211.",
]


def _is_youtube_cdn_ip(ip: str) -> bool:
    """Return True if *ip* belongs to a known YouTube/Google CDN range."""
    for prefix in _GOOGLE_CDN_PREFIXES:
        if ip.startswith(prefix):
            return True
    return False


def _is_private_ip(ip: str) -> bool:
    """Return True if *ip* is in a private (RFC 1918) range."""
    return (
        ip.startswith("10.")
        or ip.startswith("192.168.")
        or ip.startswith("172.16.")
        or ip.startswith("172.17.")
        or ip.startswith("172.31.")
        or ip.startswith("127.")
    )


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


def _list_connections(client_ip: str) -> List[Tuple[str, str]]:
    """
    List all connections from *client_ip* to YouTube/Google CDN IPs.

    Returns list of (src_ip, dst_ip) pairs.
    """
    rc, stdout, stderr = _run_conntrack(["-L", "-s", client_ip, "-p", "tcp"])
    if rc != 0:
        logger.warning("conntrack -L returned %d: %s", rc, stderr[:200])
        return []

    pairs: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    # Parse conntrack output lines:
    # tcp  6 300 ESTABLISHED src=192.168.1.5 dst=142.250.80.46 sport=54321 dport=443 ...
    dst_pattern = re.compile(r"dst=([\d.]+)")

    for line in stdout.splitlines():
        if not line.strip():
            continue
        m = dst_pattern.search(line)
        if not m:
            continue
        dst_ip = m.group(1)

        if not _is_youtube_cdn_ip(dst_ip):
            continue
        if _is_private_ip(dst_ip):
            continue

        pair = (client_ip, dst_ip)
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)

    return pairs


def kill_states_for_video(
    client_ip: str,
    video_id: Optional[str] = None,
) -> Tuple[bool, int]:
    """
    Kill Linux conntrack entries for *client_ip* to YouTube CDN connections.

    This is called after a video has been flagged mid-stream to immediately
    terminate the ongoing video stream.

    Args:
        client_ip:  The LAN IP address of the client currently streaming
        video_id:   Optional video ID (for audit log only)

    Returns:
        (success, connections_killed_count)
    """
    if not client_ip:
        logger.warning("kill_states_for_video called with empty client_ip")
        return False, 0

    # Find YouTube connections for this client
    youtube_pairs = _list_connections(client_ip)

    if not youtube_pairs:
        logger.info(
            "No YouTube connections found for client %s (video_id=%s).",
            client_ip,
            video_id or "N/A",
        )
        return True, 0

    # Kill each connection
    killed = 0
    for src_ip, dst_ip in youtube_pairs:
        logger.info(
            "Killing connection: %s -> %s (client=%s, video=%s)",
            src_ip, dst_ip, client_ip, video_id or "N/A",
        )
        rc, stdout, stderr = _run_conntrack([
            "-D", "-s", src_ip, "-d", dst_ip, "-p", "tcp",
        ])
        if rc == 0:
            killed += 1
        else:
            logger.warning(
                "conntrack -D returned %d for %s -> %s: %s",
                rc, src_ip, dst_ip, stderr[:200],
            )

    logger.info(
        "Connection kill complete: %d/%d pairs killed for client %s.",
        killed, len(youtube_pairs), client_ip,
    )
    return killed > 0 or len(youtube_pairs) == 0, killed


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
    """Return True if conntrack is available on this system."""
    rc, _, _ = _run_conntrack(["-C"], timeout=3)
    return rc == 0
