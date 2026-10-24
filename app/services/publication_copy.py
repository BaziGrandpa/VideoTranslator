from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import connect, transaction
from app.repository import Repository, now_iso
from app.services.prompts import SECURITY_BLOCK
from app.services.subtitle_planner import _call_providers

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
TITLE_SUFFIX = "【中英字幕】"
INTRO_FOOTER = (
    "\n\n原视频：{source_url}\n\n"
    "翻译 / 时间轴 / 校对：静里见明 + LLMs\n\n"
    "本视频内容版权归原作者所有。\n"
    "All rights to the video content belong to the original creator."
)
SRT_TIME_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+")
CHUNK_CHAR_LIMIT = 26000


def title_with_suffix(value: str) -> str:
    body = value.strip()
    while body.endswith(TITLE_SUFFIX):
        body = body.removesuffix(TITLE_SUFFIX).strip()
    if not body or "\n" in body or len(body + TITLE_SUFFIX) > 200:
        raise ValueError("Enter a valid video title")
    return body + TITLE_SUFFIX


def latest_output_srt(project_id: str) -> Path | None:
    """Find the latest complete, nonempty render pair, including after a subtitle rebuild."""
    output_dir = settings.output_dir / project_id
    if not output_dir.is_dir():
        return None
    versions: list[tuple[int, Path]] = []
    for video in output_dir.glob("final-v*.mp4"):
        match = re.fullmatch(r"final-v(\d+)\.mp4", video.name, re.IGNORECASE)
        if match is None or not video.is_file() or video.stat().st_size == 0:
            continue
        srt = output_dir / f"chinese-v{match.group(1)}.srt"
        if srt.is_file() and srt.stat().st_size > 0:
            versions.append((int(match.group(1)), srt))
    return max(versions, key=lambda item: item[0])[1] if versions else None


def read_complete_srt(path: Path) -> str:
    """Keep every subtitle text line while removing only SRT indices and timing rows."""
    raw = path.read_text(encoding="utf-8-sig")
    lines: list[str] = []
    for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n")):
        content = [line.strip() for line in block.splitlines() if line.strip()]
        if not content:
            continue
        if content[0].isdigit():
            content = content[1:]
        if content and SRT_TIME_RE.match(content[0]):
            content = content[1:]
        lines.extend(content)
    if not lines:
        raise ValueError("The exported Chinese subtitle file is empty")
    return "\n".join(lines)


def _provider_order(project: dict[str, Any]) -> tuple[str, ...]:
    mode = str(project.get("translation_provider") or "auto")
    if mode == "codex":
        return ("codex",)
    if mode == "grok":
        return ("grok",)
    return settings.translation_providers


def _transcript_chunks(transcript: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in transcript.splitlines():
        if current and length + len(line) + 1 > CHUNK_CHAR_LIMIT:
            chunks.append("\n".join(current))
            current = []
            length = 0
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _validated_candidates(result: dict[str, Any]) -> tuple[list[str], list[str]]:
    titles = result.get("titles")
    intros = result.get("intros")
    if not isinstance(titles, list) or len(titles) != 6:
        raise ValueError("The CLI must return exactly six titles")
    if not isinstance(intros, list) or len(intros) != 3:
        raise ValueError("The CLI must return exactly three intros")
    clean_titles: list[str] = []
    for value in titles:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("The CLI returned an empty title")
        clean_titles.append(title_with_suffix(value))
    clean_intros: list[str] = []
    for value in intros:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("The CLI returned an empty intro")
        body = value.strip().replace("\r\n", "\n")
        if len(body) > 2000:
            raise ValueError("The CLI returned an intro that is too long")
        if any(marker in body for marker in ("原视频：", "翻译 / 时间轴", "All rights to")):
            raise ValueError("The CLI put the fixed footer in an intro candidate")
        clean_intros.append(body)
    if len(set(clean_titles)) != 6 or len(set(clean_intros)) != 3:
        raise ValueError("The CLI returned duplicate publication candidates")
    return clean_titles, clean_intros


def _condense_transcript(
    transcript: str, providers: tuple[str, ...], model: str, audit_dir: Path
) -> str:
    source = transcript
    level = 0
    while len(source) > CHUNK_CHAR_LIMIT:
        level += 1
        chunks = _transcript_chunks(source)
        summaries: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            prompt = (
                "Summarize this entire sequential section of video content in Chinese, "
                "using at most 1200 Chinese characters. "
                "Preserve concrete actions, people, numbers, conditions, uncertainty, and the "
                "speaker's personal perspective. Do not invent facts or universalize anecdotes. "
                "This summary will be combined with other sections to write publication copy. "
                "Return JSON with one summary string.\n\n"
                f"{SECURITY_BLOCK}\n\nSECTION {index}/{len(chunks)}\n{chunk}"
            )
            result, _provider = _call_providers(
                prompt, SCHEMA_DIR / "publication-summary.schema.json", providers, model,
                audit_dir / f"summary-level-{level:02d}-part-{index:02d}",
                stage="publication-summary",
            )
            summary = result.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError(f"The CLI returned an empty summary for section {index}")
            if len(summary) > 2500:
                raise ValueError(f"The CLI summary for section {index} is too long")
            summaries.append(f"SECTION {index}/{len(chunks)}\n{summary.strip()}")
        next_source = "\n\n".join(summaries)
        if len(next_source) >= len(source):
            raise ValueError("The CLI summaries did not reduce the transcript length")
        source = next_source
    return source


def generate_publication_copy(project_id: str, repository: Repository) -> None:
    project = repository.get_project(project_id)
    if project is None:
        raise KeyError(project_id)
    with connect(repository.database_path) as connection:
        row = connection.execute(
            "SELECT * FROM publication_workspaces WHERE project_id = ?", (project_id,)
        ).fetchone()
    if row is None or row["copy_status"] == "completed":
        return
    with transaction(repository.database_path) as connection:
        connection.execute(
            "UPDATE publication_workspaces SET copy_status = 'running', copy_error = NULL, "
            "updated_at = ? WHERE project_id = ?", (now_iso(), project_id)
        )
    transcript = read_complete_srt(Path(row["source_srt"]))
    providers = _provider_order(project)
    model = str(project.get("codex_model") or settings.codex_model)
    audit_dir = settings.data_dir / "projects" / project_id / "artifacts" / "publication"
    source = _condense_transcript(transcript, providers, model, audit_dir)
    prompt = (
        "You are the editor of a restrained Chinese Bilibili channel. Read all supplied "
        "subtitle content before writing. Return exactly six distinct natural Chinese video "
        "titles and three concise Chinese intro bodies as JSON. Titles must accurately reflect "
        "the video's concrete subject, without clickbait, unsupported claims, or invented facts. "
        "Each intro body should usually be two sentences, at most three, about 50–100 Chinese "
        "characters: first say what the video actually covers, then raise its real question or "
        "tension. Do not present personal experience as a general scientific conclusion. "
        "Do not spoil the final conclusion, add hashtags or emoji, or write a copyright footer. "
        "Do not add the title suffix; the application adds it. Return only JSON matching the schema.\n\n"
        f"{SECURITY_BLOCK}\n\nORIGINAL VIDEO LINK: {row['source_url']}\n"
        f"VIDEO TITLE: {project.get('title') or ''}\n"
        f"BEGIN COMPLETE SUBTITLE CONTENT\n{source}\nEND COMPLETE SUBTITLE CONTENT"
    )
    result, provider = _call_providers(
        prompt, SCHEMA_DIR / "publication-copy.schema.json", providers, model,
        audit_dir / "candidates", stage="publication-copy",
    )
    titles, intros = _validated_candidates(result)
    with transaction(repository.database_path) as connection:
        connection.execute(
            """UPDATE publication_workspaces SET copy_status = 'completed', copy_error = NULL,
               titles_json = ?, intros_json = ?, selected_title = ?, selected_intro = ?,
               provider = ?, updated_at = ? WHERE project_id = ?""",
            (json.dumps(titles, ensure_ascii=False), json.dumps(intros, ensure_ascii=False),
             titles[0], intros[0], provider, now_iso(), project_id),
        )


def mark_copy_failed(project_id: str, repository: Repository, error: str) -> None:
    with transaction(repository.database_path) as connection:
        connection.execute(
            "UPDATE publication_workspaces SET copy_status = 'failed', copy_error = ?, "
            "updated_at = ? WHERE project_id = ?",
            (error, now_iso(), project_id),
        )


def _validated_thumbnail_titles(result: dict[str, Any]) -> list[str]:
    values = result.get("thumbnail_titles")
    if not isinstance(values, list) or len(values) != 5:
        raise ValueError("The CLI must return exactly five cover text candidates")
    titles: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError("The CLI returned an invalid cover text candidate")
        title = value.strip()
        if not 2 <= len(title) <= 24 or "\n" in title or TITLE_SUFFIX in title:
            raise ValueError("Each cover text candidate must have 2–24 characters on one line")
        titles.append(title)
    if len(set(titles)) != 5:
        raise ValueError("The CLI returned duplicate cover text candidates")
    return titles


def generate_thumbnail_titles(project_id: str, repository: Repository) -> None:
    project = repository.get_project(project_id)
    if project is None:
        raise KeyError(project_id)
    with connect(repository.database_path) as connection:
        row = connection.execute(
            "SELECT * FROM publication_workspaces WHERE project_id = ?", (project_id,)
        ).fetchone()
    if row is None or row["thumbnail_status"] == "completed":
        return
    with transaction(repository.database_path) as connection:
        connection.execute(
            "UPDATE publication_workspaces SET thumbnail_status = 'running', "
            "thumbnail_error = NULL, updated_at = ? WHERE project_id = ?",
            (now_iso(), project_id),
        )
    transcript = read_complete_srt(Path(row["source_srt"]))
    providers = _provider_order(project)
    model = str(project.get("codex_model") or settings.codex_model)
    audit_dir = (
        settings.data_dir / "projects" / project_id / "artifacts" / "publication" / "thumbnail-titles"
    )
    source = _condense_transcript(transcript, providers, model, audit_dir)
    prompt = (
        "You write concise Chinese text for large type on Bilibili video thumbnails. "
        "Read the supplied video subtitles and return exactly five distinct thumbnail text "
        "options as JSON. Each option should be a short, natural, attention-catching phrase "
        "of 2–24 Chinese characters, shorter than a normal video title and suitable for "
        "manual line breaks in a large cover layout. Vary the angle or emphasis. Stay true "
        "to the actual video; do not invent outcomes, numbers, guarantees, or universalize "
        "the speaker's personal experience. Use curiosity or a real tension without clickbait. "
        "Do not include the subtitle suffix, hashtags, emoji, or line breaks. "
        "Return only JSON matching the schema.\n\n"
        f"{SECURITY_BLOCK}\n\nVIDEO TITLE: {project.get('title') or ''}\n"
        f"SELECTED CHINESE VIDEO TITLE: {str(row['selected_title'] or '').removesuffix(TITLE_SUFFIX)}\n"
        f"BEGIN COMPLETE SUBTITLE CONTENT\n{source}\nEND COMPLETE SUBTITLE CONTENT"
    )
    result, _provider = _call_providers(
        prompt, SCHEMA_DIR / "publication-thumbnail-titles.schema.json", providers, model,
        audit_dir / "candidates", stage="publication-thumbnail-titles",
    )
    titles = _validated_thumbnail_titles(result)
    with transaction(repository.database_path) as connection:
        connection.execute(
            """UPDATE publication_workspaces SET thumbnail_status = 'completed',
               thumbnail_error = NULL, thumbnail_titles_json = ?,
               selected_thumbnail_title = ?, updated_at = ? WHERE project_id = ?""",
            (json.dumps(titles, ensure_ascii=False), titles[0], now_iso(), project_id),
        )


def mark_thumbnail_titles_failed(project_id: str, repository: Repository, error: str) -> None:
    with transaction(repository.database_path) as connection:
        connection.execute(
            "UPDATE publication_workspaces SET thumbnail_status = 'failed', "
            "thumbnail_error = ?, updated_at = ? WHERE project_id = ?",
            (error, now_iso(), project_id),
        )
