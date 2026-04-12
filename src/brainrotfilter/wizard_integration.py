"""
wizard_integration.py
=====================
Integrates the BrainrotFilter setup wizard into the main FastAPI application
(analyzer_service.py).

Usage -- add to analyzer_service.py
------------------------------------

    from wizard_integration import integrate_wizard
    integrate_wizard(app)

What this module does
---------------------
1. Registers all wizard API routes (prefix /api/wizard).
2. Adds the GET /setup-wizard and /wizard routes that serve wizard.html.
3. Installs a middleware that redirects anonymous visitors to /setup-wizard
   when the wizard has not been completed yet.

Wizard-complete detection (in priority order)
----------------------------------------------
1. Environment var:   BRAINROT_WIZARD_SKIP=1 (for development/testing)
2. Filesystem flag:   .wizard_complete (in config dir)
3. Database setting:  settings table -> key='wizard_complete', value='1'

Middleware bypass rules (requests that are NEVER redirected)
-------------------------------------------------------------
- /setup-wizard, /wizard, and /api/wizard/* (wizard itself)
- /api/*                          (all API calls from admin panel)
- /static/*                       (CSS, JS, images)
- /block                          (block page shown to clients)
- /warning                        (soft-block warning page)
- /favicon.ico
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("brainrotfilter.wizard_integration")

# -- Paths -----------------------------------------------------------------
CONFIG_DIR = Path(os.environ.get("BRAINROT_CONFIG_DIR", "/etc/brainrotfilter"))
DATA_DIR = Path(os.environ.get("BRAINROT_DATA_DIR", "/var/lib/brainrotfilter"))
WWW_DIR = Path(os.environ.get("BRAINROT_WWW_DIR", "/usr/share/brainrotfilter/www"))

WIZARD_HTML_PATH = WWW_DIR / "templates" / "wizard.html"
WIZARD_DONE_FLAG = CONFIG_DIR / ".wizard_complete"
DB_PATH = Path(os.environ.get("BRAINROT_DB_PATH", str(DATA_DIR / "brainrotfilter.db")))

# Fallback template search for development layout
_DEV_TEMPLATES = Path(__file__).parent.parent.parent / "templates"
if not WIZARD_HTML_PATH.exists() and (_DEV_TEMPLATES / "wizard.html").exists():
    WIZARD_HTML_PATH = _DEV_TEMPLATES / "wizard.html"

# -- URL prefixes that bypass the wizard redirect --------------------------
_BYPASS_PREFIXES = (
    "/setup-wizard",
    "/wizard",
    "/api/wizard",
    "/api/",
    "/static/",
    "/block",
    "/warning",
    "/favicon",
    "/_",
)


# ==========================================================================
# Wizard completion check
# ==========================================================================

def _wizard_completed() -> bool:
    """
    Return True if the wizard has been completed.
    """
    # Dev/CI bypass
    if os.environ.get("BRAINROT_WIZARD_SKIP", "").strip() == "1":
        return True

    # Fast filesystem check
    if WIZARD_DONE_FLAG.exists():
        return True

    # DB check
    try:
        import sqlite3
        if not DB_PATH.exists():
            return False
        with sqlite3.connect(str(DB_PATH), timeout=3) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'wizard_complete' LIMIT 1"
            ).fetchone()
            if row and row[0] == "1":
                try:
                    WIZARD_DONE_FLAG.parent.mkdir(parents=True, exist_ok=True)
                    WIZARD_DONE_FLAG.touch()
                except Exception:
                    pass
                return True
    except Exception as exc:
        logger.debug("DB wizard check failed (assuming not complete): %s", exc)

    return False


def _should_bypass(path: str) -> bool:
    """Return True if the request path should skip the wizard redirect."""
    return any(path.startswith(prefix) for prefix in _BYPASS_PREFIXES)


# ==========================================================================
# Wizard HTML serving
# ==========================================================================

def _read_wizard_html() -> str:
    """Read wizard.html from disk. Returns a minimal error page if missing."""
    if WIZARD_HTML_PATH.exists():
        return WIZARD_HTML_PATH.read_text(encoding="utf-8")

    logger.error("wizard.html not found at %s", WIZARD_HTML_PATH)
    return """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>BrainrotFilter Setup</title>
<style>body{background:#1a1a2e;color:#e2e8f0;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
.box{background:#16213e;border:1px solid rgba(0,180,216,.2);border-radius:12px;
padding:40px;max-width:500px;text-align:center;}
h1{color:#00b4d8;margin-bottom:12px;}p{color:#94a3b8;}
</style></head>
<body><div class="box">
<h1>BrainrotFilter</h1>
<p>The setup wizard template is missing.</p>
<p>Please re-install the BrainrotFilter package or check that
the wizard.html template exists.</p>
<p><a href="/" style="color:#00b4d8">Back to admin panel</a></p>
</div></body></html>"""


# ==========================================================================
# integrate() -- the main entry point
# ==========================================================================

def integrate(app: FastAPI) -> None:
    """
    Wire the setup wizard into a FastAPI application.
    """
    # -- 1. Register wizard API router ------------------------------------
    from wizard_routes import router as wizard_router
    app.include_router(wizard_router)
    logger.info("Wizard API routes registered (prefix=/api/wizard)")

    # -- 2. Serve wizard.html at /setup-wizard and /wizard ----------------
    @app.get(
        "/setup-wizard",
        response_class=HTMLResponse,
        include_in_schema=False,
        summary="First-time setup wizard",
    )
    async def serve_wizard() -> HTMLResponse:
        html = _read_wizard_html()
        return HTMLResponse(content=html, status_code=200)

    @app.get(
        "/wizard",
        response_class=HTMLResponse,
        include_in_schema=False,
        summary="First-time setup wizard (alias)",
    )
    async def serve_wizard_alias() -> HTMLResponse:
        html = _read_wizard_html()
        return HTMLResponse(content=html, status_code=200)

    logger.info("GET /setup-wizard and /wizard routes registered")

    # -- 3. Wizard redirect middleware ------------------------------------
    @app.middleware("http")
    async def wizard_redirect_middleware(request: Request, call_next: Callable):
        """
        If the wizard has not been completed and the request is for the root
        path, redirect to /setup-wizard.
        """
        path = request.url.path

        if _should_bypass(path):
            return await call_next(request)

        accept_header = request.headers.get("accept", "")
        is_browser_request = "text/html" in accept_header

        if is_browser_request and path in ("/", ""):
            if not _wizard_completed():
                logger.info("Wizard not complete -- redirecting to /setup-wizard")
                return RedirectResponse(url="/setup-wizard", status_code=302)

        return await call_next(request)

    logger.info("Wizard redirect middleware installed")


# ==========================================================================
# integrate_wizard() -- alias kept for backward compatibility
# ==========================================================================

def integrate_wizard(app: FastAPI) -> None:
    """Alias for integrate(). Prefer integrate() in new code."""
    integrate(app)
