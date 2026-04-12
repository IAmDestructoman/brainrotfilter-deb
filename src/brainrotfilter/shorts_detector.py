"""
shorts_detector.py - YouTube Shorts detection and score multiplier.

Pipeline:
  1. URL pattern check: presence of /shorts/ path segment
  2. Duration heuristic: ≤ 60 seconds is a candidate Short
  3. Title/description hashtag detection: #shorts, #short
  4. Brainrot-specific Shorts content patterns (POV, Part X, Day X, etc.)
  5. Return a bonus score (0-100) to be added to the combined brainrot score

This module does NOT call any external API; all detection is based on the
metadata already fetched by the caller.
"""

from __future__ import annotations

import logging
import re
import time
from typing import List

from models import AnalysisResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Detection confidence levels
# ---------------------------------------------------------------------------

DETECTION_METHOD_CONFIRMED = "confirmed"   # /shorts/ URL segment
DETECTION_METHOD_LIKELY = "likely"         # duration + metadata signals
DETECTION_METHOD_NONE = "none"             # not a Short

# ---------------------------------------------------------------------------
# Score bonuses
# ---------------------------------------------------------------------------

BONUS_CONFIRMED_SHORT: int = 15
BONUS_LIKELY_SHORT: int = 10
BONUS_SHORT_WITH_BRAINROT: int = 20   # replaces lower bonuses when brainrot patterns found

# ---------------------------------------------------------------------------
# Brainrot content pattern definitions
# ---------------------------------------------------------------------------

# Each entry: (pattern_name, compiled_regex)
_BRAINROT_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    # Mass-produced serialized content
    ("part_number",      re.compile(r"\bpart\s*\d+\b", re.IGNORECASE)),
    ("episode_number",   re.compile(r"\bep(?:isode)?\s*\d+\b", re.IGNORECASE)),
    # POV format
    ("pov_prefix",       re.compile(r"^\s*pov\s*:", re.IGNORECASE)),
    # Engagement bait
    ("wait_for_it",      re.compile(r"\bwait\s+for\s+it\b", re.IGNORECASE)),
    ("watch_till_end",   re.compile(r"\bwatch\s+(?:till|to|until)\s+(?:the\s+)?end\b", re.IGNORECASE)),
    # Content farm — "Day X of..."
    ("day_counter",      re.compile(r"\bday\s*\d+\s+of\b", re.IGNORECASE)),
    # ALL CAPS title (≥ 80% uppercase alphabetic chars)
    ("all_caps",         re.compile(r"^(?:[^a-z]*[A-Z][^a-z]*){4,}$")),
    # Excessive punctuation: three or more consecutive ! or ?
    ("excessive_punct",  re.compile(r"[!?]{3,}")),
    # Brainrot-era slang in title that strongly signals Shorts
    ("sigma_rizz",       re.compile(r"\b(?:sigma|rizz|rizzler|skibidi|gyatt|grimace|slay|delulu|bussin)\b", re.IGNORECASE)),
    ("no_cap",           re.compile(r"\bno\s+cap\b", re.IGNORECASE)),
    ("fanum_tax",        re.compile(r"\bfanum\s+tax\b", re.IGNORECASE)),
]

# Emoji detection — simple unicode category check (Emoji_Presentation)
_EMOJI_RE = re.compile(
    r"[\U0001F600-\U0001F64F"   # emoticons
    r"\U0001F300-\U0001F5FF"   # misc symbols & pictographs
    r"\U0001F680-\U0001F6FF"   # transport & map
    r"\U0001F700-\U0001F77F"   # alchemical symbols
    r"\U0001F780-\U0001F7FF"   # geometric shapes extended
    r"\U0001F800-\U0001F8FF"   # supplemental arrows-C
    r"\U0001F900-\U0001F9FF"   # supplemental symbols & pictographs
    r"\U0001FA00-\U0001FA6F"   # chess symbols
    r"\U0001FA70-\U0001FAFF"   # symbols & pictographs extended-A
    r"\u2600-\u26FF"           # misc symbols
    r"\u2700-\u27BF"           # dingbats
    r"]",
    re.UNICODE,
)

_SHORTS_HASHTAG_RE = re.compile(r"#short(?:s)?\b", re.IGNORECASE)
_SHORTS_URL_RE = re.compile(r"/shorts/", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_multiple_emojis(text: str, threshold: int = 3) -> bool:
    """Return True if *text* contains at least *threshold* emoji characters."""
    return len(_EMOJI_RE.findall(text)) >= threshold


def _is_all_caps(text: str) -> bool:
    """
    Return True if ≥ 75 % of alphabetic characters in *text* are uppercase.
    Ignore non-alphabetic characters (emojis, numbers, punctuation).
    """
    alpha_chars = [c for c in text if c.isalpha()]
    if len(alpha_chars) < 4:
        return False
    upper_count = sum(1 for c in alpha_chars if c.isupper())
    return (upper_count / len(alpha_chars)) >= 0.75


def _match_patterns(title: str) -> List[str]:
    """
    Run all brainrot content patterns against *title*.
    Returns list of matched pattern names.
    """
    matched: List[str] = []

    # Run compiled regexes
    for name, pattern in _BRAINROT_PATTERNS:
        if name == "all_caps":
            # Use the custom function for better accuracy
            if _is_all_caps(title):
                matched.append("all_caps")
        else:
            if pattern.search(title):
                matched.append(name)

    # Multiple emoji check
    if _has_multiple_emojis(title):
        matched.append("multiple_emojis")

    return matched


# ---------------------------------------------------------------------------
# Main analyze function
# ---------------------------------------------------------------------------


def analyze(
    video_id: str,
    url: str = "",
    duration_seconds: int = 0,
    title: str = "",
    description: str = "",
) -> AnalysisResult:
    """
    Detect whether a video is a YouTube Short and calculate a brainrot bonus score.

    Args:
        video_id:         YouTube video ID (used only for logging).
        url:              The full YouTube URL that triggered this analysis.
        duration_seconds: Video duration in seconds (from YouTube API).
        title:            Video title.
        description:      Video description (first portion is sufficient).

    Returns:
        AnalysisResult where:
          - module  = "shorts"
          - score   = bonus points to ADD to the combined brainrot score (0-100)
          - details = ShortsDetails-compatible dict
    """
    start = time.monotonic()

    is_short: bool = False
    detection_method: str = DETECTION_METHOD_NONE
    bonus_applied: int = 0
    matched_patterns: List[str] = []

    # ------------------------------------------------------------------
    # Step 1: URL-based confirmation (highest confidence)
    # ------------------------------------------------------------------
    if url and _SHORTS_URL_RE.search(url):
        is_short = True
        detection_method = DETECTION_METHOD_CONFIRMED
        logger.debug("Video %s confirmed Short via URL pattern.", video_id)

    # ------------------------------------------------------------------
    # Step 2: Duration heuristic (≤ 60 seconds)
    # ------------------------------------------------------------------
    duration_is_short = 0 < duration_seconds <= 60

    # ------------------------------------------------------------------
    # Step 3: Hashtag signals in title / description
    # ------------------------------------------------------------------
    hashtag_in_title = bool(_SHORTS_HASHTAG_RE.search(title))
    hashtag_in_desc = bool(_SHORTS_HASHTAG_RE.search(description[:500]))

    if not is_short:
        if hashtag_in_title or hashtag_in_desc:
            is_short = True
            detection_method = DETECTION_METHOD_CONFIRMED
            logger.debug("Video %s confirmed Short via #shorts hashtag.", video_id)
        elif duration_is_short:
            is_short = True
            detection_method = DETECTION_METHOD_LIKELY
            logger.debug("Video %s likely Short via duration ≤ 60 s.", video_id)

    # ------------------------------------------------------------------
    # Step 4: Brainrot content pattern matching
    # ------------------------------------------------------------------
    if title:
        matched_patterns = _match_patterns(title)

    has_brainrot_patterns = len(matched_patterns) > 0

    # ------------------------------------------------------------------
    # Step 5: Calculate bonus score
    # ------------------------------------------------------------------
    if is_short:
        if has_brainrot_patterns:
            bonus_applied = BONUS_SHORT_WITH_BRAINROT
        elif detection_method == DETECTION_METHOD_CONFIRMED:
            bonus_applied = BONUS_CONFIRMED_SHORT
        else:
            bonus_applied = BONUS_LIKELY_SHORT
    else:
        # Not identified as a Short, but still score any brainrot patterns
        # (e.g. a regular video with POV: prefix or "Day X of...")
        if has_brainrot_patterns:
            # Apply a smaller penalty for pattern matches in non-Shorts
            bonus_applied = min(5 * len(matched_patterns), 15)

    # Cap at 100
    bonus_applied = min(bonus_applied, 100)

    elapsed = time.monotonic() - start

    logger.info(
        "Shorts detection for %s: is_short=%s, method=%s, bonus=%d, patterns=%s, duration=%.2fs",
        video_id,
        is_short,
        detection_method,
        bonus_applied,
        matched_patterns,
        elapsed,
    )

    return AnalysisResult(
        module="shorts",
        score=float(bonus_applied),
        details={
            "is_short": is_short,
            "detection_method": detection_method,
            "bonus_applied": bonus_applied,
            "matched_patterns": matched_patterns,
            "duration_seconds": duration_seconds,
            "duration_is_short": duration_is_short,
            "hashtag_in_title": hashtag_in_title,
            "hashtag_in_description": hashtag_in_desc,
            "url_confirmed": bool(url and _SHORTS_URL_RE.search(url)),
            "has_brainrot_patterns": has_brainrot_patterns,
            "brainrot_pattern_count": len(matched_patterns),
        },
        duration_s=round(elapsed, 3),
    )
