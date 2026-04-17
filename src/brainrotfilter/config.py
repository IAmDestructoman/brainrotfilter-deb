"""
config.py - Configuration management for BrainrotFilter (Linux/Debian).

Loads settings from the SQLite database settings table with sensible defaults.
All thresholds and parameters are configurable at runtime without restart.

Linux paths (overridable via environment variables):
  DB:       /var/lib/brainrotfilter/brainrotfilter.db
  Keywords: /etc/brainrotfilter/keywords.json
  ACLs:     /etc/brainrotfilter/*.acl
  Logs:     /var/log/brainrotfilter/
"""

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Linux-native paths (overridable via environment variables for dev/Docker).
DB_PATH = os.environ.get(
    "BRAINROT_DB_PATH",
    "/var/lib/brainrotfilter/brainrotfilter.db",
)
KEYWORDS_PATH = os.environ.get(
    "BRAINROT_KEYWORDS_PATH",
    "/etc/brainrotfilter/keywords.json",
)
BLOCKED_VIDEOS_ACL = os.environ.get(
    "BRAINROT_BLOCKED_VIDEOS_ACL",
    "/etc/brainrotfilter/blocked_videos.acl",
)
BLOCKED_CHANNELS_ACL = os.environ.get(
    "BRAINROT_BLOCKED_CHANNELS_ACL",
    "/etc/brainrotfilter/blocked_channels.acl",
)

# Default configuration values
DEFAULTS: Dict[str, Any] = {
    # Analysis thresholds (0-100 scale)
    "keyword_threshold": 40,
    "scene_threshold": 50,
    "audio_threshold": 45,
    "combined_threshold": 45,

    # Score tier boundaries
    "monitor_score_min": 20,
    "soft_block_score_min": 35,
    "block_score_min": 55,

    # Combined score weights (must sum to 1.0 across the active analyzers)
    "weight_keyword": 0.25,
    "weight_scene": 0.20,
    "weight_audio": 0.15,
    "weight_comment": 0.15,
    "weight_engagement": 0.10,
    "weight_thumbnail": 0.10,

    # Shorts detection bonus (additive, not weighted)
    "shorts_bonus_confirmed": 15,
    "shorts_bonus_likely": 10,

    # ML scorer
    "weight_ml": 0.05,
    "ml_enabled": False,

    # Community keyword sync
    "community_keywords_enabled": False,
    "community_keywords_url": (
        "https://raw.githubusercontent.com/IAmDestructoman/brainrotfilter-deb"
        "/main/community-keywords.json"
    ),
    "community_keywords_branch": "main",
    "community_keywords_strategy": "additive",
    "community_keywords_auto_update": False,
    "community_keywords_interval_hours": 24,
    "community_keywords_last_check": "",
    "community_keywords_last_hash": "",

    # Channel auto-escalation threshold (% of flagged videos)
    "channel_flag_percentage": 30,
    "auto_escalation": True,

    # Video scanning durations (seconds)
    "initial_scan_duration": 45,
    "full_scan_time_limit": 120,

    # Scene detection sensitivity
    "scene_content_threshold": 27.0,
    "music_video_dampening": 0.6,

    # Audio analysis
    "audio_loudness_weight": 0.4,
    "audio_chaos_weight": 0.35,
    "audio_nlp_weight": 0.25,

    # YouTube API removed in 1.1.0 — classification runs on-box from
    # keyword / OCR / audio / optional LLM signals and no longer queries
    # the YouTube Data API. Leaving the empty key here would let old
    # callers silently hit the API; nothing reads it now.

    # Service configuration
    "service_host": os.environ.get("SERVICE_HOST", "0.0.0.0"),
    "service_port": int(os.environ.get("SERVICE_PORT", "8199")),
    "gateway_ip": os.environ.get("GATEWAY_IP", ""),
    "analyzer_service_url": os.environ.get(
        "ANALYZER_SERVICE_URL", "http://127.0.0.1:8199"
    ),

    # Analysis workers
    "analysis_worker_threads": int(os.environ.get("ANALYSIS_WORKER_THREADS", "4")),
    "analysis_queue_maxsize": int(os.environ.get("ANALYSIS_QUEUE_MAXSIZE", "100")),

    # Squid ACL cache TTL (seconds)
    "acl_cache_ttl": 300,

    # Request logging
    "log_all_requests": True,
    "log_retention_days": 30,

    # Vosk model path
    "vosk_model_path": os.environ.get(
        "VOSK_MODEL_PATH",
        "/usr/share/vosk/model-en-us-small",
    ),

    # Squid integration
    "squid_port": 3128,
    "squid_redirector_concurrency": 1,
    "squid_acl_concurrency": 10,

    # Parallel analysis pipeline timeouts (seconds)
    "parallel_analysis_timeout": 180,
    "phase1_timeout": 30,
    "phase2_timeout": 150,
}


class Config:
    """
    Configuration manager that loads settings from the SQLite database
    and falls back to built-in defaults when settings are missing.

    Thread-safe for reads. Reload with refresh() to pick up DB changes.
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        self._cache: Dict[str, Any] = {}
        self._loaded = False
        self.load()

    # ------------------------------------------------------------------
    # Loading & Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load all settings from the database, merging with defaults."""
        settings: Dict[str, Any] = dict(DEFAULTS)

        if not Path(self.db_path).exists():
            logger.warning(
                "Database not found at %s; using defaults only.", self.db_path
            )
            self._cache = settings
            self._loaded = True
            return

        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                cursor = conn.execute("SELECT key, value FROM settings")
                for key, raw_value in cursor.fetchall():
                    try:
                        settings[key] = json.loads(raw_value)
                    except (json.JSONDecodeError, TypeError):
                        settings[key] = raw_value
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.error("Failed to load settings from DB: %s", exc)

        self._cache = settings
        self._loaded = True
        logger.debug("Config loaded (%d keys).", len(self._cache))

    def refresh(self) -> None:
        """Reload configuration from the database (thread-safe read)."""
        self.load()

    def save(self, key: str, value: Any) -> None:
        """Persist a single setting to the database."""
        if not Path(self.db_path).exists():
            logger.error("Cannot save setting; database not found at %s.", self.db_path)
            return

        serialized = json.dumps(value)
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            try:
                conn.execute(
                    """
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                                   updated_at=excluded.updated_at
                    """,
                    (key, serialized),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.error("Failed to save setting %s: %s", key, exc)

        self._cache[key] = value

    def save_many(self, updates: Dict[str, Any]) -> None:
        """Persist multiple settings at once."""
        for key, value in updates.items():
            self.save(key, value)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        return self._cache.get(key, default)

    def get_int(self, key: str) -> int:
        return int(self._cache.get(key, DEFAULTS.get(key, 0)))

    def get_float(self, key: str) -> float:
        return float(self._cache.get(key, DEFAULTS.get(key, 0.0)))

    def get_bool(self, key: str) -> bool:
        val = self._cache.get(key, DEFAULTS.get(key, False))
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val)

    def get_str(self, key: str) -> str:
        return str(self._cache.get(key, DEFAULTS.get(key, "")))

    def all(self) -> Dict[str, Any]:
        return dict(self._cache)

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def keyword_threshold(self) -> int:
        return self.get_int("keyword_threshold")

    @property
    def scene_threshold(self) -> int:
        return self.get_int("scene_threshold")

    @property
    def audio_threshold(self) -> int:
        return self.get_int("audio_threshold")

    @property
    def combined_threshold(self) -> int:
        return self.get_int("combined_threshold")

    @property
    def monitor_score_min(self) -> int:
        return self.get_int("monitor_score_min")

    @property
    def soft_block_score_min(self) -> int:
        return self.get_int("soft_block_score_min")

    @property
    def block_score_min(self) -> int:
        return self.get_int("block_score_min")

    @property
    def weights(self) -> Dict[str, float]:
        return {
            "keyword": self.get_float("weight_keyword"),
            "scene": self.get_float("weight_scene"),
            "audio": self.get_float("weight_audio"),
            "comment": self.get_float("weight_comment"),
            "engagement": self.get_float("weight_engagement"),
            "thumbnail": self.get_float("weight_thumbnail"),
            "ml": self.get_float("weight_ml"),
        }

    @property
    def keyword_weight(self) -> float:
        return self.get_float("weight_keyword")

    @property
    def scene_weight(self) -> float:
        return self.get_float("weight_scene")

    @property
    def audio_weight(self) -> float:
        return self.get_float("weight_audio")

    @property
    def comment_weight(self) -> float:
        return self.get_float("weight_comment")

    @property
    def engagement_weight(self) -> float:
        return self.get_float("weight_engagement")

    @property
    def thumbnail_weight(self) -> float:
        return self.get_float("weight_thumbnail")

    @property
    def ml_weight(self) -> float:
        return self.get_float("weight_ml")

    @property
    def ml_enabled(self) -> bool:
        return self.get_bool("ml_enabled")

    @property
    def shorts_bonus_confirmed(self) -> int:
        return self.get_int("shorts_bonus_confirmed")

    @property
    def shorts_bonus_likely(self) -> int:
        return self.get_int("shorts_bonus_likely")

    @property
    def channel_flag_percentage(self) -> int:
        return self.get_int("channel_flag_percentage")

    @property
    def initial_scan_duration(self) -> int:
        return self.get_int("initial_scan_duration")

    @property
    def full_scan_time_limit(self) -> int:
        return self.get_int("full_scan_time_limit")

    @property
    def youtube_api_key(self) -> str:
        # Kept as a no-op getter so any lingering caller still compiles
        # (returns empty). YouTube Data API integration was dropped in
        # 1.1.0; see wizard removal + analyzer_service API route removal.
        return ""

    @property
    def service_host(self) -> str:
        """Return the host IP for redirect URLs (gateway or service host)."""
        return self.get_str("gateway_ip") or self.get_str("service_host") or "127.0.0.1"

    @property
    def analyzer_service_url(self) -> str:
        return self.get_str("analyzer_service_url")

    @property
    def service_port(self) -> int:
        return self.get_int("service_port")

    @property
    def vosk_model_path(self) -> str:
        return self.get_str("vosk_model_path")

    @property
    def acl_cache_ttl(self) -> int:
        return self.get_int("acl_cache_ttl")

    # ------------------------------------------------------------------
    # Score tier helpers
    # ------------------------------------------------------------------

    def score_to_status(self, score: float) -> str:
        if score >= self.block_score_min:
            return "block"
        if score >= self.soft_block_score_min:
            return "soft_block"
        if score >= self.monitor_score_min:
            return "monitor"
        return "allow"

    def compute_combined_score(
        self,
        keyword: float = 0,
        scene: float = 0,
        audio: float = 0,
        comment: float = 0,
        engagement: float = 0,
        thumbnail: float = 0,
        shorts_bonus: float = 0,
        ml: float = 0,
        keyword_score: Optional[float] = None,
        scene_score: Optional[float] = None,
        audio_score: Optional[float] = None,
    ) -> float:
        if keyword_score is not None:
            keyword = float(keyword_score)
        if scene_score is not None:
            scene = float(scene_score)
        if audio_score is not None:
            audio = float(audio_score)

        base = (
            float(keyword) * self.get_float("weight_keyword")
            + float(scene) * self.get_float("weight_scene")
            + float(audio) * self.get_float("weight_audio")
            + float(comment) * self.get_float("weight_comment")
            + float(engagement) * self.get_float("weight_engagement")
            + float(thumbnail) * self.get_float("weight_thumbnail")
        )

        ml_weight = self.get_float("weight_ml")
        ml_enabled = self.get_bool("ml_enabled")
        if ml_enabled and ml_weight > 0:
            base = base * (1.0 - ml_weight) + float(ml) * ml_weight

        return round(min(max(base + float(shorts_bonus), 0.0), 100.0), 2)


# Module-level singleton
config = Config()
