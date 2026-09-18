from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.repository import Repository
from app.services.subtitle_planner import audit_subtitle_window, plan_subtitle_window
from app.services.subtitle_quality import summarize_length_budget


def _providers(project: dict[str, Any], override: str | None) -> tuple[str, ...]:
    if override:
        return (override,)
    configured = str(project.get("translation_provider") or "auto").lower()
    if configured in {"codex", "grok"}:
        return (configured,)
    return settings.translation_providers


def validate(
    project_id: str,
    part_number: int,
    window_number: int,
    provider: str | None = None,
    codex_model: str | None = None,
) -> Path:
    """Run Pass 2 and Pass 3 for one Window entirely in memory."""
    repository = Repository()
    project = repository.get_project(project_id)
    if project is None:
        raise KeyError(f"Unknown project: {project_id}")

    parts = repository.list_subtitle_parts(project_id)
    if not 1 <= part_number <= len(parts):
        raise ValueError(f"Part must be between 1 and {len(parts)}")
    part = parts[part_number - 1]
    batches = [
        batch
        for batch in repository.list_subtitle_batches(int(part["id"]))
        if batch.get("error") != "Superseded by smaller retry batches"
    ]
    if not 1 <= window_number <= len(batches):
        raise ValueError(f"Window must be between 1 and {len(batches)} for Part {part_number}")
    batch = batches[window_number - 1]

    global_context = project.get("global_translation_context")
    reference = str(part.get("reference_translation") or "").strip()
    if not project.get("global_context_approved") or not isinstance(global_context, dict):
        raise RuntimeError("Pass 0 context has not been approved for this project")
    if not reference:
        raise RuntimeError(f"Part {part_number} has no completed Pass 1 reference translation")

    tokens = repository.list_timed_tokens(project_id)
    start = int(batch["start_token_id"])
    end = int(batch["end_token_id"])
    existing_segments = repository.list_segments(project_id)
    previous = [
        segment
        for segment in existing_segments
        if segment.get("end_token_id") is not None and int(segment["end_token_id"]) < start
    ][-3:]
    context = {**global_context, "reference_translation": reference}
    providers = _providers(project, provider)
    model = codex_model or str(project.get("codex_model") or settings.codex_model) or None

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (
        settings.data_dir
        / "projects"
        / project_id
        / "artifacts"
        / "targeted-window-tests"
        / f"{stamp}-part-{part_number:02d}-window-{window_number:02d}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    try:
        pass2_cues, used_provider = plan_subtitle_window(
            tokens,
            str(project["target_language"]),
            str(project.get("title") or "Untitled video"),
            start,
            end,
            context,
            previous,
            providers=providers,
            codex_model=model,
            audit_dir=output_dir / "pass2",
        )
    except Exception as exc:
        (output_dir / "failure.json").write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "part_number": part_number,
                    "window_number": window_number,
                    "token_range": [start, end],
                    "failed_stage": "pass2",
                    "error": str(exc),
                    "database_mutated": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    try:
        final_cues, issues = audit_subtitle_window(
            tokens,
            pass2_cues,
            str(project["target_language"]),
            str(project.get("title") or "Untitled video"),
            start,
            end,
            context,
            previous,
            used_provider,
            codex_model=model,
            audit_dir=output_dir / "pass3",
            include_issues=True,
        )
    except Exception as exc:
        (output_dir / "failure.json").write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "part_number": part_number,
                    "window_number": window_number,
                    "token_range": [start, end],
                    "failed_stage": "pass3",
                    "error": str(exc),
                    "pass2_cues": pass2_cues,
                    "database_mutated": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise
    report = {
        "project_id": project_id,
        "part_number": part_number,
        "window_number": window_number,
        "token_range": [start, end],
        "provider": used_provider,
        "model": model,
        "pass2_cues": pass2_cues,
        "pass2_length_summary": summarize_length_budget(pass2_cues),
        "pass3_cues": final_cues,
        "pass3_length_summary": summarize_length_budget(final_cues),
        "pass3_issues": issues,
        "database_mutated": False,
    }
    output = output_dir / "report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Pass 2 then Pass 3 for one saved Window without changing SQLite"
    )
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--part", type=int, default=1)
    parser.add_argument("--window", type=int, default=1)
    parser.add_argument("--provider", choices=("codex", "grok"), default=None)
    parser.add_argument("--codex-model", default=None)
    args = parser.parse_args()
    print(validate(args.project_id, args.part, args.window, args.provider, args.codex_model))


if __name__ == "__main__":
    main()
