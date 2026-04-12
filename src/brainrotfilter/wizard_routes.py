"""
wizard_routes.py
================
FastAPI router for the BrainrotFilter setup wizard (Linux/Debian version).

Unlike the pfSense version, this wizard configures the LOCAL Linux system
directly -- no SSH/REST connection to a remote firewall needed.

Endpoints
---------
GET  /wizard                       Redirect to /setup-wizard (compat)
GET  /api/wizard/status            Has the wizard been completed?
POST /api/wizard/test-key          Validate a YouTube Data API v3 key
GET  /api/wizard/detect            Detect local system state
POST /api/wizard/apply             Apply all settings (streaming SSE)
GET  /api/wizard/keywords          Return current keywords
PUT  /api/wizard/keywords          Replace keyword list
GET  /api/wizard/ca-export         Download CA certificate
GET  /api/wizard/interfaces        List network interfaces
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

class ApiKeyTestRequest(BaseModel):
    api_key: str = Field(..., min_length=1)

class ApiKeyTestResponse(BaseModel):
    valid: bool
    message: str
    quota_info: Optional[str] = None

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
    api_key: str
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
                "youtube_api_key": payload.api_key,
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

@router.post("/test-key", response_model=ApiKeyTestResponse)
async def test_api_key(req: ApiKeyTestRequest) -> ApiKeyTestResponse:
    test_video_id = "dQw4w9WgXcQ"
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"id": test_video_id, "part": "snippet", "key": req.api_key}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("items"):
                return ApiKeyTestResponse(
                    valid=True,
                    message="API key is valid.",
                    quota_info="1 unit used (daily limit: 10,000 units)",
                )
            return ApiKeyTestResponse(valid=True, message="API key accepted but returned no items.")
        elif resp.status_code == 403:
            error_info = resp.json().get("error", {})
            reason = error_info.get("errors", [{}])[0].get("reason", "")
            if reason == "quotaExceeded":
                return ApiKeyTestResponse(valid=True, message="Key valid but quota exhausted.")
            return ApiKeyTestResponse(valid=False, message="API key rejected (403). Enable YouTube Data API v3.")
        else:
            return ApiKeyTestResponse(valid=False, message=f"HTTP {resp.status_code}")
    except httpx.TimeoutException:
        return ApiKeyTestResponse(valid=False, message="Request timed out.")
    except Exception as exc:
        return ApiKeyTestResponse(valid=False, message=f"Error: {exc}")

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
    """List available network interfaces."""
    from linux_configurator import LinuxConfigurator
    configurator = LinuxConfigurator()
    state = configurator.detect_state()
    return {"interfaces": state.get("network_interfaces", [])}

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

        # API key
        api_key = os.environ.get("YOUTUBE_API_KEY", "")
        if api_key and api_key != "your_youtube_api_key_here":
            yield sse("api_key_check", "done", "YouTube API key configured")
        else:
            yield sse("api_key_check", "warning", "YouTube API key not set")

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
