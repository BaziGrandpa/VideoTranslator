from __future__ import annotations

import io
import json
import sqlite3
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field

from app.config import settings
from app.db import connect, transaction
from app.repository import Repository, now_iso
from app.services.publication_copy import INTRO_FOOTER, latest_output_srt, title_with_suffix
from app.services.publication_cover import (
    SIZES,
    CoverDesign,
    default_design,
    design_style,
    render_cover,
    style_line_count,
    title_line_count,
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class SelectedCopy(BaseModel):
    title: str = Field(max_length=200)
    intro: str = Field(max_length=2000)


class SelectedThumbnailTitle(BaseModel):
    title: str = Field(max_length=24)


class SelectedCoverTitle(BaseModel):
    title: str = Field(max_length=120)


def _require_project(repository: Repository, project_id: str) -> dict[str, Any]:
    project = repository.get_project(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


def _require_workspace(repository: Repository, project_id: str) -> dict[str, Any]:
    with connect(repository.database_path) as connection:
        row = connection.execute(
            "SELECT * FROM publication_workspaces WHERE project_id = ?", (project_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(409, "Open the publication workbench first")
    return dict(row)


def _publication_dir(project_id: str) -> Path:
    path = settings.data_dir / "projects" / project_id / "publication"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_output_dir(project_id: str) -> Path:
    root = settings.output_dir.resolve()
    path = root / project_id
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise HTTPException(409, "Project output path is a link")
    resolved = path.resolve()
    if resolved.parent != root or not resolved.is_dir():
        raise HTTPException(409, "The project video output folder is unavailable")
    return resolved


def _cover_row(repository: Repository, project_id: str, ratio: str) -> dict[str, Any] | None:
    with connect(repository.database_path) as connection:
        row = connection.execute(
            "SELECT * FROM publication_covers WHERE project_id = ? AND ratio = ?",
            (project_id, ratio),
        ).fetchone()
    return dict(row) if row else None


def _inherit_cover_styles(
    connection: sqlite3.Connection, project_id: str, created_at: str
) -> None:
    previous = connection.execute(
        """SELECT w.project_id FROM publication_workspaces AS w
           WHERE w.project_id != ? AND w.created_at < ?
             AND EXISTS (SELECT 1 FROM publication_covers AS c
                         WHERE c.project_id = w.project_id)
           ORDER BY w.created_at DESC LIMIT 1""",
        (project_id, created_at),
    ).fetchone()
    if previous is None:
        return
    source_id = previous["project_id"]
    for cover in connection.execute(
        "SELECT ratio, design_json FROM publication_covers WHERE project_id = ?", (source_id,)
    ).fetchall():
        ratio = cover["ratio"]
        source_design = CoverDesign.model_validate_json(cover["design_json"]).model_dump()
        active_style = {**design_style(source_design), "highlight_terms": []}
        inherited_design = CoverDesign.model_validate(
            {**active_style, "title": ""}
        ).model_dump()
        connection.execute(
            """INSERT OR IGNORE INTO publication_covers
               (project_id, ratio, design_json, updated_at) VALUES (?, ?, ?, ?)""",
            (project_id, ratio, json.dumps(inherited_design, ensure_ascii=False), created_at),
        )
        for preset in connection.execute(
            """SELECT line_count, style_json FROM publication_cover_styles
               WHERE project_id = ? AND ratio = ?""",
            (source_id, ratio),
        ).fetchall():
            inherited_style = {**json.loads(preset["style_json"]), "highlight_terms": []}
            connection.execute(
                """INSERT OR IGNORE INTO publication_cover_styles
                   (project_id, ratio, line_count, style_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, ratio, preset["line_count"],
                 json.dumps(inherited_style, ensure_ascii=False), created_at),
            )
        connection.execute(
            """INSERT OR IGNORE INTO publication_cover_styles
               (project_id, ratio, line_count, style_json, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (project_id, ratio, style_line_count(source_design["title"]),
             json.dumps(active_style, ensure_ascii=False), created_at),
        )


def _set_cover_title(
    connection: sqlite3.Connection, project_id: str, ratio: str, title: str,
    highlight_terms: list[str] | None = None,
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT design_json FROM publication_covers WHERE project_id = ? AND ratio = ?",
        (project_id, ratio),
    ).fetchone()
    if row is None:
        current = default_design(ratio)
        connection.execute(
            """INSERT INTO publication_covers
               (project_id, ratio, design_json, updated_at) VALUES (?, ?, ?, ?)""",
            (project_id, ratio, json.dumps(current), now_iso()),
        )
    else:
        current = CoverDesign.model_validate_json(row["design_json"]).model_dump()
    current_style = design_style(current)
    connection.execute(
        """INSERT OR IGNORE INTO publication_cover_styles
           (project_id, ratio, line_count, style_json, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (project_id, ratio, style_line_count(current["title"]),
         json.dumps(current_style, ensure_ascii=False), now_iso()),
    )
    if title == current["title"]:
        next_design = dict(current)
    else:
        line_count = style_line_count(title)
        preset = connection.execute(
            """SELECT style_json FROM publication_cover_styles
               WHERE project_id = ? AND ratio = ? AND line_count = ?""",
            (project_id, ratio, line_count),
        ).fetchone()
        style = json.loads(preset["style_json"]) if preset else current_style
        next_design = CoverDesign.model_validate({**style, "title": title}).model_dump()
        if preset is None:
            connection.execute(
                """INSERT INTO publication_cover_styles
                   (project_id, ratio, line_count, style_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, ratio, line_count,
                 json.dumps(style, ensure_ascii=False), now_iso()),
            )
    if highlight_terms is not None:
        next_design["highlight_terms"] = list(highlight_terms)
        connection.execute(
            """INSERT INTO publication_cover_styles
               (project_id, ratio, line_count, style_json, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(project_id, ratio, line_count) DO UPDATE SET
               style_json = excluded.style_json, updated_at = excluded.updated_at""",
            (project_id, ratio, style_line_count(title),
             json.dumps(design_style(next_design), ensure_ascii=False), now_iso()),
        )
    if next_design != current:
        connection.execute(
            """UPDATE publication_covers SET design_json = ?, revision = revision + 1,
               updated_at = ? WHERE project_id = ? AND ratio = ?""",
            (json.dumps(next_design, ensure_ascii=False), now_iso(), project_id, ratio),
        )
    return next_design


def _state(repository: Repository, project_id: str) -> dict[str, Any]:
    project = _require_project(repository, project_id)
    available = latest_output_srt(project_id) is not None
    with connect(repository.database_path) as connection:
        row = connection.execute(
            "SELECT * FROM publication_workspaces WHERE project_id = ?", (project_id,)
        ).fetchone()
        covers = connection.execute(
            "SELECT * FROM publication_covers WHERE project_id = ?", (project_id,)
        ).fetchall()
    data: dict[str, Any] = {
        "project_id": project_id,
        "project_title": project.get("display_name") or project.get("title") or "Untitled project",
        "available": available,
        "started": row is not None,
        "covers": {},
    }
    if row is not None:
        titles = json.loads(row["titles_json"])
        intros = json.loads(row["intros_json"])
        data.update({
            "copy_status": row["copy_status"],
            "copy_error": row["copy_error"],
            "titles": titles,
            "intros": intros,
            "selected_title": row["selected_title"],
            "selected_intro": row["selected_intro"],
            "intro_footer": INTRO_FOOTER.format(source_url=row["source_url"]),
            "final_intro": row["selected_intro"] + INTRO_FOOTER.format(source_url=row["source_url"])
            if row["selected_intro"] else "",
            "source_url": row["source_url"],
            "provider": row["provider"],
            "thumbnail_status": row["thumbnail_status"],
            "thumbnail_error": row["thumbnail_error"],
            "thumbnail_titles": json.loads(row["thumbnail_titles_json"]),
            "selected_thumbnail_title": row["selected_thumbnail_title"],
            "has_reference": bool(row["reference_path"] and Path(row["reference_path"]).is_file()),
            "reference_url": f"/api/projects/{project_id}/publication/reference"
            if row["reference_path"] and Path(row["reference_path"]).is_file() else None,
            "background_status": row["background_status"],
            "background_error": row["background_error"],
            "reference_revision": row["reference_revision"],
        })
    by_ratio = {cover["ratio"]: cover for cover in covers}
    for ratio in SIZES:
        cover = by_ratio.get(ratio)
        design = (
            CoverDesign.model_validate_json(cover["design_json"]).model_dump()
            if cover else default_design(ratio)
        )
        image_path = Path(cover["image_path"]) if cover and cover["image_path"] else None
        export_path = Path(cover["exported_path"]) if cover and cover["exported_path"] else None
        data["covers"][ratio] = {
            "design": design,
            "line_count": style_line_count(design["title"]),
            "title_line_count": title_line_count(design["title"]),
            "revision": cover["revision"] if cover else 0,
            "has_image": bool(image_path and image_path.is_file()),
            "image_url": f"/api/projects/{project_id}/publication/covers/{ratio}/image"
            if image_path and image_path.is_file() else None,
            "has_export": bool(export_path and export_path.is_file()),
            "export_in_output": bool(
                export_path and export_path.is_file() and
                export_path.resolve().parent == (settings.output_dir / project_id).resolve()
            ),
            "export_url": f"/api/projects/{project_id}/publication/covers/{ratio}/export"
            if export_path and export_path.is_file() else None,
            "export_stale": bool(cover and export_path and
                                 cover["exported_revision"] != cover["revision"]),
        }
    return data


def create_router(get_repository: Callable[[], Repository]) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/publication")

    @router.get("")
    def get_publication(project_id: str) -> dict[str, Any]:
        return _state(get_repository(), project_id)

    @router.post("/start")
    def start_publication(project_id: str) -> dict[str, Any]:
        repository = get_repository()
        project = _require_project(repository, project_id)
        srt = latest_output_srt(project_id)
        if srt is None:
            raise HTTPException(409, "Render a video before opening the publication workbench")
        with transaction(repository.database_path) as connection:
            existing = connection.execute(
                "SELECT project_id FROM publication_workspaces WHERE project_id = ?", (project_id,)
            ).fetchone()
            if existing is None:
                timestamp = now_iso()
                connection.execute(
                    """INSERT INTO publication_workspaces
                       (project_id, source_srt, source_url, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (project_id, str(srt), project["source_url"], timestamp, timestamp),
                )
                _inherit_cover_styles(connection, project_id, timestamp)
        workspace = _require_workspace(repository, project_id)
        if workspace["copy_status"] == "queued":
            repository.enqueue_job(project_id, "generate_publication_copy")
        if workspace["thumbnail_status"] == "queued":
            repository.enqueue_job(project_id, "generate_thumbnail_titles")
        return _state(repository, project_id)

    @router.post("/retry-copy")
    def retry_copy(project_id: str) -> dict[str, Any]:
        repository = get_repository()
        workspace = _require_workspace(repository, project_id)
        if workspace["copy_status"] != "failed":
            raise HTTPException(409, "Only failed copy generation can be retried")
        with transaction(repository.database_path) as connection:
            connection.execute(
                "UPDATE publication_workspaces SET copy_status = 'queued', copy_error = NULL, "
                "updated_at = ? WHERE project_id = ?", (now_iso(), project_id)
            )
        repository.enqueue_job(project_id, "generate_publication_copy")
        return _state(repository, project_id)

    @router.post("/retry-thumbnail-titles")
    def retry_thumbnail_titles(project_id: str) -> dict[str, Any]:
        repository = get_repository()
        workspace = _require_workspace(repository, project_id)
        if workspace["thumbnail_status"] != "failed":
            raise HTTPException(409, "Only failed cover text generation can be retried")
        with transaction(repository.database_path) as connection:
            connection.execute(
                "UPDATE publication_workspaces SET thumbnail_status = 'queued', "
                "thumbnail_error = NULL, updated_at = ? WHERE project_id = ?",
                (now_iso(), project_id),
            )
        repository.enqueue_job(project_id, "generate_thumbnail_titles")
        return _state(repository, project_id)

    @router.patch("/copy")
    def save_copy(project_id: str, payload: SelectedCopy) -> dict[str, Any]:
        repository = get_repository()
        workspace = _require_workspace(repository, project_id)
        if workspace["copy_status"] != "completed":
            raise HTTPException(409, "Wait for candidate generation to finish")
        intro = payload.intro.strip().replace("\r\n", "\n")
        try:
            title = title_with_suffix(payload.title)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if not intro:
            raise HTTPException(422, "Enter a title and intro body")
        if any(marker in intro for marker in ("原视频：", "翻译 / 时间轴", "All rights to")):
            raise HTTPException(422, "Edit only the intro body; the fixed footer is added automatically")
        with transaction(repository.database_path) as connection:
            connection.execute(
                """UPDATE publication_workspaces SET selected_title = ?, selected_intro = ?,
                   updated_at = ? WHERE project_id = ?""",
                (title, intro, now_iso(), project_id),
            )
        return _state(repository, project_id)

    @router.patch("/thumbnail-title")
    def save_thumbnail_title(project_id: str, payload: SelectedThumbnailTitle) -> dict[str, Any]:
        repository = get_repository()
        workspace = _require_workspace(repository, project_id)
        if workspace["thumbnail_status"] != "completed":
            raise HTTPException(409, "Wait for cover text candidates to finish")
        title = payload.title.strip()
        if not 2 <= len(title) <= 24 or "\n" in title or "\r" in title:
            raise HTTPException(422, "Cover text must be 2–24 characters on one line")
        with transaction(repository.database_path) as connection:
            connection.execute(
                "UPDATE publication_workspaces SET selected_thumbnail_title = ?, "
                "updated_at = ? WHERE project_id = ?",
                (title, now_iso(), project_id),
            )
        return _state(repository, project_id)

    @router.put("/reference")
    async def upload_reference(project_id: str, request: Request) -> dict[str, Any]:
        repository = get_repository()
        workspace = _require_workspace(repository, project_id)
        if workspace["background_status"] in {"queued", "running"}:
            raise HTTPException(409, "Wait for background generation to finish")
        if request.headers.get("content-length", "").isdigit() and int(
            request.headers["content-length"]
        ) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "The image exceeds 20 MB")
        raw = await request.body()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "The image exceeds 20 MB")
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("Upload a PNG, JPEG, or WebP image")
                if source.width * source.height > 25_000_000:
                    raise ValueError("The image is too large; use at most 25 megapixels")
                image = ImageOps.exif_transpose(source).convert("RGB")
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
            raise HTTPException(422, str(exc) or "Invalid image") from None
        if image.width < 640 or image.height < 360:
            raise HTTPException(422, "Use an image at least 640 pixels wide and 360 pixels high")
        path = _publication_dir(project_id) / f"reference-{uuid.uuid4().hex}.png"
        image.save(path, format="PNG")
        try:
            with transaction(repository.database_path) as connection:
                changed = connection.execute(
                    """UPDATE publication_workspaces SET reference_path = ?,
                       reference_revision = reference_revision + 1,
                       background_status = 'idle', background_error = NULL,
                       updated_at = ? WHERE project_id = ?
                       AND background_status NOT IN ('queued', 'running')""",
                    (str(path), now_iso(), project_id),
                ).rowcount
                if not changed:
                    raise HTTPException(409, "Wait for background generation to finish")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return _state(repository, project_id)

    @router.get("/reference")
    def reference_image(project_id: str) -> FileResponse:
        repository = get_repository()
        workspace = _require_workspace(repository, project_id)
        path = Path(workspace["reference_path"]) if workspace["reference_path"] else None
        if path is None or not path.is_file():
            raise HTTPException(404, "Reference image not uploaded")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.post("/generate-backgrounds")
    def queue_background_generation(project_id: str) -> dict[str, Any]:
        repository = get_repository()
        _require_workspace(repository, project_id)
        with transaction(repository.database_path) as connection:
            row = connection.execute(
                "SELECT reference_path, background_status FROM publication_workspaces "
                "WHERE project_id = ?", (project_id,),
            ).fetchone()
            if not row["reference_path"] or not Path(row["reference_path"]).is_file():
                raise HTTPException(409, "Upload a reference image first")
            if row["background_status"] in {"queued", "running"}:
                raise HTTPException(409, "Background generation is already active")
            active = connection.execute(
                "SELECT id FROM jobs WHERE project_id = ? AND kind = 'generate_publication_backgrounds' "
                "AND status IN ('queued', 'running')", (project_id,),
            ).fetchone()
            if active:
                raise HTTPException(409, "Previous background job is still finishing")
            connection.execute(
                "UPDATE publication_workspaces SET background_status = 'queued', "
                "background_error = NULL, background_generation_id = ?, updated_at = ? "
                "WHERE project_id = ?",
                (uuid.uuid4().hex, now_iso(), project_id),
            )
            connection.execute(
                "INSERT INTO jobs (id, project_id, kind, status, created_at) "
                "VALUES (?, ?, 'generate_publication_backgrounds', 'queued', ?)",
                (str(uuid.uuid4()), project_id, now_iso()),
            )
        return _state(repository, project_id)

    @router.put("/covers/{ratio}/image")
    async def upload_cover(project_id: str, ratio: str, request: Request) -> dict[str, Any]:
        if ratio not in SIZES:
            raise HTTPException(404, "Unknown cover ratio")
        repository = get_repository()
        workspace = _require_workspace(repository, project_id)
        if workspace["background_status"] in {"queued", "running"}:
            raise HTTPException(409, "Wait for background generation to finish")
        if request.headers.get("content-length", "").isdigit() and int(
            request.headers["content-length"]
        ) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "The image exceeds 20 MB")
        raw = await request.body()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "The image exceeds 20 MB")
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("Upload a PNG, JPEG, or WebP image")
                if source.width * source.height > 25_000_000:
                    raise ValueError("The image is too large; use at most 25 megapixels")
                image = ImageOps.exif_transpose(source).convert("RGB")
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
            raise HTTPException(422, str(exc) or "Invalid image") from None
        width, height = image.size
        target_width, target_height = SIZES[ratio]
        if width < 640 or height < 360:
            raise HTTPException(422, "Use an image at least 640 pixels wide and 360 pixels high")
        if abs(width / height - target_width / target_height) > 0.012:
            raise HTTPException(422, f"This slot needs a {ratio.replace('x', ':')} image")
        image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
        path = _publication_dir(project_id) / f"base-{ratio}-manual-{uuid.uuid4().hex}.png"
        temporary = path.with_name(f".uploading-{uuid.uuid4().hex}.png")
        image.save(temporary, format="PNG")
        temporary.replace(path)
        existing = _cover_row(repository, project_id, ratio)
        design = existing["design_json"] if existing else json.dumps(default_design(ratio))
        try:
            with transaction(repository.database_path) as connection:
                current = connection.execute(
                    "SELECT background_status FROM publication_workspaces WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if current is None or current["background_status"] in {"queued", "running"}:
                    raise HTTPException(409, "Wait for background generation to finish")
                connection.execute(
                    """INSERT INTO publication_covers
                       (project_id, ratio, image_path, design_json, revision, updated_at)
                       VALUES (?, ?, ?, ?, 1, ?)
                       ON CONFLICT(project_id, ratio) DO UPDATE SET
                       image_path = excluded.image_path,
                       revision = publication_covers.revision + 1,
                       updated_at = excluded.updated_at""",
                    (project_id, ratio, str(path), design, now_iso()),
                )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return _state(repository, project_id)

    @router.get("/covers/{ratio}/image")
    def cover_image(project_id: str, ratio: str) -> FileResponse:
        if ratio not in SIZES:
            raise HTTPException(404, "Unknown cover ratio")
        repository = get_repository()
        _require_workspace(repository, project_id)
        cover = _cover_row(repository, project_id, ratio)
        path = Path(cover["image_path"]) if cover and cover["image_path"] else None
        if path is None or not path.is_file():
            raise HTTPException(404, "Cover image not uploaded")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.patch("/covers/{ratio}/design")
    def save_cover_design(project_id: str, ratio: str, design: CoverDesign) -> dict[str, Any]:
        if ratio not in SIZES:
            raise HTTPException(404, "Unknown cover ratio")
        repository = get_repository()
        _require_workspace(repository, project_id)
        with transaction(repository.database_path) as connection:
            existing = connection.execute(
                "SELECT design_json FROM publication_covers WHERE project_id = ? AND ratio = ?",
                (project_id, ratio),
            ).fetchone()
            values = design.model_dump()
            try:
                line_count = style_line_count(design.title)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            serialized = design.model_dump_json()
            if existing is None:
                connection.execute(
                    """INSERT INTO publication_covers
                       (project_id, ratio, design_json, updated_at) VALUES (?, ?, ?, ?)""",
                    (project_id, ratio, serialized, now_iso()),
                )
            else:
                previous = json.loads(existing["design_json"])
                if design.title != previous["title"]:
                    raise HTTPException(409, "Confirm the cover title before editing its style")
            if existing is not None and previous != values:
                connection.execute(
                    """UPDATE publication_covers SET design_json = ?, revision = revision + 1,
                       updated_at = ? WHERE project_id = ? AND ratio = ?""",
                    (serialized, now_iso(), project_id, ratio),
                )
            connection.execute(
                """INSERT INTO publication_cover_styles
                   (project_id, ratio, line_count, style_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, ratio, line_count) DO UPDATE SET
                   style_json = excluded.style_json, updated_at = excluded.updated_at""",
                (project_id, ratio, line_count,
                 json.dumps(design_style(values), ensure_ascii=False), now_iso()),
            )
            if ratio == "16x9" and design.title.strip():
                _set_cover_title(
                    connection, project_id, "4x3", design.title, design.highlight_terms
                )
        return _state(repository, project_id)

    @router.patch("/covers/{ratio}/title")
    def confirm_cover_title(
        project_id: str, ratio: str, payload: SelectedCoverTitle
    ) -> dict[str, Any]:
        if ratio not in SIZES:
            raise HTTPException(404, "Unknown cover ratio")
        repository = get_repository()
        _require_workspace(repository, project_id)
        title = payload.title.replace("\r\n", "\n").replace("\r", "\n")
        if not title.strip():
            raise HTTPException(422, "Enter a cover title")
        with transaction(repository.database_path) as connection:
            selected = _set_cover_title(connection, project_id, ratio, title)
            if ratio == "16x9":
                _set_cover_title(
                    connection, project_id, "4x3", title, selected["highlight_terms"]
                )
        return _state(repository, project_id)

    @router.post("/covers/{ratio}/preview")
    def preview_cover(project_id: str, ratio: str, design: CoverDesign) -> Response:
        if ratio not in SIZES:
            raise HTTPException(404, "Unknown cover ratio")
        repository = get_repository()
        _require_workspace(repository, project_id)
        cover = _cover_row(repository, project_id, ratio)
        if not cover or not cover["image_path"]:
            raise HTTPException(409, "Upload this cover image first")
        try:
            rendered = render_cover(Path(cover["image_path"]), ratio, design.model_dump())
        except (OSError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from None
        return Response(rendered, media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.post("/render")
    def render_final_covers(project_id: str) -> dict[str, Any]:
        repository = get_repository()
        workspace = _require_workspace(repository, project_id)
        if workspace["background_status"] in {"queued", "running"}:
            raise HTTPException(409, "Wait for background generation to finish")
        covers = {ratio: _cover_row(repository, project_id, ratio) for ratio in SIZES}
        if any(not cover or not cover["image_path"] for cover in covers.values()):
            raise HTTPException(409, "Upload both cover images before rendering")
        rendered: dict[str, bytes] = {}
        for ratio, cover in covers.items():
            try:
                rendered[ratio] = render_cover(
                    Path(cover["image_path"]), ratio, json.loads(cover["design_json"])
                )
            except (OSError, ValueError) as exc:
                raise HTTPException(422, f"{ratio}: {exc}") from None
        output_dir = _project_output_dir(project_id)
        paths: dict[str, Path] = {}
        for ratio, content in rendered.items():
            path = output_dir / f"thumbnail-{ratio}.png"
            temporary = path.with_name(f".rendering-{uuid.uuid4().hex}.png")
            temporary.write_bytes(content)
            temporary.replace(path)
            paths[ratio] = path
        with transaction(repository.database_path) as connection:
            for ratio, path in paths.items():
                changed = connection.execute(
                    """UPDATE publication_covers SET exported_path = ?, exported_revision = ?,
                       updated_at = ? WHERE project_id = ? AND ratio = ? AND revision = ?""",
                    (str(path), covers[ratio]["revision"], now_iso(), project_id, ratio,
                     covers[ratio]["revision"]),
                ).rowcount
                if not changed:
                    raise HTTPException(409, "A cover changed during rendering; render again")
        return _state(repository, project_id)

    @router.get("/covers/{ratio}/export")
    def download_cover(project_id: str, ratio: str) -> FileResponse:
        if ratio not in SIZES:
            raise HTTPException(404, "Unknown cover ratio")
        repository = get_repository()
        _require_workspace(repository, project_id)
        cover = _cover_row(repository, project_id, ratio)
        path = Path(cover["exported_path"]) if cover and cover["exported_path"] else None
        if path is None or not path.is_file():
            raise HTTPException(404, "Cover has not been rendered")
        return FileResponse(path, media_type="image/png", filename=f"thumbnail-{ratio}.png")

    return router
