from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.db import connect, init_db
from app.repository import Repository


def test_create_project_and_edit_segment(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)

    response = client.post(
        "/api/projects",
        json={"source_url": "https://youtu.be/example", "target_language": "zh-CN"},
    )
    assert response.status_code == 201
    assert response.json()["bilingual"] is True
    project_id = response.json()["id"]
    repository.replace_segments(
        project_id,
        [
            {
                "start_ms": 1000,
                "end_ms": 2000,
                "source_text": "Hello",
                "target_text": "Hello translated",
            }
        ],
    )
    segment = repository.list_segments(project_id)[0]
    edited = client.patch(
        f"/api/projects/{project_id}/segments/{segment['id']}",
        json={"start_ms": 1100, "target_text": "Edited translation"},
    )
    assert edited.status_code == 200
    assert edited.json()["start_ms"] == 1100
    assert edited.json()["target_text"] == "Edited translation"
    boosted = client.patch(
        f"/api/projects/{project_id}/segments/{segment['id']}",
        json={"extra_line_gap": True},
    )
    assert boosted.status_code == 200
    assert boosted.json()["extra_line_gap"] is True
    assert client.get(f"/api/projects/{project_id}/segments").json()[0]["extra_line_gap"] is True


def test_timing_updates_share_neighbor_boundaries_and_reject_zero_duration(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_segments(
        project["id"],
        [
            {"start_ms": 1000, "end_ms": 2000, "source_text": "One", "target_text": "一"},
            {"start_ms": 2000, "end_ms": 3000, "source_text": "Two", "target_text": "二"},
            {
                "start_ms": 3000,
                "end_ms": 4000,
                "source_text": "Three",
                "target_text": "三",
            },
        ],
    )
    first, second, third = repository.list_segments(project["id"])

    moved_start = client.patch(
        f"/api/projects/{project['id']}/segments/{second['id']}/timing",
        json={"boundary": "start", "value_ms": 2200},
    )
    assert moved_start.status_code == 200
    saved = repository.list_segments(project["id"])
    assert saved[0]["end_ms"] == 2200
    assert saved[1]["start_ms"] == 2200

    nudged_start = client.patch(
        f"/api/projects/{project['id']}/segments/{second['id']}/timing",
        json={"boundary": "start", "delta_ms": -100},
    )
    assert nudged_start.status_code == 200
    saved = repository.list_segments(project["id"])
    assert saved[0]["end_ms"] == 2100
    assert saved[1]["start_ms"] == 2100

    independent_start = client.patch(
        f"/api/projects/{project['id']}/segments/{second['id']}/timing",
        json={"boundary": "start", "value_ms": 2300, "sync_neighbor": False},
    )
    assert independent_start.status_code == 200
    saved = repository.list_segments(project["id"])
    assert saved[0]["end_ms"] == 2100
    assert saved[1]["start_ms"] == 2300

    independent_end = client.patch(
        f"/api/projects/{project['id']}/segments/{second['id']}/timing",
        json={"boundary": "end", "value_ms": 3100, "sync_neighbor": False},
    )
    assert independent_end.status_code == 200
    saved = repository.list_segments(project["id"])
    assert saved[1]["end_ms"] == 3100
    assert saved[2]["start_ms"] == 3000

    moved_end = client.patch(
        f"/api/projects/{project['id']}/segments/{second['id']}",
        json={"end_ms": 3200},
    )
    assert moved_end.status_code == 200
    saved = repository.list_segments(project["id"])
    assert saved[1]["end_ms"] == 3200
    assert saved[2]["start_ms"] == 3200

    invalid_start = client.patch(
        f"/api/projects/{project['id']}/segments/{second['id']}/timing",
        json={"boundary": "start", "value_ms": first["start_ms"]},
    )
    assert invalid_start.status_code == 422
    invalid_end = client.patch(
        f"/api/projects/{project['id']}/segments/{second['id']}/timing",
        json={"boundary": "end", "value_ms": third["end_ms"]},
    )
    assert invalid_end.status_code == 422
    unchanged = repository.list_segments(project["id"])
    assert [(item["start_ms"], item["end_ms"]) for item in unchanged] == [
        (1000, 2100),
        (2300, 3200),
        (3200, 4000),
    ]


def test_rejects_non_youtube_url(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    monkeypatch.setattr(main_module, "repository", Repository(database))
    client = TestClient(main_module.app)
    response = client.post("/api/projects", json={"source_url": "https://example.com/video"})
    assert response.status_code == 422


def test_project_display_name_can_be_changed_without_replacing_video_title(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(project["id"], title="Original YouTube title")
    client = TestClient(main_module.app)

    renamed = client.patch(
        f"/api/projects/{project['id']}/name",
        json={"display_name": "My publishing project"},
    )

    assert renamed.status_code == 200
    assert renamed.json()["effective_name"] == "My publishing project"
    saved = client.get(f"/api/projects/{project['id']}").json()
    assert saved["display_name"] == "My publishing project"
    assert saved["title"] == "Original YouTube title"

    reset = client.patch(f"/api/projects/{project['id']}/name", json={"display_name": "   "})
    assert reset.status_code == 200
    assert reset.json()["display_name"] is None
    assert reset.json()["effective_name"] == "Original YouTube title"


def test_delete_project_removes_database_and_both_file_trees(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(main_module.settings, data_dir=tmp_path / "data", output_dir=tmp_path / "output"),
    )
    project = repository.create_project("https://youtu.be/example")
    other = repository.create_project("https://youtu.be/other")
    job = repository.enqueue_job(project["id"], "prepare")
    repository.claim_next_job()
    repository.complete_job(job["id"])
    repository.update_project(project["id"], status="waiting_review", stage="waiting_review")
    repository.replace_tokens(
        project["id"], [{"text": "Hello", "start_ms": 0, "end_ms": 1000}]
    )
    repository.initialize_subtitle_plan(
        project["id"],
        [{"start_token_id": 0, "end_token_id": 0, "start_ms": 0, "end_ms": 1000}],
    )
    part_id = repository.list_parts(project["id"])[0]["id"]
    repository.replace_cues(
        project["id"],
        part_id,
        [{
            "cue_id": 1,
            "start_token_id": 0,
            "end_token_id": 0,
            "start_ms": 0,
            "end_ms": 1000,
            "source_text": "Hello",
        }],
    )
    repository.begin_part_punctuation(project["id"], part_id, "fingerprint", "codex")
    repository.replace_segments(
        project["id"],
        [{"start_ms": 0, "end_ms": 1000, "source_text": "Hello"}],
    )
    source = tmp_path / "data" / "projects" / project["id"] / "source"
    source.mkdir(parents=True)
    (source / "video.mp4").write_bytes(b"video")
    output = tmp_path / "output" / project["id"]
    output.mkdir(parents=True)
    (output / "final-v001.mp4").write_bytes(b"render")
    (output / "english-v001.srt").write_text("subtitle")
    other_output = tmp_path / "output" / other["id"]
    other_output.mkdir()
    (other_output / "keep.txt").write_text("keep")

    client = TestClient(main_module.app)
    response = client.delete(f"/api/projects/{project['id']}")

    assert response.status_code == 200
    assert repository.get_project(project["id"]) is None
    assert repository.list_segments(project["id"]) == []
    assert not source.parent.exists()
    assert not output.exists()
    assert (other_output / "keep.txt").read_text() == "keep"
    assert client.delete(f"/api/projects/{project['id']}").status_code == 404
    with connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM projects WHERE id = ?", (project["id"],)
        ).fetchone()[0] == 0
        for table in (
            "jobs", "tokens", "parts", "cues", "segments",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (project["id"],)
            ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM part_punctuation").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM punctuation_marks").fetchone()[0] == 0


def test_delete_project_rejects_queued_and_running_jobs(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(
        main_module,
        "settings",
        replace(main_module.settings, data_dir=tmp_path / "data", output_dir=tmp_path / "output"),
    )
    project = repository.create_project("https://youtu.be/example")
    project_files = tmp_path / "data" / "projects" / project["id"]
    project_files.mkdir(parents=True)
    (project_files / "keep.txt").write_text("keep")
    repository.enqueue_job(project["id"], "prepare")
    client = TestClient(main_module.app)

    assert client.delete(f"/api/projects/{project['id']}").status_code == 409
    repository.claim_next_job()
    assert client.delete(f"/api/projects/{project['id']}").status_code == 409
    assert repository.get_project(project["id"]) is not None
    assert (project_files / "keep.txt").read_text() == "keep"


def test_project_subtitle_style_can_be_saved(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")

    response = client.patch(
        f"/api/projects/{project['id']}/subtitle-style",
        json={
            "show_english_subtitles": False,
            "chinese_subtitle_size": 46,
            "english_subtitle_size": 28,
            "chinese_subtitle_color": "#fefefe",
            "english_subtitle_color": "#a1b2c3",
            "chinese_subtitle_outline": 3.5,
            "english_subtitle_outline": 2,
            "subtitle_bottom_margin": 36,
            "subtitle_line_gap": 70,
            "subtitle_horizontal_margin": 100,
            "bottom_mask_height": 0.4,
            "bottom_mask_opacity": 0.8,
        },
    )

    assert response.status_code == 200
    style = response.json()["subtitle_style"]
    assert style["chinese"]["size"] == 46
    assert style["chinese"]["color"] == "#FEFEFE"
    assert style["english"]["color"] == "#A1B2C3"
    assert style["bottom_mask"]["opacity"] == 0.8
    assert response.json()["bilingual"] is False
    saved = client.get(f"/api/projects/{project['id']}").json()
    assert saved["subtitle_style"] == style
    assert saved["bilingual"] is False
    assert saved["subtitle_revision"] == 1

    created = client.post(
        "/api/projects",
        json={"source_url": "https://youtu.be/next-project"},
    )
    assert created.status_code == 201
    assert created.json()["subtitle_style"] == style
    assert created.json()["bilingual"] is False


def test_rebuild_clears_old_subtitle_state_before_queueing(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(
        project["id"],
        status="waiting_review",
        stage="waiting_review",
        progress=95,
        video_path=str(video_path),
        output_path=str(tmp_path / "old-output.mp4"),
        translation_context={"overview": "Old", "style": "Old", "terms": []},
        context_provider="codex",
        context_approved=1,
    )
    repository.replace_tokens(
        project["id"],
        [{"text": "Hello", "start_ms": 0, "end_ms": 1000, "source": "caption"}],
    )
    repository.initialize_subtitle_plan(
        project["id"],
        [
            {
                "start_token_id": 0,
                "end_token_id": 0,
                "start_ms": 0,
                "end_ms": 1000,
            }
        ],
    )
    part = repository.list_parts(project["id"])[0]
    repository.replace_part_segments(
        project["id"],
        part["id"],
        [
            {
                "start_ms": 0,
                "end_ms": 1000,
                "start_token_id": 0,
                "end_token_id": 0,
                "source_text": "Hello",
                "target_text": "你好",
            }
        ],
    )
    response = client.post(f"/api/projects/{project['id']}/rebuild-subtitles", json={})

    assert response.status_code == 200
    assert response.json()["kind"] == "rebuild_subtitles"
    assert response.json()["status"] == "queued"
    assert repository.list_segments(project["id"]) == []
    assert repository.list_tokens(project["id"]) == []
    assert repository.list_parts(project["id"]) == []
    rebuilt = repository.get_project(project["id"])
    assert rebuilt is not None
    assert rebuilt["status"] == "queued"
    assert rebuilt["stage"] == "queued"
    assert rebuilt["progress"] == 0
    assert rebuilt["translation_context"] is None
    assert rebuilt["context_approved"] is False
    assert rebuilt["output_path"] is None
    assert rebuilt["video_path"] == str(video_path)


def test_full_rebuild_can_preserve_existing_pass0_context(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    context = {"overview": "Existing context", "style": "Natural", "terms": []}
    repository.update_project(
        project["id"],
        video_path=str(video),
        translation_context=context,
        context_provider="codex",
        context_approved=1,
        status="waiting_review",
    )

    availability = client.get(f"/api/projects/{project['id']}/rebuild-parts")
    assert availability.status_code == 200
    assert availability.json()["pass0_available"] is True

    response = client.post(
        f"/api/projects/{project['id']}/rebuild-subtitles",
        json={"mode": "full", "skip_pass0": True},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "rebuild_subtitles_skip_pass0"
    rebuilt = repository.get_project(project["id"])
    assert rebuilt["translation_context"] == context
    assert rebuilt["context_provider"] == "codex"
    assert rebuilt["context_approved"] is True


def test_full_rebuild_rejects_skip_pass0_without_existing_context(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    repository.update_project(
        project["id"], video_path=str(video), status="waiting_review"
    )

    response = client.post(
        f"/api/projects/{project['id']}/rebuild-subtitles",
        json={"mode": "full", "skip_pass0": True},
    )

    assert response.status_code == 409
    assert "no existing context" in response.json()["detail"]


def test_selected_part_rebuild_keeps_pass0_and_visible_cues(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    context = {"overview": "Keep", "style": "Natural", "terms": []}
    repository.update_project(
        project["id"],
        video_path=str(video),
        translation_context=context,
        context_provider="codex",
        context_approved=1,
        status="waiting_review",
    )
    repository.replace_tokens(
        project["id"],
        [{"text": "Hello.", "start_ms": 0, "end_ms": 1000, "source": "test"}],
    )
    repository.initialize_subtitle_plan(
        project["id"],
        [{"start_token_id": 0, "end_token_id": 0, "start_ms": 0, "end_ms": 1000}],
    )
    part = repository.list_parts(project["id"])[0]
    repository.replace_cues(
        project["id"],
        part["id"],
        [
            {
                "cue_id": 1,
                "start_token_id": 0,
                "end_token_id": 0,
                "start_ms": 0,
                "end_ms": 1000,
                "source_text": "Hello.",
                "target_text": "你好。",
            }
        ],
    )
    repository.replace_part_segments(
        project["id"],
        part["id"],
        [
            {
                "start_ms": 0,
                "end_ms": 1000,
                "start_token_id": 0,
                "end_token_id": 0,
                "cue_id": 1,
                "subcue_id": 0,
                "source_text": "Hello.",
                "target_text": "你好。",
            }
        ],
    )

    response = client.post(
        f"/api/projects/{project['id']}/rebuild-subtitles",
        json={"mode": "selected", "part_ids": [part["id"]]},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "part_rebuild"
    rebuilt = repository.get_project(project["id"])
    assert rebuilt["translation_context"] == context
    assert rebuilt["context_approved"] is True
    assert rebuilt["pending_rebuild_scope"]["parts"][0]["part_id"] == part["id"]
    assert repository.list_segments(project["id"])[0]["target_text"] == "你好。"


def test_review_issues_are_live_segment_problems_without_severity(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    project = repository.create_project("https://youtu.be/example")
    repository.initialize_subtitle_plan(
        project["id"],
        [
            {
                "start_token_id": 0,
                "end_token_id": 2,
                "start_ms": 0,
                "end_ms": 7000,
            }
        ],
    )
    part = repository.list_parts(project["id"])[0]
    repository.replace_part_segments(
        project["id"],
        part["id"],
        [
            {
                "start_ms": 0,
                "end_ms": 7000,
                "start_token_id": 0,
                "end_token_id": 0,
                "source_text": "I just did something crazy.",
                "target_text": "我刚做了一件疯狂的事",
            },
            {
                "start_ms": 7000,
                "end_ms": 8200,
                "start_token_id": 1,
                "end_token_id": 2,
                "source_text": "Hello there.",
                "target_text": "你好",
                "quality_flags": ["pass3_invalid_output"],
                "status": "needs_review",
            },
        ],
    )

    response = TestClient(main_module.app).get(f"/api/projects/{project['id']}/review-issues")

    assert response.status_code == 200
    issues = response.json()
    assert all("severity" not in issue for issue in issues)
    assert all("replacement_cues" not in issue for issue in issues)
    by_type = {issue["issue_types"][0]: issue for issue in issues}
    assert "very_long_duration" in by_type
    assert "pass3_invalid_output" in by_type
    assert by_type["pass3_invalid_output"]["reason"] == "Pass 3 could not split this cue."


def test_exact_preview_returns_ffmpeg_frame(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    project = repository.create_project("https://youtu.be/example")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"test video placeholder")
    repository.update_project(project["id"], video_path=str(video), duration_ms=5000, bilingual=1)
    repository.replace_segments(
        project["id"],
        [
            {
                "start_ms": 1000,
                "end_ms": 2000,
                "source_text": "Exact preview",
                "target_text": "精确预览",
            }
        ],
    )
    captured = {}

    def fake_render(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return b"\x89PNG\r\n\x1a\npreview"

    monkeypatch.setattr(main_module, "render_exact_preview", fake_render)
    response = TestClient(main_module.app).post(
        f"/api/projects/{project['id']}/exact-preview",
        json={"timestamp_ms": 3000},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\x89PNG")
    assert captured["args"][0] == project["id"]
    assert captured["args"][3] == 3000
    assert len(captured["args"][2]) == 1
    assert captured["kwargs"]["cards"] == []


def test_final_render_locks_project_edits_and_exact_preview(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    project = repository.create_project("https://youtu.be/example")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"test video placeholder")
    repository.update_project(
        project["id"],
        status="running",
        stage="rendering",
        video_path=str(video),
        duration_ms=5000,
    )
    repository.replace_segments(
        project["id"],
        [
            {
                "start_ms": 1000,
                "end_ms": 2000,
                "source_text": "Locked",
                "target_text": "已锁定",
            }
        ],
    )
    segment = repository.list_segments(project["id"])[0]
    client = TestClient(main_module.app)

    edit = client.patch(
        f"/api/projects/{project['id']}/segments/{segment['id']}",
        json={"target_text": "Should not save"},
    )
    style = client.patch(
        f"/api/projects/{project['id']}/subtitle-style",
        json={
            "chinese_subtitle_size": 46,
            "english_subtitle_size": 28,
            "chinese_subtitle_outline": 3.5,
            "english_subtitle_outline": 2,
            "subtitle_bottom_margin": 36,
            "subtitle_line_gap": 70,
            "subtitle_horizontal_margin": 100,
            "bottom_mask_height": 0.4,
            "bottom_mask_opacity": 0.8,
        },
    )
    preview = client.post(
        f"/api/projects/{project['id']}/exact-preview",
        json={"segment_id": segment["id"], "timestamp_ms": 1500},
    )

    assert edit.status_code == 409
    assert style.status_code == 409
    assert preview.status_code == 409
    assert "locked" in edit.json()["detail"]


def test_project_translation_provider_can_be_selected(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example", translation_provider="codex")
    repository.update_project(project["id"], status="completed")

    response = client.patch(
        f"/api/projects/{project['id']}/translation-provider",
        json={"translation_provider": "grok"},
    )

    assert response.status_code == 200
    saved = client.get(f"/api/projects/{project['id']}").json()
    assert saved["translation_provider"] == "grok"
    assert saved["provider_usage"] == {"codex": 0, "grok": 0}

    model_response = client.patch(
        f"/api/projects/{project['id']}/codex-model",
        json={"codex_model": "gpt-5.6-luna"},
    )
    assert model_response.status_code == 200
    assert repository.get_project(project["id"])["codex_model"] == "gpt-5.6-luna"


def test_pass0_context_can_be_edited_and_approved_before_translation(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(
        project["id"],
        status="waiting_context_review",
        stage="waiting_context_review",
        progress=68,
        translation_context={
            "overview": "Initial overview",
            "style": "Initial style",
            "terms": [],
        },
        context_provider="codex",
        context_approved=0,
    )
    payload = {
        "overview": "  A revised project overview.  ",
        "style": " Natural spoken Chinese. ",
        "terms": [
            {
                "source": "  Katie ",
                "target": " 凯蒂 ",
                "category": "person",
                "must_use": False,
            }
        ],
    }

    saved = client.patch(
        f"/api/projects/{project['id']}/translation-context", json=payload
    )

    assert saved.status_code == 200
    assert saved.json()["overview"] == "A revised project overview."
    draft_project = repository.get_project(project["id"])
    assert draft_project["status"] == "waiting_context_review"
    assert draft_project["context_approved"] is False

    approved = client.post(
        f"/api/projects/{project['id']}/translation-context/approve", json=payload
    )

    assert approved.status_code == 200
    assert approved.json()["job"]["kind"] == "translate"
    approved_project = repository.get_project(project["id"])
    assert approved_project["status"] == "queued"
    assert approved_project["stage"] == "queued"
    assert approved_project["context_approved"] is True
    assert approved_project["translation_context"]["terms"][0] == {
        "source": "Katie",
        "target": "凯蒂",
        "category": "person",
        "must_use": False,
        "occurrence_count": 0,
    }


def test_pass0_context_rejects_duplicate_source_terms(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(
        project["id"],
        status="waiting_context_review",
        stage="waiting_context_review",
        translation_context={"overview": "Overview", "style": "", "terms": []},
    )

    response = client.patch(
        f"/api/projects/{project['id']}/translation-context",
        json={
            "overview": "Overview",
            "style": "",
            "terms": [
                {"source": "Dopamine", "target": "多巴胺", "category": "technical", "must_use": True},
                {"source": "dopamine", "target": "多巴胺", "category": "technical", "must_use": False},
            ],
        },
    )

    assert response.status_code == 422
    assert "Duplicate terminology source" in response.json()["detail"]


def test_retry_after_translation_failure_does_not_redownload(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(
        project["id"], status="failed", stage="failed", video_path="already-downloaded.mp4"
    )
    repository.replace_segments(
        project["id"],
        [
            {
                "start_ms": 0,
                "end_ms": 1000,
                "source_text": "Keep this transcript",
                "target_text": "",
            }
        ],
    )

    response = client.post(f"/api/projects/{project['id']}/retry", json={})
    assert response.status_code == 200
    assert response.json()["kind"] == "translate"


def test_open_output_folder_is_scoped_to_project(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "api.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    monkeypatch.setattr(main_module, "repository", repository)
    monkeypatch.setattr(
        main_module, "settings", replace(main_module.settings, output_dir=tmp_path / "output")
    )
    monkeypatch.setattr(main_module.os, "name", "nt")
    launched = []
    monkeypatch.setattr(
        main_module.subprocess, "Popen", lambda command, **kwargs: launched.append(command)
    )

    response = TestClient(main_module.app).post(
        f"/api/projects/{project['id']}/open-output-folder", json={}
    )

    assert response.status_code == 200
    assert Path(response.json()["path"]) == (tmp_path / "output" / project["id"]).resolve()
    assert launched == [["explorer.exe", str((tmp_path / "output" / project["id"]).resolve())]]
