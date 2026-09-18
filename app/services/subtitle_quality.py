from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SubtitleQualityRules:
    preferred_min_ms: int = 1200
    preferred_max_ms: int = 3500
    max_duration_ms: int = 5000
    short_duration_ms: int = 700
    short_source_words: int = 2
    short_target_units: int = 4
    preferred_target_units: int = 14
    normal_max_target_units: int = 18
    max_target_units: int = 24
    allowed_over_normal_ratio: float = 0.02
    max_target_units_per_second: float = 9.0

    def payload(self) -> dict:
        return asdict(self)


RULES = SubtitleQualityRules()
PROMPT_VERSION = "part-review-v6.3"


def target_units(text: str) -> float:
    """Approximate display load: wide characters count as 1, others as 0.5."""
    return sum(
        1.0 if unicodedata.east_asian_width(char) in {"W", "F"} else 0.5
        for char in text
        if not char.isspace()
    )


def quality_issues(start_ms: int, end_ms: int, source: str, target: str) -> list[dict]:
    duration = max(1, end_ms - start_ms)
    units = target_units(target)
    issues = []

    def add(kind: str, actual: float, limit: float) -> None:
        issues.append({"type": kind, "actual": round(actual, 3), "limit": limit})

    if duration < RULES.short_duration_ms and (
        len(source.split()) > RULES.short_source_words or units > RULES.short_target_units
    ):
        add("very_short_duration", duration, RULES.short_duration_ms)
    if duration > RULES.max_duration_ms:
        add("very_long_duration", duration, RULES.max_duration_ms)
    if units and units * 1000 / duration > RULES.max_target_units_per_second:
        add("high_target_reading_speed", units * 1000 / duration, RULES.max_target_units_per_second)
    if units > RULES.max_target_units:
        add("target_text_too_long", units, RULES.max_target_units)
    elif units > RULES.normal_max_target_units:
        add("target_text_above_normal_limit", units, RULES.normal_max_target_units)
    return issues


def analyze_quality(start_ms: int, end_ms: int, source_text: str, translation: str) -> list[str]:
    return [item["type"] for item in quality_issues(start_ms, end_ms, source_text, translation)]


def summarize_length_budget(segments: list[dict]) -> dict:
    total = len(segments)
    over_normal = sum(
        target_units(str(segment.get("translated_text", ""))) > RULES.normal_max_target_units
        for segment in segments
    )
    over_absolute = sum(
        target_units(str(segment.get("translated_text", ""))) > RULES.max_target_units
        for segment in segments
    )
    allowed_over_normal = max(1, int(total * RULES.allowed_over_normal_ratio)) if total else 0
    return {
        "total_cues": total,
        "soft_limit_units": RULES.normal_max_target_units,
        "absolute_limit_units": RULES.max_target_units,
        "over_soft_limit": over_normal,
        "over_absolute_limit": over_absolute,
        "allowed_over_soft_limit": allowed_over_normal,
        "over_soft_limit_ratio": round(over_normal / total, 4) if total else 0.0,
        "within_exception_budget": over_normal <= allowed_over_normal,
    }


def editorial_instructions() -> str:
    return (
        "YOUR HIGHEST PRIORITY IS LOSSLESS MEANING PRESERVATION!\n"
        "YOUR PRIMARY TASK IS CHINESE-FIRST CUE SEGMENTATION AND LENGTH CONTROL!\n"
        "KEEP CHINESE CUES AT OR BELOW 18 DISPLAY UNITS!\n"
        "SHORT, NATURAL CHINESE CUES ARE YOUR MAIN DELIVERABLE!\n\n"
        "Treat the corresponding passage in the complete Pass 1 translation as an authoritative "
        "semantic inventory. Every statement, example, comparison, cause, contrast, list item, "
        "name, number, qualification and punchline in the Window source and its Pass 1 passage must "
        "appear exactly once across the output cues. Never shorten, summarize, generalize or drop "
        "content to satisfy a display-unit limit. Create more cues instead. A short cue that omits "
        "meaning is invalid even when its Token coverage and length are perfect.\n\n"
        "Use Chinese punctuation in the Pass 1 passage as the first boundary proposal. Normally "
        "start with sentence boundaries at 。！？, then consider ；：，、 when a sentence needs more "
        "cues. Do not split mechanically when that would create an orphaned or unnatural fragment, "
        "and split further at a natural grammatical boundary when one punctuation-delimited unit is "
        "still too long. Short adjacent units may be combined only when all meaning remains explicit "
        "and the resulting cue stays within the length rules.\n\n"
        "Choose boundaries for the Chinese viewing experience. Divide the Chinese into short, "
        "natural information units, then align each unit with its contiguous English token range. "
        "One English sentence may require several cues; English sentence completeness is never a "
        "reason to keep a long cue. Split explanations, causal clauses, contrasts and enumerations "
        "where the Chinese remains naturally readable. Shorter cues are welcome; never pad them to "
        "reach a limit.\n\n"
        "Keep every cue at or below 18 display units by default. Above 18 and at or below 24 is a "
        "VERY RARE exception allowed only when a shorter split would damage a genuinely indivisible "
        "name, product model or fixed expression. Aim for no more than 2% of cues above 18. This is "
        "not a quota to fill. NEVER OUTPUT A CUE ABOVE 24 DISPLAY UNITS. Wide characters, including "
        "Chinese and full-width punctuation, count as 1; other non-space characters count as 0.5; "
        "spaces do not count.\n\n"
        "Use the complete Part translation as the basis. Local rephrasing is allowed only for a "
        "natural boundary adjustment; it must not remove, compress or generalize any semantic item. "
        "Preserve all meaning, facts, names, numbers, negation, tone and logical connections. Do not "
        "summarize, omit, duplicate or invent information. Each cue's Chinese must correspond to its "
        "own source range in context; neighboring cues may express one meaning group without matching "
        "word for word.\n\n"
        "Protect actual indivisible names, product models and fixed expressions. Do not treat every "
        "noun phrase or verb phrase as indivisible. Avoid isolated particles such as 呢/了/的/吗/吧 "
        "and orphaned words that clearly belong with a neighboring phrase. Preserve real person names "
        "in their original Latin spelling and follow approved global terminology.\n\n"
        "CHINESE-FIRST EXAMPLE:\n"
        "GOOD: All to answer the question, / does more dopamine make life more exciting?\n"
        "ZH: 这一切都是为了回答： / 更多多巴胺，会让生活更刺激吗？\n"
        "BAD: The other ones I wasn't so lucky, but I still went in, got my money, and felt like "
        "I accomplished something pretty epic. -> 后面几张我没那么幸运，但我还是拿回了钱，"
        "感觉完成了件了不起的事\n"
        "GOOD: The other ones I wasn't so lucky, / but I still went in, got my money, / and felt "
        "like I accomplished something pretty epic.\n"
        "ZH: 后面几张我没那么幸运 / 但我还是拿回了钱 / 感觉完成了件挺了不起的事\n"
        "Assign each Chinese unit its corresponding source Token range. The example demonstrates "
        "granularity, not a fixed number of cues.\n\n"
    )
