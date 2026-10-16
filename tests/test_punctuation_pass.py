from dataclasses import replace
from pathlib import Path

import pytest

import app.pipeline as pipeline_module
import app.services.subtitle_planner as planner_module
from app.db import connect, init_db
from app.pipeline import Pipeline
from app.repository import Repository
from app.services.cues import build_cues
from app.services.prompts import build_punctuation_prompt
from app.services.punctuation import (
    apply_punctuation,
    source_fingerprint,
    source_punctuation_count,
    validate_punctuation_annotations,
)


def _tokens(count: int, *, punctuation: bool = False, interval_ms: int = 500) -> list[dict]:
    return [
        {
            "token_id": index,
            "text": f"word{index}{'.' if punctuation and index == 4 else ''}",
            "start_ms": index * interval_ms,
            "end_ms": (index + 1) * interval_ms,
            "source": "test",
        }
        for index in range(count)
    ]


def test_prompt_contains_complete_source_and_indexed_tokens() -> None:
    tokens = [
        {**token, "normalized_text": token["text"]}
        for token in _tokens(6)
    ]
    prompt = build_punctuation_prompt(tokens, "Test video", 1, 2)
    assert "BEGIN COMPLETE PART TRANSCRIPT\nword0 word1 word2 word3 word4 word5\n" in prompt
    assert "BEGIN INDEXED TOKENS\n0\tword0\n1\tword1\n" in prompt
    assert "5\tword5\nEND INDEXED TOKENS" in prompt
    assert "PART: 1/2" in prompt


def test_hand_annotated_english_splits_cues_without_changing_word_times() -> None:
    words = [
        "why", "do", "we", "all", "need", "constant", "stimulation", "all", "day",
        "forever", "we", "scroll", "while", "we", "watch", "tv",
    ]
    original = [
        {
            "token_id": index,
            "text": word,
            "raw_text": word,
            "normalized_text": word,
            "start_ms": index * 400,
            "end_ms": (index + 1) * 400,
        }
        for index, word in enumerate(words)
    ]
    response = {"punctuation": [
        {"after_token_id": 9, "mark": "?"},
        {"after_token_id": 15, "mark": "."},
    ]}
    marks = validate_punctuation_annotations(original, response)
    effective = apply_punctuation(original, marks)
    cues = build_cues(effective)
    assert [cue["source_text"] for cue in cues] == [
        "why do we all need constant stimulation all day forever?",
        "we scroll while we watch tv.",
    ]
    assert [(cue["start_token_id"], cue["end_token_id"]) for cue in cues] == [(0, 9), (10, 15)]
    assert [(t["raw_text"], t["normalized_text"], t["start_ms"], t["end_ms"]) for t in effective] == [
        (t["raw_text"], t["normalized_text"], t["start_ms"], t["end_ms"]) for t in original
    ]


def test_two_unpunctuated_parts_run_once_each_before_pass0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(project["id"], title="Test video")
    repository.replace_tokens(project["id"], _tokens(100, interval_ms=30_000))
    before = repository.list_tokens(project["id"])
    calls: list[tuple[int, int, list[int]]] = []

    def fake_punctuation(tokens, _title, part_number, part_count, *_args):
        calls.append((part_number, part_count, [token["token_id"] for token in tokens]))
        return [{"after_token_id": tokens[4]["token_id"], "mark": "."}]

    def fake_context(tokens, *_args, **_kwargs):
        assert len(calls) == 2
        assert sum(str(token["text"]).endswith(".") for token in tokens) == 2
        return ({"overview": "Overview", "style": "Natural", "terms": []}, "codex")

    monkeypatch.setattr(pipeline_module, "generate_punctuation_annotations", fake_punctuation)
    monkeypatch.setattr(pipeline_module, "generate_translation_context", fake_context)
    monkeypatch.setattr(pipeline_module, "settings", replace(pipeline_module.settings, data_dir=tmp_path))
    pipeline = Pipeline(repository)
    pipeline.translate(project["id"])

    assert len(calls) == 2
    assert calls[0][0:2] == (1, 2)
    assert calls[1][0:2] == (2, 2)
    assert calls[0][2][-1] + 1 == calls[1][2][0]
    assert repository.get_project(project["id"])["status"] == "waiting_context_review"
    after = repository.list_tokens(project["id"])
    assert [(t["raw_text"], t["normalized_text"], t["start_ms"], t["end_ms"]) for t in after] == [
        (t["raw_text"], t["normalized_text"], t["start_ms"], t["end_ms"]) for t in before
    ]
    assert sum(str(token["text"]).endswith(".") for token in after) == 2
    pipeline.translate(project["id"])
    assert len(calls) == 2


def test_existing_punctuation_skips_model_before_pass0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_tokens(project["id"], _tokens(10, punctuation=True))
    monkeypatch.setattr(
        pipeline_module,
        "generate_punctuation_annotations",
        lambda *_args, **_kwargs: pytest.fail("punctuation CLI must be skipped"),
    )
    monkeypatch.setattr(
        pipeline_module,
        "generate_translation_context",
        lambda *_args, **_kwargs: ({"overview": "Overview", "style": "Natural", "terms": []}, "codex"),
    )
    monkeypatch.setattr(pipeline_module, "settings", replace(pipeline_module.settings, data_dir=tmp_path))
    Pipeline(repository).translate(project["id"])
    assert source_punctuation_count(repository.list_tokens(project["id"])) == 1


def test_non_speech_only_captions_fail_before_punctuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_tokens(project["id"], [
        {"text": "[Music]", "start_ms": 0, "end_ms": 1000, "source": "test"},
        {"text": "[Applause]", "start_ms": 1000, "end_ms": 2000, "source": "test"},
    ])
    monkeypatch.setattr(
        pipeline_module,
        "generate_punctuation_annotations",
        lambda *_args, **_kwargs: pytest.fail("punctuation model must not be called"),
    )
    with pytest.raises(RuntimeError, match="No usable English subtitles found"):
        Pipeline(repository).translate(project["id"])
    assert repository.list_parts(project["id"]) == []


def test_full_rebuild_enables_new_stage_and_clears_old_annotations(tmp_path: Path) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_tokens(project["id"], _tokens(8))
    repository.initialize_subtitle_plan(project["id"], [{
        "start_token_id": 0, "end_token_id": 7, "start_ms": 0, "end_ms": 4000,
    }])
    part = repository.list_parts(project["id"])[0]
    repository.begin_part_punctuation(
        project["id"], part["id"],
        source_fingerprint(repository.list_tokens(project["id"])), "codex",
    )
    repository.complete_part_punctuation(part["id"], [{"after_token_id": 4, "mark": "."}])
    repository.update_project(project["id"], punctuation_enabled=0)

    repository.clear_subtitle_data(project["id"])

    assert repository.get_project(project["id"])["punctuation_enabled"] is True
    assert repository.list_parts(project["id"]) == []
    assert repository.list_tokens(project["id"]) == []
    assert repository.get_part_punctuation(part["id"]) is None


def test_invalid_model_response_has_explicit_error_and_no_second_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_tokens(project["id"], _tokens(8))
    attempts = 0

    def fake_call(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return {"punctuation": [
            {"after_token_id": 2, "mark": "."},
            {"after_token_id": 2, "mark": ","},
        ]}

    monkeypatch.setattr(planner_module, "_audited_call", fake_call)
    monkeypatch.setattr(pipeline_module, "settings", replace(pipeline_module.settings, data_dir=tmp_path))
    pipeline = Pipeline(repository)
    with pytest.raises(RuntimeError, match="Part 1/1: after_token_id 2 is duplicated") as exc:
        pipeline.translate(project["id"])
    assert "request 1/1" in str(exc.value)
    assert "Pass 0 not started" in str(exc.value)
    assert attempts == 1
    part = repository.list_parts(project["id"])[0]
    saved = repository.get_part_punctuation(part["id"])
    assert saved["status"] == "failed"
    with connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM punctuation_marks WHERE part_id = ?", (part["id"],)
        ).fetchone()[0] == 0
    assert all(token["text"] == token["normalized_text"] for token in repository.list_tokens(project["id"]))
    with pytest.raises(RuntimeError, match="no automatic second call"):
        pipeline.translate(project["id"])
    assert attempts == 1


def test_punctuation_validation_rejects_missing_sentence_end_and_non_speech() -> None:
    tokens = [
        {**token, "normalized_text": token["text"]}
        for token in _tokens(3)
    ]
    tokens[1]["text"] = tokens[1]["normalized_text"] = "[Music]"
    with pytest.raises(ValueError, match="no sentence-ending punctuation"):
        validate_punctuation_annotations(tokens, {"punctuation": [{"after_token_id": 0, "mark": ","}]})
    with pytest.raises(ValueError, match="not speech"):
        validate_punctuation_annotations(tokens, {"punctuation": [{"after_token_id": 1, "mark": "."}]})
