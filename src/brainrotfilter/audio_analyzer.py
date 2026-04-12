"""
audio_analyzer.py - Audio content analysis for brainrot detection.

Pipeline:
  1. Extract audio from video file using ffmpeg (WAV mono 16kHz)
  2. Librosa analysis:
       - RMS energy → loudness score
       - Spectral flux + onset density → chaos score
       - Zero crossing rate
  3. Vosk speech-to-text → transcript
  4. NLP analysis on transcript:
       - Repetitive phrase detection (sliding window)
       - Nonsense/incoherence ratio
       - Keyword matches in speech
  5. Combine sub-scores → audio_score (0-100)

Sub-score weights (configurable):
  loudness_score * 0.40
  chaos_score    * 0.35
  nlp_score      * 0.25
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional, Tuple

from config import config
from models import (
    AnalysisResult,
    AudioChaosMetrics,
    AudioDetails,
    LoudnessMetrics,
    NLPFindings,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ffmpeg audio extraction
# ---------------------------------------------------------------------------


def _extract_audio(video_path: str, output_wav: str, duration_limit: int = 120) -> bool:
    """
    Use ffmpeg to extract mono 16kHz WAV audio from *video_path*.

    Limits extraction to *duration_limit* seconds.
    Returns True on success.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-t", str(duration_limit),
        "-vn",                   # no video
        "-ar", "16000",          # 16kHz sample rate (required by Vosk)
        "-ac", "1",              # mono
        "-f", "wav",
        output_wav,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=duration_limit * 2 + 30,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("ffmpeg extraction returned %d: %s", result.returncode, result.stderr[:300])
        return Path(output_wav).exists() and Path(output_wav).stat().st_size > 0
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg audio extraction timed out.")
        return False
    except Exception as exc:
        logger.error("ffmpeg error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Loudness analysis (librosa)
# ---------------------------------------------------------------------------


def _analyze_loudness(y, sr: int) -> Tuple[LoudnessMetrics, float]:
    """
    Analyse loudness characteristics using librosa arrays.

    Returns (LoudnessMetrics, loudness_score 0-100).
    """
    import numpy as np
    import librosa

    if y is None or len(y) == 0:
        return LoudnessMetrics(), 0.0

    # RMS energy per frame
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    rms_mean = float(np.mean(rms))
    rms_std = float(np.std(rms))

    # Convert to dB for dynamic range calculation
    rms_db = librosa.amplitude_to_db(rms + 1e-10)
    dynamic_range = float(np.percentile(rms_db, 95) - np.percentile(rms_db, 5))

    # Peak-to-average power ratio (PAPR) in dB
    peak = float(np.max(np.abs(y)))
    avg_power = float(np.sqrt(np.mean(y ** 2))) + 1e-10
    papr_db = 20 * np.log10(peak / avg_power) if avg_power > 0 else 0.0

    # Score: loud (low dynamic range, high RMS) = higher score
    # High PAPR with low dynamic range = heavy limiting = loud/aggressive
    norm_rms = min(rms_mean / 0.3, 1.0)           # 0.3 = typical loud video
    norm_dr = max(0.0, 1.0 - (dynamic_range / 40.0))  # less range = louder
    loudness_score = round(((norm_rms * 0.6) + (norm_dr * 0.4)) * 100.0, 2)
    loudness_score = min(100.0, max(0.0, loudness_score))

    metrics = LoudnessMetrics(
        rms_mean=round(rms_mean, 6),
        rms_std=round(rms_std, 6),
        dynamic_range_db=round(dynamic_range, 2),
        peak_to_avg_ratio=round(papr_db, 2),
        loudness_score=loudness_score,
    )
    return metrics, loudness_score


# ---------------------------------------------------------------------------
# Audio chaos analysis (librosa)
# ---------------------------------------------------------------------------


def _analyze_chaos(y, sr: int) -> Tuple[AudioChaosMetrics, float]:
    """
    Analyse chaotic / erratic audio characteristics.

    High spectral flux = rapidly changing spectrum (jarring cuts, TikTok audio).
    High onset density = frequent beat events / sound effects.
    Returns (AudioChaosMetrics, chaos_score 0-100).
    """
    import numpy as np
    import librosa

    if y is None or len(y) == 0:
        return AudioChaosMetrics(), 0.0

    # Spectral flux (frame-to-frame spectral change)
    stft = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    diff = np.diff(stft, axis=1)
    flux = np.sum(np.maximum(diff, 0), axis=0)
    flux_mean = float(np.mean(flux))
    flux_std = float(np.std(flux))

    # Zero crossing rate
    zcr = librosa.feature.zero_crossing_rate(y, frame_length=2048, hop_length=512)[0]
    zcr_mean = float(np.mean(zcr))

    # Onset density (number of onsets per second)
    onset_frames = librosa.onset.onset_detect(y=y, sr=sr, hop_length=512)
    duration_s = len(y) / sr
    onset_density = len(onset_frames) / max(duration_s, 1.0)

    # Normalise
    norm_flux = min(flux_mean / 500.0, 1.0)          # 500 = empirically loud
    norm_zcr = min(zcr_mean / 0.2, 1.0)               # 0.2 = high ZCR
    norm_onset = min(onset_density / 10.0, 1.0)        # 10 onsets/s = very chaotic

    chaos_score = round(
        (norm_flux * 0.5 + norm_zcr * 0.2 + norm_onset * 0.3) * 100.0, 2
    )
    chaos_score = min(100.0, max(0.0, chaos_score))

    metrics = AudioChaosMetrics(
        spectral_flux_mean=round(flux_mean, 4),
        spectral_flux_std=round(flux_std, 4),
        zero_crossing_rate=round(zcr_mean, 6),
        onset_density=round(onset_density, 4),
        chaos_score=chaos_score,
    )
    return metrics, chaos_score


# ---------------------------------------------------------------------------
# Vosk speech-to-text
# ---------------------------------------------------------------------------


def _transcribe_audio(wav_path: str) -> str:
    """
    Run Vosk speech-to-text on the WAV file.

    Returns a transcript string. Falls back to empty string if Vosk is
    unavailable or the model path is not configured.
    """
    model_path = config.vosk_model_path
    if not Path(model_path).exists():
        logger.debug("Vosk model not found at %s; skipping transcription.", model_path)
        return ""

    try:
        import wave
        from vosk import Model, KaldiRecognizer, SetLogLevel

        SetLogLevel(-1)  # silence Vosk console output
        model = Model(model_path)

        with wave.open(wav_path, "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                logger.debug("WAV format not mono/16-bit for %s; skipping.", wav_path)
                return ""
            sample_rate = wf.getframerate()
            recognizer = KaldiRecognizer(model, sample_rate)
            recognizer.SetWords(False)

            transcript_parts: List[str] = []
            while True:
                data = wf.readframes(4096)
                if not data:
                    break
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "")
                    if text:
                        transcript_parts.append(text)

            # Final partial result
            final = json.loads(recognizer.FinalResult())
            if final.get("text"):
                transcript_parts.append(final["text"])

        return " ".join(transcript_parts).strip()

    except ImportError:
        logger.debug("vosk not installed; skipping transcription.")
        return ""
    except Exception as exc:
        logger.error("Vosk transcription error: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# NLP analysis
# ---------------------------------------------------------------------------


def _analyze_nlp(
    transcript: str,
    keyword_analyzer_module: Optional[Any] = None,
) -> Tuple[NLPFindings, float]:
    """
    Perform NLP analysis on *transcript*.

    Detects:
      - Repetitive phrases (3–5 grams that appear frequently)
      - High ratio of repeated tokens (nonsense speech indicator)
      - Keyword matches from the keyword list
    """
    if not transcript:
        return NLPFindings(), 0.0

    words = transcript.lower().split()
    if not words:
        return NLPFindings(), 0.0

    # 1. Repetitive phrase detection (n-grams)
    repetitive: List[str] = []
    for n in (3, 4, 5):
        ngrams = [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]
        counts = Counter(ngrams)
        for phrase, cnt in counts.items():
            if cnt >= 3:  # same phrase 3+ times
                repetitive.append(phrase)

    # 2. Nonsense ratio: fraction of tokens that are repeated beyond threshold
    word_counts = Counter(words)
    total = len(words)
    repeated_tokens = sum(
        cnt for cnt in word_counts.values() if cnt > total * 0.15 and total > 20
    )
    nonsense_ratio = round(min(repeated_tokens / max(total, 1), 1.0), 4)

    # 3. Keyword matches in speech
    speech_keyword_hits: List[str] = []
    try:
        import keyword_analyzer as ka

        for kw_def in ka._kw_list.keywords:
            kw = kw_def.get("keyword", "").lower()
            if kw and kw in transcript.lower():
                speech_keyword_hits.append(kw)
    except Exception:
        pass

    # Score
    repetition_score = min(len(repetitive) * 10.0, 50.0)
    nonsense_component = nonsense_ratio * 30.0
    kw_component = min(len(speech_keyword_hits) * 5.0, 20.0)
    nlp_score = round(
        min(repetition_score + nonsense_component + kw_component, 100.0), 2
    )

    findings = NLPFindings(
        repetitive_phrases=repetitive[:10],  # cap for storage
        nonsense_ratio=nonsense_ratio,
        keyword_hits=list(set(speech_keyword_hits))[:20],
        nlp_score=nlp_score,
    )
    return findings, nlp_score


# ---------------------------------------------------------------------------
# Main analyzer function
# ---------------------------------------------------------------------------


def analyze(video_path: str, video_id: str = "") -> AnalysisResult:
    """
    Run the full audio analysis pipeline on a downloaded video file.

    Args:
        video_path: Local path to the downloaded video file.
        video_id:   YouTube video ID (for logging only).

    Returns:
        AnalysisResult with module="audio", score, details.
    """
    start = time.monotonic()

    if not Path(video_path).exists():
        elapsed = time.monotonic() - start
        return AnalysisResult(
            module="audio",
            score=0.0,
            details={"error": f"Video file not found: {video_path}"},
            error="Video file not found",
            duration_s=round(elapsed, 3),
        )

    with tempfile.TemporaryDirectory(prefix="brainrot_audio_") as tmpdir:
        wav_path = os.path.join(tmpdir, "audio.wav")

        # 1. Extract audio
        duration_limit = config.full_scan_time_limit
        if not _extract_audio(video_path, wav_path, duration_limit=duration_limit):
            elapsed = time.monotonic() - start
            return AnalysisResult(
                module="audio",
                score=0.0,
                details={"error": "Audio extraction failed"},
                error="Audio extraction failed",
                duration_s=round(elapsed, 3),
            )

        # 2. Load audio with librosa
        try:
            import librosa
            import numpy as np  # noqa: F401

            y, sr = librosa.load(wav_path, sr=16000, mono=True, duration=duration_limit)
            audio_duration = len(y) / sr
        except ImportError:
            logger.error("librosa not installed; cannot perform audio analysis.")
            elapsed = time.monotonic() - start
            return AnalysisResult(
                module="audio",
                score=0.0,
                details={"error": "librosa not available"},
                error="librosa not available",
                duration_s=round(elapsed, 3),
            )
        except Exception as exc:
            logger.error("Failed to load audio for %s: %s", video_id, exc)
            elapsed = time.monotonic() - start
            return AnalysisResult(
                module="audio",
                score=0.0,
                details={"error": str(exc)},
                error=str(exc),
                duration_s=round(elapsed, 3),
            )

        # 3. Loudness & chaos
        loudness_metrics, loudness_score = _analyze_loudness(y, sr)
        chaos_metrics, chaos_score = _analyze_chaos(y, sr)

        # 4. Speech transcription
        transcript = _transcribe_audio(wav_path)

    # 5. NLP (done outside tmpdir since it only uses the transcript string)
    nlp_findings, nlp_score = _analyze_nlp(transcript)

    # 6. Combined audio score
    w_loudness = float(config.get("audio_loudness_weight", 0.40))
    w_chaos = float(config.get("audio_chaos_weight", 0.35))
    w_nlp = float(config.get("audio_nlp_weight", 0.25))

    audio_score = round(
        loudness_score * w_loudness + chaos_score * w_chaos + nlp_score * w_nlp,
        2,
    )
    audio_score = min(100.0, max(0.0, audio_score))

    elapsed = time.monotonic() - start

    audio_details = AudioDetails(
        loudness=loudness_metrics,
        chaos=chaos_metrics,
        nlp=nlp_findings,
        speech_text=transcript[:2000],  # cap stored text
        audio_duration_s=round(audio_duration, 2),
    )

    logger.info(
        "Audio analysis for %s: score=%.1f (loudness=%.1f, chaos=%.1f, nlp=%.1f), duration=%.2fs",
        video_id or video_path,
        audio_score,
        loudness_score,
        chaos_score,
        nlp_score,
        elapsed,
    )

    return AnalysisResult(
        module="audio",
        score=audio_score,
        details=audio_details.model_dump(),
        duration_s=round(elapsed, 3),
    )
