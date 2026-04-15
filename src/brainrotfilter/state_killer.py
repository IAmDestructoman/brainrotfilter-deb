"""
state_killer.py - Linux connection tracking manipulation.

Kills active TCP connections for a client that is streaming YouTube video
through a transparent Squid proxy, forcing the stream to stop immediately
after a block decision is made mid-stream.

How conntrack looks in a transparent-proxy setup
-------------------------------------------------
iptables REDIRECT rewrites the destination of outgoing HTTPS packets.
conntrack records BOTH directions (original + NAT reply):

  tcp  ESTABLISHED
    src=CLIENT   dst=CDN_IP   sport=CLIENT_PORT  dport=443   <- original
    src=PROXY    dst=CLIENT   sport=3129         dport=CLIENT_PORT  [ASSURED]

The trick: ``dst`` in the first direction is the ORIGINAL CDN IP (not the
proxy IP), and ``sport`` in the reply direction is the Squid intercept port
(3129 for HTTPS, 3128 for HTTP).  We detect Squid-proxied connections by
checking that the reply ``sport`` is a known Squid port, then kill by
matching the original src/sport/dport to avoid hitting SSH or other traffic.

Safety constraints:
  - Only kills connections where dport ∈ {80, 443} AND reply sport ∈ Squid ports
  - Never touches SSH (22), DNS (53), or LAN-only traffic
  - Logs every kill action for audit purposes
  - Requires CAP_NET_ADMIN (granted via AmbientCapabilities in the service unit)
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Squid transparent-proxy intercept ports (must match squid.conf)
SQUID_HTTP_PORT  = 3128
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


# Matches the inline NAT conntrack line:
#   src=CLIENT dst=CDN sport=CPORT dport=443 src=PROXY dst=CLIENT sport=3129 dport=CPORT
_NAT_LINE = re.compile(
    r"src=([\d.]+)\s+dst=([\d.]+)\s+sport=(\d+)\s+dport=(\d+)"   # original
    r".*?"
    r"src=[\d.]+\s+dst=[\d.]+\s+sport=(\d+)\s+dport=\d+"          # reply
)


def _list_proxied_connections(
    client_ip: str,
) -> List[Tuple[str, str, int, int]]:
    """
    Find ESTABLISHED TCP connections from *client_ip* that are being
    transparently proxied through Squid.

    Detection: dport ∈ {80, 443} in the original direction AND reply
    sport ∈ {SQUID_HTTP_PORT, SQUID_HTTPS_PORT}.

    Returns list of (client_ip, cdn_ip, client_port, cdn_port) tuples.
    """
    rc, stdout, stderr = _run_conntrack(["-L", "-s", client_ip, "-p", "tcp"])
    if rc != 0:
        logger.warning("conntrack -L returned %d: %s", rc, stderr[:200])
        return []

    results = []
    for line in stdout.splitlines():
        if "ESTABLISHED" not in line:
            continue
        m = _NAT_LINE.search(line)
        if not m:
            continue
        src_ip  = m.group(1)
        dst_ip  = m.group(2)
        sport   = int(m.group(3))
        dport   = int(m.group(4))
        reply_sport = int(m.group(5))

        if src_ip != client_ip:
            continue
        if dport not in (80, 443):
            continue
        if reply_sport not in (SQUID_HTTP_PORT, SQUID_HTTPS_PORT):
            continue
        if _is_private_ip(dst_ip):
            continue

        results.append((client_ip, dst_ip, sport, dport))

    return results


def kill_states_for_video(
    client_ip: str,
    video_id: Optional[str] = None,
) -> Tuple[bool, int]:
    """
    Kill active Squid-proxied connections for *client_ip*.

    Finds ESTABLISHED TCP connections from the client through the Squid
    transparent proxy and deletes their conntrack entries, triggering a
    TCP RST.  The browser/app must reconnect; on reconnect the blocked
    video URL is caught by the Squid external ACL and denied.

    Args:
        client_ip:  LAN IP of the client currently streaming
        video_id:   Optional video ID (audit log only)

    Returns:
        (success, connections_killed_count)
    """
    if not client_ip or client_ip in ("-", "unknown", "localhost"):
        logger.warning("kill_states_for_video: invalid client_ip=%r", client_ip)
        return False, 0

    conns = _list_proxied_connections(client_ip)

    if not conns:
        logger.info(
            "No proxied connections found for client %s (video=%s).",
            client_ip, video_id or "N/A",
        )
        return True, 0

    killed = 0
    for src_ip, dst_ip, sport, dport in conns:
        logger.info(
            "Killing: %s:%d -> %s:%d (via Squid) [video=%s]",
            src_ip, sport, dst_ip, dport, video_id or "N/A",
        )
        rc, _, stderr = _run_conntrack([
            "-D",
            "-s", src_ip,
            "-d", dst_ip,
            "-p", "tcp",
            "--sport", str(sport),
            "--dport", str(dport),
        ])
        if rc == 0:
            killed += 1
        else:
            logger.warning(
                "conntrack -D failed for %s:%d->%s:%d: %s",
                src_ip, sport, dst_ip, dport, stderr[:200],
            )

    logger.info(
        "State kill done: %d/%d killed for client=%s video=%s.",
        killed, len(conns), client_ip, video_id or "N/A",
    )
    return killed > 0 or len(conns) == 0, killed


def kill_states_for_channel(
    client_ip: str,
    channel_id: str,
) -> Tuple[bool, int]:
    """Kill connections when an entire channel is blocked."""
    logger.info(
        "Channel block kill: client=%s channel=%s", client_ip, channel_id
    )
    return kill_states_for_video(client_ip, video_id=f"channel:{channel_id}")


def is_conntrack_available() -> bool:
    """Return True if conntrack is usable on this system."""
    rc, _, _ = _run_conntrack(["-C"], timeout=3)
    return rc == 0
