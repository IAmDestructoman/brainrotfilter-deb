"""
parallel_analyzer.py — Parallelized drop-in replacement for _run_analysis().

Replaces the sequential keyword → scene → audio pipeline with a two-phase
concurrent execution model that minimises total wall-clock time.

──────────────────────────────────────────────────────────────────────────────
Architecture:

  Phase 1 (concurrent, no video download needed):
    ├── YouTube API metadata fetch
    ├── Keyword analysis (uses metadata)
    ├── Comment analysis (uses YouTube API — no video download)
    ├── Engagement analysis (uses metadata only)
    └── Shorts detection (uses URL + metadata)

  Phase 1.5 (thumbnail download — lightweight, parallel with Phase 2):
    └── Thumbnail download + visual analysis

  Phase 2 (concurrent, after one shared video download):
    ├── [Early exit check] — if scores alone are conclusive, skip Phase 2
    ├── Download video once  →  shared temp file
    ├── Scene analysis       ┐  run in parallel on
    └── Audio analysis       ┘  the same downloaded file

  Early termination:
    • If keyword_score > block_score_min:
        Block immediately — skip download + analysis entirely.
    • If max_possible_combined_score < allow_threshold:
        Allow immediately — no point in downloading.
    • Per-phase and global timeouts prevent runaway analysis.

  GPU routing:
    • Auto-detects CUDA via gpu_utils.detect_gpu()
    • If GPU available and Whisper installed → uses Whisper for STT
    • Hints OpenCV to use CUDA video backend if available

  Scoring weights (configurable, defaults):
    keyword:    0.25
    scene:      0.20
    audio:      0.15
    comment:    0.15
    engagement: 0.10
    thumbnail:  0.10
    shorts:     additive bonus (0-15 pts)
    ml:         0.0 (disabled; user enables after training)

──────────────────────────────────────────────────────────────────────────────
Public API:

  parallel_analyze(video_id: str) -> None
      Main entry point.  Matches the signature expected by AnalysisQueue._worker.

  _phase1_metadata_and_keywords(video_id: str) -> dict
  _phase1_5_thumbnail_analysis(thumbnail_url, video_id) -> dict
  _phase2_download_and_analyze(video_id, metadata, keyword_result) -> dict
  _download_video_once(video_id, duration_seconds) -> str | None
  _can_skip_download(keyword_score, config_snapshot) -> tuple[bool, str]
  _detect_gpu() -> dict

──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import config
from gpu_utils import configure_opencv_cuda, detect_gpu, get_whisper_model
from models import (
    SceneDetails,
    VideoAnalysis,
    VideoStatus,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration snapshot (read once per analysis run to avoid mid-run changes)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _ConfigSnapshot:
    keyword_threshold: float
    scene_threshold: float
    audio_threshold: float
    block_score_min: float
    soft_block_score_min: float
    monitor_score_min: float
    weight_keyword: float
    weight_scene: float
    weight_audio: float
    weight_comment: float
    weight_engagement: float
    weight_thumbnail: float
    weight_ml: float
    ml_enabled: bool
    shorts_bonus_confirmed: int
    shorts_bonus_likely: int
    initial_scan_duration: int
    full_scan_time_limit: int
    total_analysis_timeout: int
    phase1_timeout: int
    phase2_timeout: int

    @classmethod
    def from_config(cls) -> "_ConfigSnapshot":
        return cls(
            keyword_threshold=config.get_float("keyword_threshold"),
            scene_threshold=config.get_float("scene_threshold"),
            audio_threshold=config.get_float("audio_threshold"),
            block_score_min=config.get_float("block_score_min"),
            soft_block_score_min=config.get_float("soft_block_score_min"),
            monitor_score_min=config.get_float("monitor_score_min"),
            weight_keyword=config.get_float("weight_keyword"),
            weight_scene=config.get_float("weight_scene"),
            weight_audio=config.get_float("weight_audio"),
            weight_comment=config.get_float("weight_comment"),
            weight_engagement=config.get_float("weight_engagement"),
            weight_thumbnail=config.get_float("weight_thumbnail"),
            weight_ml=config.get_float("weight_ml"),
            ml_enabled=config.get_bool("ml_enabled"),
            shorts_bonus_confirmed=config.get_int("shorts_bonus_confirmed"),
            shorts_bonus_likely=config.get_int("shorts_bonus_likely"),
            initial_scan_duration=config.get_int("initial_scan_duration"),
            full_scan_time_limit=config.get_int("full_scan_time_limit"),
            total_analysis_timeout=int(config.get("parallel_analysis_timeout", 180)),
            phase1_timeout=int(config.get("phase1_timeout", 30)),
            phase2_timeout=int(config.get("phase2_timeout", 150)),
        )

    def max_possible_combined(
        self,
        keyword_score: float,
        assume_scene: float = 100.0,
        assume_audio: float = 100.0,
        assume_comment: float = 100.0,
        assume_engagement: float = 100.0,
        assume_thumbnail: float = 100.0,
    ) -> float:
        """
        Upper bound on combined score across all analyzers.
        Used for early-allow termination: if even worst-case scores cannot
        reach the monitor threshold, there is nothing to act on.
        """
        return (
            keyword_score * self.weight_keyword
            + assume_scene * self.weight_scene
            + assume_audio * self.weight_audio
            + assume_comment * self.weight_comment
            + assume_engagement * self.weight_engagement
            + assume_thumbnail * self.weight_thumbnail
        )

    def compute_combined(
        self,
        kw: float,
        sc: float,
        au: float,
        comment: float = 0.0,
        engagement: float = 0.0,
        thumbnail: float = 0.0,
        shorts_bonus: float = 0.0,
        ml: float = 0.0,
    ) -> float:
        base = (
            kw * self.weight_keyword
            + sc * self.weight_scene
            + au * self.weight_audio
            + comment * self.weight_comment
            + engagement * self.weight_engagement
            + thumbnail * self.weight_thumbnail
        )
        if self.ml_enabled and self.weight_ml > 0:
            base = base * (1.0 - self.weight_ml) + ml * self.weight_ml
        return round(min(max(base + shorts_bonus, 0.0), 100.0), 2)


# ─────────────────────────────────────────────────────────────────────────────
# GPU detection wrapper (thin layer over gpu_utils)
# ─────────────────────────────────────────────────────────────────────────────


def _detect_gpu() -> Dict[str, Any]:
    """
    Return GPU capability info.  Thin wrapper over gpu_utils.detect_gpu()
    so callers in this module use a consistent local interface.

    Returns:
        dict with keys: has_cuda, cuda_version, gpu_name, gpu_memory_mb,
                        has_whisper, has_vosk, use_whisper, device
    """
    return detect_gpu()


# ─────────────────────────────────────────────────────────────────────────────
# Early termination helpers
# ─────────────────────────────────────────────────────────────────────────────


def _can_skip_download(
    keyword_score: float,
    cfg: _ConfigSnapshot,
    comment_score: float = 0.0,
    engagement_score: float = 0.0,
    shorts_bonus: float = 0.0,
) -> Tuple[bool, str]:
    """
    Determine whether the video download + expensive analysis can be skipped.

    Returns:
        (should_skip: bool, reason: str)

    Two conditions trigger a skip:
      1. Combined Phase 1 score (keyword + comment + engagement + shorts_bonus)
         already exceeds the block threshold — block immediately.
      2. Even with worst-case scene+audio+thumbnail scores, the combined score
         cannot reach the monitor threshold — allow immediately.
    """
    # Condition 1: Phase 1 scores alone produce a certain block
    phase1_combined = cfg.compute_combined(
        kw=keyword_score,
        sc=0.0,
        au=0.0,
        comment=comment_score,
        engagement=engagement_score,
        shorts_bonus=shorts_bonus,
    )
    if phase1_combined >= cfg.block_score_min:
        return True, (
            f"phase1_combined={phase1_combined:.1f} >= block_min={cfg.block_score_min}"
        )

    # Condition 2: even maximum scene+audio+thumbnail cannot change the outcome
    max_possible = cfg.max_possible_combined(
        keyword_score=keyword_score,
        assume_comment=comment_score,
        assume_engagement=engagement_score,
        assume_thumbnail=100.0,
    )
    # Add shorts bonus on top
    max_possible_with_bonus = min(100.0, max_possible + shorts_bonus)
    if max_possible_with_bonus < cfg.monitor_score_min:
        return True, (
            f"max_possible_combined={max_possible_with_bonus:.1f} < monitor_min={cfg.monitor_score_min}"
        )

    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: metadata + keywords + thumbnail — all concurrent
# ─────────────────────────────────────────────────────────────────────────────


def _phase1_metadata_and_keywords(video_id: str) -> Dict[str, Any]:
    """
    Fetch YouTube metadata and run all lightweight (no-download) analyzers:
    keyword analysis, comment analysis, engagement analysis, and Shorts
    detection — all concurrently.

    No video download is performed in this phase.

    Returns a dict with keys:
        yt_data          : dict | None   — raw YouTube API response
        title            : str
        description      : str
        tags             : list[str]
        category_id      : str
        channel_id       : str
        thumbnail_url    : str
        duration_seconds : int
        view_count       : int
        like_count       : int
        comment_count    : int
        keyword_score    : float
        keyword_matches  : list
        keyword_details  : dict
        comment_score    : float
        comment_details  : dict
        engagement_score : float
        engagement_details: dict
        shorts_bonus     : float
        shorts_details   : dict
        phase1_elapsed   : float
        phase1_errors    : list[str]
    """
    start = time.monotonic()
    errors: List[str] = []

    # ── Sub-task 1: YouTube metadata ──────────────────────────────────────────
    def _fetch_metadata() -> Optional[Dict[str, Any]]:
        try:
            from youtube_api import get_video_details  # type: ignore
            return get_video_details(video_id)
        except Exception as exc:
            errors.append(f"metadata:{exc}")
            logger.warning("Phase1 metadata fetch failed for %s: %s", video_id, exc)
            return None

    # Run metadata fetch first — all other Phase 1 tasks depend on it.
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="p1") as pool:

        meta_future: Future = pool.submit(_fetch_metadata)
        yt_data = meta_future.result()  # blocks; others need metadata fields

        thumbnail_url = (yt_data or {}).get("thumbnail_url", "")
        title = (yt_data or {}).get("title", "")
        description = (yt_data or {}).get("description", "")
        tags = (yt_data or {}).get("tags", [])
        category_id = (yt_data or {}).get("category_id", "")
        channel_id = (yt_data or {}).get("channel_id", "")
        duration_seconds = int((yt_data or {}).get("duration_seconds", 0))
        view_count = int((yt_data or {}).get("view_count", 0))
        like_count = int((yt_data or {}).get("like_count", 0))
        comment_count = int((yt_data or {}).get("comment_count", 0))

        # ── Sub-task 2: Keyword analysis ──────────────────────────────────────
        def _run_keyword_analysis() -> Any:
            try:
                import keyword_analyzer  # type: ignore
                return keyword_analyzer.analyze(
                    video_id=video_id,
                    title=title,
                    description=description,
                    tags=tags,
                    thumbnail_url=thumbnail_url,
                    category_id=category_id,
                )
            except Exception as exc:
                errors.append(f"keyword:{exc}")
                logger.error("Phase1 keyword analysis failed for %s: %s", video_id, exc)
                return None

        # ── Sub-task 3: Comment analysis (YouTube API, no download) ───────────
        def _run_comment_analysis() -> Tuple[float, Dict[str, Any]]:
            try:
                import comment_analyzer  # type: ignore
                result = comment_analyzer.analyze(
                    video_id=video_id,
                    api_key=config.get_str("youtube_api_key"),
                )
                return float(result.score), result.details or {}
            except ImportError:
                logger.debug("comment_analyzer not available; comment_score=0.")
                return 0.0, {"error": "module_unavailable"}
            except Exception as exc:
                errors.append(f"comment:{exc}")
                logger.warning("Comment analysis failed for %s: %s", video_id, exc)
                return 0.0, {"error": str(exc)}

        # ── Sub-task 4: Engagement analysis (metadata only) ───────────────────
        def _run_engagement_analysis() -> Tuple[float, Dict[str, Any]]:
            try:
                import engagement_analyzer  # type: ignore
                result = engagement_analyzer.analyze(
                    video_id=video_id,
                    video_data={
                        "view_count": view_count,
                        "like_count": like_count,
                        "comment_count": comment_count,
                        "duration": duration_seconds,
                        "title": title,
                        "published_at": (yt_data or {}).get("published_at", ""),
                    },
                )
                return float(result.score), result.details or {}
            except ImportError:
                logger.debug("engagement_analyzer not available; engagement_score=0.")
                return 0.0, {"error": "module_unavailable"}
            except Exception as exc:
                errors.append(f"engagement:{exc}")
                logger.warning("Engagement analysis failed for %s: %s", video_id, exc)
                return 0.0, {"error": str(exc)}

        # ── Sub-task 5: Shorts detection (URL + metadata heuristics) ──────────
        def _run_shorts_detection() -> Tuple[float, Dict[str, Any]]:
            try:
                import shorts_detector  # type: ignore
                result = shorts_detector.detect(
                    video_id=video_id,
                    duration_seconds=duration_seconds,
                    title=title,
                    description=description,
                    tags=tags,
                )
                return float(result.bonus), result.details or {}
            except ImportError:
                # Fallback: inline heuristic shorts detection
                return _inline_shorts_heuristic(
                    video_id, duration_seconds, title, description, tags
                )
            except Exception as exc:
                errors.append(f"shorts:{exc}")
                logger.debug("Shorts detection failed for %s: %s", video_id, exc)
                return 0.0, {"error": str(exc)}

        # Launch all Phase 1 sub-tasks in parallel
        kw_future:   Future = pool.submit(_run_keyword_analysis)
        co_future:   Future = pool.submit(_run_comment_analysis)
        eng_future:  Future = pool.submit(_run_engagement_analysis)
        sh_future:   Future = pool.submit(_run_shorts_detection)

        # Collect results with individual timeouts
        kw_result = None
        comment_score = 0.0
        comment_details: Dict[str, Any] = {}
        engagement_score = 0.0
        engagement_details: Dict[str, Any] = {}
        shorts_bonus = 0.0
        shorts_details: Dict[str, Any] = {}

        try:
            kw_result = kw_future.result(timeout=25)
        except FuturesTimeoutError:
            errors.append("keyword:timeout")
            logger.warning("Keyword analysis timed out for %s.", video_id)

        try:
            comment_score, comment_details = co_future.result(timeout=20)
        except FuturesTimeoutError:
            errors.append("comment:timeout")
            comment_details = {"error": "timeout"}
            logger.warning("Comment analysis timed out for %s.", video_id)

        try:
            engagement_score, engagement_details = eng_future.result(timeout=15)
        except FuturesTimeoutError:
            errors.append("engagement:timeout")
            engagement_details = {"error": "timeout"}
            logger.warning("Engagement analysis timed out for %s.", video_id)

        try:
            shorts_bonus, shorts_details = sh_future.result(timeout=10)
        except FuturesTimeoutError:
            errors.append("shorts:timeout")
            shorts_details = {"error": "timeout"}
            logger.warning("Shorts detection timed out for %s.", video_id)

    keyword_score = 0.0
    keyword_matches: List[Any] = []
    keyword_details: Dict[str, Any] = {}
    if kw_result is not None:
        keyword_score = float(kw_result.score)
        keyword_details = kw_result.details or {}
        keyword_matches = keyword_details.get("matched_keywords", [])

    phase1_elapsed = round(time.monotonic() - start, 3)
    logger.info(
        "Phase1 complete for %s: kw=%.1f co=%.1f eng=%.1f shorts_bonus=%.0f elapsed=%.2fs%s",
        video_id,
        keyword_score,
        comment_score,
        engagement_score,
        shorts_bonus,
        phase1_elapsed,
        f" errors={errors}" if errors else "",
    )

    return {
        "yt_data": yt_data,
        "title": title,
        "description": description,
        "tags": tags,
        "category_id": category_id,
        "channel_id": channel_id,
        "thumbnail_url": thumbnail_url,
        "duration_seconds": duration_seconds,
        "view_count": view_count,
        "like_count": like_count,
        "comment_count": comment_count,
        "keyword_score": keyword_score,
        "keyword_matches": keyword_matches,
        "keyword_details": keyword_details,
        "comment_score": comment_score,
        "comment_details": comment_details,
        "engagement_score": engagement_score,
        "engagement_details": engagement_details,
        "shorts_bonus": shorts_bonus,
        "shorts_details": shorts_details,
        "phase1_elapsed": phase1_elapsed,
        "phase1_errors": errors,
    }


def _inline_shorts_heuristic(
    video_id: str,
    duration_seconds: int,
    title: str,
    description: str,
    tags: List[str],
) -> Tuple[float, Dict[str, Any]]:
    """
    Fallback Shorts detection when shorts_detector module is not installed.

    Checks:
    1. Duration <= 60s → likely Shorts
    2. '#shorts' in title, description, or tags → confirmed Shorts
    3. Duration <= 120s + brainrot title patterns → likely Shorts

    Returns (bonus_score, details_dict).
    """
    cfg = config
    is_confirmed = False
    is_likely = False
    reasons: List[str] = []

    # Signal 1: explicit #shorts tag
    all_text = " ".join([title, description] + tags).lower()
    if "#shorts" in all_text or "#short" in all_text:
        is_confirmed = True
        reasons.append("#shorts_tag")

    # Signal 2: sub-60s video
    if duration_seconds > 0 and duration_seconds <= 60:
        is_confirmed = True
        reasons.append(f"duration={duration_seconds}s")

    # Signal 3: sub-120s with brainrot title keywords
    if not is_confirmed and 0 < duration_seconds <= 120:
        brainrot_triggers = ["skibidi", "rizz", "sigma", "ohio", "npc", "brainrot", "gyat"]
        if any(kw in all_text for kw in brainrot_triggers):
            is_likely = True
            reasons.append("sub120s_brainrot_title")

    bonus = 0.0
    if is_confirmed:
        bonus = float(cfg.get("shorts_bonus_confirmed", 15))
    elif is_likely:
        bonus = float(cfg.get("shorts_bonus_likely", 10))

    return bonus, {
        "is_shorts": is_confirmed,
        "is_likely_shorts": is_likely,
        "bonus": bonus,
        "reasons": reasons,
        "source": "inline_heuristic",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.5: Thumbnail analysis (lightweight, parallel with Phase 2)
# ─────────────────────────────────────────────────────────────────────────────


def _phase1_5_thumbnail_analysis(
    thumbnail_url: str,
    video_id: str,
    tmpdir: str,
) -> Tuple[float, Dict[str, Any]]:
    """
    Download the video thumbnail and run visual analysis on it.

    This phase is intentionally lightweight: it downloads the thumbnail image
    (not the video) and runs thumbnail_analyzer if available, or falls back
    to the OCR already performed inside keyword_analyzer.

    Args:
        thumbnail_url : Full URL of the thumbnail
        video_id      : YouTube video ID (for logging)
        tmpdir        : Temporary directory for the downloaded image

    Returns:
        (thumbnail_score: float, thumbnail_details: dict)
    """
    if not thumbnail_url:
        return 0.0, {"error": "no_thumbnail_url"}

    try:
        # Download thumbnail
        import urllib.request  # stdlib
        thumb_path = os.path.join(tmpdir, f"{video_id}_thumb.jpg")
        req = urllib.request.Request(
            thumbnail_url,
            headers={"User-Agent": "BrainrotFilter/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            with open(thumb_path, "wb") as fh:
                fh.write(resp.read())
        logger.debug("Thumbnail downloaded for %s: %s", video_id, thumb_path)
    except Exception as exc:
        logger.debug("Thumbnail download failed for %s: %s", video_id, exc)
        return 0.0, {"error": f"download_failed: {exc}"}

    # Try dedicated thumbnail_analyzer first
    try:
        import thumbnail_analyzer  # type: ignore
        result = thumbnail_analyzer.analyze(
            thumbnail_url=thumbnail_url,
            video_id=video_id,
        )
        return float(result.score), result.details or {}
    except ImportError:
        pass  # fall through to lightweight inline analysis
    except Exception as exc:
        logger.warning("thumbnail_analyzer failed for %s: %s", video_id, exc)

    # Lightweight fallback: OCR the thumbnail for brainrot keywords
    return _thumbnail_ocr_fallback(thumb_path, video_id)


def _thumbnail_ocr_fallback(
    image_path: str,
    video_id: str,
) -> Tuple[float, Dict[str, Any]]:
    """
    Lightweight thumbnail analysis fallback using pytesseract OCR.
    Scans for brainrot keywords in thumbnail text.
    Returns (score, details).
    """
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        img = Image.open(image_path)
        text = pytesseract.image_to_string(img).lower()

        brainrot_triggers = [
            "skibidi", "rizz", "sigma", "ohio", "npc", "brainrot",
            "gyat", "gyatt", "fanum", "hawk tuah", "mewing", "gigachad",
        ]
        matched = [kw for kw in brainrot_triggers if kw in text]
        score = min(100.0, len(matched) * 15.0)

        return score, {
            "ocr_text_snippet": text[:200],
            "matched_keywords": matched,
            "score": score,
            "source": "ocr_fallback",
        }
    except ImportError:
        return 0.0, {"error": "pytesseract_unavailable", "source": "ocr_fallback"}
    except Exception as exc:
        logger.debug("Thumbnail OCR fallback failed for %s: %s", video_id, exc)
        return 0.0, {"error": str(exc), "source": "ocr_fallback"}


# ─────────────────────────────────────────────────────────────────────────────
# Shared video downloader
# ─────────────────────────────────────────────────────────────────────────────


def _download_video_once(
    video_id: str,
    duration_seconds: int,
    tmpdir: str,
    scan_duration: int,
) -> Optional[str]:
    """
    Download a video segment ONCE and return the local file path.

    Both scene_analyzer and audio_analyzer will consume the same file,
    avoiding duplicate network requests.

    Args:
        video_id        : YouTube video ID
        duration_seconds: Total video duration (used to cap scan_duration)
        tmpdir          : Temp directory to write the file into
        scan_duration   : How many seconds of video to download

    Returns:
        Absolute path to the downloaded file, or None on failure.
    """
    if duration_seconds > 0:
        scan_duration = min(scan_duration, duration_seconds)

    output_path = os.path.join(tmpdir, f"{video_id}_shared.mp4")
    time_range = f"*0:00-0:{scan_duration // 60:02d}:{scan_duration % 60:02d}"

    cmd = [
        "yt-dlp",
        "--format", "worstvideo[ext=mp4]+worstaudio[ext=m4a]/worst[acodec!=none][height<=360]/worst[acodec!=none]/worst",
        "--download-sections", time_range,
        "--output", output_path,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--force-overwrites",
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    import subprocess  # noqa: PLC0415

    try:
        timeout_secs = scan_duration * 3 + 60
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "yt-dlp exited %d for %s: %s",
                result.returncode,
                video_id,
                result.stderr[:300],
            )

        video_file = Path(output_path)
        if video_file.exists() and video_file.stat().st_size > 0:
            logger.info(
                "Shared video download complete for %s: %.1f MB",
                video_id,
                video_file.stat().st_size / 1_048_576,
            )
            return output_path

        # yt-dlp may have added an extension
        for candidate in Path(tmpdir).glob(f"{video_id}_shared.*"):
            if candidate.stat().st_size > 0:
                logger.debug("Found yt-dlp output at %s", candidate)
                return str(candidate)

        logger.warning("Shared download produced no usable file for %s.", video_id)
        return None

    except subprocess.TimeoutExpired:
        logger.error("Shared video download timed out for %s (%ds).", video_id, scan_duration)
        return None
    except Exception as exc:
        logger.error("Shared video download failed for %s: %s", video_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: download once → scene + audio in parallel
# ─────────────────────────────────────────────────────────────────────────────


def _phase2_download_and_analyze(
    video_id: str,
    metadata: Dict[str, Any],
    keyword_result: Dict[str, Any],
    cfg: _ConfigSnapshot,
    whisper_model: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Download the video once and run scene + audio analysis in parallel.

    Args:
        video_id        : YouTube video ID
        metadata        : dict from _phase1_metadata_and_keywords
        keyword_result  : Phase 1 keyword portion (same dict, subset used)
        cfg             : Configuration snapshot
        whisper_model   : Pre-loaded Whisper model (avoids reload per call)

    Returns:
        dict with keys:
            scene_score     : float
            scene_details   : dict
            audio_score     : float
            audio_details   : dict
            phase2_elapsed  : float
            phase2_errors   : list[str]
            video_path      : str | None  (for debugging)
    """
    start = time.monotonic()
    errors: List[str] = []
    category_id = metadata.get("category_id", "")
    duration_seconds = metadata.get("duration_seconds", 0)

    scene_score = 0.0
    scene_details: Dict[str, Any] = {}
    audio_score = 0.0
    audio_details: Dict[str, Any] = {}
    video_path: Optional[str] = None

    tmpdir = tempfile.mkdtemp(prefix="brainrot_p2_")
    try:
        # ── 1. Download shared video ──────────────────────────────────────
        dl_start = time.monotonic()
        video_path = _download_video_once(
            video_id=video_id,
            duration_seconds=duration_seconds,
            tmpdir=tmpdir,
            scan_duration=cfg.initial_scan_duration,
        )
        dl_elapsed = time.monotonic() - dl_start

        if video_path is None:
            errors.append("download:failed")
            logger.warning("Phase2 download failed for %s; scene+audio score=0.", video_id)
            return {
                "scene_score": 0.0,
                "scene_details": {"error": "Download failed"},
                "audio_score": 0.0,
                "audio_details": {"error": "Download failed"},
                "phase2_elapsed": round(time.monotonic() - start, 3),
                "phase2_errors": errors,
                "video_path": None,
            }

        logger.info("Shared download %.2fs for %s.", dl_elapsed, video_id)

        # ── 2. Scene + audio in parallel on the same file ─────────────────
        def _run_scene() -> Tuple[float, Dict[str, Any]]:
            try:
                import scene_analyzer  # type: ignore

                # Use GPU-hinted OpenCV if available
                gpu_info = _detect_gpu()
                if gpu_info["has_cuda"]:
                    configure_opencv_cuda()

                # scene_analyzer.analyze() normally downloads its own video.
                # Here we bypass the download by calling _detect_scenes directly.
                total_cuts, vid_duration = scene_analyzer._detect_scenes(
                    video_path,
                    threshold=float(config.get("scene_content_threshold", 27.0)),
                )

                if vid_duration > 0:
                    cpm = (total_cuts / vid_duration) * 60.0
                else:
                    cpm = 0.0

                avg_dur = (vid_duration / (total_cuts + 1)) if total_cuts > 0 else vid_duration
                music_dampening = float(config.get("music_video_dampening", 0.6))
                is_music = category_id == "10"

                raw_score = scene_analyzer._cpm_to_score(cpm)
                final_score = raw_score * music_dampening if is_music else raw_score

                details = SceneDetails(
                    total_cuts=total_cuts,
                    cuts_per_minute=round(cpm, 2),
                    avg_scene_duration_s=round(avg_dur, 2),
                    analysis_duration_s=round(vid_duration, 2),
                    is_music_video=is_music,
                    dampening_applied=is_music,
                    dampening_factor=music_dampening if is_music else 1.0,
                    full_scan_performed=False,
                )
                return round(final_score, 2), details.model_dump()

            except Exception as exc:
                errors.append(f"scene:{exc}")
                logger.error("Phase2 scene analysis failed for %s: %s", video_id, exc)
                return 0.0, {"error": str(exc)}

        def _run_audio() -> Tuple[float, Dict[str, Any]]:
            try:
                import audio_analyzer  # type: ignore

                # Pass the shared video file path; audio_analyzer.analyze()
                # accepts a video_path directly.
                audio_result = audio_analyzer.analyze(
                    video_path=video_path,
                    video_id=video_id,
                    # Inject Whisper model if available so it isn't reloaded inside
                    _whisper_model=whisper_model,
                )
                return float(audio_result.score), audio_result.details or {}

            except TypeError:
                # audio_analyzer.analyze() may not accept _whisper_model kwarg
                # (older version without gpu_utils integration); fall back.
                try:
                    import audio_analyzer  # type: ignore  # noqa: F811

                    audio_result = audio_analyzer.analyze(
                        video_path=video_path,
                        video_id=video_id,
                    )
                    return float(audio_result.score), audio_result.details or {}
                except Exception as exc2:
                    errors.append(f"audio:{exc2}")
                    logger.error("Phase2 audio analysis failed for %s: %s", video_id, exc2)
                    return 0.0, {"error": str(exc2)}
            except Exception as exc:
                errors.append(f"audio:{exc}")
                logger.error("Phase2 audio analysis failed for %s: %s", video_id, exc)
                return 0.0, {"error": str(exc)}

        remaining = cfg.phase2_timeout - (time.monotonic() - start)
        per_task_timeout = max(10.0, remaining - 5.0)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="p2") as pool:
            scene_future: Future = pool.submit(_run_scene)
            audio_future: Future = pool.submit(_run_audio)

            # Collect results, respecting individual timeouts
            try:
                scene_score, scene_details = scene_future.result(timeout=per_task_timeout)
            except FuturesTimeoutError:
                errors.append("scene:timeout")
                scene_score, scene_details = 0.0, {"error": "Timeout"}
                logger.warning("Scene analysis timed out for %s.", video_id)

            try:
                audio_score, audio_details = audio_future.result(timeout=per_task_timeout)
            except FuturesTimeoutError:
                errors.append("audio:timeout")
                audio_score, audio_details = 0.0, {"error": "Timeout"}
                logger.warning("Audio analysis timed out for %s.", video_id)

    finally:
        # Clean up shared temp directory
        shutil.rmtree(tmpdir, ignore_errors=True)

    phase2_elapsed = round(time.monotonic() - start, 3)
    logger.info(
        "Phase2 complete for %s: scene=%.1f, audio=%.1f, elapsed=%.2fs%s",
        video_id,
        scene_score,
        audio_score,
        phase2_elapsed,
        f" errors={errors}" if errors else "",
    )

    return {
        "scene_score": scene_score,
        "scene_details": scene_details,
        "audio_score": audio_score,
        "audio_details": audio_details,
        "phase2_elapsed": phase2_elapsed,
        "phase2_errors": errors,
        "video_path": None,  # already cleaned up
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────


def parallel_analyze(video_id: str) -> None:
    """
    Full parallel analysis pipeline for a single video.

    Drop-in replacement for _run_analysis(video_id) in analyzer_service.py.
    This function does NOT raise — all errors are logged and the video record
    is written with whatever scores are available when an error occurs.

    Pipeline:
        Phase 1 (concurrent): metadata + keywords + comments + engagement + shorts
        [Early exit check]
        Phase 1.5 + Phase 2 (concurrent):
            Phase 1.5: thumbnail download + visual analysis
            Phase 2: download once → scene + audio in parallel
        Persist to DB, update ACLs, kill states if needed, update channel profile.
    """
    logger.info("Starting parallel analysis for video: %s", video_id)
    overall_start = time.monotonic()

    # Snapshot config once for this run
    cfg = _ConfigSnapshot.from_config()

    # Pre-load Whisper model if GPU is available (avoids cold-start in Phase 2)
    gpu_info = _detect_gpu()
    whisper_model = None
    if gpu_info["use_whisper"]:
        logger.debug("Pre-loading Whisper model for %s...", video_id)
        whisper_model = get_whisper_model()

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    try:
        phase1 = _phase1_metadata_and_keywords(video_id)
    except Exception as exc:
        logger.error("Phase1 crashed for %s: %s", video_id, exc)
        phase1 = {
            "yt_data": None, "title": "", "description": "", "tags": [],
            "category_id": "", "channel_id": "", "thumbnail_url": "",
            "duration_seconds": 0, "view_count": 0, "like_count": 0,
            "comment_count": 0, "keyword_score": 0.0,
            "keyword_matches": [], "keyword_details": {},
            "comment_score": 0.0, "comment_details": {},
            "engagement_score": 0.0, "engagement_details": {},
            "shorts_bonus": 0.0, "shorts_details": {},
            "phase1_elapsed": 0.0, "phase1_errors": [str(exc)],
        }

    keyword_score: float = phase1["keyword_score"]
    comment_score: float = phase1.get("comment_score", 0.0)
    engagement_score: float = phase1.get("engagement_score", 0.0)
    shorts_bonus: float = phase1.get("shorts_bonus", 0.0)
    thumbnail_url: str = phase1["thumbnail_url"]

    # Check global timeout
    elapsed_so_far = time.monotonic() - overall_start
    if elapsed_so_far >= cfg.total_analysis_timeout:
        logger.warning(
            "Global timeout hit after Phase 1 for %s (%.1fs); using available scores.",
            video_id,
            elapsed_so_far,
        )
        _persist_result(
            video_id=video_id,
            phase1=phase1,
            scene_score=0.0,
            scene_details={"error": "Global timeout"},
            audio_score=0.0,
            audio_details={"error": "Global timeout"},
            thumbnail_score=0.0,
            thumbnail_details={"error": "Global timeout"},
            cfg=cfg,
            skip_reason="global_timeout",
        )
        return

    # ── Early termination check (now includes comment + engagement + shorts) ──
    skip_download, skip_reason = _can_skip_download(
        keyword_score=keyword_score,
        cfg=cfg,
        comment_score=comment_score,
        engagement_score=engagement_score,
        shorts_bonus=shorts_bonus,
    )
    if skip_download:
        logger.info(
            "Early exit for %s: %s. Skipping video download.",
            video_id,
            skip_reason,
        )
        _persist_result(
            video_id=video_id,
            phase1=phase1,
            scene_score=0.0,
            scene_details={},
            audio_score=0.0,
            audio_details={},
            thumbnail_score=0.0,
            thumbnail_details={},
            cfg=cfg,
            skip_reason=skip_reason,
        )
        return

    # ── Phase 1.5 (thumbnail) + Phase 2 (scene+audio) — run concurrently ─────
    p15_tmpdir = tempfile.mkdtemp(prefix="brainrot_p15_")
    thumbnail_score = 0.0
    thumbnail_details: Dict[str, Any] = {}
    phase2: Dict[str, Any] = {
        "scene_score": 0.0, "scene_details": {}, "audio_score": 0.0,
        "audio_details": {}, "phase2_elapsed": 0.0, "phase2_errors": [],
    }

    try:
        def _run_phase2() -> Dict[str, Any]:
            try:
                return _phase2_download_and_analyze(
                    video_id=video_id,
                    metadata=phase1,
                    keyword_result=phase1,
                    cfg=cfg,
                    whisper_model=whisper_model,
                )
            except Exception as exc2:
                logger.error("Phase2 crashed for %s: %s", video_id, exc2)
                return {
                    "scene_score": 0.0,
                    "scene_details": {"error": str(exc2)},
                    "audio_score": 0.0,
                    "audio_details": {"error": str(exc2)},
                    "phase2_elapsed": 0.0,
                    "phase2_errors": [str(exc2)],
                }

        def _run_phase15() -> Tuple[float, Dict[str, Any]]:
            return _phase1_5_thumbnail_analysis(
                thumbnail_url=thumbnail_url,
                video_id=video_id,
                tmpdir=p15_tmpdir,
            )

        remaining_budget = cfg.total_analysis_timeout - (time.monotonic() - overall_start)
        thumb_timeout = min(30.0, max(5.0, remaining_budget * 0.2))
        p2_timeout = max(10.0, remaining_budget - 5.0)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="p15p2") as pool:
            p2_future: Future = pool.submit(_run_phase2)
            p15_future: Future = pool.submit(_run_phase15)

            try:
                thumbnail_score, thumbnail_details = p15_future.result(timeout=thumb_timeout)
            except FuturesTimeoutError:
                thumbnail_details = {"error": "timeout"}
                logger.warning("Phase 1.5 thumbnail analysis timed out for %s.", video_id)
            except Exception as exc15:
                thumbnail_details = {"error": str(exc15)}
                logger.warning("Phase 1.5 failed for %s: %s", video_id, exc15)

            try:
                phase2 = p2_future.result(timeout=p2_timeout)
            except FuturesTimeoutError:
                logger.error("Phase 2 timed out for %s.", video_id)
                phase2 = {
                    "scene_score": 0.0,
                    "scene_details": {"error": "phase2_timeout"},
                    "audio_score": 0.0,
                    "audio_details": {"error": "phase2_timeout"},
                    "phase2_elapsed": 0.0,
                    "phase2_errors": ["phase2_timeout"],
                }
    finally:
        shutil.rmtree(p15_tmpdir, ignore_errors=True)

    # ── Persist results ───────────────────────────────────────────────────────
    _persist_result(
        video_id=video_id,
        phase1=phase1,
        scene_score=phase2.get("scene_score", 0.0),
        scene_details=phase2.get("scene_details", {}),
        audio_score=phase2.get("audio_score", 0.0),
        audio_details=phase2.get("audio_details", {}),
        thumbnail_score=thumbnail_score,
        thumbnail_details=thumbnail_details,
        cfg=cfg,
        skip_reason=None,
    )

    total_elapsed = time.monotonic() - overall_start
    logger.info(
        "Parallel analysis complete for %s in %.1fs "
        "(kw=%.1f co=%.1f eng=%.1f thumb=%.1f scene=%.1f audio=%.1f shorts=%.0f "
        "p1=%.1fs p2=%.1fs)",
        video_id,
        total_elapsed,
        keyword_score,
        comment_score,
        engagement_score,
        thumbnail_score,
        phase2.get("scene_score", 0.0),
        phase2.get("audio_score", 0.0),
        shorts_bonus,
        phase1["phase1_elapsed"],
        phase2.get("phase2_elapsed", 0.0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Result persistence (shared by early-exit and normal paths)
# ─────────────────────────────────────────────────────────────────────────────


def _persist_result(
    video_id: str,
    phase1: Dict[str, Any],
    scene_score: float,
    scene_details: Dict[str, Any],
    audio_score: float,
    audio_details: Dict[str, Any],
    cfg: _ConfigSnapshot,
    skip_reason: Optional[str],
    thumbnail_score: float = 0.0,
    thumbnail_details: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Compute the combined score, build a VideoAnalysis record, and persist it
    to the database.  Also updates ACL files, kills connection states if needed,
    and updates the channel profile.

    Called by both the normal and early-exit code paths.
    Now includes comment, engagement, thumbnail, and shorts bonus scores.
    """
    keyword_score: float = phase1["keyword_score"]
    comment_score: float = phase1.get("comment_score", 0.0)
    engagement_score: float = phase1.get("engagement_score", 0.0)
    shorts_bonus: float = phase1.get("shorts_bonus", 0.0)
    thumbnail_details = thumbnail_details or {}

    combined_score = cfg.compute_combined(
        kw=keyword_score,
        sc=scene_score,
        au=audio_score,
        comment=comment_score,
        engagement=engagement_score,
        thumbnail=thumbnail_score,
        shorts_bonus=shorts_bonus,
    )
    status_str = config.score_to_status(combined_score)

    # Build keyword match objects
    from models import (  # noqa: PLC0415
        KeywordMatch as _KM, SceneDetails as _SD, AudioDetails as _AD,
        CommentDetails as _CD, EngagementDetails as _ED,
        ThumbnailDetails as _TD, ShortsDetails as _ShD,
    )

    matched_kw_objects: List[Any] = []
    for kw in phase1.get("keyword_matches", []):
        try:
            matched_kw_objects.append(_KM(**kw))
        except Exception:
            pass

    def _safe_model(cls: Any, data: Dict[str, Any]) -> Optional[Any]:
        try:
            return cls(**data) if data else None
        except Exception:
            return None

    from datetime import datetime  # noqa: PLC0415

    # Build extended details that include all new analyzer outputs
    extended_keyword_details = dict(phase1.get("keyword_details") or {})
    extended_keyword_details.update({
        "comment_score": comment_score,
        "comment_details": phase1.get("comment_details", {}),
        "engagement_score": engagement_score,
        "engagement_details": phase1.get("engagement_details", {}),
        "shorts_bonus": shorts_bonus,
        "shorts_details": phase1.get("shorts_details", {}),
        "thumbnail_score": thumbnail_score,
        "thumbnail_details": thumbnail_details,
    })

    video = VideoAnalysis(
        video_id=video_id,
        channel_id=phase1.get("channel_id", ""),
        title=phase1.get("title", ""),
        description=(phase1.get("description", "") or "")[:500],
        thumbnail_url=phase1.get("thumbnail_url", ""),
        keyword_score=keyword_score,
        scene_score=scene_score,
        audio_score=audio_score,
        combined_score=combined_score,
        comment_score=comment_score,
        engagement_score=engagement_score,
        thumbnail_score=thumbnail_score,
        shorts_score=shorts_bonus,
        status=VideoStatus(status_str),
        matched_keywords=matched_kw_objects,
        scene_details=_safe_model(_SD, scene_details),
        audio_details=_safe_model(_AD, audio_details),
        comment_details=_safe_model(_CD, phase1.get("comment_details", {})),
        engagement_details=_safe_model(_ED, phase1.get("engagement_details", {})),
        thumbnail_details=_safe_model(_TD, thumbnail_details or {}),
        shorts_details=_safe_model(_ShD, phase1.get("shorts_details", {})),
        analyzed_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )

    try:
        from db_manager import db  # noqa: PLC0415
        db.upsert_video(video)
    except Exception as exc:
        logger.error("Failed to persist video record for %s: %s", video_id, exc)
        return

    # Update Squid ACL files
    try:
        from analyzer_service import _update_acl_files  # noqa: PLC0415
        _update_acl_files()
    except Exception as exc:
        logger.error("ACL update failed after analysis of %s: %s", video_id, exc)

    # Kill active connection states if the video is blocked
    if status_str in ("block", "soft_block"):
        try:
            from state_killer import kill_states_for_video  # noqa: PLC0415
            from db_manager import db  # noqa: PLC0415, F811

            recent_clients = db.get_recent_clients_for_video(video_id, max_age_seconds=300)
            for cip in recent_clients:
                success, count = kill_states_for_video(cip, video_id)
                if count > 0:
                    logger.info(
                        "Killed %d state(s) for client %s watching flagged video %s",
                        count, cip, video_id,
                    )
        except Exception as exc:
            logger.error("State kill failed for %s: %s", video_id, exc)

    # Update channel profile
    channel_id = phase1.get("channel_id", "")
    if channel_id:
        try:
            from channel_profiler import update_channel_after_video  # noqa: PLC0415
            update_channel_after_video(channel_id, status_str)
        except Exception as exc:
            logger.error("Channel profile update failed for %s: %s", channel_id, exc)

    logger.info(
        "Persisted %s: status=%s combined=%.1f "
        "(kw=%.1f sc=%.1f au=%.1f co=%.1f eng=%.1f thumb=%.1f shorts_bonus=%.0f)%s",
        video_id,
        status_str,
        combined_score,
        keyword_score,
        scene_score,
        audio_score,
        comment_score,
        engagement_score,
        thumbnail_score,
        shorts_bonus,
        f" [early_exit: {skip_reason}]" if skip_reason else "",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Module self-test (run directly to verify imports / GPU detection)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    print("=== GPU Detection ===")
    print(json.dumps(_detect_gpu(), indent=2))

    print("\n=== Config Snapshot ===")
    snap = _ConfigSnapshot.from_config()
    print(f"  block_score_min     : {snap.block_score_min}")
    print(f"  monitor_score_min   : {snap.monitor_score_min}")
    print(f"  initial_scan_dur    : {snap.initial_scan_duration}s")
    print(f"  total_timeout       : {snap.total_analysis_timeout}s")

    if len(sys.argv) > 1:
        vid = sys.argv[1]
        print(f"\n=== Analyzing video: {vid} ===")
        parallel_analyze(vid)
    else:
        print("\nPass a YouTube video ID as argument to run a full analysis test.")
        print("  python parallel_analyzer.py dQw4w9WgXcQ")
