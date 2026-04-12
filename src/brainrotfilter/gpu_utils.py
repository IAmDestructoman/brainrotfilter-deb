"""
gpu_utils.py - GPU capability detection and accelerated inference utilities.

Provides:
  detect_gpu()             -> dict   GPU availability, CUDA version, device info
  get_whisper_model(size)  -> model  Lazy-load OpenAI Whisper model (GPU if available)
  transcribe_with_whisper(audio_path, model) -> str  GPU-accelerated STT

All functions gracefully degrade to CPU if CUDA or Whisper is unavailable.
The module never raises at import time — callers can always expect a valid return value.

Environment variables:
  BRAINROT_USE_WHISPER   : "true"/"false" — force-enable/disable Whisper
  WHISPER_MODEL_SIZE     : tiny | base | small | medium | large  (default: base)
  VOSK_MODEL_PATH        : fallback Vosk model path
  CUDA_VISIBLE_DEVICES   : standard CUDA env var (respected automatically by torch)
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Module-level state ────────────────────────────────────────────────────────
_gpu_info_cache: Optional[Dict[str, Any]] = None
_gpu_info_lock = threading.Lock()

_whisper_model_cache: Dict[str, Any] = {}  # size -> model
_whisper_model_lock = threading.Lock()

# ── Constants ─────────────────────────────────────────────────────────────────
_DEFAULT_WHISPER_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "base")
_USE_WHISPER_ENV = os.environ.get("BRAINROT_USE_WHISPER", "").lower()
_VOSK_MODEL_PATH = os.environ.get(
    "VOSK_MODEL_PATH",
    "/app/models/vosk-model-small-en-us",
)


# ─────────────────────────────────────────────────────────────────────────────
# GPU detection
# ─────────────────────────────────────────────────────────────────────────────


def detect_gpu() -> Dict[str, Any]:
    """
    Probe the current environment for GPU/CUDA capabilities.

    Returns a dict with keys:
      has_cuda      : bool   — CUDA is available via PyTorch
      cuda_version  : str    — CUDA version string (e.g. "12.2") or ""
      gpu_name      : str    — GPU display name or ""
      gpu_memory_mb : int    — Total VRAM in megabytes (0 if unknown)
      gpu_count     : int    — Number of visible CUDA devices
      has_whisper   : bool   — openai-whisper is importable
      has_vosk      : bool   — vosk is importable and model exists
      use_whisper   : bool   — recommended: use Whisper for STT
      device        : str    — "cuda:0", "cpu", etc.

    Results are cached after the first call.
    """
    global _gpu_info_cache

    with _gpu_info_lock:
        if _gpu_info_cache is not None:
            return dict(_gpu_info_cache)

        info: Dict[str, Any] = {
            "has_cuda": False,
            "cuda_version": "",
            "gpu_name": "",
            "gpu_memory_mb": 0,
            "gpu_count": 0,
            "has_whisper": False,
            "has_vosk": False,
            "use_whisper": False,
            "device": "cpu",
        }

        # ── PyTorch / CUDA ────────────────────────────────────────────────
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                info["has_cuda"] = True
                info["cuda_version"] = torch.version.cuda or ""
                info["gpu_count"] = torch.cuda.device_count()
                info["device"] = "cuda:0"

                try:
                    dev = torch.cuda.current_device()
                    info["gpu_name"] = torch.cuda.get_device_name(dev)
                    props = torch.cuda.get_device_properties(dev)
                    info["gpu_memory_mb"] = props.total_memory // (1024 * 1024)
                except Exception as exc:
                    logger.debug("Could not query GPU properties: %s", exc)
            else:
                logger.debug("torch available but CUDA not detected.")
        except ImportError:
            logger.debug("PyTorch not installed; no CUDA capability.")
        except Exception as exc:
            logger.warning("Unexpected error during CUDA probe: %s", exc)

        # ── Whisper ───────────────────────────────────────────────────────
        try:
            import whisper  # type: ignore  # noqa: F401

            info["has_whisper"] = True
        except ImportError:
            logger.debug("openai-whisper not installed.")

        # ── Vosk ──────────────────────────────────────────────────────────
        try:
            import vosk  # type: ignore  # noqa: F401

            vosk_path = _VOSK_MODEL_PATH
            info["has_vosk"] = Path(vosk_path).exists()
            if not info["has_vosk"]:
                logger.debug("Vosk installed but model not found at %s.", vosk_path)
        except ImportError:
            logger.debug("vosk not installed.")

        # ── Decide STT backend ────────────────────────────────────────────
        # Priority: env override > Whisper+CUDA > Vosk > nothing
        if _USE_WHISPER_ENV == "true":
            info["use_whisper"] = info["has_whisper"]
        elif _USE_WHISPER_ENV == "false":
            info["use_whisper"] = False
        else:
            # Auto: prefer Whisper when GPU is available
            info["use_whisper"] = info["has_cuda"] and info["has_whisper"]

        _gpu_info_cache = dict(info)
        logger.info(
            "GPU probe: cuda=%s (%s, %dMB), whisper=%s, vosk=%s, use_whisper=%s",
            info["has_cuda"],
            info["gpu_name"] or "N/A",
            info["gpu_memory_mb"],
            info["has_whisper"],
            info["has_vosk"],
            info["use_whisper"],
        )
        return dict(info)


def invalidate_gpu_cache() -> None:
    """Force re-detection on next detect_gpu() call (useful in tests)."""
    global _gpu_info_cache
    with _gpu_info_lock:
        _gpu_info_cache = None


# ─────────────────────────────────────────────────────────────────────────────
# Whisper model management
# ─────────────────────────────────────────────────────────────────────────────


def get_whisper_model(size: str = _DEFAULT_WHISPER_SIZE) -> Optional[Any]:
    """
    Lazily load and cache a Whisper model of the given *size*.

    The model is loaded on the first call and reused on subsequent calls.
    Loads on GPU if CUDA is available, otherwise on CPU.

    Args:
        size: One of "tiny", "base", "small", "medium", "large".
              Defaults to the WHISPER_MODEL_SIZE env var or "base".

    Returns:
        A loaded whisper model instance, or None if Whisper is unavailable.
    """
    with _whisper_model_lock:
        if size in _whisper_model_cache:
            return _whisper_model_cache[size]

        try:
            import whisper  # type: ignore

            gpu_info = detect_gpu()
            device = gpu_info["device"] if gpu_info["has_cuda"] else "cpu"

            logger.info("Loading Whisper model '%s' on device '%s'...", size, device)
            model = whisper.load_model(size, device=device)
            _whisper_model_cache[size] = model
            logger.info("Whisper model '%s' loaded successfully.", size)
            return model

        except ImportError:
            logger.warning("openai-whisper not installed; cannot load Whisper model.")
            return None
        except Exception as exc:
            logger.error("Failed to load Whisper model '%s': %s", size, exc)
            return None


def unload_whisper_model(size: Optional[str] = None) -> None:
    """
    Release the cached Whisper model from memory.

    If *size* is None, all loaded models are released.
    Useful when VRAM is needed for other tasks.
    """
    with _whisper_model_lock:
        if size is None:
            keys = list(_whisper_model_cache.keys())
        else:
            keys = [size] if size in _whisper_model_cache else []

        for k in keys:
            model = _whisper_model_cache.pop(k, None)
            if model is not None:
                try:
                    # Release GPU memory
                    import torch  # type: ignore
                    del model
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    logger.info("Whisper model '%s' unloaded.", k)
                except Exception as exc:
                    logger.debug("Error unloading Whisper model: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Whisper transcription
# ─────────────────────────────────────────────────────────────────────────────


def transcribe_with_whisper(
    audio_path: str,
    model: Optional[Any] = None,
    language: str = "en",
    task: str = "transcribe",
) -> str:
    """
    Transcribe *audio_path* using OpenAI Whisper.

    GPU-accelerated when CUDA is available; falls back to CPU transparently.
    Falls back to an empty string if Whisper is unavailable or transcription fails.

    Args:
        audio_path : Path to the audio file (WAV, MP3, FLAC, etc.)
        model      : Pre-loaded whisper model. If None, loads the default size.
        language   : Hint to Whisper about the expected language (default "en").
        task       : "transcribe" or "translate" (default "transcribe").

    Returns:
        Transcribed text as a string, or "" on failure.
    """
    if not Path(audio_path).exists():
        logger.warning("Audio file not found for Whisper transcription: %s", audio_path)
        return ""

    # Load model if not provided
    if model is None:
        model = get_whisper_model()

    if model is None:
        logger.warning("No Whisper model available; skipping transcription.")
        return ""

    try:
        import whisper  # type: ignore  # noqa: F401

        logger.debug("Whisper transcribing: %s", audio_path)
        result = model.transcribe(
            audio_path,
            language=language,
            task=task,
            fp16=detect_gpu()["has_cuda"],   # FP16 only supported on CUDA
            verbose=False,
        )
        text: str = result.get("text", "").strip()
        logger.debug("Whisper transcription complete: %d chars", len(text))
        return text

    except Exception as exc:
        logger.error("Whisper transcription failed for %s: %s", audio_path, exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# OpenCV CUDA hint
# ─────────────────────────────────────────────────────────────────────────────


def configure_opencv_cuda() -> bool:
    """
    Attempt to enable CUDA-accelerated video backend in OpenCV.

    Returns True if OpenCV was compiled with CUDA support and CUDA is available.
    This is a best-effort hint — OpenCV will fall back to CPU automatically.
    """
    try:
        import cv2  # type: ignore

        if not detect_gpu()["has_cuda"]:
            return False

        cuda_count = cv2.cuda.getCudaEnabledDeviceCount()
        if cuda_count > 0:
            cv2.cuda.setDevice(0)
            logger.info("OpenCV CUDA enabled: %d device(s) found.", cuda_count)
            return True
        else:
            logger.debug("OpenCV not compiled with CUDA support (CUDA device count=0).")
            return False
    except (ImportError, AttributeError):
        logger.debug("OpenCV CUDA API not available.")
        return False
    except Exception as exc:
        logger.debug("OpenCV CUDA configuration error: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: select STT backend
# ─────────────────────────────────────────────────────────────────────────────


def transcribe_audio(
    audio_path: str,
    whisper_model: Optional[Any] = None,
) -> str:
    """
    Unified transcription entry point.

    Automatically selects Whisper (GPU) or Vosk (CPU) based on availability
    and the BRAINROT_USE_WHISPER environment variable.

    Args:
        audio_path    : Path to WAV or audio file.
        whisper_model : Optional pre-loaded Whisper model to reuse.

    Returns:
        Transcript string (may be empty if both backends fail).
    """
    info = detect_gpu()

    if info["use_whisper"]:
        logger.debug("STT backend: Whisper (%s)", info["device"])
        return transcribe_with_whisper(audio_path, model=whisper_model)

    # Fall back to Vosk
    logger.debug("STT backend: Vosk (CPU)")
    return _transcribe_with_vosk(audio_path)


def _transcribe_with_vosk(wav_path: str) -> str:
    """
    CPU-only Vosk transcription (internal fallback).

    Kept here so gpu_utils.py is a single-file STT abstraction layer.
    The audio file must be a 16-bit mono WAV at 16 kHz.
    """
    model_path = _VOSK_MODEL_PATH
    if not Path(model_path).exists():
        logger.debug("Vosk model not found at %s; skipping transcription.", model_path)
        return ""

    try:
        import json as _json
        import wave
        from vosk import KaldiRecognizer, Model, SetLogLevel  # type: ignore

        SetLogLevel(-1)
        model = Model(model_path)

        with wave.open(wav_path, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                logger.debug("WAV is not mono/16-bit; skipping Vosk.")
                return ""
            sr = wf.getframerate()
            rec = KaldiRecognizer(model, sr)
            rec.SetWords(False)

            parts: list = []
            while True:
                data = wf.readframes(4096)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    r = _json.loads(rec.Result())
                    if r.get("text"):
                        parts.append(r["text"])

            final = _json.loads(rec.FinalResult())
            if final.get("text"):
                parts.append(final["text"])

        return " ".join(parts).strip()

    except ImportError:
        logger.debug("vosk not installed; cannot transcribe.")
        return ""
    except Exception as exc:
        logger.error("Vosk transcription error: %s", exc)
        return ""
