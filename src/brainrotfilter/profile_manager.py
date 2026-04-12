"""
profile_manager.py - Multi-network family profile management for BrainrotFilter.

Provides per-device filtering thresholds and response modes so different
network segments (children's tablet, teenager's laptop, adult's workstation)
can have appropriately calibrated filtering without touching global settings.

Integration: The Squid redirector calls get_effective_thresholds(client_ip)
before every filtering decision; the decision engine uses those per-profile
thresholds instead of the global defaults.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Tuple

from config import DB_PATH, config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in preset profiles
# ---------------------------------------------------------------------------

PRESET_PROFILES: Dict[str, Dict[str, Any]] = {
    "children": {
        "name": "Children (Ages 5-12)",
        "thresholds": {
            "combined_threshold": 25,
            "block_min": 35,
            "soft_block_min": 25,
            "monitor_min": 10,
            "keyword_threshold": 20,
            "scene_threshold": 25,
            "audio_threshold": 25,
            "keyword_weight": 0.45,
            "scene_weight": 0.35,
            "audio_weight": 0.20,
        },
        "response_mode": "strict",
        "description": (
            "Aggressive blocking for young children. Very low thresholds "
            "with strict response mode — anything mildly flagged is blocked."
        ),
    },
    "teens": {
        "name": "Teens (Ages 13-17)",
        "thresholds": {
            "combined_threshold": 40,
            "block_min": 50,
            "soft_block_min": 35,
            "monitor_min": 20,
            "keyword_threshold": 35,
            "scene_threshold": 40,
            "audio_threshold": 38,
            "keyword_weight": 0.40,
            "scene_weight": 0.35,
            "audio_weight": 0.25,
        },
        "response_mode": "standard",
        "description": (
            "Balanced filtering for teenagers. Standard thresholds with "
            "soft-block warnings before hard blocks."
        ),
    },
    "adults": {
        "name": "Adults",
        "thresholds": {
            "combined_threshold": 65,
            "block_min": 75,
            "soft_block_min": 60,
            "monitor_min": 40,
            "keyword_threshold": 60,
            "scene_threshold": 65,
            "audio_threshold": 60,
            "keyword_weight": 0.40,
            "scene_weight": 0.35,
            "audio_weight": 0.25,
        },
        "response_mode": "standard",
        "description": (
            "Permissive thresholds for adults. Only extreme content is blocked; "
            "most borderline videos are monitored or warned."
        ),
    },
    "unrestricted": {
        "name": "Unrestricted (Monitor Only)",
        "thresholds": {},
        "response_mode": "monitor_only",
        "description": (
            "No blocking at all. All requests are logged for visibility "
            "but nothing is ever redirected or blocked."
        ),
    },
    "guest": {
        "name": "Guest Network",
        "thresholds": {
            "combined_threshold": 45,
            "block_min": 55,
            "soft_block_min": 40,
            "monitor_min": 25,
        },
        "response_mode": "standard",
        "description": (
            "Moderate filtering for guest devices. Global defaults apply "
            "unless overridden below."
        ),
    },
}


# ---------------------------------------------------------------------------
# Schema DDL for profile tables
# ---------------------------------------------------------------------------

_PROFILE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    ip_ranges       TEXT    NOT NULL DEFAULT '[]',   -- JSON array of IP ranges / single IPs
    mac_addresses   TEXT    NOT NULL DEFAULT '[]',   -- JSON array of MAC addresses
    thresholds      TEXT    NOT NULL DEFAULT '{}',   -- JSON object of threshold overrides
    response_mode   TEXT    NOT NULL DEFAULT 'standard'
                        CHECK(response_mode IN ('standard','strict','monitor_only','disabled')),
    schedule        TEXT    NOT NULL DEFAULT '[]',   -- JSON array of schedule rule objects
    enabled         INTEGER NOT NULL DEFAULT 1,
    is_default      INTEGER NOT NULL DEFAULT 0,
    preset_key      TEXT,                             -- Which PRESET_PROFILES key this was created from
    description     TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_profiles_enabled ON profiles(enabled);

CREATE TABLE IF NOT EXISTS profile_devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    ip_address  TEXT    NOT NULL DEFAULT '',
    mac_address TEXT    NOT NULL DEFAULT '',
    device_name TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_profile_devices_profile ON profile_devices(profile_id);
CREATE INDEX IF NOT EXISTS idx_profile_devices_ip      ON profile_devices(ip_address);
CREATE INDEX IF NOT EXISTS idx_profile_devices_mac     ON profile_devices(mac_address);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ip_in_range(ip_str: str, range_str: str) -> bool:
    """
    Return True if *ip_str* falls within *range_str*.

    Supported formats:
    - Single IP:      "192.168.1.50"
    - CIDR notation:  "192.168.1.0/24"
    - Dash range:     "192.168.1.100-192.168.1.120"
    """
    try:
        client = ipaddress.ip_address(ip_str)
        if "-" in range_str:
            parts = range_str.split("-", 1)
            start = ipaddress.ip_address(parts[0].strip())
            end = ipaddress.ip_address(parts[1].strip())
            return start <= client <= end
        elif "/" in range_str:
            net = ipaddress.ip_network(range_str.strip(), strict=False)
            return client in net
        else:
            return client == ipaddress.ip_address(range_str.strip())
    except (ValueError, TypeError):
        return False


def _mac_normalise(mac: str) -> str:
    """Normalise a MAC address to lowercase colon-separated form."""
    mac = mac.strip().lower().replace("-", ":").replace(".", ":")
    # Handle compact forms like aabbccddeeff → aa:bb:cc:dd:ee:ff
    if len(mac) == 12 and ":" not in mac:
        mac = ":".join(mac[i:i+2] for i in range(0, 12, 2))
    return mac


def _schedule_active(rule: Dict[str, Any]) -> bool:
    """
    Return True if *rule* is currently active.

    Rule format:
      {
        "days": [0,1,2,3,4],   # 0=Monday … 6=Sunday
        "start_time": "20:00",
        "end_time": "08:00",   # end < start means overnight range
        "mode": "strict"
      }
    """
    now = datetime.now()
    weekday = now.weekday()  # 0=Monday
    days = rule.get("days", list(range(7)))
    if weekday not in days:
        return False

    try:
        start_h, start_m = map(int, rule["start_time"].split(":"))
        end_h, end_m = map(int, rule["end_time"].split(":"))
    except (KeyError, ValueError):
        return False

    now_minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if start_minutes <= end_minutes:
        # Same-day range e.g. 08:00–18:00
        return start_minutes <= now_minutes < end_minutes
    else:
        # Overnight range e.g. 20:00–08:00
        return now_minutes >= start_minutes or now_minutes < end_minutes


# ---------------------------------------------------------------------------
# Profile dataclass (plain dict-backed; avoids Pydantic dependency here)
# ---------------------------------------------------------------------------

class Profile:
    """In-memory representation of a profile row."""

    __slots__ = (
        "id", "name", "ip_ranges", "mac_addresses", "thresholds",
        "response_mode", "schedule", "enabled", "is_default",
        "preset_key", "description", "created_at", "updated_at",
    )

    def __init__(
        self,
        *,
        id: Optional[int] = None,
        name: str = "",
        ip_ranges: Optional[List[str]] = None,
        mac_addresses: Optional[List[str]] = None,
        thresholds: Optional[Dict[str, Any]] = None,
        response_mode: str = "standard",
        schedule: Optional[List[Dict[str, Any]]] = None,
        enabled: bool = True,
        is_default: bool = False,
        preset_key: Optional[str] = None,
        description: str = "",
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        self.id = id
        self.name = name
        self.ip_ranges = ip_ranges or []
        self.mac_addresses = [_mac_normalise(m) for m in (mac_addresses or [])]
        self.thresholds = thresholds or {}
        self.response_mode = response_mode
        self.schedule = schedule or []
        self.enabled = enabled
        self.is_default = is_default
        self.preset_key = preset_key
        self.description = description
        self.created_at = created_at or _now_iso()
        self.updated_at = updated_at or _now_iso()

    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ip_ranges": self.ip_ranges,
            "mac_addresses": self.mac_addresses,
            "thresholds": self.thresholds,
            "response_mode": self.response_mode,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "is_default": self.is_default,
            "preset_key": self.preset_key,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Profile":
        return cls(
            id=row["id"],
            name=row["name"],
            ip_ranges=json.loads(row["ip_ranges"] or "[]"),
            mac_addresses=json.loads(row["mac_addresses"] or "[]"),
            thresholds=json.loads(row["thresholds"] or "{}"),
            response_mode=row["response_mode"],
            schedule=json.loads(row["schedule"] or "[]"),
            enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
            preset_key=row["preset_key"],
            description=row["description"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ---------------------------------------------------------------------------
# ProfileManager
# ---------------------------------------------------------------------------

class ProfileManager:
    """
    Manages device profiles with per-profile filtering thresholds.

    Thread-safety: all DB operations use a connection-per-call pattern.
    The IP→profile cache is protected by a threading.Lock.
    """

    # How long to cache IP → profile lookups (seconds)
    CACHE_TTL: int = 300

    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        self._cache: Dict[str, Tuple[Optional[int], float]] = {}  # ip → (profile_id, expiry)
        self._lock = threading.Lock()
        self._ensure_schema()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path, timeout=10)
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

    def _ensure_schema(self) -> None:
        """Create profile tables if they do not exist."""
        try:
            with self._conn() as conn:
                for stmt in _PROFILE_SCHEMA_SQL.strip().split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        conn.execute(stmt)
            logger.debug("Profile schema verified.")
        except sqlite3.Error as exc:
            logger.error("Failed to initialise profile schema: %s", exc)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_profile(self, profile: Profile) -> int:
        """
        Insert a new profile.  Returns the new row ID.

        Raises ValueError if name is empty or response_mode is invalid.
        """
        if not profile.name.strip():
            raise ValueError("Profile name must not be empty.")
        valid_modes = {"standard", "strict", "monitor_only", "disabled"}
        if profile.response_mode not in valid_modes:
            raise ValueError(f"response_mode must be one of {valid_modes}.")

        now = _now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO profiles
                    (name, ip_ranges, mac_addresses, thresholds, response_mode,
                     schedule, enabled, is_default, preset_key, description,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    profile.name.strip(),
                    json.dumps(profile.ip_ranges),
                    json.dumps(profile.mac_addresses),
                    json.dumps(profile.thresholds),
                    profile.response_mode,
                    json.dumps(profile.schedule),
                    1 if profile.enabled else 0,
                    1 if profile.is_default else 0,
                    profile.preset_key,
                    profile.description,
                    now, now,
                ),
            )
            new_id: int = cur.lastrowid  # type: ignore[assignment]

        profile.id = new_id
        profile.created_at = now
        profile.updated_at = now
        self._invalidate_cache()
        logger.info("Created profile id=%d name=%r", new_id, profile.name)
        return new_id

    def update_profile(self, profile_id: int, updates: Dict[str, Any]) -> bool:
        """
        Apply *updates* to an existing profile.  Returns True if a row was updated.
        """
        allowed = {
            "name", "ip_ranges", "mac_addresses", "thresholds",
            "response_mode", "schedule", "enabled", "is_default",
            "preset_key", "description",
        }
        clean: Dict[str, Any] = {}
        for k, v in updates.items():
            if k not in allowed:
                continue
            if k in ("ip_ranges", "mac_addresses", "thresholds", "schedule"):
                clean[k] = json.dumps(v)
            elif k == "enabled":
                clean[k] = 1 if v else 0
            elif k == "is_default":
                clean[k] = 1 if v else 0
            else:
                clean[k] = v

        if not clean:
            return False

        clean["updated_at"] = _now_iso()
        set_clause = ", ".join(f"{k}=?" for k in clean)
        values = list(clean.values()) + [profile_id]

        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE profiles SET {set_clause} WHERE id=?", values
            )
            changed = cur.rowcount > 0

        if changed:
            self._invalidate_cache()
        return changed

    def delete_profile(self, profile_id: int) -> bool:
        """Delete a profile and its device assignments.  Returns True if deleted."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
            changed = cur.rowcount > 0
        if changed:
            self._invalidate_cache()
        return changed

    def get_profile(self, profile_id: int) -> Optional[Profile]:
        """Return a single profile by ID, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
        return Profile.from_row(row) if row else None

    def list_profiles(self) -> List[Profile]:
        """Return all profiles ordered by is_default DESC, name ASC."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM profiles ORDER BY is_default DESC, name ASC"
            ).fetchall()
        return [Profile.from_row(r) for r in rows]

    def get_default_profile(self) -> Optional[Profile]:
        """Return the default profile (is_default=1), if any."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM profiles WHERE is_default=1 LIMIT 1"
            ).fetchone()
        return Profile.from_row(row) if row else None

    def set_default_profile(self, profile_id: int) -> bool:
        """Mark *profile_id* as default, clearing any previous default."""
        with self._conn() as conn:
            conn.execute("UPDATE profiles SET is_default=0")
            cur = conn.execute(
                "UPDATE profiles SET is_default=1 WHERE id=?", (profile_id,)
            )
            changed = cur.rowcount > 0
        if changed:
            self._invalidate_cache()
        return changed

    def create_from_preset(self, preset_key: str, name_override: str = "") -> int:
        """
        Create a new profile from a built-in preset.
        Returns the new profile ID.
        """
        if preset_key not in PRESET_PROFILES:
            raise ValueError(f"Unknown preset key: {preset_key!r}. "
                             f"Valid keys: {list(PRESET_PROFILES.keys())}")
        data = PRESET_PROFILES[preset_key]
        name = name_override.strip() or data["name"]
        profile = Profile(
            name=name,
            thresholds=dict(data.get("thresholds", {})),
            response_mode=data.get("response_mode", "standard"),
            preset_key=preset_key,
            description=data.get("description", ""),
        )
        return self.create_profile(profile)

    # ------------------------------------------------------------------
    # Device assignment
    # ------------------------------------------------------------------

    def assign_device(
        self,
        profile_id: int,
        ip: str = "",
        mac: str = "",
        name: str = "",
    ) -> bool:
        """
        Assign an IP address and/or MAC address to *profile_id*.
        Returns True on success.
        """
        if not ip and not mac:
            raise ValueError("At least one of ip or mac must be provided.")
        mac_clean = _mac_normalise(mac) if mac else ""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO profile_devices (profile_id, ip_address, mac_address, device_name)
                VALUES (?, ?, ?, ?)
                """,
                (profile_id, ip.strip(), mac_clean, name.strip()),
            )
        self._invalidate_cache()
        logger.debug("Assigned device ip=%r mac=%r to profile %d", ip, mac_clean, profile_id)
        return True

    def remove_device(self, device_id: int) -> bool:
        """Remove a device assignment by its row ID.  Returns True if removed."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM profile_devices WHERE id=?", (device_id,)
            )
            changed = cur.rowcount > 0
        if changed:
            self._invalidate_cache()
        return changed

    def get_device_assignments(self, profile_id: int) -> List[Dict[str, Any]]:
        """Return all device assignments for *profile_id*."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, profile_id, ip_address, mac_address, device_name, created_at
                FROM profile_devices WHERE profile_id=? ORDER BY id
                """,
                (profile_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def all_device_assignments(self) -> List[Dict[str, Any]]:
        """Return every device assignment with the profile name included."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT pd.id, pd.profile_id, p.name AS profile_name,
                       pd.ip_address, pd.mac_address, pd.device_name, pd.created_at
                FROM profile_devices pd
                JOIN profiles p ON p.id = pd.profile_id
                ORDER BY pd.profile_id, pd.id
                """,
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Schedule helpers
    # ------------------------------------------------------------------

    def get_active_mode(self, profile: Profile) -> str:
        """
        Return the effective response_mode for *profile* given the current time.

        Checks schedule rules in order; the first matching rule overrides the
        profile's base response_mode.  Returns base mode if no rule matches.
        """
        for rule in profile.schedule:
            if _schedule_active(rule):
                override_mode = rule.get("mode", "")
                valid = {"standard", "strict", "monitor_only", "disabled"}
                if override_mode in valid:
                    logger.debug(
                        "Schedule rule active for profile %r → mode=%r",
                        profile.name, override_mode,
                    )
                    return override_mode
        return profile.response_mode

    def add_schedule_rule(
        self,
        profile_id: int,
        days: List[int],
        start_time: str,
        end_time: str,
        mode: str,
    ) -> bool:
        """
        Append a schedule rule to *profile_id*.

        days: list of 0-6 (0=Monday).
        start_time / end_time: "HH:MM" strings.
        mode: response_mode value.
        """
        profile = self.get_profile(profile_id)
        if not profile:
            return False
        new_rule = {
            "days": days,
            "start_time": start_time,
            "end_time": end_time,
            "mode": mode,
        }
        profile.schedule.append(new_rule)
        return self.update_profile(profile_id, {"schedule": profile.schedule})

    def set_schedule(self, profile_id: int, schedule: List[Dict[str, Any]]) -> bool:
        """Replace the entire schedule for *profile_id*."""
        return self.update_profile(profile_id, {"schedule": schedule})

    # ------------------------------------------------------------------
    # IP lookup — main integration point
    # ------------------------------------------------------------------

    def get_profile_for_ip(self, client_ip: str) -> Optional[Profile]:
        """
        Determine which profile applies to *client_ip*.

        Resolution order:
        1. Cache hit (5-minute TTL)
        2. Explicit IP/range match in profiles table (ip_ranges column)
        3. Explicit device-level assignment in profile_devices table
        4. MAC-based lookup (for DHCP environments — MAC resolved externally)
        5. Default profile (is_default=1)
        6. None (no filtering profile; global defaults apply)
        """
        now = time.monotonic()

        # --- Cache lookup ---
        with self._lock:
            cached = self._cache.get(client_ip)
            if cached is not None:
                pid, expiry = cached
                if now < expiry:
                    if pid is None:
                        return None
                    return self.get_profile(pid)

        profile = self._resolve_profile(client_ip)
        pid = profile.id if profile else None

        # --- Cache store ---
        with self._lock:
            self._cache[client_ip] = (pid, now + self.CACHE_TTL)

        return profile

    def _resolve_profile(self, client_ip: str) -> Optional[Profile]:
        """Internal uncached profile resolution."""
        profiles = self.list_profiles()

        # 1. Check ip_ranges in each profile
        for p in profiles:
            if not p.enabled:
                continue
            for ip_range in p.ip_ranges:
                if _ip_in_range(client_ip, ip_range):
                    return p

        # 2. Check profile_devices table (explicit IP assignment)
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT pd.profile_id FROM profile_devices pd
                JOIN profiles p ON p.id = pd.profile_id
                WHERE pd.ip_address = ? AND p.enabled = 1
                LIMIT 1
                """,
                (client_ip,),
            ).fetchone()
            if row:
                return self.get_profile(row["profile_id"])

        # 3. Default profile
        default = self.get_default_profile()
        if default and default.enabled:
            return default

        return None

    def get_profile_for_mac(self, mac: str) -> Optional[Profile]:
        """Look up a profile by MAC address (used in DHCP environments)."""
        mac_clean = _mac_normalise(mac)
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT pd.profile_id FROM profile_devices pd
                JOIN profiles p ON p.id = pd.profile_id
                WHERE pd.mac_address = ? AND p.enabled = 1
                LIMIT 1
                """,
                (mac_clean,),
            ).fetchone()
            if row:
                return self.get_profile(row["profile_id"])
        # Also check mac_addresses JSON column in profiles
        for p in self.list_profiles():
            if not p.enabled:
                continue
            if mac_clean in p.mac_addresses:
                return p
        return None

    # ------------------------------------------------------------------
    # Threshold resolution — main integration point for decision engine
    # ------------------------------------------------------------------

    def get_effective_thresholds(self, client_ip: str) -> Dict[str, Any]:
        """
        Return the merged threshold dict for *client_ip*.

        Priority (highest → lowest):
          1. Profile thresholds (if a profile matches and mode is not monitor_only/disabled)
          2. Global defaults from config

        When mode is 'monitor_only', block_min and soft_block_min are set to
        infinity so nothing ever results in a block decision.

        When mode is 'disabled', returns a sentinel indicating no filtering.

        When mode is 'strict', block_min is set to monitor_min (block everything
        above the monitor floor).
        """
        # --- Global defaults ---
        defaults: Dict[str, Any] = {
            "keyword_threshold": config.keyword_threshold,
            "scene_threshold": config.scene_threshold,
            "audio_threshold": config.audio_threshold,
            "combined_threshold": config.combined_threshold,
            "block_min": config.block_score_min,
            "soft_block_min": config.soft_block_score_min,
            "monitor_min": config.monitor_score_min,
            "keyword_weight": config.weights["keyword"],
            "scene_weight": config.weights["scene"],
            "audio_weight": config.weights["audio"],
            "response_mode": "standard",
            "profile_id": None,
            "profile_name": None,
            "_disabled": False,
        }

        profile = self.get_profile_for_ip(client_ip)
        if profile is None:
            return defaults

        # Determine effective mode (schedule may override base mode)
        mode = self.get_active_mode(profile)
        defaults["response_mode"] = mode
        defaults["profile_id"] = profile.id
        defaults["profile_name"] = profile.name

        if mode == "disabled":
            defaults["_disabled"] = True
            return defaults

        if mode == "monitor_only":
            defaults["block_min"] = 99999
            defaults["soft_block_min"] = 99999
            # Still merge profile thresholds for scores/logging
            for k, v in profile.thresholds.items():
                defaults[k] = v
            return defaults

        # Merge profile-specific threshold overrides
        for k, v in profile.thresholds.items():
            defaults[k] = v

        if mode == "strict":
            # In strict mode, collapse soft_block_min down to monitor_min
            # so content above the monitor floor is hard-blocked immediately.
            defaults["block_min"] = defaults["monitor_min"]
            defaults["soft_block_min"] = defaults["monitor_min"]

        return defaults

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _invalidate_cache(self) -> None:
        """Clear the IP→profile cache (called after any profile mutation)."""
        with self._lock:
            self._cache.clear()
        logger.debug("Profile IP cache invalidated.")

    def invalidate_ip(self, client_ip: str) -> None:
        """Invalidate cache for a single IP address."""
        with self._lock:
            self._cache.pop(client_ip, None)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_recently_seen_ips(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Return the most recently seen client IPs from the requests table,
        annotated with which profile (if any) they map to.

        Used by the admin panel's "Scan Network" feature.
        """
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    """
                    SELECT client_ip, COUNT(*) as request_count,
                           MAX(timestamp) as last_seen
                    FROM requests
                    GROUP BY client_ip
                    ORDER BY last_seen DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.OperationalError:
            # requests table may not exist yet in fresh installs
            return []

        result = []
        for row in rows:
            ip = row["client_ip"]
            p = self.get_profile_for_ip(ip)
            result.append({
                "ip": ip,
                "request_count": row["request_count"],
                "last_seen": row["last_seen"],
                "profile_id": p.id if p else None,
                "profile_name": p.name if p else None,
            })
        return result

    def profile_device_count(self, profile_id: int) -> int:
        """Return how many devices are explicitly assigned to *profile_id*."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM profile_devices WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
        return row[0] if row else 0

    def duplicate_profile(self, profile_id: int, new_name: str) -> int:
        """
        Create a copy of *profile_id* with *new_name*.
        Device assignments are NOT copied (the copy starts with no devices).
        Returns the new profile ID.
        """
        original = self.get_profile(profile_id)
        if not original:
            raise ValueError(f"Profile {profile_id} not found.")
        copy = Profile(
            name=new_name.strip() or f"Copy of {original.name}",
            ip_ranges=list(original.ip_ranges),
            mac_addresses=list(original.mac_addresses),
            thresholds=dict(original.thresholds),
            response_mode=original.response_mode,
            schedule=list(original.schedule),
            enabled=original.enabled,
            is_default=False,
            preset_key=original.preset_key,
            description=original.description,
        )
        return self.create_profile(copy)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

profile_manager = ProfileManager()
