"""
wizard_routes.py
================
FastAPI router for the BrainrotFilter setup wizard (Linux/Debian version).

This wizard configures the local Linux system for transparent proxy filtering.

Endpoints
---------
GET  /wizard                       Redirect to /setup-wizard (compat)
GET  /api/wizard/status            Has the wizard been completed?
#   (POST /api/wizard/test-key was removed in 1.1.0 along with YouTube API)
GET  /api/wizard/detect            Detect local system state
POST /api/wizard/apply             Apply all settings (streaming SSE)
GET  /api/wizard/keywords          Return current keywords
PUT  /api/wizard/keywords          Replace keyword list
GET  /api/wizard/ca-export         Download CA certificate
GET  /api/wizard/interfaces        List network interfaces (with MAC/state/speed/carrier)
POST /api/wizard/configure-bridge  Create transparent L2 bridge (br0) from 2 NICs
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, validator

logger = logging.getLogger("brainrotfilter.wizard")

# -- Constants ----------------------------------------------------------------
CONFIG_DIR = Path(os.environ.get("BRAINROT_CONFIG_DIR", "/etc/brainrotfilter"))
DATA_DIR = Path(os.environ.get("BRAINROT_DATA_DIR", "/var/lib/brainrotfilter"))

for _p in [CONFIG_DIR, DATA_DIR, Path("/tmp/brainrotfilter")]:
    try:
        _p.mkdir(parents=True, exist_ok=True)
        break
    except Exception:
        continue

KEYWORDS_FILE = CONFIG_DIR / "keywords.json"
DB_PATH = Path(os.environ.get("BRAINROT_DB_PATH", str(DATA_DIR / "brainrotfilter.db")))
WIZARD_DONE_FLAG = CONFIG_DIR / ".wizard_complete"

# -- Router -------------------------------------------------------------------
router = APIRouter(prefix="/api/wizard", tags=["wizard"])


# -- Pydantic Models ----------------------------------------------------------

class Keyword(BaseModel):
    keyword: str
    weight: int = Field(default=5, ge=1, le=10)
    category: str = "custom"

class KeywordsPayload(BaseModel):
    keywords: List[Keyword]

class Thresholds(BaseModel):
    combined: int = Field(default=45, ge=0, le=100)
    keyword_weight: float = Field(default=0.40)
    scene_weight: float = Field(default=0.35)
    audio_weight: float = Field(default=0.25)
    monitor_min: int = Field(default=20, ge=0, le=100)
    soft_min: int = Field(default=35, ge=0, le=100)
    block_min: int = Field(default=55, ge=0, le=100)
    channel_flag_percentage: int = Field(default=30, ge=1, le=100)
    auto_escalation: bool = True
    initial_scan_duration: int = Field(default=45, ge=10, le=600)
    full_scan_time_limit: int = Field(default=120, ge=30, le=3600)

    @validator("keyword_weight", "scene_weight", "audio_weight")
    def clamp_weight(cls, v: float) -> float:
        return round(max(0.0, min(1.0, v)), 4)

class WizardApplyRequest(BaseModel):
    # Accept `api_key` as an optional legacy field so older frontends that
    # still submit it don't 422. Value is ignored — classification no
    # longer uses the YouTube Data API.
    api_key: Optional[str] = None
    # Network settings
    network_interface: str = "eth0"
    # Detection settings
    thresholds: Thresholds = Field(default_factory=Thresholds)
    keywords: List[Keyword] = Field(default_factory=list)
    # What to configure
    create_ca: bool = True
    ca_name: str = "BrainrotFilter CA"
    configure_squid: bool = True
    setup_iptables: bool = True
    block_quic: bool = True
    ssl_pinning_methods: List[str] = Field(default_factory=list)

class WizardStatus(BaseModel):
    completed: bool
    redirect: Optional[str] = None


class BridgeConfigRequest(BaseModel):
    """Payload for POST /api/wizard/configure-bridge."""
    wan_nic: str = Field(..., min_length=1)
    lan_nic: str = Field(..., min_length=1)
    mgmt_ip: Optional[str] = None
    mgmt_mask: int = Field(default=24, ge=1, le=32)
    gateway: str = ""
    dns: List[str] = Field(default_factory=list)

    @validator("lan_nic")
    def lan_not_same_as_wan(cls, v, values):
        wan = values.get("wan_nic")
        if wan and v == wan:
            raise ValueError("lan_nic must differ from wan_nic")
        return v


# -- Utility helpers ----------------------------------------------------------

def _save_setting(key: str, value: str) -> None:
    try:
        import sqlite3
        with sqlite3.connect(str(DB_PATH), timeout=3) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) "
                "VALUES (?, ?, strftime('%s','now'))",
                (key, value),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not save setting %s: %s", key, exc)

def _get_setting(key: str, default: str = "") -> str:
    try:
        import sqlite3
        with sqlite3.connect(str(DB_PATH), timeout=3) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else default
    except Exception:
        return default

def _write_keywords(keywords: List[Keyword]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {"keywords": [k.dict() for k in keywords]}
    KEYWORDS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("Wrote %d keywords to %s", len(keywords), KEYWORDS_FILE)


# -- SSE apply stream ---------------------------------------------------------

async def _stream_apply_progress(
    payload: WizardApplyRequest,
) -> AsyncIterator[str]:
    """Generator that performs each setup step, emitting SSE events."""
    from linux_configurator import LinuxConfigurator, LinuxConfig

    def sse(step: str, status: str, message: str, **extra) -> str:
        obj: Dict[str, Any] = {"step": step, "status": status, "message": message}
        obj.update(extra)
        return f"data: {json.dumps(obj)}\n\n"

    try:
        # -- Step 1: Save configuration ------------------------------------
        yield sse("save_config", "running", "Saving configuration...")
        try:
            settings_to_save = {
                "combined_threshold": str(payload.thresholds.combined),
                "keyword_weight": str(payload.thresholds.keyword_weight),
                "scene_weight": str(payload.thresholds.scene_weight),
                "audio_weight": str(payload.thresholds.audio_weight),
                "monitor_min": str(payload.thresholds.monitor_min),
                "soft_block_min": str(payload.thresholds.soft_min),
                "block_min": str(payload.thresholds.block_min),
                "channel_flag_percentage": str(payload.thresholds.channel_flag_percentage),
                "auto_escalation": "1" if payload.thresholds.auto_escalation else "0",
                "initial_scan_duration": str(payload.thresholds.initial_scan_duration),
                "full_scan_time_limit": str(payload.thresholds.full_scan_time_limit),
                "network_interface": payload.network_interface,
            }
            for k, v in settings_to_save.items():
                _save_setting(k, v)
            if payload.keywords:
                _write_keywords(payload.keywords)
            yield sse("save_config", "done", "Configuration saved")
        except Exception as exc:
            yield sse("save_config", "error", f"Failed to save config: {exc}")
            return

        # -- Step 2: Initialize configurator -------------------------------
        linux_config = LinuxConfig(
            network_interface=payload.network_interface,
            ca_name=payload.ca_name,
            block_quic=payload.block_quic,
        )
        configurator = LinuxConfigurator(linux_config)

        # -- Step 3: Create CA certificate ---------------------------------
        yield sse("ca_create", "running", "Creating CA certificate...")
        await asyncio.sleep(0.1)
        try:
            if payload.create_ca:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, configurator.create_ca, payload.ca_name,
                )
                if result.get("success"):
                    existed = result.get("already_existed", False)
                    msg = "Using existing CA" if existed else f"CA created: {payload.ca_name}"
                    yield sse("ca_create", "done", msg)
                else:
                    yield sse("ca_create", "error", f"CA creation failed: {result.get('error')}")
            else:
                yield sse("ca_create", "done", "CA creation skipped")
        except Exception as exc:
            yield sse("ca_create", "error", f"CA creation failed: {exc}")

        # -- Step 4: Configure Squid ---------------------------------------
        yield sse("squid_config", "running", "Configuring Squid SSL interception...")
        await asyncio.sleep(0.1)
        try:
            if payload.configure_squid:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, configurator.configure_squid)
                if result.get("success"):
                    yield sse("squid_config", "done", "Squid configured for SSL interception")
                else:
                    yield sse("squid_config", "error", f"Squid config failed: {result.get('error')}")
            else:
                yield sse("squid_config", "done", "Squid configuration skipped")
        except Exception as exc:
            yield sse("squid_config", "error", f"Squid config failed: {exc}")

        # -- Step 5: Install helper scripts --------------------------------
        yield sse("install_helpers", "running", "Installing helper scripts...")
        await asyncio.sleep(0.1)
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, configurator.install_helpers)
            installed = result.get("installed", [])
            yield sse("install_helpers", "done", f"Installed {len(installed)} helper script(s)")
        except Exception as exc:
            yield sse("install_helpers", "error", f"Helper install failed: {exc}")

        # -- Step 6: Setup iptables ----------------------------------------
        yield sse("iptables", "running", "Configuring iptables rules...")
        await asyncio.sleep(0.1)
        try:
            if payload.setup_iptables:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, configurator.setup_iptables)
                if result.get("success"):
                    added = result.get("rules_added", 0)
                    yield sse("iptables", "done", f"iptables configured ({added} rules added)")
                else:
                    errors = result.get("errors", [])
                    yield sse("iptables", "error", f"iptables errors: {'; '.join(errors)}")
            else:
                yield sse("iptables", "done", "iptables configuration skipped")
        except Exception as exc:
            yield sse("iptables", "error", f"iptables setup failed: {exc}")

        # -- Step 7: SSL pinning bypass ------------------------------------
        ssl_methods = payload.ssl_pinning_methods or []
        if ssl_methods:
            yield sse("ssl_pinning", "running", "Configuring SSL pinning bypass...")
            await asyncio.sleep(0.1)
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, configurator.configure_ssl_pinning, ssl_methods,
                )
                configured = list(result.get("results", {}).keys())
                yield sse("ssl_pinning", "done", f"SSL pinning bypass configured: {configured}")
            except Exception as exc:
                yield sse("ssl_pinning", "error", f"SSL pinning failed: {exc}")
        else:
            yield sse("ssl_pinning", "done", "SSL pinning bypass skipped")

        # -- Step 8: Restart Squid -----------------------------------------
        yield sse("squid_restart", "running", "Restarting Squid service...")
        await asyncio.sleep(0.1)
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, configurator.restart_squid)
            if result.get("success"):
                yield sse("squid_restart", "done", "Squid restarted successfully")
            else:
                yield sse("squid_restart", "warning", f"Squid restart: {result.get('error')}")
        except Exception as exc:
            yield sse("squid_restart", "warning", f"Squid restart: {exc}")

        # -- Step 9: Verify ------------------------------------------------
        yield sse("verify", "running", "Verifying configuration...")
        await asyncio.sleep(1.0)
        try:
            loop = asyncio.get_event_loop()
            checks = await loop.run_in_executor(None, configurator.verify_setup)
            issues = []
            if not checks.get("squid_running"):
                issues.append("Squid not running")
            if not checks.get("helpers_installed"):
                issues.append("Helper scripts missing")
            if not checks.get("ca_exists"):
                issues.append("CA certificate missing")
            if issues:
                yield sse("verify", "done", f"Setup complete with warnings: {', '.join(issues)}", warnings=issues)
            else:
                yield sse("verify", "done", "All checks passed")
        except Exception as exc:
            yield sse("verify", "done", "Setup complete (verification skipped)")

        # -- Mark complete -------------------------------------------------
        try:
            _save_setting("wizard_complete", "1")
            WIZARD_DONE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            WIZARD_DONE_FLAG.touch()
        except Exception:
            pass

        # -- Start filtering services --------------------------------------
        # icap + watchdog have ConditionPathExists on the flag we just
        # wrote, so they were no-ops on boot. Start them now via systemctl
        # (polkit rule 50-brainrotfilter.rules allows the brainrotfilter
        # user to manage these specific units — sudo is blocked by
        # NoNewPrivileges=true on the main service).
        try:
            import subprocess as _sp
            for unit in ("brainrotfilter-icap.service",
                         "brainrotfilter-watchdog.service"):
                _sp.run(
                    ["systemctl", "start", unit],
                    timeout=10, check=False, capture_output=True,
                )
            yield sse("services", "done", "Filtering services started")
        except Exception as exc:
            yield sse("services", "done", f"Services will start at next boot ({exc})")

        # -- Factory snapshot (BTRFS appliances only) ----------------------
        # If the root filesystem is BTRFS and snapper is available, take a
        # "factory -- post-wizard" snapshot that the user can roll back to
        # later from the admin panel.
        try:
            from snapshot_manager import (
                create_factory_snapshot,
                is_btrfs_root,
            )
            if is_btrfs_root():
                yield sse(
                    "factory_snapshot",
                    "running",
                    "Creating factory snapshot...",
                )
                loop = asyncio.get_event_loop()
                snap_res = await loop.run_in_executor(
                    None,
                    create_factory_snapshot,
                    "factory -- post-wizard",
                )
                if snap_res.get("success"):
                    snap_id = snap_res.get("snapshot_id")
                    if snap_res.get("already_existed"):
                        msg = (
                            f"Factory snapshot already exists (#{snap_id})"
                            if snap_id is not None
                            else "Factory snapshot already exists"
                        )
                    else:
                        msg = (
                            f"Factory snapshot created (#{snap_id})"
                            if snap_id is not None
                            else "Factory snapshot created"
                        )
                    yield sse("factory_snapshot", "done", msg)
                else:
                    yield sse(
                        "factory_snapshot",
                        "warning",
                        f"Factory snapshot skipped: "
                        f"{snap_res.get('error') or 'unknown error'}",
                    )
            else:
                logger.debug("Root is not BTRFS -- skipping factory snapshot")
        except Exception as exc:
            logger.warning("Factory snapshot step failed: %s", exc)
            yield sse(
                "factory_snapshot",
                "warning",
                f"Factory snapshot skipped: {exc}",
            )

        yield sse("complete", "done", "Setup complete!", done=True)

    except Exception as exc:
        logger.error("Wizard apply error: %s", exc)
        yield sse("error", "error", f"Unexpected error: {exc}")


# -- Route Handlers -----------------------------------------------------------

@router.get("/status", response_model=WizardStatus)
async def wizard_status() -> WizardStatus:
    completed = (
        os.environ.get("BRAINROT_WIZARD_SKIP", "").strip() == "1"
        or WIZARD_DONE_FLAG.exists()
        or _get_setting("wizard_complete") == "1"
    )
    return WizardStatus(
        completed=completed,
        redirect="/setup-wizard" if not completed else None,
    )

@router.get("/detect")
async def detect_system_state() -> Dict[str, Any]:
    """Detect current Linux system configuration state."""
    from linux_configurator import LinuxConfigurator
    configurator = LinuxConfigurator()
    state = configurator.detect_state()
    state["success"] = True
    return state

@router.get("/interfaces")
async def list_interfaces() -> Dict[str, Any]:
    """List available network interfaces with enough detail to identify
    WAN vs LAN: MAC, operstate, carrier (cable plugged?), speed (Mbps),
    and a hint whether the interface is virtual/bridge.
    """
    from linux_configurator import LinuxConfigurator
    configurator = LinuxConfigurator()
    state = configurator.detect_state()
    ifaces = state.get("network_interfaces", [])
    # Also enrich with IPv4 address if available -- helpful for the user.
    try:
        import socket
        import fcntl
        import struct
        def _ip_for(ifname: str) -> Optional[str]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    # SIOCGIFADDR
                    packed = fcntl.ioctl(
                        s.fileno(),
                        0x8915,
                        struct.pack("256s", ifname[:15].encode()),
                    )
                    return socket.inet_ntoa(packed[20:24])
                finally:
                    s.close()
            except Exception:
                return None
        for i in ifaces:
            i["addr"] = _ip_for(i["name"])
    except Exception:
        # fcntl isn't available on every platform; ignore silently.
        pass

    # Detect an existing br0 created out-of-band (e.g. via the TUI
    # "Assign Interfaces" action). If present, the wizard can skip its
    # own bridge-creation step and just adopt the existing setup.
    bridge: Dict[str, Any] = {"exists": False, "name": "br0", "members": []}
    try:
        import os
        brif_dir = "/sys/class/net/br0/brif"
        if os.path.isdir(brif_dir):
            bridge["exists"] = True
            bridge["members"] = sorted(os.listdir(brif_dir))
            try:
                with open("/sys/class/net/br0/operstate") as f:
                    bridge["state"] = f.read().strip()
            except Exception:
                bridge["state"] = "unknown"
    except Exception:
        pass

    return {"interfaces": ifaces, "bridge": bridge}


@router.post("/configure-bridge")
async def configure_bridge(payload: BridgeConfigRequest) -> Dict[str, Any]:
    """Create a transparent L2 bridge (``br0``) from two NICs.

    Writes netplan + sysctl + modules-load configuration and returns the
    applied config. The caller is expected to trigger ``netplan apply``
    (or reboot) afterwards to activate the bridge.
    """
    from linux_configurator import LinuxConfigurator
    configurator = LinuxConfigurator()
    try:
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: configurator.configure_bridge(
                wan_nic=payload.wan_nic,
                lan_nic=payload.lan_nic,
                mgmt_ip=payload.mgmt_ip,
                mgmt_mask=payload.mgmt_mask,
                gateway=payload.gateway,
                dns=payload.dns,
            ),
        )
        if not result.get("success"):
            raise HTTPException(
                status_code=500,
                detail=result.get("error", "configure_bridge failed"),
            )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("configure_bridge error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

@router.get("/keywords")
async def get_keywords() -> Dict[str, Any]:
    try:
        if KEYWORDS_FILE.exists():
            return json.loads(KEYWORDS_FILE.read_text())
        return {"keywords": _builtin_keywords()}
    except Exception:
        return {"keywords": _builtin_keywords()}

@router.put("/keywords")
async def put_keywords(payload: KeywordsPayload) -> Dict[str, Any]:
    try:
        _write_keywords(payload.keywords)
        return {"success": True, "count": len(payload.keywords)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

@router.get("/ca-export")
async def export_ca():
    """Download the BrainrotFilter CA certificate as a PEM file."""
    from fastapi.responses import Response
    from linux_configurator import LinuxConfigurator
    configurator = LinuxConfigurator()
    pem = configurator.get_ca_cert_pem()
    if not pem:
        raise HTTPException(status_code=404, detail="CA not found. Run the setup wizard first.")
    return Response(
        content=pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": "attachment; filename=BrainrotFilter-CA.crt"},
    )

@router.get("/container-health")
async def container_health():
    """Stream system health checks as SSE events."""
    import sys as _sys

    async def stream():
        def sse(step, status, message):
            return f"data: {json.dumps({'step': step, 'status': status, 'message': message})}\n\n"

        yield sse("python_env", "done", f"Python {_sys.version.split()[0]}")

        # GPU check
        try:
            import torch
            if torch.cuda.is_available():
                yield sse("gpu_check", "done", f"GPU: {torch.cuda.get_device_name(0)}")
            else:
                yield sse("gpu_check", "warning", "No GPU -- CPU mode")
        except ImportError:
            yield sse("gpu_check", "warning", "PyTorch not installed -- CPU mode")

        # DB check
        try:
            import sqlite3
            with sqlite3.connect(str(DB_PATH), timeout=3) as conn:
                conn.execute("SELECT 1")
            yield sse("db_check", "done", "Database OK")
        except Exception as e:
            yield sse("db_check", "error", str(e))

        yield sse("service_ready", "done", "System ready")
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.post("/apply")
async def apply_wizard(payload: WizardApplyRequest, request: Request) -> StreamingResponse:
    return StreamingResponse(
        _stream_apply_progress(payload),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _builtin_keywords() -> List[Dict[str, Any]]:
    """Return built-in default brainrot keyword list."""
    return [
        {"keyword": "skibidi", "weight": 9, "category": "slang"},
        {"keyword": "rizz", "weight": 8, "category": "slang"},
        {"keyword": "sigma", "weight": 7, "category": "slang"},
        {"keyword": "gyatt", "weight": 9, "category": "slang"},
        {"keyword": "sussy", "weight": 6, "category": "slang"},
        {"keyword": "based", "weight": 5, "category": "slang"},
        {"keyword": "no cap", "weight": 6, "category": "slang"},
        {"keyword": "bussin", "weight": 5, "category": "slang"},
        {"keyword": "gooning", "weight": 10, "category": "slang"},
        {"keyword": "edging", "weight": 9, "category": "slang"},
        {"keyword": "mewing", "weight": 7, "category": "slang"},
        {"keyword": "looksmaxxing", "weight": 7, "category": "slang"},
        {"keyword": "delulu", "weight": 7, "category": "slang"},
        {"keyword": "mogging", "weight": 7, "category": "slang"},
        {"keyword": "NPC", "weight": 8, "category": "slang"},
        {"keyword": "ohio", "weight": 8, "category": "meme"},
        {"keyword": "only in ohio", "weight": 9, "category": "meme"},
        {"keyword": "fanum tax", "weight": 7, "category": "meme"},
        {"keyword": "brainrot", "weight": 10, "category": "format"},
        {"keyword": "NPC stream", "weight": 9, "category": "format"},
        {"keyword": "alpha male", "weight": 7, "category": "format"},
        {"keyword": "split screen", "weight": 7, "category": "format"},
        {"keyword": "subway surfers", "weight": 8, "category": "visual_cue"},
        {"keyword": "minecraft parkour", "weight": 8, "category": "visual_cue"},
        {"keyword": "ear rape", "weight": 9, "category": "audio_cue"},
        {"keyword": "phonk", "weight": 6, "category": "audio_cue"},
    ]
