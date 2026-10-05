from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.source_sentences import classify_token_kinds

PUNCTUATION_MARKS = frozenset({",", ".", "?", "!", ":", ";"})
SENTENCE_END_MARKS = frozenset({".", "?", "!"})
PROMPT_VERSION = "punctuation-v2"


def source_fingerprint(tokens: list[dict[str, Any]]) -> str:
    source = [
        [
            int(token["sequence"]),
            str(token.get("raw_text", token["normalized_text"])),
            str(token["normalized_text"]),
            int(token["start_ms"]),
            int(token["end_ms"]),
        ]
        for token in tokens
    ]
    payload = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_punctuation_count(tokens: list[dict[str, Any]]) -> int:
    return sum(
        char in PUNCTUATION_MARKS
        for token in tokens
        for char in str(token["normalized_text"])
    )


def validate_punctuation_annotations(
    tokens: list[dict[str, Any]], response: dict[str, Any]
) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or set(response) != {"punctuation"}:
        raise ValueError("response must contain only a punctuation array")
    rows = response["punctuation"]
    if not isinstance(rows, list):
        raise TypeError("punctuation must be an array")
    kinds = classify_token_kinds(tokens)
    speech_ids = {
        int(token["sequence"])
        for token, kind in zip(tokens, kinds, strict=True)
        if kind == "speech"
    }
    accepted: list[dict[str, Any]] = []
    previous_id = -1
    sentence_ends = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"after_token_id", "mark"}:
            raise ValueError(f"punctuation row {index} must have after_token_id and mark only")
        token_id = row["after_token_id"]
        mark = row["mark"]
        if isinstance(token_id, bool) or not isinstance(token_id, int):
            raise TypeError(f"punctuation row {index} has an invalid after_token_id")
        if token_id <= previous_id:
            raise ValueError(f"after_token_id {token_id} is duplicated or out of order")
        if token_id not in speech_ids:
            raise ValueError(f"after_token_id {token_id} is outside this Part or is not speech")
        if not isinstance(mark, str) or mark not in PUNCTUATION_MARKS:
            raise ValueError(f"after_token_id {token_id} has unsupported mark {mark!r}")
        previous_id = token_id
        sentence_ends += mark in SENTENCE_END_MARKS
        accepted.append({"after_token_id": token_id, "mark": mark})
    if sentence_ends == 0:
        raise ValueError("response contains no sentence-ending punctuation (. ? !)")
    return accepted


def apply_punctuation(
    tokens: list[dict[str, Any]], marks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {int(row["after_token_id"]): str(row["mark"]) for row in marks}
    return [
        {**token, "text": str(token["normalized_text"]) + by_id.get(int(token["sequence"]), "")}
        for token in tokens
    ]
