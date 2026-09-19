from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from app.config import command_available, settings
from app.services.prompts import (
    build_global_context_consolidation_prompt,
    build_global_context_prompt,
    build_reference_translation_prompt,
    build_sentence_alignment_prompt,
    build_sentence_boundary_prompt,
    build_sentence_split_prompt,
)
from app.services.source_sentences import (
    classify_token_kinds,
    materialize_aligned_sentences,
    materialize_sentence_splits,
    visible_token_ids,
)
from app.services.subtitle_quality import PASS3_INVALID_OUTPUT, RULES, target_units
from app.services.subtitle_text import normalize_source_text, normalize_translated_text
from app.services.translation_audit import call_metadata, record_event
from app.services.translator import (
    run_codex_text,
    run_codex_with_schema,
    run_grok_text,
    run_grok_with_schema,
)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
SENTENCE_ALIGNMENT_SCHEMA_PATH = SCHEMA_DIR / "subtitle-sentence-alignments.schema.json"
SENTENCE_SPLIT_SCHEMA_PATH = SCHEMA_DIR / "subtitle-sentence-splits.schema.json"
SENTENCE_BOUNDARY_SCHEMA_PATH = SCHEMA_DIR / "subtitle-sentence-boundaries.schema.json"
GLOBAL_CONTEXT_SCHEMA_PATH = SCHEMA_DIR / "global-translation-context.schema.json"
QUOTA_RE = re.compile(r"quota|usage.?limit|rate.?limit|too many requests", re.IGNORECASE)
logger = logging.getLogger(__name__)
GLOBAL_CONTEXT_DIRECT_TOKEN_LIMIT = 12_000
GLOBAL_CONTEXT_CHUNK_TOKENS = 6_000
GLOBAL_CONTEXT_TERM_LIMIT = 40
GLOBAL_CONTEXT_CHUNK_TERM_LIMIT = 60


class TranslationQuotaError(RuntimeError):
    """All configured local CLIs are temporarily blocked by a quota or rate limit."""


def generate_reference_translation(
    tokens: list[dict[str, Any]],
    target_language: str,
    title: str,
    part_number: int,
    part_count: int,
    providers: tuple[str, ...] | None = None,
    codex_model: str | None = None,
    audit_dir: Path | None = None,
    global_context: dict[str, Any] | None = None,
) -> tuple[dict[str, str], str]:
    source_text = normalize_source_text(" ".join(str(token["text"]) for token in tokens))
    if not source_text:
        raise RuntimeError("The part has no source text to translate")
    prompt = build_reference_translation_prompt(
        source_text, target_language, title, part_number, part_count, global_context
    )
    raw, provider = _call_text(
        prompt,
        providers,
        codex_model,
        audit_dir,
        stage="reference-translation",
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
        stage="reference-translation",
        part=[part_number, part_count],
    )
    return {
        "source_text": source_text,
        "reference_translation": translation,
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
    try:
        result = _call_provider(provider, prompt, schema_path, codex_model)
    except Exception as exc:
        record_event(audit_dir, "call-error", error=str(exc), **fields)
        raise
    record_event(audit_dir, "response", result=result, **fields)
    return result


def generate_global_translation_context(
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

    if len(tokens) <= GLOBAL_CONTEXT_DIRECT_TOKEN_LIMIT:
        transcript = normalize_source_text(" ".join(str(token["text"]) for token in tokens))
        prompt = build_global_context_prompt(
            transcript,
            target_language,
            title,
            chunk_label=None,
            term_limit=GLOBAL_CONTEXT_TERM_LIMIT,
            chunk_term_limit=GLOBAL_CONTEXT_CHUNK_TERM_LIMIT,
        )
        result, provider = _call_providers(
            prompt,
            GLOBAL_CONTEXT_SCHEMA_PATH,
            providers,
            codex_model,
            audit_dir,
            stage="context",
        )
        context = _normalize_global_context(result, term_limit=GLOBAL_CONTEXT_CHUNK_TERM_LIMIT)
        counted = _add_term_occurrence_counts(context, transcript)
        return _select_consistency_terms(counted), provider

    candidates: list[dict[str, Any]] = []
    used_providers: list[str] = []
    chunk_count = (len(tokens) + GLOBAL_CONTEXT_CHUNK_TOKENS - 1) // GLOBAL_CONTEXT_CHUNK_TOKENS
    for chunk_index, start in enumerate(
        range(0, len(tokens), GLOBAL_CONTEXT_CHUNK_TOKENS), start=1
    ):
        chunk = tokens[start : start + GLOBAL_CONTEXT_CHUNK_TOKENS]
        transcript = normalize_source_text(" ".join(str(token["text"]) for token in chunk))
        prompt = build_global_context_prompt(
            transcript,
            target_language,
            title,
            chunk_label=f"{chunk_index}/{chunk_count}",
            term_limit=GLOBAL_CONTEXT_TERM_LIMIT,
            chunk_term_limit=GLOBAL_CONTEXT_CHUNK_TERM_LIMIT,
        )
        candidate, provider = _call_providers(
            prompt,
            GLOBAL_CONTEXT_SCHEMA_PATH,
            providers,
            codex_model,
            audit_dir / f"chunk-{chunk_index:02d}" if audit_dir else None,
            stage="context",
        )
        candidates.append(
            _normalize_global_context(candidate, term_limit=GLOBAL_CONTEXT_CHUNK_TERM_LIMIT)
        )
        used_providers.append(provider)

    full_transcript = normalize_source_text(" ".join(str(token["text"]) for token in tokens))
    candidate_stats = _candidate_term_stats(candidates, full_transcript)

    consolidation_prompt = build_global_context_consolidation_prompt(
        title,
        target_language,
        candidate_stats,
        candidates,
        term_limit=GLOBAL_CONTEXT_TERM_LIMIT,
    )
    consolidated, provider = _call_providers(
        consolidation_prompt,
        GLOBAL_CONTEXT_SCHEMA_PATH,
        providers,
        codex_model,
        audit_dir / "consolidation" if audit_dir else None,
        stage="context",
    )
    record_event(
        audit_dir,
        "global-context-consolidated",
        chunk_count=chunk_count,
        chunk_providers=used_providers,
        provider=provider,
    )
    context = _normalize_global_context(
        consolidated, term_limit=GLOBAL_CONTEXT_CHUNK_TERM_LIMIT
    )
    counted = _add_term_occurrence_counts(context, full_transcript)
    return _select_consistency_terms(counted), provider


def _normalize_global_context(
    result: dict[str, Any], *, term_limit: int = GLOBAL_CONTEXT_TERM_LIMIT
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
    return {
        "overview": overview.strip(),
        "style": style.strip(),
        "terms": normalized_terms[:term_limit],
    }


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
        "terms": [term for _, term in ranked[:GLOBAL_CONTEXT_TERM_LIMIT]],
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


def align_part_sentences(
    tokens: list[dict[str, Any]],
    source_sentences: list[dict[str, Any]],
    target_language: str,
    title: str,
    part_number: int,
    part_count: int,
    translation_context: dict[str, Any],
    providers: tuple[str, ...] | None = None,
    codex_model: str | None = None,
    audit_dir: Path | None = None,
    editable_ids: set[int] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Pass 2: align one complete Part to fixed Python-owned source sentences."""
    editable = [
        sentence
        for sentence in source_sentences
        if editable_ids is None or int(sentence["source_sentence_id"]) in editable_ids
    ]
    if not editable:
        raise RuntimeError("Pass 2 has no editable source sentences")
    prompt = build_sentence_alignment_prompt(
        source_sentences,
        target_language,
        title,
        part_number,
        part_count,
        translation_context,
        {int(item["source_sentence_id"]) for item in editable},
    )
    errors: list[str] = []
    quota_errors = 0
    token_range = [
        int(source_sentences[0]["start_token_id"]),
        int(source_sentences[-1]["end_token_id"]),
    ]
    for provider in providers or settings.translation_providers:
        try:
            result = _audited_call(
                provider,
                prompt,
                SENTENCE_ALIGNMENT_SCHEMA_PATH,
                codex_model,
                audit_dir,
                stage="part-sentence-alignment",
                part=[part_number, part_count],
                token_range=token_range,
            )
            segments = materialize_aligned_sentences(tokens, editable, result)
            record_event(
                audit_dir,
                "part-sentence-alignment-validated",
                part=[part_number, part_count],
                token_range=token_range,
                source_sentence_count=len(editable),
                segments=segments,
            )
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
            record_event(audit_dir, "part-sentence-alignment-error", provider=provider, error=str(exc))
            if QUOTA_RE.search(str(exc)):
                quota_errors += 1
    message = "; ".join(errors)
    if errors and quota_errors == len(errors):
        raise TranslationQuotaError(message)
    raise RuntimeError("All local Pass 2 CLIs failed: " + message)


def split_long_sentences(
    tokens: list[dict[str, Any]],
    parent_segments: list[dict[str, Any]],
    all_source_sentences: list[dict[str, Any]],
    target_language: str,
    title: str,
    translation_context: dict[str, Any],
    providers: tuple[str, ...] | None = None,
    codex_model: str | None = None,
    audit_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Pass 3: run focused boundary-only and free-splitting requests once each."""
    del translation_context
    if not parent_segments:
        raise RuntimeError("Pass 3 has no long source sentences to split")
    errors: list[str] = []
    quota_errors = 0
    for provider in providers or settings.translation_providers:
        try:
            prepared: dict[int, list[str]] = {}
            boundary_parents: list[dict[str, Any]] = []
            free_split_parents: list[dict[str, Any]] = []
            for parent in parent_segments:
                pieces = _split_chinese_on_punctuation(str(parent["translated_text"]))
                if pieces is None:
                    free_split_parents.append(parent)
                    continue
                sentence_id = int(parent["source_sentence_id"])
                prepared[sentence_id] = pieces
                boundary_parents.append(parent)

            accepted: dict[int, list[dict[str, Any]]] = {}
            invalid: dict[int, str] = {}
            if boundary_parents:
                boundary_result = _audited_call(
                    provider,
                    build_sentence_boundary_prompt(
                        tokens,
                        boundary_parents,
                        all_source_sentences,
                        prepared,
                        target_language,
                        title,
                    ),
                    SENTENCE_BOUNDARY_SCHEMA_PATH,
                    codex_model,
                    audit_dir,
                    stage="pass3-1-boundary-assignment",
                    token_range=_parent_token_range(boundary_parents),
                )
                (
                    boundary_accepted,
                    boundary_invalid,
                    boundary_fallback_ids,
                ) = _partition_sentence_boundary_result(
                    tokens, boundary_parents, prepared, boundary_result
                )
                accepted.update(boundary_accepted)
                invalid.update(boundary_invalid)
                record_event(
                    audit_dir,
                    "pass3-1-boundaries-validated",
                    source_sentence_count=len(boundary_parents),
                    accepted_source_sentence_ids=sorted(boundary_accepted),
                    proportional_fallback_source_sentence_ids=sorted(boundary_fallback_ids),
                    invalid_source_sentences=boundary_invalid,
                )

            if free_split_parents:
                split_result = _audited_call(
                    provider,
                    build_sentence_split_prompt(
                        tokens,
                        free_split_parents,
                        all_source_sentences,
                        target_language,
                        title,
                    ),
                    SENTENCE_SPLIT_SCHEMA_PATH,
                    codex_model,
                    audit_dir,
                    stage="pass3-2-chinese-splitting",
                    token_range=_parent_token_range(free_split_parents),
                )
                split_accepted, split_invalid = _partition_sentence_split_result(
                    tokens, free_split_parents, split_result
                )
                accepted.update(split_accepted)
                invalid.update(split_invalid)
                record_event(
                    audit_dir,
                    "pass3-2-splits-validated",
                    source_sentence_count=len(free_split_parents),
                    accepted_source_sentence_ids=sorted(split_accepted),
                    invalid_source_sentences=split_invalid,
                )

            segments = _assemble_sentence_split_results(parent_segments, accepted, invalid)
            record_event(
                audit_dir,
                "long-sentence-splits-finalized",
                token_range=_parent_token_range(parent_segments),
                source_sentence_count=len(parent_segments),
                manual_review_source_sentence_ids=sorted(invalid),
                segments=segments,
            )
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
            record_event(audit_dir, "long-sentence-split-error", provider=provider, error=str(exc))
            if QUOTA_RE.search(str(exc)):
                quota_errors += 1
    message = "; ".join(errors)
    if errors and quota_errors == len(errors):
        raise TranslationQuotaError(message)
    raise RuntimeError("All local Pass 3 CLIs failed: " + message)


def _parent_token_range(parent_segments: list[dict[str, Any]]) -> list[int]:
    return [
        min(int(item["start_token_id"]) for item in parent_segments),
        max(int(item["end_token_id"]) for item in parent_segments),
    ]


CHINESE_SPLIT_RE = re.compile(r"(?<=[，。！？；])")
CHINESE_CONTINUATION_PUNCTUATION = ("，", "；")


def _split_chinese_on_punctuation(value: str) -> list[str] | None:
    """Pre-split Chinese on internal punctuation and absorb short fragments."""
    text = str(value).strip()
    raw = [piece.strip() for piece in CHINESE_SPLIT_RE.split(text) if piece.strip()]
    if len(raw) < 2:
        return None

    pieces = raw
    changed = True
    while changed and len(pieces) > 1:
        changed = False
        for index, piece in enumerate(pieces):
            cleaned = normalize_translated_text(piece)
            if target_units(cleaned) >= RULES.short_target_units:
                continue
            prefer_next = piece.rstrip().endswith(CHINESE_CONTINUATION_PUNCTUATION)
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
    return [normalize_translated_text(piece) for piece in pieces]


def _partition_sentence_boundary_result(
    tokens: list[dict[str, Any]],
    parent_segments: list[dict[str, Any]],
    prepared: dict[int, list[str]],
    result: dict[str, Any],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, str], set[int]]:
    rows = result.get("sentences") if isinstance(result, dict) else None
    expected = {int(parent["source_sentence_id"]): parent for parent in parent_segments}
    if not isinstance(rows, list):
        rows = []
    rows_by_id: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sentence_id = row.get("source_sentence_id")
        if (
            isinstance(sentence_id, int)
            and not isinstance(sentence_id, bool)
            and sentence_id in expected
        ):
            rows_by_id.setdefault(sentence_id, []).append(row)

    accepted: dict[int, list[dict[str, Any]]] = {}
    invalid: dict[int, str] = {}
    fallback_ids: set[int] = set()
    for sentence_id, parent in expected.items():
        matches = rows_by_id.get(sentence_id, [])
        error: str | None = None
        standard_subs: list[dict[str, Any]] = []
        if len(matches) != 1:
            error = (
                "Pass 3-1 omitted this source_sentence_id"
                if not matches
                else "Pass 3-1 duplicated this source_sentence_id"
            )
        chinese = prepared[sentence_id]
        if error is None:
            boundaries = matches[0].get("boundaries")
            if not isinstance(boundaries, list) or len(boundaries) != len(chinese):
                error = "Pass 3-1 boundary count does not match subsentence count"
            else:
                for expected_sub_id, (boundary, text) in enumerate(
                    zip(boundaries, chinese, strict=True), start=1
                ):
                    if (
                        not isinstance(boundary, dict)
                        or boundary.get("subsentence_id") != expected_sub_id
                    ):
                        error = "Pass 3-1 returned invalid subsentence_id sequence"
                        standard_subs = []
                        break
                    standard_subs.append(
                        {
                            "end_token_id": boundary.get("end_token_id"),
                            "chinese_sentence": text,
                        }
                    )
        try:
            if error is not None:
                raise ValueError(error)
            accepted[sentence_id] = materialize_sentence_splits(
                tokens,
                [parent],
                {
                    "sentences": [
                        {
                            "source_sentence_id": sentence_id,
                            "subsentences": standard_subs,
                        }
                    ]
                },
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            try:
                fallback_subs = _proportional_boundary_subsentences(
                    tokens, parent, chinese
                )
                accepted[sentence_id] = materialize_sentence_splits(
                    tokens,
                    [parent],
                    {
                        "sentences": [
                            {
                                "source_sentence_id": sentence_id,
                                "subsentences": fallback_subs,
                            }
                        ]
                    },
                )
                fallback_ids.add(sentence_id)
            except (TypeError, ValueError, RuntimeError) as fallback_exc:
                invalid[sentence_id] = (
                    f"{str(exc).replace('Pass 3', 'Pass 3-1')}; "
                    f"proportional fallback failed: {fallback_exc}"
                )
    return accepted, invalid, fallback_ids


def _proportional_boundary_subsentences(
    tokens: list[dict[str, Any]],
    parent: dict[str, Any],
    chinese: list[str],
) -> list[dict[str, Any]]:
    if not chinese:
        raise ValueError("Pass 3-1 has no prepared Chinese subsentences")
    start = int(parent["start_token_id"])
    end = int(parent["end_token_id"])
    speech_ids = visible_token_ids(tokens, start, end, classify_token_kinds(tokens))
    if len(speech_ids) < len(chinese):
        raise ValueError("Pass 3-1 has fewer speech Tokens than Chinese subsentences")

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
        result.append({"end_token_id": boundary, "chinese_sentence": text})
    return result


def _partition_sentence_split_result(
    tokens: list[dict[str, Any]],
    parent_segments: list[dict[str, Any]],
    result: dict[str, Any],
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, str]]:
    """Validate each parent independently so one malformed ID does not discard its peers."""
    parents = {int(parent["source_sentence_id"]): parent for parent in parent_segments}
    expected_ids = list(parents)
    rows = result.get("sentences") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return {}, {sentence_id: "Pass 3 response has no sentences array" for sentence_id in expected_ids}

    rows_by_id: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sentence_id = row.get("source_sentence_id")
        if isinstance(sentence_id, bool) or not isinstance(sentence_id, int):
            continue
        if sentence_id in parents:
            rows_by_id.setdefault(sentence_id, []).append(row)

    accepted: dict[int, list[dict[str, Any]]] = {}
    invalid: dict[int, str] = {}
    for sentence_id in expected_ids:
        matches = rows_by_id.get(sentence_id, [])
        if not matches:
            invalid[sentence_id] = "Pass 3 omitted this source_sentence_id"
            continue
        if len(matches) > 1:
            invalid[sentence_id] = "Pass 3 duplicated this source_sentence_id"
            continue
        try:
            segments = materialize_sentence_splits(
                tokens,
                [parents[sentence_id]],
                {"sentences": matches},
            )
            accepted[sentence_id] = segments
        except (TypeError, ValueError, RuntimeError) as exc:
            invalid[sentence_id] = str(exc)
    return accepted, invalid


def _assemble_sentence_split_results(
    parent_segments: list[dict[str, Any]],
    accepted: dict[int, list[dict[str, Any]]],
    invalid: dict[int, str],
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for parent in parent_segments:
        sentence_id = int(parent["source_sentence_id"])
        if sentence_id in accepted:
            segments.extend(accepted[sentence_id])
            continue
        flags = list(parent.get("quality_flags") or [])
        if PASS3_INVALID_OUTPUT not in flags:
            flags.append(PASS3_INVALID_OUTPUT)
        segments.append(
            {
                **parent,
                "sub_id": 0,
                "quality_flags": flags,
                "status": "needs_review",
                "pass3_error": invalid.get(sentence_id, "Pass 3 did not return a usable split"),
            }
        )
    return segments


def _planning_ranges(
    tokens: list[dict[str, Any]],
    target_size: int,
    start_token_id: int = 0,
    end_token_id: int | None = None,
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = start_token_id
    final = len(tokens) - 1 if end_token_id is None else end_token_id
    while start <= final:
        target_end = min(final, start + target_size - 1)
        if target_end < final:
            search_start = max(start + target_size // 2, target_end - 60)
            search_end = min(final, target_end + 60)
            candidates = [
                index
                for index in range(search_start, search_end + 1)
                if str(tokens[index]["text"]).endswith((".", "?", "!"))
            ]
            if candidates:
                target_end = min(candidates, key=lambda value: abs(value - target_end))
        ranges.append((start, target_end))
        start = target_end + 1
    return ranges


def find_natural_split(tokens: list[dict[str, Any]], start: int, end: int) -> int:
    midpoint = (start + end) // 2
    radius = max(1, min(80, (end - start) // 3))
    candidates = [
        index
        for index in range(max(start, midpoint - radius), min(end - 1, midpoint + radius) + 1)
        if str(tokens[index]["text"]).endswith((".", "?", "!"))
    ]
    return min(candidates, key=lambda value: abs(value - midpoint)) if candidates else midpoint
