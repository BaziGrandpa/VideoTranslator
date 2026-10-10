from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.cards.model import CardPayload, CardStyle
from app.db import connect, transaction


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _decode(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["style"] = json.loads(result.pop("style_json"))
    return result


class CardRepository:
    def __init__(self, database_path: Path | None = None):
        self.database_path = database_path

    def list(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM commentary_cards WHERE project_id = ? ORDER BY start_ms, id",
                (project_id,),
            ).fetchall()
        return [_decode(row) for row in rows]

    def get(self, project_id: str, card_id: int) -> dict[str, Any] | None:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM commentary_cards WHERE project_id = ? AND id = ?",
                (project_id, card_id),
            ).fetchone()
        return _decode(row) if row else None

    def default_style(self) -> dict[str, Any]:
        with connect(self.database_path) as connection:
            saved = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'default_commentary_card_style'"
            ).fetchone()
            if saved is None:
                saved = connection.execute(
                    """SELECT style_json AS value FROM commentary_cards
                       ORDER BY updated_at DESC, id DESC LIMIT 1"""
                ).fetchone()
        if saved is not None:
            try:
                return CardStyle.model_validate_json(saved["value"]).model_dump()
            except ValueError:
                pass
        return CardStyle().model_dump()

    @staticmethod
    def _remember_style(connection: Any, payload: CardPayload, timestamp: str) -> None:
        connection.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES ('default_commentary_card_style', ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value, updated_at = excluded.updated_at""",
            (payload.style.model_dump_json(), timestamp),
        )

    def state(self, project_id: str) -> dict[str, int]:
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT revision, rendered_revision FROM commentary_card_render_state WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return dict(row) if row else {"revision": 0, "rendered_revision": 0}

    def _bump(self, connection: Any, project_id: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO commentary_card_render_state(project_id) VALUES (?)",
            (project_id,),
        )
        connection.execute(
            "UPDATE commentary_card_render_state SET revision = revision + 1 WHERE project_id = ?",
            (project_id,),
        )

    def _check_overlap(
        self, connection: Any, project_id: str, payload: CardPayload, excluding: int | None = None
    ) -> None:
        row = connection.execute(
            """SELECT id FROM commentary_cards
               WHERE project_id = ? AND id != ? AND start_ms < ? AND end_ms > ? LIMIT 1""",
            (project_id, excluding or -1, payload.end_ms, payload.start_ms),
        ).fetchone()
        if row:
            raise ValueError(f"Card overlaps card {row['id']}; cards may touch but cannot overlap")

    def create(self, project_id: str, payload: CardPayload) -> dict[str, Any]:
        timestamp = _now_iso()
        with transaction(self.database_path) as connection:
            self._check_overlap(connection, project_id, payload)
            cursor = connection.execute(
                """INSERT INTO commentary_cards
                   (project_id, start_ms, end_ms, chinese_term, english_term,
                    explanation, style_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id, payload.start_ms, payload.end_ms, payload.chinese_term,
                    payload.english_term, payload.explanation,
                    payload.style.model_dump_json(), timestamp, timestamp,
                ),
            )
            self._bump(connection, project_id)
            self._remember_style(connection, payload, timestamp)
            card_id = cursor.lastrowid
        result = self.get(project_id, card_id)
        assert result is not None
        return result

    def update(self, project_id: str, card_id: int, payload: CardPayload) -> dict[str, Any]:
        timestamp = _now_iso()
        with transaction(self.database_path) as connection:
            self._check_overlap(connection, project_id, payload, card_id)
            cursor = connection.execute(
                """UPDATE commentary_cards SET start_ms = ?, end_ms = ?, chinese_term = ?,
                   english_term = ?, explanation = ?, style_json = ?, updated_at = ?
                   WHERE project_id = ? AND id = ?""",
                (
                    payload.start_ms, payload.end_ms, payload.chinese_term,
                    payload.english_term, payload.explanation, payload.style.model_dump_json(),
                    timestamp, project_id, card_id,
                ),
            )
            if not cursor.rowcount:
                raise KeyError(card_id)
            self._bump(connection, project_id)
            self._remember_style(connection, payload, timestamp)
        result = self.get(project_id, card_id)
        assert result is not None
        return result

    def delete(self, project_id: str, card_id: int) -> None:
        with transaction(self.database_path) as connection:
            cursor = connection.execute(
                "DELETE FROM commentary_cards WHERE project_id = ? AND id = ?",
                (project_id, card_id),
            )
            if not cursor.rowcount:
                raise KeyError(card_id)
            self._bump(connection, project_id)

    def mark_rendered(self, project_id: str) -> None:
        with transaction(self.database_path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO commentary_card_render_state(project_id) VALUES (?)",
                (project_id,),
            )
            connection.execute(
                """UPDATE commentary_card_render_state SET rendered_revision = revision
                   WHERE project_id = ?""",
                (project_id,),
            )
