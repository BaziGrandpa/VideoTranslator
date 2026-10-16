from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass

from app.services.cues import classify_token_kinds
from app.services.subtitle_text import display_target_text


@dataclass(frozen=True)
class SubtitleQualityRules:
    max_duration_ms: int = 5000
    short_duration_ms: int = 700
    short_source_words: int = 2
    short_target_units: int = 4
    normal_max_target_units: int = 18
    max_target_units: int = 24
    allowed_over_normal_ratio: float = 0.02
    min_long_segment_units_per_second: float = 2.5
    max_target_units_per_second: float = 12.0

    def payload(self) -> dict:
        return asdict(self)


RULES = SubtitleQualityRules()
PROMPT_VERSION = "cue-segment-pipeline-v1"


def target_units(text: str) -> float:
    """Approximate display load: wide characters count as 1, others as 0.5."""
    return sum(
        1.0 if unicodedata.east_asian_width(char) in {"W", "F"} else 0.5
        for char in display_target_text(text)
        if not char.isspace()
    )


def quality_issues(start_ms: int, end_ms: int, source: str, target: str) -> list[dict]:
    duration = max(1, end_ms - start_ms)
    units = target_units(target)
    units_per_second = units * 1000 / duration
    issues = []
    visible_characters = [
        char for char in display_target_text(target)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    ]

    def add(kind: str, actual: float, limit: float) -> None:
        issues.append({"type": kind, "actual": round(actual, 3), "limit": limit})

    if duration < RULES.short_duration_ms and (
        len(source.split()) > RULES.short_source_words or units > RULES.short_target_units
    ):
        add("very_short_duration", duration, RULES.short_duration_ms)
    if (
        duration > RULES.max_duration_ms
        and units_per_second < RULES.min_long_segment_units_per_second
    ):
        add("very_long_duration", duration, RULES.max_duration_ms)
    if units and units_per_second > RULES.max_target_units_per_second:
        add("high_target_reading_speed", units_per_second, RULES.max_target_units_per_second)
    if units > RULES.max_target_units:
        add("target_text_too_long", units, RULES.max_target_units)
    if len(visible_characters) == 1 and "\u3400" <= visible_characters[0] <= "\u9fff":
        add("single_chinese_character", 1, 2)
    return issues


def analyze_quality(start_ms: int, end_ms: int, source_text: str, translation: str) -> list[str]:
    return [item["type"] for item in quality_issues(start_ms, end_ms, source_text, translation)]


PASS3_INVALID_OUTPUT = "pass3_invalid_output"
AUDIO_EFFECT_BOUNDARY = "audio_effect_boundary"


def audio_effect_boundary_segment_ids(
    tokens: list[dict],
    segments: list[dict],
    component_token_ranges: dict[int, list[tuple[int, int]]] | None = None,
) -> set[int]:
    """Find segments on both sides of an unfinished sentence interrupted by an audio label."""
    kinds = classify_token_kinds(tokens)
    if not kinds:
        return set()
    token_ids = [int(token.get("token_id", index)) for index, token in enumerate(tokens)]
    owners: dict[int, set[int]] = {}
    for segment in segments:
        segment_id = int(segment["id"])
        start, end = segment.get("start_token_id"), segment.get("end_token_id")
        ranges = (
            [(int(start), int(end))]
            if start is not None and end is not None
            else (component_token_ranges or {}).get(segment_id, [])
        )
        for range_start, range_end in ranges:
            for token_id in range(range_start, range_end + 1):
                owners.setdefault(token_id, set()).add(segment_id)
    flagged: set[int] = set()
    index = 0
    while index < len(tokens):
        if kinds[index] != "stage_direction":
            index += 1
            continue
        start = index
        while index < len(tokens) and kinds[index] == "stage_direction":
            index += 1
        labels = " ".join(str(token["text"]).casefold() for token in tokens[start:index])
        if not any(label in labels for label in ("music", "applause", "音乐", "掌声", "♪", "♫", "♬")):
            continue
        before = start - 1
        while before >= 0 and kinds[before] != "speech":
            before -= 1
        after = index
        while after < len(tokens) and kinds[after] != "speech":
            after += 1
        if before < 0 or after == len(tokens):
            continue
        preceding = str(tokens[before]["text"]).strip().rstrip("\"'”’》」』】）)]}")
        if preceding.endswith((".", "?", "!")):
            continue
        left_ids = owners.get(token_ids[before], set())
        right_ids = owners.get(token_ids[after], set())
        if left_ids and right_ids:
            flagged.update(left_ids | right_ids)
    return flagged


def stamp_segment_review_flags(segments: list[dict]) -> list[dict]:
    """Mark review flags once, after a Part's Pass 3 list is final."""
    stamped: list[dict] = []
    for segment in segments:
        item = dict(segment)
        mechanical = analyze_quality(
            int(item["start_ms"]),
            int(item["end_ms"]),
            str(item.get("source_text", "")),
            str(item.get("target_text", "")),
        )
        flags = []
        if PASS3_INVALID_OUTPUT in (segment.get("quality_flags") or []):
            flags.append(PASS3_INVALID_OUTPUT)
        flags.extend(kind for kind in mechanical if kind not in flags)
        item["quality_flags"] = flags
        item["status"] = "needs_review" if flags else "machine_translated"
        stamped.append(item)
    return stamped


def segment_review_problems(segment: dict) -> list[dict]:
    problems = quality_issues(
        int(segment["start_ms"]),
        int(segment["end_ms"]),
        str(segment.get("source_text", "")),
        str(segment.get("target_text", "")),
    )
    if PASS3_INVALID_OUTPUT in (segment.get("quality_flags") or []):
        problems.append({"type": PASS3_INVALID_OUTPUT, "actual": 0, "limit": 0})
    return problems


def describe_review_problems(problems: list[dict]) -> str:
    parts: list[str] = []
    for problem in problems:
        kind = problem["type"]
        actual = problem.get("actual")
        limit = problem.get("limit")
        if kind == "target_text_too_long":
            parts.append(f"Chinese is {actual} display units; the limit is {limit}.")
        elif kind == "very_short_duration":
            parts.append(f"Duration {actual} ms is below {limit} ms.")
        elif kind == "very_long_duration":
            parts.append(
                f"Duration {actual} ms exceeds {limit} ms, with fewer than "
                f"{RULES.min_long_segment_units_per_second} display units/s."
            )
        elif kind == "high_target_reading_speed":
            parts.append(f"Reading speed is {actual} units/s; the limit is {limit}.")
        elif kind == "single_chinese_character":
            parts.append("Chinese subtitle has only one character.")
        elif kind == PASS3_INVALID_OUTPUT:
            parts.append("Pass 3 could not split this cue.")
        elif kind == AUDIO_EFFECT_BOUNDARY:
            parts.append("Music or applause interrupts nearby speech. Check both subtitles for a split sentence or misplaced translation.")
    return " ".join(parts)


def summarize_length_budget(segments: list[dict]) -> dict:
    total = len(segments)
    over_normal = sum(
        target_units(str(segment.get("target_text", ""))) > RULES.normal_max_target_units
        for segment in segments
    )
    over_absolute = sum(
        target_units(str(segment.get("target_text", ""))) > RULES.max_target_units
        for segment in segments
    )
    allowed_over_normal = max(1, int(total * RULES.allowed_over_normal_ratio)) if total else 0
    return {
        "total_segments": total,
        "soft_limit_units": RULES.normal_max_target_units,
        "absolute_limit_units": RULES.max_target_units,
        "over_soft_limit": over_normal,
        "over_absolute_limit": over_absolute,
        "allowed_over_soft_limit": allowed_over_normal,
        "over_soft_limit_ratio": round(over_normal / total, 4) if total else 0.0,
        "within_exception_budget": over_normal <= allowed_over_normal,
    }
