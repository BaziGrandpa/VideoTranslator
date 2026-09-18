from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.caption_tokens import tokens_from_segments

logger = logging.getLogger(__name__)


def transcribe_tokens(video_path: Path) -> list[dict[str, Any]]:
    try:
        import torch
        import whisperx
    except ImportError as exc:
        raise RuntimeError(
            "The video has no English subtitles and WhisperX is not installed. "
            "Run pip install -e .[asr]"
        ) from exc

    device = settings.whisper_device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = settings.whisper_compute_type
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    # WhisperX invokes a command named `ffmpeg` internally. Make the configured
    # binary discoverable even when Windows has not refreshed PATH yet.
    ffmpeg_path = Path(settings.ffmpeg_bin)
    if ffmpeg_path.is_absolute():
        os.environ["PATH"] = f"{ffmpeg_path.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    audio = whisperx.load_audio(str(video_path))
    model = whisperx.load_model(
        settings.whisper_model,
        device,
        compute_type=compute_type,
        language="en",
    )
    result = model.transcribe(audio, batch_size=8, language="en")
    try:
        align_model, metadata = whisperx.load_align_model(language_code="en", device=device)
        result = whisperx.align(
            result["segments"], align_model, metadata, audio, device, return_char_alignments=False
        )
    except Exception as exc:  # noqa: BLE001 - alignment failure should not discard ASR output
        # Segment timestamps are still usable if forced alignment is unavailable.
        logger.warning("WhisperX forced alignment failed; using ASR segment times: %s", exc)
    segments = [
        {
            "start_ms": int(float(segment["start"]) * 1000),
            "end_ms": int(float(segment["end"]) * 1000),
            "source_text": str(segment["text"]).strip(),
        }
        for segment in result.get("segments", [])
        if str(segment.get("text", "")).strip()
    ]
    aligned_tokens: list[dict[str, Any]] = []
    for segment in result.get("segments", []):
        words = segment.get("words") or []
        if not words or any(word.get("start") is None or word.get("end") is None for word in words):
            fallback_segments = [
                {
                    "start_ms": int(float(segment["start"]) * 1000),
                    "end_ms": int(float(segment["end"]) * 1000),
                    "source_text": str(segment.get("text", "")).strip(),
                }
            ]
            for token in tokens_from_segments(fallback_segments, source="whisperx_segment"):
                token["sequence"] = len(aligned_tokens)
                aligned_tokens.append(token)
            continue
        for word in words:
            text = str(word.get("word", "")).strip()
            if not text:
                continue
            aligned_tokens.append(
                {
                    "sequence": len(aligned_tokens),
                    "text": text,
                    "raw_text": text,
                    "normalized_text": text,
                    "start_ms": int(float(word["start"]) * 1000),
                    "end_ms": int(float(word["end"]) * 1000),
                    "source": "whisperx_aligned",
                    "confidence": word.get("score"),
                    "timing_inferred": False,
                }
            )
    if aligned_tokens:
        return aligned_tokens
    return tokens_from_segments(segments, source="whisperx_segment")


def transcribe(video_path: Path) -> list[dict[str, Any]]:
    """Compatibility wrapper returning cue-like ASR segments."""
    tokens = transcribe_tokens(video_path)
    if not tokens:
        return []
    return [
        {
            "start_ms": tokens[0]["start_ms"],
            "end_ms": tokens[-1]["end_ms"],
            "source_text": " ".join(str(token["text"]) for token in tokens),
        }
    ]
