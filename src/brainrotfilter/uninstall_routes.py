"""
uninstall_routes.py
===================
FastAPI router for uninstalling BrainrotFilter configuration from the local
Linux system.

Endpoints
---------
GET  /uninstall          Serve uninstall page
GET  /uninstall/status   Check what BrainrotFilter has configured (JSON)
POST /uninstall/apply    SSE stream -- run uninstall steps
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logger = logging.getLogger("brainrotfilter.uninstall")

router = APIRouter(tags=["uninstall"])

# -- Templates --------------------------------------------------------------
WWW_DIR = Path(
    os.environ.get("BRAINROT_WWW_DIR", "/usr/share/brainrotfilter/www")
)
TEMPLATES_DIR = WWW_DIR / "templates"

# Fallback: dev paths
if not TEMPLATES_DIR.exists():
    _dev = Path(__file__).parent.parent.parent / "templates"
    if _dev.exists():
        TEMPLATES_DIR = _dev

_templates: Optional[Jinja2Templates] = None
if TEMPLATES_DIR.exists():
    _templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# -- Models -----------------------------------------------------------------

class UninstallApplyRequest(BaseModel):
    """No credentials needed for local Linux uninstall."""
    confirm: bool = True


# -- SSE uninstall stream ---------------------------------------------------

async def _stream_uninstall(
    payload: UninstallApplyRequest,
) -> AsyncIterator[str]:
    """Run uninstall steps, emitting SSE events."""
    from linux_configurator import LinuxConfigurator

    def sse(step: str, status: str, message: str, **extra) -> str:
        obj: Dict[str, Any] = {
            "step": step, "status": status, "message": message,
        }
        obj.update(extra)
        return f"data: {json.dumps(obj)}\n\n"

    configurator = LinuxConfigurator()

    try:
        # -- Detect current state ----------------------------------------
        yield sse("detect", "running", "Detecting current configuration...")
        await asyncio.sleep(0.1)
        try:
            state = configurator.detect_state()
            yield sse("detect", "done", "Configuration detected")
        except Exception as exc:
            yield sse("detect", "error", f"Detection failed: {exc}")
            return

        # -- Remove Squid configuration ----------------------------------
        yield sse(
            "remove_squid_config", "running",
            "Removing Squid SSL configuration...",
        )
        await asyncio.sleep(0.1)
        try:
            squid_conf = Path("/etc/squid/conf.d/brainrotfilter.conf")
            if squid_conf.exists():
                squid_conf.unlink()
                yield sse(
                    "remove_squid_config", "done",
                    "Squid configuration removed",
                )
            else:
                yield sse(
                    "remove_squid_config", "done",
                    "Squid configuration not found (already clean)",
                )
        except Exception as exc:
            yield sse("remove_squid_config", "error", str(exc))

        # -- Remove CA certificate ----------------------------------------
        yield sse(
            "remove_ca", "running", "Removing BrainrotFilter CA..."
        )
        await asyncio.sleep(0.1)
        try:
            ca_dir = Path("/etc/brainrotfilter/ssl")
            removed = 0
            if ca_dir.exists():
                for f in ca_dir.iterdir():
                    f.unlink()
                    removed += 1
                ca_dir.rmdir()
            yield sse(
                "remove_ca", "done",
                f"Removed CA directory ({removed} file(s))",
            )
        except Exception as exc:
            yield sse("remove_ca", "error", str(exc))

        # -- Remove iptables rules ----------------------------------------
        yield sse(
            "remove_iptables", "running",
            "Removing iptables rules...",
        )
        await asyncio.sleep(0.1)
        try:
            configurator.remove_iptables()
            yield sse(
                "remove_iptables", "done",
                "iptables rules removed",
            )
        except Exception as exc:
            yield sse("remove_iptables", "error", str(exc))

        # -- Remove DNS/hosts entries ------------------------------------
        yield sse(
            "remove_dns_blocks", "running",
            "Removing /etc/hosts entries...",
        )
        await asyncio.sleep(0.1)
        try:
            hosts = Path("/etc/hosts")
            if hosts.exists():
                lines = hosts.read_text().splitlines()
                cleaned = [
                    l for l in lines
                    if "# BrainrotFilter" not in l
                ]
                if len(cleaned) < len(lines):
                    hosts.write_text("\n".join(cleaned) + "\n")
                    removed = len(lines) - len(cleaned)
                    yield sse(
                        "remove_dns_blocks", "done",
                        f"Removed {removed} /etc/hosts entry(ies)",
                    )
                else:
                    yield sse(
                        "remove_dns_blocks", "done",
                        "No BrainrotFilter entries in /etc/hosts",
                    )
            else:
                yield sse(
                    "remove_dns_blocks", "done",
                    "/etc/hosts not found",
                )
        except Exception as exc:
            yield sse("remove_dns_blocks", "error", str(exc))

        # -- Remove shell helpers -----------------------------------------
        yield sse(
            "remove_helpers", "running",
            "Removing shell helper scripts...",
        )
        await asyncio.sleep(0.1)
        try:
            scripts_dir = Path("/usr/lib/brainrotfilter/scripts")
            if scripts_dir.exists():
                import shutil
                shutil.rmtree(scripts_dir)
                yield sse(
                    "remove_helpers", "done",
                    "Shell helper scripts removed",
                )
            else:
                yield sse(
                    "remove_helpers", "done",
                    "Shell helpers not found (already clean)",
                )
        except Exception as exc:
            yield sse("remove_helpers", "error", str(exc))

        # -- Restart Squid ------------------------------------------------
        yield sse(
            "restart_squid", "running", "Restarting Squid service..."
        )
        await asyncio.sleep(0.1)
        try:
            configurator.restart_squid()
            yield sse(
                "restart_squid", "done",
                "Squid restarted successfully",
            )
        except Exception as exc:
            yield sse("restart_squid", "warning", str(exc))

        # -- Remove wizard complete flag ----------------------------------
        yield sse("cleanup", "running", "Cleaning up...")
        await asyncio.sleep(0.1)
        try:
            flag = Path("/etc/brainrotfilter/.wizard_complete")
            if flag.exists():
                flag.unlink()
            yield sse("cleanup", "done", "Wizard flag removed")
        except Exception as exc:
            yield sse("cleanup", "warning", str(exc))

        # -- Verify removal -----------------------------------------------
        yield sse("verify", "running", "Verifying removal...")
        await asyncio.sleep(1.0)
        try:
            state_after = configurator.detect_state()
            issues = []
            if state_after.get("squid_configured"):
                issues.append("Squid config still present")
            if state_after.get("iptables_configured"):
                issues.append("iptables rules still present")
            if state_after.get("ca_exists"):
                issues.append("CA certificate still present")
            if issues:
                yield sse(
                    "verify", "warning",
                    f"Warnings: {', '.join(issues)}",
                )
            else:
                yield sse("verify", "done", "All BrainrotFilter "
                          "configuration removed from this system")
        except Exception as exc:
            yield sse("verify", "warning",
                      f"Verification skipped: {exc}")

        yield sse("complete", "done",
                  "Uninstall complete. You can now safely stop "
                  "the BrainrotFilter service.",
                  done=True)

    except Exception as exc:
        yield sse("error", "error", f"Unexpected error: {exc}")


# ==========================================================================
# Route handlers
# ==========================================================================

@router.get("/uninstall", response_class=HTMLResponse,
            summary="Uninstall page")
async def uninstall_page(request: Request):
    """Serve the BrainrotFilter uninstall page."""
    if _templates is None:
        return HTMLResponse(
            "<h1>Templates not found</h1>", status_code=500
        )
    return _templates.TemplateResponse(
        request,
        "uninstall.html",
        {"active_page": "uninstall"},
    )


@router.get("/uninstall/status", summary="Check BrainrotFilter status")
async def uninstall_status() -> Dict[str, Any]:
    """Check what BrainrotFilter has configured on this system."""
    from linux_configurator import LinuxConfigurator

    configurator = LinuxConfigurator()
    status: Dict[str, Any] = {
        "success": True,
        "ca_exists": False,
        "squid_configured": False,
        "iptables_rules": False,
        "helpers_installed": False,
        "config_dir_exists": False,
        "dns_blocks": False,
    }
    try:
        state = configurator.detect_state()
        status["ca_exists"] = state.get("ca_exists", False)
        status["squid_configured"] = state.get("squid_configured", False)
        status["iptables_rules"] = state.get("iptables_configured", False)
        status["helpers_installed"] = state.get("helpers_installed", False)
        status["config_dir_exists"] = Path("/etc/brainrotfilter").exists()

        # Check /etc/hosts for BrainrotFilter entries
        hosts = Path("/etc/hosts")
        if hosts.exists():
            content = hosts.read_text()
            status["dns_blocks"] = "# BrainrotFilter" in content

        return status

    except Exception as exc:
        return {"success": False, "error": str(exc)}


@router.post("/uninstall/apply",
             summary="Uninstall BrainrotFilter (streaming SSE)")
async def apply_uninstall(
    payload: UninstallApplyRequest = UninstallApplyRequest(),
) -> StreamingResponse:
    """Run uninstall steps. Returns SSE stream with progress."""
    return StreamingResponse(
        _stream_uninstall(payload),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
