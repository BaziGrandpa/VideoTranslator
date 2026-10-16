from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.worker as worker_module
from app.db import init_db
from app.repository import Repository
from app.services.subtitle_planner import TranslationQuotaError


def _repository(tmp_path: Path) -> Repository:
    database = tmp_path / "jobs.db"
    init_db(database)
    return Repository(database)


def test_caption_track_without_speech_reports_no_subtitles(
    tmp_path: Path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_tokens(
        project["id"],
        [{"text": "[Music]", "start_ms": 0, "end_ms": 1000, "source": "youtube_auto"}],
    )
    job = repository.enqueue_job(project["id"], "translate")
    monkeypatch.setattr(worker_module, "init_db", lambda: None)
    monkeypatch.setattr(worker_module, "Repository", lambda: repository)
    monkeypatch.setattr(worker_module, "settings", SimpleNamespace(ensure_directories=lambda: None))

    worker_module.run_worker(once=True)

    failed = repository.get_project(project["id"])
    assert failed["status"] == "failed"
    assert failed["stage"] == "no_subtitles"
    assert "No usable English subtitles found" in failed["error"]
    assert repository.get_job(job["id"])["status"] == "failed"


@pytest.mark.parametrize(
    ("previous_status", "output_path"),
    [("waiting_review", None), ("completed", "old-final.mp4")],
)
def test_failed_render_restores_review_state_without_retry_job(
    tmp_path: Path, monkeypatch, previous_status: str, output_path: str | None
) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.update_project(
        project_id,
        status="queued",
        stage="render_queued",
        output_path=output_path,
    )
    job = repository.enqueue_job(project_id, "render", return_status=previous_status)

    class FailingPipeline:
        def __init__(self, _repository):
            pass

        def render(self, _project_id):
            raise RuntimeError("FFmpeg failed")

    monkeypatch.setattr(worker_module, "init_db", lambda: None)
    monkeypatch.setattr(worker_module, "Repository", lambda: repository)
    monkeypatch.setattr(worker_module, "Pipeline", FailingPipeline)
    monkeypatch.setattr(worker_module, "settings", SimpleNamespace(ensure_directories=lambda: None))
    worker_module.run_worker(once=True)

    restored = repository.get_project(project_id)
    assert restored["status"] == previous_status
    assert restored["stage"] == previous_status
    assert restored["output_path"] == output_path
    assert "FFmpeg failed" in restored["error"]
    assert repository.get_job(job["id"])["status"] == "failed"
    assert repository.claim_next_job() is None


@pytest.mark.parametrize("failure", [RuntimeError, TranslationQuotaError])
def test_failed_part_test_releases_scope_and_preserves_existing_cues(
    tmp_path: Path, monkeypatch, failure: type[RuntimeError]
) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.update_project(project_id, status="waiting_review", stage="waiting_review")
    repository.replace_segments(
        project_id,
        [{"start_ms": 0, "end_ms": 1000, "source_text": "Hello", "target_text": "旧字幕"}],
    )
    before = repository.list_segments(project_id)
    repository.begin_part_rebuild(project_id, {"run_id": "test", "parts": []})
    job = repository.enqueue_job(project_id, "part_rebuild", return_status="waiting_review")

    class FailingPipeline:
        def __init__(self, _repository):
            pass

        def part_rebuild(self, _project_id):
            raise failure("test output invalid")

    monkeypatch.setattr(worker_module, "init_db", lambda: None)
    monkeypatch.setattr(worker_module, "Repository", lambda: repository)
    monkeypatch.setattr(worker_module, "Pipeline", FailingPipeline)
    monkeypatch.setattr(worker_module, "settings", SimpleNamespace(ensure_directories=lambda: None))
    worker_module.run_worker(once=True)

    restored = repository.get_project(project_id)
    assert restored["status"] == "waiting_review"
    assert restored["pending_rebuild_scope"] is None
    assert repository.list_segments(project_id) == before
    assert repository.get_job(job["id"])["status"] == "failed"


@pytest.mark.parametrize("kind", ["render", "part_rebuild"])
def test_worker_restart_does_not_rerun_interrupted_interactive_job(
    tmp_path: Path, kind: str
) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    if kind == "part_rebuild":
        repository.begin_part_rebuild(project_id, {"run_id": "test", "parts": []})
    else:
        repository.update_project(project_id, status="queued", stage="render_queued")
    job = repository.enqueue_job(project_id, kind, return_status="waiting_review")
    assert repository.claim_next_job()["id"] == job["id"]

    assert repository.recover_running_jobs() == 1
    assert repository.get_job(job["id"])["status"] == "failed"
    assert repository.claim_next_job() is None
    restored = repository.get_project(project_id)
    assert restored["status"] == "waiting_review"
    assert restored["pending_rebuild_scope"] is None


@pytest.mark.parametrize("kind", ["render", "part_rebuild"])
def test_worker_restart_recognizes_published_interactive_job(tmp_path: Path, kind: str) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    if kind == "part_rebuild":
        repository.begin_part_rebuild(project_id, {"run_id": "test", "parts": []})
    else:
        repository.update_project(project_id, status="queued", stage="render_queued")
    job = repository.enqueue_job(project_id, kind, return_status="waiting_review")
    assert repository.claim_next_job()["id"] == job["id"]

    if kind == "part_rebuild":
        repository.update_project(
            project_id,
            status="waiting_review",
            stage="waiting_review",
            pending_rebuild_scope=None,
            subtitle_revision=2,
        )
    else:
        repository.update_project(
            project_id,
            status="completed",
            stage="completed",
            output_path="final-v001.mp4",
            rendered_revision=1,
        )

    assert repository.recover_running_jobs() == 1
    assert repository.get_job(job["id"])["status"] == "completed"
    assert repository.get_project(project_id)["status"] == (
        "waiting_review" if kind == "part_rebuild" else "completed"
    )


def test_render_failure_can_be_followed_by_new_manual_render(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.update_project(project_id, status="waiting_review", stage="waiting_review")
    repository.replace_segments(
        project_id,
        [{"start_ms": 0, "end_ms": 1000, "source_text": "Hello", "target_text": "你好"}],
    )

    first = client.post(f"/api/projects/{project_id}/approve-and-render", json={})
    assert first.status_code == 200
    first_job = first.json()
    repository.fail_interactive_job(first_job["id"], "FFmpeg failed")
    assert repository.get_project(project_id)["status"] == "waiting_review"

    second = client.post(f"/api/projects/{project_id}/approve-and-render", json={})
    assert second.status_code == 200
    assert second.json()["kind"] == "render"
    assert second.json()["id"] != first_job["id"]


@pytest.mark.parametrize("running", [False, True])
def test_cancel_render_restores_project_and_keeps_previous_output(
    tmp_path: Path, monkeypatch, running: bool
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setattr(main_module, "repository", repository)
    client = TestClient(main_module.app)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.update_project(
        project_id, status="queued", stage="render_queued", output_path="old-final.mp4"
    )
    job = repository.enqueue_job(project_id, "render", return_status="completed")
    if running:
        repository.claim_next_job()
        repository.update_project(project_id, status="running", stage="rendering")

    response = client.post(f"/api/projects/{project_id}/cancel-render", json={})
    assert response.status_code == 200
    if running:
        assert response.json()["status"] == "cancelling"
        assert repository.render_cancel_requested(project_id)
        assert repository.get_project(project_id)["stage"] == "render_cancelling"
        assert repository.publish_render(project_id, "new-final.mp4") is False
        repository.cancel_render_job(job["id"])
    else:
        assert response.json()["status"] == "cancelled"
        assert repository.claim_next_job() is None

    restored = repository.get_project(project_id)
    assert restored["status"] == "completed"
    assert restored["stage"] == "completed"
    assert restored["output_path"] == "old-final.mp4"
    assert restored["error"] is None
    assert repository.get_job(job["id"])["status"] == "cancelled"
    assert client.post(f"/api/projects/{project_id}/cancel-render", json={}).status_code == 409


def test_worker_restart_finishes_requested_render_cancellation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.update_project(project_id, status="queued", stage="render_queued")
    job = repository.enqueue_job(project_id, "render", return_status="waiting_review")
    repository.claim_next_job()
    repository.update_project(project_id, status="running", stage="rendering")
    repository.request_render_cancel(project_id)

    assert repository.recover_running_jobs() == 1
    assert repository.get_job(job["id"])["status"] == "cancelled"
    assert repository.get_project(project_id)["stage"] == "waiting_review"


def test_pass2_rolls_back_sentences_when_cue_write_fails(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.initialize_subtitle_plan(
        project_id,
        [{"start_token_id": 0, "end_token_id": 0, "start_ms": 0, "end_ms": 1000}],
    )
    part_id = repository.list_parts(project_id)[0]["id"]
    sentence = {
        "cue_id": 1,
        "start_token_id": 0,
        "end_token_id": 0,
        "start_ms": 0,
        "end_ms": 1000,
        "source_text": "Hello.",
        "target_text": "旧译文",
    }
    repository.replace_cues(project_id, part_id, [sentence])

    original = repository.replace_cues

    def fail_cue_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("cue insert failed")

    monkeypatch.setattr(repository, "replace_cues", fail_cue_write)
    with pytest.raises(RuntimeError, match="cue insert failed"):
        repository.commit_pass2(project_id, part_id, [{**sentence, "target_text": "新译文"}])
    assert repository.list_cues(part_id)[0]["target_text"] == "旧译文"
    assert repository.list_segments(project_id) == []
    assert repository.list_parts(project_id)[0]["status"] == "pending"

    monkeypatch.setattr(repository, "replace_cues", original)
    repository.commit_pass2(project_id, part_id, [{**sentence, "target_text": "新译文"}])
    assert repository.list_cues(part_id)[0]["target_text"] == "新译文"
    assert repository.list_segments(project_id) == []
    assert repository.list_parts(project_id)[0]["status"] == "pass2_complete"
