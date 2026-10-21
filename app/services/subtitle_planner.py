from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from app.config import command_available, settings
from app.services.card_candidates import anchor_candidates
from app.services.cues import (
    LONG_PAUSE_MS,
    classify_token_kinds,
    materialize_aligned_cues,
    materialize_subcues,
    visible_source_text,
    visible_token_ids,
)
from app.services.prompts import (
    build_cue_alignment_prompt,
    build_part_translation_prompt,
    build_punctuation_prompt,
    build_subcue_boundary_prompt,
    build_subcue_split_prompt,
    build_translation_context_consolidation_prompt,
    build_translation_context_prompt,
)
from app.services.punctuation import PROMPT_VERSION as PUNCTUATION_PROMPT_VERSION
from app.services.punctuation import validate_punctuation_annotations
from app.services.subtitle_quality import PASS3_INVALID_OUTPUT, RULES, target_units
from app.services.subtitle_text import (
    display_target_text,
    normalize_source_text,
    normalize_target_text,
)
from app.services.translation_audit import call_metadata, record_event
from app.services.translator import (
    activate_cli_call,
    deactivate_cli_call,
    run_codex_text,
    run_codex_with_schema,
    run_grok_text,
    run_grok_with_schema,
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
CUE_ALIGNMENT_SCHEMA_PATH = SCHEMA_DIR / "subtitle-cue-alignments.schema.json"
SUBCUE_SPLIT_SCHEMA_PATH = SCHEMA_DIR / "subtitle-cue-splits.schema.json"
SUBCUE_BOUNDARY_SCHEMA_PATH = SCHEMA_DIR / "subtitle-cue-boundaries.schema.json"
TRANSLATION_CONTEXT_SCHEMA_PATH = SCHEMA_DIR / "translation-context.schema.json"
PUNCTUATION_SCHEMA_PATH = SCHEMA_DIR / "subtitle-punctuation.schema.json"
QUOTA_RE = re.compile(r"quota|usage.?limit|rate.?limit|too many requests", re.IGNORECASE)
logger = logging.getLogger(__name__)
TRANSLATION_CONTEXT_DIRECT_TOKEN_LIMIT = 12_000
TRANSLATION_CONTEXT_CHUNK_TOKENS = 6_000
TRANSLATION_CONTEXT_TERM_LIMIT = 40
TRANSLATION_CONTEXT_CHUNK_TERM_LIMIT = 60
SHORT_SUBCUE_SPEECH_TOKENS = 3
SHORT_CHINESE_CUE_CHARS = 3
CHINESE_SENTENCE_END_RE = re.compile(r"[。.][\"'”’》」』】）)]*$")


class TranslationQuotaError(RuntimeError):
    """All configured local CLIs are temporarily blocked by a quota or rate limit."""


def generate_punctuation_annotations(
    tokens: list[dict[str, Any]],
    title: str,
    part_number: int,
    part_count: int,
    provider: str,
    codex_model: str | None = None,
    audit_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """One provider invocation for one Part; invalid output is never repaired or retried."""
    prompt = build_punctuation_prompt(tokens, title, part_number, part_count)
    result = _audited_call(
        provider,
        prompt,
        PUNCTUATION_SCHEMA_PATH,
        codex_model,
        audit_dir,
        stage="punctuation-restoration",
        part=[part_number, part_count],
        punctuation_prompt_version=PUNCTUATION_PROMPT_VERSION,
    )
    try:
        marks = validate_punctuation_annotations(tokens, result)
    except (TypeError, ValueError) as exc:
        record_event(
            audit_dir,
            "punctuation-validation-error",
            error=str(exc),
            part=[part_number, part_count],
            punctuation_prompt_version=PUNCTUATION_PROMPT_VERSION,
        )
        raise
    record_event(
        audit_dir,
        "punctuation-validated",
        marks=marks,
        part=[part_number, part_count],
        punctuation_prompt_version=PUNCTUATION_PROMPT_VERSION,
    )
    return marks


def translate_part(
    tokens: list[dict[str, Any]],
    target_language: str,
    title: str,
    part_number: int,
    part_count: int,
    providers: tuple[str, ...] | None = None,
    codex_model: str | None = None,
    audit_dir: Path | None = None,
    translation_context: dict[str, Any] | None = None,
) -> tuple[dict[str, str], str]:
    source_text = normalize_source_text(" ".join(str(token["text"]) for token in tokens))
    if not source_text:
        raise RuntimeError("The part has no source text to translate")
    prompt = build_part_translation_prompt(
        source_text, target_language, title, part_number, part_count, translation_context
    )
    raw, provider = _call_text(
        prompt,
        providers,
        codex_model,
        audit_dir,
        stage="part-translation",
        part=[part_number, part_count],
    )
    translation = _clean_plain_translation(raw)
    if not translation:
        raise RuntimeError("Reference translation is empty")
    record_event(
        audit_dir,
        "response",
        source_text=source_text,
        translation=translation,
        **call_metadata(provider, codex_model),
        stage="part-translation",
        part=[part_number, part_count],
    )
    return {
        "source_text": source_text,
        "target_text": translation,
    }, provider


def _clean_plain_translation(value: str) -> str:
    text = value.strip()
    fenced = re.fullmatch(r"```(?:text|plaintext|markdown)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    return text


def _call_providers(
    prompt: str,
    schema_path: Path,
    providers: tuple[str, ...] | None = None,
    codex_model: str | None = None,
    audit_dir: Path | None = None,
    **metadata: Any,
) -> tuple[dict[str, Any], str]:
    return _call_with_providers(
        providers,
        lambda provider: _audited_call(
            provider, prompt, schema_path, codex_model, audit_dir, **metadata
        ),
        failure_message="All local subtitle CLIs failed: ",
    )


def _without_provider(fields: dict[str, Any]) -> dict[str, Any]:
    """Audit records keep provider; the CLI label already receives it positionally."""
    return {key: value for key, value in fields.items() if key != "provider"}


def _call_text(
    prompt: str,
    providers: tuple[str, ...] | None,
    codex_model: str | None,
    audit_dir: Path | None,
    **metadata: Any,
) -> tuple[str, str]:
    fields_base = {"output": "plain-text", **metadata}

    def invoke(provider: str) -> str:
        fields = {**call_metadata(provider, codex_model), **fields_base}
        record_event(audit_dir, "request", prompt=prompt, **fields)
        token = activate_cli_call(provider, **_without_provider(fields))
        try:
            if provider == "codex":
                if not command_available(settings.codex_cli):
                    raise RuntimeError(f"Command not found: {settings.codex_cli}")
                raw = run_codex_text(prompt, codex_model)
            elif provider in {"grok", "grokbuild"}:
                if not command_available(settings.grok_cli):
                    raise RuntimeError(f"Command not found: {settings.grok_cli}")
                raw = run_grok_text(prompt)
            else:
                raise RuntimeError(f"Unsupported translation provider: {provider}")
        except Exception as exc:
            record_event(audit_dir, "call-error", error=str(exc), **fields)
            raise
        finally:
            deactivate_cli_call(token)
        return raw

    return _call_with_providers(
        providers, invoke, failure_message="All local part translation CLIs failed: "
    )


def _call_with_providers(
    providers: tuple[str, ...] | None,
    invoke,
    *,
    failure_message: str,
) -> tuple[Any, str]:
    errors: list[str] = []
    quota_errors = 0
    for provider in providers or settings.translation_providers:
        try:
            return invoke(provider), provider
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
        ) as exc:
            detail = f"{provider}: {exc}"
            logger.warning("Local translation provider failed: %s", detail)
            errors.append(detail)
            if QUOTA_RE.search(str(exc)):
                quota_errors += 1
    message = "; ".join(errors)
    if errors and quota_errors == len(errors):
        raise TranslationQuotaError(message)
    raise RuntimeError(failure_message + message)


def _call_provider(
    provider: str,
    prompt: str,
    schema_path: Path,
    codex_model: str | None = None,
) -> dict[str, Any]:
    if provider == "codex":
        if not command_available(settings.codex_cli):
            raise RuntimeError(f"Command not found: {settings.codex_cli}")
        return run_codex_with_schema(prompt, schema_path, codex_model)
    if provider in {"grok", "grokbuild"}:
        if not command_available(settings.grok_cli):
            raise RuntimeError(f"Command not found: {settings.grok_cli}")
        return run_grok_with_schema(prompt, schema_path)
    raise RuntimeError(f"Unsupported subtitle planning provider: {provider}")


def _audited_call(
    provider: str,
    prompt: str,
    schema_path: Path,
    codex_model: str | None,
    audit_dir: Path | None,
    **metadata: Any,
) -> dict[str, Any]:
    fields = {**call_metadata(provider, codex_model), **metadata}
    record_event(audit_dir, "request", prompt=prompt, schema=schema_path.name, **fields)
    token = activate_cli_call(provider, **_without_provider(fields))
    try:
        result = _call_provider(provider, prompt, schema_path, codex_model)
    except Exception as exc:
        record_event(audit_dir, "call-error", error=str(exc), **fields)
        raise
    finally:
        deactivate_cli_call(token)
    record_event(audit_dir, "response", result=result, **fields)
    return result


def generate_translation_context(
    tokens: list[dict[str, Any]],
    target_language: str,
    title: str,
    providers: tuple[str, ...] | None = None,
    codex_model: str | None = None,
    audit_dir: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Create one canonical terminology and naming context for the whole project."""
    if not tokens:
        raise RuntimeError("Cannot build global translation context without source tokens")

    if len(tokens) <= TRANSLATION_CONTEXT_DIRECT_TOKEN_LIMIT:
        transcript = normalize_source_text(" ".join(str(token["text"]) for token in tokens))
        prompt = build_translation_context_prompt(
            transcript,
            target_language,
            title,
            chunk_label=None,
            term_limit=TRANSLATION_CONTEXT_TERM_LIMIT,
            chunk_term_limit=TRANSLATION_CONTEXT_CHUNK_TERM_LIMIT,
        )
        result, provider = _call_providers(
            prompt,
            TRANSLATION_CONTEXT_SCHEMA_PATH,
            providers,
            codex_model,
            audit_dir,
            stage="context",
        )
        context = _normalize_translation_context(result, term_limit=TRANSLATION_CONTEXT_CHUNK_TERM_LIMIT)
        counted = _add_term_occurrence_counts(context, transcript)
        selected = _select_consistency_terms(counted)
        selected["card_candidates"] = anchor_candidates(context.get("card_candidates", []), tokens)
        return selected, provider

    candidates: list[dict[str, Any]] = []
    used_providers: list[str] = []
    chunk_count = (len(tokens) + TRANSLATION_CONTEXT_CHUNK_TOKENS - 1) // TRANSLATION_CONTEXT_CHUNK_TOKENS
    for chunk_index, start in enumerate(
        range(0, len(tokens), TRANSLATION_CONTEXT_CHUNK_TOKENS), start=1
    ):
        chunk = tokens[start : start + TRANSLATION_CONTEXT_CHUNK_TOKENS]
        transcript = normalize_source_text(" ".join(str(token["text"]) for token in chunk))
        prompt = build_translation_context_prompt(
            transcript,
            target_language,
            title,
            chunk_label=f"{chunk_index}/{chunk_count}",
            term_limit=TRANSLATION_CONTEXT_TERM_LIMIT,
            chunk_term_limit=TRANSLATION_CONTEXT_CHUNK_TERM_LIMIT,
        )
        candidate, provider = _call_providers(
            prompt,
            TRANSLATION_CONTEXT_SCHEMA_PATH,
            providers,
            codex_model,
            audit_dir / f"chunk-{chunk_index:02d}" if audit_dir else None,
            stage="context",
            chunk=f"{chunk_index}/{chunk_count}",
        )
        candidates.append(
            _normalize_translation_context(candidate, term_limit=TRANSLATION_CONTEXT_CHUNK_TERM_LIMIT)
        )
        used_providers.append(provider)

    full_transcript = normalize_source_text(" ".join(str(token["text"]) for token in tokens))
    candidate_stats = _candidate_term_stats(candidates, full_transcript)

    consolidation_prompt = build_translation_context_consolidation_prompt(
        title,
        target_language,
        candidate_stats,
        candidates,
        term_limit=TRANSLATION_CONTEXT_TERM_LIMIT,
    )
    consolidated, provider = _call_providers(
        consolidation_prompt,
        TRANSLATION_CONTEXT_SCHEMA_PATH,
        providers,
        codex_model,
        audit_dir / "consolidation" if audit_dir else None,
        stage="context",
        chunk="consolidation",
    )
    record_event(
        audit_dir,
        "translation-context-consolidated",
        chunk_count=chunk_count,
        chunk_providers=used_providers,
        provider=provider,
    )
    context = _normalize_translation_context(
        consolidated, term_limit=TRANSLATION_CONTEXT_CHUNK_TERM_LIMIT
    )
    counted = _add_term_occurrence_counts(context, full_transcript)
    selected = _select_consistency_terms(counted)
    selected["card_candidates"] = anchor_candidates(context.get("card_candidates", []), tokens)
    return selected, provider


def _normalize_translation_context(
    result: dict[str, Any], *, term_limit: int = TRANSLATION_CONTEXT_TERM_LIMIT
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise TypeError("Global translation context must be an object")
    overview = result.get("overview")
    style = result.get("style")
    terms = result.get("terms")
    if not isinstance(overview, str) or not overview.strip():
        raise RuntimeError("Global translation context has no overview")
    if not isinstance(style, str):
        raise TypeError("Global translation context has an invalid style")
    if not isinstance(terms, list):
        raise TypeError("Global translation context has an invalid terms list")
    normalized_terms: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in terms:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "other")).strip() or "other"
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        if not source or not target or source.casefold() in seen:
            continue
        normalized_terms.append(
            {
                "source": source,
                "target": target,
                "category": category,
                "must_use": bool(item.get("must_use", False)),
            }
        )
        seen.add(source.casefold())
    normalized = {
        "overview": overview.strip(),
        "style": style.strip(),
        "terms": normalized_terms[:term_limit],
    }
    if "card_candidates" in result:
        raw_candidates = result["card_candidates"]
        if not isinstance(raw_candidates, list):
            raise TypeError("Global translation context has an invalid card candidates list")
        normalized["card_candidates"] = [
            {"source": str(item.get("source", "")).strip(),
             "display": str(item.get("display", "")).strip()}
            for item in raw_candidates
            if isinstance(item, dict)
        ]
    return normalized


def _count_term_occurrences(transcript: str, source: str) -> int:
    words = re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?", source.casefold())
    if not words:
        return 0
    phrase = r"[\s-]+".join(re.escape(word) for word in words)
    return len(
        re.findall(
            rf"(?<![A-Za-z0-9]){phrase}(?![A-Za-z0-9])",
            transcript.casefold(),
        )
    )


def _add_term_occurrence_counts(context: dict[str, Any], transcript: str) -> dict[str, Any]:
    return {
        **context,
        "terms": [
            {**term, "occurrence_count": _count_term_occurrences(transcript, term["source"])}
            for term in context.get("terms", [])
        ],
    }


def _select_consistency_terms(context: dict[str, Any]) -> dict[str, Any]:
    indexed_terms = [
        (index, term)
        for index, term in enumerate(context.get("terms", []))
        if int(term.get("occurrence_count", 0)) >= 2
    ]
    ranked = sorted(
        indexed_terms,
        key=lambda pair: (
            str(pair[1].get("category", "")) == "person",
            -int(pair[1].get("occurrence_count", 0)),
            pair[0],
        ),
    )
    return {
        **context,
        "terms": [term for _, term in ranked[:TRANSLATION_CONTEXT_TERM_LIMIT]],
    }


def _candidate_term_stats(
    candidates: list[dict[str, Any]], transcript: str
) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        seen_in_chunk: set[str] = set()
        for term in candidate.get("terms", []):
            source = str(term.get("source", "")).strip()
            key = source.casefold()
            if not source or key in seen_in_chunk:
                continue
            seen_in_chunk.add(key)
            if key not in stats:
                stats[key] = {
                    "source": source,
                    "whole_video_occurrences": _count_term_occurrences(transcript, source),
                    "candidate_chunk_count": 0,
                }
            stats[key]["candidate_chunk_count"] += 1
    return sorted(
        stats.values(),
        key=lambda item: (
            -int(item["candidate_chunk_count"]),
            -int(item["whole_video_occurrences"]),
            str(item["source"]).casefold(),
        ),
    )


def align_part_cues(
    tokens: list[dict[str, Any]],
    cues: list[dict[str, Any]],
    target_language: str,
    title: str,
    part_number: int,
    part_count: int,
    translation_context: dict[str, Any],
    providers: tuple[str, ...] | None = None,
    codex_model: str | None = None,
    audit_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Pass 2: place Pass 1 Chinese onto every Cue in one Part."""
    if not cues:
        raise RuntimeError("Pass 2 has no cues")
    prompt = build_cue_alignment_prompt(
        cues,
        target_language,
        title,
        part_number,
        part_count,
        translation_context,
    )
    errors: list[str] = []
    quota_errors = 0
    token_range = [
        int(cues[0]["start_token_id"]),
        int(cues[-1]["end_token_id"]),
    ]
    for provider in providers or settings.translation_providers:
        try:
            result = _audited_call(
                provider,
                prompt,
                CUE_ALIGNMENT_SCHEMA_PATH,
                codex_model,
                audit_dir,
                stage="part-cue-alignment",
                part=[part_number, part_count],
                token_range=token_range,
            )
            aligned_cues = materialize_aligned_cues(tokens, cues, result)
            record_event(
                audit_dir,
                "part-cue-alignment-validated",
                part=[part_number, part_count],
                token_range=token_range,
                cue_count=len(cues),
                cues=aligned_cues,
            )
            return aligned_cues, provider
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
        ) as exc:
            errors.append(f"{provider}: {exc}")
            record_event(audit_dir, "part-cue-alignment-error", provider=provider, error=str(exc))
            if QUOTA_RE.search(str(exc)):
                quota_errors += 1
    message = "; ".join(errors)
    if errors and quota_errors == len(errors):
        raise TranslationQuotaError(message)
    raise RuntimeError("Pass 2 alignment failed for configured providers: " + message)


def split_long_cues(
    tokens: list[dict[str, Any]],
    parent_cues: list[dict[str, Any]],
    all_cues: list[dict[str, Any]],
    target_language: str,
    title: str,
    translation_context: dict[str, Any],
    providers: tuple[str, ...] | None = None,
    codex_model: str | None = None,
    audit_dir: Path | None = None,
    include_subcues: bool = False,
) -> tuple[list[dict[str, Any]], str] | tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """Pass 3: run focused boundary-only and free-splitting requests once each."""
    del translation_context
    if not parent_cues:
        raise RuntimeError("Pass 3 has no long Cues to split")
    errors: list[str] = []
    quota_errors = 0
    for provider in providers or settings.translation_providers:
        try:
            prepared: dict[int, list[str]] = {}
            boundary_parents: list[dict[str, Any]] = []
            free_split_parents: list[dict[str, Any]] = []
            for parent in parent_cues:
                pieces = _split_chinese_on_punctuation(str(parent["target_text"]))
                if pieces is None:
                    free_split_parents.append(parent)
                    continue
                cue_id = int(parent["cue_id"])
                prepared[cue_id] = pieces
                boundary_parents.append(parent)

            accepted: dict[int, list[dict[str, Any]]] = {}
            invalid: dict[int, str] = {}
            raw_subcues: list[dict[str, Any]] = []
            if boundary_parents:
                boundary_result = _audited_call(
                    provider,
                    build_subcue_boundary_prompt(
                        tokens,
                        boundary_parents,
                        all_cues,
                        prepared,
                        target_language,
                        title,
                    ),
                    SUBCUE_BOUNDARY_SCHEMA_PATH,
                    codex_model,
                    audit_dir,
                    stage="pass3-1-boundary-assignment",
                    token_range=_parent_token_range(boundary_parents),
                )
                (
                    boundary_accepted,
                    boundary_invalid,
                    boundary_fallback_ids,
                ) = _partition_cue_boundary_result(
                    tokens, boundary_parents, prepared, boundary_result
                )
                accepted.update(boundary_accepted)
                invalid.update(boundary_invalid)
                raw_subcues.extend(
                    {
                        **subcue,
                        "split_pass": "pass3_1",
                        "boundary_source": (
                            "proportional_fallback" if cue_id in boundary_fallback_ids else "model"
                        ),
                    }
                    for cue_id, subcues in boundary_accepted.items()
                    for subcue in subcues
                )
                record_event(
                    audit_dir,
                    "pass3-1-boundaries-validated",
                    cue_count=len(boundary_parents),
                    accepted_cue_ids=sorted(boundary_accepted),
                    proportional_fallback_cue_ids=sorted(boundary_fallback_ids),
                    invalid_cues=boundary_invalid,
                )

            if free_split_parents:
                split_result = _audited_call(
                    provider,
                    build_subcue_split_prompt(
                        tokens,
                        free_split_parents,
                        all_cues,
                        target_language,
                        title,
                    ),
                    SUBCUE_SPLIT_SCHEMA_PATH,
                    codex_model,
                    audit_dir,
                    stage="pass3-2-chinese-splitting",
                    token_range=_parent_token_range(free_split_parents),
                )
                split_accepted, split_invalid = _partition_cue_split_result(
                    tokens, free_split_parents, split_result
                )
                accepted.update(split_accepted)
                invalid.update(split_invalid)
                raw_subcues.extend(
                    {**subcue, "split_pass": "pass3_2", "boundary_source": "model"}
                    for subcues in split_accepted.values()
                    for subcue in subcues
                )
                record_event(
                    audit_dir,
                    "pass3-2-splits-validated",
                    cue_count=len(free_split_parents),
                    accepted_cue_ids=sorted(split_accepted),
                    invalid_cues=split_invalid,
                )

            segments = _assemble_cue_split_results(parent_cues, accepted, invalid)
            segments, short_token_merges = _merge_short_token_subcues(tokens, segments)
            record_event(
                audit_dir,
                "long-cue-splits-finalized",
                token_range=_parent_token_range(parent_cues),
                cue_count=len(parent_cues),
                manual_review_cue_ids=sorted(invalid),
                short_token_subcue_merges=short_token_merges,
                segments=segments,
            )
            if include_subcues:
                return segments, provider, raw_subcues
            return segments, provider
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
        ) as exc:
            errors.append(f"{provider}: {exc}")
            record_event(audit_dir, "long-cue-split-error", provider=provider, error=str(exc))
            if QUOTA_RE.search(str(exc)):
                quota_errors += 1
    message = "; ".join(errors)
    if errors and quota_errors == len(errors):
        raise TranslationQuotaError(message)
    raise RuntimeError("All local Pass 3 CLIs failed: " + message)


def _parent_token_range(parent_cues: list[dict[str, Any]]) -> list[int]:
    return [
        min(int(item["start_token_id"]) for item in parent_cues),
        max(int(item["end_token_id"]) for item in parent_cues),
    ]


CHINESE_CLOSING_MARKS = "”’》」』】）)]}"
CHINESE_SPLIT_RE = re.compile(
    r"[，。！？；]+[" + re.escape(CHINESE_CLOSING_MARKS) + r"]*"
)
CHINESE_CONTINUATION_PUNCTUATION = ("，", "；")


def _split_chinese_on_punctuation(value: str) -> list[str] | None:
    """Pre-split Chinese on internal punctuation and absorb short fragments."""
    text = str(value).strip()
    raw: list[str] = []
    start = 0
    for match in CHINESE_SPLIT_RE.finditer(text):
        piece = text[start : match.end()].strip()
        if piece:
            raw.append(piece)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        raw.append(tail)
    if len(raw) < 2:
        return None

    pieces = raw
    changed = True
    while changed and len(pieces) > 1:
        changed = False
        for index, piece in enumerate(pieces):
            cleaned = normalize_target_text(piece)
            if target_units(cleaned) >= RULES.short_target_units:
                continue
            prefer_next = piece.rstrip().rstrip(CHINESE_CLOSING_MARKS).endswith(
                CHINESE_CONTINUATION_PUNCTUATION
            )
            preferred = index + 1 if prefer_next and index + 1 < len(pieces) else index - 1
            if preferred < 0:
                preferred = index + 1
            if preferred < index:
                pieces[preferred] = pieces[preferred] + piece
            else:
                pieces[preferred] = piece + pieces[preferred]
            del pieces[index]
            changed = True
            break
    return [normalize_target_text(piece) for piece in pieces]


def _merge_short_token_subcues(
    tokens: list[dict[str, Any]], segments: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Absorb short timing spans within a parent when the Chinese still fits."""
    rows = [dict(segment) for segment in segments]
    kinds = classify_token_kinds(tokens)
    merge_counts: dict[int, int] = {}
    index = 0
    while index < len(rows):
        current = rows[index]
        if int(current.get("subcue_id") or 0) == 0:
            index += 1
            continue
        speech_count = len(visible_token_ids(
            tokens, int(current["start_token_id"]), int(current["end_token_id"]), kinds
        ))
        if speech_count > SHORT_SUBCUE_SPEECH_TOKENS:
            index += 1
            continue

        candidates: list[tuple[bool, float, bool, int, str]] = []
        for neighbor in (index - 1, index + 1):
            if not 0 <= neighbor < len(rows):
                continue
            other = rows[neighbor]
            if (
                int(other.get("subcue_id") or 0) == 0
                or other["cue_id"] != current["cue_id"]
            ):
                continue
            left_index = min(index, neighbor)
            left, right = rows[left_index : left_index + 2]
            left_text = str(left["target_text"])
            separator = "" if left_text.endswith(
                ("，", "。", "！", "？", "；", "：", ",", ".", "!", "?", ";", ":")
            ) else "，"
            chinese = normalize_target_text(
                f"{left_text}{separator}{right['target_text']}"
            )
            units = target_units(chinese)
            if units > RULES.max_target_units:
                continue
            speech_count = len(
                visible_token_ids(
                    tokens, int(left["start_token_id"]), int(right["end_token_id"]), kinds
                )
            )
            candidates.append(
                (
                    speech_count <= SHORT_SUBCUE_SPEECH_TOKENS,
                    units,
                    neighbor > index,
                    left_index,
                    chinese,
                )
            )
        if not candidates:
            index += 1
            continue

        _, _, _, left_index, chinese = min(candidates)
        left, right = rows[left_index : left_index + 2]
        start = int(left["start_token_id"])
        end = int(right["end_token_id"])
        rows[left_index : left_index + 2] = [
            {
                **left,
                "end_ms": right["end_ms"],
                "end_token_id": end,
                "source_text": visible_source_text(tokens, start, end, kinds),
                "target_text": chinese,
                "quality_flags": list(dict.fromkeys(
                    [*(left.get("quality_flags") or []), *(right.get("quality_flags") or [])]
                )),
            }
        ]
        cue_id = int(left["cue_id"])
        merge_counts[cue_id] = merge_counts.get(cue_id, 0) + 1
        index = max(0, left_index - 1)

    previous_cue_id: int | None = None
    subcue_id = 0
    for row in rows:
        cue_id = int(row["cue_id"])
        if cue_id != previous_cue_id:
            subcue_id = 0
            previous_cue_id = cue_id
        if int(row.get("subcue_id") or 0) > 0:
            subcue_id += 1
            row["subcue_id"] = subcue_id
    return rows, merge_counts


def merge_short_chinese_cues(
    tokens: list[dict[str, Any]], segments: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, int]]]:
    """Absorb a short, sentence-ending Pass 2 cue into its preceding whole cue."""
    rows = sorted(
        (dict(segment) for segment in segments),
        key=lambda segment: int(segment["start_token_id"]),
    )
    kinds = classify_token_kinds(tokens)
    merges: list[dict[str, int]] = []
    index = 1
    while index < len(rows):
        left, current = rows[index - 1 : index + 1]
        chinese = str(current["target_text"])
        body = display_target_text(chinese)
        chinese_chars = [char for char in body if "\u3400" <= char <= "\u9fff"]
        gap_ms = int(current["start_ms"]) - int(left["end_ms"])
        if (
            int(left.get("subcue_id") or 0) != 0
            or int(current.get("subcue_id") or 0) != 0
            or not 1 <= len(chinese_chars) <= SHORT_CHINESE_CUE_CHARS
            or any(char.isalnum() and not "\u3400" <= char <= "\u9fff" for char in body)
            or not CHINESE_SENTENCE_END_RE.search(chinese)
            or int(left["end_token_id"]) + 1 != int(current["start_token_id"])
            or not 0 <= gap_ms < LONG_PAUSE_MS
            or (
                left.get("part_id") is not None
                and current.get("part_id") is not None
                and left["part_id"] != current["part_id"]
            )
        ):
            index += 1
            continue
        combined = normalize_target_text(f"{left['target_text']}{chinese}")
        if target_units(combined) > RULES.max_target_units:
            index += 1
            continue

        start = int(left["start_token_id"])
        end = int(current["end_token_id"])
        rows[index - 1 : index + 1] = [{
            **left,
            "end_ms": current["end_ms"],
            "end_token_id": end,
            "source_text": visible_source_text(tokens, start, end, kinds),
            "target_text": combined,
            "quality_flags": list(dict.fromkeys(
                [*(left.get("quality_flags") or []), *(current.get("quality_flags") or [])]
            )),
        }]
        merges.append({
            "kept_cue_id": int(left["cue_id"]),
            "absorbed_cue_id": int(current["cue_id"]),
            "start_token_id": start,
            "end_token_id": end,
        })
        index = max(1, index - 1)
    return rows, merges


def _partition_cue_boundary_result(
    tokens: list[dict[str, Any]],
    parent_cues: list[dict[str, Any]],
    prepared: dict[int, list[str]],
    result: dict[str, Any],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, str], set[int]]:
    rows = result.get("cues") if isinstance(result, dict) else None
    expected = {int(parent["cue_id"]): parent for parent in parent_cues}
    if not isinstance(rows, list):
        rows = []
    rows_by_id: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cue_id = row.get("cue_id")
        if (
            isinstance(cue_id, int)
            and not isinstance(cue_id, bool)
            and cue_id in expected
        ):
            rows_by_id.setdefault(cue_id, []).append(row)

    accepted: dict[int, list[dict[str, Any]]] = {}
    invalid: dict[int, str] = {}
    fallback_ids: set[int] = set()
    for cue_id, parent in expected.items():
        matches = rows_by_id.get(cue_id, [])
        error: str | None = None
        standard_subs: list[dict[str, Any]] = []
        if len(matches) != 1:
            error = (
                "Pass 3-1 omitted this cue_id"
                if not matches
                else "Pass 3-1 duplicated this cue_id"
            )
        chinese = prepared[cue_id]
        if error is None:
            boundaries = matches[0].get("boundaries")
            if not isinstance(boundaries, list) or len(boundaries) != len(chinese):
                error = "Pass 3-1 boundary count does not match Sub-cue count"
            else:
                for expected_subcue_id, (boundary, text) in enumerate(
                    zip(boundaries, chinese, strict=True), start=1
                ):
                    if (
                        not isinstance(boundary, dict)
                        or boundary.get("subcue_id") != expected_subcue_id
                    ):
                        error = "Pass 3-1 returned invalid subcue_id order"
                        standard_subs = []
                        break
                    standard_subs.append(
                        {
                            "end_token_id": boundary.get("end_token_id"),
                            "target_text": text,
                        }
                    )
        try:
            if error is not None:
                raise ValueError(error)
            accepted[cue_id] = materialize_subcues(
                tokens,
                [parent],
                {
                    "cues": [
                        {
                            "cue_id": cue_id,
                            "subcues": standard_subs,
                        }
                    ]
                },
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            try:
                fallback_subs = _proportional_boundary_subcues(
                    tokens, parent, chinese
                )
                accepted[cue_id] = materialize_subcues(
                    tokens,
                    [parent],
                    {
                        "cues": [
                            {
                                "cue_id": cue_id,
                                "subcues": fallback_subs,
                            }
                        ]
                    },
                )
                fallback_ids.add(cue_id)
            except (TypeError, ValueError, RuntimeError) as fallback_exc:
                invalid[cue_id] = (
                    f"{str(exc).replace('Pass 3', 'Pass 3-1')}; "
                    f"proportional fallback failed: {fallback_exc}"
                )
    return accepted, invalid, fallback_ids


def _proportional_boundary_subcues(
    tokens: list[dict[str, Any]],
    parent: dict[str, Any],
    chinese: list[str],
) -> list[dict[str, Any]]:
    if not chinese:
        raise ValueError("Pass 3-1 has no prepared Chinese subcues")
    start = int(parent["start_token_id"])
    end = int(parent["end_token_id"])
    speech_ids = visible_token_ids(tokens, start, end, classify_token_kinds(tokens))
    if len(speech_ids) < len(chinese):
        raise ValueError("Pass 3-1 has fewer speech Tokens than Chinese subcues")

    weights = [max(target_units(text), 0.5) for text in chinese]
    total_weight = sum(weights)
    total_speech = len(speech_ids)
    assigned = 0
    cumulative_weight = 0.0
    result: list[dict[str, Any]] = []
    for index, (text, weight) in enumerate(zip(chinese, weights, strict=True)):
        remaining_subs = len(chinese) - index - 1
        if remaining_subs == 0:
            boundary = end
        else:
            cumulative_weight += weight
            proposed = round(total_speech * cumulative_weight / total_weight)
            assigned = max(assigned + 1, min(proposed, total_speech - remaining_subs))
            boundary = speech_ids[assigned - 1]
        result.append({"end_token_id": boundary, "target_text": text})
    return result


def _partition_cue_split_result(
    tokens: list[dict[str, Any]],
    parent_cues: list[dict[str, Any]],
    result: dict[str, Any],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, str]]:
    """Validate each parent independently so one malformed ID does not discard its peers."""
    parents = {int(parent["cue_id"]): parent for parent in parent_cues}
    expected_ids = list(parents)
    rows = result.get("cues") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return {}, {cue_id: "Pass 3 response has no cues array" for cue_id in expected_ids}

    rows_by_id: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cue_id = row.get("cue_id")
        if isinstance(cue_id, bool) or not isinstance(cue_id, int):
            continue
        if cue_id in parents:
            rows_by_id.setdefault(cue_id, []).append(row)

    accepted: dict[int, list[dict[str, Any]]] = {}
    invalid: dict[int, str] = {}
    for cue_id in expected_ids:
        matches = rows_by_id.get(cue_id, [])
        if not matches:
            invalid[cue_id] = "Pass 3 omitted this cue_id"
            continue
        if len(matches) > 1:
            invalid[cue_id] = "Pass 3 duplicated this cue_id"
            continue
        try:
            segments = materialize_subcues(
                tokens,
                [parents[cue_id]],
                {"cues": matches},
            )
            accepted[cue_id] = segments
        except (TypeError, ValueError, RuntimeError) as exc:
            invalid[cue_id] = str(exc)
    return accepted, invalid


def _assemble_cue_split_results(
    parent_cues: list[dict[str, Any]],
    accepted: dict[int, list[dict[str, Any]]],
    invalid: dict[int, str],
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for parent in parent_cues:
        cue_id = int(parent["cue_id"])
        if cue_id in accepted:
            segments.extend(accepted[cue_id])
            continue
        flags = list(parent.get("quality_flags") or [])
        if PASS3_INVALID_OUTPUT not in flags:
            flags.append(PASS3_INVALID_OUTPUT)
        segments.append(
            {
                **parent,
                "subcue_id": None,
                "quality_flags": flags,
                "status": "needs_review",
                "pass3_error": invalid.get(cue_id, "Pass 3 did not return a usable split"),
            }
        )
    return segments
