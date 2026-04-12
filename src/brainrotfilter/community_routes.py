"""
community_routes.py — FastAPI router for the community keyword sharing system.

Mounted at /api/community by analyzer_service.py.

Endpoints:
  GET  /api/community/status      — Current sync status
  POST /api/community/check       — Manually trigger update check (no apply)
  POST /api/community/sync        — Fetch + apply community keyword update
  POST /api/community/rollback    — Restore keywords from last backup
  GET  /api/community/diff        — Preview pending changes before applying
  POST /api/community/submit      — Stage a keyword suggestion locally
  GET  /api/community/submissions — List pending keyword submissions
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/community", tags=["community"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class SubmitKeywordRequest(BaseModel):
    keyword: str = Field(..., description="The keyword or phrase to suggest")
    weight: int = Field(..., ge=1, le=10, description="Brainrot signal weight (1-10)")
    category: str = Field(
        ...,
        description="Keyword category (slang, phrases, channel_patterns, audio_speech, emojis)",
    )
    evidence: str = Field(
        "",
        description="Optional context or evidence for why this keyword signals brainrot",
    )


class CommunityConfigUpdate(BaseModel):
    community_keywords_enabled: Optional[bool] = None
    community_keywords_url: Optional[str] = None
    community_keywords_branch: Optional[str] = None
    community_keywords_strategy: Optional[str] = None
    community_keywords_auto_update: Optional[bool] = None
    community_keywords_interval_hours: Optional[int] = Field(None, ge=1, le=720)


# ---------------------------------------------------------------------------
# Helper: load manager and config
# ---------------------------------------------------------------------------


def _get_manager():
    """Return the CommunityKeywordManager singleton."""
    try:
        from community_keywords import community_manager  # type: ignore
        return community_manager
    except ImportError as exc:
        logger.error("community_keywords module not available: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Community keyword module not available",
        )


def _get_config():
    """Return the shared config singleton."""
    try:
        from config import config  # type: ignore
        return config
    except ImportError:
        raise HTTPException(status_code=503, detail="Config module not available")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status")
async def community_status() -> Dict[str, Any]:
    """
    Return the current community keyword sync status.

    Includes: enabled flag, last check time, last content hash, local keyword
    version, strategy, auto-update settings, and backup availability.
    """
    mgr = _get_manager()
    try:
        status = mgr.get_update_status()
        return {"ok": True, "status": status}
    except Exception as exc:
        logger.error("community/status failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/check")
async def community_check() -> Dict[str, Any]:
    """
    Manually trigger an update check without applying changes.

    Fetches the current community keyword list and returns what would change.
    Does NOT modify local keywords.
    """
    mgr = _get_manager()
    try:
        diff = mgr.get_pending_diff()
        if diff is None:
            return {
                "ok": False,
                "error": "Failed to fetch community keywords",
                "diff": None,
            }
        return {
            "ok": True,
            "diff": diff,
            "message": (
                f"{diff['total']} change(s) pending: "
                f"+{len(diff['added'])} added, "
                f"{len(diff['modified'])} modified, "
                f"{len(diff['removed'])} removed."
            ),
        }
    except Exception as exc:
        logger.error("community/check failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/sync")
async def community_sync() -> Dict[str, Any]:
    """
    Fetch the latest community keyword list and apply it.

    Respects the configured merge strategy. Backs up the current keywords.json
    before applying. Returns the diff of changes made.

    The rate-limit interval is bypassed for manual syncs — the check interval
    only applies to auto-update runs.
    """
    mgr = _get_manager()

    # Temporarily clear last_check so the interval check is bypassed
    try:
        from config import config  # type: ignore
        config.save("community_keywords_last_check", "")
    except Exception:
        pass

    try:
        diff = mgr.auto_update()
        if diff.get("error") and not diff.get("changed"):
            return {
                "ok": False,
                "error": diff["error"],
                "changed": False,
                "diff": diff,
            }
        return {
            "ok": True,
            "changed": diff.get("changed", False),
            "diff": diff,
            "message": (
                f"Sync complete: +{len(diff.get('added', []))} added, "
                f"{len(diff.get('modified', []))} modified, "
                f"{len(diff.get('removed', []))} removed."
            ),
        }
    except Exception as exc:
        logger.error("community/sync failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/rollback")
async def community_rollback() -> Dict[str, Any]:
    """
    Restore keywords.json from the most recent backup.

    The backup is created automatically before each sync operation.
    Returns success/failure status.
    """
    mgr = _get_manager()
    try:
        success = mgr.rollback_keywords()
        if success:
            return {"ok": True, "message": "Keywords restored from backup."}
        return {
            "ok": False,
            "error": "No backup available or rollback failed. "
                     "Ensure a sync has been performed first.",
        }
    except Exception as exc:
        logger.error("community/rollback failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/diff")
async def community_diff() -> Dict[str, Any]:
    """
    Show pending keyword changes before applying them.

    Fetches community keywords and computes the diff against current local
    keywords. Does NOT modify any files.
    """
    mgr = _get_manager()
    try:
        diff = mgr.get_pending_diff()
        if diff is None:
            return {
                "ok": False,
                "error": "Failed to fetch community keywords. Check network connectivity.",
                "diff": None,
            }
        return {"ok": True, "diff": diff}
    except Exception as exc:
        logger.error("community/diff failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/submit")
async def community_submit(req: SubmitKeywordRequest) -> Dict[str, Any]:
    """
    Stage a keyword suggestion in the local pending_submissions.json.

    The staged suggestion can then be submitted as a GitHub Issue or PR
    using the admin panel's "Copy as GitHub Issue" button.
    """
    mgr = _get_manager()
    try:
        result = mgr.submit_keyword(
            keyword=req.keyword,
            weight=req.weight,
            category=req.category,
            evidence=req.evidence,
        )
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return {
            "ok": True,
            "submission": result,
            "message": f"Keyword '{req.keyword}' staged for community submission.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("community/submit failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/submissions")
async def community_submissions() -> Dict[str, Any]:
    """
    List all pending keyword submissions staged for community contribution.

    Each submission includes the keyword, weight, category, evidence, and
    a formatted GitHub Issue body that can be copied directly.
    """
    mgr = _get_manager()
    try:
        submissions = mgr.get_submissions()
        # Attach formatted GitHub Issue text for each
        for sub in submissions:
            sub["github_issue_body"] = mgr.format_github_issue(sub)
        return {
            "ok": True,
            "count": len(submissions),
            "submissions": submissions,
        }
    except Exception as exc:
        logger.error("community/submissions failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.put("/config")
async def community_update_config(req: CommunityConfigUpdate) -> Dict[str, Any]:
    """
    Update community keyword sync configuration settings.

    Only the fields present in the request body are updated.
    """
    cfg = _get_config()
    updates: Dict[str, Any] = {}

    payload = req.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="No settings provided")

    valid_keys = {
        "community_keywords_enabled",
        "community_keywords_url",
        "community_keywords_branch",
        "community_keywords_strategy",
        "community_keywords_auto_update",
        "community_keywords_interval_hours",
    }
    for key, value in payload.items():
        if key in valid_keys:
            updates[key] = value

    try:
        cfg.save_many(updates)
        cfg.refresh()
    except Exception as exc:
        logger.error("Failed to save community config: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "ok": True,
        "updated": updates,
        "message": f"Updated {len(updates)} community keyword setting(s).",
    }
