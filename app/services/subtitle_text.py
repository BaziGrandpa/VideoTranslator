from __future__ import annotations

import re

KNOWN_SQUARE_ANNOTATION_RE = re.compile(
    r"\s*[\[［【]\s*(?:clears?\s+throat|throat\s+clearing|laughs?|laughing|chuckles?|snorts?|sighs?|gasps?|music|"
    r"applause|cheering|inaudible|crosstalk|laughter|笑|笑声|轻笑|哼笑|音乐|"
    r"掌声|欢呼|叹气|吸气|听不清|杂音)\s*[\]］】]\s*",
    re.IGNORECASE,
)
KNOWN_PARENTHETICAL_RE = re.compile(
    r"\s*[（(]\s*(?:clears?\s+throat|throat\s+clearing|laughs?|laughing|chuckles?|snorts?|sighs?|gasps?|music|"
    r"applause|cheering|inaudible|crosstalk|laughter|笑|笑声|轻笑|哼笑|音乐|"
    r"掌声|欢呼|叹气|吸气|听不清|杂音)\s*[）)]\s*",
    re.IGNORECASE,
)
SPEAKER_MARK_RE = re.compile(r"(?:^|\s)>>\s*")
WHITESPACE_RE = re.compile(r"\s+")
LEADING_DASH_RE = re.compile(r"^[—–―⸺⸻]+")
TRAILING_DISPLAY_MARK_RE = re.compile(r"[，。,.—–―⸺⸻]+(?=\s*[\"'”’》」』】）)]*\s*$)")
LATIN_WORD_SPACE_RE = re.compile(r"(?<=[A-Za-z0-9])\s+(?=[A-Za-z0-9])")
LATIN_SPACE_SENTINEL = "\ue000"


def remove_stage_directions(value: str) -> str:
    value = SPEAKER_MARK_RE.sub(" ", value)
    value = KNOWN_SQUARE_ANNOTATION_RE.sub(" ", value)
    return KNOWN_PARENTHETICAL_RE.sub(" ", value)


def normalize_source_text(value: str) -> str:
    value = remove_stage_directions(str(value))
    return WHITESPACE_RE.sub(" ", value).strip()


def normalize_target_text(value: str) -> str:
    value = remove_stage_directions(str(value))
    if re.search(r"[\u3400-\u9fff]", value):
        # Chinese subtitles do not keep layout whitespace, but spaces inside
        # Latin names, brands and model phrases carry meaning (Bonnie Blue,
        # New York, 45A Ferrari) and must survive normalization.
        value = LATIN_WORD_SPACE_RE.sub(LATIN_SPACE_SENTINEL, value)
        value = WHITESPACE_RE.sub("", value)
        value = value.replace(LATIN_SPACE_SENTINEL, " ")
        value = LEADING_DASH_RE.sub("", value)
        return value.strip()
    return WHITESPACE_RE.sub(" ", value).strip()


def normalize_manual_target_text(value: str) -> str:
    """Keep explicit Segment line breaks while normalizing the text on each line."""
    lines = str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(normalized for line in lines if (normalized := normalize_target_text(line)))


def display_target_text(value: str) -> str:
    """Remove trailing marks only when producing visible subtitle text."""
    return TRAILING_DISPLAY_MARK_RE.sub("", normalize_manual_target_text(value))


def normalize_segment_texts(source_text: str, target_text: str) -> tuple[str, str]:
    return normalize_source_text(source_text), normalize_target_text(target_text)
