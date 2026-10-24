from __future__ import annotations

import io
import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image, ImageChops

from app import publication_api
from app.config import settings
from app.db import connect, init_db
from app.repository import Repository
from app.services import publication_backgrounds, publication_copy
from app.services.publication_cover import CoverDesign, render_cover


def test_existing_publication_table_adds_background_columns(tmp_path: Path) -> None:
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE publication_workspaces (
               project_id TEXT PRIMARY KEY, copy_status TEXT NOT NULL DEFAULT 'queued',
               copy_error TEXT, source_srt TEXT NOT NULL, source_url TEXT NOT NULL,
               titles_json TEXT NOT NULL DEFAULT '[]', intros_json TEXT NOT NULL DEFAULT '[]',
               selected_title TEXT NOT NULL DEFAULT '', selected_intro TEXT NOT NULL DEFAULT '',
               provider TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
    init_db(database)
    with connect(database) as connection:
        names = {
            row[1] for row in connection.execute("PRAGMA table_info(publication_workspaces)")
        }
    assert {
        "reference_path", "reference_revision", "background_status",
        "background_error", "background_generation_id",
        "thumbnail_titles_json", "thumbnail_status", "thumbnail_error",
        "selected_thumbnail_title",
    } <= names
    with connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'publication_cover_styles'"
        ).fetchone() is not None


def _setup(tmp_path: Path, monkeypatch) -> tuple[Repository, str, dict]:
    configured = replace(
        settings, data_dir=tmp_path / "data", output_dir=tmp_path / "output"
    )
    monkeypatch.setattr(publication_api, "settings", configured)
    monkeypatch.setattr(publication_copy, "settings", configured)
    monkeypatch.setattr(publication_backgrounds, "settings", configured)
    database = tmp_path / "publication.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://www.youtube.com/watch?v=real123")
    project_id = project["id"]
    output = configured.output_dir / project_id
    output.mkdir(parents=True)
    (output / "final-v001.mp4").write_bytes(b"completed video")
    (output / "chinese-v001.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n这是开头\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n这是视频末尾\n",
        encoding="utf-8",
    )
    router = publication_api.create_router(lambda: repository)
    endpoints = {(route.path.rsplit("/publication", 1)[-1], method): route.endpoint
                 for route in router.routes for method in route.methods}
    return repository, project_id, endpoints


def test_publication_copy_starts_once_and_keeps_exact_footer(tmp_path: Path, monkeypatch) -> None:
    repository, project_id, endpoints = _setup(tmp_path, monkeypatch)
    first = endpoints["/start", "POST"](project_id)
    second = endpoints["/start", "POST"](project_id)
    assert first["copy_status"] == second["copy_status"] == "queued"
    with connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id = ? AND kind = 'generate_publication_copy'",
            (project_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id = ? AND kind = 'generate_thumbnail_titles'",
            (project_id,),
        ).fetchone()[0] == 1

    prompts: list[str] = []

    def fake_call(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return {
            "titles": [f"标题 {index}" for index in range(1, 7)],
            "intros": [f"简介 {index}。这是值得思考的问题。" for index in range(1, 4)],
        }, "codex"

    monkeypatch.setattr(publication_copy, "_call_providers", fake_call)
    publication_copy.generate_publication_copy(project_id, repository)
    state = endpoints["", "GET"](project_id)
    assert "这是开头" in prompts[0] and "这是视频末尾" in prompts[0]
    assert len(state["titles"]) == 6 and len(state["intros"]) == 3
    assert all(title.endswith("【中英字幕】") for title in state["titles"])
    assert state["final_intro"] == (
        "简介 1。这是值得思考的问题。\n\n"
        "原视频：https://www.youtube.com/watch?v=real123\n\n"
        "翻译 / 时间轴 / 校对：静里见明 + LLMs\n\n"
        "本视频内容版权归原作者所有。\n"
        "All rights to the video content belong to the original creator."
    )
    edited = endpoints["/copy", "PATCH"](
        project_id,
        publication_api.SelectedCopy(title="人工修改标题", intro="人工修改的简介正文。"),
    )
    assert edited["selected_title"] == "人工修改标题【中英字幕】"
    assert edited["final_intro"].startswith("人工修改的简介正文。\n\n原视频：")
    output = tmp_path / "output" / project_id
    (output / "final-v002.mp4").write_bytes(b"new render")
    (output / "chinese-v002.srt").write_text("new subtitle", encoding="utf-8")
    endpoints["/start", "POST"](project_id)
    assert len(prompts) == 1
    assert endpoints["", "GET"](project_id)["selected_title"] == "人工修改标题【中英字幕】"


def test_five_thumbnail_text_candidates_are_saved_and_existing_copy_is_preserved(
    tmp_path: Path, monkeypatch
) -> None:
    repository, project_id, endpoints = _setup(tmp_path, monkeypatch)
    endpoints["/start", "POST"](project_id)
    prompts: list[str] = []

    def fake_call(prompt, schema_path, *_args, **_kwargs):
        assert schema_path.name == "publication-thumbnail-titles.schema.json"
        prompts.append(prompt)
        return {"thumbnail_titles": [
            "每天30分钟够吗", "少做一点会怎样", "练习时间的疑问", "三十分钟之后", "时间短也有效吗",
        ]}, "codex"

    monkeypatch.setattr(publication_copy, "_call_providers", fake_call)
    publication_copy.generate_thumbnail_titles(project_id, repository)
    state = endpoints["", "GET"](project_id)
    assert state["thumbnail_status"] == "completed"
    assert len(state["thumbnail_titles"]) == 5
    assert "这是开头" in prompts[0] and "这是视频末尾" in prompts[0]
    changed = endpoints["/thumbnail-title", "PATCH"](
        project_id, publication_api.SelectedThumbnailTitle(title="自己改的短句"),
    )
    assert changed["selected_thumbnail_title"] == "自己改的短句"
    endpoints["/start", "POST"](project_id)
    assert len(prompts) == 1
    assert endpoints["", "GET"](project_id)["selected_thumbnail_title"] == "自己改的短句"
    with connect(repository.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id = ? AND kind = 'generate_thumbnail_titles'",
            (project_id,),
        ).fetchone()[0] == 1


def test_two_uploaded_covers_keep_independent_designs_and_exports(
    tmp_path: Path, monkeypatch
) -> None:
    _repository, project_id, endpoints = _setup(tmp_path, monkeypatch)
    endpoints["/start", "POST"](project_id)

    class ImageRequest:
        def __init__(self, size: tuple[int, int]):
            image = Image.new("RGB", size, "#887766")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            self.data = buffer.getvalue()
            self.headers = {"content-length": str(len(self.data))}

        async def body(self):
            return self.data

    upload = endpoints["/covers/{ratio}/image", "PUT"]
    for ratio, size in (("16x9", (960, 540)), ("4x3", (960, 720))):
        coroutine = upload(project_id, ratio, ImageRequest(size))
        try:
            coroutine.send(None)
        except StopIteration:
            pass
    save_design = endpoints["/covers/{ratio}/design", "PATCH"]
    confirm_title = endpoints["/covers/{ratio}/title", "PATCH"]
    confirm_title(project_id, "16x9", publication_api.SelectedCoverTitle(title="十天之后\n会怎样？"))
    save_design(project_id, "16x9", CoverDesign(title="十天之后\n会怎样？", font_size=106))
    confirm_title(project_id, "4x3", publication_api.SelectedCoverTitle(title="十天\n之后"))
    save_design(project_id, "4x3", CoverDesign(title="十天\n之后", font_size=150, y_pct=35))
    result = endpoints["/render", "POST"](project_id)
    assert result["covers"]["16x9"]["has_export"]
    assert result["covers"]["4x3"]["has_export"]
    assert all(cover["export_in_output"] for cover in result["covers"].values())
    output_dir = tmp_path / "output" / project_id
    first_4x3 = (output_dir / "thumbnail-4x3.png").read_bytes()
    assert (output_dir / "final-v001.mp4").read_bytes() == b"completed video"
    for ratio, size in (("16x9", (1536, 864)), ("4x3", (1536, 1152))):
        response = endpoints["/covers/{ratio}/export", "GET"](project_id, ratio)
        assert Path(response.path) == output_dir / f"thumbnail-{ratio}.png"
        with Image.open(response.path) as exported:
            assert exported.size == size
    confirm_title(project_id, "4x3", publication_api.SelectedCoverTitle(title="另一句标题"))
    changed = endpoints["", "GET"](project_id)
    assert changed["covers"]["4x3"]["export_stale"]
    assert not changed["covers"]["16x9"]["export_stale"]
    rerendered = endpoints["/render", "POST"](project_id)
    assert not rerendered["covers"]["4x3"]["export_stale"]
    assert (output_dir / "thumbnail-4x3.png").read_bytes() != first_4x3


def test_cover_styles_follow_ratio_and_explicit_line_count(tmp_path: Path, monkeypatch) -> None:
    repository, project_id, endpoints = _setup(tmp_path, monkeypatch)
    endpoints["/start", "POST"](project_id)
    confirm = endpoints["/covers/{ratio}/title", "PATCH"]
    save = endpoints["/covers/{ratio}/design", "PATCH"]

    def title(ratio: str, value: str) -> dict:
        return confirm(project_id, ratio, publication_api.SelectedCoverTitle(title=value))

    assert title("16x9", "一行标题")["covers"]["16x9"]["line_count"] == 1
    save(project_id, "16x9", CoverDesign(title="一行标题", font_size=90, x_pct=8))
    two = title("16x9", "两行\n标题")["covers"]["16x9"]
    assert two["line_count"] == 2
    assert two["design"]["font_size"] == 90 and two["design"]["x_pct"] == 8
    save(project_id, "16x9", CoverDesign(title="两行\n标题", font_size=130, x_pct=12))
    three = title("16x9", "这次\n是三行\n标题")["covers"]["16x9"]
    assert three["line_count"] == 3 and three["design"]["font_size"] == 130
    save(project_id, "16x9", CoverDesign(title="这次\n是三行\n标题", font_size=150))
    assert title("16x9", "换成一行")["covers"]["16x9"]["design"]["font_size"] == 90
    assert title("16x9", "再回\n两行")["covers"]["16x9"]["design"]["font_size"] == 130
    four = title("16x9", "一\n二\n三\n四")["covers"]["16x9"]
    assert four["line_count"] == 4 and four["design"]["font_size"] == 130
    save(project_id, "16x9", CoverDesign(title="一\n二\n三\n四", font_size=62, x_pct=16))
    five = title("16x9", "一\n二\n三\n四\n五")["covers"]["16x9"]
    assert five["line_count"] == 4 and five["title_line_count"] == 5
    assert five["design"]["font_size"] == 62 and five["design"]["x_pct"] == 16
    save(project_id, "16x9", CoverDesign(title="一\n二\n三\n四\n五", font_size=58, x_pct=19))
    assert title("16x9", "一\n二\n三\n四")["covers"]["16x9"]["design"]["font_size"] == 58
    seven = title("16x9", "一\n二\n三\n四\n五\n六\n七")["covers"]["16x9"]
    assert seven["line_count"] == 4 and seven["title_line_count"] == 7
    assert seven["design"]["font_size"] == 58
    assert title("16x9", "再回\n两行")["covers"]["16x9"]["design"]["font_size"] == 130

    other = title("4x3", "四比三\n两行")["covers"]["4x3"]
    assert other["design"]["font_size"] == 114
    save(project_id, "4x3", CoverDesign(title="四比三\n两行", font_size=177))
    assert title("4x3", "四比三\n新的两行")["covers"]["4x3"]["design"]["font_size"] == 177
    other_five = title("4x3", "一\n二\n三\n四\n五")["covers"]["4x3"]
    assert other_five["line_count"] == 4 and other_five["title_line_count"] == 5
    assert other_five["design"]["font_size"] == 114
    save(project_id, "4x3", CoverDesign(title="一\n二\n三\n四\n五", font_size=70))
    assert title("4x3", "一\n二\n三\n四")["covers"]["4x3"]["design"]["font_size"] == 70
    assert endpoints["", "GET"](project_id)["covers"]["16x9"]["design"]["font_size"] == 130
    with pytest.raises(HTTPException) as mismatch:
        save(project_id, "16x9", CoverDesign(title="未经确认的标题", font_size=99))
    assert mismatch.value.status_code == 409
    with connect(repository.database_path) as connection:
        slots = connection.execute(
            "SELECT ratio, line_count, style_json FROM publication_cover_styles "
            "WHERE project_id = ? ORDER BY ratio, line_count", (project_id,),
        ).fetchall()
    assert {(row["ratio"], row["line_count"]) for row in slots} == {
        ("16x9", 1), ("16x9", 2), ("16x9", 3), ("16x9", 4),
        ("4x3", 1), ("4x3", 2), ("4x3", 3), ("4x3", 4),
    }
    assert all('"title"' not in row["style_json"] for row in slots)
    init_db(repository.database_path)
    assert title("16x9", "另一行")["covers"]["16x9"]["design"]["font_size"] == 90


def test_new_workbench_inherits_previous_cover_styles_without_copying_content(
    tmp_path: Path, monkeypatch
) -> None:
    repository, source_id, endpoints = _setup(tmp_path, monkeypatch)
    start = endpoints["/start", "POST"]
    confirm = endpoints["/covers/{ratio}/title", "PATCH"]
    save = endpoints["/covers/{ratio}/design", "PATCH"]
    start(source_id)
    for ratio, size in (("16x9", 164), ("4x3", 194)):
        confirm(source_id, ratio, publication_api.SelectedCoverTitle(title="旧标题\n第二行"))
        save(source_id, ratio, CoverDesign(
            title="旧标题\n第二行", highlight_terms=["旧标题"], font_size=size,
            line_spacing=1.31, x_pct=6, y_pct=21, shadow_blur=40,
        ))

    target_id = repository.create_project("https://www.youtube.com/watch?v=new123")["id"]
    output = publication_api.settings.output_dir / target_id
    output.mkdir(parents=True)
    (output / "final-v001.mp4").write_bytes(b"completed video")
    (output / "chinese-v001.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n新字幕\n", encoding="utf-8"
    )
    opened = start(target_id)
    for ratio, size in (("16x9", 164), ("4x3", 194)):
        cover = opened["covers"][ratio]
        assert cover["design"]["title"] == ""
        assert cover["design"]["highlight_terms"] == []
        assert not cover["has_image"]
        assert cover["design"]["font_size"] == size
        selected = confirm(
            target_id, ratio, publication_api.SelectedCoverTitle(title="新标题\n第二行")
        )["covers"][ratio]
        assert selected["line_count"] == 2
        assert selected["design"]["font_size"] == size
        assert selected["design"]["highlight_terms"] == []
        assert selected["design"]["shadow_blur"] == 40

    with connect(repository.database_path) as connection:
        presets = connection.execute(
            "SELECT style_json FROM publication_cover_styles WHERE project_id = ?",
            (target_id,),
        ).fetchall()
    assert presets and all(json.loads(row["style_json"])["highlight_terms"] == []
                           for row in presets)

    save(target_id, "16x9", CoverDesign(title="新标题\n第二行", font_size=80))
    assert start(target_id)["covers"]["16x9"]["design"]["font_size"] == 80
    assert endpoints["", "GET"](source_id)["covers"]["16x9"]["design"]["font_size"] == 164


def test_16x9_title_and_highlights_sync_to_4x3_without_copying_layout(
    tmp_path: Path, monkeypatch
) -> None:
    _, project_id, endpoints = _setup(tmp_path, monkeypatch)
    endpoints["/start", "POST"](project_id)
    confirm = endpoints["/covers/{ratio}/title", "PATCH"]
    save = endpoints["/covers/{ratio}/design", "PATCH"]

    first = confirm(
        project_id, "16x9", publication_api.SelectedCoverTitle(title="第一行\n第二行")
    )
    assert first["covers"]["4x3"]["design"]["title"] == "第一行\n第二行"
    save(project_id, "4x3", CoverDesign(
        title="第一行\n第二行", font_size=191, x_pct=11, highlight_terms=["第二行"]
    ))
    updated = save(project_id, "16x9", CoverDesign(
        title="第一行\n第二行", font_size=161, highlight_terms=["第一行"]
    ))
    other = updated["covers"]["4x3"]
    assert other["design"]["title"] == "第一行\n第二行"
    assert other["design"]["highlight_terms"] == ["第一行"]
    assert other["design"]["font_size"] == 191 and other["design"]["x_pct"] == 11
    assert updated["covers"]["16x9"]["design"]["font_size"] == 161

    three = confirm(
        project_id, "16x9", publication_api.SelectedCoverTitle(title="一\n二\n三")
    )
    assert three["covers"]["4x3"]["design"]["title"] == "一\n二\n三"
    assert three["covers"]["4x3"]["design"]["highlight_terms"] == \
        three["covers"]["16x9"]["design"]["highlight_terms"]
    assert three["covers"]["4x3"]["design"]["font_size"] == 191

    direct = confirm(
        project_id, "4x3", publication_api.SelectedCoverTitle(title="四比三单独修改")
    )
    assert direct["covers"]["16x9"]["design"]["title"] == "一\n二\n三"


def test_shadow_is_visible_without_a_stroke_around_the_text(tmp_path: Path) -> None:
    design = CoverDesign(
        title="影", font_size=160, mask_opacity=0, shadow_opacity=1,
        shadow_blur=0, shadow_dx=3, shadow_dy=5,
    ).model_dump()
    image_path = tmp_path / "white.png"
    Image.new("RGB", (1536, 864), "#ffffff").save(image_path)
    text_only = Image.open(io.BytesIO(render_cover(
        image_path, "16x9", {**design, "shadow_opacity": 0, "text_color": "#000000"}
    )))
    shadow_only = Image.open(io.BytesIO(render_cover(
        image_path, "16x9", {**design, "text_color": "#ffffff"}
    )))
    white = Image.new("RGB", shadow_only.size, "#ffffff")
    text_bounds = ImageChops.difference(text_only, white).getbbox()
    shadow_bounds = ImageChops.difference(shadow_only, white).getbbox()
    assert text_bounds is not None and shadow_bounds is not None
    assert shadow_bounds[0] >= text_bounds[0] + 12
    assert shadow_bounds[1] >= text_bounds[1] + 20

    blurred = Image.open(io.BytesIO(render_cover(
        image_path, "16x9", {**design, "shadow_blur": 40}
    )))
    difference = ImageChops.difference(blurred, white)
    assert difference.getchannel("R").getextrema()[1] >= 100


def test_shadow_strength_above_100_extends_the_directional_cast(tmp_path: Path) -> None:
    image_path = tmp_path / "white.png"
    Image.new("RGB", (1536, 864), "#ffffff").save(image_path)
    white = Image.new("RGB", (1536, 864), "#ffffff")
    bounds = []
    for strength in (1, 2, 3):
        rendered = Image.open(io.BytesIO(render_cover(image_path, "16x9", {
            **CoverDesign(
                title="影", font_size=160, mask_opacity=0, shadow_blur=0,
                shadow_dx=3, shadow_dy=5, text_color="#ffffff",
            ).model_dump(),
            "shadow_opacity": strength,
        })))
        bounds.append(ImageChops.difference(rendered, white).getbbox())
    assert all(bound is not None for bound in bounds)
    assert bounds[0][2] < bounds[1][2] < bounds[2][2]
    assert bounds[0][3] < bounds[1][3] < bounds[2][3]


def test_old_cover_design_ignores_removed_shadow_fields_for_preview(
    tmp_path: Path, monkeypatch
) -> None:
    repository, project_id, endpoints = _setup(tmp_path, monkeypatch)
    endpoints["/start", "POST"](project_id)
    image_path = tmp_path / "legacy-cover.png"
    Image.new("RGB", (1536, 864), "#333333").save(image_path)
    legacy = CoverDesign(title="旧封面标题").model_dump()
    legacy["shadow_spread"] = 10
    legacy["shadow_glow"] = 0.7
    with connect(repository.database_path) as connection:
        connection.execute(
            """INSERT INTO publication_covers
               (project_id, ratio, image_path, design_json, updated_at)
               VALUES (?, '16x9', ?, ?, '2026-01-01')""",
            (project_id, str(image_path), json.dumps(legacy, ensure_ascii=False)),
        )
    design = endpoints["", "GET"](project_id)["covers"]["16x9"]["design"]
    assert "shadow_spread" not in design
    assert "shadow_glow" not in design
    response = endpoints["/covers/{ratio}/preview", "POST"](
        project_id, "16x9", CoverDesign.model_validate(design)
    )
    assert response.body.startswith(b"\x89PNG\r\n\x1a\n")


def test_reference_generates_two_model_backgrounds_and_preserves_old_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    repository, project_id, endpoints = _setup(tmp_path, monkeypatch)
    endpoints["/start", "POST"](project_id)

    class ImageRequest:
        def __init__(self, color: str):
            image = Image.new("RGB", (1100, 700), color)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            self.data = buffer.getvalue()
            self.headers = {"content-length": str(len(self.data))}

        async def body(self):
            return self.data

    def upload_reference(color: str) -> dict:
        coroutine = endpoints["/reference", "PUT"](project_id, ImageRequest(color))
        try:
            coroutine.send(None)
        except StopIteration as result:
            return result.value
        raise AssertionError("Reference upload did not finish")

    first = upload_reference("#624e38")
    assert first["has_reference"]
    reference_path = Path(endpoints["/reference", "GET"](project_id).path)
    assert reference_path.is_file()
    queued = endpoints["/generate-backgrounds", "POST"](project_id)
    assert queued["background_status"] == "queued"
    with pytest.raises(HTTPException) as duplicate:
        endpoints["/generate-backgrounds", "POST"](project_id)
    assert duplicate.value.status_code == 409
    with pytest.raises(HTTPException) as replacement:
        upload_reference("#ffffff")
    assert replacement.value.status_code == 409

    calls: list[tuple[tuple[Path, ...], str]] = []

    def fake_codex(references: tuple[Path, ...], destination: Path, ratio: str, _model: str) -> None:
        calls.append((references, ratio))
        assert "$imagegen" in publication_backgrounds._prompt(ratio, destination)
        assert "programmatic crop" in publication_backgrounds._prompt(ratio, destination)
        size = (1600, 900) if ratio == "16x9" else (1200, 900)
        Image.new("RGB", size, "#556677" if ratio == "4x3" else "#101010").save(destination)

    monkeypatch.setattr(publication_backgrounds, "_run_codex", fake_codex)
    publication_backgrounds.generate_publication_backgrounds(project_id, repository)
    with connect(repository.database_path) as connection:
        first_job = connection.execute(
            "SELECT id FROM jobs WHERE project_id = ? AND kind = 'generate_publication_backgrounds' "
            "ORDER BY created_at LIMIT 1", (project_id,),
        ).fetchone()["id"]
    repository.complete_job(first_job)
    generated = endpoints["", "GET"](project_id)
    assert generated["background_status"] == "completed"
    assert calls[0] == ((reference_path,), "4x3")
    assert calls[1][1] == "16x9"
    assert calls[1][0][0].name == "guide-16x9.png"
    assert calls[1][0][1].name == "base-4x3.png"
    assert calls[1][0][2] == reference_path
    with Image.open(calls[1][0][0]) as guide, Image.open(calls[1][0][1]) as master:
        assert guide.size == (1536, 864)
        center = master.resize((1152, 864), Image.Resampling.LANCZOS)
        assert guide.crop((192, 0, 1344, 864)).tobytes() == center.tobytes()
    assert "PRIMARY visual source" in publication_backgrounds._prompt("16x9", Path("out.png"))
    assert "MASTER composition" in publication_backgrounds._prompt("4x3", Path("out.png"))
    for ratio, size in (("16x9", (1536, 864)), ("4x3", (1536, 1152))):
        path = Path(endpoints["/covers/{ratio}/image", "GET"](project_id, ratio).path)
        with Image.open(path) as image:
            assert image.size == size
            if ratio == "16x9":
                assert image.getpixel((700, 500)) == (85, 102, 119)
                assert image.getpixel((20, 500)) == (16, 16, 16)
            else:
                assert image.getpixel((700, 500)) == (85, 102, 119)
    paths = {
        ratio: endpoints["/covers/{ratio}/image", "GET"](project_id, ratio).path
        for ratio in ("16x9", "4x3")
    }

    # A later failed attempt must leave both previously accepted backgrounds in place.
    endpoints["/generate-backgrounds", "POST"](project_id)

    def failing_codex(_references: tuple[Path, ...], destination: Path, ratio: str, _model: str) -> None:
        if ratio == "4x3":
            Image.new("RGB", (1200, 900), "#ffffff").save(destination)
        else:
            raise RuntimeError(f"image tool unavailable for {ratio}")

    monkeypatch.setattr(publication_backgrounds, "_run_codex", failing_codex)
    with pytest.raises(RuntimeError, match="image tool unavailable"):
        publication_backgrounds.generate_publication_backgrounds(project_id, repository)
    publication_backgrounds.mark_background_failed(project_id, repository, "image tool unavailable")
    failed = endpoints["", "GET"](project_id)
    assert failed["background_status"] == "failed"
    assert failed["background_error"] == "image tool unavailable"
    assert all(failed["covers"][ratio]["has_image"] for ratio in paths)
    assert {
        ratio: endpoints["/covers/{ratio}/image", "GET"](project_id, ratio).path
        for ratio in paths
    } == paths

    updated = upload_reference("#112233")
    assert updated["background_status"] == "idle"
    assert updated["reference_revision"] == first["reference_revision"] + 1


def test_codex_background_call_attaches_image_and_allows_project_output(
    tmp_path: Path, monkeypatch
) -> None:
    reference = tmp_path / "reference.png"
    Image.new("RGB", (960, 540), "#112233").save(reference)
    destination = tmp_path / "generated" / "base-4x3.png"
    captured: dict = {}

    def fake_command(command: list[str], prompt: str):
        captured.update(command=command, prompt=prompt)
        Image.new("RGB", (1536, 1152), "#445566").save(destination)
        return subprocess.CompletedProcess(command, 0, "completed", "")

    monkeypatch.setattr(publication_backgrounds, "command_available", lambda _path: True)
    monkeypatch.setattr(publication_backgrounds, "_run_command", fake_command)
    publication_backgrounds._run_codex((reference,), destination, "4x3", "")
    assert captured["command"][:4] == [
        settings.codex_cli, "exec", "--ephemeral", "--sandbox"
    ]
    assert "workspace-write" in captured["command"]
    assert captured["command"][captured["command"].index("--image") + 1] == str(reference)
    assert captured["command"][captured["command"].index("-C") + 1] == str(destination.parent)
    assert captured["prompt"].startswith("$imagegen")
    assert str(destination) in captured["prompt"]
    second = tmp_path / "generated" / "base-16x9.png"
    guide = tmp_path / "generated" / "guide-16x9.png"
    publication_backgrounds._make_companion_guide(destination, guide)
    publication_backgrounds._run_codex((guide, destination, reference), second, "16x9", "")
    images = [
        captured["command"][index + 1]
        for index, part in enumerate(captured["command"]) if part == "--image"
    ]
    assert images == [str(guide), str(destination), str(reference)]


def test_16x9_companion_keeps_all_four_quadrants_of_4x3_master(tmp_path: Path) -> None:
    master_path = tmp_path / "master-4x3.png"
    companion_path = tmp_path / "companion-16x9.png"
    master = Image.new("RGB", (1536, 1152))
    colors = {
        (0, 0): (255, 0, 0), (1, 0): (0, 255, 0),
        (0, 1): (0, 0, 255), (1, 1): (255, 255, 0),
    }
    for x in range(2):
        for y in range(2):
            master.paste(colors[x, y], (x * 768, y * 576, (x + 1) * 768, (y + 1) * 576))
    master.save(master_path)
    Image.new("RGB", (1536, 864), "#101010").save(companion_path)

    publication_backgrounds._preserve_master_center(master_path, companion_path)
    with Image.open(companion_path) as image:
        assert image.getpixel((50, 400)) == (16, 16, 16)
        assert image.getpixel((1480, 400)) == (16, 16, 16)
        assert image.getpixel((350, 100)) == colors[0, 0]
        assert image.getpixel((1180, 100)) == colors[1, 0]
        assert image.getpixel((350, 750)) == colors[0, 1]
        assert image.getpixel((1180, 750)) == colors[1, 1]


def test_long_publication_source_reads_every_subtitle_before_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    repository, project_id, endpoints = _setup(tmp_path, monkeypatch)
    srt = tmp_path / "output" / project_id / "chinese-v001.srt"
    srt.write_text(
        "\n\n".join(
            f"{index}\n00:00:01,000 --> 00:00:02,000\n第{index}段独有内容"
            for index in range(1, 81)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(publication_copy, "CHUNK_CHAR_LIMIT", 300)
    endpoints["/start", "POST"](project_id)
    summary_prompts: list[str] = []

    def fake_call(prompt, schema_path, *_args, **_kwargs):
        if schema_path.name == "publication-summary.schema.json":
            summary_prompts.append(prompt)
            return {"summary": "本段内容摘要"}, "codex"
        return {
            "titles": [f"标题 {index}" for index in range(6)],
            "intros": [f"简介 {index}" for index in range(3)],
        }, "codex"

    monkeypatch.setattr(publication_copy, "_call_providers", fake_call)
    publication_copy.generate_publication_copy(project_id, repository)
    combined = "\n".join(summary_prompts)
    assert "第1段独有内容" in combined
    assert "第80段独有内容" in combined
    assert endpoints["", "GET"](project_id)["copy_status"] == "completed"
