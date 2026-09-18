"""Back up one project's subtitles and queue regeneration using its saved tokens/context."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime

from app.config import settings
from app.db import transaction
from app.repository import now_iso


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id", type=lambda value: str(uuid.UUID(value)))
    args = parser.parse_args()
    project_id = args.project_id
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = settings.data_dir / "projects" / project_id / "artifacts" / "backups"
    with transaction() as connection:
        project = connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise RuntimeError("Project not found")
        if (
            project["status"] in {"queued", "running"}
            or connection.execute(
                "SELECT 1 FROM jobs WHERE project_id = ? AND status IN ('queued', 'running')",
                (project_id,),
            ).fetchone()
        ):
            raise RuntimeError("Cannot regenerate a project with active work")
        if not connection.execute(
            "SELECT 1 FROM timed_tokens WHERE project_id = ?", (project_id,)
        ).fetchone():
            raise RuntimeError("Project has no saved timed tokens")
        snapshot = {"project": dict(project)}
        for table in ("timed_tokens", "segments", "subtitle_parts", "subtitle_batches"):
            snapshot[table] = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} WHERE project_id = ? ORDER BY sequence", (project_id,)
                )
            ]
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"before-quality-regeneration-{timestamp}.json"
        with backup_path.open("x", encoding="utf-8") as backup:
            json.dump(snapshot, backup, ensure_ascii=False, indent=2)
        connection.execute("DELETE FROM segments WHERE project_id = ?", (project_id,))
        connection.execute(
            "UPDATE subtitle_batches SET status = 'pending', attempts = 0, provider = NULL, "
            "error = NULL, updated_at = ? WHERE project_id = ? "
            "AND COALESCE(error, '') != 'Superseded by smaller retry batches'",
            (now_iso(), project_id),
        )
        connection.execute(
            "UPDATE subtitle_parts SET status = 'pending', source_text = '', "
            "reference_translation = '', translation_provider = NULL, error = NULL, "
            "updated_at = ? "
            "WHERE project_id = ?",
            (now_iso(), project_id),
        )
        connection.execute(
            "UPDATE projects SET status = 'queued', stage = 'queued', progress = 0, error = NULL, "
            "subtitle_revision = subtitle_revision + 1, updated_at = ? WHERE id = ?",
            (now_iso(), project_id),
        )
        job_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO jobs (id, project_id, kind, status, created_at) "
            "VALUES (?, ?, 'translate', 'queued', ?)",
            (job_id, project_id, now_iso()),
        )
    print(
        json.dumps(
            {"backup": str(backup_path), "job_id": job_id, "project_id": project_id},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
