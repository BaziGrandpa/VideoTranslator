from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

from app.services.subtitles import TIMESTAMP_RE, timestamp_to_ms

INLINE_TIMESTAMP_RE = re.compile(r"<(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})>")
MARKUP_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def parse_vtt_captions(path: Path) -> list[dict[str, Any]]:
    """Parse VTT snapshots without treating whitespace-only body lines as separators."""
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    headers: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = TIMESTAMP_RE.search(line)
        if match:
            headers.append((index, match))

    captions: list[dict[str, Any]] = []
    for position, (line_index, match) in enumerate(headers):
        next_index = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        body_lines = [line for line in lines[line_index + 1 : next_index] if line.strip()]
        if not body_lines:
            continue
        captions.append(
            {
                "start_ms": timestamp_to_ms(match.group("start")),
                "end_ms": timestamp_to_ms(match.group("end")),
                "markup": "\n".join(body_lines),
            }
        )
    return captions


def reconstruct_vtt_tokens(path: Path, source: str = "youtube") -> list[dict[str, Any]]:
    """Convert rolling VTT display snapshots into one canonical timed-token stream."""
    emitted: list[dict[str, Any]] = []
    previous_snapshot: list[str] = []

    for caption in parse_vtt_captions(path):
        snapshot = _parse_snapshot_tokens(caption)
        if not snapshot:
            continue
        normalized = [_token_key(item["text"]) for item in snapshot]
        overlap = _suffix_prefix_overlap(previous_snapshot, normalized)

        # Very short YouTube cues are finalized display snapshots. They contain no
        # newly spoken content unless their text extends the previous snapshot.
        duration_ms = int(caption["end_ms"]) - int(caption["start_ms"])
        if duration_ms <= 50 and overlap == 0 and previous_snapshot:
            previous_snapshot = normalized
            continue

        new_tokens = snapshot[overlap:]
        if new_tokens:
            _assign_token_intervals(new_tokens, int(caption["start_ms"]), int(caption["end_ms"]))
            for token in new_tokens:
                token["sequence"] = len(emitted)
                token["source"] = source
                emitted.append(token)
        previous_snapshot = normalized

    _normalize_global_timing(emitted)
    return emitted


def tokens_from_segments(
    segments: list[dict[str, Any]],
    source: str,
) -> list[dict[str, Any]]:
    """Create timed tokens from ordinary caption or ASR segments."""
    tokens: list[dict[str, Any]] = []
    for segment in segments:
        words = str(segment.get("source_text", "")).split()
        if not words:
            continue
        start_ms = int(segment["start_ms"])
        end_ms = int(segment["end_ms"])
        duration = max(len(words), end_ms - start_ms)
        for index, word in enumerate(words):
            word_start = start_ms + round(duration * index / len(words))
            word_end = start_ms + round(duration * (index + 1) / len(words))
            tokens.append(
                {
                    "sequence": len(tokens),
                    "text": word,
                    "raw_text": word,
                    "normalized_text": word,
                    "start_ms": word_start,
                    "end_ms": max(word_start + 1, min(end_ms, word_end)),
                    "source": source,
                    "confidence": segment.get("confidence"),
                    "timing_inferred": True,
                }
            )
    _normalize_global_timing(tokens)
    return tokens


def _parse_snapshot_tokens(caption: dict[str, Any]) -> list[dict[str, Any]]:
    markup = str(caption["markup"])
    parts = INLINE_TIMESTAMP_RE.split(markup)
    current_ms = int(caption["start_ms"])
    has_explicit_time = False
    tokens: list[dict[str, Any]] = []

    for index, part in enumerate(parts):
        if index % 2:
            current_ms = timestamp_to_ms(part)
            has_explicit_time = True
            continue
        cleaned = html.unescape(MARKUP_RE.sub("", part))
        cleaned = SPACE_RE.sub(" ", cleaned).strip()
        for word in cleaned.split():
            tokens.append(
                {
                    "text": word,
                    "raw_text": word,
                    "normalized_text": word,
                    "start_ms": current_ms,
                    "end_ms": current_ms,
                    "confidence": None,
                    "timing_inferred": not has_explicit_time,
                }
            )
        has_explicit_time = False
    return tokens


def _token_key(value: str) -> str:
    return value.casefold().replace("’", "'")


def _suffix_prefix_overlap(previous: list[str], current: list[str]) -> int:
    maximum = min(len(previous), len(current))
    for size in range(maximum, 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


def _assign_token_intervals(tokens: list[dict[str, Any]], cue_start: int, cue_end: int) -> None:
    if not tokens:
        return
    starts = [max(cue_start, min(cue_end - 1, int(token["start_ms"]))) for token in tokens]

    # Untimed runs share the same provisional start. Spread them until the next
    # explicit timestamp or the end of the source cue.
    run_start = 0
    while run_start < len(tokens):
        run_end = run_start + 1
        while run_end < len(tokens) and starts[run_end] <= starts[run_start]:
            run_end += 1
        boundary = starts[run_end] if run_end < len(tokens) else cue_end
        if run_end - run_start > 1 and boundary > starts[run_start]:
            width = boundary - starts[run_start]
            count = run_end - run_start
            for offset in range(count):
                starts[run_start + offset] = starts[run_start] + round(width * offset / count)
                tokens[run_start + offset]["timing_inferred"] = True
        run_start = run_end

    for index, token in enumerate(tokens):
        start_ms = starts[index]
        next_start = starts[index + 1] if index + 1 < len(tokens) else cue_end
        token["start_ms"] = start_ms
        token["end_ms"] = max(start_ms + 1, next_start)


def _normalize_global_timing(tokens: list[dict[str, Any]]) -> None:
    for index, token in enumerate(tokens):
        token["sequence"] = index
        token["start_ms"] = max(0, int(token["start_ms"]))
        token["end_ms"] = max(token["start_ms"] + 1, int(token["end_ms"]))
        if index and token["start_ms"] < tokens[index - 1]["start_ms"]:
            token["start_ms"] = tokens[index - 1]["start_ms"]
            token["end_ms"] = max(token["start_ms"] + 1, token["end_ms"])
