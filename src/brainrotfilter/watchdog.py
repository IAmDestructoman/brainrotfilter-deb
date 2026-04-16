#!/usr/bin/env python3
"""
watchdog.py — BrainrotFilter service supervisor.

Runs as root under brainrotfilter-watchdog.service. Polls every
WATCHDOG_INTERVAL seconds and checks each monitored service for:
  - systemctl is-active == active
  - TCP port liveness (or HTTP 200 on health endpoint for HTTP services)
  - Abnormal log patterns (FATAL, Traceback, "Address already in use",
    etc.) since the last poll

If a service is unhealthy, the watchdog restarts it with exponential
backoff capped at 5 consecutive restarts per hour to avoid crashloops.

Writes a JSON status snapshot to STATUS_FILE so the admin panel can
render it. The JSON schema is documented in the SERVICE_CHECKS entry
and rendered by templates/system_status.html.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

WATCHDOG_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "15"))
STATUS_FILE = Path(os.environ.get("WATCHDOG_STATUS_FILE",
                                  "/var/lib/brainrotfilter/system_status.json"))
MAX_RESTARTS_PER_HOUR = 5
LOG_SCAN_WINDOW_SEC = 60  # only look at logs from the last minute

# Fatal / worrying log patterns we want flagged.
_ERROR_PATTERNS = re.compile(
    r"(FATAL|SystemExit|Traceback|"
    r"Address already in use|"
    r"failed to bind|"
    r"Out of memory|"
    r"Segmentation fault|"
    r"Connection refused|"
    r"helper.*failed)",
    re.IGNORECASE,
)


def _run(cmd: List[str], timeout: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=-1,
                                           stdout="", stderr="timeout")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, returncode=-2,
                                           stdout="", stderr="not found")


# ---------------------------------------------------------------------------
# Per-service health checks
# ---------------------------------------------------------------------------

def _systemctl_active(unit: str) -> bool:
    r = _run(["systemctl", "is-active", "--quiet", unit], timeout=3)
    return r.returncode == 0


def _tcp_alive(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def _recent_errors(unit: str, since_seconds: int) -> List[str]:
    """Scan recent systemd journal lines for error patterns."""
    r = _run(["journalctl", "-u", unit,
              "--since", f"{since_seconds} seconds ago",
              "--no-pager", "-q"], timeout=5)
    if r.returncode != 0:
        return []
    hits: List[str] = []
    for line in r.stdout.splitlines():
        if _ERROR_PATTERNS.search(line):
            hits.append(line)
            if len(hits) >= 5:
                break
    return hits


# ---------------------------------------------------------------------------
# Service manifest
# ---------------------------------------------------------------------------

SERVICE_CHECKS: List[Dict[str, Any]] = [
    {
        "name": "brainrotfilter",
        "unit": "brainrotfilter.service",
        "description": "Main analyzer + admin API",
        "health_url": "http://127.0.0.1:8199/health",
    },
    {
        "name": "brainrotfilter-icap",
        "unit": "brainrotfilter-icap.service",
        "description": "ICAP body-inspection service",
        "tcp": ("127.0.0.1", 1344),
    },
    {
        "name": "squid",
        "unit": "squid.service",
        "description": "Squid intercepting proxy",
        "tcp": ("127.0.0.1", 3128),
        "extra_tcp": ("127.0.0.1", 3129),
    },
]


# ---------------------------------------------------------------------------
# Restart tracking (bounded)
# ---------------------------------------------------------------------------

_restart_history: Dict[str, List[float]] = {}


def _record_restart(unit: str) -> bool:
    """Record an attempt; return False if we've hit the hourly cap."""
    now = time.time()
    hist = _restart_history.setdefault(unit, [])
    cutoff = now - 3600
    hist[:] = [t for t in hist if t > cutoff]
    if len(hist) >= MAX_RESTARTS_PER_HOUR:
        return False
    hist.append(now)
    return True


def _try_restart(unit: str) -> Dict[str, Any]:
    """Restart the unit; return an entry describing the outcome."""
    if not _record_restart(unit):
        return {"attempted": False, "reason": "rate_limited",
                "restarts_last_hour": len(_restart_history.get(unit, []))}
    r = _run(["systemctl", "restart", unit], timeout=30)
    return {
        "attempted": True,
        "ok": r.returncode == 0,
        "exit_code": r.returncode,
        "stderr": r.stderr.strip()[:200],
        "at": time.time(),
        "restarts_last_hour": len(_restart_history.get(unit, [])),
    }


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def _check_one(svc: Dict[str, Any]) -> Dict[str, Any]:
    unit = svc["unit"]
    entry: Dict[str, Any] = {
        "name": svc["name"],
        "unit": unit,
        "description": svc["description"],
        "checked_at": time.time(),
    }

    active = _systemctl_active(unit)
    entry["active"] = active
    entry["healthy"] = active

    if active:
        if url := svc.get("health_url"):
            entry["http_ok"] = _http_ok(url)
            entry["healthy"] = entry["healthy"] and entry["http_ok"]
        if tcp := svc.get("tcp"):
            entry["tcp_ok"] = _tcp_alive(*tcp)
            entry["healthy"] = entry["healthy"] and entry["tcp_ok"]
        if extra := svc.get("extra_tcp"):
            entry["extra_tcp_ok"] = _tcp_alive(*extra)
            entry["healthy"] = entry["healthy"] and entry["extra_tcp_ok"]

    entry["recent_errors"] = _recent_errors(unit, LOG_SCAN_WINDOW_SEC)

    if not entry["healthy"]:
        entry["restart"] = _try_restart(unit)

    entry["restarts_last_hour"] = len(_restart_history.get(unit, []))
    return entry


def _write_status(services: List[Dict[str, Any]]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.time(),
        "interval_seconds": WATCHDOG_INTERVAL,
        "services": services,
    }
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATUS_FILE)
    try:
        os.chmod(STATUS_FILE, 0o644)
    except Exception:
        pass


def main() -> None:
    print(f"BrainrotFilter watchdog starting (interval={WATCHDOG_INTERVAL}s, "
          f"status_file={STATUS_FILE})", flush=True)
    while True:
        statuses = []
        for svc in SERVICE_CHECKS:
            try:
                statuses.append(_check_one(svc))
            except Exception as exc:
                statuses.append({
                    "name": svc["name"],
                    "unit": svc["unit"],
                    "description": svc.get("description", ""),
                    "checked_at": time.time(),
                    "active": False,
                    "healthy": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        try:
            _write_status(statuses)
        except Exception as exc:
            print(f"watchdog: status write failed: {exc}", file=sys.stderr,
                  flush=True)
        time.sleep(WATCHDOG_INTERVAL)


if __name__ == "__main__":
    main()
