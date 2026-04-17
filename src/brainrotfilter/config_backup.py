"""
config_backup.py - Export / import BrainrotFilter configuration bundles.

Packages the SQLite database, environment file, keyword lists, and Squid
config fragments into a single gzipped tarball that can be downloaded
from the admin panel and later restored to another appliance (or the
same one after a rebuild).

Typical use:

    from config_backup import export_config_bundle, import_config_bundle

    data = export_config_bundle()                      # bytes (.tar.gz)
    result = import_config_bundle(data)                # restores + restarts
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    from version import __version__
except ImportError:  # pragma: no cover - fallback when imported standalone
    __version__ = "0.0.0"

logger = logging.getLogger(__name__)


# Files included in the bundle.  Tuple is (absolute_path, archive_name).
# Archive names use forward slashes and mirror the on-disk layout so a
# plain `tar tf` listing is self-documenting.
_BUNDLE_FILES: List[Tuple[str, str]] = [
    ("/var/lib/brainrotfilter/brainrotfilter.db",   "var/lib/brainrotfilter/brainrotfilter.db"),
    ("/etc/brainrotfilter/brainrotfilter.env",      "etc/brainrotfilter/brainrotfilter.env"),
    ("/etc/brainrotfilter/keywords.json",           "etc/brainrotfilter/keywords.json"),
    ("/etc/brainrotfilter/community-keywords.json", "etc/brainrotfilter/community-keywords.json"),
    ("/etc/brainrotfilter/squid_brainrot.conf",     "etc/brainrotfilter/squid_brainrot.conf"),
    ("/etc/squid/conf.d/brainrotfilter.conf",       "etc/squid/conf.d/brainrotfilter.conf"),
]

_METADATA_NAME = "metadata.json"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_config_bundle() -> bytes:
    """
    Build an in-memory gzipped tarball of the BrainrotFilter config.

    Files that don't exist on disk are silently skipped (fresh installs
    may not have Squid fragments yet, for example).  Always includes a
    ``metadata.json`` at the archive root describing the export.
    """
    buf = io.BytesIO()
    included: List[str] = []

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for src, arcname in _BUNDLE_FILES:
            p = Path(src)
            if not p.exists() or not p.is_file():
                logger.debug("export_config_bundle: skipping missing %s", src)
                continue
            try:
                tar.add(str(p), arcname=arcname, recursive=False)
                included.append(src)
            except Exception as exc:
                logger.warning("export_config_bundle: failed to add %s: %s", src, exc)

        # Metadata
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "unknown"

        metadata = {
            "version": __version__,
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "hostname": hostname,
            "files": included,
        }
        meta_bytes = json.dumps(metadata, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name=_METADATA_NAME)
        info.size = len(meta_bytes)
        info.mtime = int(datetime.utcnow().timestamp())
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(meta_bytes))

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _version_major_minor(v: str) -> Tuple[int, int]:
    parts = (v or "0.0.0").split(".")
    try:
        return int(parts[0]), int(parts[1] if len(parts) > 1 else 0)
    except ValueError:
        return 0, 0


def _safe_extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo, dest_root: Path) -> Path:
    """
    Extract a single tar member under *dest_root*, resolving to absolute paths
    while blocking path-traversal (``..``) escapes and symlinks.
    """
    # Reject anything that's not a regular file (no symlinks / devices).
    if not member.isfile():
        raise ValueError(f"refusing non-regular member {member.name!r}")

    # Normalise archive name -> absolute path (archive uses forward slashes and
    # a root-less layout mirroring /).
    arcname = member.name.replace("\\", "/").lstrip("/")
    target = (dest_root / arcname).resolve()
    if not str(target).startswith(str(dest_root.resolve())):
        raise ValueError(f"blocked path traversal for {member.name!r}")

    target.parent.mkdir(parents=True, exist_ok=True)
    extracted = tar.extractfile(member)
    if extracted is None:
        raise ValueError(f"could not read member {member.name!r}")
    with open(target, "wb") as fh:
        shutil.copyfileobj(extracted, fh)
    return target


def _restart_brainrotfilter() -> Tuple[bool, str]:
    """Restart brainrotfilter.service via systemctl.  Returns (ok, message)."""
    try:
        res = subprocess.run(
            ["systemctl", "restart", "brainrotfilter.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode == 0:
            return True, "brainrotfilter.service restarted"
        return False, f"systemctl exit {res.returncode}: {res.stderr.strip()}"
    except FileNotFoundError:
        return False, "systemctl not available"
    except Exception as exc:
        return False, f"restart failed: {exc}"


def import_config_bundle(tar_bytes: bytes, restart_services: bool = True) -> Dict[str, Any]:
    """
    Restore files from a bundle produced by :func:`export_config_bundle`.

    The import is done via a staging temp dir so a corrupt archive can't
    leave the system with half-written files.  Version compatibility is
    checked at the major.minor level; mismatches are reported but do not
    block restoration (we emit a warning in ``errors``).
    """
    result: Dict[str, Any] = {
        "success": False,
        "restored": [],
        "errors": [],
    }

    if not tar_bytes:
        result["errors"].append("empty upload")
        return result

    allowed_archive_paths = {arcname for _src, arcname in _BUNDLE_FILES}

    # Resolve archive-name -> absolute destination.  We don't trust paths
    # inside the archive for the *final* write location — we map them.
    arc_to_dest = {arcname: src for src, arcname in _BUNDLE_FILES}

    try:
        tar_buf = io.BytesIO(tar_bytes)
        with tarfile.open(fileobj=tar_buf, mode="r:gz") as tar:
            members = tar.getmembers()

            # ── Metadata check ──────────────────────────────────────────
            meta_member = next(
                (m for m in members if m.name == _METADATA_NAME),
                None,
            )
            if meta_member is None:
                result["errors"].append("metadata.json missing from bundle")
            else:
                try:
                    mf = tar.extractfile(meta_member)
                    metadata = json.loads(mf.read().decode("utf-8")) if mf else {}
                    bundle_ver = str(metadata.get("version") or "0.0.0")
                    cur_mm = _version_major_minor(__version__)
                    bundle_mm = _version_major_minor(bundle_ver)
                    if cur_mm != bundle_mm:
                        msg = (
                            f"version mismatch: bundle {bundle_ver} "
                            f"vs installed {__version__} "
                            f"(major.minor differ — restoring anyway)"
                        )
                        result["errors"].append(msg)
                        logger.warning(msg)
                    result["bundle_version"] = bundle_ver
                    result["exported_at"] = metadata.get("exported_at")
                    result["hostname"] = metadata.get("hostname")
                except Exception as exc:
                    result["errors"].append(f"could not parse metadata.json: {exc}")

            # ── Stage extraction to a temp dir ─────────────────────────
            with tempfile.TemporaryDirectory(prefix="brainrot-import-") as tmpdir:
                staging = Path(tmpdir)
                staged: Dict[str, Path] = {}  # arcname -> staged path

                for m in members:
                    if m.name == _METADATA_NAME:
                        continue
                    arc = m.name.replace("\\", "/").lstrip("/")
                    if arc not in allowed_archive_paths:
                        logger.warning("skipping unexpected member: %s", m.name)
                        continue
                    try:
                        staged_path = _safe_extract_member(tar, m, staging)
                        staged[arc] = staged_path
                    except Exception as exc:
                        result["errors"].append(f"extract {m.name}: {exc}")

                # ── Copy staged files into place ───────────────────────
                for arc, staged_path in staged.items():
                    dest = Path(arc_to_dest[arc])
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        # Preserve a .bak of the previous file, best-effort.
                        if dest.exists():
                            try:
                                shutil.copy2(str(dest), str(dest) + ".bak")
                            except Exception as bex:
                                logger.debug("backup of %s failed: %s", dest, bex)
                        shutil.copy2(str(staged_path), str(dest))
                        # Reasonable default perms; .env holds secrets.
                        try:
                            if dest.name.endswith(".env"):
                                os.chmod(dest, 0o640)
                            else:
                                os.chmod(dest, 0o644)
                        except Exception:
                            pass
                        result["restored"].append(str(dest))
                    except Exception as exc:
                        result["errors"].append(f"install {dest}: {exc}")

        # ── Restart service if requested ──────────────────────────────
        if restart_services and result["restored"]:
            ok, msg = _restart_brainrotfilter()
            result["restart"] = {"ok": ok, "message": msg}
            if not ok:
                result["errors"].append(msg)

        result["success"] = bool(result["restored"]) and not any(
            e for e in result["errors"]
            if not e.startswith("version mismatch")
        )
    except tarfile.ReadError as exc:
        result["errors"].append(f"not a valid tar.gz: {exc}")
    except Exception as exc:
        logger.exception("import_config_bundle failed")
        result["errors"].append(str(exc))

    return result
