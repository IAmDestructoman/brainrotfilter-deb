"""
snapshot_manager.py
===================
BTRFS + snapper integration for factory reset on the BrainrotFilter appliance.

This module provides helpers to:
  - Detect whether / is on a BTRFS filesystem
  - Ensure snapper has a "root" config for /
  - Create a "factory" snapshot tagged via snapper userdata
  - List all snapshots
  - Roll back to the factory snapshot

All public functions return a dict containing at least:
    {"success": bool, "error": str, ...}

All subprocess calls use timeout=30 and structured error handling so that a
hung snapper/btrfs invocation cannot take the analyzer service down with it.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("brainrotfilter.snapshot_manager")

# Marker stored in snapper userdata so we can find the "factory" snapshot again
# after reboot / rollback / etc.
FACTORY_USERDATA_KEY = "factory"
FACTORY_USERDATA_VALUE = "true"
DEFAULT_FACTORY_DESCRIPTION = "factory -- post-wizard"

_SUBPROCESS_TIMEOUT = 30


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------

def _run(cmd: List[str]) -> Dict[str, Any]:
    """
    Run a command with a bounded timeout and return a structured result.

    Returns a dict with:
        success : bool
        returncode : int
        stdout : str
        stderr : str
        error  : str  (empty on success)
    """
    logger.debug("snapshot_manager running: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "error": "" if proc.returncode == 0 else (proc.stderr or "").strip(),
        }
    except FileNotFoundError as exc:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "error": f"command not found: {cmd[0]} ({exc})",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "error": f"timeout after {exc.timeout}s running {cmd[0]}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _tool_available(name: str) -> bool:
    return shutil.which(name) is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_btrfs_root() -> bool:
    """
    Return True if / is on a BTRFS filesystem.

    Uses `findmnt / -o FSTYPE -n` which prints just the fstype (e.g. "btrfs").
    """
    if not _tool_available("findmnt"):
        logger.debug("findmnt not available; assuming non-btrfs root")
        return False
    res = _run(["findmnt", "/", "-o", "FSTYPE", "-n"])
    if not res["success"]:
        logger.debug("findmnt failed: %s", res.get("error"))
        return False
    fstype = res["stdout"].strip().lower()
    return fstype == "btrfs"


def _snapper_available() -> bool:
    return _tool_available("snapper")


def _has_root_config() -> bool:
    """Return True if snapper has a config named 'root'."""
    if not _snapper_available():
        return False
    res = _run(["snapper", "list-configs"])
    if not res["success"]:
        return False
    # Typical output:
    #   Config | Subvolume
    #   -------+----------
    #   root   | /
    for line in res["stdout"].splitlines():
        parts = [p.strip() for p in line.split("|")]
        if parts and parts[0] == "root":
            return True
    return False


def ensure_snapper_configured() -> Dict[str, Any]:
    """
    Ensure snapper has a 'root' config for /. Create one if missing.

    Returns a dict with:
        success : bool
        configured : bool  (True if a root config already existed or was created)
        created : bool     (True if we just created the config)
        error : str
    """
    if not is_btrfs_root():
        return {
            "success": False,
            "configured": False,
            "created": False,
            "error": "root filesystem is not btrfs",
        }
    if not _snapper_available():
        return {
            "success": False,
            "configured": False,
            "created": False,
            "error": "snapper not installed",
        }

    if _has_root_config():
        return {
            "success": True,
            "configured": True,
            "created": False,
            "error": "",
        }

    res = _run(["snapper", "-c", "root", "create-config", "/"])
    if not res["success"]:
        return {
            "success": False,
            "configured": False,
            "created": False,
            "error": res["error"] or "snapper create-config failed",
        }
    return {
        "success": True,
        "configured": True,
        "created": True,
        "error": "",
    }


def _parse_snapshot_list_json(stdout: str) -> List[Dict[str, Any]]:
    """
    Parse the output of `snapper list --output-format json`.

    Newer snapper versions emit:
        {"root": [ { "number": 1, "description": "...", "userdata": {...}, ...}, ... ]}
    Some versions emit a bare list. Handle both.
    """
    try:
        data = json.loads(stdout)
    except Exception as exc:
        logger.warning("failed to parse snapper JSON: %s", exc)
        return []
    if isinstance(data, dict):
        for key in ("root", "snapshots"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        # Fall back to first list-valued entry
        for val in data.values():
            if isinstance(val, list):
                return val
        return []
    if isinstance(data, list):
        return data
    return []


def list_snapshots() -> List[Dict[str, Any]]:
    """
    Return all snapshots from snapper's root config.

    On error returns an empty list. Callers that need the error string should
    use the lower-level helpers directly.
    """
    if not is_btrfs_root() or not _snapper_available():
        return []
    if not _has_root_config():
        return []

    res = _run(["snapper", "-c", "root", "list", "--output-format", "json"])
    if res["success"] and res["stdout"]:
        return _parse_snapshot_list_json(res["stdout"])

    # Fallback: parse the plain-text output. This is best-effort; we only need
    # number/description/userdata for our callers.
    res_txt = _run(["snapper", "-c", "root", "list"])
    if not res_txt["success"]:
        return []
    snapshots: List[Dict[str, Any]] = []
    for line in res_txt["stdout"].splitlines():
        parts = [p.strip() for p in line.split("|")]
        # Header/separator rows
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        snap: Dict[str, Any] = {"number": int(parts[0])}
        if len(parts) >= 6:
            snap["type"] = parts[1]
            snap["date"] = parts[3]
            snap["description"] = parts[-2] if len(parts) >= 7 else ""
            snap["userdata"] = parts[-1]
        snapshots.append(snap)
    return snapshots


def _userdata_has_factory_flag(userdata: Any) -> bool:
    """
    snapper's JSON output represents userdata as a dict, but the plain-text
    fallback gives us a comma-separated string like 'factory=true,foo=bar'.
    Handle both.
    """
    if userdata is None:
        return False
    if isinstance(userdata, dict):
        val = userdata.get(FACTORY_USERDATA_KEY)
        return str(val).lower() == FACTORY_USERDATA_VALUE
    if isinstance(userdata, str):
        for entry in userdata.split(","):
            entry = entry.strip()
            if "=" not in entry:
                continue
            k, v = entry.split("=", 1)
            if k.strip() == FACTORY_USERDATA_KEY and v.strip().lower() == FACTORY_USERDATA_VALUE:
                return True
    return False


def get_factory_snapshot_id() -> Optional[int]:
    """
    Return the snapshot number of the snapshot flagged as the factory baseline,
    or None if no such snapshot exists.

    Selection rules, in priority order:
      1. A snapshot whose userdata contains factory=true.
      2. A snapshot whose description starts with 'factory' (case-insensitive).

    If multiple match, the lowest-numbered one is returned (oldest factory).
    """
    snapshots = list_snapshots()
    if not snapshots:
        return None

    # Pass 1: userdata flag
    flagged: List[int] = []
    for snap in snapshots:
        num = snap.get("number")
        if not isinstance(num, int):
            try:
                num = int(str(num))
            except Exception:
                continue
        if _userdata_has_factory_flag(snap.get("userdata")):
            flagged.append(num)
    if flagged:
        return min(flagged)

    # Pass 2: description prefix
    by_desc: List[int] = []
    for snap in snapshots:
        num = snap.get("number")
        if not isinstance(num, int):
            try:
                num = int(str(num))
            except Exception:
                continue
        desc = str(snap.get("description") or "").strip().lower()
        if desc.startswith("factory"):
            by_desc.append(num)
    if by_desc:
        return min(by_desc)

    return None


def create_factory_snapshot(
    description: str = DEFAULT_FACTORY_DESCRIPTION,
) -> Dict[str, Any]:
    """
    Create a snapper snapshot flagged as the factory baseline.

    The snapshot is tagged via --userdata factory=true so that
    get_factory_snapshot_id() can find it again after reboot.

    Returns:
        success : bool
        snapshot_id : Optional[int]
        description : str
        already_existed : bool
        error : str
    """
    result: Dict[str, Any] = {
        "success": False,
        "snapshot_id": None,
        "description": description,
        "already_existed": False,
        "error": "",
    }

    if not is_btrfs_root():
        result["error"] = "root filesystem is not btrfs; snapshot skipped"
        return result
    if not _snapper_available():
        result["error"] = "snapper not installed"
        return result

    cfg = ensure_snapper_configured()
    if not cfg.get("success"):
        result["error"] = cfg.get("error") or "snapper configure failed"
        return result

    existing = get_factory_snapshot_id()
    if existing is not None:
        result["success"] = True
        result["snapshot_id"] = existing
        result["already_existed"] = True
        return result

    # `snapper create` prints the new snapshot number when given --print-number.
    # --cleanup-algorithm=number ensures the snapshot can be retained properly.
    cmd = [
        "snapper", "-c", "root", "create",
        "--type", "single",
        "--cleanup-algorithm", "number",
        "--description", description,
        "--userdata", f"{FACTORY_USERDATA_KEY}={FACTORY_USERDATA_VALUE}",
        "--print-number",
    ]
    res = _run(cmd)
    if not res["success"]:
        result["error"] = res["error"] or "snapper create failed"
        return result

    snap_id: Optional[int] = None
    stdout = res["stdout"].strip()
    if stdout:
        try:
            snap_id = int(stdout.splitlines()[-1].strip())
        except Exception:
            snap_id = None

    if snap_id is None:
        # Fall back to locating it by the userdata flag we just set.
        snap_id = get_factory_snapshot_id()

    result["success"] = True
    result["snapshot_id"] = snap_id
    return result


def rollback_to_factory() -> Dict[str, Any]:
    """
    Roll back the root subvolume to the factory snapshot.

    The rollback takes effect on the next boot -- snapper prepares the default
    subvolume but does NOT reboot for us. Callers should surface the
    `reboot_required` field to the user.

    Returns:
        success : bool
        snapshot_id : Optional[int]
        reboot_required : bool
        note : str
        error : str
    """
    result: Dict[str, Any] = {
        "success": False,
        "snapshot_id": None,
        "reboot_required": False,
        "note": "",
        "error": "",
    }

    if not is_btrfs_root():
        result["error"] = "root filesystem is not btrfs; rollback unavailable"
        return result
    if not _snapper_available():
        result["error"] = "snapper not installed"
        return result
    if not _has_root_config():
        result["error"] = "snapper root config not present"
        return result

    snap_id = get_factory_snapshot_id()
    if snap_id is None:
        result["error"] = "no factory snapshot found"
        return result

    res = _run(["snapper", "-c", "root", "rollback", str(snap_id)])
    if not res["success"]:
        result["error"] = res["error"] or "snapper rollback failed"
        result["snapshot_id"] = snap_id
        return result

    result["success"] = True
    result["snapshot_id"] = snap_id
    result["reboot_required"] = True
    result["note"] = (
        "Rollback prepared. Reboot the appliance to boot into the factory "
        "snapshot."
    )
    return result
