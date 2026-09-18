from __future__ import annotations

import re

SQUARE_ANNOTATION_RE = re.compile(r"\s*[\[［【][^\]］】]{1,80}[\]］】]\s*")
KNOWN_PARENTHETICAL_RE = re.compile(
    r"\s*[（(]\s*(?:laughs?|laughing|chuckles?|snorts?|sighs?|gasps?|music|"
    r"applause|cheering|inaudible|crosstalk|laughter|笑|笑声|轻笑|哼笑|音乐|"
    r"掌声|欢呼|叹气|吸气|听不清|杂音)\s*[）)]\s*",
    re.IGNORECASE,
)
SPEAKER_MARK_RE = re.compile(r"(?:^|\s)>>\s*")
WHITESPACE_RE = re.compile(r"\s+")
TRAILING_PERIOD_RE = re.compile(r"[，。,.]+(?=\s*[\"'”’》」』】）)]*\s*$)")
LATIN_WORD_SPACE_RE = re.compile(r"(?<=[A-Za-z0-9])\s+(?=[A-Za-z0-9])")
LATIN_SPACE_SENTINEL = "\ue000"


def remove_stage_directions(value: str) -> str:
    value = SPEAKER_MARK_RE.sub(" ", value)
    value = SQUARE_ANNOTATION_RE.sub(" ", value)
    return KNOWN_PARENTHETICAL_RE.sub(" ", value)


def normalize_source_text(value: str) -> str:
    value = remove_stage_directions(str(value))
    return WHITESPACE_RE.sub(" ", value).strip()


def normalize_translated_text(value: str) -> str:
    value = remove_stage_directions(str(value))
    if re.search(r"[\u3400-\u9fff]", value):
        # Chinese subtitles do not keep layout whitespace, but spaces inside
        # Latin names, brands and model phrases carry meaning (Bonnie Blue,
        # New York, 45A Ferrari) and must survive normalization.
        value = LATIN_WORD_SPACE_RE.sub(LATIN_SPACE_SENTINEL, value)
        value = WHITESPACE_RE.sub("", value)
        value = value.replace(LATIN_SPACE_SENTINEL, " ")
        value = TRAILING_PERIOD_RE.sub("", value)
        return value.strip()
    return WHITESPACE_RE.sub(" ", value).strip()


def normalize_segment_texts(source_text: str, translated_text: str) -> tuple[str, str]:
    return normalize_source_text(source_text), normalize_translated_text(translated_text)
