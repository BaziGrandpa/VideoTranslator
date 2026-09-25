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
        [{"start_ms": 0, "end_ms": 1000, "source_text": "Hello", "translated_text": "旧字幕"}],
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
        [{"start_ms": 0, "end_ms": 1000, "source_text": "Hello", "translated_text": "你好"}],
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


def test_pass2_rolls_back_sentences_when_cue_write_fails(tmp_path: Path, monkeypatch) -> None:
    repository = _repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.initialize_subtitle_plan(
        project_id,
        [{"start_token_id": 0, "end_token_id": 0, "start_ms": 0, "end_ms": 1000}],
    )
    part_id = repository.list_subtitle_parts(project_id)[0]["id"]
    sentence = {
        "source_sentence_id": 1,
        "start_token_id": 0,
        "end_token_id": 0,
        "start_ms": 0,
        "end_ms": 1000,
        "source_sentence": "Hello.",
        "chinese_sentence": "旧译文",
    }
    repository.replace_source_sentences(project_id, part_id, [sentence])
    segment = {
        "start_token_id": 0,
        "end_token_id": 0,
        "start_ms": 0,
        "end_ms": 1000,
        "source_sentence_id": 1,
        "source_text": "Hello.",
        "translated_text": "新译文",
    }

    def fail_cue_write(*_args, **_kwargs):
        raise RuntimeError("cue insert failed")

    original = repository.replace_part_segments
    monkeypatch.setattr(repository, "replace_part_segments", fail_cue_write)
    with pytest.raises(RuntimeError, match="cue insert failed"):
        repository.commit_pass2(
            project_id, part_id, [{**sentence, "chinese_sentence": "新译文"}], [segment]
        )
    assert repository.list_source_sentences(part_id)[0]["chinese_sentence"] == "旧译文"
    assert repository.list_segments(project_id) == []
    assert repository.list_subtitle_parts(project_id)[0]["status"] == "pending"

    monkeypatch.setattr(repository, "replace_part_segments", original)
    repository.commit_pass2(
        project_id, part_id, [{**sentence, "chinese_sentence": "新译文"}], [segment]
    )
    assert repository.list_source_sentences(part_id)[0]["chinese_sentence"] == "新译文"
    assert repository.list_segments(project_id)[0]["translated_text"] == "新译文"
    assert repository.list_subtitle_parts(project_id)[0]["status"] == "pass2_complete"
