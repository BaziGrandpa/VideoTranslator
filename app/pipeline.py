from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import settings
from app.repository import Repository
from app.services.caption_tokens import reconstruct_vtt_tokens
from app.services.downloader import download_youtube
from app.services.renderer import render_project
from app.services.source_sentences import build_source_sentences
from app.services.subtitle_parts import build_subtitle_parts
from app.services.subtitle_planner import (
    TranslationQuotaError,
    align_part_sentences,
    generate_global_translation_context,
    generate_reference_translation,
    split_long_sentences,
)
from app.services.subtitle_quality import (
    RULES,
    stamp_cue_review_flags,
    summarize_length_budget,
    target_units,
)
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

    def rebuild_subtitles(self, project_id: str, *, skip_pass0: bool = False) -> None:
        project = self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        if not project.get("video_path") or not Path(project["video_path"]).exists():
            raise RuntimeError("The project has no downloaded video to rebuild")
        source_dir = self.project_dir(project_id) / "source"
        captions = sorted(source_dir.glob("video*.vtt"))
        if skip_pass0 and not isinstance(project.get("global_translation_context"), dict):
            raise RuntimeError("Pass 0 has no existing context to reuse")
        self.repository.clear_subtitle_data(
            project_id, preserve_global_context=skip_pass0
        )
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

    def part_rebuild(self, project_id: str) -> None:
        """Rerun Pass 1–3 for each selected Part and publish those Parts together."""
        project = self.repository.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        scope = project.get("pending_rebuild_scope")
        if isinstance(scope, dict) and "windows" in scope and "parts" not in scope:
            raise RuntimeError(
                "This queued rebuild still uses Window selection. Choose Parts and start it again."
            )
        if not isinstance(scope, dict) or not isinstance(scope.get("parts"), list):
            raise TypeError("The project has no pending selected-Part rebuild")
        tokens = self.repository.list_timed_tokens(project_id)
        if not tokens:
            raise RuntimeError("Selected Part rebuild requires the existing Timed Tokens")
        global_context = project.get("global_translation_context")
        if not isinstance(global_context, dict) or not project.get("global_context_approved"):
            raise RuntimeError("Selected Part rebuild requires approved translation guidance")
        providers = _provider_sequence(str(project.get("translation_provider") or "auto"))
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
        parts = self.repository.list_subtitle_parts(project_id)
        parts_by_id = {int(part["id"]): part for part in parts}
        selected_parts = sorted(
            (dict(item) for item in scope["parts"]),
            key=lambda item: int(parts_by_id[int(item["part_id"])]["sequence"]),
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
            source_sentences = self.repository.list_source_sentences(part_id)
            if not source_sentences:
                raise RuntimeError(
                    f"Part {int(part['sequence']) + 1} has no cues. Run one full rebuild first."
                )
            self.repository.update_project(
                project_id,
                status="running",
                stage="translating_reference",
                progress=70,
                pipeline_detail={
                    "pass": 1,
                    "mode": "parts",
                    "part": int(part["sequence"]) + 1,
                    "part_count": len(parts),
                },
                error=None,
            )
            reference, reference_provider = generate_reference_translation(
                tokens[int(part["start_token_id"]) : int(part["end_token_id"]) + 1],
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                int(part["sequence"]) + 1,
                len(parts),
                providers,
                codex_model,
                audit_dir=run_dir / f"part-{int(part['sequence']) + 1:02d}" / "pass1",
                global_context=global_context,
            )
            context = {**global_context, "reference_translation": reference["reference_translation"]}
            self.repository.update_project(
                project_id,
                stage="planning_subtitles",
                progress=74 + round(12 * result_index / max(1, len(selected_parts))),
                pipeline_detail={
                    "pass": 2,
                    "mode": "parts",
                    "part": int(part["sequence"]) + 1,
                    "part_count": len(parts),
                    "source_sentence_count": len(source_sentences),
                },
                error=None,
            )
            selected_parents, _pass2_provider = align_part_sentences(
                tokens,
                source_sentences,
                str(project["target_language"]),
                str(project.get("title") or "Untitled video"),
                int(part["sequence"]) + 1,
                len(parts),
                context,
                providers=providers,
                codex_model=codex_model,
                audit_dir=run_dir / f"part-{int(part['sequence']) + 1:02d}" / "pass2",
            )
            chinese_by_id = {
                int(segment["source_sentence_id"]): str(segment["translated_text"])
                for segment in selected_parents
            }
            source_sentences = [
                {
                    **sentence,
                    "chinese_sentence": chinese_by_id.get(
                        int(sentence["source_sentence_id"]),
                        str(sentence.get("chinese_sentence", "")),
                    ),
                }
                for sentence in source_sentences
            ]
            long_parents = [
                parent
                for parent in selected_parents
                if target_units(str(parent["translated_text"])) > RULES.max_target_units
            ]
            selected_final = selected_parents
            if long_parents:
                self.repository.update_project(
                    project_id,
                    stage="auditing_subtitles",
                    progress=90 + round(5 * result_index / max(1, len(selected_parts))),
                    pipeline_detail={
                        "pass": 3,
                        "mode": "parts",
                        "part": int(part["sequence"]) + 1,
                        "part_count": len(parts),
                        "long_source_sentence_count": len(long_parents),
                    },
                    error=None,
                )
                split_segments, _pass3_provider = split_long_sentences(
                    tokens,
                    long_parents,
                    source_sentences,
                    str(project["target_language"]),
                    str(project.get("title") or "Untitled video"),
                    context,
                    providers=providers,
                    codex_model=codex_model,
                    audit_dir=run_dir / f"part-{int(part['sequence']) + 1:02d}" / "pass3",
                )
                split_ids = {int(item["source_sentence_id"]) for item in long_parents}
                selected_final = [
                    item
                    for item in selected_parents
                    if int(item["source_sentence_id"]) not in split_ids
                ] + split_segments
            final_segments = sorted(
                stamp_cue_review_flags(selected_final),
                key=lambda item: int(item["start_token_id"]),
            )
            _validate_range_coverage(
                final_segments, int(part["start_token_id"]), int(part["end_token_id"])
            )
            results.append(
                {
                    "part_id": part_id,
                    "source_text": reference["source_text"],
                    "reference_translation": reference["reference_translation"],
                    "reference_provider": reference_provider,
                    "source_sentences": source_sentences,
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
            plan = build_subtitle_parts(tokens, self._chapter_starts_ms(project_id))
            self.repository.initialize_subtitle_plan(project_id, plan)
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

            source_sentences = self.repository.list_source_sentences(int(part["id"]))
            if not source_sentences:
                source_sentences = build_source_sentences(
                    tokens, int(part["start_token_id"]), int(part["end_token_id"])
                )
                self.repository.replace_source_sentences(
                    project_id, int(part["id"]), source_sentences
                )
            context = {
                **global_context,
                "reference_translation": part["reference_translation"],
            }
            part_segments = [
                segment
                for segment in self.repository.list_segments(project_id)
                if int(segment.get("part_id") or -1) == int(part["id"])
            ]
            pass2_complete = bool(source_sentences) and all(
                str(sentence.get("chinese_sentence", "")).strip()
                for sentence in source_sentences
            )
            if not pass2_complete:
                self.repository.update_project(
                    project_id,
                    status="running",
                    stage="planning_subtitles",
                    progress=72 + round(12 * part_index / max(1, len(parts))),
                    pipeline_detail={
                        "pass": 2,
                        "mode": "full",
                        "part": part_index + 1,
                        "part_count": len(parts),
                        "source_sentence_count": len(source_sentences),
                    },
                    error=None,
                )
                self.repository.update_subtitle_part(
                    int(part["id"]), status="planning_subtitles", error=None
                )
                try:
                    part_segments, _provider = align_part_sentences(
                        tokens,
                        source_sentences,
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
                    self.repository.update_subtitle_part(
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
                    self.repository.update_subtitle_part(
                        int(part["id"]), status="failed", error=str(exc)
                    )
                    raise
                chinese_by_id = {
                    int(segment["source_sentence_id"]): str(segment["translated_text"])
                    for segment in part_segments
                }
                source_sentences = [
                    {
                        **sentence,
                        "chinese_sentence": chinese_by_id[int(sentence["source_sentence_id"])],
                    }
                    for sentence in source_sentences
                ]
                self.repository.replace_source_sentences(
                    project_id, int(part["id"]), source_sentences
                )
                self.repository.replace_part_segments(project_id, int(part["id"]), part_segments)
                part_artifacts = self.project_dir(project_id) / "artifacts" / "parts"
                part_artifacts.mkdir(parents=True, exist_ok=True)
                part_name = f"part-{part_index + 1:02d}"
                (part_artifacts / f"{part_name}-source-sentences.json").write_text(
                    json.dumps(source_sentences, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (part_artifacts / f"{part_name}-pass2-aligned-sentences.json").write_text(
                    json.dumps(part_segments, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            latest_part = next(
                item
                for item in self.repository.list_subtitle_parts(project_id)
                if item["id"] == part["id"]
            )
            if latest_part["status"] != "completed":
                self._split_part_long_sentences(
                    project,
                    latest_part,
                    tokens,
                    source_sentences,
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

    def _split_part_long_sentences(
        self,
        project: dict,
        part: dict,
        tokens: list[dict],
        source_sentences: list[dict],
        context: dict,
        providers: tuple[str, ...],
        codex_model: str,
    ) -> None:
        project_id = str(project["id"])
        all_parts = self.repository.list_subtitle_parts(project_id)
        parent_segments = [
            segment
            for segment in self.repository.list_segments(project_id)
            if int(segment.get("part_id") or -1) == int(part["id"])
        ]
        if not parent_segments:
            raise RuntimeError(f"Part {int(part['sequence']) + 1} has no Pass 2 sentences")
        long_parents = [
            segment
            for segment in parent_segments
            if target_units(str(segment["translated_text"])) > RULES.max_target_units
        ]
        self.repository.update_project(
            project_id,
            status="running",
            stage="auditing_subtitles",
            pipeline_detail={
                "pass": 3,
                "mode": "full",
                "part": int(part["sequence"]) + 1,
                "part_count": len(all_parts),
                "long_source_sentence_count": len(long_parents),
            },
            error=None,
        )
        self.repository.update_subtitle_part(
            int(part["id"]), status="splitting_long_sentences", error=None
        )
        final_segments = parent_segments
        if long_parents:
            try:
                split_segments, _provider = split_long_sentences(
                    tokens,
                    long_parents,
                    source_sentences,
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
                )
            except TranslationQuotaError as exc:
                self.repository.update_subtitle_part(
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
                self.repository.update_subtitle_part(
                    int(part["id"]), status="failed", error=str(exc)
                )
                raise
            replaced_ids = {int(segment["source_sentence_id"]) for segment in long_parents}
            final_segments = [
                segment
                for segment in parent_segments
                if int(segment["source_sentence_id"]) not in replaced_ids
            ] + split_segments
            final_segments.sort(key=lambda item: int(item["start_token_id"]))
        _validate_range_coverage(
            final_segments, int(part["start_token_id"]), int(part["end_token_id"])
        )
        final_segments = stamp_cue_review_flags(final_segments)
        self.repository.replace_part_segments(project_id, int(part["id"]), final_segments)
        self.repository.update_subtitle_part(int(part["id"]), status="completed", error=None)
        part_artifacts = self.project_dir(project_id) / "artifacts" / "parts"
        part_artifacts.mkdir(parents=True, exist_ok=True)
        part_name = f"part-{int(part['sequence']) + 1:02d}"
        (part_artifacts / f"{part_name}-pass3-splits.json").write_text(
            json.dumps(final_segments, ensure_ascii=False, indent=2), encoding="utf-8"
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
