from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.services.subtitle_text import (
    display_target_text,
    normalize_source_text,
    normalize_target_text,
)

LONG_PAUSE_MS = 1200
MIN_FRAGMENT_WORDS = 3
MIN_CONTINUATION_WORDS = 4
MAX_CUE_WORDS = 40

_KNOWN_STAGE_RE = re.compile(
    r"^[\[［【（(]\s*(?:clears?\s+throat|throat\s+clearing|laughs?|laughing|chuckles?|snorts?|sighs?|gasps?|music|"
    r"applause|cheering|inaudible|crosstalk|laughter|笑|笑声|轻笑|哼笑|音乐|"
    r"掌声|欢呼|叹气|吸气|听不清|杂音)\s*[\]］】）)]$",
    re.IGNORECASE,
)
_SPEAKER_MARKERS = {">>", "››"}
_MUSIC_MARKERS = {"♪", "♫", "♬"}
_TRAILING_CLOSERS = "\"'”’》」』】）)]}"
_ABBREVIATIONS = {
    "dr.", "mr.", "mrs.", "ms.", "prof.", "sr.", "jr.", "st.",
    "vs.", "etc.", "e.g.", "i.e.", "u.s.", "u.k.",
}


def token_kind(token_or_text: dict[str, Any] | str) -> str:
    text = str(token_or_text.get("text", "") if isinstance(token_or_text, dict) else token_or_text)
    stripped = text.strip()
    if stripped in _SPEAKER_MARKERS:
        return "speaker_marker"
    if stripped in _MUSIC_MARKERS or _KNOWN_STAGE_RE.fullmatch(stripped):
        return "stage_direction"
    if stripped and not any(char.isalnum() for char in stripped):
        return "punctuation"
    return "speech"


def classify_token_kinds(tokens: list[dict[str, Any]]) -> list[str]:
    """Classify tokens while recognizing annotations split across token boundaries."""
    kinds = [token_kind(token) for token in tokens]
    opening_marks = "[［【（("
    closing_marks = "]］】）)"
    for start, token in enumerate(tokens):
        if kinds[start] != "speech":
            continue
        first = str(token.get("text", "")).strip()
        if not first.startswith(tuple(opening_marks)):
            continue
        pieces: list[str] = []
        for end in range(start, min(len(tokens), start + 8)):
            pieces.append(str(tokens[end].get("text", "")).strip())
            candidate = " ".join(pieces)
            if _KNOWN_STAGE_RE.fullmatch(candidate):
                kinds[start : end + 1] = ["stage_direction"] * (end - start + 1)
                break
            if any(mark in pieces[-1] for mark in closing_marks):
                break
    return kinds


def visible_source_text(
    tokens: list[dict[str, Any]],
    start: int,
    end: int,
    kinds: list[str] | None = None,
) -> str:
    kinds = kinds or classify_token_kinds(tokens)
    pieces = [
        str(tokens[index]["text"])
        for index in range(start, end + 1)
        if kinds[index] in {"speech", "punctuation"}
    ]
    value = normalize_source_text(" ".join(pieces))
    return re.sub(r"\s+([,.;:!?])", r"\1", value).strip()


def visible_token_ids(
    tokens: list[dict[str, Any]],
    start: int,
    end: int,
    kinds: list[str] | None = None,
) -> list[int]:
    kinds = kinds or classify_token_kinds(tokens)
    return [index for index in range(start, end + 1) if kinds[index] == "speech"]


def visible_timing(
    tokens: list[dict[str, Any]],
    start: int,
    end: int,
    kinds: list[str] | None = None,
) -> tuple[int, int]:
    visible = visible_token_ids(tokens, start, end, kinds)
    if not visible:
        raise ValueError(f"Token range {start}-{end} contains no spoken text")
    return int(tokens[visible[0]]["start_ms"]), int(tokens[visible[-1]]["end_ms"])


def build_cues(
    tokens: list[dict[str, Any]],
    start_token_id: int = 0,
    end_token_id: int | None = None,
) -> list[dict[str, Any]]:
    if not tokens:
        return []
    end_token_id = len(tokens) - 1 if end_token_id is None else end_token_id
    if not 0 <= start_token_id <= end_token_id < len(tokens):
        raise ValueError("Invalid Cue Token range")

    kinds = classify_token_kinds(tokens)
    cuts: dict[int, str] = {}
    previous_speech: int | None = None
    for index in range(start_token_id, end_token_id + 1):
        kind = kinds[index]
        if kind == "speaker_marker":
            if index > start_token_id:
                cuts[index - 1] = "hard"
            previous_speech = None
            continue
        if kind != "speech":
            continue
        if previous_speech is not None:
            gap = int(tokens[index]["start_ms"]) - int(tokens[previous_speech]["end_ms"])
            if gap >= LONG_PAUSE_MS:
                cuts[previous_speech] = "hard"
        boundary = _terminal_boundary(str(tokens[index]["text"]))
        if boundary:
            cuts[index] = boundary
        previous_speech = index

    raw_ranges: list[dict[str, Any]] = []
    range_start = start_token_id
    for cut in sorted(index for index in cuts if start_token_id <= index < end_token_id):
        if cut < range_start:
            continue
        raw_ranges.append({"start": range_start, "end": cut, "boundary": cuts[cut]})
        range_start = cut + 1
    raw_ranges.append({"start": range_start, "end": end_token_id, "boundary": "part_end"})
    raw_ranges = _absorb_empty_ranges(tokens, raw_ranges, kinds)
    raw_ranges = _merge_short_ranges(tokens, raw_ranges, kinds)
    raw_ranges = _split_oversized_ranges(tokens, raw_ranges, kinds)

    result: list[dict[str, Any]] = []
    for item in raw_ranges:
        start, end = int(item["start"]), int(item["end"])
        source = visible_source_text(tokens, start, end, kinds)
        if not source:
            continue
        start_ms, end_ms = visible_timing(tokens, start, end, kinds)
        result.append(
            {
                "cue_id": len(result) + 1,
                "start_token_id": start,
                "end_token_id": end,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "source_text": source,
                "hidden_token_ids": [
                    index
                    for index in range(start, end + 1)
                    if kinds[index] in {"stage_direction", "speaker_marker"}
                ],
            }
        )
    _validate_cue_coverage(result, start_token_id, end_token_id)
    return result


def materialize_aligned_cues(
    tokens: list[dict[str, Any]],
    cues: list[dict[str, Any]],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = result.get("cues") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise TypeError("Pass 2 response must contain a cues array")
    expected_ids = [int(item["cue_id"]) for item in cues]
    received: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("Pass 2 Cue must be an object")
        cue_id = row.get("cue_id")
        chinese = row.get("target_text")
        if isinstance(cue_id, bool) or not isinstance(cue_id, int):
            raise TypeError("Pass 2 returned an invalid cue_id")
        if cue_id in received:
            raise ValueError(f"Pass 2 duplicated cue_id {cue_id}")
        if not isinstance(chinese, str) or not display_target_text(chinese):
            raise ValueError(f"Pass 2 returned empty Chinese for cue_id {cue_id}")
        received[cue_id] = normalize_target_text(chinese)
    if list(received) != expected_ids:
        raise ValueError(
            f"Pass 2 cue_id coverage mismatch; expected {expected_ids}, "
            f"received {list(received)}"
        )

    kinds = classify_token_kinds(tokens)
    segments: list[dict[str, Any]] = []
    for cue in cues:
        cue_id = int(cue["cue_id"])
        start, end = int(cue["start_token_id"]), int(cue["end_token_id"])
        start_ms, end_ms = visible_timing(tokens, start, end, kinds)
        chinese = received[cue_id]
        segments.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start_token_id": start,
                "end_token_id": end,
                "cue_id": cue_id,
                "source_text": str(cue["source_text"]),
                "target_text": chinese,
            }
        )
    return segments


def materialize_subcues(
    tokens: list[dict[str, Any]],
    parent_segments: list[dict[str, Any]],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = result.get("cues") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise TypeError("Pass 3 response must contain a cues array")
    parents = {int(item["cue_id"]): item for item in parent_segments}
    received: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("Pass 3 Cue must be an object")
        cue_id = row.get("cue_id")
        subs = row.get("subcues")
        if isinstance(cue_id, bool) or not isinstance(cue_id, int):
            raise TypeError("Pass 3 returned an invalid cue_id")
        if cue_id in received:
            raise ValueError(f"Pass 3 duplicated cue_id {cue_id}")
        if cue_id not in parents or not isinstance(subs, list) or not subs:
            raise ValueError(f"Pass 3 returned invalid subcues for cue_id {cue_id}")
        received[cue_id] = subs
    if list(received) != list(parents):
        raise ValueError(
            f"Pass 3 cue_id coverage mismatch; expected {list(parents)}, "
            f"received {list(received)}"
        )

    kinds = classify_token_kinds(tokens)
    materialized: list[dict[str, Any]] = []
    for cue_id, parent in parents.items():
        next_start = int(parent["start_token_id"])
        parent_end = int(parent["end_token_id"])
        for subcue_id, sub in enumerate(received[cue_id], start=1):
            if not isinstance(sub, dict):
                raise TypeError("Pass 3 Sub-cue must be an object")
            end = sub.get("end_token_id")
            chinese = sub.get("target_text")
            if isinstance(end, bool) or not isinstance(end, int) or not next_start <= end <= parent_end:
                raise ValueError(f"Pass 3 returned an invalid end_token_id for cue_id {cue_id}")
            if not isinstance(chinese, str) or not display_target_text(chinese):
                raise ValueError(f"Pass 3 returned empty Chinese for cue_id {cue_id}")
            if not visible_token_ids(tokens, next_start, end, kinds):
                raise ValueError(f"Pass 3 created a non-speech-only range {next_start}-{end}")
            start_ms, end_ms = visible_timing(tokens, next_start, end, kinds)
            source = visible_source_text(tokens, next_start, end, kinds)
            translated = normalize_target_text(chinese)
            materialized.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "start_token_id": next_start,
                    "end_token_id": end,
                    "cue_id": cue_id,
                    "subcue_id": subcue_id,
                    "source_text": source,
                    "target_text": translated,
                    "quality_flags": [],
                    "status": "machine_translated",
                }
            )
            next_start = end + 1
        if next_start != parent_end + 1:
            raise ValueError(f"Pass 3 did not end cue_id {cue_id} at its parent boundary")
    return materialized


def token_payload(tokens: list[dict[str, Any]], start: int, end: int) -> list[dict[str, Any]]:
    kinds = classify_token_kinds(tokens)
    return [
        {"token_id": index, "text": str(tokens[index]["text"]), "kind": kinds[index]}
        for index in range(start, end + 1)
    ]


def _terminal_boundary(text: str) -> str | None:
    stripped = text.strip().rstrip(_TRAILING_CLOSERS)
    lowered = stripped.casefold()
    if lowered in _ABBREVIATIONS or re.search(r"\d\.\d", stripped):
        return None
    if stripped.endswith(("?", "!", ".")):
        return "complete"
    if stripped.endswith((",", ":", ";")):
        return "continuation"
    return None


def _word_count(
    tokens: list[dict[str, Any]], item: dict[str, Any], kinds: list[str]
) -> int:
    return sum(
        kinds[index] == "speech"
        for index in range(int(item["start"]), int(item["end"]) + 1)
    )


def _absorb_empty_ranges(
    tokens: list[dict[str, Any]],
    ranges: list[dict[str, Any]],
    kinds: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    prefix_start: int | None = None
    for item in ranges:
        if _word_count(tokens, item, kinds) == 0:
            if result:
                result[-1]["end"] = item["end"]
                result[-1]["boundary"] = item["boundary"]
            elif prefix_start is None:
                prefix_start = int(item["start"])
            continue
        current = dict(item)
        if prefix_start is not None:
            current["start"] = prefix_start
            prefix_start = None
        result.append(current)
    return result


def _boundary_is_hard(ranges: list[dict[str, Any]], left_index: int) -> bool:
    return str(ranges[left_index].get("boundary")) == "hard"


def _merge_short_ranges(
    tokens: list[dict[str, Any]],
    ranges: list[dict[str, Any]],
    kinds: list[str],
) -> list[dict[str, Any]]:
    ranges = [dict(item) for item in ranges]
    changed = True
    while changed and len(ranges) > 1:
        changed = False
        for index, item in enumerate(ranges):
            minimum_words = (
                MIN_CONTINUATION_WORDS
                if item.get("boundary") == "continuation"
                else MIN_FRAGMENT_WORDS
            )
            if _word_count(tokens, item, kinds) >= minimum_words:
                continue
            preferred = index + 1 if item.get("boundary") == "continuation" else index - 1
            alternatives = [preferred, index - 1 if preferred == index + 1 else index + 1]
            target = None
            for candidate in alternatives:
                if not 0 <= candidate < len(ranges):
                    continue
                if _boundary_is_hard(ranges, min(index, candidate)):
                    continue
                target = candidate
                break
            if target is None:
                continue
            left, right = min(index, target), max(index, target)
            ranges[left] = {
                "start": ranges[left]["start"],
                "end": ranges[right]["end"],
                "boundary": ranges[right]["boundary"],
            }
            del ranges[right]
            changed = True
            break
    return ranges


def _split_oversized_ranges(
    tokens: list[dict[str, Any]],
    ranges: list[dict[str, Any]],
    kinds: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in ranges:
        start, end = int(item["start"]), int(item["end"])
        speech_ids = visible_token_ids(tokens, start, end, kinds)
        while len(speech_ids) > MAX_CUE_WORDS:
            cut = speech_ids[MAX_CUE_WORDS - 1]
            result.append({"start": start, "end": cut, "boundary": "fallback"})
            start = cut + 1
            speech_ids = visible_token_ids(tokens, start, end, kinds)
        result.append({"start": start, "end": end, "boundary": item["boundary"]})
    return result


def _validate_cue_coverage(cues: Iterable[dict[str, Any]], start: int, end: int) -> None:
    expected = start
    rows = list(cues)
    for cue in rows:
        if int(cue["start_token_id"]) != expected:
            raise RuntimeError("Cue Token coverage is not contiguous")
        expected = int(cue["end_token_id"]) + 1
    if not rows or expected != end + 1:
        raise RuntimeError("Cue Token coverage is incomplete")
