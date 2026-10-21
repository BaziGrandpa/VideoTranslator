from __future__ import annotations

import re
from typing import Any

WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
MAX_CARD_CANDIDATES = 40


def words(value: str) -> list[str]:
    return [word.casefold() for word in WORD_RE.findall(value)]


def first_token_span(tokens: list[dict[str, Any]], source: str) -> tuple[int, int] | None:
    """Locate a copied source phrase in immutable source words, ignoring punctuation."""
    sought = words(source)
    if not sought:
        return None
    stream: list[tuple[str, int]] = []
    for token in tokens:
        token_id = int(token["token_id"])
        for word in words(str(token.get("normalized_text", token.get("text", "")))):
            stream.append((word, token_id))
    for index in range(len(stream) - len(sought) + 1):
        if [word for word, _ in stream[index:index + len(sought)]] == sought:
            return stream[index][1], stream[index + len(sought) - 1][1]
    return None


def anchor_candidates(
    candidates: list[dict[str, Any]], tokens: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop individual unmatched model suggestions without invalidating Pass 0."""
    anchored: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        source = str(item.get("source", "")).strip()
        display = str(item.get("display", "")).strip() or source
        key = source.casefold()
        if not source or not display or key in seen:
            continue
        span = first_token_span(tokens, source)
        if span is None:
            continue
        anchored.append({
            "source": source,
            "display": display,
            "start_token_id": span[0],
            "end_token_id": span[1],
        })
        seen.add(key)
        if len(anchored) >= MAX_CARD_CANDIDATES:
            break
    return sorted(anchored, key=lambda item: item["start_token_id"])
