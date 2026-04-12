"""
thumbnail_analyzer.py - Visual pattern detection on YouTube thumbnails.

Uses PIL/Pillow, numpy, pytesseract (OCR), and OpenCV for image analysis.
All libraries are optional at import time — missing libraries disable the
relevant sub-analysis and the score is derived from the available components.

Pipeline:
  1. Download thumbnail (maxresdefault.jpg → hqdefault.jpg fallback)
  2. Color saturation analysis (hyper-saturated thumbnails → brainrot signal)
  3. Text overlay detection via pytesseract OCR
  4. Red arrow / red circle detection (contour detection on red channel mask)
  5. Face detection (OpenCV Haar Cascade — exaggerated close-ups = brainrot)
  6. Visual complexity / chaos (Canny edge density + color histogram entropy)
  7. Brightness extremes (high contrast / neon = brainrot signal)
  8. Sum sub-scores → thumbnail_score (0-100)

Sub-score caps:
  saturation_score     0-20
  text_overlay_score   0-25
  red_elements_score   0-15
  face_score           0-15
  complexity_score     0-15
  brightness_score     0-10
"""

from __future__ import annotations

import io
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from models import AnalysisResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurable constant: path to OpenCV Haar Cascade face detector XML
# Change this if OpenCV is installed in a non-standard location.
# ---------------------------------------------------------------------------
HAAR_CASCADE_PATH: str = (
    "/usr/local/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
)
# Alternate common paths tried in order if the above doesn't exist:
_CASCADE_FALLBACKS: List[str] = [
    "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
    "/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml",
    "/usr/local/lib/python3.11/dist-packages/cv2/data/haarcascade_frontalface_default.xml",
    "/usr/local/lib/python3.10/dist-packages/cv2/data/haarcascade_frontalface_default.xml",
]

THUMBNAIL_TIMEOUT = 10  # seconds for HTTP download

# Brainrot keywords to look for in OCR text
_BRAINROT_TEXT_RE = re.compile(
    r"\b(?:skibidi|rizz|sigma|gyatt|fanum|brainrot|ohio|slay|bussin|delulu"
    r"|mewing|goat|npc|pov|wait for it|watch till|no cap|sus|sheesh|based"
    r"|cringe|based|op|lowkey|highkey|mid|goated|glazing)\b",
    re.IGNORECASE,
)
_CAPS_WORD_RE = re.compile(r"\b[A-Z]{3,}\b")
_EXCLAMATION_RE = re.compile(r"[!?]{2,}")

# ---------------------------------------------------------------------------
# Thumbnail URL helpers
# ---------------------------------------------------------------------------

_THUMBNAIL_TEMPLATES: List[str] = [
    "https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
    "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    "https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
]


def _download_thumbnail(thumbnail_url: str, video_id: str = "") -> Optional[bytes]:
    """
    Download the thumbnail image.

    If *thumbnail_url* is provided, attempt it first, then fall back to
    YouTube's standard thumbnail CDN URLs derived from *video_id*.
    Returns raw image bytes, or None on failure.
    """
    urls_to_try: List[str] = []
    if thumbnail_url:
        urls_to_try.append(thumbnail_url)
    if video_id:
        for tmpl in _THUMBNAIL_TEMPLATES:
            url = tmpl.format(video_id=video_id)
            if url not in urls_to_try:
                urls_to_try.append(url)

    for url in urls_to_try:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=THUMBNAIL_TIMEOUT) as resp:
                data = resp.read()
            if len(data) > 500:  # sanity: skip tiny placeholder images
                logger.debug("Thumbnail downloaded from %s (%d bytes)", url, len(data))
                return data
        except Exception as exc:
            logger.debug("Thumbnail download failed for %s: %s", url, exc)
            continue

    logger.warning("Could not download thumbnail for video %s.", video_id)
    return None


def _find_cascade_path() -> str:
    """Return the first existing Haar cascade XML path, or empty string."""
    candidates = [HAAR_CASCADE_PATH] + _CASCADE_FALLBACKS
    for p in candidates:
        if Path(p).exists():
            return p
    # Try to find via cv2 package data
    try:
        import cv2  # type: ignore
        cv2_data = Path(cv2.__file__).parent / "data" / "haarcascade_frontalface_default.xml"
        if cv2_data.exists():
            return str(cv2_data)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Sub-analysis functions
# ---------------------------------------------------------------------------


def _analyze_saturation(img_array: Any) -> Tuple[float, Dict[str, float]]:
    """
    Analyze color saturation in HSV space.

    Returns (score 0-20, metrics_dict).
    Brainrot thumbnails tend to use hyper-saturated colors.
    """
    try:
        import numpy as np  # type: ignore
        import cv2  # type: ignore

        hsv = cv2.cvtColor(img_array, cv2.COLOR_BGR2HSV)
        sat_channel = hsv[:, :, 1].astype(np.float32)
        avg_sat = float(np.mean(sat_channel))
        max_sat = float(np.max(sat_channel))
        p90_sat = float(np.percentile(sat_channel, 90))

        # OpenCV HSV saturation range: 0-255
        # avg_sat > 150 is very saturated; > 200 is hyper-saturated
        score = min(20.0, (avg_sat / 255.0) * 20.0 * 1.5)
        # Boost if the 90th percentile is very high (neon / brainrot palette)
        if p90_sat > 220:
            score = min(20.0, score + 4.0)

        return round(score, 2), {
            "avg_saturation": round(avg_sat, 2),
            "max_saturation": round(max_sat, 2),
            "p90_saturation": round(p90_sat, 2),
        }
    except Exception as exc:
        logger.debug("Saturation analysis failed: %s", exc)
        return 0.0, {}


def _analyze_text_overlay(img_array: Any) -> Tuple[float, Dict[str, Any]]:
    """
    Use pytesseract to detect text overlays in the thumbnail.

    Returns (score 0-25, metrics_dict).
    """
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
        import numpy as np  # type: ignore  # noqa: F401

        # Convert BGR numpy array to PIL Image for pytesseract
        pil_img = Image.fromarray(img_array[:, :, ::-1])  # BGR → RGB
        img_h, img_w = img_array.shape[:2]
        total_area = img_h * img_w

        # Get bounding boxes for text blocks
        try:
            data = pytesseract.image_to_data(
                pil_img,
                config="--psm 3 --oem 3",
                output_type=pytesseract.Output.DICT,
            )
        except Exception:
            data = {}

        full_text = pytesseract.image_to_string(pil_img, config="--psm 3").strip()

        # Calculate approximate text-covered area
        text_area = 0
        n = len(data.get("text", []))
        for i in range(n):
            txt = str(data["text"][i]).strip() if data["text"][i] else ""
            conf = int(data["conf"][i]) if data["conf"][i] else 0
            if txt and conf > 40:
                w = int(data["width"][i] or 0)
                h = int(data["height"][i] or 0)
                text_area += w * h

        text_area_ratio = min(1.0, text_area / max(total_area, 1))

        # Qualitative flags
        has_caps = bool(_CAPS_WORD_RE.search(full_text))
        has_exclamation = bool(_EXCLAMATION_RE.search(full_text))
        has_brainrot_text = bool(_BRAINROT_TEXT_RE.search(full_text))
        caps_word_count = len(_CAPS_WORD_RE.findall(full_text))

        # Score components
        area_component = min(15.0, text_area_ratio * 50.0)   # 0-15
        content_component = 0.0
        if has_brainrot_text:
            content_component += 5.0
        if has_caps:
            content_component += min(3.0, caps_word_count * 1.0)
        if has_exclamation:
            content_component += 2.0
        content_component = min(10.0, content_component)

        score = min(25.0, area_component + content_component)

        return round(score, 2), {
            "text_detected": bool(full_text),
            "text_content": full_text[:200],
            "text_area_ratio": round(text_area_ratio, 4),
            "has_caps": has_caps,
            "has_exclamation": has_exclamation,
            "has_brainrot_text": has_brainrot_text,
            "caps_word_count": caps_word_count,
        }
    except ImportError:
        logger.debug("pytesseract or PIL not available; skipping text overlay analysis.")
        return 0.0, {"text_detected": False, "text_content": "", "text_area_ratio": 0.0}
    except Exception as exc:
        logger.debug("Text overlay analysis failed: %s", exc)
        return 0.0, {"text_detected": False, "text_content": "", "text_area_ratio": 0.0}


def _analyze_red_elements(img_array: Any) -> Tuple[float, Dict[str, Any]]:
    """
    Detect prominent red elements (clickbait arrows/circles) via HSV masking.

    Returns (score 0-15, metrics_dict).
    """
    try:
        import numpy as np  # type: ignore
        import cv2  # type: ignore

        hsv = cv2.cvtColor(img_array, cv2.COLOR_BGR2HSV)
        img_h, img_w = img_array.shape[:2]
        total_area = img_h * img_w

        # Red spans two HSV hue ranges in OpenCV (0-10 and 170-180)
        mask1 = cv2.inRange(hsv, np.array([0, 100, 100]), np.array([10, 255, 255]))
        mask2 = cv2.inRange(hsv, np.array([170, 100, 100]), np.array([180, 255, 255]))
        red_mask = cv2.bitwise_or(mask1, mask2)

        # Find contours in the red mask
        contours, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        significant_contours = [c for c in contours if cv2.contourArea(c) > total_area * 0.005]
        red_pixel_ratio = float(np.sum(red_mask > 0)) / total_area

        # Score based on number and size of red elements
        contour_score = min(10.0, len(significant_contours) * 2.5)
        pixel_score = min(5.0, red_pixel_ratio * 50.0)
        score = min(15.0, contour_score + pixel_score)

        return round(score, 2), {
            "red_elements_count": len(significant_contours),
            "red_pixel_ratio": round(red_pixel_ratio, 4),
        }
    except Exception as exc:
        logger.debug("Red element analysis failed: %s", exc)
        return 0.0, {"red_elements_count": 0, "red_pixel_ratio": 0.0}


def _analyze_faces(img_array: Any) -> Tuple[float, Dict[str, Any]]:
    """
    Detect faces using OpenCV Haar Cascade.

    Returns (score 0-15, metrics_dict).
    Multiple large faces or extreme close-ups are common in brainrot thumbnails.
    """
    try:
        import cv2  # type: ignore

        cascade_path = _find_cascade_path()
        if not cascade_path:
            logger.debug("Haar cascade not found; skipping face detection.")
            return 0.0, {"faces_detected": 0}

        gray = cv2.cvtColor(img_array, cv2.COLOR_BGR2GRAY)
        face_cascade = cv2.CascadeClassifier(cascade_path)
        faces = face_cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=4,
            minSize=(30, 30),
        )

        img_h, img_w = img_array.shape[:2]
        total_area = img_h * img_w

        face_count = len(faces)
        if face_count == 0:
            return 0.0, {"faces_detected": 0, "max_face_ratio": 0.0}

        face_areas = [w * h for (_, _, w, h) in faces]
        max_face_area = max(face_areas)
        max_face_ratio = max_face_area / total_area

        # Close-up exaggerated face = high ratio (> 0.2 of image area)
        face_size_score = min(8.0, max_face_ratio * 40.0)
        # Multiple faces (often reaction faces / split screen)
        face_count_score = min(7.0, face_count * 2.5)

        score = min(15.0, face_size_score + face_count_score)

        return round(score, 2), {
            "faces_detected": face_count,
            "max_face_ratio": round(max_face_ratio, 4),
            "face_areas": face_areas[:5],  # limit to 5 for storage
        }
    except Exception as exc:
        logger.debug("Face detection failed: %s", exc)
        return 0.0, {"faces_detected": 0}


def _analyze_complexity(img_array: Any) -> Tuple[float, Dict[str, Any]]:
    """
    Measure visual complexity using Canny edge density and color histogram entropy.

    Returns (score 0-15, metrics_dict).
    Chaotic / busy thumbnails score higher.
    """
    try:
        import numpy as np  # type: ignore
        import cv2  # type: ignore

        gray = cv2.cvtColor(img_array, cv2.COLOR_BGR2GRAY)

        # Canny edge detection
        edges = cv2.Canny(gray, threshold1=50, threshold2=150)
        edge_density = float(np.sum(edges > 0)) / (edges.shape[0] * edges.shape[1])

        # Color histogram entropy (all 3 channels combined)
        entropies: List[float] = []
        for channel in range(3):
            hist = cv2.calcHist([img_array], [channel], None, [256], [0, 256])
            hist = hist.flatten() / max(hist.sum(), 1)
            # Shannon entropy
            ent = -float(np.sum(hist[hist > 0] * np.log2(hist[hist > 0])))
            entropies.append(ent)
        color_entropy = sum(entropies) / len(entropies)

        # edge_density: 0 = uniform, 0.3+ = very busy
        edge_score = min(8.0, edge_density * 27.0)
        # color_entropy: max is log2(256) ≈ 8.0 for perfectly uniform histogram
        entropy_score = min(7.0, (color_entropy / 8.0) * 7.0)

        score = min(15.0, edge_score + entropy_score)

        return round(score, 2), {
            "edge_density": round(edge_density, 4),
            "color_entropy": round(color_entropy, 4),
        }
    except Exception as exc:
        logger.debug("Complexity analysis failed: %s", exc)
        return 0.0, {"edge_density": 0.0, "color_entropy": 0.0}


def _analyze_brightness(img_array: Any) -> Tuple[float, Dict[str, Any]]:
    """
    Detect extreme brightness contrast and neon-like color distribution.

    Returns (score 0-10, metrics_dict).
    """
    try:
        import numpy as np  # type: ignore
        import cv2  # type: ignore

        gray = cv2.cvtColor(img_array, cv2.COLOR_BGR2GRAY).astype(np.float32)
        mean_brightness = float(np.mean(gray))
        std_brightness = float(np.std(gray))

        # High standard deviation = high contrast / extreme values
        contrast_score = min(5.0, (std_brightness / 128.0) * 5.0)

        # Count near-white (> 230) and near-black (< 25) pixels
        bright_ratio = float(np.sum(gray > 230)) / gray.size
        dark_ratio = float(np.sum(gray < 25)) / gray.size
        extreme_ratio = bright_ratio + dark_ratio
        extreme_score = min(5.0, extreme_ratio * 20.0)

        score = min(10.0, contrast_score + extreme_score)

        return round(score, 2), {
            "mean_brightness": round(mean_brightness, 2),
            "brightness_std": round(std_brightness, 2),
            "bright_pixel_ratio": round(bright_ratio, 4),
            "dark_pixel_ratio": round(dark_ratio, 4),
        }
    except Exception as exc:
        logger.debug("Brightness analysis failed: %s", exc)
        return 0.0, {"mean_brightness": 0.0, "brightness_std": 0.0}


# ---------------------------------------------------------------------------
# Main analyze function
# ---------------------------------------------------------------------------


def analyze(thumbnail_url: str, video_id: str = "") -> AnalysisResult:
    """
    Run full visual analysis on a YouTube video thumbnail.

    Args:
        thumbnail_url: Direct URL to the thumbnail image (e.g. from YouTube API).
                       If empty, will attempt to derive from video_id.
        video_id:      YouTube video ID used for fallback URL construction and logging.

    Returns:
        AnalysisResult with module="thumbnail", score (0-100), and details dict
        containing: avg_saturation, text_detected, text_content, text_area_ratio,
        red_elements_count, faces_detected, edge_density, color_entropy,
        score_breakdown.
    """
    start = time.monotonic()

    # ----------------------------------------------------------------
    # Download thumbnail
    # ----------------------------------------------------------------
    raw_bytes = _download_thumbnail(thumbnail_url, video_id)

    if raw_bytes is None:
        elapsed = time.monotonic() - start
        logger.info("Thumbnail analysis for %s skipped: download failed.", video_id or thumbnail_url)
        return AnalysisResult(
            module="thumbnail",
            score=0.0,
            details={
                "avg_saturation": 0.0,
                "text_detected": False,
                "text_content": "",
                "text_area_ratio": 0.0,
                "red_elements_count": 0,
                "faces_detected": 0,
                "edge_density": 0.0,
                "color_entropy": 0.0,
                "score_breakdown": {},
                "download_error": True,
            },
            error="thumbnail_download_failed",
            duration_s=round(elapsed, 3),
        )

    # ----------------------------------------------------------------
    # Decode to numpy array via OpenCV
    # ----------------------------------------------------------------
    try:
        import numpy as np  # type: ignore
        import cv2  # type: ignore

        nparr = np.frombuffer(raw_bytes, dtype=np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("cv2.imdecode returned None")
    except Exception as exc:
        # Fall back: try PIL if OpenCV unavailable
        try:
            from PIL import Image  # type: ignore
            import numpy as np  # type: ignore
            pil_img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
            img = np.array(pil_img)[:, :, ::-1].copy()  # RGB → BGR
        except Exception as pil_exc:
            elapsed = time.monotonic() - start
            logger.warning(
                "Could not decode thumbnail for %s: cv2=%s, pil=%s",
                video_id, exc, pil_exc,
            )
            return AnalysisResult(
                module="thumbnail",
                score=0.0,
                details={"decode_error": str(exc)},
                error="thumbnail_decode_failed",
                duration_s=round(elapsed, 3),
            )

    # ----------------------------------------------------------------
    # Run sub-analyzers
    # ----------------------------------------------------------------
    sat_score, sat_metrics = _analyze_saturation(img)
    text_score, text_metrics = _analyze_text_overlay(img)
    red_score, red_metrics = _analyze_red_elements(img)
    face_score, face_metrics = _analyze_faces(img)
    complexity_score, complexity_metrics = _analyze_complexity(img)
    brightness_score, brightness_metrics = _analyze_brightness(img)

    total_score = min(
        100.0,
        sat_score + text_score + red_score + face_score + complexity_score + brightness_score,
    )

    elapsed = time.monotonic() - start

    logger.info(
        "Thumbnail analysis for %s: score=%.1f (sat=%.1f, text=%.1f, red=%.1f, "
        "face=%.1f, complexity=%.1f, brightness=%.1f), duration=%.2fs",
        video_id or thumbnail_url,
        total_score,
        sat_score,
        text_score,
        red_score,
        face_score,
        complexity_score,
        brightness_score,
        elapsed,
    )

    return AnalysisResult(
        module="thumbnail",
        score=round(total_score, 2),
        details={
            # Flattened key metrics for quick access
            "avg_saturation": sat_metrics.get("avg_saturation", 0.0),
            "text_detected": text_metrics.get("text_detected", False),
            "text_content": text_metrics.get("text_content", ""),
            "text_area_ratio": text_metrics.get("text_area_ratio", 0.0),
            "red_elements_count": red_metrics.get("red_elements_count", 0),
            "faces_detected": face_metrics.get("faces_detected", 0),
            "edge_density": complexity_metrics.get("edge_density", 0.0),
            "color_entropy": complexity_metrics.get("color_entropy", 0.0),
            # Full breakdown
            "saturation_metrics": sat_metrics,
            "text_metrics": text_metrics,
            "red_metrics": red_metrics,
            "face_metrics": face_metrics,
            "complexity_metrics": complexity_metrics,
            "brightness_metrics": brightness_metrics,
            "score_breakdown": {
                "saturation_score": sat_score,
                "text_overlay_score": text_score,
                "red_elements_score": red_score,
                "face_score": face_score,
                "complexity_score": complexity_score,
                "brightness_score": brightness_score,
            },
        },
        duration_s=round(elapsed, 3),
    )
