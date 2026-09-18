from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import settings
from app.repository import Repository
from app.services.caption_tokens import reconstruct_vtt_tokens
from app.services.downloader import download_youtube
from app.services.renderer import render_project
from app.services.subtitle_parts import build_subtitle_parts
from app.services.subtitle_planner import (
    TranslationQuotaError,
    audit_subtitle_window,
    find_natural_split,
    generate_global_translation_context,
    generate_reference_translation,
    plan_subtitle_window,
)
from app.services.subtitle_quality import summarize_length_budget
from app.services.transcriber import transcribe_tokens


class Pipeline:
    def __init__(self, repository: Repository | None = None):
        self.repository = repository or Repository()

    def project_dir(self, project_id: str) -> Path:
        path = settings.data_dir / "projects" / project_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def prepare(self, project_id: str) -> None:
        project = self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        project_dir = self.project_dir(project_id)
        self.repository.update_project(
            project_id, status="running", stage="downloading", progress=3, error=None
        )
        downloaded = download_youtube(
            project["source_url"],
            project_dir,
            int(project["max_height"]),
            progress_callback=lambda value: self.repository.update_project(
                project_id, progress=value
            ),
        )
        self.repository.update_project(
            project_id,
            youtube_id=downloaded["youtube_id"],
            title=downloaded["title"],
            duration_ms=downloaded["duration_ms"],
            video_path=str(downloaded["video_path"]),
            subtitle_source=downloaded["subtitle_source"],
            stage="reconstructing_timeline",
            progress=55,
        )
        tokens = self._build_timed_tokens(
            project_id,
            Path(downloaded["video_path"]),
            Path(downloaded["subtitle_path"]) if downloaded["subtitle_path"] else None,
            str(downloaded["subtitle_source"] or "youtube_caption"),
        )
        self._save_tokens_and_translate(project_id, tokens)

    def rebuild_subtitles(self, project_id: str) -> None:
        project = self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if not project.get("video_path") or not Path(project["video_path"]).exists():
            raise RuntimeError("The project has no downloaded video to rebuild")
        source_dir = self.project_dir(project_id) / "source"
        captions = sorted(source_dir.glob("video*.vtt"))
        self.repository.clear_subtitle_data(project_id)
        self.repository.update_project(
            project_id,
            status="running",
            stage="reconstructing_timeline",
            progress=55,
        )
        tokens = self._build_timed_tokens(
            project_id,
            Path(project["video_path"]),
            captions[0] if captions else None,
            str(project.get("subtitle_source") or "youtube_caption"),
        )
        self._save_tokens_and_translate(project_id, tokens)

    def partial_rebuild(self, project_id: str) -> None:
        """Rebuild selected immutable Window envelopes and publish only after full success."""
        project = self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        scope = project.get("pending_rebuild_scope")
        if not isinstance(scope, dict) or not isinstance(scope.get("windows"), list):
            raise RuntimeError("The project has no pending selected-Window rebuild")
        tokens = self.repository.list_timed_tokens(project_id)
        if not tokens:
            raise RuntimeError("Selected-Window rebuild requires the existing Timed Tokens")
        providers = _provider_sequence(str(project.get("translation_provider") or "auto"))
        codex_model = str(project.get("codex_model") or settings.codex_model)
        run_dir = (
            self.project_dir(project_id)
            / "artifacts"
            / "partial-rebuilds"
            / str(scope.get("run_id") or "unknown-run")
        )

        global_context = project.get("global_translation_context")
        if not isinstance(global_context, dict):
            self.repository.update_project(
                project_id,
                status="running",
                stage="building_global_context",
                progress=66,
                pipeline_detail={"pass": 0, "mode": "selected"},
                error=None,
            )
            global_context, global_provider = generate_global_translation_context(
                tokens,
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                providers,
                codex_model,
                audit_dir=run_dir / "pass0",
            )
            self.repository.update_project(
                project_id,
                global_translation_context=global_context,
                global_context_provider=global_provider,
                global_context_approved=0,
                status="waiting_context_review",
                stage="waiting_context_review",
                progress=68,
                pipeline_detail={"pass": 0, "mode": "selected", "waiting_review": True},
                error=None,
            )
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "global-translation-context.json").write_text(
                json.dumps(global_context, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return
        if not project.get("global_context_approved"):
            self.repository.update_project(
                project_id,
                status="waiting_context_review",
                stage="waiting_context_review",
                progress=68,
                pipeline_detail={"pass": 0, "mode": "selected", "waiting_review": True},
                error=None,
            )
            return

        current_revision = int(project["subtitle_revision"])
        expected_revision = int(scope.get("base_subtitle_revision", -1))
        if current_revision != expected_revision:
            raise RuntimeError(
                "Subtitles changed after the selected Windows were queued; start the selection again"
            )
        parts = self.repository.list_subtitle_parts(project_id)
        parts_by_id = {int(part["id"]): part for part in parts}
        existing_segments = self.repository.list_segments(project_id)
        selected_windows = [dict(item) for item in scope["windows"]]
        affected_part_ids = sorted(
            {int(item["part_id"]) for item in selected_windows},
            key=lambda part_id: int(parts_by_id[part_id]["sequence"]),
        )
        results: list[dict[str, Any]] = []
        for result_index, part_id in enumerate(affected_part_ids):
            part = parts_by_id.get(part_id)
            if part is None:
                raise RuntimeError(f"Selected Part {part_id} no longer exists")
            windows = sorted(
                [item for item in selected_windows if int(item["part_id"]) == part_id],
                key=lambda item: int(item["start_token_id"]),
            )
            active_batches = {
                int(batch["id"]): batch
                for batch in self.repository.list_subtitle_batches(part_id)
                if batch.get("error") != "Superseded by smaller retry batches"
            }
            for window in windows:
                batch = active_batches.get(int(window["batch_id"]))
                if batch is None or (
                    int(batch["start_token_id"]) != int(window["start_token_id"])
                    or int(batch["end_token_id"]) != int(window["end_token_id"])
                ):
                    raise RuntimeError("A selected Window changed after it was queued")

            part_start = int(part["start_token_id"])
            part_end = int(part["end_token_id"])
            part_segments = [
                dict(segment)
                for segment in existing_segments
                if int(segment.get("part_id") or -1) == part_id
            ]
            _validate_range_coverage(part_segments, part_start, part_end)
            self.repository.update_project(
                project_id,
                status="running",
                stage="translating_reference",
                progress=70,
                pipeline_detail={
                    "pass": 1,
                    "mode": "selected",
                    "part": int(part["sequence"]) + 1,
                    "part_count": len(parts),
                },
                error=None,
            )
            reference, reference_provider = generate_reference_translation(
                tokens[part_start : part_end + 1],
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                int(part["sequence"]) + 1,
                len(parts),
                providers,
                codex_model,
                audit_dir=run_dir / f"part-{int(part['sequence']) + 1:02d}" / "pass1",
                global_context=global_context,
            )
            context = {
                **global_context,
                "reference_translation": reference["reference_translation"],
            }
            hybrid = part_segments
            batch_providers: dict[int, str] = {}
            self.repository.update_project(
                project_id,
                stage="planning_subtitles",
                progress=72 + round(14 * result_index / max(1, len(affected_part_ids))),
            )
            for selected_index, window in enumerate(windows, start=1):
                start = int(window["start_token_id"])
                end = int(window["end_token_id"])
                self.repository.update_project(
                    project_id,
                    status="running",
                    stage="planning_subtitles",
                    progress=72 + round(14 * result_index / max(1, len(affected_part_ids))),
                    pipeline_detail={
                        "pass": 2,
                        "mode": "selected",
                        "part": int(part["sequence"]) + 1,
                        "part_count": len(parts),
                        "window": int(window["window_number"]),
                        "window_count": len(active_batches),
                        "selected_window": selected_index,
                        "selected_window_count": len(windows),
                        "start_token_id": start,
                        "end_token_id": end,
                    },
                    error=None,
                )
                context_segments = [
                    *[
                        segment
                        for segment in existing_segments
                        if int(segment.get("end_token_id") or -1) < part_start
                    ],
                    *hybrid,
                ]
                previous = [
                    segment
                    for segment in context_segments
                    if segment.get("end_token_id") is not None
                    and int(segment["end_token_id"]) < start
                ][-3:]
                planned, used_provider = plan_subtitle_window(
                    tokens,
                    str(project["target_language"]),
                    str(project.get("title") or "Untitled video"),
                    start,
                    end,
                    context,
                    previous,
                    providers=providers,
                    codex_model=codex_model,
                    audit_dir=(
                        run_dir
                        / f"part-{int(part['sequence']) + 1:02d}"
                        / f"window-{int(window['window_number']):02d}-pass2"
                    ),
                )
                retained = [
                    segment
                    for segment in hybrid
                    if int(segment["end_token_id"]) < start
                    or int(segment["start_token_id"]) > end
                ]
                hybrid = sorted(
                    [*retained, *planned], key=lambda item: int(item["start_token_id"])
                )
                _validate_range_coverage(hybrid, part_start, part_end)
                batch_providers[int(window["batch_id"])] = used_provider

            editable_ranges = [
                (int(window["start_token_id"]), int(window["end_token_id"]))
                for window in windows
            ]
            review_ranges = _merge_token_ranges(editable_ranges)
            untouched_metadata = {
                (int(segment["start_token_id"]), int(segment["end_token_id"])): (
                    segment.get("status", "machine_translated"),
                    list(segment.get("quality_flags", [])),
                )
                for segment in part_segments
                if not any(
                    left <= int(segment["start_token_id"])
                    and int(segment["end_token_id"]) <= right
                    for left, right in editable_ranges
                )
            }
            audit_provider = (
                list(batch_providers.values())[-1] if batch_providers else providers[0]
            )
            staged_part_ids = {int(item["part_id"]) for item in results}
            audit_context_segments = [
                segment
                for segment in existing_segments
                if int(segment.get("part_id") or -1) not in staged_part_ids
            ]
            for staged in results:
                audit_context_segments.extend(staged["segments"])
            previous_part_cues = [
                segment
                for segment in audit_context_segments
                if segment.get("end_token_id") is not None
                and int(segment["end_token_id"]) < part_start
            ][-3:]
            self.repository.update_project(
                project_id,
                status="running",
                stage="auditing_subtitles",
                progress=90 + round(5 * result_index / max(1, len(affected_part_ids))),
                pipeline_detail={
                    "pass": 3,
                    "mode": "selected",
                    "part": int(part["sequence"]) + 1,
                    "part_count": len(parts),
                    "windows": [int(window["window_number"]) for window in windows],
                },
                error=None,
            )
            audited, issues = audit_subtitle_window(
                tokens,
                hybrid,
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                part_start,
                part_end,
                context,
                previous_part_cues,
                audit_provider,
                codex_model=codex_model,
                audit_dir=(
                    run_dir
                    / f"part-{int(part['sequence']) + 1:02d}"
                    / "pass3"
                ),
                include_issues=True,
                editable_ranges=review_ranges,
                replacement_ranges=editable_ranges,
            )
            for segment in audited:
                metadata = untouched_metadata.get(
                    (int(segment["start_token_id"]), int(segment["end_token_id"]))
                )
                if metadata is not None:
                    segment["status"], segment["quality_flags"] = metadata
            _validate_range_coverage(audited, part_start, part_end)
            results.append(
                {
                    "part_id": part_id,
                    "source_text": reference["source_text"],
                    "reference_translation": reference["reference_translation"],
                    "reference_provider": reference_provider,
                    "segments": audited,
                    "issues": issues,
                    "selected_ranges": [
                        {"start_token_id": left, "end_token_id": right}
                        for left, right in editable_ranges
                    ],
                    "batch_providers": batch_providers,
                }
            )

        self.repository.update_project(
            project_id,
            stage="validating_subtitles",
            progress=96,
            pipeline_detail={"label": "Final validation", "mode": "selected"},
            error=None,
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "staged-result.json").write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "selected_windows": selected_windows,
                    "affected_parts": affected_part_ids,
                    "ready_for_atomic_commit": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.repository.commit_partial_rebuild(project_id, expected_revision, results)

    def _build_timed_tokens(
        self,
        project_id: str,
        video_path: Path,
        subtitle_path: Path | None,
        subtitle_source: str,
    ) -> list[dict]:
        if subtitle_path:
            return reconstruct_vtt_tokens(subtitle_path, source=subtitle_source)
        self.repository.update_project(
            project_id, stage="transcribing", progress=58, subtitle_source="whisperx"
        )
        return transcribe_tokens(video_path)

    def _save_tokens_and_translate(self, project_id: str, tokens: list[dict]) -> None:
        if not tokens:
            raise RuntimeError("No usable English timed tokens were generated")

        # The normalized token timeline is the durable source of truth. A CLI
        # failure can be retried without downloading or transcribing again.
        self.repository.replace_timed_tokens(project_id, tokens)
        artifacts_dir = self.project_dir(project_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "timed-tokens.json").write_text(
            json.dumps(tokens, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.repository.update_project(project_id, progress=65)
        self.translate(project_id)

    def translate(self, project_id: str) -> None:
        project = self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        tokens = self.repository.list_timed_tokens(project_id)
        if not tokens:
            raise RuntimeError("The project has no timed tokens to plan and translate")
        providers = _provider_sequence(str(project.get("translation_provider") or "auto"))
        codex_model = str(project.get("codex_model") or settings.codex_model)

        global_context = project.get("global_translation_context")
        if not isinstance(global_context, dict):
            self.repository.update_project(
                project_id,
                status="running",
                stage="building_global_context",
                progress=66,
                pipeline_detail={"pass": 0, "mode": "full"},
                error=None,
            )
            global_context, global_provider = generate_global_translation_context(
                tokens,
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                providers,
                codex_model,
                audit_dir=self.project_dir(project_id)
                / "artifacts"
                / "translation-audit"
                / "global-context",
            )
            self.repository.update_project(
                project_id,
                global_translation_context=global_context,
                global_context_provider=global_provider,
                global_context_approved=0,
                status="waiting_context_review",
                stage="waiting_context_review",
                progress=68,
                pipeline_detail={"pass": 0, "mode": "full", "waiting_review": True},
            )
            artifacts_dir = self.project_dir(project_id) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "global-translation-context.json").write_text(
                json.dumps(global_context, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return

        if not project.get("global_context_approved"):
            self.repository.update_project(
                project_id,
                status="waiting_context_review",
                stage="waiting_context_review",
                progress=68,
                pipeline_detail={"pass": 0, "mode": "full", "waiting_review": True},
                error=None,
            )
            return

        artifacts_dir = self.project_dir(project_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "global-translation-context-reviewed.json").write_text(
            json.dumps(global_context, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        parts = self.repository.list_subtitle_parts(project_id)
        if not parts:
            plan = build_subtitle_parts(
                tokens,
                max(40, settings.subtitle_planner_window_tokens),
                self._chapter_starts_ms(project_id),
            )
            self.repository.initialize_subtitle_plan(
                project_id, plan, settings.subtitle_planner_window_tokens
            )
            parts = self.repository.list_subtitle_parts(project_id)
        self.repository.update_project(
            project_id,
            status="running",
            stage="translating_reference",
            progress=68,
            error=None,
        )

        for part_index, part in enumerate(parts):
            if not part["reference_translation"]:
                self.repository.update_project(
                    project_id,
                    status="running",
                    stage="translating_reference",
                    pipeline_detail={
                        "pass": 1,
                        "mode": "full",
                        "part": part_index + 1,
                        "part_count": len(parts),
                    },
                    error=None,
                )
                self.repository.update_subtitle_part(
                    part["id"], status="translating_reference", error=None
                )
                reference, provider = generate_reference_translation(
                    tokens[part["start_token_id"] : part["end_token_id"] + 1],
                    str(project["target_language"]),
                    str(project.get("title") or "Untitled video"),
                    part_index + 1,
                    len(parts),
                    providers,
                    codex_model,
                    audit_dir=self.project_dir(project_id)
                    / "artifacts"
                    / "translation-audit"
                    / f"part-{part['id']}-reference",
                    global_context=global_context,
                )
                part_artifacts = self.project_dir(project_id) / "artifacts" / "parts"
                part_artifacts.mkdir(parents=True, exist_ok=True)
                part_name = f"part-{part_index + 1:02d}"
                (part_artifacts / f"{part_name}-source.txt").write_text(
                    reference["source_text"], encoding="utf-8"
                )
                (part_artifacts / f"{part_name}-reference-translation.txt").write_text(
                    reference["reference_translation"], encoding="utf-8"
                )
                self.repository.update_subtitle_part(
                    part["id"],
                    status="planning_subtitles",
                    source_text=reference["source_text"],
                    reference_translation=reference["reference_translation"],
                    translation_provider=provider,
                    error=None,
                )
                part = next(
                    item
                    for item in self.repository.list_subtitle_parts(project_id)
                    if item["id"] == part["id"]
                )

            while True:
                batches = self.repository.list_subtitle_batches(part["id"])
                pending = [
                    batch
                    for batch in batches
                    if batch["status"] != "completed"
                    and batch.get("error") != "Superseded by smaller retry batches"
                ]
                if not pending:
                    break
                batch = pending[0]
                active_batches = [
                    item
                    for item in batches
                    if item.get("error") != "Superseded by smaller retry batches"
                ]
                window_number = next(
                    index
                    for index, item in enumerate(active_batches, start=1)
                    if int(item["id"]) == int(batch["id"])
                )
                self.repository.update_project(
                    project_id,
                    status="running",
                    stage="planning_subtitles",
                    pipeline_detail={
                        "pass": 2,
                        "mode": "full",
                        "part": part_index + 1,
                        "part_count": len(parts),
                        "window": window_number,
                        "window_count": len(active_batches),
                        "start_token_id": int(batch["start_token_id"]),
                        "end_token_id": int(batch["end_token_id"]),
                    },
                    error=None,
                )
                self.repository.update_subtitle_batch(
                    batch["id"],
                    status="translating",
                    attempts=int(batch["attempts"]) + 1,
                    error=None,
                )
                previous_cues = [
                    segment
                    for segment in self.repository.list_segments(project_id)
                    if int(segment.get("end_token_id") or -1) < batch["start_token_id"]
                ][-3:]
                context = {
                    **global_context,
                    "reference_translation": part["reference_translation"],
                }
                audit_dir = (
                    self.project_dir(project_id)
                    / "artifacts"
                    / "translation-audit"
                    / f"batch-{batch['id']}"
                )
                try:
                    segments, provider = plan_subtitle_window(
                        tokens,
                        str(project["target_language"]),
                        str(project.get("title") or "Untitled video"),
                        batch["start_token_id"],
                        batch["end_token_id"],
                        context,
                        previous_cues,
                        providers=providers,
                        codex_model=codex_model,
                        audit_dir=audit_dir,
                    )
                except TranslationQuotaError as exc:
                    self.repository.update_subtitle_batch(
                        batch["id"], status="waiting_for_quota", error=str(exc)
                    )
                    self.repository.update_subtitle_part(
                        part["id"], status="waiting_for_quota", error=str(exc)
                    )
                    self.repository.update_project(
                        project_id,
                        status="waiting_for_quota",
                        stage="waiting_for_quota",
                        error=str(exc),
                    )
                    raise
                except Exception as exc:
                    batch_size = batch["end_token_id"] - batch["start_token_id"] + 1
                    if batch_size > 350:
                        split_after = find_natural_split(
                            tokens, batch["start_token_id"], batch["end_token_id"]
                        )
                        self.repository.split_subtitle_batch(batch["id"], split_after)
                        continue
                    self.repository.update_subtitle_batch(
                        batch["id"], status="failed", error=str(exc)
                    )
                    self.repository.update_subtitle_part(
                        part["id"], status="failed", error=str(exc)
                    )
                    raise
                self.repository.save_batch_segments(
                    project_id, part["id"], batch["id"], segments, provider
                )
                all_parts = self.repository.list_subtitle_parts(project_id)
                completed = sum(int(item["completed_batches"] or 0) for item in all_parts)
                total = sum(int(item["batch_count"] or 0) for item in all_parts)
                self.repository.update_project(
                    project_id, progress=min(94, 72 + round(22 * completed / max(1, total)))
                )

            latest_part = next(
                item
                for item in self.repository.list_subtitle_parts(project_id)
                if item["id"] == part["id"]
            )
            if latest_part["status"] != "completed":
                self._audit_part(
                    project,
                    latest_part,
                    tokens,
                    global_context,
                    providers,
                    codex_model,
                )

        self.repository.update_project(
            project_id,
            stage="validating_subtitles",
            progress=96,
            pipeline_detail={"label": "Final validation", "mode": "full"},
        )
        segments = self.repository.list_segments(project_id)
        _validate_complete_coverage(tokens, segments)
        artifacts_dir = self.project_dir(project_id) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "subtitle-cues.json").write_text(
            json.dumps(segments, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (artifacts_dir / "subtitle-quality-summary.json").write_text(
            json.dumps(summarize_length_budget(segments), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.repository.update_project(
            project_id,
            status="waiting_review",
            stage="waiting_review",
            progress=100,
            pipeline_detail=None,
            subtitle_revision=max(1, int(project["subtitle_revision"]) + 1),
            error=None,
        )

    def _audit_part(
        self,
        project: dict,
        part: dict,
        tokens: list[dict],
        global_context: dict,
        providers: tuple[str, ...],
        codex_model: str,
    ) -> None:
        project_id = str(project["id"])
        all_parts = self.repository.list_subtitle_parts(project_id)
        self.repository.update_project(
            project_id,
            status="running",
            stage="auditing_subtitles",
            pipeline_detail={
                "pass": 3,
                "mode": "full",
                "part": int(part["sequence"]) + 1,
                "part_count": len(all_parts),
                "windows": [
                    index
                    for index, batch in enumerate(
                        [
                            item
                            for item in self.repository.list_subtitle_batches(part["id"])
                            if item.get("error") != "Superseded by smaller retry batches"
                        ],
                        start=1,
                    )
                ],
            },
            error=None,
        )
        self.repository.update_subtitle_part(part["id"], status="auditing_subtitles", error=None)
        project_segments = self.repository.list_segments(project_id)
        part_segments = [
            segment for segment in project_segments if int(segment.get("part_id") or -1) == part["id"]
        ]
        if not part_segments:
            raise RuntimeError(f"Part {part['sequence'] + 1} has no Pass 2 cues to audit")
        previous_cues = [
            segment
            for segment in project_segments
            if int(segment.get("end_token_id") or -1) < int(part["start_token_id"])
        ][-3:]
        context = {
            **global_context,
            "reference_translation": part["reference_translation"],
        }
        batches = [
            batch
            for batch in self.repository.list_subtitle_batches(part["id"])
            if batch.get("error") != "Superseded by smaller retry batches"
        ]
        provider = next(
            (
                str(batch["provider"])
                for batch in reversed(batches)
                if batch.get("provider")
            ),
            providers[0],
        )
        audit_dir = (
            self.project_dir(project_id)
            / "artifacts"
            / "translation-audit"
            / f"part-{part['id']}-semantic-audit"
        )
        try:
            audited_segments, review_issues = audit_subtitle_window(
                tokens,
                part_segments,
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                int(part["start_token_id"]),
                int(part["end_token_id"]),
                context,
                previous_cues,
                provider,
                codex_model=codex_model,
                audit_dir=audit_dir,
                include_issues=True,
                replacement_ranges=[
                    (int(batch["start_token_id"]), int(batch["end_token_id"]))
                    for batch in batches
                ],
            )
        except TranslationQuotaError as exc:
            self.repository.update_subtitle_part(
                part["id"], status="waiting_for_quota", error=str(exc)
            )
            self.repository.update_project(
                project_id,
                status="waiting_for_quota",
                stage="waiting_for_quota",
                error=str(exc),
            )
            raise
        except Exception as exc:
            self.repository.update_subtitle_part(part["id"], status="failed", error=str(exc))
            raise
        self.repository.replace_part_segments(project_id, part["id"], audited_segments)
        self.repository.replace_part_review_issues(project_id, part["id"], review_issues)
        self.repository.update_subtitle_part(part["id"], status="completed", error=None)

    def _chapter_starts_ms(self, project_id: str) -> list[int]:
        metadata_path = self.project_dir(project_id) / "metadata.json"
        if not metadata_path.exists():
            return []
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        chapters = metadata.get("chapters") or []
        return [
            round(float(chapter["start_time"]) * 1000)
            for chapter in chapters
            if isinstance(chapter, dict) and chapter.get("start_time") is not None
        ]

    def render(self, project_id: str) -> None:
        project = self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if not project.get("video_path"):
            raise RuntimeError("The project has no downloaded video")
        segments = self.repository.list_segments(project_id)
        self.repository.update_project(
            project_id,
            status="running",
            stage="rendering",
            progress=5,
            pipeline_detail={"label": "Rendering video"},
            error=None,
        )
        output_path = render_project(
            self.project_dir(project_id),
            Path(project["video_path"]),
            segments,
            bool(project["bilingual"]),
            output_dir=settings.output_dir / project_id,
            progress_callback=lambda value: self.repository.update_project(
                project_id, progress=max(5, min(99, value))
            ),
            subtitle_style=project["subtitle_style"],
        )
        latest = self.repository.get_project(project_id)
        assert latest is not None
        self.repository.update_project(
            project_id,
            status="completed",
            stage="completed",
            progress=100,
            pipeline_detail=None,
            output_path=str(output_path),
            rendered_revision=latest["subtitle_revision"],
            error=None,
        )


def _merge_terms(
    existing: list[dict[str, str]], candidates: list[dict[str, str]]
) -> list[dict[str, str]]:
    result = list(existing)
    known = {str(item.get("source", "")).casefold() for item in result}
    for candidate in candidates:
        source = str(candidate.get("source", "")).strip()
        target = str(candidate.get("target", "")).strip()
        if source and target and source.casefold() not in known:
            result.append({"source": source, "target": target})
            known.add(source.casefold())
    return result[:20]


def _provider_sequence(mode: str) -> tuple[str, ...]:
    if mode == "codex":
        return ("codex",)
    if mode == "grok":
        return ("grok",)
    return settings.translation_providers


def _validate_complete_coverage(tokens: list[dict], segments: list[dict]) -> None:
    if not segments:
        raise RuntimeError("Subtitle planning produced no reviewable segments")
    expected = 0
    for segment in segments:
        start_id = segment.get("start_token_id")
        end_id = segment.get("end_token_id")
        if start_id != expected or not isinstance(end_id, int) or end_id < start_id:
            raise RuntimeError(
                f"Subtitle token coverage is invalid at token {expected}: {start_id}-{end_id}"
            )
        expected = end_id + 1
    if expected != len(tokens):
        raise RuntimeError(
            f"Subtitle token coverage stopped at {expected - 1}; expected {len(tokens) - 1}"
        )


def _validate_range_coverage(segments: list[dict], start: int, end: int) -> None:
    if not segments:
        raise RuntimeError(f"Subtitle range {start}-{end} has no Cues")
    expected = start
    for segment in sorted(segments, key=lambda item: int(item["start_token_id"])):
        segment_start = segment.get("start_token_id")
        segment_end = segment.get("end_token_id")
        if (
            not isinstance(segment_start, int)
            or not isinstance(segment_end, int)
            or segment_start != expected
            or segment_end < segment_start
        ):
            raise RuntimeError(
                f"Subtitle Token coverage is invalid at {expected}: "
                f"{segment_start}-{segment_end}"
            )
        expected = segment_end + 1
    if expected != end + 1:
        raise RuntimeError(
            f"Subtitle Token coverage stopped at {expected - 1}; expected {end}"
        )


def _merge_token_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
