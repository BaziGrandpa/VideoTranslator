from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.config import settings
from app.repository import Repository
from app.services.caption_tokens import reconstruct_vtt_tokens
from app.services.cues import build_cues
from app.services.downloader import download_youtube
from app.services.parts import build_parts
from app.services.punctuation import source_fingerprint, source_punctuation_count
from app.services.renderer import render_project
from app.services.subtitle_planner import (
    TranslationQuotaError,
    align_part_cues,
    generate_punctuation_annotations,
    generate_translation_context,
    merge_short_chinese_cues,
    split_long_cues,
    translate_part,
)
from app.services.subtitle_quality import (
    RULES,
    stamp_segment_review_flags,
    summarize_length_budget,
    target_units,
)
from app.services.transcriber import transcribe_tokens
from app.services.translation_audit import record_event

logger = logging.getLogger(__name__)


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
        tokens = self._build_tokens(
            project_id,
            Path(downloaded["video_path"]),
            Path(downloaded["subtitle_path"]) if downloaded["subtitle_path"] else None,
            str(downloaded["subtitle_source"] or "youtube_caption"),
        )
        self._save_tokens_and_translate(project_id, tokens)

    def rebuild_subtitles(self, project_id: str, *, skip_pass0: bool = False) -> None:
        project = self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if not project.get("video_path") or not Path(project["video_path"]).exists():
            raise RuntimeError("The project has no downloaded video to rebuild")
        source_dir = self.project_dir(project_id) / "source"
        captions = sorted(source_dir.glob("video*.vtt"))
        if skip_pass0 and not isinstance(project.get("translation_context"), dict):
            raise RuntimeError("Pass 0 has no existing context to reuse")
        self.repository.clear_subtitle_data(
            project_id, preserve_translation_context=skip_pass0
        )
        self.repository.update_project(
            project_id,
            status="running",
            stage="reconstructing_timeline",
            progress=55,
        )
        tokens = self._build_tokens(
            project_id,
            Path(project["video_path"]),
            captions[0] if captions else None,
            str(project.get("subtitle_source") or "youtube_caption"),
        )
        self._save_tokens_and_translate(project_id, tokens)

    def part_rebuild(self, project_id: str) -> None:
        """Rerun Pass 1–3 for each selected Part and publish those Parts together."""
        project = self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        scope = project.get("pending_rebuild_scope")
        if not isinstance(scope, dict) or not isinstance(scope.get("parts"), list):
            raise TypeError("The project has no pending selected-Part rebuild")
        tokens = self.repository.list_tokens(project_id)
        if not tokens:
            raise RuntimeError("Selected Part rebuild requires the existing Timed Tokens")
        translation_context = project.get("translation_context")
        if not isinstance(translation_context, dict) or not project.get("context_approved"):
            raise RuntimeError("Selected Part rebuild requires approved translation guidance")
        providers = _provider_order(str(project.get("translation_provider") or "auto"))
        codex_model = str(project.get("codex_model") or settings.codex_model)
        run_dir = (
            self.project_dir(project_id)
            / "artifacts"
            / "part-rebuilds"
            / str(scope.get("run_id") or "unknown-run")
        )
        expected_revision = int(scope.get("base_subtitle_revision", -1))
        if int(project["subtitle_revision"]) != expected_revision:
            raise RuntimeError(
                "Subtitles changed after the selected Parts were queued; start the selection again"
            )
        parts = self.repository.list_parts(project_id)
        parts_by_id = {int(part["id"]): part for part in parts}
        selected_parts = sorted(
            (dict(item) for item in scope["parts"]),
            key=lambda item: int(parts_by_id[int(item["part_id"])]["part_number"]),
        )
        results: list[dict[str, Any]] = []
        for result_index, selected_part in enumerate(selected_parts):
            part_id = int(selected_part["part_id"])
            part = parts_by_id.get(part_id)
            if part is None:
                raise RuntimeError(f"Selected Part {part_id} no longer exists")
            if (
                int(part["start_token_id"]) != int(selected_part["start_token_id"])
                or int(part["end_token_id"]) != int(selected_part["end_token_id"])
            ):
                raise RuntimeError("A selected Part changed after it was queued")
            cues = self.repository.list_cues(part_id)
            if not cues:
                raise RuntimeError(
                    f"Part {int(part['part_number'])} has no cues. Run one full rebuild first."
                )
            self.repository.update_project(
                project_id,
                status="running",
                stage="translating_part",
                progress=70,
                pipeline_detail={
                    "pass": 1,
                    "mode": "parts",
                    "part": int(part["part_number"]),
                    "part_count": len(parts),
                },
                error=None,
            )
            translated_part, translation_provider = translate_part(
                tokens[int(part["start_token_id"]) : int(part["end_token_id"]) + 1],
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                int(part["part_number"]),
                len(parts),
                providers,
                codex_model,
                audit_dir=run_dir / f"part-{int(part['part_number']):02d}" / "pass1",
                translation_context=translation_context,
            )
            context = {**translation_context, "target_text": translated_part["target_text"]}
            self.repository.update_project(
                project_id,
                stage="aligning_cues",
                progress=74 + round(12 * result_index / max(1, len(selected_parts))),
                pipeline_detail={
                    "pass": 2,
                    "mode": "parts",
                    "part": int(part["part_number"]),
                    "part_count": len(parts),
                    "cue_count": len(cues),
                },
                error=None,
            )
            aligned_cues, _pass2_provider = align_part_cues(
                tokens,
                cues,
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                int(part["part_number"]),
                len(parts),
                context,
                providers=providers,
                codex_model=codex_model,
                audit_dir=run_dir / f"part-{int(part['part_number']):02d}" / "pass2",
            )
            chinese_by_id = {
                int(segment["cue_id"]): str(segment["target_text"])
                for segment in aligned_cues
            }
            cues = [
                {
                    **cue,
                    "target_text": chinese_by_id.get(
                        int(cue["cue_id"]),
                        str(cue.get("target_text", "")),
                    ),
                }
                for cue in cues
            ]
            long_parents = [
                parent
                for parent in aligned_cues
                if target_units(str(parent["target_text"])) > RULES.max_target_units
            ]
            selected_final = aligned_cues
            raw_subcues: list[dict[str, Any]] = []
            if long_parents:
                self.repository.update_project(
                    project_id,
                    stage="splitting_cues",
                    progress=90 + round(5 * result_index / max(1, len(selected_parts))),
                    pipeline_detail={
                        "pass": 3,
                        "mode": "parts",
                        "part": int(part["part_number"]),
                        "part_count": len(parts),
                        "long_cue_count": len(long_parents),
                    },
                    error=None,
                )
                split_result = split_long_cues(
                    tokens,
                    long_parents,
                    cues,
                    str(project["target_language"]),
                    str(project.get("title") or "Untitled video"),
                    context,
                    providers=providers,
                    codex_model=codex_model,
                    audit_dir=run_dir / f"part-{int(part['part_number']):02d}" / "pass3",
                    include_subcues=True,
                )
                split_segments, _pass3_provider = split_result[:2]
                raw_subcues = split_result[2] if len(split_result) == 3 else [
                    row for row in split_segments if row.get("subcue_id")
                ]
                split_ids = {int(item["cue_id"]) for item in long_parents}
                selected_final = [
                    item
                    for item in aligned_cues
                    if int(item["cue_id"]) not in split_ids
                ] + split_segments
            final_segments, short_chinese_merges = merge_short_chinese_cues(
                tokens, selected_final
            )
            if short_chinese_merges:
                record_event(
                    run_dir / f"part-{int(part['part_number']):02d}" / "pass3",
                    "short-chinese-cues-merged",
                    merges=short_chinese_merges,
                )
            final_segments = stamp_segment_review_flags(final_segments)
            _validate_range_coverage(
                final_segments, int(part["start_token_id"]), int(part["end_token_id"])
            )
            results.append(
                {
                    "part_id": part_id,
                    "source_text": translated_part["source_text"],
                    "target_text": translated_part["target_text"],
                    "translation_provider": translation_provider,
                    "cues": cues,
                    "subcues": raw_subcues,
                    "segments": final_segments,
                }
            )

        self.repository.update_project(
            project_id,
            stage="validating_subtitles",
            progress=96,
            pipeline_detail={"label": "Final validation", "mode": "parts"},
            error=None,
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "staged-result.json").write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "selected_parts": [
                        int(item["part_id"]) for item in selected_parts
                    ],
                    "ready_for_atomic_commit": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.repository.commit_part_rebuild(project_id, expected_revision, results)

    def _build_tokens(
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
        self.repository.replace_tokens(project_id, tokens)
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
        tokens = self.repository.list_tokens(project_id)
        if not tokens:
            raise RuntimeError("The project has no timed tokens to plan and translate")
        providers = _provider_order(str(project.get("translation_provider") or "auto"))
        codex_model = str(project.get("codex_model") or settings.codex_model)

        if project.get("punctuation_enabled"):
            parts = self.repository.list_parts(project_id)
            if not parts:
                plan = build_parts(tokens, self._chapter_starts_ms(project_id))
                self.repository.initialize_subtitle_plan(project_id, plan)
                parts = self.repository.list_parts(project_id)
            self._prepare_part_punctuation(project, tokens, parts, providers, codex_model)
            tokens = self.repository.list_tokens(project_id)

        translation_context = project.get("translation_context")
        if not isinstance(translation_context, dict):
            self.repository.update_project(
                project_id,
                status="running",
                stage="building_translation_context",
                progress=66,
                pipeline_detail={"pass": 0, "mode": "full"},
                error=None,
            )
            translation_context, global_provider = generate_translation_context(
                tokens,
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                providers,
                codex_model,
                audit_dir=self.project_dir(project_id)
                / "artifacts"
                / "translation-audit"
                / "translation-context",
            )
            self.repository.update_project(
                project_id,
                translation_context=translation_context,
                context_provider=global_provider,
                context_approved=0,
                status="waiting_context_review",
                stage="waiting_context_review",
                progress=68,
                pipeline_detail={"pass": 0, "mode": "full", "waiting_review": True},
            )
            artifacts_dir = self.project_dir(project_id) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "translation-context.json").write_text(
                json.dumps(translation_context, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return

        if not project.get("context_approved"):
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
        (artifacts_dir / "translation-context-reviewed.json").write_text(
            json.dumps(translation_context, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        parts = self.repository.list_parts(project_id)
        if not parts:
            plan = build_parts(tokens, self._chapter_starts_ms(project_id))
            self.repository.initialize_subtitle_plan(project_id, plan)
            parts = self.repository.list_parts(project_id)
        self.repository.update_project(
            project_id,
            status="running",
            stage="translating_part",
            progress=68,
            error=None,
        )

        for part_index, part in enumerate(parts):
            if not part["target_text"]:
                self.repository.update_project(
                    project_id,
                    status="running",
                    stage="translating_part",
                    pipeline_detail={
                        "pass": 1,
                        "mode": "full",
                        "part": part_index + 1,
                        "part_count": len(parts),
                    },
                    error=None,
                )
                self.repository.update_part(
                    part["id"], status="translating_part", error=None
                )
                translated_part, provider = translate_part(
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
                    / f"part-{part['id']}-pass1",
                    translation_context=translation_context,
                )
                part_artifacts = self.project_dir(project_id) / "artifacts" / "parts"
                part_artifacts.mkdir(parents=True, exist_ok=True)
                part_name = f"part-{part_index + 1:02d}"
                (part_artifacts / f"{part_name}-source.txt").write_text(
                    translated_part["source_text"], encoding="utf-8"
                )
                (part_artifacts / f"{part_name}-part-translation.txt").write_text(
                    translated_part["target_text"], encoding="utf-8"
                )
                self.repository.update_part(
                    part["id"],
                    status="aligning_cues",
                    source_text=translated_part["source_text"],
                    target_text=translated_part["target_text"],
                    translation_provider=provider,
                    error=None,
                )
                part = next(
                    item
                    for item in self.repository.list_parts(project_id)
                    if item["id"] == part["id"]
                )

            cues = self.repository.list_cues(int(part["id"]))
            if not cues:
                cues = build_cues(
                    tokens, int(part["start_token_id"]), int(part["end_token_id"])
                )
                self.repository.replace_cues(
                    project_id, int(part["id"]), cues
                )
            context = {
                **translation_context,
                "target_text": part["target_text"],
            }
            pass2_complete = bool(cues) and all(
                str(cue.get("target_text", "")).strip() for cue in cues
            ) and part["status"] in {"pass2_complete", "splitting_long_cues", "failed", "completed"}
            if not pass2_complete:
                self.repository.update_project(
                    project_id,
                    status="running",
                    stage="aligning_cues",
                    progress=72 + round(12 * part_index / max(1, len(parts))),
                    pipeline_detail={
                        "pass": 2,
                        "mode": "full",
                        "part": part_index + 1,
                        "part_count": len(parts),
                        "cue_count": len(cues),
                    },
                    error=None,
                )
                self.repository.update_part(
                    int(part["id"]), status="aligning_cues", error=None
                )
                try:
                    aligned_cues, _provider = align_part_cues(
                        tokens,
                        cues,
                        str(project["target_language"]),
                        str(project.get("title") or "Untitled video"),
                        part_index + 1,
                        len(parts),
                        context,
                        providers=providers,
                        codex_model=codex_model,
                        audit_dir=(
                            self.project_dir(project_id)
                            / "artifacts"
                            / "translation-audit"
                            / f"part-{part['id']}-pass2"
                        ),
                    )
                except TranslationQuotaError as exc:
                    self.repository.update_part(
                        int(part["id"]), status="waiting_for_quota", error=str(exc)
                    )
                    self.repository.update_project(
                        project_id,
                        status="waiting_for_quota",
                        stage="waiting_for_quota",
                        error=str(exc),
                    )
                    raise
                except Exception as exc:
                    self.repository.update_part(
                        int(part["id"]), status="failed", error=str(exc)
                    )
                    raise
                chinese_by_id = {
                    int(segment["cue_id"]): str(segment["target_text"])
                    for segment in aligned_cues
                }
                cues = [
                    {
                        **cue,
                        "target_text": chinese_by_id[int(cue["cue_id"])],
                    }
                    for cue in cues
                ]
                self.repository.commit_pass2(project_id, int(part["id"]), cues)
                part_artifacts = self.project_dir(project_id) / "artifacts" / "parts"
                part_artifacts.mkdir(parents=True, exist_ok=True)
                part_name = f"part-{part_index + 1:02d}"
                (part_artifacts / f"{part_name}-cues.json").write_text(
                    json.dumps(cues, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (part_artifacts / f"{part_name}-pass2-aligned-cues.json").write_text(
                    json.dumps(aligned_cues, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            latest_part = next(
                item
                for item in self.repository.list_parts(project_id)
                if item["id"] == part["id"]
            )
            if latest_part["status"] != "completed":
                self._split_part_long_cues(
                    project,
                    latest_part,
                    tokens,
                    cues,
                    context,
                    providers,
                    codex_model,
                )
            self.repository.update_project(
                project_id,
                progress=min(94, 72 + round(22 * (part_index + 1) / max(1, len(parts)))),
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
        (artifacts_dir / "subtitle-segments.json").write_text(
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

    def _prepare_part_punctuation(
        self,
        project: dict,
        tokens: list[dict],
        parts: list[dict],
        providers: tuple[str, ...],
        codex_model: str,
    ) -> None:
        project_id = str(project["id"])
        for part_index, part in enumerate(parts, start=1):
            part_tokens = tokens[int(part["start_token_id"]): int(part["end_token_id"]) + 1]
            count = source_punctuation_count(part_tokens)
            if count:
                logger.info(
                    "Punctuation Pass skipped: project %s, Part %s/%s, %s original marks in %s tokens",
                    project_id, part_index, len(parts), count, len(part_tokens),
                )
                continue
            fingerprint = source_fingerprint(part_tokens)
            saved = self.repository.get_part_punctuation(int(part["id"]))
            if saved is not None:
                if saved["source_fingerprint"] != fingerprint:
                    raise RuntimeError(
                        f"Punctuation Pass failed — Part {part_index}/{len(parts)}: "
                        "the original tokens changed; run a full rebuild"
                    )
                if saved["status"] == "completed":
                    logger.info(
                        "Punctuation Pass reused: project %s, Part %s/%s, %s original marks",
                        project_id, part_index, len(parts), count,
                    )
                    continue
                raise RuntimeError(
                    f"Punctuation Pass failed — Part {part_index}/{len(parts)}: "
                    f"previous request is {saved['status']} ({saved['error'] or 'interrupted'}); "
                    "no automatic second call; run a full rebuild to retry"
                )
            if not providers or providers[0] not in {"codex", "grok", "grokbuild"}:
                raise RuntimeError(
                    f"Punctuation Pass failed — Part {part_index}/{len(parts)}: "
                    "no supported model provider is configured"
                )
            provider = providers[0]
            audit_dir = (
                self.project_dir(project_id) / "artifacts" / "translation-audit"
                / f"part-{part_index:02d}-punctuation"
            )
            logger.info(
                "Punctuation Pass required: project %s, Part %s/%s, 0 original marks "
                "in %s tokens; provider %s; request 1/1",
                project_id, part_index, len(parts), len(part_tokens), provider,
            )
            self.repository.update_project(
                project_id,
                status="running",
                stage="restoring_punctuation",
                progress=65,
                pipeline_detail={
                    "label": "Restoring English punctuation (request 1/1)",
                    "part": part_index,
                    "part_count": len(parts),
                },
                error=None,
            )
            self.repository.begin_part_punctuation(
                project_id, int(part["id"]), fingerprint, provider
            )
            try:
                marks = generate_punctuation_annotations(
                    part_tokens,
                    str(project.get("title") or "Untitled video"),
                    part_index,
                    len(parts),
                    provider,
                    codex_model,
                    audit_dir,
                )
                self.repository.complete_part_punctuation(int(part["id"]), marks)
            except Exception as exc:
                self.repository.fail_part_punctuation(int(part["id"]), str(exc))
                message = (
                    f"Punctuation Pass failed — Part {part_index}/{len(parts)}: {exc}; "
                    f"provider {provider}, request 1/1, no punctuation saved; "
                    f"Pass 0 not started; audit: {audit_dir}"
                )
                logger.error(message)
                raise RuntimeError(message) from exc
            logger.info(
                "Punctuation Pass completed: project %s, Part %s/%s, %s marks validated",
                project_id, part_index, len(parts), len(marks),
            )

    def _split_part_long_cues(
        self,
        project: dict,
        part: dict,
        tokens: list[dict],
        cues: list[dict],
        context: dict,
        providers: tuple[str, ...],
        codex_model: str,
    ) -> None:
        project_id = str(project["id"])
        all_parts = self.repository.list_parts(project_id)
        parent_cues = [
            {
                **cue,
                "subcue_id": None,
                "quality_flags": [],
                "status": "machine_translated",
            }
            for cue in cues
        ]
        if not parent_cues:
            raise RuntimeError(f"Part {int(part['part_number'])} has no Pass 2 Cues")
        long_parents = [
            segment
            for segment in parent_cues
            if target_units(str(segment["target_text"])) > RULES.max_target_units
        ]
        self.repository.update_project(
            project_id,
            status="running",
            stage="splitting_cues",
            pipeline_detail={
                "pass": 3,
                "mode": "full",
                "part": int(part["part_number"]),
                "part_count": len(all_parts),
                "long_cue_count": len(long_parents),
            },
            error=None,
        )
        self.repository.update_part(
            int(part["id"]), status="splitting_long_cues", error=None
        )
        final_segments = parent_cues
        if long_parents:
            try:
                split_result = split_long_cues(
                    tokens,
                    long_parents,
                    cues,
                    str(project["target_language"]),
                    str(project.get("title") or "Untitled video"),
                    context,
                    providers=providers,
                    codex_model=codex_model,
                    audit_dir=(
                        self.project_dir(project_id)
                        / "artifacts"
                        / "translation-audit"
                        / f"part-{part['id']}-pass3"
                    ),
                    include_subcues=True,
                )
                split_segments, _provider = split_result[:2]
                raw_subcues = split_result[2] if len(split_result) == 3 else [
                    row for row in split_segments if row.get("subcue_id")
                ]
            except TranslationQuotaError as exc:
                self.repository.update_part(
                    int(part["id"]), status="waiting_for_quota", error=str(exc)
                )
                self.repository.update_project(
                    project_id,
                    status="waiting_for_quota",
                    stage="waiting_for_quota",
                    error=str(exc),
                )
                raise
            except Exception as exc:
                self.repository.update_part(
                    int(part["id"]), status="failed", error=str(exc)
                )
                raise
            replaced_ids = {int(segment["cue_id"]) for segment in long_parents}
            final_segments = [
                segment
                for segment in parent_cues
                if int(segment["cue_id"]) not in replaced_ids
            ] + split_segments
            final_segments.sort(key=lambda item: int(item["start_token_id"]))
        final_segments, short_chinese_merges = merge_short_chinese_cues(tokens, final_segments)
        if short_chinese_merges:
            record_event(
                self.project_dir(project_id)
                / "artifacts"
                / "translation-audit"
                / f"part-{part['id']}-pass3",
                "short-chinese-cues-merged",
                merges=short_chinese_merges,
            )
        _validate_range_coverage(
            final_segments, int(part["start_token_id"]), int(part["end_token_id"])
        )
        final_segments = stamp_segment_review_flags(final_segments)
        self.repository.replace_part_segments(
            project_id, int(part["id"]), final_segments,
            subcues=raw_subcues if long_parents else [],
        )
        self.repository.update_part(int(part["id"]), status="completed", error=None)
        part_artifacts = self.project_dir(project_id) / "artifacts" / "parts"
        part_artifacts.mkdir(parents=True, exist_ok=True)
        part_name = f"part-{int(part['part_number']):02d}"
        (part_artifacts / f"{part_name}-pass3-final-segments.json").write_text(
            json.dumps(final_segments, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (part_artifacts / f"{part_name}-pass3-subcues.json").write_text(
            json.dumps(self.repository.list_subcues(int(part["id"])), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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



def _provider_order(mode: str) -> tuple[str, ...]:
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
