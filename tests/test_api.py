from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.db import init_db
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
                "translated_text": "Hello translated",
            }
        ],
    )
    segment = repository.list_segments(project_id)[0]
    edited = client.patch(
        f"/api/projects/{project_id}/segments/{segment['id']}",
        json={"start_ms": 1100, "translated_text": "Edited translation"},
    )
    assert edited.status_code == 200
    assert edited.json()["start_ms"] == 1100
    assert edited.json()["translated_text"] == "Edited translation"


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
        global_translation_context={"overview": "Old", "style": "Old", "terms": []},
        global_context_provider="codex",
        global_context_approved=1,
    )
    repository.replace_timed_tokens(
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
                "windows": [(0, 0)],
            }
        ],
        window_size=1000,
    )
    part = repository.list_subtitle_parts(project["id"])[0]
    batch = repository.list_subtitle_batches(part["id"])[0]
    repository.save_batch_segments(
        project["id"],
        part["id"],
        batch["id"],
        [
            {
                "start_ms": 0,
                "end_ms": 1000,
                "start_token_id": 0,
                "end_token_id": 0,
                "source_text": "Hello",
                "translated_text": "你好",
            }
        ],
        "codex",
    )
    repository.replace_part_review_issues(
        project["id"],
        part["id"],
        [
            {
                "issue_id": "old-issue",
                "action": "review_timing",
                "severity": "low",
                "issue_types": ["timing"],
                "reason": "Old review issue",
                "start_token_id": 0,
                "end_token_id": 0,
                "replacement_cues": [],
            }
        ],
    )

    response = client.post(f"/api/projects/{project['id']}/rebuild-subtitles", json={})

    assert response.status_code == 200
    assert response.json()["kind"] == "rebuild_subtitles"
    assert response.json()["status"] == "queued"
    assert repository.list_segments(project["id"]) == []
    assert repository.list_timed_tokens(project["id"]) == []
    assert repository.list_subtitle_parts(project["id"]) == []
    assert repository.list_review_issues(project["id"]) == []
    rebuilt = repository.get_project(project["id"])
    assert rebuilt is not None
    assert rebuilt["status"] == "queued"
    assert rebuilt["stage"] == "queued"
    assert rebuilt["progress"] == 0
    assert rebuilt["global_translation_context"] is None
    assert rebuilt["global_context_approved"] is False
    assert rebuilt["output_path"] is None
    assert rebuilt["video_path"] == str(video_path)


def test_review_issues_include_pass3_suggestions_and_current_quality_flags(
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
                "windows": [(0, 2)],
            }
        ],
        600,
    )
    part = repository.list_subtitle_parts(project["id"])[0]
    batch = repository.list_subtitle_batches(part["id"])[0]
    repository.save_batch_segments(
        project["id"],
        part["id"],
        batch["id"],
        [
            {
                "start_ms": 0,
                "end_ms": 7000,
                "start_token_id": 0,
                "end_token_id": 2,
                "source_text": "I just did something crazy.",
                "translated_text": "那双700美元的鞋还在袋子里",
                "quality_flags": ["very_long_duration"],
                "status": "needs_review",
            }
        ],
        "codex",
    )
    repository.replace_batch_review_issues(
        project["id"],
        part["id"],
        batch["id"],
        [
            {
                "issue_id": "possible-shift",
                "action": "suggest_translation",
                "severity": "high",
                "issue_types": ["possible_semantic_misalignment"],
                "reason": "The nearby Chinese meanings may be shifted.",
                "start_token_id": 0,
                "end_token_id": 2,
                "replacement_cues": [
                    {
                        "start_token_id": 0,
                        "end_token_id": 2,
                        "translation": "我刚做了一件疯狂的事",
                    }
                ],
                "auto_applied": False,
            }
        ],
    )

    response = TestClient(main_module.app).get(f"/api/projects/{project['id']}/review-issues")

    assert response.status_code == 200
    issues = response.json()
    semantic = next(issue for issue in issues if issue["source"] == "pass3")
    quality = next(issue for issue in issues if issue["source"] == "quality")
    segment = repository.list_segments(project["id"])[0]
    assert semantic["replacement_cues"][0]["segment_id"] == segment["id"]
    assert semantic["replacement_cues"][0]["translation"] == "我刚做了一件疯狂的事"
    assert semantic["affected_segment_ids"] == [segment["id"]]
    assert "very_long_duration" in quality["issue_types"]


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
                "translated_text": "精确预览",
            }
        ],
    )
    segment = repository.list_segments(project["id"])[0]
    captured = {}

    def fake_render(*args):
        captured["args"] = args
        return b"\x89PNG\r\n\x1a\npreview"

    monkeypatch.setattr(main_module, "render_exact_preview", fake_render)
    response = TestClient(main_module.app).post(
        f"/api/projects/{project['id']}/exact-preview",
        json={"segment_id": segment["id"], "timestamp_ms": 1500},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\x89PNG")
    assert captured["args"][0] == project["id"]
    assert captured["args"][3] == 1500


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
                "translated_text": "已锁定",
            }
        ],
    )
    segment = repository.list_segments(project["id"])[0]
    client = TestClient(main_module.app)

    edit = client.patch(
        f"/api/projects/{project['id']}/segments/{segment['id']}",
        json={"translated_text": "Should not save"},
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
        global_translation_context={
            "overview": "Initial overview",
            "style": "Initial style",
            "terms": [],
        },
        global_context_provider="codex",
        global_context_approved=0,
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
        f"/api/projects/{project['id']}/global-translation-context", json=payload
    )

    assert saved.status_code == 200
    assert saved.json()["overview"] == "A revised project overview."
    draft_project = repository.get_project(project["id"])
    assert draft_project["status"] == "waiting_context_review"
    assert draft_project["global_context_approved"] is False

    approved = client.post(
        f"/api/projects/{project['id']}/global-translation-context/approve", json=payload
    )

    assert approved.status_code == 200
    assert approved.json()["job"]["kind"] == "translate"
    approved_project = repository.get_project(project["id"])
    assert approved_project["status"] == "queued"
    assert approved_project["stage"] == "queued"
    assert approved_project["global_context_approved"] is True
    assert approved_project["global_translation_context"]["terms"][0] == {
        "source": "Katie",
        "target": "Katie",
        "category": "person",
        "must_use": True,
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
        global_translation_context={"overview": "Overview", "style": "", "terms": []},
    )

    response = client.patch(
        f"/api/projects/{project['id']}/global-translation-context",
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
                "translated_text": "",
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
