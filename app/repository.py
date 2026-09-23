from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.db import connect, transaction
from app.services.subtitle_style import merge_subtitle_style
from app.services.subtitle_text import normalize_segment_texts

TERMINAL_JOB_STATUSES = {"completed", "failed"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def decode_project(row: sqlite3.Row) -> dict[str, Any]:
    project = dict(row)
    project["bilingual"] = bool(project["bilingual"])
    project["global_context_approved"] = bool(project["global_context_approved"])
    try:
        saved_style = json.loads(project.get("subtitle_style") or "null")
    except json.JSONDecodeError:
        saved_style = None
    project["subtitle_style"] = merge_subtitle_style(saved_style)
    try:
        global_context = json.loads(project.get("global_translation_context") or "null")
    except json.JSONDecodeError:
        global_context = None
    project["global_translation_context"] = (
        global_context if isinstance(global_context, dict) else None
    )
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
                    codex_model, global_context_approved,
                    status, stage, progress, subtitle_style, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'queued', 'queued', 0, ?, ?, ?)
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
        return decode_project(row) if row is not None else None

    def list_projects(self) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [decode_project(row) for row in rows]

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
                    SELECT global_context_provider AS provider
                    FROM projects
                    WHERE id = ? AND global_context_provider IS NOT NULL
                    UNION ALL
                    SELECT translation_provider AS provider
                    FROM subtitle_parts
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
            "global_translation_context",
            "global_context_provider",
            "global_context_approved",
            "pending_rebuild_scope",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if "global_translation_context" in values and not isinstance(
            values["global_translation_context"], str
        ):
            values["global_translation_context"] = json.dumps(
                values["global_translation_context"], ensure_ascii=False
            )
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
        if not values:
            return
        values["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with transaction(self.database_path) as connection:
            connection.execute(
                f"UPDATE projects SET {assignments} WHERE id = ?",
                (*values.values(), project_id),
            )

    def replace_segments(self, project_id: str, segments: Iterable[dict[str, Any]]) -> None:
        timestamp = now_iso()
        normalized_segments = []
        for segment in segments:
            source_text, translated_text = normalize_segment_texts(
                str(segment.get("source_text", "")),
                str(segment.get("translated_text", "")),
            )
            if source_text or translated_text:
                normalized_segments.append((segment, source_text, translated_text))
        with transaction(self.database_path) as connection:
            connection.execute("DELETE FROM segments WHERE project_id = ?", (project_id,))
            connection.executemany(
                """
                INSERT INTO segments (
                    project_id, sequence, start_ms, end_ms, source_text,
                    translated_text, start_token_id, end_token_id, source_sentence_id,
                    sub_id, quality_flags, part_id, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        index,
                        int(segment["start_ms"]),
                        int(segment["end_ms"]),
                        source_text,
                        translated_text,
                        segment.get("start_token_id"),
                        segment.get("end_token_id"),
                        segment.get("source_sentence_id"),
                        segment.get("sub_id"),
                        json.dumps(segment.get("quality_flags", [])),
                        segment.get("part_id"),
                        segment.get("status", "machine_translated"),
                        timestamp,
                    )
                    for index, (segment, source_text, translated_text) in enumerate(
                        normalized_segments
                    )
                ],
            )

    def list_segments(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? ORDER BY start_ms, sequence",
                (project_id,),
            ).fetchall()
        return [self._decode_segment(row) for row in rows]

    def get_segment(self, project_id: str, segment_id: int) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND id = ?",
                (project_id, segment_id),
            ).fetchone()
        return self._decode_segment(row) if row is not None else None

    @staticmethod
    def _decode_segment(row: sqlite3.Row) -> dict[str, Any]:
        segment = dict(row)
        try:
            segment["quality_flags"] = json.loads(segment.get("quality_flags") or "[]")
        except json.JSONDecodeError:
            segment["quality_flags"] = []
        return segment

    def replace_timed_tokens(
        self,
        project_id: str,
        tokens: Iterable[dict[str, Any]],
    ) -> None:
        with transaction(self.database_path) as connection:
            connection.execute("DELETE FROM timed_tokens WHERE project_id = ?", (project_id,))
            connection.executemany(
                """
                INSERT INTO timed_tokens (
                    project_id, sequence, raw_text, normalized_text, start_ms,
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

    def list_timed_tokens(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM timed_tokens WHERE project_id = ? ORDER BY sequence",
                (project_id,),
            ).fetchall()
        tokens = [dict(row) for row in rows]
        for token in tokens:
            token["text"] = token["normalized_text"]
            token["timing_inferred"] = bool(token["timing_inferred"])
        return tokens

    def replace_source_sentences(
        self,
        project_id: str,
        part_id: int,
        sentences: Iterable[dict[str, Any]],
    ) -> None:
        timestamp = now_iso()
        rows = list(sentences)
        with transaction(self.database_path) as connection:
            connection.execute("DELETE FROM source_sentences WHERE part_id = ?", (part_id,))
            connection.executemany(
                """
                INSERT INTO source_sentences (
                    project_id, part_id, source_sentence_id, start_token_id, end_token_id,
                    start_ms, end_ms, source_sentence, chinese_sentence, hidden_token_ids,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        part_id,
                        int(sentence["source_sentence_id"]),
                        int(sentence["start_token_id"]),
                        int(sentence["end_token_id"]),
                        int(sentence["start_ms"]),
                        int(sentence["end_ms"]),
                        str(sentence["source_sentence"]),
                        str(sentence.get("chinese_sentence", "")),
                        json.dumps(sentence.get("hidden_token_ids", [])),
                        timestamp,
                        timestamp,
                    )
                    for sentence in rows
                ],
            )

    def list_source_sentences(self, part_id: int) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM source_sentences WHERE part_id = ? ORDER BY source_sentence_id",
                (part_id,),
            ).fetchall()
        result = []
        for row in rows:
            sentence = dict(row)
            try:
                sentence["hidden_token_ids"] = json.loads(
                    sentence.get("hidden_token_ids") or "[]"
                )
            except json.JSONDecodeError:
                sentence["hidden_token_ids"] = []
            result.append(sentence)
        return result

    def clear_subtitle_data(
        self, project_id: str, *, preserve_global_context: bool = False
    ) -> None:
        with transaction(self.database_path) as connection:
            connection.execute("DELETE FROM segments WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM subtitle_parts WHERE project_id = ?", (project_id,))
            connection.execute("DELETE FROM timed_tokens WHERE project_id = ?", (project_id,))
            connection.execute(
                "DELETE FROM jobs WHERE project_id = ? AND status IN ('completed', 'failed')",
                (project_id,),
            )
            context_reset = (
                ""
                if preserve_global_context
                else """
                    global_translation_context = NULL, global_context_provider = NULL,
                    global_context_approved = 0,
                """
            )
            connection.execute(
                f"""
                UPDATE projects
                SET status = 'queued', stage = 'queued', progress = 0, error = NULL,
                    pipeline_detail = NULL,
                    subtitle_revision = 0, rendered_revision = NULL, output_path = NULL,
                    {context_reset}
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
                rows = []
                for segment in result["segments"]:
                    source_text, translated_text = normalize_segment_texts(
                        str(segment.get("source_text", "")),
                        str(segment.get("translated_text", "")),
                    )
                    if not source_text and not translated_text:
                        continue
                    rows.append((segment, source_text, translated_text))

                source_sentences = list(result.get("source_sentences", []))
                if source_sentences:
                    connection.execute(
                        "DELETE FROM source_sentences WHERE part_id = ?", (part_id,)
                    )
                    connection.executemany(
                        """
                        INSERT INTO source_sentences (
                            project_id, part_id, source_sentence_id, start_token_id,
                            end_token_id, start_ms, end_ms, source_sentence,
                            chinese_sentence, hidden_token_ids, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                project_id,
                                part_id,
                                int(sentence["source_sentence_id"]),
                                int(sentence["start_token_id"]),
                                int(sentence["end_token_id"]),
                                int(sentence["start_ms"]),
                                int(sentence["end_ms"]),
                                str(sentence["source_sentence"]),
                                str(sentence.get("chinese_sentence", "")),
                                json.dumps(sentence.get("hidden_token_ids", [])),
                                timestamp,
                                timestamp,
                            )
                            for sentence in source_sentences
                        ],
                    )

                connection.execute("DELETE FROM segments WHERE part_id = ?", (part_id,))
                next_sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), -1) + 1 FROM segments WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()[0]
                )
                connection.executemany(
                    """
                    INSERT INTO segments (
                        project_id, sequence, start_ms, end_ms, source_text,
                        translated_text, start_token_id, end_token_id, source_sentence_id,
                        sub_id, quality_flags, part_id, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            project_id,
                            next_sequence + index,
                            int(segment["start_ms"]),
                            int(segment["end_ms"]),
                            source_text,
                            translated_text,
                            int(segment["start_token_id"]),
                            int(segment["end_token_id"]),
                            segment.get("source_sentence_id"),
                            segment.get("sub_id"),
                            json.dumps(segment.get("quality_flags", [])),
                            part_id,
                            segment.get("status", "machine_translated"),
                            timestamp,
                        )
                        for index, (segment, source_text, translated_text) in enumerate(rows)
                    ],
                )

                connection.execute(
                    """
                    UPDATE subtitle_parts
                    SET status = 'completed', source_text = ?, reference_translation = ?,
                        translation_provider = ?, error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(result["source_text"]),
                        str(result["reference_translation"]),
                        str(result["reference_provider"]),
                        timestamp,
                        part_id,
                    ),
                )

            ordered = connection.execute(
                "SELECT id FROM segments WHERE project_id = ? ORDER BY start_ms, id",
                (project_id,),
            ).fetchall()
            connection.execute(
                "UPDATE segments SET sequence = sequence + 1000000 WHERE project_id = ?",
                (project_id,),
            )
            connection.executemany(
                "UPDATE segments SET sequence = ? WHERE id = ?",
                [(index, row["id"]) for index, row in enumerate(ordered)],
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
        allowed = {"start_ms", "end_ms", "source_text", "translated_text", "status"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if "source_text" in values:
            values["source_text"] = normalize_segment_texts(str(values["source_text"]), "")[0]
        if "translated_text" in values:
            values["translated_text"] = normalize_segment_texts("", str(values["translated_text"]))[
                1
            ]
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
                "SELECT * FROM segments WHERE project_id = ? AND sequence = ?",
                (project_id, int(current["sequence"]) - 1),
            ).fetchone()
            following = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND sequence = ?",
                (project_id, int(current["sequence"]) + 1),
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
                "ORDER BY sequence",
                affected_ids,
            ).fetchall()
        return [self._decode_segment(row) for row in rows]

    def split_segment(self, project_id: str, segment_id: int, at_ms: int) -> None:
        with transaction(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND id = ?",
                (project_id, segment_id),
            ).fetchone()
            if row is None:
                raise KeyError(segment_id)
            if at_ms <= row["start_ms"] + 100 or at_ms >= row["end_ms"] - 100:
                raise ValueError("The split point must be at least 100 ms from both boundaries")
            midpoint = max(1, len(row["source_text"]) // 2)
            source_left = row["source_text"][:midpoint].strip()
            source_right = row["source_text"][midpoint:].strip()
            translated_midpoint = max(1, len(row["translated_text"]) // 2)
            translated_left = row["translated_text"][:translated_midpoint].strip()
            translated_right = row["translated_text"][translated_midpoint:].strip()
            # Shift through a temporary range to avoid the UNIQUE(project_id, sequence)
            # constraint being checked row-by-row by SQLite.
            connection.execute(
                "UPDATE segments SET sequence = sequence + 1000000 WHERE project_id = ? AND sequence > ?",
                (project_id, row["sequence"]),
            )
            connection.execute(
                "UPDATE segments SET sequence = sequence - 999999 WHERE project_id = ? AND sequence >= 1000000",
                (project_id,),
            )
            connection.execute(
                """
                UPDATE segments SET end_ms = ?, source_text = ?, translated_text = ?,
                    status = 'edited', updated_at = ? WHERE id = ?
                """,
                (at_ms, source_left, translated_left, now_iso(), segment_id),
            )
            connection.execute(
                """
                INSERT INTO segments (
                    project_id, sequence, start_ms, end_ms, source_text,
                    translated_text, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'edited', ?)
                """,
                (
                    project_id,
                    row["sequence"] + 1,
                    at_ms,
                    row["end_ms"],
                    source_right,
                    translated_right,
                    now_iso(),
                ),
            )
            connection.execute(
                "UPDATE projects SET subtitle_revision = subtitle_revision + 1, updated_at = ? WHERE id = ?",
                (now_iso(), project_id),
            )

    def merge_with_next(self, project_id: str, segment_id: int) -> None:
        with transaction(self.database_path) as connection:
            current = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND id = ?",
                (project_id, segment_id),
            ).fetchone()
            if current is None:
                raise KeyError(segment_id)
            following = connection.execute(
                "SELECT * FROM segments WHERE project_id = ? AND sequence = ?",
                (project_id, current["sequence"] + 1),
            ).fetchone()
            if following is None:
                raise ValueError("This is already the final subtitle")
            connection.execute(
                """
                UPDATE segments SET end_ms = ?, source_text = ?, translated_text = ?,
                    status = 'edited', updated_at = ? WHERE id = ?
                """,
                (
                    following["end_ms"],
                    normalize_segment_texts(
                        f"{current['source_text']} {following['source_text']}",
                        f"{current['translated_text']}{following['translated_text']}",
                    )[0],
                    normalize_segment_texts(
                        f"{current['source_text']} {following['source_text']}",
                        f"{current['translated_text']}{following['translated_text']}",
                    )[1],
                    now_iso(),
                    current["id"],
                ),
            )
            connection.execute("DELETE FROM segments WHERE id = ?", (following["id"],))
            connection.execute(
                "UPDATE segments SET sequence = sequence + 1000000 WHERE project_id = ? AND sequence > ?",
                (project_id, following["sequence"]),
            )
            connection.execute(
                "UPDATE segments SET sequence = sequence - 1000001 WHERE project_id = ? AND sequence >= 1000000",
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
            connection.execute("DELETE FROM subtitle_parts WHERE project_id = ?", (project_id,))
            for part_sequence, part in enumerate(parts):
                connection.execute(
                    """
                    INSERT INTO subtitle_parts (
                        project_id, sequence, start_token_id, end_token_id,
                        start_ms, end_ms, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        project_id,
                        part_sequence,
                        part["start_token_id"],
                        part["end_token_id"],
                        part["start_ms"],
                        part["end_ms"],
                        timestamp,
                        timestamp,
                    ),
                )

    def list_subtitle_parts(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM subtitle_parts
                WHERE project_id = ?
                ORDER BY sequence
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_subtitle_part(self, part_id: int, **fields: Any) -> None:
        allowed = {
            "status",
            "source_text",
            "reference_translation",
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
                f"UPDATE subtitle_parts SET {assignments} WHERE id = ?",
                (*values.values(), part_id),
            )

    def replace_part_segments(
        self,
        project_id: str,
        part_id: int,
        segments: Iterable[dict[str, Any]],
    ) -> None:
        timestamp = now_iso()
        rows = []
        for segment in segments:
            source_text, translated_text = normalize_segment_texts(
                str(segment.get("source_text", "")),
                str(segment.get("translated_text", "")),
            )
            if not source_text and not translated_text:
                continue
            rows.append((segment, source_text, translated_text))

        with transaction(self.database_path) as connection:
            connection.execute("DELETE FROM segments WHERE part_id = ?", (part_id,))
            next_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 FROM segments WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0]
            )
            connection.executemany(
                """
                INSERT INTO segments (
                    project_id, sequence, start_ms, end_ms, source_text,
                    translated_text, start_token_id, end_token_id, source_sentence_id,
                    sub_id, quality_flags, part_id, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        next_sequence + index,
                        int(segment["start_ms"]),
                        int(segment["end_ms"]),
                        source_text,
                        translated_text,
                        int(segment["start_token_id"]),
                        int(segment["end_token_id"]),
                        segment.get("source_sentence_id"),
                        segment.get("sub_id"),
                        json.dumps(segment.get("quality_flags", [])),
                        part_id,
                        segment.get("status", "machine_translated"),
                        timestamp,
                    )
                    for index, (segment, source_text, translated_text) in enumerate(rows)
                ],
            )
            ordered = connection.execute(
                "SELECT id FROM segments WHERE project_id = ? ORDER BY start_ms, id",
                (project_id,),
            ).fetchall()
            connection.execute(
                "UPDATE segments SET sequence = sequence + 1000000 WHERE project_id = ?",
                (project_id,),
            )
            connection.executemany(
                "UPDATE segments SET sequence = ? WHERE id = ?",
                [(index, row["id"]) for index, row in enumerate(ordered)],
            )

    def normalize_existing_segments(self, project_id: str | None = None) -> int:
        changed = 0
        with transaction(self.database_path) as connection:
            query = "SELECT id, source_text, translated_text FROM segments"
            params: tuple[Any, ...] = ()
            if project_id is not None:
                query += " WHERE project_id = ?"
                params = (project_id,)
            for row in connection.execute(query, params).fetchall():
                source_text, translated_text = normalize_segment_texts(
                    row["source_text"], row["translated_text"]
                )
                if source_text != row["source_text"] or translated_text != row["translated_text"]:
                    connection.execute(
                        "UPDATE segments SET source_text = ?, translated_text = ?, updated_at = ? WHERE id = ?",
                        (source_text, translated_text, now_iso(), row["id"]),
                    )
                    changed += 1
        return changed

    def enqueue_job(self, project_id: str, kind: str) -> dict[str, Any]:
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
                INSERT INTO jobs (id, project_id, kind, status, created_at)
                VALUES (?, ?, ?, 'queued', ?)
                """,
                (job_id, project_id, kind, now_iso()),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

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

    def recover_running_jobs(self) -> int:
        with transaction(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
            )
            return cursor.rowcount
