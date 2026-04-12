"""
ml_classifier.py - Lightweight ML classifier for BrainrotFilter.

Trains a binary classifier (brainrot vs not-brainrot) on the videos that
admins have manually overridden, learning from those human decisions.

Algorithm: LogisticRegression (primary) with optional RandomForest for larger
datasets.  Falls back gracefully when scikit-learn is not installed.

Model storage: /usr/local/etc/brainrotfilter/ml_model.pkl (via joblib)
Feature vector: 22 fixed-length features extracted from video analysis data.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from config import DB_PATH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional sklearn import — graceful degradation if not installed
# ---------------------------------------------------------------------------

try:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import StratifiedKFold, cross_val_score
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    import joblib
    _SKLEARN_AVAILABLE = True
except ImportError:
    _SKLEARN_AVAILABLE = False
    np = None  # type: ignore[assignment]
    logger.warning(
        "scikit-learn not available. ML classifier will run in stub mode. "
        "Install with: pip install scikit-learn joblib numpy"
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_DIR = Path(os.environ.get(
    "BRAINROT_MODEL_DIR",
    "/usr/local/etc/brainrotfilter",
))
MODEL_PATH = MODEL_DIR / "ml_model.pkl"
MODEL_MAX_VERSIONS = 3

# Minimum training set requirements
MIN_TOTAL_SAMPLES = 20
MIN_PER_CLASS = 10

# Retrain triggers
NEW_OVERRIDES_TRIGGER = 10   # retrain after this many new manual overrides
STALE_DAYS_TRIGGER = 7       # retrain if model older than this many days

# Feature count (must match extract_features output)
N_FEATURES = 22
FEATURE_NAMES = [
    "keyword_score",
    "scene_score",
    "audio_score",
    "shorts_score",
    "comment_score",
    "thumbnail_score",
    "engagement_score",
    "is_short",
    "duration_log",
    "view_count_log",
    "like_count_log",
    "comment_count_log",
    "engagement_ratio",
    "channel_flagged_pct",
    "channel_subscriber_log",
    "matched_keyword_count",
    "cuts_per_minute",
    "audio_chaos_score",
    "repetitive_speech_ratio",
    "thumbnail_saturation",
    "text_overlay_ratio",
    "category_is_entertainment",
]


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def _safe_log(x: float) -> float:
    """log(x+1), clamped to [0, 20]."""
    try:
        val = math.log1p(max(float(x), 0.0))
        return min(val, 20.0)
    except (ValueError, TypeError):
        return 0.0


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.0


def extract_features(video_data: dict) -> "np.ndarray":  # type: ignore[name-defined]
    """
    Extract a fixed-length 22-element feature vector from video analysis data.

    video_data is a dict in the shape of the `videos` DB row with nested
    scene_details and audio_details JSON objects decoded.

    Returns a numpy array of shape (22,).  If sklearn/numpy is not available,
    returns a plain list (the caller must handle both types).
    """
    scene = video_data.get("scene_details") or {}
    if isinstance(scene, str):
        try:
            scene = json.loads(scene)
        except (json.JSONDecodeError, TypeError):
            scene = {}

    audio = video_data.get("audio_details") or {}
    if isinstance(audio, str):
        try:
            audio = json.loads(audio)
        except (json.JSONDecodeError, TypeError):
            audio = {}

    chaos = audio.get("chaos") or {}
    nlp = audio.get("nlp") or {}
    thumbnail_meta = video_data.get("thumbnail_meta") or {}
    channel_meta = video_data.get("channel_meta") or {}

    # --- Score features (0-100) ---
    keyword_score = _clamp(video_data.get("keyword_score", 0.0))
    scene_score = _clamp(video_data.get("scene_score", 0.0))
    audio_score = _clamp(video_data.get("audio_score", 0.0))
    # Shorts / vertical video proxy: derived from scene_score + is_short flag
    shorts_score = _clamp(video_data.get("shorts_score", scene_score * 0.7))
    # Comment toxicity score if available, else 0
    comment_score = _clamp(video_data.get("comment_score", 0.0))
    thumbnail_score = _clamp(video_data.get("thumbnail_score", 0.0))

    # Engagement heuristic: high engagement relative to subscribers is typical
    # for viral short-form content.
    view_count = max(0.0, float(video_data.get("view_count", 0) or 0))
    like_count = max(0.0, float(video_data.get("like_count", 0) or 0))
    comment_count_raw = max(0.0, float(video_data.get("comment_count", 0) or 0))
    engagement_num = like_count + comment_count_raw
    engagement_ratio = min(engagement_num / max(view_count, 1.0), 1.0)
    # Normalise engagement to 0-100 (0.05 ratio → 100 is already extreme)
    engagement_score = _clamp(engagement_ratio * 2000.0)

    # --- Binary / categorical ---
    duration_s = max(0.0, float(video_data.get("duration_seconds", 0) or 0))
    is_short = 1.0 if (duration_s > 0 and duration_s <= 60.0) or bool(video_data.get("is_short")) else 0.0

    category_id = str(video_data.get("category_id", ""))
    category_is_entertainment = 1.0 if category_id in ("24", "22", "10") else 0.0

    # --- Log-normalised counts ---
    duration_log = _safe_log(duration_s)
    view_count_log = _safe_log(view_count)
    like_count_log = _safe_log(like_count)
    comment_count_log = _safe_log(comment_count_raw)

    # --- Channel features ---
    channel_flagged_pct = _clamp(
        float(channel_meta.get("flagged_percentage", video_data.get("channel_flagged_percentage", 0.0)) or 0.0)
    )
    channel_sub_count = max(0.0, float(channel_meta.get("subscriber_count", video_data.get("channel_subscriber_count", 0)) or 0))
    channel_subscriber_log = _safe_log(channel_sub_count)

    # --- Keyword count ---
    matched_kws = video_data.get("matched_keywords")
    if isinstance(matched_kws, str):
        try:
            matched_kws = json.loads(matched_kws)
        except (json.JSONDecodeError, TypeError):
            matched_kws = []
    matched_keyword_count = float(len(matched_kws or []))

    # --- Scene features ---
    cuts_per_minute = _clamp(
        float(scene.get("cuts_per_minute", video_data.get("cuts_per_minute", 0.0)) or 0.0),
        lo=0.0, hi=300.0,
    )

    # --- Audio features ---
    audio_chaos_score = _clamp(float(chaos.get("chaos_score", 0.0) or 0.0))
    repetitive_speech_ratio = _clamp(float(nlp.get("nonsense_ratio", 0.0) or 0.0) * 100.0)

    # --- Thumbnail features ---
    thumbnail_saturation = _clamp(float(thumbnail_meta.get("saturation", 0.0) or 0.0))
    text_overlay_ratio = _clamp(float(thumbnail_meta.get("text_overlay_ratio", 0.0) or 0.0) * 100.0)

    feat = [
        keyword_score,
        scene_score,
        audio_score,
        shorts_score,
        comment_score,
        thumbnail_score,
        engagement_score,
        is_short,
        duration_log,
        view_count_log,
        like_count_log,
        comment_count_log,
        engagement_ratio * 100.0,   # scale to 0-100
        channel_flagged_pct,
        channel_subscriber_log,
        matched_keyword_count,
        cuts_per_minute,
        audio_chaos_score,
        repetitive_speech_ratio,
        thumbnail_saturation,
        text_overlay_ratio,
        category_is_entertainment,
    ]

    if _SKLEARN_AVAILABLE and np is not None:
        return np.array(feat, dtype=np.float64)
    return feat  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Model version helpers
# ---------------------------------------------------------------------------

def _timestamped_model_path() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return MODEL_DIR / f"ml_model_{ts}.pkl"


def _list_model_versions() -> List[Path]:
    """Return model pkl files sorted newest-first."""
    versions = sorted(MODEL_DIR.glob("ml_model_*.pkl"), reverse=True)
    return versions


def _prune_old_versions() -> None:
    """Keep only the N most recent versioned model files."""
    versions = _list_model_versions()
    for old in versions[MODEL_MAX_VERSIONS:]:
        try:
            old.unlink()
            logger.info("Pruned old model version: %s", old.name)
        except OSError as exc:
            logger.warning("Could not prune %s: %s", old.name, exc)


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

@contextmanager
def _db_conn(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _load_training_data(db_path: str) -> Tuple[List[dict], List[int]]:
    """
    Load all manually-overridden videos from the DB for training.

    Labels:
    - 1 (brainrot)    if status in ('block', 'soft_block')
    - 0 (not brainrot) if status == 'allow'
    - skip             if status == 'monitor' (ambiguous label)

    Returns (video_rows, labels) — parallel lists.
    """
    try:
        with _db_conn(db_path) as conn:
            rows = conn.execute(
                """
                SELECT v.*,
                       c.flagged_percentage AS channel_flagged_percentage,
                       c.subscriber_count   AS channel_subscriber_count
                FROM videos v
                LEFT JOIN channels c ON c.channel_id = v.channel_id
                WHERE v.manual_override = 1
                  AND v.status IN ('block', 'soft_block', 'allow')
                ORDER BY v.updated_at DESC
                """
            ).fetchall()
    except sqlite3.Error as exc:
        logger.error("DB error loading training data: %s", exc)
        return [], []

    video_dicts: List[dict] = []
    labels: List[int] = []

    for row in rows:
        d = dict(row)
        # Decode JSON fields
        for field in ("matched_keywords", "scene_details", "audio_details"):
            val = d.get(field, "")
            if isinstance(val, str):
                try:
                    d[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    d[field] = {} if field != "matched_keywords" else []

        status = d.get("status", "")
        if status in ("block", "soft_block"):
            label = 1
        elif status == "allow":
            label = 0
        else:
            continue  # skip monitor

        video_dicts.append(d)
        labels.append(label)

    return video_dicts, labels


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class BrainrotClassifier:
    """
    Lightweight ML classifier that learns from admin override decisions.

    Uses scikit-learn LogisticRegression (small, fast, interpretable).
    Falls back gracefully if sklearn is not installed.
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        self._pipeline: Optional[Any] = None   # sklearn Pipeline
        self._rf_pipeline: Optional[Any] = None
        self._active_pipeline: Optional[Any] = None  # whichever is current

        self._trained = False
        self._accuracy: float = 0.0
        self._n_samples: int = 0
        self._n_brainrot: int = 0
        self._n_not_brainrot: int = 0
        self._trained_at: Optional[str] = None
        self._model_path: Optional[str] = None
        self._feature_importance: Dict[str, float] = {}

        self._lock = threading.Lock()
        self._overrides_since_train: int = 0

        self._load_model()

    # ------------------------------------------------------------------
    # Model persistence
    # ------------------------------------------------------------------

    def _load_model(self) -> bool:
        """Try to load the most recent model from disk."""
        if not _SKLEARN_AVAILABLE:
            return False
        if not MODEL_PATH.exists():
            # Try versioned files
            versions = _list_model_versions()
            if not versions:
                logger.debug("No trained ML model found.")
                return False
            model_file = versions[0]
        else:
            model_file = MODEL_PATH

        try:
            data = joblib.load(str(model_file))
            with self._lock:
                self._active_pipeline = data.get("pipeline")
                self._accuracy = data.get("accuracy", 0.0)
                self._n_samples = data.get("n_samples", 0)
                self._n_brainrot = data.get("n_brainrot", 0)
                self._n_not_brainrot = data.get("n_not_brainrot", 0)
                self._trained_at = data.get("trained_at")
                self._model_path = str(model_file)
                self._feature_importance = data.get("feature_importance", {})
                self._trained = True
            logger.info(
                "ML model loaded from %s (accuracy=%.3f, n=%d)",
                model_file.name, self._accuracy, self._n_samples,
            )
            return True
        except Exception as exc:
            logger.warning("Failed to load ML model from %s: %s", model_file, exc)
            return False

    def _save_model(self, pipeline: Any, accuracy: float,
                    n_samples: int, n_brainrot: int, n_not_brainrot: int,
                    feature_importance: Dict[str, float]) -> str:
        """Persist model to disk (versioned + symlink)."""
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        versioned = _timestamped_model_path()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = {
            "pipeline": pipeline,
            "accuracy": accuracy,
            "n_samples": n_samples,
            "n_brainrot": n_brainrot,
            "n_not_brainrot": n_not_brainrot,
            "trained_at": now_iso,
            "feature_importance": feature_importance,
        }
        joblib.dump(data, str(versioned), compress=3)
        # Also write/overwrite the stable path
        joblib.dump(data, str(MODEL_PATH), compress=3)
        _prune_old_versions()
        logger.info("ML model saved: %s (accuracy=%.3f)", versioned.name, accuracy)
        return str(versioned)

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self) -> Dict[str, Any]:
        """
        Train the classifier on all manually-overridden videos.

        Returns a status dict:
        {
            "trained": bool,
            "accuracy": float,
            "samples": int,
            "n_brainrot": int,
            "n_not_brainrot": int,
            "feature_importance": dict,
            "model_path": str,
            "message": str,
        }
        """
        if not _SKLEARN_AVAILABLE:
            return {
                "trained": False,
                "accuracy": 0.0,
                "samples": 0,
                "n_brainrot": 0,
                "n_not_brainrot": 0,
                "feature_importance": {},
                "model_path": "",
                "message": "scikit-learn is not installed. Cannot train model.",
            }

        logger.info("Starting ML training…")
        video_rows, labels = _load_training_data(self._db_path)

        n_total = len(labels)
        n_brainrot = sum(1 for label in labels if label == 1)
        n_not = n_total - n_brainrot

        if n_total < MIN_TOTAL_SAMPLES:
            return {
                "trained": False,
                "accuracy": 0.0,
                "samples": n_total,
                "n_brainrot": n_brainrot,
                "n_not_brainrot": n_not,
                "feature_importance": {},
                "model_path": "",
                "message": (
                    f"Insufficient training data ({n_total} samples, need {MIN_TOTAL_SAMPLES}). "
                    "Override more videos and try again."
                ),
            }

        if n_brainrot < MIN_PER_CLASS or n_not < MIN_PER_CLASS:
            return {
                "trained": False,
                "accuracy": 0.0,
                "samples": n_total,
                "n_brainrot": n_brainrot,
                "n_not_brainrot": n_not,
                "feature_importance": {},
                "model_path": "",
                "message": (
                    f"Class imbalance: {n_brainrot} brainrot, {n_not} not-brainrot. "
                    f"Need at least {MIN_PER_CLASS} examples per class."
                ),
            }

        # Extract feature matrix
        X_list = [extract_features(row) for row in video_rows]
        X = np.array(X_list, dtype=np.float64)
        y = np.array(labels, dtype=np.int32)

        # --------------- Logistic Regression ---------------
        lr_pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(
                C=1.0,
                penalty="l2",
                max_iter=500,
                class_weight="balanced",
                random_state=42,
                solver="lbfgs",
            )),
        ])

        # Cross-validation (5-fold stratified)
        cv_splits = min(5, min(n_brainrot, n_not))
        cv_splits = max(cv_splits, 2)
        try:
            cv = StratifiedKFold(n_splits=cv_splits, shuffle=True, random_state=42)
            cv_scores = cross_val_score(lr_pipeline, X, y, cv=cv, scoring="accuracy")
            accuracy = float(cv_scores.mean())
        except Exception as exc:
            logger.warning("CV failed (%s), using train-set accuracy.", exc)
            lr_pipeline.fit(X, y)
            accuracy = float((lr_pipeline.predict(X) == y).mean())

        lr_pipeline.fit(X, y)
        best_pipeline = lr_pipeline
        best_accuracy = accuracy

        # --------------- Random Forest (if ≥100 samples) ---------------
        if n_total >= 100:
            rf_pipeline = Pipeline([
                ("scaler", StandardScaler()),
                ("clf", RandomForestClassifier(
                    n_estimators=100,
                    max_depth=8,
                    class_weight="balanced",
                    random_state=42,
                    n_jobs=1,
                )),
            ])
            try:
                rf_cv = StratifiedKFold(n_splits=cv_splits, shuffle=True, random_state=42)
                rf_scores = cross_val_score(rf_pipeline, X, y, cv=rf_cv, scoring="accuracy")
                rf_accuracy = float(rf_scores.mean())
                rf_pipeline.fit(X, y)
                if rf_accuracy > best_accuracy:
                    best_pipeline = rf_pipeline
                    best_accuracy = rf_accuracy
                    logger.info("RF outperformed LR (%.3f > %.3f); using RF.", rf_accuracy, accuracy)
            except Exception as exc:
                logger.warning("RF training failed (%s); keeping LR.", exc)

        # --------------- Feature importance ---------------
        clf = best_pipeline.named_steps["clf"]
        feature_importance: Dict[str, float] = {}
        try:
            if hasattr(clf, "coef_"):
                importances = np.abs(clf.coef_[0])
            elif hasattr(clf, "feature_importances_"):
                importances = clf.feature_importances_
            else:
                importances = np.zeros(N_FEATURES)

            total = importances.sum() or 1.0
            feature_importance = {
                FEATURE_NAMES[i]: round(float(importances[i] / total) * 100, 2)
                for i in range(min(len(importances), N_FEATURES))
            }
        except Exception as exc:
            logger.warning("Feature importance extraction failed: %s", exc)

        # --------------- Save ---------------
        saved_path = self._save_model(
            best_pipeline, best_accuracy, n_total, n_brainrot, n_not, feature_importance
        )

        with self._lock:
            self._active_pipeline = best_pipeline
            self._accuracy = best_accuracy
            self._n_samples = n_total
            self._n_brainrot = n_brainrot
            self._n_not_brainrot = n_not
            self._trained = True
            self._trained_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._model_path = saved_path
            self._feature_importance = feature_importance
            self._overrides_since_train = 0

        logger.info(
            "ML training complete: accuracy=%.3f, samples=%d (%d brainrot, %d not)",
            best_accuracy, n_total, n_brainrot, n_not,
        )

        return {
            "trained": True,
            "accuracy": best_accuracy,
            "samples": n_total,
            "n_brainrot": n_brainrot,
            "n_not_brainrot": n_not,
            "feature_importance": feature_importance,
            "model_path": saved_path,
            "message": (
                f"Model trained on {n_total} samples "
                f"(CV accuracy: {best_accuracy:.1%})."
            ),
        }

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, video_data: dict) -> Dict[str, Any]:
        """
        Predict brainrot probability for a video.

        Returns:
        {
            "available": bool,
            "probability": float (0-1),
            "prediction": "brainrot" | "not_brainrot",
            "confidence": float (0-1),
            "ml_score": float (0-100),
            "feature_contributions": dict,
        }
        """
        stub = {
            "available": False,
            "probability": 0.5,
            "prediction": "not_brainrot",
            "confidence": 0.0,
            "ml_score": 0.0,
            "feature_contributions": {},
        }

        if not _SKLEARN_AVAILABLE:
            return stub

        with self._lock:
            pipeline = self._active_pipeline
            trained = self._trained

        if not trained or pipeline is None:
            return stub

        try:
            feat = extract_features(video_data)
            X = np.array([feat], dtype=np.float64)

            proba = pipeline.predict_proba(X)[0]
            # Class order: pipeline was trained on 0=not_brainrot, 1=brainrot
            classes = pipeline.named_steps["clf"].classes_
            brainrot_idx = list(classes).index(1) if 1 in classes else 1
            prob_brainrot = float(proba[brainrot_idx])

            prediction = "brainrot" if prob_brainrot >= 0.5 else "not_brainrot"
            confidence = abs(prob_brainrot - 0.5) * 2.0  # 0 at boundary, 1 at extremes
            ml_score = prob_brainrot * 100.0

            # Feature contributions (dot product of normalised features with coefficients)
            contributions: Dict[str, float] = {}
            clf = pipeline.named_steps["clf"]
            if hasattr(clf, "coef_"):
                scaler = pipeline.named_steps["scaler"]
                feat_scaled = scaler.transform(X)[0]
                coefs = clf.coef_[0]
                for i, name in enumerate(FEATURE_NAMES):
                    if i < len(coefs):
                        contributions[name] = round(float(feat_scaled[i] * coefs[i]), 4)

            return {
                "available": True,
                "probability": round(prob_brainrot, 4),
                "prediction": prediction,
                "confidence": round(confidence, 4),
                "ml_score": round(ml_score, 2),
                "feature_contributions": contributions,
                "feature_values": {
                    FEATURE_NAMES[i]: round(float(feat[i]), 3)
                    for i in range(min(len(feat), N_FEATURES))
                },
            }

        except Exception as exc:
            logger.warning("ML predict error: %s", exc)
            return stub

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return model status for the admin panel."""
        with self._lock:
            return {
                "sklearn_available": _SKLEARN_AVAILABLE,
                "trained": self._trained,
                "accuracy": round(self._accuracy, 4),
                "samples": self._n_samples,
                "n_brainrot": self._n_brainrot,
                "n_not_brainrot": self._n_not_brainrot,
                "trained_at": self._trained_at,
                "model_path": self._model_path,
                "feature_importance": self._feature_importance,
                "overrides_since_last_train": self._overrides_since_train,
                "versions": [str(p.name) for p in _list_model_versions()[:5]],
            }

    def record_new_override(self) -> None:
        """
        Called whenever a new manual override is saved.
        Increments the counter used to trigger auto-retraining.
        """
        with self._lock:
            self._overrides_since_train += 1

    def check_retrain_needed(self) -> bool:
        """
        Return True if retraining should be triggered automatically.

        Triggers when:
        - NEW_OVERRIDES_TRIGGER or more new manual overrides since last train
        - Model is older than STALE_DAYS_TRIGGER days
        - No model exists and sufficient training data is available
        """
        if not _SKLEARN_AVAILABLE:
            return False

        with self._lock:
            overrides = self._overrides_since_train
            trained = self._trained
            trained_at = self._trained_at

        if not trained:
            # Check if we have enough data to start
            _, labels = _load_training_data(self._db_path)
            n_brainrot = sum(1 for label in labels if label == 1)
            n_not = len(labels) - n_brainrot
            return (len(labels) >= MIN_TOTAL_SAMPLES and
                    n_brainrot >= MIN_PER_CLASS and
                    n_not >= MIN_PER_CLASS)

        if overrides >= NEW_OVERRIDES_TRIGGER:
            return True

        if trained_at:
            try:
                dt = datetime.fromisoformat(trained_at.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
                if age_days >= STALE_DAYS_TRIGGER:
                    return True
            except (ValueError, TypeError):
                pass

        return False

    # ------------------------------------------------------------------
    # Model rollback
    # ------------------------------------------------------------------

    def rollback(self, version_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Roll back to a previous model version.

        If *version_name* is None, rolls back to the second-most-recent version.
        Returns a status dict.
        """
        if not _SKLEARN_AVAILABLE:
            return {"success": False, "message": "sklearn not available."}

        versions = _list_model_versions()
        if not versions:
            return {"success": False, "message": "No versioned models available."}

        if version_name:
            target = MODEL_DIR / version_name
            if not target.exists():
                return {"success": False, "message": f"Version {version_name!r} not found."}
        else:
            # Roll back to second-most-recent (index 1)
            if len(versions) < 2:
                return {"success": False, "message": "No older version to roll back to."}
            target = versions[1]

        try:
            data = joblib.load(str(target))
            # Overwrite stable MODEL_PATH
            import shutil
            shutil.copy2(str(target), str(MODEL_PATH))

            with self._lock:
                self._active_pipeline = data.get("pipeline")
                self._accuracy = data.get("accuracy", 0.0)
                self._n_samples = data.get("n_samples", 0)
                self._n_brainrot = data.get("n_brainrot", 0)
                self._n_not_brainrot = data.get("n_not_brainrot", 0)
                self._trained_at = data.get("trained_at")
                self._model_path = str(target)
                self._feature_importance = data.get("feature_importance", {})
                self._trained = True

            return {
                "success": True,
                "message": f"Rolled back to {target.name}.",
                "accuracy": self._accuracy,
                "trained_at": self._trained_at,
            }
        except Exception as exc:
            return {"success": False, "message": f"Rollback failed: {exc}"}

    # ------------------------------------------------------------------
    # Helpers used by the admin panel
    # ------------------------------------------------------------------

    def get_recent_predictions(self, db_path: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Return ML predictions for the most recently-analyzed videos.
        Used by the admin panel predictions table.
        """
        if not _SKLEARN_AVAILABLE or not self._trained:
            return []

        try:
            with _db_conn(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT v.*,
                           c.flagged_percentage AS channel_flagged_percentage,
                           c.subscriber_count   AS channel_subscriber_count
                    FROM videos v
                    LEFT JOIN channels c ON c.channel_id = v.channel_id
                    ORDER BY v.analyzed_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("DB error loading videos for predictions: %s", exc)
            return []

        results = []
        for row in rows:
            d = dict(row)
            for field in ("matched_keywords", "scene_details", "audio_details"):
                val = d.get(field, "")
                if isinstance(val, str):
                    try:
                        d[field] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        d[field] = {} if field != "matched_keywords" else []

            pred = self.predict(d)
            actual_status = d.get("status", "")
            is_override = bool(d.get("manual_override"))

            # Determine if ML prediction matches actual (for overridden videos)
            ml_correct = None
            if is_override and actual_status in ("block", "soft_block", "allow"):
                ml_label = 1 if actual_status in ("block", "soft_block") else 0
                pred_label = 1 if pred.get("prediction") == "brainrot" else 0
                ml_correct = ml_label == pred_label

            results.append({
                "video_id": d.get("video_id"),
                "title": d.get("title", ""),
                "actual_status": actual_status,
                "ml_score": pred.get("ml_score", 0.0),
                "ml_prediction": pred.get("prediction"),
                "ml_confidence": pred.get("confidence", 0.0),
                "ml_probability": pred.get("probability", 0.0),
                "manual_override": is_override,
                "ml_correct": ml_correct,
            })
        return results

    def get_feature_importance_sorted(self) -> List[Dict[str, Any]]:
        """Return feature importance sorted descending for the bar chart."""
        with self._lock:
            fi = dict(self._feature_importance)
        if not fi:
            return [{"name": n, "importance": 0.0} for n in FEATURE_NAMES]
        total = sum(fi.values()) or 1.0
        return sorted(
            [{"name": k, "importance": round(v / total * 100, 2)} for k, v in fi.items()],
            key=lambda x: x["importance"],
            reverse=True,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

classifier = BrainrotClassifier()
