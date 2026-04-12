"""
comment_analyzer.py - Comment sentiment and brainrot signal analysis.

Pipeline:
  1. Fetch top 50 comments via YouTube Data API v3 (commentThreads.list)
  2. Keyword density: match comments against brainrot keywords (keywords.json)
  3. Emoji flooding: emoji-to-text ratio across all comment text
  4. Comment quality metrics: avg length, unique-word ratio, caps-lock ratio
  5. Repetition detection: near-duplicate comment detection
  6. Toxicity signals: basic profanity/toxicity keyword matching
  7. Aggregate sub-scores → comment_score (0-100)

Sub-score weights:
  keyword_density_score  (0-40)
  emoji_flood_score      (0-20)
  quality_score          (0-20)
  repetition_score       (0-20)
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

from config import KEYWORDS_PATH
from models import AnalysisResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

YOUTUBE_COMMENTS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
MAX_COMMENTS = 50
API_TIMEOUT = 15  # seconds

# ---------------------------------------------------------------------------
# Emoji detection regex
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile(
    r"[\U0001F600-\U0001F64F"
    r"\U0001F300-\U0001F5FF"
    r"\U0001F680-\U0001F6FF"
    r"\U0001F700-\U0001F77F"
    r"\U0001F780-\U0001F7FF"
    r"\U0001F800-\U0001F8FF"
    r"\U0001F900-\U0001F9FF"
    r"\U0001FA00-\U0001FA6F"
    r"\U0001FA70-\U0001FAFF"
    r"\u2600-\u26FF"
    r"\u2700-\u27BF"
    r"]",
    re.UNICODE,
)

# Basic toxicity / profanity signals (low-fidelity, intentionally conservative)
_TOXICITY_PATTERNS = re.compile(
    r"\b(?:kys|kill\s+yourself|f[u4]ck\s+you|shut\s+up|idiot|moron|retard|loser|cringe)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Keyword loading helper (reuse keywords.json)
# ---------------------------------------------------------------------------


def _load_keywords(path: str = KEYWORDS_PATH) -> List[str]:
    """
    Load flat list of brainrot keyword strings from keywords.json.

    Returns an empty list on failure so analysis degrades gracefully.
    """
    if not Path(path).exists():
        logger.warning("keywords.json not found at %s; comment keyword analysis disabled.", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        keywords: List[str] = []
        for entries in data.get("categories", {}).values():
            for entry in entries:
                kw = entry.get("keyword", "").lower().strip()
                if kw:
                    keywords.append(kw)
        logger.debug("Loaded %d keywords for comment analysis.", len(keywords))
        return keywords
    except Exception as exc:
        logger.error("Failed to load keywords.json for comment analysis: %s", exc)
        return []


# Module-level keyword list (loaded once)
_KEYWORDS: List[str] = _load_keywords()


def reload_keywords() -> None:
    """Reload the keyword list from disk."""
    global _KEYWORDS
    _KEYWORDS = _load_keywords()


# ---------------------------------------------------------------------------
# YouTube API: fetch comments
# ---------------------------------------------------------------------------


def _fetch_comments(video_id: str, api_key: str) -> Tuple[List[str], str]:
    """
    Fetch up to MAX_COMMENTS top comments for *video_id* via the YouTube API.

    Returns:
        (list_of_comment_texts, error_message_or_empty_string)
    """
    if not api_key:
        return [], "no_api_key"

    params = {
        "part": "snippet",
        "videoId": video_id,
        "maxResults": str(MAX_COMMENTS),
        "order": "relevance",
        "textFormat": "plainText",
        "key": api_key,
    }
    url = f"{YOUTUBE_COMMENTS_URL}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BrainrotFilter/1.0"})
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 403:
            # Might be comments disabled or quota exceeded
            if "commentsDisabled" in body:
                logger.info("Comments disabled for video %s.", video_id)
                return [], "comments_disabled"
            if "quotaExceeded" in body:
                logger.warning("YouTube API quota exceeded for video %s.", video_id)
                return [], "quota_exceeded"
            return [], f"http_403: {body}"
        return [], f"http_{exc.code}: {body}"
    except Exception as exc:
        logger.warning("Failed to fetch comments for %s: %s", video_id, exc)
        return [], str(exc)

    comments: List[str] = []
    for item in data.get("items", []):
        try:
            text = (
                item["snippet"]["topLevelComment"]["snippet"].get("textDisplay", "")
            )
            if text:
                comments.append(text)
        except (KeyError, TypeError):
            continue

    return comments, ""


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def _count_emojis(text: str) -> int:
    return len(_EMOJI_RE.findall(text))


def _count_words(text: str) -> int:
    return len(re.findall(r"\w+", text))


def _keyword_density_score(comments: List[str], keywords: List[str]) -> Tuple[float, float, List[str]]:
    """
    Compute keyword density score (0-40) across all comments.

    Returns:
        (score, density_ratio, top_matched_keywords)
    """
    if not comments or not keywords:
        return 0.0, 0.0, []

    all_text = " ".join(comments).lower()
    total_words = max(_count_words(all_text), 1)

    kw_counter: Counter = Counter()
    for kw in keywords:
        # Simple substring count for short keywords, word-boundary for longer
        if len(kw) <= 3:
            count = all_text.count(kw)
        else:
            count = len(re.findall(r"\b" + re.escape(kw) + r"\b", all_text))
        if count > 0:
            kw_counter[kw] += count

    total_matches = sum(kw_counter.values())
    density = total_matches / total_words

    # Map density to 0-40 score: density=0.01 → ~10, density=0.05 → ~40
    score = min(40.0, density * 800.0)

    top_keywords = [kw for kw, _ in kw_counter.most_common(10)]
    return round(score, 2), round(density, 5), top_keywords


def _emoji_flood_score(comments: List[str]) -> Tuple[float, float]:
    """
    Compute emoji flooding score (0-20).

    Returns:
        (score, emoji_to_word_ratio)
    """
    if not comments:
        return 0.0, 0.0

    total_emojis = sum(_count_emojis(c) for c in comments)
    total_words = max(sum(_count_words(c) for c in comments), 1)
    ratio = total_emojis / total_words

    # Map ratio to 0-20 score: ratio=0.05 → ~5, ratio=0.3+ → 20
    score = min(20.0, ratio * 67.0)
    return round(score, 2), round(ratio, 5)


def _quality_score(comments: List[str]) -> Tuple[float, Dict[str, float]]:
    """
    Compute comment quality score (0-20).
    Higher score = lower quality = more brainrot signal.

    Metrics:
      - avg_length: very short comments = lower quality
      - unique_words_ratio: low uniqueness = repetitive / bot-like
      - caps_ratio: high = chaotic / shouting

    Returns:
        (score, metrics_dict)
    """
    if not comments:
        return 0.0, {}

    lengths = [len(c) for c in comments]
    avg_length = sum(lengths) / len(lengths)

    all_words = re.findall(r"\w+", " ".join(comments).lower())
    total_words = max(len(all_words), 1)
    unique_ratio = len(set(all_words)) / total_words

    alpha_chars = [c for text in comments for c in text if c.isalpha()]
    caps_ratio = (
        sum(1 for c in alpha_chars if c.isupper()) / max(len(alpha_chars), 1)
    )

    # Very short avg length (< 15 chars) → higher brainrot signal
    length_component = max(0.0, (15.0 - avg_length) / 15.0) * 7.0   # 0-7

    # Low unique words ratio (< 0.4) → more repetitive → higher signal
    uniqueness_component = max(0.0, (0.4 - unique_ratio) / 0.4) * 7.0  # 0-7

    # High caps ratio (> 0.3) → shouting / chaotic
    caps_component = min(1.0, caps_ratio / 0.3) * 6.0  # 0-6

    score = length_component + uniqueness_component + caps_component

    metrics = {
        "avg_comment_length": round(avg_length, 1),
        "unique_words_ratio": round(unique_ratio, 3),
        "caps_ratio": round(caps_ratio, 3),
        "length_component": round(length_component, 2),
        "uniqueness_component": round(uniqueness_component, 2),
        "caps_component": round(caps_component, 2),
    }

    return round(min(score, 20.0), 2), metrics


def _repetition_score(comments: List[str]) -> Tuple[float, float]:
    """
    Detect duplicate / near-duplicate comments (spam/bot-like or copy-paste brainrot).

    Uses normalized text fingerprinting: strip whitespace, lowercase, remove punctuation.

    Returns:
        (score_0_to_20, repetition_ratio)
    """
    if not comments:
        return 0.0, 0.0

    def _fingerprint(text: str) -> str:
        text = text.lower()
        text = re.sub(r"[^\w\s]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    fps = [_fingerprint(c) for c in comments if _fingerprint(c)]
    if not fps:
        return 0.0, 0.0

    total = len(fps)
    counter: Counter = Counter(fps)
    duplicates = sum(v - 1 for v in counter.values() if v > 1)
    ratio = duplicates / total

    score = min(20.0, ratio * 100.0)
    return round(score, 2), round(ratio, 4)


def _toxicity_count(comments: List[str]) -> int:
    """Count comments containing toxicity signals."""
    return sum(1 for c in comments if _TOXICITY_PATTERNS.search(c))


# ---------------------------------------------------------------------------
# Main analyze function
# ---------------------------------------------------------------------------


def analyze(video_id: str, api_key: str = "") -> AnalysisResult:
    """
    Fetch and analyze YouTube comments for brainrot signals.

    Args:
        video_id: YouTube video ID.
        api_key:  YouTube Data API v3 key. If empty, returns a zero score
                  with an explanatory error note.

    Returns:
        AnalysisResult with module="comment", score (0-100), and details dict
        containing: comments_fetched, comments_analyzed, keyword_density,
        top_matched_keywords, emoji_ratio, avg_comment_length,
        repetition_ratio, score_breakdown.
    """
    start = time.monotonic()

    # ----------------------------------------------------------------
    # Fetch comments
    # ----------------------------------------------------------------
    comments, fetch_error = _fetch_comments(video_id, api_key)

    if not comments:
        elapsed = time.monotonic() - start
        logger.info(
            "Comment analysis for %s skipped: %s",
            video_id,
            fetch_error or "no comments returned",
        )
        return AnalysisResult(
            module="comment",
            score=0.0,
            details={
                "comments_fetched": 0,
                "comments_analyzed": 0,
                "keyword_density": 0.0,
                "top_matched_keywords": [],
                "emoji_ratio": 0.0,
                "avg_comment_length": 0.0,
                "repetition_ratio": 0.0,
                "toxicity_count": 0,
                "score_breakdown": {
                    "keyword_density_score": 0.0,
                    "emoji_flood_score": 0.0,
                    "quality_score": 0.0,
                    "repetition_score": 0.0,
                },
                "fetch_error": fetch_error or "no_comments",
            },
            error=fetch_error or None,
            duration_s=round(elapsed, 3),
        )

    # ----------------------------------------------------------------
    # Run analysis sub-modules
    # ----------------------------------------------------------------
    kw_score, kw_density, top_keywords = _keyword_density_score(comments, _KEYWORDS)
    emoji_score, emoji_ratio = _emoji_flood_score(comments)
    qual_score, qual_metrics = _quality_score(comments)
    rep_score, rep_ratio = _repetition_score(comments)
    tox_count = _toxicity_count(comments)

    total_score = min(100.0, kw_score + emoji_score + qual_score + rep_score)

    elapsed = time.monotonic() - start

    logger.info(
        "Comment analysis for %s: score=%.1f (kw=%.1f, emoji=%.1f, qual=%.1f, rep=%.1f), "
        "comments=%d, duration=%.2fs",
        video_id,
        total_score,
        kw_score,
        emoji_score,
        qual_score,
        rep_score,
        len(comments),
        elapsed,
    )

    return AnalysisResult(
        module="comment",
        score=round(total_score, 2),
        details={
            "comments_fetched": len(comments),
            "comments_analyzed": len(comments),
            "keyword_density": kw_density,
            "top_matched_keywords": top_keywords,
            "emoji_ratio": emoji_ratio,
            "avg_comment_length": qual_metrics.get("avg_comment_length", 0.0),
            "unique_words_ratio": qual_metrics.get("unique_words_ratio", 0.0),
            "caps_ratio": qual_metrics.get("caps_ratio", 0.0),
            "repetition_ratio": rep_ratio,
            "toxicity_count": tox_count,
            "fetch_error": fetch_error or None,
            "score_breakdown": {
                "keyword_density_score": kw_score,
                "emoji_flood_score": emoji_score,
                "quality_score": qual_score,
                "repetition_score": rep_score,
            },
        },
        duration_s=round(elapsed, 3),
    )
