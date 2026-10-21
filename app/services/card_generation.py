from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.cards.model import CardPayload, CardStyle
from app.cards.repository import CardRepository
from app.cards.visual import render_card_png
from app.services.prompts import SECURITY_BLOCK
from app.services.renderer import probe_video
from app.services.subtitle_planner import _call_providers

logger = logging.getLogger(__name__)
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "commentary-card-copy.schema.json"
CARD_DURATION_MS = 5000
MIN_CARD_DURATION_MS = 2000


def build_card_copy_prompt(
    part_text: str, candidates: list[dict[str, Any]], part_number: int, part_count: int
) -> str:
    names = [{"source": item["source"], "display": item["display"]} for item in candidates]
    return (
        "You are writing short explanatory commentary cards for a Chinese audience. "
        "Read the complete English Part to understand each candidate in context. For every "
        "candidate, return its source value unchanged, a natural Chinese name for its corrected "
        "display term, and a Chinese explanation aiming for about 40 Chinese characters. Use enough "
        "detail to make the concept useful to viewers; a longer explanation is fine when "
        "needed, and the editor will review the wording. The card's purpose is to add "
        "useful background knowledge the speaker did not have time to explain. "
        "You may use reliable general knowledge beyond the transcript, but avoid speculative "
        "details, unsupported numbers, and claims of scientific certainty. Explain the sense "
        "relevant here, not every meaning of the word. Do not choose times, positions, styles, "
        "or occurrence numbers. Return JSON matching the schema, with exactly one card for "
        "each supplied candidate.\n\n"
        f"{SECURITY_BLOCK}\n\n"
        f"PART: {part_number}/{part_count}\n"
        f"CANDIDATES: {json.dumps(names, ensure_ascii=False)}\n"
        f"BEGIN COMPLETE PART TRANSCRIPT\n{part_text}\nEND COMPLETE PART TRANSCRIPT"
    )


def _card_copy(
    part_text: str,
    candidates: list[dict[str, Any]],
    part_number: int,
    part_count: int,
    providers: tuple[str, ...],
    codex_model: str,
    audit_dir: Path,
) -> dict[str, dict[str, str]]:
    prompt = build_card_copy_prompt(part_text, candidates, part_number, part_count)
    result, _provider = _call_providers(
        prompt, SCHEMA_PATH, providers, codex_model, audit_dir,
        stage="card-copy", part=[part_number, part_count],
    )
    cards = result.get("cards")
    if not isinstance(cards, list):
        raise TypeError("Card Pass returned no cards list")
    expected = {str(item["source"]).casefold() for item in candidates}
    found: dict[str, dict[str, str]] = {}
    for item in cards:
        if not isinstance(item, dict):
            continue
        key = str(item.get("source", "")).casefold()
        chinese = str(item.get("chinese_term", "")).strip()
        explanation = str(item.get("explanation", "")).strip()
        if key not in expected or not chinese or not explanation:
            logger.warning("Card Pass dropped invalid card copy for %s", key)
            continue
        found[key] = {"chinese_term": chinese, "explanation": explanation}
    return found


def _first_available_slot(
    desired_ms: int, duration_ms: int, occupied: list[tuple[int, int]],
    fade_ms: int,
) -> tuple[int, int] | None:
    start = max(0, desired_ms)
    for busy_start, busy_end in sorted(occupied):
        if busy_end <= start:
            continue
        if start + CARD_DURATION_MS <= busy_start:
            break
        start = busy_end
    end = min(duration_ms, start + CARD_DURATION_MS)
    if end - start < max(MIN_CARD_DURATION_MS, fade_ms):
        return None
    return start, end


def generate_cards_for_project(
    project: dict[str, Any], parts: list[dict[str, Any]],
    cues_by_part: dict[int, list[dict[str, Any]]],
    repository: CardRepository, providers: tuple[str, ...], codex_model: str,
    audit_dir: Path,
) -> dict[str, Any]:
    """Create cards from Pass 0 candidates, preserving existing manual and generated cards."""
    candidates = project.get("translation_context", {}).get("card_candidates", [])
    if not candidates:
        return {"created": 0, "skipped": []}
    existing = repository.list(str(project["id"]))
    generated_keys = {str(card.get("generated_from") or "") for card in existing}
    pending = [item for item in candidates if str(item["source"]).casefold() not in generated_keys]
    if not pending:
        return {"created": 0, "skipped": []}
    video_path = Path(str(project.get("video_path") or ""))
    width, height = probe_video(video_path)
    duration = int(project.get("duration_ms") or 0)
    style = CardStyle.model_validate(repository.default_style())
    occupied = [(int(card["start_ms"]), int(card["end_ms"])) for card in existing]
    created = 0
    skipped: list[str] = []
    part_count = len(parts)
    for part in parts:
        part_candidates = [
            item for item in pending
            if int(part["start_token_id"]) <= int(item["start_token_id"])
            <= int(part["end_token_id"])
        ]
        if not part_candidates:
            continue
        part_number = int(part["part_number"])
        copy = _card_copy(
            str(part["source_text"]), part_candidates, part_number, part_count,
            providers, codex_model, audit_dir / f"part-{part_number:02d}",
        )
        cues = cues_by_part.get(int(part["id"]), [])
        for item in part_candidates:
            source = str(item["source"])
            content = copy.get(source.casefold())
            if content is None:
                skipped.append(source)
                continue
            anchor = int(item["start_token_id"])
            cue = next(
                (cue for cue in cues if int(cue["start_token_id"]) <= anchor
                 <= int(cue["end_token_id"])),
                None,
            )
            if cue is None:
                skipped.append(source)
                continue
            slot = _first_available_slot(
                int(cue["start_ms"]), duration, occupied,
                style.fade_in_ms + style.fade_out_ms,
            )
            if slot is None:
                skipped.append(source)
                continue
            payload = CardPayload(
                start_ms=slot[0], end_ms=slot[1],
                chinese_term=content["chinese_term"],
                english_term=str(item["display"]),
                explanation=content["explanation"], style=style,
            )
            try:
                render_card_png(payload, width, height)
                saved = repository.create_generated(str(project["id"]), source, payload)
            except ValueError as exc:
                logger.warning("Generated card skipped for %s: %s", source, exc)
                skipped.append(source)
                continue
            if saved is not None:
                created += 1
                occupied.append(slot)
    return {"created": created, "skipped": skipped}
