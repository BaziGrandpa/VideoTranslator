from __future__ import annotations

from typing import Any

MIN_PART_MS = 20 * 60 * 1000
TARGET_PART_MS = 30 * 60 * 1000
MAX_PART_MS = 40 * 60 * 1000


def build_subtitle_parts(
    tokens: list[dict[str, Any]],
    chapter_starts_ms: list[int] | None = None,
) -> list[dict[str, Any]]:
    if not tokens:
        return []
    total_ms = int(tokens[-1]["end_ms"]) - int(tokens[0]["start_ms"])
    if total_ms <= MAX_PART_MS:
        boundaries = [len(tokens) - 1]
    else:
        part_count = max(2, round(total_ms / TARGET_PART_MS))
        while total_ms / part_count > MAX_PART_MS:
            part_count += 1
        while part_count > 1 and total_ms / part_count < MIN_PART_MS:
            part_count -= 1
        boundaries = _balanced_natural_boundaries(tokens, part_count, chapter_starts_ms or [])

    result: list[dict[str, Any]] = []
    start = 0
    for end in boundaries:
        result.append(
            {
                "start_token_id": start,
                "end_token_id": end,
                "start_ms": int(tokens[start]["start_ms"]),
                "end_ms": int(tokens[end]["end_ms"]),
            }
        )
        start = end + 1
    return result


def _balanced_natural_boundaries(
    tokens: list[dict[str, Any]], part_count: int, chapter_starts_ms: list[int]
) -> list[int]:
    first_ms = int(tokens[0]["start_ms"])
    total_ms = int(tokens[-1]["end_ms"]) - first_ms
    boundaries: list[int] = []
    previous_end = -1
    for part_number in range(1, part_count):
        target_ms = first_ms + round(total_ms * part_number / part_count)
        remaining_parts = part_count - part_number
        minimum_ms = (
            first_ms if previous_end < 0 else int(tokens[previous_end]["end_ms"])
        ) + MIN_PART_MS
        maximum_ms = min(
            (first_ms if previous_end < 0 else int(tokens[previous_end]["end_ms"])) + MAX_PART_MS,
            int(tokens[-1]["end_ms"]) - remaining_parts * MIN_PART_MS,
        )
        candidates = []
        for index in range(previous_end + 1, len(tokens) - 1):
            end_ms = int(tokens[index]["end_ms"])
            if end_ms < minimum_ms or end_ms > maximum_ms:
                continue
            gap_ms = max(0, int(tokens[index + 1]["start_ms"]) - end_ms)
            sentence_end = str(tokens[index]["text"]).endswith((".", "?", "!"))
            is_chapter = any(
                abs(int(tokens[index + 1]["start_ms"]) - value) <= 1500
                for value in chapter_starts_ms
            )
            if is_chapter:
                priority = -1
            elif gap_ms >= 1800:
                priority = 0
            elif sentence_end and gap_ms >= 500:
                priority = 1
            elif sentence_end:
                priority = 2
            else:
                priority = 3
            candidates.append((priority, abs(end_ms - target_ms), index))
        if not candidates:
            candidates = [
                (3, abs(int(token["end_ms"]) - target_ms), index)
                for index, token in enumerate(tokens[:-1])
                if index > previous_end
            ]
        chosen = min(candidates)[2]
        boundaries.append(chosen)
        previous_end = chosen
    boundaries.append(len(tokens) - 1)
    return boundaries
