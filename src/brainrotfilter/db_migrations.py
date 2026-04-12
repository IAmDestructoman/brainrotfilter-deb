"""
db_migrations.py - Database schema migration system for BrainrotFilter.

Provides a version-controlled migration system that safely applies incremental
ALTER TABLE / CREATE TABLE changes to an existing SQLite database without data loss.

Usage:
    from db_migrations import run_migrations
    run_migrations()   # idempotent — safe to call on every startup

Each migration is identified by an integer version number.  The current schema
version is stored in the settings table under key "schema_version".  Migrations
are applied in ascending version order; already-applied migrations are skipped.

Migration history:
  Version 1 (baseline): tables created by db_manager.py's initial setup
  Version 2: Add new analyzer columns to videos table (shorts, comment,
             thumbnail, engagement scores + details + is_short flag)
  Version 3: Add profiles and device_assignments tables for family profiles
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Dict, List, Tuple

from config import DB_PATH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Migration registry
# Each migration is a (version, description, list_of_sql_statements) tuple.
# SQL statements are executed in order within a single transaction.
# ---------------------------------------------------------------------------

Migration = Tuple[int, str, List[str]]

# fmt: off
_MIGRATIONS: List[Migration] = [
    # ------------------------------------------------------------------
    # Version 2: New analyzer columns on videos table
    # ------------------------------------------------------------------
    (
        2,
        "Add shorts/comment/thumbnail/engagement columns to videos table",
        [
            # Score columns
            "ALTER TABLE videos ADD COLUMN shorts_score REAL DEFAULT 0",
            "ALTER TABLE videos ADD COLUMN comment_score REAL DEFAULT 0",
            "ALTER TABLE videos ADD COLUMN thumbnail_score REAL DEFAULT 0",
            "ALTER TABLE videos ADD COLUMN engagement_score REAL DEFAULT 0",

            # JSON detail blobs
            "ALTER TABLE videos ADD COLUMN shorts_details TEXT DEFAULT '{}'",
            "ALTER TABLE videos ADD COLUMN comment_details TEXT DEFAULT '{}'",
            "ALTER TABLE videos ADD COLUMN thumbnail_details TEXT DEFAULT '{}'",
            "ALTER TABLE videos ADD COLUMN engagement_details TEXT DEFAULT '{}'",

            # Denormalized boolean for fast Shorts queries
            "ALTER TABLE videos ADD COLUMN is_short BOOLEAN DEFAULT 0",
        ],
    ),

    # ------------------------------------------------------------------
    # Version 3: Family profiles and device assignment tables
    # ------------------------------------------------------------------
    (
        3,
        "Create profiles and device_assignments tables for family profiles",
        [
            """
            CREATE TABLE IF NOT EXISTS profiles (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT    NOT NULL,
                ip_ranges           TEXT    NOT NULL DEFAULT '[]',
                keyword_threshold   REAL    NOT NULL DEFAULT 40,
                scene_threshold     REAL    NOT NULL DEFAULT 50,
                audio_threshold     REAL    NOT NULL DEFAULT 45,
                combined_threshold  REAL    NOT NULL DEFAULT 45,
                block_min           REAL    NOT NULL DEFAULT 55,
                soft_block_min      REAL    NOT NULL DEFAULT 35,
                monitor_min         REAL    NOT NULL DEFAULT 20,
                created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS device_assignments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id   INTEGER NOT NULL,
                ip_address   TEXT    NOT NULL DEFAULT '',
                mac_address  TEXT    NOT NULL DEFAULT '',
                device_name  TEXT    NOT NULL DEFAULT '',
                FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
            )
            """,
            # Index for fast IP lookup during Squid redirection
            "CREATE INDEX IF NOT EXISTS idx_device_ip ON device_assignments(ip_address)",
            "CREATE INDEX IF NOT EXISTS idx_device_profile ON device_assignments(profile_id)",
        ],
    ),

    # ------------------------------------------------------------------
    # Version 4: Wizard CA certificate storage for CA export/download
    # ------------------------------------------------------------------
    (
        4,
        "Create wizard_ca table for CA certificate export",
        [
            """
            CREATE TABLE IF NOT EXISTS wizard_ca (
                id          INTEGER PRIMARY KEY,
                crt         TEXT    NOT NULL,
                refid       TEXT    NOT NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ],
    ),
]
# fmt: on

# ---------------------------------------------------------------------------
# Schema version helpers
# ---------------------------------------------------------------------------

_SCHEMA_VERSION_KEY = "schema_version"


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """
    Read the current schema version from the settings table.

    Returns 1 if the settings table exists but has no version record
    (baseline schema created by db_manager.py initial setup).
    Returns 0 if the settings table does not yet exist at all.
    """
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (_SCHEMA_VERSION_KEY,),
        ).fetchone()
        if row is None:
            return 1  # table exists but no version recorded → baseline
        return int(row[0])
    except sqlite3.OperationalError:
        # settings table does not exist
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Persist the schema version to the settings table."""
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (_SCHEMA_VERSION_KEY, str(version)),
    )


# ---------------------------------------------------------------------------
# Column existence helper (for idempotent ALTER TABLE)
# ---------------------------------------------------------------------------


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if *column* already exists in *table*."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row[1] == column for row in rows)
    except sqlite3.OperationalError:
        return False


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if *table* exists in the database."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Per-statement idempotency wrappers
# ---------------------------------------------------------------------------


def _apply_alter_column(conn: sqlite3.Connection, sql: str) -> None:
    """
    Execute an ALTER TABLE ... ADD COLUMN statement, skipping it gracefully
    if the column already exists (SQLite raises an error on duplicate columns).
    """
    # Parse out table and column names from the SQL
    # Expected form: ALTER TABLE <table> ADD COLUMN <column> ...
    import re
    m = re.match(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
        sql.strip(),
        re.IGNORECASE,
    )
    if m:
        table, column = m.group(1), m.group(2)
        if _column_exists(conn, table, column):
            logger.debug("Column %s.%s already exists, skipping ALTER.", table, column)
            return
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            logger.debug("Duplicate column, skipping: %s", sql)
        else:
            raise


def _apply_statement(conn: sqlite3.Connection, sql: str) -> None:
    """
    Apply a single SQL statement with appropriate idempotency handling.

    - ALTER TABLE … ADD COLUMN: skip if column exists
    - CREATE TABLE IF NOT EXISTS: always safe to re-run
    - CREATE INDEX IF NOT EXISTS: always safe to re-run
    - Other: execute directly
    """
    sql_stripped = sql.strip().upper()

    if sql_stripped.startswith("ALTER TABLE") and "ADD COLUMN" in sql_stripped:
        _apply_alter_column(conn, sql.strip())
    else:
        # CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS are inherently
        # idempotent; all other statements are run as-is.
        conn.execute(sql)


# ---------------------------------------------------------------------------
# Main migration runner
# ---------------------------------------------------------------------------


def run_migrations(db_path: str = DB_PATH) -> int:
    """
    Check the current schema version and apply any pending migrations.

    This function is idempotent: calling it multiple times is safe.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        The final schema version after all migrations are applied.

    Raises:
        RuntimeError: If a migration fails and cannot be rolled back cleanly.
    """
    start = time.monotonic()

    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception as exc:
        logger.error("db_migrations: cannot open database at %s: %s", db_path, exc)
        raise

    try:
        current_version = _get_schema_version(conn)
        logger.info(
            "db_migrations: current schema version = %d, available migrations = %d",
            current_version,
            len(_MIGRATIONS),
        )

        applied = 0

        for version, description, statements in sorted(_MIGRATIONS, key=lambda m: m[0]):
            if version <= current_version:
                logger.debug("Migration v%d already applied, skipping.", version)
                continue

            logger.info("Applying migration v%d: %s", version, description)
            try:
                with conn:  # begins a transaction; auto-commits or rolls back
                    for sql in statements:
                        _apply_statement(conn, sql)
                    _set_schema_version(conn, version)
                current_version = version
                applied += 1
                logger.info("Migration v%d applied successfully.", version)
            except Exception as exc:
                logger.error(
                    "Migration v%d FAILED: %s — database left at version %d.",
                    version,
                    exc,
                    current_version,
                )
                raise RuntimeError(
                    f"Migration v{version} ('{description}') failed: {exc}"
                ) from exc

        elapsed = time.monotonic() - start
        if applied:
            logger.info(
                "db_migrations: %d migration(s) applied, final version = %d (%.2fs)",
                applied,
                current_version,
                elapsed,
            )
        else:
            logger.debug(
                "db_migrations: schema up to date (version %d) in %.2fs",
                current_version,
                elapsed,
            )

        return current_version

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration status inspection
# ---------------------------------------------------------------------------


def get_migration_status(db_path: str = DB_PATH) -> Dict[str, object]:
    """
    Return a summary dict describing the current migration state.

    Useful for admin panel diagnostics.

    Returns:
        {
            "current_version": int,
            "latest_version": int,
            "pending_count": int,
            "pending_versions": [int, ...],
            "is_up_to_date": bool,
        }
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        current_version = _get_schema_version(conn)
        conn.close()
    except Exception as exc:
        logger.warning("get_migration_status: %s", exc)
        current_version = 0

    latest_version = max((m[0] for m in _MIGRATIONS), default=1)
    pending = [m[0] for m in _MIGRATIONS if m[0] > current_version]

    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "pending_count": len(pending),
        "pending_versions": pending,
        "is_up_to_date": current_version >= latest_version,
    }


# ---------------------------------------------------------------------------
# CLI entry point (for manual runs / install scripts)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    logger.info("Running migrations on: %s", db)
    final_version = run_migrations(db)
    status = get_migration_status(db)
    print(json.dumps(status, indent=2))
    sys.exit(0 if status["is_up_to_date"] else 1)
