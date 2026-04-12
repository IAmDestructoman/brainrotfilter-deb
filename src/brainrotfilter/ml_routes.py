"""
ml_routes.py - FastAPI router for ML classifier management.

Registers at prefix /api/ml.  Include this router in the main FastAPI app
in analyzer_service.py:

    from ml_routes import router as ml_router
    app.include_router(ml_router)

All training is performed synchronously inside a thread-pool executor so the
event loop is not blocked.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from config import DB_PATH
from ml_classifier import (
    FEATURE_NAMES,
    N_FEATURES,
    classifier,
    extract_features,
    _load_training_data,
    _list_model_versions,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ml", tags=["ml"])

# Shared thread pool for blocking training calls
_train_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ml_train")

# Training lock to prevent concurrent retrains
import threading  # noqa: E402
_training_lock = threading.Lock()
_training_in_progress = False


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class TrainResponse(BaseModel):
    trained: bool
    accuracy: float
    samples: int
    n_brainrot: int
    n_not_brainrot: int
    feature_importance: Dict[str, float] = Field(default_factory=dict)
    model_path: str = ""
    message: str = ""


class RollbackRequest(BaseModel):
    version_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_in_executor(fn, *args):
    """Run a blocking function in the thread-pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_train_executor, fn, *args)


def _get_video_from_db(video_id: str) -> Optional[dict]:
    """Load a video row from the DB (with channel enrichment)."""
    import sqlite3
    import json

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT v.*,
                       c.flagged_percentage AS channel_flagged_percentage,
                       c.subscriber_count   AS channel_subscriber_count
                FROM videos v
                LEFT JOIN channels c ON c.channel_id = v.channel_id
                WHERE v.video_id = ?
                LIMIT 1
                """,
                (video_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("DB error fetching video %s: %s", video_id, exc)
        return None

    if not row:
        return None

    d = dict(row)
    for field in ("matched_keywords", "scene_details", "audio_details"):
        val = d.get(field, "")
        if isinstance(val, str):
            try:
                d[field] = json.loads(val)
            except Exception:
                d[field] = {} if field != "matched_keywords" else []
    return d


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/status", summary="ML model status")
async def ml_status() -> Dict[str, Any]:
    """
    Return the current ML model status: trained/untrained, accuracy,
    sample count, last trained timestamp, and feature importance.
    """
    status = classifier.get_status()

    # Append training data overview (live DB counts)
    try:
        _, labels = await _run_in_executor(_load_training_data, DB_PATH)
        n_total = len(labels)
        n_brainrot = sum(1 for label in labels if label == 1)
        status["total_manual_overrides"] = n_total
        status["available_brainrot_labels"] = n_brainrot
        status["available_not_brainrot_labels"] = n_total - n_brainrot
    except Exception as exc:
        logger.warning("Could not count training samples: %s", exc)
        status["total_manual_overrides"] = 0

    status["training_in_progress"] = _training_in_progress
    return status


@router.post("/train", summary="Train or retrain the classifier", response_model=TrainResponse)
async def train_model(background_tasks: BackgroundTasks) -> Dict[str, Any]:
    """
    Trigger a (re)training run.  Training executes in a thread-pool so it
    does not block the event loop.  Only one training run may run at a time.
    """
    global _training_in_progress

    if _training_in_progress:
        raise HTTPException(status_code=409, detail="Training is already in progress.")

    if not _training_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Training lock could not be acquired.")

    _training_in_progress = True

    try:
        result = await _run_in_executor(classifier.train)
    finally:
        _training_in_progress = False
        _training_lock.release()

    return result


@router.get("/predict/{video_id}", summary="Get ML prediction for a video")
async def predict_video(video_id: str) -> Dict[str, Any]:
    """
    Run the trained model against a specific video and return its brainrot
    probability, prediction label, confidence, and feature contributions.
    """
    video = await _run_in_executor(_get_video_from_db, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail=f"Video {video_id!r} not found.")

    pred = await _run_in_executor(classifier.predict, video)
    pred["video_id"] = video_id
    pred["title"] = video.get("title", "")
    pred["actual_status"] = video.get("status", "")
    pred["manual_override"] = bool(video.get("manual_override", False))
    return pred


@router.get("/features/{video_id}", summary="Extract and show feature vector for a video")
async def get_features(video_id: str) -> Dict[str, Any]:
    """
    Return the 22-element feature vector that would be passed to the ML model
    for a given video.  Useful for debugging and understanding the model's inputs.
    """
    video = await _run_in_executor(_get_video_from_db, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail=f"Video {video_id!r} not found.")

    feat = await _run_in_executor(extract_features, video)
    # Convert to a named dict for readability
    feat_list = list(feat)
    feature_dict = {
        FEATURE_NAMES[i]: round(float(feat_list[i]), 4)
        for i in range(min(len(feat_list), N_FEATURES))
    }

    return {
        "video_id": video_id,
        "title": video.get("title", ""),
        "feature_count": N_FEATURES,
        "features": feature_dict,
    }


@router.get("/importance", summary="Feature importance rankings")
async def feature_importance() -> Dict[str, Any]:
    """
    Return features sorted by their importance to the current model.
    If the model has not been trained, returns all zeros.
    """
    ranked = classifier.get_feature_importance_sorted()
    return {
        "trained": classifier.get_status()["trained"],
        "features": ranked,
        "feature_names": FEATURE_NAMES,
    }


@router.get("/predictions", summary="Recent ML predictions table")
async def recent_predictions(
    limit: int = Query(default=20, ge=1, le=100),
) -> Dict[str, Any]:
    """
    Return ML predictions for the most recently-analyzed videos, annotated
    with whether the prediction was correct for manually-overridden ones.
    """
    preds = await _run_in_executor(
        classifier.get_recent_predictions, DB_PATH, limit
    )
    correct = sum(1 for p in preds if p.get("ml_correct") is True)
    total_labeled = sum(1 for p in preds if p.get("ml_correct") is not None)
    return {
        "predictions": preds,
        "total": len(preds),
        "labeled_count": total_labeled,
        "correct_count": correct,
        "prediction_accuracy": round(correct / total_labeled, 4) if total_labeled > 0 else None,
    }


@router.post("/rollback", summary="Roll back to a previous model version")
async def rollback_model(body: RollbackRequest) -> Dict[str, Any]:
    """
    Roll back to a previous versioned model.  If *version_name* is omitted,
    rolls back to the second-most-recent version.
    """
    result = await _run_in_executor(classifier.rollback, body.version_name)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Rollback failed."))
    return result


@router.get("/versions", summary="List saved model versions")
async def list_versions() -> Dict[str, Any]:
    """Return the list of saved model version files."""
    versions = _list_model_versions()
    return {
        "versions": [
            {
                "name": p.name,
                "path": str(p),
                "size_kb": round(p.stat().st_size / 1024, 1) if p.exists() else 0,
            }
            for p in versions[:10]
        ]
    }


@router.post("/check-retrain", summary="Check if retraining is recommended")
async def check_retrain() -> Dict[str, Any]:
    """
    Check whether the classifier recommends retraining based on the number
    of new overrides and model age.
    """
    needed = await _run_in_executor(classifier.check_retrain_needed)
    status = classifier.get_status()
    return {
        "retrain_needed": needed,
        "overrides_since_last_train": status.get("overrides_since_last_train", 0),
        "trained_at": status.get("trained_at"),
        "reason": (
            "New overrides threshold reached or model is stale."
            if needed else
            "No retraining needed at this time."
        ),
    }
