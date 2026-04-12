"""
scene_analyzer.py - Scene cut detection for brainrot scoring.

Pipeline:
  1. Download first N seconds of video via yt-dlp (worst quality to save time)
  2. Run PySceneDetect ContentDetector to find scene cuts
  3. Calculate cuts_per_minute, total_cuts, avg_scene_duration
  4. Apply music video dampening if YouTube category_id == "10"
  5. If initial score exceeds threshold, re-download up to full_scan_time_limit
     and re-analyse for a more accurate result
  6. Map cuts_per_minute to scene_score (0-100)
  7. Return AnalysisResult with module="scene"

Score mapping (before dampening):
  < 5  cuts/min  → very low score  (~5)
  5-10            → low            (~20)
  10-20           → moderate       (~45)
  20-40           → high           (~70)
  40-60           → very high      (~85)
  > 60            → capped at 100
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Tuple

from config import config
from models import AnalysisResult, SceneDetails

logger = logging.getLogger(__name__)

# Minimum cuts/min that we consider worth re-scanning with full video
_RESCAN_TRIGGER_CPM = 15.0

# Score curve: (cuts_per_minute_threshold, score_value) pairs — linear interp
_SCORE_CURVE: list = [
    (0.0, 0.0),
    (5.0, 10.0),
    (10.0, 25.0),
    (20.0, 50.0),
    (35.0, 70.0),
    (50.0, 85.0),
    (75.0, 95.0),
    (100.0, 100.0),
]


# ---------------------------------------------------------------------------
# Video download
# ---------------------------------------------------------------------------


def _download_video_segment(
    video_id: str,
    output_path: str,
    duration_seconds: int,
) -> bool:
    """
    Download a video segment using yt-dlp.

    Uses worst available video quality to minimise download size.
    Returns True on success, False on failure.
    """
    time_range = f"*0:00-0:{duration_seconds // 60:02d}:{duration_seconds % 60:02d}"

    cmd = [
        "yt-dlp",
        "--format", "worstvideo[ext=mp4]/worstvideo/worst[ext=mp4]/worst",
        "--download-sections", time_range,
        "--output", output_path,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--force-overwrites",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=duration_seconds * 3 + 60,  # generous timeout
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "yt-dlp exited %d for %s: %s",
                result.returncode,
                video_id,
                result.stderr[:300],
            )
            return Path(output_path).exists() and Path(output_path).stat().st_size > 0

        return Path(output_path).exists() and Path(output_path).stat().st_size > 0
    except subprocess.TimeoutExpired:
        logger.error("Video download timed out for %s (duration=%ds)", video_id, duration_seconds)
        return False
    except Exception as exc:
        logger.error("Video download failed for %s: %s", video_id, exc)
        return False


# ---------------------------------------------------------------------------
# Scene detection
# ---------------------------------------------------------------------------


def _detect_scenes(video_path: str, threshold: float = 27.0) -> Tuple[int, float]:
    """
    Run PySceneDetect ContentDetector on *video_path*.

    Returns (total_cuts, video_duration_seconds).
    """
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import ContentDetector

        video = open_video(video_path)
        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector(threshold=threshold))

        # Detect scenes (reads the whole video file)
        scene_manager.detect_scenes(video, show_progress=False)
        scene_list = scene_manager.get_scene_list()

        total_cuts = max(0, len(scene_list) - 1)  # n scenes → n-1 cuts

        # Get total duration from video
        duration = video.duration.get_seconds() if video.duration else 0.0
        return total_cuts, float(duration)

    except ImportError:
        logger.error("scenedetect not installed. Cannot perform scene analysis.")
        return 0, 0.0
    except Exception as exc:
        logger.error("Scene detection failed on %s: %s", video_path, exc)
        return 0, 0.0


# ---------------------------------------------------------------------------
# Score mapping
# ---------------------------------------------------------------------------


def _cpm_to_score(cuts_per_minute: float) -> float:
    """
    Map cuts_per_minute to a 0-100 score using the linear piecewise curve.
    """
    if cuts_per_minute <= 0:
        return 0.0
    if cuts_per_minute >= _SCORE_CURVE[-1][0]:
        return 100.0

    for i in range(len(_SCORE_CURVE) - 1):
        x0, y0 = _SCORE_CURVE[i]
        x1, y1 = _SCORE_CURVE[i + 1]
        if x0 <= cuts_per_minute <= x1:
            t = (cuts_per_minute - x0) / (x1 - x0)
            return round(y0 + t * (y1 - y0), 2)

    return 100.0


# ---------------------------------------------------------------------------
# Main analyzer function
# ---------------------------------------------------------------------------


def analyze(
    video_id: str,
    category_id: str = "",
    duration_seconds: int = 0,
) -> AnalysisResult:
    """
    Run scene cut detection for *video_id*.

    Args:
        video_id:        YouTube video ID
        category_id:     YouTube category ID (used to detect music videos)
        duration_seconds: Total video duration (used to decide if full scan
                          would exceed the time limit)

    Returns:
        AnalysisResult with module="scene", score, details.
    """
    start = time.monotonic()
    initial_scan_dur = config.initial_scan_duration
    full_scan_limit = config.full_scan_time_limit
    threshold = float(config.get("scene_content_threshold", 27.0))
    music_dampening = float(config.get("music_video_dampening", 0.6))
    is_music = category_id == "10"

    details = SceneDetails(is_music_video=is_music)

    with tempfile.TemporaryDirectory(prefix="brainrot_scene_") as tmpdir:
        video_path = os.path.join(tmpdir, f"{video_id}.mp4")

        # --- Initial scan (first N seconds) ---
        scan_dur = min(initial_scan_dur, duration_seconds) if duration_seconds > 0 else initial_scan_dur
        logger.info("Downloading %ds clip for scene analysis: %s", scan_dur, video_id)

        if not _download_video_segment(video_id, video_path, scan_dur):
            elapsed = time.monotonic() - start
            logger.warning("Video download failed for scene analysis: %s", video_id)
            return AnalysisResult(
                module="scene",
                score=0.0,
                details={"error": "Download failed", "is_music_video": is_music},
                error="Video download failed",
                duration_s=round(elapsed, 3),
            )

        total_cuts, vid_duration = _detect_scenes(video_path, threshold)

        if vid_duration > 0:
            cpm = (total_cuts / vid_duration) * 60.0
        else:
            cpm = 0.0

        avg_scene_dur = (vid_duration / (total_cuts + 1)) if total_cuts > 0 else vid_duration

        initial_score = _cpm_to_score(cpm)

        details.total_cuts = total_cuts
        details.cuts_per_minute = round(cpm, 2)
        details.avg_scene_duration_s = round(avg_scene_dur, 2)
        details.analysis_duration_s = round(vid_duration, 2)

        # --- Re-scan decision ---
        should_full_scan = (
            initial_score > config.scene_threshold
            and not is_music  # music videos are expected to have many cuts
            and (duration_seconds == 0 or duration_seconds <= full_scan_limit)
        )

        if should_full_scan:
            logger.info(
                "Initial score %.1f > threshold; performing full scan for %s",
                initial_score,
                video_id,
            )
            full_path = os.path.join(tmpdir, f"{video_id}_full.mp4")
            if _download_video_segment(video_id, full_path, full_scan_limit):
                full_cuts, full_duration = _detect_scenes(full_path, threshold)
                if full_duration > 0:
                    full_cpm = (full_cuts / full_duration) * 60.0
                    full_avg = (full_duration / (full_cuts + 1)) if full_cuts > 0 else full_duration
                    # Use full scan numbers
                    total_cuts = full_cuts
                    cpm = full_cpm
                    avg_scene_dur = full_avg
                    details.total_cuts = full_cuts
                    details.cuts_per_minute = round(full_cpm, 2)
                    details.avg_scene_duration_s = round(full_avg, 2)
                    details.analysis_duration_s = round(full_duration, 2)
                    details.full_scan_performed = True
            else:
                logger.warning("Full scan download failed for %s; using initial scan.", video_id)

        # --- Final score with music dampening ---
        raw_score = _cpm_to_score(cpm)
        if is_music:
            final_score = raw_score * music_dampening
            details.dampening_applied = True
            details.dampening_factor = music_dampening
        else:
            final_score = raw_score
            details.dampening_applied = False

    elapsed = time.monotonic() - start
    logger.info(
        "Scene analysis for %s: score=%.1f, cuts=%d, cpm=%.1f, duration=%.2fs",
        video_id,
        final_score,
        details.total_cuts,
        details.cuts_per_minute,
        elapsed,
    )

    return AnalysisResult(
        module="scene",
        score=round(final_score, 2),
        details=details.model_dump(),
        duration_s=round(elapsed, 3),
    )
