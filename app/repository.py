from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.db import connect, transaction
from app.services.punctuation import (
    apply_punctuation,
    source_fingerprint,
    validate_punctuation_annotations,
)
from app.services.subtitle_style import merge_subtitle_style
from app.services.subtitle_text import normalize_manual_target_text, normalize_segment_texts

TERMINAL_JOB_STATUSES = {"completed", "failed"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def decode_project(row: sqlite3.Row, context: sqlite3.Row | None = None) -> dict[str, Any]:
    project = dict(row)
    project["bilingual"] = bool(project["bilingual"])
    project["punctuation_enabled"] = bool(project["punctuation_enabled"])
    try:
        saved_style = json.loads(project.get("subtitle_style") or "null")
    except json.JSONDecodeError:
        saved_style = None
    project["subtitle_style"] = merge_subtitle_style(saved_style)
    if context is not None:
        guidance = {
            "overview": context["overview"],
            "style": context["style"],
            "terms": json.loads(context["terms_json"]),
        }
        candidates = json.loads(context["card_candidates_json"])
        if candidates:
            guidance["card_candidates"] = candidates
        project["translation_context"] = guidance
    else:
        project["translation_context"] = None
    project["context_provider"] = context["provider"] if context is not None else None
    project["context_approved"] = bool(context["approved"]) if context is not None else False
    try:
        rebuild_scope = json.loads(project.get("pending_rebuild_scope") or "null")
    except json.JSONDecodeError:
        rebuild_scope = None
    project["pending_rebuild_scope"] = (
        rebuild_scope if isinstance(rebuild_scope, dict) else None
    )
    try:
        pipeline_detail = json.loads(project.get("pipeline_detail") or "null")
    except json.JSONDecodeError:
        pipeline_detail = None
    project["pipeline_detail"] = (
        pipeline_detail if isinstance(pipeline_detail, dict) else None
    )
    return project


class Repository:
    def __init__(self, database_path: Path | None = None):
        self.database_path = database_path

    def create_project(
        self,
        source_url: str,
        target_language: str = "zh-CN",
        max_height: int = 1080,
        bilingual: bool | None = None,
        translation_provider: str = "auto",
        codex_model: str = "",
    ) -> dict[str, Any]:
        project_id = str(uuid.uuid4())
        timestamp = now_iso()
        if bilingual is None:
            bilingual = self.default_bilingual()
        style = self.default_subtitle_style()
        with transaction(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO projects (
                    id, source_url, target_language, max_height, bilingual, translation_provider,
                    codex_model, punctuation_enabled,
                    status, stage, progress, subtitle_style, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'queued', 'queued', 0, ?, ?, ?)
                """,
                (
                    project_id,
                    source_url,
                    target_language,
                    max_height,
                    int(bilingual),
                    translation_provider,
                    codex_model,
                    json.dumps(style),
                    timestamp,
                    timestamp,
                ),
            )
        project = self.get_project(project_id)
        assert project is not None
        return project

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            context = connection.execute(
                "SELECT * FROM translation_contexts WHERE project_id = ?", (project_id,)
            ).fetchone() if row is not None else None
        return decode_project(row, context) if row is not None else None

    def set_resume_time(self, project_id: str, time_ms: int) -> int:
        with transaction(self.database_path) as connection:
            project = connection.execute(
                "SELECT duration_ms FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            duration_ms = project["duration_ms"]
            saved_ms = min(time_ms, int(duration_ms)) if duration_ms is not None else time_ms
            connection.execute(
                "UPDATE projects SET resume_time_ms = ? WHERE id = ?",
                (saved_ms, project_id),
            )
        return saved_ms

    def list_projects(self) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
            contexts = {
                row["project_id"]: row
                for row in connection.execute("SELECT * FROM translation_contexts").fetchall()
            }
        return [decode_project(row, contexts.get(row["id"])) for row in rows]

    def set_subtitle_style(self, project_id: str, style: dict[str, Any]) -> dict[str, Any]:
        normalized = merge_subtitle_style(style)
        with transaction(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE projects
                SET subtitle_style = ?, subtitle_revision = subtitle_revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(normalized), now_iso(), project_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(project_id)
        return normalized

    def default_subtitle_style(self) -> dict[str, Any]:
        saved = self._setting("default_subtitle_style")
        if not saved:
            return merge_subtitle_style(None)
        try:
            value = json.loads(saved)
        except json.JSONDecodeError:
            return merge_subtitle_style(None)
        return merge_subtitle_style(value)

    def default_bilingual(self) -> bool:
        saved = self._setting("default_bilingual")
        if saved is None:
            return True
        return saved == "1"

    def remember_subtitle_defaults(self, style: dict[str, Any], bilingual: bool) -> None:
        normalized = merge_subtitle_style(style)
        timestamp = now_iso()
        with transaction(self.database_path) as connection:
            for key, value in (
                ("default_subtitle_style", json.dumps(normalized, ensure_ascii=False)),
                ("default_bilingual", "1" if bilingual else "0"),
            ):
                connection.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, value, timestamp),
                )

    def _setting(self, key: str) -> str | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    def provider_usage(self, project_id: str) -> dict[str, int]:
        usage = {"codex": 0, "grok": 0}
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT provider, COUNT(*) AS request_count
                FROM (
                    SELECT provider
                    FROM translation_contexts
                    WHERE project_id = ?
                    UNION ALL
                    SELECT translation_provider AS provider
                    FROM parts
                    WHERE project_id = ? AND translation_provider IS NOT NULL
                )
                GROUP BY provider
                """,
                (project_id, project_id),
            ).fetchall()
        for row in rows:
            provider = "grok" if row["provider"] == "grokbuild" else row["provider"]
            if provider in usage:
                usage[provider] += int(row["request_count"])
        return usage

    def update_project(self, project_id: str, **fields: Any) -> None:
        context_fields = {
            key: fields.pop(key)
            for key in ("translation_context", "context_provider", "context_approved")
            if key in fields
        }
        allowed = {
            "youtube_id",
            "title",
            "display_name",
            "duration_ms",
            "status",
            "stage",
            "progress",
            "pipeline_detail",
            "error",
            "subtitle_source",
            "video_path",
            "output_path",
            "subtitle_revision",
            "rendered_revision",
            "bilingual",
            "translation_provider",
            "codex_model",
            "punctuation_enabled",
            "pending_rebuild_scope",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if "pending_rebuild_scope" in values and not isinstance(
            values["pending_rebuild_scope"], str
        ):
            values["pending_rebuild_scope"] = (
                json.dumps(values["pending_rebuild_scope"], ensure_ascii=False)
                if values["pending_rebuild_scope"] is not None
                else None
            )
        if "pipeline_detail" in values and not isinstance(values["pipeline_detail"], str):
            values["pipeline_detail"] = (
                json.dumps(values["pipeline_detail"], ensure_ascii=False)
                if values["pipeline_detail"] is not None
                else None
            )
        if not values and not context_fields:
            return
        with transaction(self.database_path) as connection:
            if values:
                values["updated_at"] = now_iso()
                assignments = ", ".join(f"{key} = ?" for key in values)
                connection.execute(
                    f"UPDATE projects SET {assignments} WHERE id = ?",
                    (*values.values(), project_id),
                )
            if context_fields:
                self._update_translation_context(connection, project_id, context_fields)

    @staticmethod
    def _update_translation_context(
        connection: sqlite3.Connection, project_id: str, fields: dict[str, Any]
    ) -> None:
        previous = connection.execute(
            "SELECT * FROM translation_contexts WHERE project_id = ?", (project_id,)
        ).fetchone()
        context = fields.get("translation_context")
        if isinstance(context, str):
            context = json.loads(context)
        if context is None and "translation_context" not in fields and previous is not None:
            context = {
                "overview": previous["overview"],
                "style": previous["style"],
                "terms": json.loads(previous["terms_json"]),
                "card_candidates": json.loads(previous["card_candidates_json"]),
            }
        if context is None:
            connection.execute(
                "DELETE FROM translation_contexts WHERE project_id = ?", (project_id,)
            )
            return
        timestamp = now_iso()
        connection.execute(
            """
            INSERT INTO translation_contexts (
                project_id, overview, style, terms_json, card_candidates_json,
                provider, approved, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                overview = excluded.overview, style = excluded.style,
                terms_json = excluded.terms_json,
                card_candidates_json = excluded.card_candidates_json,
                provider = excluded.provider,
                approved = excluded.approved, updated_at = excluded.updated_at
            """,
            (
                project_id, str(context["overview"]), str(context["style"]),
                json.dumps(context.get("terms", []), ensure_ascii=False),
                json.dumps(context.get("card_candidates", []), ensure_ascii=False),
                str(fields.get("context_provider") or (previous["provider"] if previous else "manual")),
                int(bool(fields.get("context_approved", previous["approved"] if previous else 0))),
                previous["created_at"] if previous else timestamp, timestamp,
            ),
        )

    def replace_segments(self, project_id: str, segments: Iterable[dict[str, Any]]) -> None:
        timestamp = now_iso()
        normalized_segments = []
        for segment in segments:
            source_text, target_text = normalize_segment_texts(
                str(segment.get("source_text", "")),
                str(segment.get("target_text", "")),
            )
            if source_text or target_text:
                normalized_segments.append((segment, source_text, target_text))
        with transaction(self.database_path) as connection:
            connection.execute("DELETE FROM segments WHERE project_id = ?", (project_id,))
            connection.executemany(
                """
                INSERT INTO segments (
                    project_id, segment_order, start_ms, end_ms, source_text,
                    target_text, start_token_id, end_token_id,
                    quality_flags, part_id, chinese_only, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        index,
                        int(segment["start_ms"]),
                        int(segment["end_ms"]),
                        source_text,
                        target_text,
                        segment.get("start_token_id"),
                        segment.get("end_token_id"),
                        json.dumps(segment.get("quality_flags", [])),
                        segment.get("part_id"),
                        int(bool(segment.get("chinese_only", False))),
                        segment.get("status", "machine_translated"),
                        timestamp,
                    )
                    for index, (segment, source_text, target_text) in enumerate(
                        normalized_segments, start=1
                    )
                ],
            )

    def list_segments(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? ORDER BY start_ms, segment_order",
                (project_id,),
            ).fetchall()
            segments = [self._decode_segment(row) for row in rows]
            components_by_segment: dict[int, list[dict[str, Any]]] = {}
            component_rows = connection.execute(
                """
                SELECT sc.segment_id, sc.component_order, sc.part_id, sc.cue_id, sc.subcue_id
                FROM segment_components sc
                JOIN segments s ON s.id = sc.segment_id
                WHERE s.project_id = ?
                ORDER BY s.segment_order, sc.component_order
                """,
                (project_id,),
            ).fetchall()
            for row in component_rows:
                components_by_segment.setdefault(int(row["segment_id"]), []).append({
                    "component_order": row["component_order"],
                    "part_id": row["part_id"],
                    "cue_id": row["cue_id"],
                    "subcue_id": row["subcue_id"],
                })
            for segment in segments:
                segment["components"] = components_by_segment.get(segment["id"], [])
        return segments

    def get_segment(self, project_id: str, segment_id: int) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND id = ?",
                (project_id, segment_id),
            ).fetchone()
            if row is None:
                return None
            segment = self._decode_segment(row)
            segment["components"] = self._segment_components(connection, segment_id)
        return segment

    def segment_component_token_ranges(self, project_id: str) -> dict[int, list[tuple[int, int]]]:
        """Original Cue/Sub-cue ranges for Segments without an exact Token range."""
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT sc.segment_id,
                       COALESCE(sub.start_token_id, cue.start_token_id) AS start_token_id,
                       COALESCE(sub.end_token_id, cue.end_token_id) AS end_token_id
                FROM segment_components sc
                JOIN segments s ON s.id = sc.segment_id
                JOIN cues cue ON cue.part_id = sc.part_id AND cue.cue_id = sc.cue_id
                LEFT JOIN subcues sub ON sub.part_id = sc.part_id
                    AND sub.cue_id = sc.cue_id AND sub.subcue_id = sc.subcue_id
                WHERE s.project_id = ?
                ORDER BY sc.segment_id, sc.component_order
                """,
                (project_id,),
            ).fetchall()
        ranges: dict[int, list[tuple[int, int]]] = {}
        for row in rows:
            ranges.setdefault(int(row["segment_id"]), []).append(
                (int(row["start_token_id"]), int(row["end_token_id"]))
            )
        return ranges

    @staticmethod
    def _segment_components(connection: sqlite3.Connection, segment_id: int) -> list[dict[str, Any]]:
        return [
            dict(row) for row in connection.execute(
                """SELECT component_order, part_id, cue_id, subcue_id
                   FROM segment_components WHERE segment_id = ? ORDER BY component_order""",
                (segment_id,),
            ).fetchall()
        ]

    @staticmethod
    def _decode_segment(row: sqlite3.Row) -> dict[str, Any]:
        segment = dict(row)
        segment["extra_line_gap"] = bool(segment.get("extra_line_gap", 0))
        segment["chinese_only"] = bool(segment.get("chinese_only", 0))
        try:
            segment["quality_flags"] = json.loads(segment.get("quality_flags") or "[]")
        except json.JSONDecodeError:
            segment["quality_flags"] = []
        return segment

    def replace_tokens(
        self,
        project_id: str,
        tokens: Iterable[dict[str, Any]],
    ) -> None:
        with transaction(self.database_path) as connection:
            connection.execute(
                "DELETE FROM part_punctuation WHERE part_id IN (SELECT id FROM parts WHERE project_id = ?)",
                (project_id,),
            )
            connection.execute("DELETE FROM tokens WHERE project_id = ?", (project_id,))
            connection.executemany(
                """
                INSERT INTO tokens (
                    project_id, token_id, raw_text, normalized_text, start_ms,
                    end_ms, source, confidence, timing_inferred
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        index,
                        str(token.get("raw_text", token["text"])),
                        str(token.get("normalized_text", token["text"])),
                        int(token["start_ms"]),
                        int(token["end_ms"]),
                        str(token.get("source", "unknown")),
                        token.get("confidence"),
                        int(bool(token.get("timing_inferred", False))),
                    )
                    for index, token in enumerate(tokens)
                ],
            )

    def list_tokens(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM tokens WHERE project_id = ? ORDER BY token_id",
                (project_id,),
            ).fetchall()
            punctuation_rows = connection.execute(
                """
                SELECT pp.part_id, pp.source_fingerprint,
                       sp.start_token_id, sp.end_token_id
                FROM part_punctuation pp
                JOIN parts sp ON sp.id = pp.part_id
                WHERE sp.project_id = ? AND pp.status = 'completed'
                ORDER BY sp.part_number
                """,
                (project_id,),
            ).fetchall()
            punctuation_marks = {
                int(row["part_id"]): [
                    {"after_token_id": int(mark["token_id"]), "mark": str(mark["mark"])}
                    for mark in connection.execute(
                        "SELECT token_id, mark FROM punctuation_marks WHERE part_id = ? ORDER BY token_id",
                        (row["part_id"],),
                    ).fetchall()
                ]
                for row in punctuation_rows
            }
        tokens = [dict(row) for row in rows]
        for token in tokens:
            token["text"] = token["normalized_text"]
            token["timing_inferred"] = bool(token["timing_inferred"])
        marks: list[dict[str, Any]] = []
        for row in punctuation_rows:
            part_tokens = tokens[int(row["start_token_id"]): int(row["end_token_id"]) + 1]
            if source_fingerprint(part_tokens) != row["source_fingerprint"]:
                raise RuntimeError(f"Punctuation source changed for Part ID {row['part_id']}")
            marks.extend(validate_punctuation_annotations(
                part_tokens, {"punctuation": punctuation_marks[int(row["part_id"])]}
            ))
        if marks:
            tokens = apply_punctuation(tokens, marks)
        return tokens

    def get_part_punctuation(self, part_id: int) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM part_punctuation WHERE part_id = ?", (part_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def begin_part_punctuation(
        self, project_id: str, part_id: int, fingerprint: str, provider: str
    ) -> None:
        with transaction(self.database_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM parts WHERE id = ? AND project_id = ?", (part_id, project_id)
            ).fetchone()
            if exists is None:
                raise KeyError(part_id)
            connection.execute(
                """
                INSERT INTO part_punctuation (
                    part_id, source_fingerprint, status,
                    provider, error, updated_at
                ) VALUES (?, ?, 'requested', ?, NULL, ?)
                """,
                (part_id, fingerprint, provider, now_iso()),
            )

    def complete_part_punctuation(self, part_id: int, marks: list[dict[str, Any]]) -> None:
        with transaction(self.database_path) as connection:
            part = connection.execute(
                """
                SELECT sp.project_id, pp.source_fingerprint, sp.start_token_id, sp.end_token_id
                FROM part_punctuation pp JOIN parts sp ON sp.id = pp.part_id
                WHERE pp.part_id = ? AND pp.status = 'requested'
                """,
                (part_id,),
            ).fetchone()
            if part is None:
                raise RuntimeError(f"Punctuation request for Part ID {part_id} is not active")
            rows = connection.execute(
                """
                SELECT * FROM tokens
                WHERE project_id = ? AND token_id BETWEEN ? AND ? ORDER BY token_id
                """,
                (part["project_id"], part["start_token_id"], part["end_token_id"]),
            ).fetchall()
            tokens = [{**dict(row), "text": row["normalized_text"]} for row in rows]
            if source_fingerprint(tokens) != part["source_fingerprint"]:
                raise RuntimeError(f"Punctuation source changed for Part ID {part_id}")
            validated = validate_punctuation_annotations(tokens, {"punctuation": marks})
            changed = connection.execute(
                """
                UPDATE part_punctuation
                SET status = 'completed', error = NULL, updated_at = ?
                WHERE part_id = ? AND status = 'requested'
                """,
                (now_iso(), part_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"Punctuation request for Part ID {part_id} is not active")
            connection.executemany(
                "INSERT INTO punctuation_marks (part_id, token_id, mark) VALUES (?, ?, ?)",
                [(part_id, row["after_token_id"], row["mark"]) for row in validated],
            )

    def fail_part_punctuation(self, part_id: int, error: str) -> None:
        with transaction(self.database_path) as connection:
            connection.execute(
                """
                UPDATE part_punctuation
                SET status = 'failed', error = ?, updated_at = ?
                WHERE part_id = ? AND status = 'requested'
                """,
                (error, now_iso(), part_id),
            )

    def replace_cues(
        self,
        project_id: str,
        part_id: int,
        cues: Iterable[dict[str, Any]],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        timestamp = now_iso()
        rows = list(cues)
        with (transaction(self.database_path) if connection is None else nullcontext(connection)) as db:
            db.execute("DELETE FROM cues WHERE part_id = ?", (part_id,))
            db.executemany(
                """
                INSERT INTO cues (
                    project_id, part_id, cue_id, start_token_id, end_token_id,
                    start_ms, end_ms, source_text, target_text, hidden_token_ids,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        part_id,
                        int(cue["cue_id"]),
                        int(cue["start_token_id"]),
                        int(cue["end_token_id"]),
                        int(cue["start_ms"]),
                        int(cue["end_ms"]),
                        str(cue["source_text"]),
                        str(cue.get("target_text", "")),
                        json.dumps(cue.get("hidden_token_ids", [])),
                        timestamp,
                        timestamp,
                    )
                    for cue in rows
                ],
            )

    def list_cues(self, part_id: int) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM cues WHERE part_id = ? ORDER BY cue_id",
                (part_id,),
            ).fetchall()
        result = []
        for row in rows:
            cue = dict(row)
            try:
                cue["hidden_token_ids"] = json.loads(
                    cue.get("hidden_token_ids") or "[]"
                )
            except json.JSONDecodeError:
                cue["hidden_token_ids"] = []
            result.append(cue)
        return result

    def clear_subtitle_data(
        self, project_id: str, *, preserve_translation_context: bool = False
    ) -> None:
        with transaction(self.database_path) as connection:
            connection.execute("DELETE FROM segments WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM parts WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM tokens WHERE project_id = ?", (project_id,))
            connection.execute(
                "DELETE FROM jobs WHERE project_id = ? AND status IN ('completed', 'failed')",
                (project_id,),
            )
            if not preserve_translation_context:
                connection.execute(
                    "DELETE FROM translation_contexts WHERE project_id = ?", (project_id,)
                )
            connection.execute(
                """
                UPDATE projects
                SET status = 'queued', stage = 'queued', progress = 0, error = NULL,
                    pipeline_detail = NULL,
                    punctuation_enabled = 1,
                    subtitle_revision = 0, rendered_revision = NULL, output_path = NULL,
                    pending_rebuild_scope = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_iso(), project_id),
            )

    def begin_part_rebuild(self, project_id: str, scope: dict[str, Any]) -> None:
        """Queue a Part rebuild without deleting the currently visible subtitles or Pass 0."""
        with transaction(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE projects
                SET status = 'queued', stage = 'part_rebuild_queued', progress = 0,
                    error = NULL, pipeline_detail = NULL,
                    pending_rebuild_scope = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(scope, ensure_ascii=False), now_iso(), project_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(project_id)

    def commit_part_rebuild(
        self,
        project_id: str,
        expected_revision: int,
        part_results: list[dict[str, Any]],
    ) -> None:
        """Atomically publish staged Part results after a selected-Part rebuild."""
        timestamp = now_iso()
        with transaction(self.database_path) as connection:
            project = connection.execute(
                "SELECT subtitle_revision FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            if int(project["subtitle_revision"]) != expected_revision:
                raise RuntimeError(
                    "Subtitles changed while the selected-Part rebuild was running; "
                    "the staged result was not published"
                )

            for result in part_results:
                part_id = int(result["part_id"])
                connection.execute("DELETE FROM segments WHERE part_id = ?", (part_id,))
                rows = []
                for segment in result["segments"]:
                    source_text, target_text = normalize_segment_texts(
                        str(segment.get("source_text", "")),
                        str(segment.get("target_text", "")),
                    )
                    if not source_text and not target_text:
                        continue
                    rows.append((segment, source_text, target_text))

                cues = list(result.get("cues", []))
                if cues:
                    connection.execute(
                        "DELETE FROM cues WHERE part_id = ?", (part_id,)
                    )
                    connection.executemany(
                        """
                        INSERT INTO cues (
                            project_id, part_id, cue_id, start_token_id,
                            end_token_id, start_ms, end_ms, source_text,
                            target_text, hidden_token_ids, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                project_id,
                                part_id,
                                int(cue["cue_id"]),
                                int(cue["start_token_id"]),
                                int(cue["end_token_id"]),
                                int(cue["start_ms"]),
                                int(cue["end_ms"]),
                                str(cue["source_text"]),
                                str(cue.get("target_text", "")),
                                json.dumps(cue.get("hidden_token_ids", [])),
                                timestamp,
                                timestamp,
                            )
                            for cue in cues
                        ],
                    )

                self._replace_subcues(connection, part_id, result.get("subcues", []))
                for segment, _, _ in rows:
                    if segment.get("pass3_error"):
                        connection.execute(
                            """
                            UPDATE cues SET split_status = 'failed', split_error = ?, updated_at = ?
                            WHERE part_id = ? AND cue_id = ?
                            """,
                            (str(segment["pass3_error"]), timestamp, part_id, segment["cue_id"]),
                        )
                next_segment_order = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(segment_order), 0) + 1 FROM segments WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()[0]
                )
                connection.executemany(
                    """
                    INSERT INTO segments (
                        project_id, segment_order, start_ms, end_ms, source_text,
                        target_text, start_token_id, end_token_id,
                        quality_flags, part_id, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            project_id,
                            next_segment_order + index,
                            int(segment["start_ms"]),
                            int(segment["end_ms"]),
                            source_text,
                            target_text,
                            int(segment["start_token_id"]),
                            int(segment["end_token_id"]),
                            json.dumps(segment.get("quality_flags", [])),
                            part_id,
                            segment.get("status", "machine_translated"),
                            timestamp,
                        )
                        for index, (segment, source_text, target_text) in enumerate(rows)
                    ],
                )
                self._link_segment_components(connection, part_id)

                connection.execute(
                    """
                    UPDATE parts
                    SET status = 'completed', source_text = ?, target_text = ?,
                        translation_provider = ?, error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(result["source_text"]),
                        str(result["target_text"]),
                        str(result["translation_provider"]),
                        timestamp,
                        part_id,
                    ),
                )

            ordered = connection.execute(
                "SELECT id FROM segments WHERE project_id = ? ORDER BY start_ms, id",
                (project_id,),
            ).fetchall()
            connection.execute(
                "UPDATE segments SET segment_order = segment_order + 1000000 WHERE project_id = ?",
                (project_id,),
            )
            connection.executemany(
                "UPDATE segments SET segment_order = ? WHERE id = ?",
                [(index, row["id"]) for index, row in enumerate(ordered, start=1)],
            )
            connection.execute(
                """
                UPDATE projects
                SET status = 'waiting_review', stage = 'waiting_review', progress = 100,
                    error = NULL, pipeline_detail = NULL, pending_rebuild_scope = NULL,
                    subtitle_revision = subtitle_revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, project_id),
            )

    def update_segment(self, project_id: str, segment_id: int, **fields: Any) -> dict[str, Any]:
        allowed = {"start_ms", "end_ms", "source_text", "target_text", "status", "extra_line_gap", "chinese_only"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if "extra_line_gap" in values:
            values["extra_line_gap"] = int(bool(values["extra_line_gap"]))
        if "chinese_only" in values:
            values["chinese_only"] = int(bool(values["chinese_only"]))
        if "source_text" in values:
            values["source_text"] = normalize_segment_texts(str(values["source_text"]), "")[0]
        if "target_text" in values:
            values["target_text"] = normalize_manual_target_text(str(values["target_text"]))
        if values:
            values["updated_at"] = now_iso()
            assignments = ", ".join(f"{key} = ?" for key in values)
            with transaction(self.database_path) as connection:
                cursor = connection.execute(
                    f"UPDATE segments SET {assignments} WHERE project_id = ? AND id = ?",
                    (*values.values(), project_id, segment_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(segment_id)
                connection.execute(
                    """
                    UPDATE projects
                    SET subtitle_revision = subtitle_revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now_iso(), project_id),
                )
        segment = self.get_segment(project_id, segment_id)
        if segment is None:
            raise KeyError(segment_id)
        return segment

    def update_segment_boundary(
        self,
        project_id: str,
        segment_id: int,
        boundary: str,
        *,
        value_ms: int | None = None,
        delta_ms: int | None = None,
        sync_neighbor: bool = True,
    ) -> list[dict[str, Any]]:
        if boundary not in {"start", "end"}:
            raise ValueError("Boundary must be start or end")
        if (value_ms is None) == (delta_ms is None):
            raise ValueError("Provide exactly one timing value or delta")
        timestamp = now_iso()
        affected_ids: list[int] = []
        with transaction(self.database_path) as connection:
            current = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND id = ?",
                (project_id, segment_id),
            ).fetchone()
            if current is None:
                raise KeyError(segment_id)
            previous = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND segment_order = ?",
                (project_id, int(current["segment_order"]) - 1),
            ).fetchone()
            following = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND segment_order = ?",
                (project_id, int(current["segment_order"]) + 1),
            ).fetchone()
            original = int(current["start_ms"] if boundary == "start" else current["end_ms"])
            updated = int(value_ms if value_ms is not None else original + int(delta_ms or 0))

            if boundary == "start":
                if updated < 0:
                    raise ValueError("Start time cannot be negative")
                if updated >= int(current["end_ms"]):
                    raise ValueError("Start time must be earlier than this subtitle's end time")
                if previous is not None and updated <= int(previous["start_ms"]):
                    raise ValueError("Start time must be later than the previous subtitle's start time")
                connection.execute(
                    "UPDATE segments SET start_ms = ?, status = 'edited', updated_at = ? WHERE id = ?",
                    (updated, timestamp, current["id"]),
                )
                affected_ids.append(int(current["id"]))
                if previous is not None and sync_neighbor:
                    connection.execute(
                        "UPDATE segments SET end_ms = ?, status = 'edited', updated_at = ? WHERE id = ?",
                        (updated, timestamp, previous["id"]),
                    )
                    affected_ids.append(int(previous["id"]))
            else:
                if updated <= int(current["start_ms"]):
                    raise ValueError("End time must be later than this subtitle's start time")
                if following is not None and updated >= int(following["end_ms"]):
                    raise ValueError("End time must be earlier than the next subtitle's end time")
                connection.execute(
                    "UPDATE segments SET end_ms = ?, status = 'edited', updated_at = ? WHERE id = ?",
                    (updated, timestamp, current["id"]),
                )
                affected_ids.append(int(current["id"]))
                if following is not None and sync_neighbor:
                    connection.execute(
                        "UPDATE segments SET start_ms = ?, status = 'edited', updated_at = ? WHERE id = ?",
                        (updated, timestamp, following["id"]),
                    )
                    affected_ids.append(int(following["id"]))

            connection.execute(
                """
                UPDATE projects
                SET subtitle_revision = subtitle_revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, project_id),
            )
            rows = connection.execute(
                f"SELECT * FROM segments WHERE id IN ({','.join('?' for _ in affected_ids)}) "
                "ORDER BY segment_order",
                affected_ids,
            ).fetchall()
        return [self._decode_segment(row) for row in rows]

    def split_segment(self, project_id: str, segment_id: int) -> int:
        with transaction(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND id = ?",
                (project_id, segment_id),
            ).fetchone()
            if row is None:
                raise KeyError(segment_id)
            split_ms = (int(row["start_ms"]) + int(row["end_ms"])) // 2
            if split_ms - int(row["start_ms"]) < 100 or int(row["end_ms"]) - split_ms < 100:
                raise ValueError("The subtitle must be at least 200 ms long to split")
            components = self._segment_components(connection, segment_id)
            part_ids = {int(item["part_id"]) for item in components}
            if row["part_id"] is not None:
                part_ids.add(int(row["part_id"]))
            if not components or len(part_ids) != 1:
                raise ValueError("This subtitle needs a single Part and Cue provenance to split")
            part_id = part_ids.pop()
            # Shift through a temporary range to avoid the UNIQUE(project_id, segment_order)
            # constraint being checked row-by-row by SQLite.
            connection.execute(
                "UPDATE segments SET segment_order = segment_order + 1000000 WHERE project_id = ? AND segment_order > ?",
                (project_id, row["segment_order"]),
            )
            connection.execute(
                "UPDATE segments SET segment_order = segment_order - 999999 WHERE project_id = ? AND segment_order >= 1000000",
                (project_id,),
            )
            connection.execute(
                """
                UPDATE segments SET end_ms = ?, start_token_id = NULL, end_token_id = NULL,
                    part_id = ?, status = 'edited', updated_at = ? WHERE id = ?
                """,
                (split_ms, part_id, now_iso(), segment_id),
            )
            inserted = connection.execute(
                """
                INSERT INTO segments (
                    project_id, segment_order, start_ms, end_ms, source_text,
                    target_text, part_id, extra_line_gap, chinese_only, status, updated_at
                ) VALUES (?, ?, ?, ?, '', '', ?, ?, ?, 'edited', ?)
                """,
                (
                    project_id,
                    row["segment_order"] + 1,
                    split_ms,
                    row["end_ms"],
                    part_id,
                    row["extra_line_gap"],
                    row["chinese_only"],
                    now_iso(),
                ),
            )
            connection.execute(
                """
                INSERT INTO segment_components (segment_id, component_order, part_id, cue_id, subcue_id)
                SELECT ?, component_order, part_id, cue_id, subcue_id
                FROM segment_components WHERE segment_id = ?
                """,
                (inserted.lastrowid, segment_id),
            )
            connection.execute(
                "UPDATE projects SET subtitle_revision = subtitle_revision + 1, updated_at = ? WHERE id = ?",
                (now_iso(), project_id),
            )
            return int(inserted.lastrowid)

    def merge_with_next(self, project_id: str, segment_id: int) -> None:
        with transaction(self.database_path) as connection:
            current = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND id = ?",
                (project_id, segment_id),
            ).fetchone()
            if current is None:
                raise KeyError(segment_id)
            following = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND segment_order = ?",
                (project_id, current["segment_order"] + 1),
            ).fetchone()
            if following is None:
                raise ValueError("This is already the final subtitle")
            current_components = self._segment_components(connection, int(current["id"]))
            following_components = self._segment_components(connection, int(following["id"]))
            current_parts = {int(item["part_id"]) for item in current_components}
            following_parts = {int(item["part_id"]) for item in following_components}
            if current["part_id"] is not None:
                current_parts.add(int(current["part_id"]))
            if following["part_id"] is not None:
                following_parts.add(int(following["part_id"]))
            if (current_parts or following_parts) and (
                len(current_parts) != 1 or current_parts != following_parts
            ):
                raise ValueError("Subtitles from different Parts cannot be merged")
            if bool(current["chinese_only"]) != bool(following["chinese_only"]):
                raise ValueError("Subtitles with different English display settings cannot be merged")
            merged_end_token_id = (
                following["end_token_id"]
                if following["end_token_id"] is not None
                else current["end_token_id"]
            )
            connection.execute(
                """
                UPDATE segments SET end_ms = ?, end_token_id = ?,
                    source_text = ?, target_text = ?,
                    extra_line_gap = ?, status = 'edited', updated_at = ? WHERE id = ?
                """,
                (
                    following["end_ms"],
                    merged_end_token_id,
                    normalize_segment_texts(
                        f"{current['source_text']} {following['source_text']}",
                        f"{current['target_text']}{following['target_text']}",
                    )[0],
                    normalize_manual_target_text(
                        f"{current['target_text']}{following['target_text']}"
                    ),
                    int(bool(current["extra_line_gap"] or following["extra_line_gap"])),
                    now_iso(),
                    current["id"],
                ),
            )
            next_order = len(current_components) + 1
            existing_keys = {
                (item["part_id"], item["cue_id"], item["subcue_id"])
                for item in current_components
            }
            for component in following_components:
                key = (component["part_id"], component["cue_id"], component["subcue_id"])
                if key in existing_keys:
                    continue
                connection.execute(
                    """
                    INSERT INTO segment_components
                        (segment_id, component_order, part_id, cue_id, subcue_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (current["id"], next_order, *key),
                )
                existing_keys.add(key)
                next_order += 1
            connection.execute("DELETE FROM segments WHERE id = ?", (following["id"],))
            connection.execute(
                "UPDATE segments SET segment_order = segment_order + 1000000 WHERE project_id = ? AND segment_order > ?",
                (project_id, following["segment_order"]),
            )
            connection.execute(
                "UPDATE segments SET segment_order = segment_order - 1000001 WHERE project_id = ? AND segment_order >= 1000000",
                (project_id,),
            )
            connection.execute(
                "UPDATE projects SET subtitle_revision = subtitle_revision + 1, updated_at = ? WHERE id = ?",
                (now_iso(), project_id),
            )

    def initialize_subtitle_plan(self, project_id: str, parts: list[dict[str, Any]]) -> None:
        timestamp = now_iso()
        with transaction(self.database_path) as connection:
            connection.execute("DELETE FROM segments WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM parts WHERE project_id = ?", (project_id,))
            for part_number, part in enumerate(parts, start=1):
                connection.execute(
                    """
                    INSERT INTO parts (
                        project_id, part_number, start_token_id, end_token_id,
                        start_ms, end_ms, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        project_id,
                        part_number,
                        part["start_token_id"],
                        part["end_token_id"],
                        part["start_ms"],
                        part["end_ms"],
                        timestamp,
                        timestamp,
                    ),
                )

    def list_parts(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM parts
                WHERE project_id = ?
                ORDER BY part_number
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_part(self, part_id: int, **fields: Any) -> None:
        allowed = {
            "status",
            "source_text",
            "target_text",
            "translation_provider",
            "error",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with transaction(self.database_path) as connection:
            connection.execute(
                f"UPDATE parts SET {assignments} WHERE id = ?",
                (*values.values(), part_id),
            )

    def replace_part_segments(
        self,
        project_id: str,
        part_id: int,
        segments: Iterable[dict[str, Any]],
        *,
        subcues: Iterable[dict[str, Any]] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        timestamp = now_iso()
        rows = []
        for segment in segments:
            source_text, target_text = normalize_segment_texts(
                str(segment.get("source_text", "")),
                str(segment.get("target_text", "")),
            )
            if not source_text and not target_text:
                continue
            rows.append((segment, source_text, target_text))

        with (transaction(self.database_path) if connection is None else nullcontext(connection)) as db:
            db.execute("DELETE FROM segments WHERE part_id = ?", (part_id,))
            if subcues is not None:
                self._replace_subcues(db, part_id, subcues)
                for segment, _, _ in rows:
                    if segment.get("pass3_error"):
                        db.execute(
                            """
                            UPDATE cues SET split_status = 'failed', split_error = ?, updated_at = ?
                            WHERE part_id = ? AND cue_id = ?
                            """,
                            (str(segment["pass3_error"]), timestamp, part_id, segment["cue_id"]),
                        )
            next_segment_order = int(
                db.execute(
                    "SELECT COALESCE(MAX(segment_order), 0) + 1 FROM segments WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0]
            )
            db.executemany(
                """
                INSERT INTO segments (
                    project_id, segment_order, start_ms, end_ms, source_text,
                    target_text, start_token_id, end_token_id,
                    quality_flags, part_id, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        next_segment_order + index,
                        int(segment["start_ms"]),
                        int(segment["end_ms"]),
                        source_text,
                        target_text,
                        int(segment["start_token_id"]),
                        int(segment["end_token_id"]),
                        json.dumps(segment.get("quality_flags", [])),
                        part_id,
                        segment.get("status", "machine_translated"),
                        timestamp,
                    )
                    for index, (segment, source_text, target_text) in enumerate(rows)
                ],
            )
            ordered = db.execute(
                "SELECT id FROM segments WHERE project_id = ? ORDER BY start_ms, id",
                (project_id,),
            ).fetchall()
            db.execute(
                "UPDATE segments SET segment_order = segment_order + 1000000 WHERE project_id = ?",
                (project_id,),
            )
            db.executemany(
                "UPDATE segments SET segment_order = ? WHERE id = ?",
                [(index, row["id"]) for index, row in enumerate(ordered, start=1)],
            )
            self._link_segment_components(db, part_id)

    def _replace_subcues(
        self, db: sqlite3.Connection, part_id: int, subcues: Iterable[dict[str, Any]]
    ) -> None:
        db.execute("DELETE FROM subcues WHERE part_id = ?", (part_id,))
        db.execute(
            "UPDATE cues SET split_status = 'not_needed', split_error = NULL WHERE part_id = ?",
            (part_id,),
        )
        for subcue in subcues:
            cue_id = int(subcue["cue_id"])
            db.execute(
                """
                INSERT INTO subcues (
                    part_id, cue_id, subcue_id, start_token_id, end_token_id,
                    start_ms, end_ms, source_text, target_text, split_pass,
                    boundary_source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    part_id, cue_id, int(subcue["subcue_id"]),
                    int(subcue["start_token_id"]), int(subcue["end_token_id"]),
                    int(subcue["start_ms"]), int(subcue["end_ms"]),
                    str(subcue["source_text"]), str(subcue["target_text"]),
                    str(subcue.get("split_pass", "pass3_2")),
                    str(subcue.get("boundary_source", "model")), now_iso(),
                ),
            )
            db.execute(
                "UPDATE cues SET split_status = 'completed', split_error = NULL WHERE part_id = ? AND cue_id = ?",
                (part_id, cue_id),
            )

    def list_subcues(self, part_id: int) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM subcues WHERE part_id = ? ORDER BY cue_id, subcue_id",
                (part_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _link_segment_components(db: sqlite3.Connection, part_id: int) -> None:
        cues = db.execute(
            "SELECT cue_id, start_token_id, end_token_id FROM cues WHERE part_id = ? ORDER BY cue_id",
            (part_id,),
        ).fetchall()
        subcues = db.execute(
            "SELECT cue_id, subcue_id, start_token_id, end_token_id FROM subcues WHERE part_id = ? ORDER BY cue_id, subcue_id",
            (part_id,),
        ).fetchall()
        segments = db.execute(
            "SELECT id, start_token_id, end_token_id FROM segments WHERE part_id = ? ORDER BY segment_order",
            (part_id,),
        ).fetchall()
        for segment in segments:
            start, end = segment["start_token_id"], segment["end_token_id"]
            if start is None or end is None:
                continue
            components: list[tuple[int, int | None]] = []
            for cue in cues:
                if int(cue["end_token_id"]) < start or int(cue["start_token_id"]) > end:
                    continue
                matches = [
                    subcue for subcue in subcues
                    if int(subcue["cue_id"]) == int(cue["cue_id"])
                    and int(subcue["end_token_id"]) >= start
                    and int(subcue["start_token_id"]) <= end
                ]
                if matches:
                    components.extend(
                        (int(cue["cue_id"]), int(subcue["subcue_id"]))
                        for subcue in matches
                    )
                else:
                    components.append((int(cue["cue_id"]), None))
            db.executemany(
                """
                INSERT INTO segment_components (segment_id, component_order, part_id, cue_id, subcue_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (int(segment["id"]), index, part_id, cue_id, subcue_id)
                    for index, (cue_id, subcue_id) in enumerate(components, start=1)
                ],
            )

    def commit_pass2(
        self,
        project_id: str,
        part_id: int,
        cues: Iterable[dict[str, Any]],
    ) -> None:
        """Checkpoint Pass 2 on Cues; Segments are published by Pass 3."""
        with transaction(self.database_path) as connection:
            self.replace_cues(project_id, part_id, cues, connection=connection)
            changed = connection.execute(
                """
                UPDATE parts
                SET status = 'pass2_complete', error = NULL, updated_at = ?
                WHERE project_id = ? AND id = ?
                """,
                (now_iso(), project_id, part_id),
            ).rowcount
            if changed != 1:
                raise KeyError(part_id)

    def enqueue_job(
        self, project_id: str, kind: str, *, return_status: str | None = None
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        with transaction(self.database_path) as connection:
            active = connection.execute(
                """
                SELECT id FROM jobs
                WHERE project_id = ? AND kind = ? AND status IN ('queued', 'running')
                """,
                (project_id, kind),
            ).fetchone()
            if active:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id = ?", (active["id"],)
                ).fetchone()
                return dict(row)
            connection.execute(
                """
                INSERT INTO jobs (id, project_id, kind, status, return_status, created_at)
                VALUES (?, ?, ?, 'queued', ?, ?)
                """,
                (job_id, project_id, kind, return_status, now_iso()),
            )
        return self.get_job(job_id)

    def has_active_job(self, project_id: str, kind: str) -> bool:
        with connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT 1 FROM jobs WHERE project_id = ? AND kind = ?
                   AND status IN ('queued', 'running') LIMIT 1""",
                (project_id, kind),
            ).fetchone()
        return row is not None

    def get_job(self, job_id: str) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def render_cancel_requested(self, project_id: str) -> bool:
        with connect(self.database_path) as connection:
            row = connection.execute(
                """SELECT cancel_requested FROM jobs
                   WHERE project_id = ? AND kind = 'render' AND status = 'running'
                   ORDER BY started_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def publish_render(self, project_id: str, output_path: str) -> bool:
        """Publish project state only if its active render was not cancelled."""
        with transaction(self.database_path) as connection:
            job = connection.execute(
                """SELECT cancel_requested FROM jobs WHERE project_id = ?
                   AND kind = 'render' AND status = 'running'
                   ORDER BY started_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            if job is None:
                raise RuntimeError("The active render job was not found")
            if job["cancel_requested"]:
                return False
            changed = connection.execute(
                """UPDATE projects SET status = 'completed', stage = 'completed',
                   progress = 100, pipeline_detail = NULL, output_path = ?,
                   rendered_revision = subtitle_revision, error = NULL, updated_at = ?
                   WHERE id = ?""",
                (output_path, now_iso(), project_id),
            ).rowcount
            if changed != 1:
                raise KeyError(project_id)
        return True

    def request_render_cancel(self, project_id: str) -> str:
        """Cancel a queued render now, or signal its running worker to stop."""
        with transaction(self.database_path) as connection:
            job = connection.execute(
                """SELECT * FROM jobs WHERE project_id = ? AND kind = 'render'
                   AND status IN ('queued', 'running')
                   ORDER BY created_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            if job is None:
                raise ValueError("No active render to cancel")
            project = connection.execute(
                "SELECT stage FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None or project["stage"] not in {
                "render_queued", "rendering", "render_cancelling"
            }:
                raise ValueError("The render has already finished")
            if job["status"] == "queued":
                self._cancel_render_job(connection, job)
                return "cancelled"
            connection.execute(
                "UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job["id"],)
            )
            connection.execute(
                """UPDATE projects SET stage = 'render_cancelling',
                   pipeline_detail = ?, updated_at = ? WHERE id = ?
                   AND stage IN ('render_queued', 'rendering', 'render_cancelling')""",
                ('{"label": "Cancelling render"}', now_iso(), project_id),
            )
            return "cancelling"

    def cancel_render_job(self, job_id: str) -> None:
        with transaction(self.database_path) as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["kind"] != "render" or job["status"] not in {"queued", "running"}:
                raise ValueError("Not an active render job")
            self._cancel_render_job(connection, job)

    @staticmethod
    def _cancel_render_job(connection: sqlite3.Connection, job: sqlite3.Row) -> None:
        project = connection.execute(
            "SELECT output_path FROM projects WHERE id = ?", (job["project_id"],)
        ).fetchone()
        if project is None:
            raise KeyError(job["project_id"])
        return_status = job["return_status"]
        if return_status not in {"waiting_review", "completed"}:
            return_status = "completed" if project["output_path"] else "waiting_review"
        timestamp = now_iso()
        connection.execute(
            """UPDATE jobs SET status = 'cancelled', cancel_requested = 1,
               finished_at = ?, error = NULL WHERE id = ?""",
            (timestamp, job["id"]),
        )
        connection.execute(
            """UPDATE projects SET status = ?, stage = ?, progress = 100,
               pipeline_detail = NULL, error = NULL, updated_at = ? WHERE id = ?""",
            (return_status, return_status, timestamp, job["project_id"]),
        )

    def claim_next_job(self) -> dict[str, Any] | None:
        with transaction(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            changed = connection.execute(
                """
                UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now_iso(), row["id"]),
            ).rowcount
            if changed == 0:
                return None
            claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            return dict(claimed)

    def complete_job(self, job_id: str) -> None:
        with transaction(self.database_path) as connection:
            connection.execute(
                "UPDATE jobs SET status = 'completed', finished_at = ?, error = NULL WHERE id = ?",
                (now_iso(), job_id),
            )

    def fail_job(self, job_id: str, error: str) -> None:
        with transaction(self.database_path) as connection:
            connection.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (now_iso(), error, job_id),
            )

    def fail_interactive_job(self, job_id: str, error: str) -> None:
        """Finish a render or selected-Part test without failing the project."""
        with transaction(self.database_path) as connection:
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["kind"] not in {"render", "part_rebuild"}:
                raise ValueError(f"Not an interactive job: {job['kind']}")
            self._fail_interactive_job(connection, job, error)

    @staticmethod
    def _fail_interactive_job(
        connection: sqlite3.Connection, job: sqlite3.Row, error: str
    ) -> None:
        project = connection.execute(
            "SELECT output_path FROM projects WHERE id = ?", (job["project_id"],)
        ).fetchone()
        if project is None:
            raise KeyError(job["project_id"])
        return_status = job["return_status"]
        if return_status not in {"waiting_review", "completed"}:
            return_status = "completed" if project["output_path"] else "waiting_review"
        timestamp = now_iso()
        connection.execute(
            """
            UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?
            """,
            (timestamp, error, job["id"]),
        )
        connection.execute(
            """
            UPDATE projects
            SET status = ?, stage = ?, progress = 100, pipeline_detail = NULL,
                pending_rebuild_scope = CASE WHEN ? = 'part_rebuild'
                    THEN NULL ELSE pending_rebuild_scope END,
                error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                return_status,
                return_status,
                job["kind"],
                error,
                timestamp,
                job["project_id"],
            ),
        )

    def recover_running_jobs(self) -> int:
        with transaction(self.database_path) as connection:
            interactive = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'running' AND kind IN ('render', 'part_rebuild')
                """
            ).fetchall()
            for job in interactive:
                project = connection.execute(
                    """
                    SELECT status, stage, output_path, rendered_revision,
                           pending_rebuild_scope
                    FROM projects WHERE id = ?
                    """,
                    (job["project_id"],),
                ).fetchone()
                render_published = (
                    job["kind"] == "render"
                    and project is not None
                    and project["status"] == "completed"
                    and project["stage"] == "completed"
                    and project["output_path"]
                    and project["rendered_revision"] is not None
                )
                part_published = (
                    job["kind"] == "part_rebuild"
                    and project is not None
                    and project["status"] == "waiting_review"
                    and project["pending_rebuild_scope"] is None
                )
                if render_published or part_published:
                    connection.execute(
                        """
                        UPDATE jobs SET status = 'completed', finished_at = ?, error = NULL
                        WHERE id = ?
                        """,
                        (now_iso(), job["id"]),
                    )
                elif job["kind"] == "render" and job["cancel_requested"]:
                    self._cancel_render_job(connection, job)
                else:
                    self._fail_interactive_job(
                        connection, job, "Interrupted while the worker was stopped"
                    )
            cursor = connection.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
            )
            return len(interactive) + cursor.rowcount
