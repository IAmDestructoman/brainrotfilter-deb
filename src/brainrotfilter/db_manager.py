"""
db_manager.py - SQLite database manager for BrainrotFilter.

Database location: /usr/local/etc/brainrotfilter/brainrotfilter.db

Tables:
  videos    - Per-video analysis records
  channels  - Channel-level profiles
  requests  - Request log (all Squid hits)
  whitelist - Whitelisted videos/channels
  settings  - Key-value configuration store

All public methods are synchronous and thread-safe via the connection-per-call
pattern (SQLite WAL mode).  An async wrapper (aiosqlite) is provided for use
inside FastAPI route handlers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from config import DB_PATH
from models import (
    ChannelProfile,
    DashboardStats,
    RequestLog,
    VideoAnalysis,
    WhitelistEntry,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS videos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id          TEXT NOT NULL UNIQUE,
    channel_id        TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    thumbnail_url     TEXT NOT NULL DEFAULT '',
    keyword_score     REAL NOT NULL DEFAULT 0,
    scene_score       REAL NOT NULL DEFAULT 0,
    audio_score       REAL NOT NULL DEFAULT 0,
    combined_score    REAL NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('allow','monitor','soft_block','block','pending')),
    matched_keywords  TEXT NOT NULL DEFAULT '[]',   -- JSON array
    scene_details     TEXT NOT NULL DEFAULT '{}',   -- JSON object
    audio_details     TEXT NOT NULL DEFAULT '{}',   -- JSON object
    analyzed_at       TEXT,
    updated_at        TEXT,
    manual_override   INTEGER NOT NULL DEFAULT 0,
    override_by       TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
CREATE INDEX IF NOT EXISTS idx_videos_status  ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_score   ON videos(combined_score);

CREATE TABLE IF NOT EXISTS channels (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id        TEXT NOT NULL UNIQUE,
    channel_name      TEXT NOT NULL DEFAULT '',
    subscriber_count  INTEGER NOT NULL DEFAULT 0,
    total_videos      INTEGER NOT NULL DEFAULT 0,
    videos_analyzed   INTEGER NOT NULL DEFAULT 0,
    videos_flagged    INTEGER NOT NULL DEFAULT 0,
    flagged_percentage REAL NOT NULL DEFAULT 0,
    avg_video_length  REAL NOT NULL DEFAULT 0,
    upload_frequency  REAL NOT NULL DEFAULT 0,
    tier              TEXT NOT NULL DEFAULT 'allow'
                        CHECK(tier IN ('allow','monitor','soft_block','block')),
    auto_escalated    INTEGER NOT NULL DEFAULT 0,
    last_analyzed     TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_channels_tier ON channels(tier);

CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_ip    TEXT NOT NULL,
    video_id     TEXT NOT NULL DEFAULT '',
    channel_id   TEXT NOT NULL DEFAULT '',
    timestamp    TEXT NOT NULL DEFAULT (datetime('now')),
    action_taken TEXT NOT NULL DEFAULT 'allow'
                   CHECK(action_taken IN ('allow','monitor','soft_block','block','pending')),
    scores       TEXT NOT NULL DEFAULT '{}',   -- JSON
    user_agent   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_requests_timestamp  ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_client_ip  ON requests(client_ip);
CREATE INDEX IF NOT EXISTS idx_requests_video_id   ON requests(video_id);

CREATE TABLE IF NOT EXISTS whitelist (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL CHECK(type IN ('video','channel')),
    target_id  TEXT NOT NULL,
    added_by   TEXT NOT NULL DEFAULT 'admin',
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(type, target_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


@contextmanager
def _get_conn(db_path: str = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a configured SQLite connection."""
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DatabaseManager
# ---------------------------------------------------------------------------


class DatabaseManager:
    """
    Central database access layer.

    Usage::

        db = DatabaseManager()
        db.initialize()          # once at startup
        video = db.get_video("dQw4w9WgXcQ")
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Create DB directory and apply the schema (idempotent)."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with _get_conn(self.db_path) as conn:
            conn.executescript(_SCHEMA_SQL)
        logger.info("Database initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return dict(row)

    def _now(self) -> str:
        return datetime.utcnow().isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    # Videos
    # ------------------------------------------------------------------

    def upsert_video(self, video: VideoAnalysis) -> None:
        """Insert or update a video analysis record."""
        matched_kw_json = json.dumps(
            [kw.model_dump() for kw in video.matched_keywords]
        )
        scene_json = json.dumps(
            video.scene_details.model_dump() if video.scene_details else {}
        )
        audio_json = json.dumps(
            video.audio_details.model_dump() if video.audio_details else {}
        )
        now = self._now()

        with _get_conn(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO videos (
                    video_id, channel_id, title, description, thumbnail_url,
                    keyword_score, scene_score, audio_score, combined_score, status,
                    matched_keywords, scene_details, audio_details,
                    analyzed_at, updated_at, manual_override, override_by
                ) VALUES (
                    :video_id, :channel_id, :title, :description, :thumbnail_url,
                    :keyword_score, :scene_score, :audio_score, :combined_score, :status,
                    :matched_keywords, :scene_details, :audio_details,
                    :analyzed_at, :updated_at, :manual_override, :override_by
                )
                ON CONFLICT(video_id) DO UPDATE SET
                    channel_id       = excluded.channel_id,
                    title            = excluded.title,
                    description      = excluded.description,
                    thumbnail_url    = excluded.thumbnail_url,
                    keyword_score    = excluded.keyword_score,
                    scene_score      = excluded.scene_score,
                    audio_score      = excluded.audio_score,
                    combined_score   = excluded.combined_score,
                    status           = CASE WHEN videos.manual_override = 1
                                           THEN videos.status
                                           ELSE excluded.status END,
                    matched_keywords = excluded.matched_keywords,
                    scene_details    = excluded.scene_details,
                    audio_details    = excluded.audio_details,
                    analyzed_at      = excluded.analyzed_at,
                    updated_at       = excluded.updated_at
                """,
                {
                    "video_id": video.video_id,
                    "channel_id": video.channel_id,
                    "title": video.title,
                    "description": video.description,
                    "thumbnail_url": video.thumbnail_url,
                    "keyword_score": video.keyword_score,
                    "scene_score": video.scene_score,
                    "audio_score": video.audio_score,
                    "combined_score": video.combined_score,
                    "status": video.status if isinstance(video.status, str) else video.status.value,
                    "matched_keywords": matched_kw_json,
                    "scene_details": scene_json,
                    "audio_details": audio_json,
                    "analyzed_at": video.analyzed_at.isoformat() if video.analyzed_at else now,
                    "updated_at": now,
                    "manual_override": int(video.manual_override),
                    "override_by": video.override_by,
                },
            )

    def get_video(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single video record by video_id."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return self._row_to_dict(row)

    def get_videos(
        self,
        status: Optional[str] = None,
        channel_id: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
        order_by: str = "updated_at",
        order_dir: str = "DESC",
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Return a paginated list of video records with optional filtering.

        Returns (items, total_count).
        """
        where_clauses: List[str] = []
        params: List[Any] = []

        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if channel_id:
            where_clauses.append("channel_id = ?")
            params.append(channel_id)
        if search:
            where_clauses.append("(title LIKE ? OR video_id LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        safe_order_by = order_by if order_by in (
            "updated_at", "analyzed_at", "combined_score", "keyword_score",
            "scene_score", "audio_score", "title",
        ) else "updated_at"
        safe_dir = "DESC" if order_dir.upper() == "DESC" else "ASC"
        offset = (page - 1) * per_page

        with _get_conn(self.db_path) as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM videos {where_sql}", params
            ).fetchone()
            total = total_row[0] if total_row else 0

            rows = conn.execute(
                f"""
                SELECT * FROM videos {where_sql}
                ORDER BY {safe_order_by} {safe_dir}
                LIMIT ? OFFSET ?
                """,
                params + [per_page, offset],
            ).fetchall()

        return [dict(r) for r in rows], total

    def set_video_status(
        self,
        video_id: str,
        status: str,
        manual_override: bool = False,
        override_by: Optional[str] = None,
    ) -> None:
        """Update the status of a video (optionally marking as manual override)."""
        with _get_conn(self.db_path) as conn:
            conn.execute(
                """
                UPDATE videos
                SET status = ?, manual_override = ?, override_by = ?, updated_at = ?
                WHERE video_id = ?
                """,
                (status, int(manual_override), override_by, self._now(), video_id),
            )

    def get_video_status(self, video_id: str) -> Optional[str]:
        """Return just the status string for a video, or None if unknown."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
        return row["status"] if row else None

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def upsert_channel(self, channel: ChannelProfile) -> None:
        """Insert or update a channel record."""
        now = self._now()
        with _get_conn(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO channels (
                    channel_id, channel_name, subscriber_count, total_videos,
                    videos_analyzed, videos_flagged, flagged_percentage,
                    avg_video_length, upload_frequency, tier, auto_escalated,
                    last_analyzed, created_at, updated_at
                ) VALUES (
                    :channel_id, :channel_name, :subscriber_count, :total_videos,
                    :videos_analyzed, :videos_flagged, :flagged_percentage,
                    :avg_video_length, :upload_frequency, :tier, :auto_escalated,
                    :last_analyzed, :created_at, :updated_at
                )
                ON CONFLICT(channel_id) DO UPDATE SET
                    channel_name      = excluded.channel_name,
                    subscriber_count  = excluded.subscriber_count,
                    total_videos      = excluded.total_videos,
                    videos_analyzed   = excluded.videos_analyzed,
                    videos_flagged    = excluded.videos_flagged,
                    flagged_percentage= excluded.flagged_percentage,
                    avg_video_length  = excluded.avg_video_length,
                    upload_frequency  = excluded.upload_frequency,
                    tier              = excluded.tier,
                    auto_escalated    = excluded.auto_escalated,
                    last_analyzed     = excluded.last_analyzed,
                    updated_at        = excluded.updated_at
                """,
                {
                    "channel_id": channel.channel_id,
                    "channel_name": channel.channel_name,
                    "subscriber_count": channel.subscriber_count,
                    "total_videos": channel.total_videos,
                    "videos_analyzed": channel.videos_analyzed,
                    "videos_flagged": channel.videos_flagged,
                    "flagged_percentage": channel.flagged_percentage,
                    "avg_video_length": channel.avg_video_length,
                    "upload_frequency": channel.upload_frequency,
                    "tier": channel.tier if isinstance(channel.tier, str) else channel.tier.value,
                    "auto_escalated": int(channel.auto_escalated),
                    "last_analyzed": channel.last_analyzed.isoformat() if channel.last_analyzed else now,
                    "created_at": channel.created_at.isoformat() if channel.created_at else now,
                    "updated_at": now,
                },
            )

    def get_channel(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single channel record."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        return self._row_to_dict(row)

    def get_channels(
        self,
        tier: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return a paginated, optionally filtered list of channel records."""
        where_clauses: List[str] = []
        params: List[Any] = []

        if tier:
            where_clauses.append("tier = ?")
            params.append(tier)
        if search:
            where_clauses.append("(channel_name LIKE ? OR channel_id LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        offset = (page - 1) * per_page

        with _get_conn(self.db_path) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM channels {where_sql}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT * FROM channels {where_sql}
                ORDER BY flagged_percentage DESC, updated_at DESC
                LIMIT ? OFFSET ?
                """,
                params + [per_page, offset],
            ).fetchall()

        return [dict(r) for r in rows], total

    def get_channel_tier(self, channel_id: str) -> Optional[str]:
        """Return just the tier for a channel, or None if unknown."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT tier FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        return row["tier"] if row else None

    def set_channel_tier(
        self, channel_id: str, tier: str, auto_escalated: bool = False
    ) -> None:
        """Update the tier of a channel."""
        with _get_conn(self.db_path) as conn:
            conn.execute(
                """
                UPDATE channels
                SET tier = ?, auto_escalated = ?, updated_at = ?
                WHERE channel_id = ?
                """,
                (tier, int(auto_escalated), self._now(), channel_id),
            )

    def get_channel_videos(
        self, channel_id: str
    ) -> List[Dict[str, Any]]:
        """Return all analyzed video records for a given channel."""
        with _get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE channel_id = ? ORDER BY analyzed_at DESC",
                (channel_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Request logs
    # ------------------------------------------------------------------

    def log_request(self, log: RequestLog) -> None:
        """Insert a request log entry."""
        scores_json = json.dumps(log.scores or {})
        action = log.action_taken if isinstance(log.action_taken, str) else log.action_taken.value
        with _get_conn(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO requests
                    (client_ip, video_id, channel_id, timestamp, action_taken, scores, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log.client_ip,
                    log.video_id,
                    log.channel_id,
                    log.timestamp.isoformat(timespec="seconds"),
                    action,
                    scores_json,
                    log.user_agent,
                ),
            )

    def get_logs(
        self,
        client_ip: Optional[str] = None,
        video_id: Optional[str] = None,
        action: Optional[str] = None,
        page: int = 1,
        per_page: int = 100,
        since_hours: int = 24,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return a paginated list of request log entries."""
        since = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat()
        where_clauses = ["timestamp >= ?"]
        params: List[Any] = [since]

        if client_ip:
            where_clauses.append("client_ip = ?")
            params.append(client_ip)
        if video_id:
            where_clauses.append("video_id = ?")
            params.append(video_id)
        if action:
            where_clauses.append("action_taken = ?")
            params.append(action)

        where_sql = "WHERE " + " AND ".join(where_clauses)
        offset = (page - 1) * per_page

        with _get_conn(self.db_path) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM requests {where_sql}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT * FROM requests {where_sql}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                params + [per_page, offset],
            ).fetchall()

        return [dict(r) for r in rows], total

    def purge_old_logs(self, retention_days: int = 30) -> int:
        """Delete request logs older than *retention_days*."""
        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
        with _get_conn(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM requests WHERE timestamp < ?", (cutoff,)
            )
        deleted = cur.rowcount
        logger.info("Purged %d old log entries.", deleted)
        return deleted

    # ------------------------------------------------------------------
    # Whitelist
    # ------------------------------------------------------------------

    def add_whitelist(self, entry: WhitelistEntry) -> None:
        """Add an entry to the whitelist (upsert on type+target_id)."""
        type_val = entry.type if isinstance(entry.type, str) else entry.type.value
        now = self._now()
        with _get_conn(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO whitelist (type, target_id, added_by, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(type, target_id) DO UPDATE SET
                    added_by   = excluded.added_by,
                    reason     = excluded.reason,
                    created_at = excluded.created_at
                """,
                (type_val, entry.target_id, entry.added_by, entry.reason, now),
            )

    def remove_whitelist(self, entry_id: int) -> bool:
        """Remove a whitelist entry by primary key. Returns True if found."""
        with _get_conn(self.db_path) as conn:
            cur = conn.execute("DELETE FROM whitelist WHERE id = ?", (entry_id,))
        return cur.rowcount > 0

    def is_whitelisted(self, target_id: str, wl_type: str) -> bool:
        """Check if a video or channel is on the whitelist."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM whitelist WHERE type = ? AND target_id = ?",
                (wl_type, target_id),
            ).fetchone()
        return row is not None

    def get_whitelist(self, page: int = 1, per_page: int = 50) -> Tuple[List[Dict[str, Any]], int]:
        """Return a paginated list of whitelist entries."""
        offset = (page - 1) * per_page
        with _get_conn(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM whitelist").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM whitelist ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (per_page, offset),
            ).fetchall()
        return [dict(r) for r in rows], total

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        """Return the raw string value for a settings key, or None."""
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: Any) -> None:
        """Persist a settings value (serialized as JSON)."""
        serialized = json.dumps(value)
        now = self._now()
        with _get_conn(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, serialized, now),
            )

    def get_all_settings(self) -> Dict[str, Any]:
        """Return all settings as a dict (values parsed from JSON)."""
        with _get_conn(self.db_path) as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        result: Dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                result[row["key"]] = row["value"]
        return result

    # ------------------------------------------------------------------
    # Statistics / aggregation
    # ------------------------------------------------------------------

    def get_dashboard_stats(self) -> DashboardStats:
        """Aggregate statistics for the dashboard."""
        today = datetime.utcnow().date().isoformat()

        with _get_conn(self.db_path) as conn:
            # Video counts per status
            status_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM videos GROUP BY status"
            ).fetchall()
            status_counts: Dict[str, int] = {r["status"]: r["cnt"] for r in status_rows}

            # Channel counts
            total_channels = conn.execute(
                "SELECT COUNT(*) FROM channels"
            ).fetchone()[0]
            blocked_channels = conn.execute(
                "SELECT COUNT(*) FROM channels WHERE tier = 'block'"
            ).fetchone()[0]

            # Today's request counts
            today_total = conn.execute(
                "SELECT COUNT(*) FROM requests WHERE timestamp >= ?", (today,)
            ).fetchone()[0]
            today_blocked = conn.execute(
                """
                SELECT COUNT(*) FROM requests
                WHERE timestamp >= ? AND action_taken IN ('block','soft_block')
                """,
                (today,),
            ).fetchone()[0]

            # Average score
            avg_row = conn.execute(
                "SELECT AVG(combined_score) FROM videos WHERE status != 'pending'"
            ).fetchone()
            avg_score = round(avg_row[0] or 0, 2)

            # Top matched keywords (parse from JSON)
            kw_rows = conn.execute(
                """
                SELECT matched_keywords FROM videos
                WHERE status IN ('block','soft_block')
                ORDER BY updated_at DESC LIMIT 200
                """
            ).fetchall()
            kw_counts: Dict[str, int] = {}
            for row in kw_rows:
                try:
                    kws = json.loads(row["matched_keywords"])
                    for kw in kws:
                        word = kw.get("keyword", "")
                        kw_counts[word] = kw_counts.get(word, 0) + 1
                except Exception:
                    pass
            top_kws = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)[:10]

            # Recent blocks
            recent = conn.execute(
                """
                SELECT video_id, title, combined_score, status, updated_at
                FROM videos
                WHERE status IN ('block','soft_block')
                ORDER BY updated_at DESC LIMIT 10
                """
            ).fetchall()

        return DashboardStats(
            total_videos_analyzed=sum(status_counts.values()),
            total_videos_blocked=status_counts.get("block", 0),
            total_videos_soft_blocked=status_counts.get("soft_block", 0),
            total_videos_monitored=status_counts.get("monitor", 0),
            total_videos_allowed=status_counts.get("allow", 0),
            total_channels_profiled=total_channels,
            total_channels_blocked=blocked_channels,
            total_requests_today=today_total,
            total_requests_blocked_today=today_blocked,
            avg_combined_score=avg_score,
            top_matched_keywords=[{"keyword": k, "count": c} for k, c in top_kws],
            recent_blocks=[dict(r) for r in recent],
        )

    def get_blocked_video_ids(self) -> List[str]:
        """Return all video IDs with status 'block' or 'soft_block'."""
        with _get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT video_id FROM videos WHERE status IN ('block','soft_block')"
            ).fetchall()
        return [r["video_id"] for r in rows]

    def get_blocked_channel_ids(self) -> List[str]:
        """Return all channel IDs with tier 'block' or 'soft_block'."""
        with _get_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT channel_id FROM channels WHERE tier IN ('block','soft_block')"
            ).fetchall()
        return [r["channel_id"] for r in rows]

    def get_requests_today_count_by_ip(self) -> Dict[str, int]:
        """Count today's requests grouped by client IP (for abuse detection)."""
        today = datetime.utcnow().date().isoformat()
        with _get_conn(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT client_ip, COUNT(*) as cnt
                FROM requests WHERE timestamp >= ?
                GROUP BY client_ip ORDER BY cnt DESC
                """,
                (today,),
            ).fetchall()
        return {r["client_ip"]: r["cnt"] for r in rows}

    def get_channel_flagged_stats(
        self, channel_id: str
    ) -> Dict[str, Any]:
        """
        Compute real-time flagged stats for a channel from the videos table.
        Returns: {'videos_analyzed': int, 'videos_flagged': int, 'flagged_percentage': float}
        """
        with _get_conn(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('block','soft_block') THEN 1 ELSE 0 END) AS flagged
                FROM videos
                WHERE channel_id = ? AND status != 'pending'
                """,
                (channel_id,),
            ).fetchone()
        total = row["total"] or 0
        flagged = row["flagged"] or 0
        pct = round((flagged / total) * 100, 2) if total > 0 else 0.0
        return {
            "videos_analyzed": total,
            "videos_flagged": flagged,
            "flagged_percentage": pct,
        }

    def get_recent_clients_for_video(
        self, video_id: str, max_age_seconds: int = 300
    ) -> List[str]:
        """
        Return distinct client IPs that requested *video_id* within the last
        *max_age_seconds*.  Used by the state killer to find active viewers.
        """
        cutoff = (
            datetime.utcnow()
            - __import__("datetime").timedelta(seconds=max_age_seconds)
        ).isoformat()
        with _get_conn(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT client_ip
                FROM requests
                WHERE video_id = ? AND timestamp >= ?
                """,
                (video_id, cutoff),
            ).fetchall()
        return [r["client_ip"] for r in rows]


# Module-level singleton
db = DatabaseManager()
