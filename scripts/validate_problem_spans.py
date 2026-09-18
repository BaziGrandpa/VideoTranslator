from __future__ import annotations

import argparse
import json
from itertools import pairwise
from pathlib import Path
from typing import Any

from app.config import settings
from app.repository import Repository
from app.services.subtitle_planner import (
    audit_subtitle_window,
    generate_global_translation_context,
    generate_reference_translation,
)

PROBLEM_GROUPS = {
    "entity_45a_ferrari": ("sick brand new 45A",),
    "orphan_question_particle": ("Where in the world will my feed",),
    "orphan_it": ("I forget all about",),
    "orphan_turn": ("wild and unexpected",),
    "orphan_best_idea": ("because I have the absolute",),
}
GLOBAL_TERM_PATTERNS = ("doom scrolling", "Katie")


def _matching_segment_indexes(
    segments: list[dict[str, Any]], patterns: tuple[str, ...]
) -> list[int]:
    lowered = tuple(pattern.casefold() for pattern in patterns)
    return [
        index
        for index, segment in enumerate(segments)
        if any(pattern in str(segment["source_text"]).casefold() for pattern in lowered)
    ]


def _expanded_group(
    segments: list[dict[str, Any]], patterns: tuple[str, ...]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    matches = _matching_segment_indexes(segments, patterns)
    if not matches:
        raise RuntimeError(f"No existing cue matches {patterns}")
    left = max(0, min(matches) - 1)
    right = min(len(segments) - 1, max(matches) + 1)
    return segments[left : right + 1], segments[max(0, left - 3) : left]


def _selected_context_tokens(
    tokens: list[dict[str, Any]], segments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    selected_ids: set[int] = set()
    patterns = (
        tuple(pattern for group_patterns in PROBLEM_GROUPS.values() for pattern in group_patterns)
        + GLOBAL_TERM_PATTERNS
    )
    for index in _matching_segment_indexes(segments, patterns):
        segment = segments[index]
        start = max(0, int(segment["start_token_id"]) - 12)
        end = min(len(tokens) - 1, int(segment["end_token_id"]) + 12)
        selected_ids.update(range(start, end + 1))
    return [tokens[index] for index in sorted(selected_ids)]


def _has_split(cues: list[dict[str, Any]], left_suffix: str, right_prefix: str) -> bool:
    for left, right in pairwise(cues):
        if str(left["source_text"]).strip().casefold().endswith(left_suffix.casefold()) and str(
            right["source_text"]
        ).strip().casefold().startswith(right_prefix.casefold()):
            return True
    return False


def _checks(
    global_context: dict[str, Any], revised: dict[str, list[dict[str, Any]]]
) -> dict[str, bool]:
    terms = {
        str(item.get("source", "")).casefold(): str(item.get("target", "")).strip()
        for item in global_context.get("terms", [])
    }
    all_cues = [cue for cues in revised.values() for cue in cues]
    particles = {"呢", "了", "的", "吗", "吧", "呢？", "吗？"}
    return {
        "katie_preserved": terms.get("katie") == "Katie",
        "doom_scrolling_is_global": "doom scrolling" in terms,
        "no_particle_only_translation": all(
            str(cue["translated_text"]).strip() not in particles for cue in all_cues
        ),
        "45a_ferrari_not_split": not _has_split(revised["entity_45a_ferrari"], "45A", "Ferrari"),
        "it_not_orphaned": not any(
            str(cue["source_text"]).strip().casefold() == "it." for cue in revised["orphan_it"]
        ),
        "turn_not_orphaned": not any(
            str(cue["source_text"]).strip().casefold() == "turn." for cue in revised["orphan_turn"]
        ),
        "best_idea_not_split": not _has_split(revised["orphan_best_idea"], "absolute", "best"),
    }


def validate(project_id: str, provider: str, codex_model: str | None) -> Path:
    repository = Repository()
    project = repository.get_project(project_id)
    if project is None:
        raise KeyError(project_id)
    tokens = repository.list_timed_tokens(project_id)
    segments = repository.list_segments(project_id)
    if not tokens or not segments:
        raise RuntimeError("The project needs timed tokens and saved cues for targeted validation")

    providers = (provider,)
    root = settings.data_dir / "projects" / project_id / "artifacts" / "targeted-regression"
    root.mkdir(parents=True, exist_ok=True)
    context_tokens = _selected_context_tokens(tokens, segments)
    global_context, context_provider = generate_global_translation_context(
        context_tokens,
        str(project["target_language"]),
        str(project.get("title") or "Untitled video"),
        providers,
        codex_model,
        root / "global-context",
    )
    reference, reference_provider = generate_reference_translation(
        context_tokens,
        str(project["target_language"]),
        str(project.get("title") or "Untitled video"),
        1,
        1,
        providers,
        codex_model,
        root / "reference",
        global_context,
    )
    context = {**global_context, **reference}

    revised: dict[str, list[dict[str, Any]]] = {}
    originals: dict[str, list[dict[str, Any]]] = {}
    for name, patterns in PROBLEM_GROUPS.items():
        current, previous = _expanded_group(segments, patterns)
        originals[name] = current
        start = int(current[0]["start_token_id"])
        end = int(current[-1]["end_token_id"])
        revised[name] = audit_subtitle_window(
            tokens,
            current,
            str(project["target_language"]),
            str(project.get("title") or "Untitled video"),
            start,
            end,
            context,
            previous,
            provider,
            codex_model,
            root / name,
        )

    here_go = next(
        segment for segment in segments if str(segment["source_text"]).strip() == "Here we go."
    )
    report = {
        "project_id": project_id,
        "scope": "selected problem spans only",
        "providers": {
            "global_context": context_provider,
            "reference": reference_provider,
            "audit": provider,
        },
        "selected_source_token_count": len(context_tokens),
        "global_context": global_context,
        "timing_check": {
            "source": here_go["source_text"],
            "duration_ms": int(here_go["end_ms"]) - int(here_go["start_ms"]),
            "quality_flags": here_go.get("quality_flags", []),
        },
        "original": originals,
        "revised": revised,
        "checks": _checks(global_context, revised),
    }
    output = root / "report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate known subtitle problem spans only")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--provider", choices=("codex", "grok"), default="codex")
    parser.add_argument("--codex-model", default=None)
    args = parser.parse_args()
    output = validate(args.project_id, args.provider, args.codex_model)
    print(output)


if __name__ == "__main__":
    main()
