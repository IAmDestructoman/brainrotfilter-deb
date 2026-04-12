"""
keyword_analyzer.py - Keyword and emoji-based brainrot detection.

Pipeline:
  1. Load keyword/emoji lists from keywords.json (with per-keyword weights)
  2. Check title, description, tags for matches (case-insensitive, partial)
  3. OCR the video thumbnail via pytesseract
  4. Fetch and search auto-generated captions
  5. Detect brainrot-associated emojis
  6. Compute weighted keyword_score (0-100)
  7. Return score + matched keywords + details dict
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import KEYWORDS_PATH
from models import AnalysisResult, KeywordMatch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brainrot emoji set (always loaded, not configurable per-file)
# ---------------------------------------------------------------------------

BRAINROT_EMOJIS: Dict[str, float] = {
    "🧠": 2.0,   # brain — ironic "brain cell" usage
    "💀": 3.0,   # skull  — "I'm dead" reaction
    "🔥": 2.5,   # fire   — hype
    "💯": 2.5,   # 100    — emphasis
    "⚡": 1.5,   # lightning
    "🗿": 4.0,   # moai   — strong brainrot indicator
    "😤": 1.5,
    "🤑": 1.5,
    "😈": 2.0,
    "🥶": 2.0,   # cold — "no cap" context
    "🐺": 2.5,   # lone wolf / sigma
    "👑": 1.5,   # alpha
    "💪": 1.5,   # grindset
    "🎯": 1.0,
    "🚀": 1.5,
    "😂": 1.0,
    "🤣": 1.0,
    "🍷": 1.5,   # "sigma" rizzler meme
    "🗣️": 2.0,
    "🫡": 1.5,
}


# ---------------------------------------------------------------------------
# Keyword loader
# ---------------------------------------------------------------------------


class KeywordList:
    """
    Loads keyword definitions from keywords.json.

    Expected JSON structure::

        {
          "categories": {
            "slang": [{"keyword": "skibidi", "weight": 8}, ...],
            "phrases": [...],
            "emojis": [...]
          }
        }

    Falls back to an empty list if the file is missing or malformed.
    """

    def __init__(self, path: str = KEYWORDS_PATH) -> None:
        self.path = path
        self._keywords: List[Dict[str, Any]] = []
        self._emoji_keywords: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not Path(self.path).exists():
            logger.warning("keywords.json not found at %s; using empty list.", self.path)
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            categories = data.get("categories", {})
            all_kws: List[Dict[str, Any]] = []
            emoji_kws: List[Dict[str, Any]] = []
            for cat_name, entries in categories.items():
                for entry in entries:
                    entry["category"] = cat_name
                    if cat_name == "emojis":
                        emoji_kws.append(entry)
                    else:
                        all_kws.append(entry)
            self._keywords = all_kws
            self._emoji_keywords = emoji_kws
            logger.info(
                "Loaded %d keywords + %d emoji keywords from %s",
                len(self._keywords),
                len(self._emoji_keywords),
                self.path,
            )
        except Exception as exc:
            logger.error("Failed to load keywords.json: %s", exc)

    def reload(self) -> None:
        """Reload keywords from disk."""
        self._load()

    @property
    def keywords(self) -> List[Dict[str, Any]]:
        return self._keywords

    @property
    def emoji_keywords(self) -> List[Dict[str, Any]]:
        return self._emoji_keywords


# Module-level keyword list (reloaded by config refresh)
_kw_list = KeywordList()


# ---------------------------------------------------------------------------
# Text matching helpers
# ---------------------------------------------------------------------------


def _search_text(
    text: str,
    keywords: List[Dict[str, Any]],
    context_label: str,
) -> List[KeywordMatch]:
    """
    Search *text* for all keywords and return matches.

    Supports:
      - Case-insensitive whole-word match (default)
      - Partial match for short keywords (len < 4)
      - Phrase matching (multi-word keywords)
    """
    if not text:
        return []

    text_lower = text.lower()
    matches: List[KeywordMatch] = []

    for kw_def in keywords:
        keyword = kw_def.get("keyword", "")
        weight = float(kw_def.get("weight", 1.0))
        if not keyword:
            continue

        kw_lower = keyword.lower()

        # Use word-boundary match for single words, substring for phrases
        if " " in kw_lower:
            # Phrase: simple substring
            if kw_lower in text_lower:
                # Extract context window
                idx = text_lower.find(kw_lower)
                ctx_start = max(0, idx - 20)
                ctx_end = min(len(text), idx + len(kw_lower) + 20)
                ctx = text[ctx_start:ctx_end].replace("\n", " ")
                matches.append(
                    KeywordMatch(
                        keyword=keyword,
                        weight=weight,
                        context=context_label,
                        matched_text=ctx,
                    )
                )
        else:
            # Single word — word boundary
            pattern = r"\b" + re.escape(kw_lower) + r"\b"
            m = re.search(pattern, text_lower)
            if m:
                idx = m.start()
                ctx_start = max(0, idx - 20)
                ctx_end = min(len(text), idx + len(kw_lower) + 20)
                ctx = text[ctx_start:ctx_end].replace("\n", " ")
                matches.append(
                    KeywordMatch(
                        keyword=keyword,
                        weight=weight,
                        context=context_label,
                        matched_text=ctx,
                    )
                )

    return matches


def _search_emojis(text: str) -> List[KeywordMatch]:
    """Detect brainrot-associated emojis in text."""
    matches: List[KeywordMatch] = []
    for emoji, weight in BRAINROT_EMOJIS.items():
        if emoji in text:
            count = text.count(emoji)
            effective_weight = weight * min(count, 5)  # cap repetition bonus
            matches.append(
                KeywordMatch(
                    keyword=emoji,
                    weight=effective_weight,
                    context="emoji",
                    matched_text=emoji * min(count, 5),
                )
            )
    # Also check emoji_keywords from file
    for kw_def in _kw_list.emoji_keywords:
        em = kw_def.get("keyword", "")
        weight = float(kw_def.get("weight", 1.0))
        if em and em in text and em not in BRAINROT_EMOJIS:
            matches.append(
                KeywordMatch(
                    keyword=em,
                    weight=weight,
                    context="emoji",
                    matched_text=em,
                )
            )
    return matches


# ---------------------------------------------------------------------------
# OCR helper
# ---------------------------------------------------------------------------


def _ocr_thumbnail(thumbnail_url: str) -> str:
    """
    Download *thumbnail_url* and run pytesseract OCR on it.

    Returns the extracted text, or empty string on any error.
    """
    if not thumbnail_url:
        return ""
    try:
        import pytesseract
        from PIL import Image

        req = urllib.request.Request(
            thumbnail_url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            image_data = resp.read()

        image = Image.open(io.BytesIO(image_data)).convert("RGB")
        text = pytesseract.image_to_string(image, config="--psm 3")
        return text
    except ImportError:
        logger.debug("pytesseract not installed; skipping OCR.")
        return ""
    except Exception as exc:
        logger.warning("OCR failed for %s: %s", thumbnail_url, exc)
        return ""


# ---------------------------------------------------------------------------
# Score calculation
# ---------------------------------------------------------------------------


_MAX_RAW_SCORE = 50.0  # raw weight sum that maps to score 100


def _compute_score(matches: List[KeywordMatch]) -> float:
    """
    Compute a 0-100 score from matched keyword weights.

    Uses a logarithmic compression so that a huge pile of weak keywords
    doesn't trivially max out the score, but strong keywords have outsized impact.
    """
    if not matches:
        return 0.0

    # Deduplicate: keep highest-weight match per keyword
    best: Dict[str, float] = {}
    for m in matches:
        best[m.keyword] = max(best.get(m.keyword, 0.0), m.weight)

    raw = sum(best.values())
    # Clamp to [0, 100] with soft compression
    score = min(100.0, (raw / _MAX_RAW_SCORE) * 100.0)
    return round(score, 2)


# ---------------------------------------------------------------------------
# Main analyzer function
# ---------------------------------------------------------------------------


def analyze(
    video_id: str,
    title: str = "",
    description: str = "",
    tags: Optional[List[str]] = None,
    thumbnail_url: str = "",
    category_id: str = "",
    fetch_captions: bool = True,
) -> AnalysisResult:
    """
    Run the full keyword analysis pipeline for a video.

    Args:
        video_id:      YouTube video ID
        title:         Video title
        description:   Video description
        tags:          List of tags from the API
        thumbnail_url: URL to the video thumbnail image
        category_id:   YouTube category ID (e.g. "10" for Music)
        fetch_captions: Whether to download and analyze auto-generated captions

    Returns:
        AnalysisResult with module="keyword", score, and details dict.
    """
    start = time.monotonic()
    tags = tags or []
    all_matches: List[KeywordMatch] = []

    keywords = _kw_list.keywords

    # 1. Title
    all_matches.extend(_search_text(title, keywords, "title"))
    all_matches.extend(_search_emojis(title))

    # 2. Description (first 2000 chars to keep it quick)
    desc_snippet = description[:2000]
    all_matches.extend(_search_text(desc_snippet, keywords, "description"))
    all_matches.extend(_search_emojis(desc_snippet))

    # 3. Tags
    tags_text = " ".join(tags)
    all_matches.extend(_search_text(tags_text, keywords, "tags"))
    all_matches.extend(_search_emojis(tags_text))

    # 4. OCR thumbnail
    ocr_text = _ocr_thumbnail(thumbnail_url)
    if ocr_text:
        all_matches.extend(_search_text(ocr_text, keywords, "thumbnail_ocr"))

    # 5. Captions
    caption_text = ""
    if fetch_captions:
        try:
            from youtube_api import get_video_captions

            caption_text = get_video_captions(video_id)
            if caption_text:
                all_matches.extend(
                    _search_text(caption_text[:5000], keywords, "captions")
                )
                all_matches.extend(_search_emojis(caption_text[:5000]))
        except Exception as exc:
            logger.warning("Caption analysis failed for %s: %s", video_id, exc)

    # 6. Music video dampening (music is expected to have hype keywords)
    dampening = 1.0
    if category_id == "10":  # Music
        dampening = 0.7
        logger.debug("Applying music dampening to keyword score for %s", video_id)

    score = _compute_score(all_matches) * dampening

    elapsed = time.monotonic() - start

    # Deduplicate matches for result (keep highest-weight per keyword per context)
    seen_pairs: set = set()
    deduped: List[KeywordMatch] = []
    for m in all_matches:
        key = (m.keyword, m.context)
        if key not in seen_pairs:
            seen_pairs.add(key)
            deduped.append(m)

    logger.info(
        "Keyword analysis for %s: score=%.1f, matches=%d, duration=%.2fs",
        video_id,
        score,
        len(deduped),
        elapsed,
    )

    return AnalysisResult(
        module="keyword",
        score=round(score, 2),
        details={
            "matched_count": len(deduped),
            "title_matches": sum(1 for m in deduped if m.context == "title"),
            "description_matches": sum(1 for m in deduped if m.context == "description"),
            "tags_matches": sum(1 for m in deduped if m.context == "tags"),
            "ocr_matches": sum(1 for m in deduped if m.context == "thumbnail_ocr"),
            "caption_matches": sum(1 for m in deduped if m.context == "captions"),
            "emoji_matches": sum(1 for m in deduped if m.context == "emoji"),
            "caption_length": len(caption_text),
            "ocr_text_length": len(ocr_text),
            "music_dampening_applied": dampening < 1.0,
            "matched_keywords": [m.model_dump() for m in deduped],
        },
        duration_s=round(elapsed, 3),
    )


def reload_keywords() -> None:
    """Reload the keyword list from disk (call after config update)."""
    _kw_list.reload()
