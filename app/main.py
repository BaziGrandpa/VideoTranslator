from __future__ import annotations

import importlib.util
import json
import logging
import mimetypes
import os
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import command_available, settings
from app.db import init_db
from app.repository import Repository
from app.services.codex_models import list_codex_models
from app.services.exact_preview import (
    PreviewCancelled,
    preview_processes,
    render_exact_preview,
)
from app.services.subtitle_quality import quality_issues
from app.services.subtitle_style import default_subtitle_style, merge_subtitle_style
from app.services.subtitles import validate_segments

STATIC_DIR = Path(__file__).resolve().parent / "static"
request_logger = logging.getLogger("video-translator-http")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_directories()
    init_db()
    yield


app = FastAPI(title="Video Translator", version="0.1.0", lifespan=lifespan)
repository = Repository()


@app.middleware("http")
async def log_failed_requests(request: Request, call_next):
    response = await call_next(request)
    if response.status_code >= 400:
        request_logger.warning(
            "%s %s returned %s",
            request.method,
            request.url.path,
            response.status_code,
        )
    return response


class ProjectCreate(BaseModel):
    source_url: str
    target_language: str = "zh-CN"
    max_height: int = Field(default=1080, ge=360, le=2160)
    bilingual: bool = True
    translation_provider: Literal["auto", "codex", "grok"] = "auto"
    codex_model: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9._-]*$")


class ProjectRename(BaseModel):
    display_name: str = Field(default="", max_length=160)


class SegmentUpdate(BaseModel):
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=1)
    source_text: str | None = None
    translated_text: str | None = None
    status: str | None = None


class SplitRequest(BaseModel):
    at_ms: int = Field(ge=0)


class SubtitleStyleUpdate(BaseModel):
    show_english_subtitles: bool | None = None
    chinese_subtitle_size: int = Field(ge=12, le=120)
    english_subtitle_size: int = Field(ge=12, le=120)
    chinese_subtitle_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    english_subtitle_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    chinese_subtitle_outline: float = Field(ge=0, le=10)
    english_subtitle_outline: float = Field(ge=0, le=10)
    subtitle_bottom_margin: int = Field(ge=0, le=400)
    subtitle_line_gap: int = Field(ge=0, le=300)
    subtitle_horizontal_margin: int = Field(ge=0, le=600)
    bottom_mask_height: float = Field(ge=0.05, le=0.8)
    bottom_mask_opacity: float = Field(ge=0, le=1)


class TranslationProviderUpdate(BaseModel):
    translation_provider: Literal["auto", "codex", "grok"]


class CodexModelUpdate(BaseModel):
    codex_model: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9._-]*$")


class GlobalContextTerm(BaseModel):
    source: str = Field(min_length=1, max_length=300)
    target: str = Field(min_length=1, max_length=300)
    category: Literal[
        "person",
        "organization",
        "brand",
        "product",
        "place",
        "technical",
        "slang",
        "other",
    ] = "other"
    must_use: bool = False
    occurrence_count: int = Field(default=0, ge=0)


class GlobalContextUpdate(BaseModel):
    overview: str = Field(min_length=1, max_length=12000)
    style: str = Field(default="", max_length=12000)
    terms: list[GlobalContextTerm] = Field(default_factory=list, max_length=40)


class ExactPreviewRequest(BaseModel):
    segment_id: int = Field(ge=1)
    timestamp_ms: int = Field(ge=0)


class RebuildWindowSelection(BaseModel):
    batch_id: int = Field(ge=1)
    start_token_id: int = Field(ge=0)
    end_token_id: int = Field(ge=0)


class SubtitleRebuildRequest(BaseModel):
    mode: Literal["full", "selected"] = "full"
    windows: list[RebuildWindowSelection] = Field(default_factory=list)


def validate_youtube_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    if parsed.scheme not in {"http", "https"} or host not in allowed:
        raise HTTPException(status_code=422, detail="Enter a valid YouTube video URL")
    return url.strip()


def require_project(project_id: str) -> dict:
    project = repository.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def project_is_rendering(project: dict) -> bool:
    return project.get("stage") in {"render_queued", "rendering"}


def require_project_not_rendering(project_id: str) -> dict:
    project = require_project(project_id)
    if project_is_rendering(project):
        raise HTTPException(
            status_code=409,
            detail="The current project is locked while the final video is rendering",
        )
    return project


def normalize_global_context(payload: GlobalContextUpdate) -> dict:
    overview = payload.overview.strip()
    if not overview:
        raise HTTPException(status_code=422, detail="Project overview cannot be empty")
    terms = []
    seen: set[str] = set()
    for item in payload.terms:
        source = item.source.strip()
        target = item.target.strip()
        if item.category == "person":
            target = source
        if not source or not target:
            raise HTTPException(
                status_code=422,
                detail="Every terminology row needs both a source and preferred translation",
            )
        key = source.casefold()
        if key in seen:
            raise HTTPException(status_code=422, detail=f"Duplicate terminology source: {source}")
        seen.add(key)
        terms.append(
            {
                "source": source,
                "target": target,
                "category": item.category,
                "must_use": item.category == "person" or item.must_use,
                "occurrence_count": item.occurrence_count,
            }
        )
    return {"overview": overview, "style": payload.style.strip(), "terms": terms}


def require_global_context_review(project_id: str) -> dict:
    project = require_project(project_id)
    if project.get("stage") != "waiting_context_review":
        raise HTTPException(
            status_code=409,
            detail="Global translation context can only be edited at the Pass 0 review checkpoint",
        )
    if not isinstance(project.get("global_translation_context"), dict):
        raise HTTPException(status_code=409, detail="Pass 0 has not produced a context to review")
    return project


def _active_batches(repository_part: dict) -> list[dict]:
    return [
        batch
        for batch in repository.list_subtitle_batches(int(repository_part["id"]))
        if batch.get("error") != "Superseded by smaller retry batches"
    ]


def _merged_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def build_partial_rebuild_scope(
    project: dict, requested: list[RebuildWindowSelection]
) -> dict:
    if not requested:
        raise HTTPException(status_code=422, detail="Select at least one subtitle Window")
    tokens = repository.list_timed_tokens(str(project["id"]))
    parts = repository.list_subtitle_parts(str(project["id"]))
    segments = repository.list_segments(str(project["id"]))
    if not tokens or not parts or not segments:
        raise HTTPException(
            status_code=409,
            detail="Selected-Window rebuild requires existing Tokens, Parts, Windows, and Cues",
        )
    available: dict[int, tuple[dict, dict, int]] = {}
    for part in parts:
        for window_number, batch in enumerate(_active_batches(part), start=1):
            available[int(batch["id"])] = (part, batch, window_number)

    selected: list[dict] = []
    seen: set[int] = set()
    for item in requested:
        if item.batch_id in seen:
            raise HTTPException(status_code=422, detail="A Window was selected more than once")
        seen.add(item.batch_id)
        resolved = available.get(item.batch_id)
        if resolved is None:
            raise HTTPException(status_code=409, detail=f"Window {item.batch_id} is no longer active")
        part, batch, window_number = resolved
        if (
            item.start_token_id != int(batch["start_token_id"])
            or item.end_token_id != int(batch["end_token_id"])
        ):
            raise HTTPException(
                status_code=409,
                detail=f"Window {item.batch_id} Token boundaries changed; reopen the selector",
            )
        selected.append(
            {
                "batch_id": int(batch["id"]),
                "batch_sequence": int(batch["sequence"]),
                "part_id": int(part["id"]),
                "part_sequence": int(part["sequence"]),
                "window_number": window_number,
                "start_token_id": int(batch["start_token_id"]),
                "end_token_id": int(batch["end_token_id"]),
            }
        )

    for part_id in {item["part_id"] for item in selected}:
        part = next(item for item in parts if int(item["id"]) == part_id)
        part_segments = [
            segment for segment in segments if int(segment.get("part_id") or -1) == part_id
        ]
        expected = int(part["start_token_id"])
        for segment in part_segments:
            start = segment.get("start_token_id")
            end = segment.get("end_token_id")
            if not isinstance(start, int) or not isinstance(end, int) or start != expected or end < start:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Part {int(part['sequence']) + 1} no longer has exact Token coverage. "
                        "Use a full rebuild before selecting individual Windows."
                    ),
                )
            expected = end + 1
        if not part_segments or expected != int(part["end_token_id"]) + 1:
            raise HTTPException(
                status_code=409,
                detail=f"Part {int(part['sequence']) + 1} has incomplete Token coverage",
            )
        writable = _merged_ranges(
            [
                (item["start_token_id"], item["end_token_id"])
                for item in selected
                if item["part_id"] == part_id
            ]
        )
        for segment in part_segments:
            start = int(segment["start_token_id"])
            end = int(segment["end_token_id"])
            overlaps = any(not (end < left or start > right) for left, right in writable)
            contained = any(left <= start and end <= right for left, right in writable)
            if overlaps and not contained:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"A Cue crosses the selected Window boundary at Tokens {start}-{end}. "
                        "Select the adjacent Window too."
                    ),
                )

    selected.sort(key=lambda item: item["batch_sequence"])
    return {
        "version": 1,
        "run_id": str(uuid.uuid4()),
        "base_subtitle_revision": int(project["subtitle_revision"]),
        "windows": selected,
    }


@app.get("/api/health")
def health() -> dict:
    codex_available = command_available(settings.codex_cli)
    grok_available = command_available(settings.grok_cli)
    configured_cli_available = any(
        (provider == "codex" and codex_available)
        or (provider in {"grok", "grokbuild"} and grok_available)
        or provider in {"manual", "copy"}
        for provider in settings.translation_providers
    )
    return {
        "ok": True,
        "subtitle_style": default_subtitle_style(),
        "dependencies": {
            "ffmpeg": command_available(settings.ffmpeg_bin),
            "ffprobe": command_available(settings.ffprobe_bin),
            "yt_dlp": command_available("yt-dlp"),
            "node": command_available("node"),
            "whisperx": importlib.util.find_spec("whisperx") is not None,
            "codex_cli": codex_available,
            "grok_cli": grok_available,
            "translation_cli": configured_cli_available,
        },
        "translation_providers": settings.translation_providers,
        "codex_model": settings.codex_model or "cli-default",
        "codex_reasoning_effort": settings.codex_reasoning_effort or "cli-default",
        "grok_model": settings.grok_model or "cli-default",
    }


@app.get("/api/projects")
def list_projects() -> list[dict]:
    return repository.list_projects()


@app.get("/api/codex-models")
def codex_models() -> dict:
    try:
        return {"models": list_codex_models()}
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as exc:
        return {"models": [], "error": str(exc)[-300:]}


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate) -> dict:
    source_url = validate_youtube_url(payload.source_url)
    project = repository.create_project(
        source_url=source_url,
        target_language=payload.target_language,
        max_height=payload.max_height,
        bilingual=payload.bilingual,
        translation_provider=payload.translation_provider,
        codex_model=payload.codex_model,
    )
    repository.enqueue_job(project["id"], "prepare")
    return repository.get_project(project["id"])


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    project = require_project(project_id)
    project["segment_count"] = len(repository.list_segments(project_id))
    project["token_count"] = len(repository.list_timed_tokens(project_id))
    project["subtitle_parts"] = repository.list_subtitle_parts(project_id)
    project["provider_usage"] = repository.provider_usage(project_id)
    project["render_is_stale"] = (
        project["rendered_revision"] is not None
        and project["rendered_revision"] != project["subtitle_revision"]
    )
    return project


@app.patch("/api/projects/{project_id}/global-translation-context")
def update_global_translation_context(
    project_id: str, payload: GlobalContextUpdate
) -> dict:
    require_global_context_review(project_id)
    context = normalize_global_context(payload)
    repository.update_project(project_id, global_translation_context=context)
    return context


@app.post("/api/projects/{project_id}/global-translation-context/approve")
def approve_global_translation_context(
    project_id: str, payload: GlobalContextUpdate
) -> dict:
    project = require_global_context_review(project_id)
    context = normalize_global_context(payload)
    partial = isinstance(project.get("pending_rebuild_scope"), dict)
    repository.update_project(
        project_id,
        global_translation_context=context,
        global_context_approved=1,
        status="queued",
        stage="partial_rebuild_queued" if partial else "queued",
        progress=68,
        error=None,
    )
    job = repository.enqueue_job(project_id, "partial_rebuild" if partial else "translate")
    return {"context": context, "job": job}


@app.patch("/api/projects/{project_id}/name")
def rename_project(project_id: str, payload: ProjectRename) -> dict:
    project = require_project(project_id)
    display_name = payload.display_name.strip() or None
    repository.update_project(project_id, display_name=display_name)
    return {
        "display_name": display_name,
        "effective_name": display_name or project.get("title") or "Waiting for video metadata",
    }


@app.patch("/api/projects/{project_id}/translation-provider")
def update_translation_provider(project_id: str, payload: TranslationProviderUpdate) -> dict:
    project = require_project(project_id)
    if project["status"] in {"queued", "running"}:
        raise HTTPException(
            status_code=409,
            detail="Translation provider cannot change while the project is processing",
        )
    repository.update_project(project_id, translation_provider=payload.translation_provider)
    return {"translation_provider": payload.translation_provider}


@app.patch("/api/projects/{project_id}/codex-model")
def update_codex_model(project_id: str, payload: CodexModelUpdate) -> dict:
    project = require_project(project_id)
    if project["status"] in {"queued", "running"}:
        raise HTTPException(
            status_code=409,
            detail="Codex model cannot change while the project is processing",
        )
    repository.update_project(project_id, codex_model=payload.codex_model)
    return {"codex_model": payload.codex_model}


@app.post("/api/projects/{project_id}/retry")
def retry_project(project_id: str) -> dict:
    project = require_project(project_id)
    if project["status"] not in {"failed", "waiting_for_quota"}:
        raise HTTPException(
            status_code=409, detail="Only failed or quota-paused projects can be retried"
        )
    if isinstance(project.get("pending_rebuild_scope"), dict):
        kind = "partial_rebuild"
        repository.update_project(
            project_id,
            status="queued",
            stage="partial_rebuild_queued",
            progress=0,
            error=None,
        )
        return repository.enqueue_job(project_id, kind)
    segments = repository.list_segments(project_id)
    tokens = repository.list_timed_tokens(project_id)
    if project.get("video_path") and tokens:
        kind = "translate"
    elif project.get("video_path") and segments:
        kind = (
            "translate"
            if any(not segment["translated_text"].strip() for segment in segments)
            else "render"
        )
    else:
        kind = "prepare"
    repository.update_project(project_id, status="queued", stage="queued", progress=0, error=None)
    return repository.enqueue_job(project_id, kind)


@app.get("/api/projects/{project_id}/rebuild-windows")
def list_rebuild_windows(project_id: str) -> dict:
    project = require_project(project_id)
    tokens = repository.list_timed_tokens(project_id)
    parts = repository.list_subtitle_parts(project_id)
    if not tokens:
        return {"parts": [], "selected_rebuild_available": False}
    result = []
    for part in parts:
        windows = []
        for window_number, batch in enumerate(_active_batches(part), start=1):
            start = int(batch["start_token_id"])
            end = int(batch["end_token_id"])
            preview = " ".join(str(token["text"]) for token in tokens[start : min(end + 1, start + 18)])
            windows.append(
                {
                    "batch_id": int(batch["id"]),
                    "window_number": window_number,
                    "start_token_id": start,
                    "end_token_id": end,
                    "start_ms": int(tokens[start]["start_ms"]) if start < len(tokens) else None,
                    "end_ms": int(tokens[end]["end_ms"]) if end < len(tokens) else None,
                    "preview": preview,
                }
            )
        result.append(
            {
                "part_id": int(part["id"]),
                "part_number": int(part["sequence"]) + 1,
                "start_token_id": int(part["start_token_id"]),
                "end_token_id": int(part["end_token_id"]),
                "windows": windows,
            }
        )
    return {"parts": result, "selected_rebuild_available": bool(tokens and parts)}


@app.post("/api/projects/{project_id}/rebuild-subtitles")
def rebuild_subtitles(project_id: str, payload: SubtitleRebuildRequest) -> dict:
    project = require_project(project_id)
    if project["status"] in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="The project is currently processing")
    if not project.get("video_path") or not Path(project["video_path"]).exists():
        raise HTTPException(status_code=409, detail="The project has no downloaded video")
    if payload.mode == "selected":
        scope = build_partial_rebuild_scope(project, payload.windows)
        repository.begin_partial_rebuild(project_id, scope)
        return repository.enqueue_job(project_id, "partial_rebuild")
    # Rebuild is destructive by design. Clear the previous subtitle pipeline state
    # synchronously so the API response never leaves stale cues visible while the
    # replacement job waits for a worker.
    repository.clear_subtitle_data(project_id)
    return repository.enqueue_job(project_id, "rebuild_subtitles")


@app.get("/api/projects/{project_id}/segments")
def list_segments(project_id: str) -> list[dict]:
    require_project(project_id)
    return repository.list_segments(project_id)


@app.get("/api/projects/{project_id}/review-issues")
def list_review_issues(project_id: str) -> list[dict]:
    require_project(project_id)
    segments = repository.list_segments(project_id)
    stored = repository.list_review_issues(project_id)
    result: list[dict] = []
    covered_timing_segment_ids: set[int] = set()

    for issue in stored:
        start = int(issue["start_token_id"])
        end = int(issue["end_token_id"])
        affected = [
            segment
            for segment in segments
            if segment.get("start_token_id") is not None
            and segment.get("end_token_id") is not None
            and int(segment["start_token_id"]) <= end
            and int(segment["end_token_id"]) >= start
        ]
        if issue["action"] == "review_timing":
            covered_timing_segment_ids.update(int(segment["id"]) for segment in affected)
        replacements = []
        for replacement in issue["replacement_cues"]:
            matching = next(
                (
                    segment
                    for segment in segments
                    if segment.get("start_token_id") == replacement.get("start_token_id")
                    and segment.get("end_token_id") == replacement.get("end_token_id")
                ),
                None,
            )
            replacements.append(
                {
                    **replacement,
                    "segment_id": int(matching["id"]) if matching else None,
                }
            )
        result.append(
            {
                "id": f"pass3-{issue['id']}",
                "source": "pass3",
                "action": issue["action"],
                "issue_types": issue["issue_types"],
                "severity": issue["severity"],
                "reason": issue["reason"],
                "start_token_id": start,
                "end_token_id": end,
                "replacement_cues": replacements,
                "affected_segment_ids": [int(segment["id"]) for segment in affected],
                "auto_applied": issue["auto_applied"],
            }
        )

    for segment in segments:
        problems = quality_issues(
            int(segment["start_ms"]),
            int(segment["end_ms"]),
            str(segment["source_text"]),
            str(segment["translated_text"]),
        )
        if not problems or int(segment["id"]) in covered_timing_segment_ids:
            continue
        descriptions = ", ".join(
            f"{problem['type'].replace('_', ' ')} ({problem['actual']} / {problem['limit']})"
            for problem in problems
        )
        result.append(
            {
                "id": f"quality-{segment['id']}",
                "source": "quality",
                "action": "review_timing",
                "issue_types": [problem["type"] for problem in problems],
                "severity": "high"
                if any(
                    problem["type"] in {"target_text_too_long", "very_long_duration"}
                    for problem in problems
                )
                else "medium",
                "reason": f"Manual timing or text adjustment recommended: {descriptions}.",
                "start_token_id": segment.get("start_token_id"),
                "end_token_id": segment.get("end_token_id"),
                "replacement_cues": [],
                "affected_segment_ids": [int(segment["id"])],
                "auto_applied": False,
            }
        )

    return sorted(
        result,
        key=lambda issue: (
            issue["start_token_id"] if issue["start_token_id"] is not None else 10**12,
            issue["end_token_id"] if issue["end_token_id"] is not None else 10**12,
            issue["id"],
        ),
    )


@app.patch("/api/projects/{project_id}/subtitle-style")
def update_subtitle_style(project_id: str, payload: SubtitleStyleUpdate) -> dict:
    project = require_project_not_rendering(project_id)
    style = merge_subtitle_style(project["subtitle_style"])
    values = payload.model_dump()
    style["chinese"]["size"] = values["chinese_subtitle_size"]
    style["english"]["size"] = values["english_subtitle_size"]
    if values["chinese_subtitle_color"] is not None:
        style["chinese"]["color"] = values["chinese_subtitle_color"].upper()
    if values["english_subtitle_color"] is not None:
        style["english"]["color"] = values["english_subtitle_color"].upper()
    style["chinese"]["outline"] = values["chinese_subtitle_outline"]
    style["english"]["outline"] = values["english_subtitle_outline"]
    style["layout"]["bottom_margin"] = values["subtitle_bottom_margin"]
    style["layout"]["line_gap"] = values["subtitle_line_gap"]
    style["layout"]["horizontal_margin"] = values["subtitle_horizontal_margin"]
    style["bottom_mask"]["height"] = values["bottom_mask_height"]
    style["bottom_mask"]["opacity"] = values["bottom_mask_opacity"]
    saved_style = repository.set_subtitle_style(project_id, style)
    if values["show_english_subtitles"] is not None:
        repository.update_project(project_id, bilingual=int(values["show_english_subtitles"]))
    return {
        "subtitle_style": saved_style,
        "bilingual": bool(
            project["bilingual"]
            if values["show_english_subtitles"] is None
            else values["show_english_subtitles"]
        ),
    }


@app.post("/api/projects/{project_id}/exact-preview")
def exact_preview(project_id: str, payload: ExactPreviewRequest) -> Response:
    project = require_project_not_rendering(project_id)
    video_path = Path(str(project.get("video_path") or ""))
    if not video_path.exists():
        raise HTTPException(status_code=409, detail="The project has no downloaded video")
    segment = repository.get_segment(project_id, payload.segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Subtitle not found")
    timestamp_ms = min(
        payload.timestamp_ms,
        max(0, int(project.get("duration_ms") or payload.timestamp_ms + 1) - 1),
    )
    try:
        content = render_exact_preview(
            project_id,
            video_path,
            segment,
            timestamp_ms,
            bool(project["bilingual"]),
            project["subtitle_style"],
        )
    except PreviewCancelled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/projects/{project_id}/exact-preview/cancel")
def cancel_exact_preview(project_id: str) -> dict:
    require_project(project_id)
    return {"cancelled": preview_processes.cancel(project_id)}


@app.patch("/api/projects/{project_id}/segments/{segment_id}")
def update_segment(project_id: str, segment_id: int, payload: SegmentUpdate) -> dict:
    require_project_not_rendering(project_id)
    current = repository.get_segment(project_id, segment_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Subtitle not found")
    fields = payload.model_dump(exclude_unset=True)
    start_ms = int(fields.get("start_ms", current["start_ms"]))
    end_ms = int(fields.get("end_ms", current["end_ms"]))
    if end_ms <= start_ms:
        raise HTTPException(status_code=422, detail="End time must be later than start time")
    try:
        return repository.update_segment(project_id, segment_id, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="Subtitle not found") from None


@app.post("/api/projects/{project_id}/segments/{segment_id}/split")
def split_segment(project_id: str, segment_id: int, payload: SplitRequest) -> dict:
    require_project_not_rendering(project_id)
    try:
        repository.split_segment(project_id, segment_id, payload.at_ms)
    except KeyError:
        raise HTTPException(status_code=404, detail="Subtitle not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True}


@app.post("/api/projects/{project_id}/segments/{segment_id}/merge-next")
def merge_next(project_id: str, segment_id: int) -> dict:
    require_project_not_rendering(project_id)
    try:
        repository.merge_with_next(project_id, segment_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Subtitle not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True}


@app.post("/api/projects/{project_id}/approve-and-render")
def approve_and_render(project_id: str) -> dict:
    project = require_project(project_id)
    if project["status"] not in {"waiting_review", "completed", "failed"}:
        raise HTTPException(
            status_code=409, detail="The project cannot be rendered in its current state"
        )
    parts = repository.list_subtitle_parts(project_id)
    if parts and any(part["status"] != "completed" for part in parts):
        raise HTTPException(
            status_code=409,
            detail="All subtitle parts must finish translating before the full video can render",
        )
    segments = repository.list_segments(project_id)
    problems = validate_segments(segments)
    if problems:
        raise HTTPException(status_code=422, detail=problems)
    preview_processes.cancel(project_id)
    for segment in segments:
        if segment["status"] != "reviewed":
            repository.update_segment(project_id, segment["id"], status="reviewed")
    repository.update_project(
        project_id, status="queued", stage="render_queued", progress=0, error=None
    )
    return repository.enqueue_job(project_id, "render")


@app.get("/api/projects/{project_id}/video")
def project_video(project_id: str) -> FileResponse:
    project = require_project(project_id)
    if not project.get("video_path") or not Path(project["video_path"]).exists():
        raise HTTPException(status_code=404, detail="The video has not finished downloading")
    media_type = mimetypes.guess_type(project["video_path"])[0] or "application/octet-stream"
    return FileResponse(project["video_path"], media_type=media_type)


@app.post("/api/projects/{project_id}/open-output-folder")
def open_output_folder(project_id: str) -> dict:
    require_project(project_id)
    output_root = settings.output_dir.resolve()
    project_output = (output_root / project_id).resolve()
    if not project_output.is_relative_to(output_root):
        raise HTTPException(status_code=400, detail="Invalid project output path")
    project_output.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        raise HTTPException(
            status_code=501, detail="Opening output folders is currently supported on Windows"
        )
    subprocess.Popen(
        ["explorer.exe", str(project_output)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "path": str(project_output)}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
