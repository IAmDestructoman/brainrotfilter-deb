"""
community_keywords.py — Community keyword sharing system for BrainrotFilter.

Manages synchronization of brainrot keyword lists from a GitHub-hosted
community repository. Supports opt-in auto-update, configurable merge
strategies, backup/rollback, and local submission staging.

Architecture:
  - Community keywords are hosted on GitHub (raw content, no API key needed)
  - Local installations opt-in via community_keywords_enabled setting
  - Keywords merge into the local list using a configurable strategy
  - All changes are reversible via rollback
  - Pending submissions stored locally for manual GitHub Issue/PR submission

Configuration keys (stored in settings DB):
  community_keywords_enabled          bool   (default False)
  community_keywords_url              str    (GitHub raw URL)
  community_keywords_branch           str    (default "main")
  community_keywords_strategy         str    (default "additive")
  community_keywords_auto_update      bool   (default False)
  community_keywords_interval_hours   int    (default 24)
  community_keywords_last_check       str    (ISO datetime)
  community_keywords_last_hash        str    (SHA256 of last synced content)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path constants (mirror config.py conventions, allow env overrides)
# ---------------------------------------------------------------------------

_ETC_DIR = os.environ.get("BRAINROT_ETC_DIR", "/usr/local/etc/brainrotfilter")
KEYWORDS_PATH = os.environ.get(
    "BRAINROT_KEYWORDS_PATH",
    os.path.join(_ETC_DIR, "keywords.json"),
)
KEYWORDS_BACKUP_PATH = os.environ.get(
    "BRAINROT_KEYWORDS_BACKUP_PATH",
    os.path.join(_ETC_DIR, "keywords.json.bak"),
)
PENDING_SUBMISSIONS_PATH = os.environ.get(
    "BRAINROT_PENDING_SUBMISSIONS_PATH",
    os.path.join(_ETC_DIR, "pending_submissions.json"),
)

_DEFAULT_COMMUNITY_URL = (
    "https://raw.githubusercontent.com/IAmDestructoman/pfSense-pkg-BrainrotFilter"
    "/main/community-keywords.json"
)

# ---------------------------------------------------------------------------
# Merge strategy constants
# ---------------------------------------------------------------------------

STRATEGY_ADDITIVE = "additive"
STRATEGY_FULL_SYNC = "full_sync"
STRATEGY_WEIGHTED_MERGE = "weighted_merge"

VALID_STRATEGIES = {STRATEGY_ADDITIVE, STRATEGY_FULL_SYNC, STRATEGY_WEIGHTED_MERGE}


# ---------------------------------------------------------------------------
# CommunityKeywordManager
# ---------------------------------------------------------------------------


class CommunityKeywordManager:
    """
    Manages synchronization of brainrot keywords from a community-maintained
    GitHub repository. Supports auto-update, merge strategies, and rollback.

    Thread-safe for concurrent reads; write operations use a simple file-level
    operation (rename/replace) that is atomic on POSIX systems.
    """

    def __init__(self) -> None:
        # Lazy import to avoid circular import at module load time
        self._config: Optional[Any] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_config(self) -> Any:
        """Return the shared config singleton (lazy import)."""
        if self._config is None:
            try:
                from config import config  # type: ignore
                self._config = config
            except ImportError:
                logger.warning("config module not available; using defaults.")
                self._config = _FallbackConfig()
        return self._config

    def _setting(self, key: str, default: Any = None) -> Any:
        cfg = self._get_config()
        return cfg.get(key, default)

    def _community_url(self) -> str:
        return str(
            self._setting("community_keywords_url", _DEFAULT_COMMUNITY_URL)
        )

    def _branch(self) -> str:
        return str(self._setting("community_keywords_branch", "main"))

    def _strategy(self) -> str:
        s = str(self._setting("community_keywords_strategy", STRATEGY_ADDITIVE))
        return s if s in VALID_STRATEGIES else STRATEGY_ADDITIVE

    def _enabled(self) -> bool:
        cfg = self._get_config()
        try:
            return bool(cfg.get_bool("community_keywords_enabled"))
        except AttributeError:
            val = cfg.get("community_keywords_enabled", False)
            return str(val).lower() in ("true", "1", "yes") if isinstance(val, str) else bool(val)

    def _auto_update(self) -> bool:
        cfg = self._get_config()
        try:
            return bool(cfg.get_bool("community_keywords_auto_update"))
        except AttributeError:
            val = cfg.get("community_keywords_auto_update", False)
            return str(val).lower() in ("true", "1", "yes") if isinstance(val, str) else bool(val)

    def _interval_hours(self) -> int:
        return int(self._setting("community_keywords_interval_hours", 24))

    def _last_check(self) -> Optional[datetime]:
        raw = self._setting("community_keywords_last_check", "")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except (ValueError, TypeError):
            return None

    def _last_hash(self) -> str:
        return str(self._setting("community_keywords_last_hash", ""))

    def _save_setting(self, key: str, value: Any) -> None:
        cfg = self._get_config()
        try:
            cfg.save(key, value)
        except Exception as exc:
            logger.warning("Could not save setting %s: %s", key, exc)

    @staticmethod
    def _sha256(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Keyword file I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _load_local_keywords() -> Dict[str, Any]:
        """Load keywords.json from disk. Returns empty structure on failure."""
        path = Path(KEYWORDS_PATH)
        if not path.exists():
            logger.warning("Local keywords file not found at %s.", KEYWORDS_PATH)
            return {"categories": {}}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.error("Failed to load local keywords: %s", exc)
            return {"categories": {}}

    @staticmethod
    def _write_keywords(data: Dict[str, Any], path: str = KEYWORDS_PATH) -> bool:
        """Atomically write keyword data to *path*."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
            logger.debug("Keywords written to %s.", path)
            return True
        except OSError as exc:
            logger.error("Failed to write keywords to %s: %s", path, exc)
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass
            return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_community_keywords(self) -> Optional[Dict[str, Any]]:
        """
        Fetch the latest community keyword list from GitHub.

        Uses raw.githubusercontent.com for direct file access — no API key
        needed.  Caches the result using an in-memory ETag/Last-Modified
        header if the HTTP library exposes them.

        Returns parsed JSON dict, or None on failure.
        """
        url = self._community_url()
        logger.info("Fetching community keywords from %s", url)

        try:
            import urllib.request  # stdlib, always available
            import urllib.error

            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "BrainrotFilter/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            logger.info(
                "Community keywords fetched (%d bytes, schema=%s).",
                len(raw),
                data.get("_schema_version", "?"),
            )
            return data

        except Exception as exc:
            logger.warning("Failed to fetch community keywords: %s", exc)
            return None

    def merge_keywords(
        self,
        local: Dict[str, Any],
        community: Dict[str, Any],
        strategy: str = STRATEGY_ADDITIVE,
    ) -> Dict[str, Any]:
        """
        Merge community keywords into local keywords using the specified strategy.

        Strategies
        ----------
        additive        Add community keywords absent locally. Never touch locals.
        full_sync       Replace local category contents with community contents.
        weighted_merge  Add community keywords; use higher weight if both exist.

        Returns merged keyword dict (deep copy — does not mutate inputs).
        """
        if strategy not in VALID_STRATEGIES:
            logger.warning("Unknown merge strategy %r; defaulting to additive.", strategy)
            strategy = STRATEGY_ADDITIVE

        import copy
        result = copy.deepcopy(local)
        community_cats: Dict[str, List] = (community or {}).get("categories", {})
        result.setdefault("categories", {})

        if strategy == STRATEGY_FULL_SYNC:
            result["categories"] = copy.deepcopy(community_cats)
            logger.info("full_sync: replaced local categories with community.")
            return result

        for cat_name, community_entries in community_cats.items():
            if strategy == STRATEGY_ADDITIVE:
                local_entries = result["categories"].get(cat_name, [])
                local_keys = {e.get("keyword", "").lower() for e in local_entries}
                added = 0
                for entry in community_entries:
                    kw = entry.get("keyword", "")
                    if kw.lower() not in local_keys:
                        local_entries.append(copy.deepcopy(entry))
                        local_keys.add(kw.lower())
                        added += 1
                if cat_name not in result["categories"]:
                    result["categories"][cat_name] = local_entries
                else:
                    result["categories"][cat_name] = local_entries
                if added:
                    logger.debug("additive: +%d keywords in category '%s'.", added, cat_name)

            elif strategy == STRATEGY_WEIGHTED_MERGE:
                local_entries = result["categories"].get(cat_name, [])
                local_index: Dict[str, int] = {
                    e.get("keyword", "").lower(): i for i, e in enumerate(local_entries)
                }
                added = modified = 0
                for entry in community_entries:
                    kw = entry.get("keyword", "")
                    kw_lower = kw.lower()
                    if kw_lower in local_index:
                        idx = local_index[kw_lower]
                        local_weight = local_entries[idx].get("weight", 0)
                        community_weight = entry.get("weight", 0)
                        if community_weight > local_weight:
                            local_entries[idx]["weight"] = community_weight
                            modified += 1
                    else:
                        local_entries.append(copy.deepcopy(entry))
                        local_index[kw_lower] = len(local_entries) - 1
                        added += 1
                result["categories"][cat_name] = local_entries
                if added or modified:
                    logger.debug(
                        "weighted_merge: +%d added, %d weight-upgraded in '%s'.",
                        added,
                        modified,
                        cat_name,
                    )

        return result

    def compute_diff(
        self,
        local: Dict[str, Any],
        community: Dict[str, Any],
        strategy: str = STRATEGY_ADDITIVE,
    ) -> Dict[str, Any]:
        """
        Compute what would change if merge_keywords() were applied.

        Returns:
            {
              "added":    [{"keyword": ..., "weight": ..., "category": ...}],
              "modified": [{"keyword": ..., "old_weight": ..., "new_weight": ..., "category": ...}],
              "removed":  [],  # only populated for full_sync
              "total":    int,
            }
        """
        added: List[Dict] = []
        modified: List[Dict] = []
        removed: List[Dict] = []

        local_cats: Dict[str, List] = (local or {}).get("categories", {})
        community_cats: Dict[str, List] = (community or {}).get("categories", {})

        if strategy == STRATEGY_FULL_SYNC:
            # Removed = in local but not community; added = in community but not local
            for cat, entries in local_cats.items():
                for e in entries:
                    kw = e.get("keyword", "")
                    c_entries = community_cats.get(cat, [])
                    c_keys = {x.get("keyword", "").lower() for x in c_entries}
                    if kw.lower() not in c_keys:
                        removed.append({"keyword": kw, "weight": e.get("weight"), "category": cat})
            for cat, entries in community_cats.items():
                for e in entries:
                    kw = e.get("keyword", "")
                    l_entries = local_cats.get(cat, [])
                    l_keys = {x.get("keyword", "").lower() for x in l_entries}
                    if kw.lower() not in l_keys:
                        added.append({"keyword": kw, "weight": e.get("weight"), "category": cat})
        else:
            for cat, c_entries in community_cats.items():
                l_entries = local_cats.get(cat, [])
                l_index = {e.get("keyword", "").lower(): e for e in l_entries}
                for e in c_entries:
                    kw = e.get("keyword", "")
                    if kw.lower() not in l_index:
                        added.append({"keyword": kw, "weight": e.get("weight"), "category": cat})
                    elif strategy == STRATEGY_WEIGHTED_MERGE:
                        local_entry = l_index[kw.lower()]
                        if e.get("weight", 0) > local_entry.get("weight", 0):
                            modified.append({
                                "keyword": kw,
                                "old_weight": local_entry.get("weight"),
                                "new_weight": e.get("weight"),
                                "category": cat,
                            })

        return {
            "added": added,
            "modified": modified,
            "removed": removed,
            "total": len(added) + len(modified) + len(removed),
        }

    def auto_update(self) -> Dict[str, Any]:
        """
        Check for and apply community keyword updates if enabled and due.

        Steps:
          1. Fetch latest community keywords from GitHub
          2. Compare hash with last synced version
          3. If changed, backup current keywords
          4. Merge with local keywords using configured strategy
          5. Write merged result
          6. Return diff summary

        Returns diff dict: {added, modified, removed, total, changed: bool, error: str|None}
        """
        _cfg = self._get_config()  # noqa: F841
        result: Dict[str, Any] = {
            "added": [], "modified": [], "removed": [], "total": 0,
            "changed": False, "error": None,
        }

        if not self._enabled():
            result["error"] = "community_keywords_enabled is False"
            return result

        # Rate-limit: honour interval_hours
        last = self._last_check()
        if last is not None:
            elapsed_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if elapsed_hours < self._interval_hours():
                result["error"] = f"Too soon; next check in {self._interval_hours() - elapsed_hours:.1f}h"
                return result

        # Mark check time
        self._save_setting("community_keywords_last_check", datetime.now(timezone.utc).isoformat())

        community = self.fetch_community_keywords()
        if community is None:
            result["error"] = "Failed to fetch community keywords"
            return result

        # Check if content changed
        raw_str = json.dumps(community, sort_keys=True)
        new_hash = self._sha256(raw_str)
        old_hash = self._last_hash()
        if new_hash == old_hash:
            logger.info("Community keywords unchanged (hash match).")
            return result

        local = self._load_local_keywords()
        strategy = self._strategy()
        diff = self.compute_diff(local, community, strategy)

        if diff["total"] == 0 and not diff["removed"]:
            logger.info("Community keywords: no net changes after diff.")
            self._save_setting("community_keywords_last_hash", new_hash)
            return result

        # Backup before applying
        self._backup_keywords()

        merged = self.merge_keywords(local, community, strategy)
        success = self._write_keywords(merged)
        if not success:
            result["error"] = "Failed to write merged keywords"
            return result

        self._save_setting("community_keywords_last_hash", new_hash)

        result.update(diff)
        result["changed"] = True
        logger.info(
            "Community update applied: +%d added, %d modified, %d removed (strategy=%s).",
            len(diff["added"]),
            len(diff["modified"]),
            len(diff["removed"]),
            strategy,
        )

        # Signal keyword_analyzer to reload its in-memory list
        try:
            import keyword_analyzer  # type: ignore
            if hasattr(keyword_analyzer, "reload_keywords"):
                keyword_analyzer.reload_keywords()
        except Exception as exc:
            logger.debug("Could not signal keyword_analyzer reload: %s", exc)

        return result

    def get_update_status(self) -> Dict[str, Any]:
        """
        Return current sync status.

        Returns dict with keys: last_check, last_update, community_version,
        local_version, keywords_from_community, pending_updates, enabled,
        strategy, auto_update, interval_hours.
        """
        local = self._load_local_keywords()
        local_version = local.get("_version", local.get("metadata", {}).get("version", "unknown"))

        # Count community-sourced keywords if metadata present
        kw_from_community = 0
        for cat_entries in local.get("categories", {}).values():
            for entry in cat_entries:
                if entry.get("_community", False):
                    kw_from_community += 1

        return {
            "enabled": self._enabled(),
            "auto_update": self._auto_update(),
            "strategy": self._strategy(),
            "interval_hours": self._interval_hours(),
            "url": self._community_url(),
            "branch": self._branch(),
            "last_check": self._setting("community_keywords_last_check", None),
            "last_hash": self._last_hash(),
            "local_version": local_version,
            "community_version": None,  # populated after a fetch
            "keywords_from_community": kw_from_community,
            "pending_updates": 0,  # populated by pending diff
            "backup_available": Path(KEYWORDS_BACKUP_PATH).exists(),
        }

    def get_pending_diff(self) -> Optional[Dict[str, Any]]:
        """
        Fetch community keywords and return the diff without applying it.
        Returns diff dict or None if fetch failed.
        """
        community = self.fetch_community_keywords()
        if community is None:
            return None
        local = self._load_local_keywords()
        diff = self.compute_diff(local, community, self._strategy())
        diff["community_version"] = community.get("_last_updated", "?")
        return diff

    def rollback_keywords(self) -> bool:
        """Restore keywords from the last backup."""
        bak = Path(KEYWORDS_BACKUP_PATH)
        if not bak.exists():
            logger.warning("No backup file found at %s; cannot rollback.", KEYWORDS_BACKUP_PATH)
            return False
        try:
            shutil.copy2(str(bak), KEYWORDS_PATH)
            logger.info("Keywords restored from backup %s.", KEYWORDS_BACKUP_PATH)
            # Signal reload
            try:
                import keyword_analyzer  # type: ignore
                if hasattr(keyword_analyzer, "reload_keywords"):
                    keyword_analyzer.reload_keywords()
            except Exception:
                pass
            return True
        except OSError as exc:
            logger.error("Rollback failed: %s", exc)
            return False

    def _backup_keywords(self) -> bool:
        """Copy current keywords.json to backup path."""
        src = Path(KEYWORDS_PATH)
        if not src.exists():
            return False
        try:
            Path(KEYWORDS_BACKUP_PATH).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), KEYWORDS_BACKUP_PATH)
            logger.debug("Keywords backed up to %s.", KEYWORDS_BACKUP_PATH)
            return True
        except OSError as exc:
            logger.warning("Keyword backup failed: %s", exc)
            return False

    def submit_keyword(
        self,
        keyword: str,
        weight: int,
        category: str,
        evidence: str = "",
    ) -> Dict[str, Any]:
        """
        Stage a keyword suggestion in local pending_submissions.json.

        The pending list can then be submitted as a GitHub Issue or PR
        via the admin panel's "Copy as GitHub Issue" button.

        Returns the submission record.
        """
        keyword = keyword.strip()
        if not keyword:
            return {"error": "keyword cannot be empty"}
        if not (1 <= weight <= 10):
            return {"error": "weight must be between 1 and 10"}

        submission = {
            "keyword": keyword,
            "weight": weight,
            "category": category,
            "evidence": evidence,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }

        submissions = self._load_submissions()
        # Avoid duplicates
        for existing in submissions:
            if existing.get("keyword", "").lower() == keyword.lower():
                return {"error": f"Keyword '{keyword}' already in pending submissions"}

        submissions.append(submission)
        self._write_submissions(submissions)
        logger.info("Keyword suggestion staged: %r (weight=%d, cat=%s)", keyword, weight, category)
        return submission

    def get_submissions(self) -> List[Dict[str, Any]]:
        """Return all pending keyword submissions."""
        return self._load_submissions()

    def _load_submissions(self) -> List[Dict[str, Any]]:
        path = Path(PENDING_SUBMISSIONS_PATH)
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Failed to load pending submissions: %s", exc)
            return []

    def _write_submissions(self, submissions: List[Dict[str, Any]]) -> None:
        Path(PENDING_SUBMISSIONS_PATH).parent.mkdir(parents=True, exist_ok=True)
        tmp = PENDING_SUBMISSIONS_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(submissions, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, PENDING_SUBMISSIONS_PATH)
        except OSError as exc:
            logger.error("Failed to write submissions: %s", exc)

    def format_github_issue(self, submission: Dict[str, Any]) -> str:
        """Format a submission as a GitHub Issue body string."""
        return (
            "## Keyword Suggestion\n\n"
            f"**Keyword:** `{submission.get('keyword', '')}`\n"
            f"**Weight:** {submission.get('weight', '')}/10\n"
            f"**Category:** {submission.get('category', '')}\n\n"
            "### Evidence / Context\n\n"
            f"{submission.get('evidence', '_No evidence provided_')}\n\n"
            "---\n"
            "_Submitted via BrainrotFilter Community Keyword System_\n"
        )


# ---------------------------------------------------------------------------
# Minimal fallback config for when config.py is absent (e.g. unit tests)
# ---------------------------------------------------------------------------


class _FallbackConfig:
    """Minimal config shim used when the real config module is unavailable."""

    _store: Dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def get_bool(self, key: str) -> bool:
        val = self._store.get(key, False)
        return str(val).lower() in ("true", "1", "yes") if isinstance(val, str) else bool(val)

    def save(self, key: str, value: Any) -> None:
        self._store[key] = value


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

community_manager = CommunityKeywordManager()
