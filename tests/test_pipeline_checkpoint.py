from dataclasses import replace
from pathlib import Path

import pytest

import app.pipeline as pipeline_module
from app.db import init_db
from app.pipeline import Pipeline
from app.repository import Repository


def _tokens(count: int) -> list[dict]:
    return [
        {
            "text": f"word-{index}." if index % 4 == 3 else f"word-{index}",
            "start_ms": index * 500,
            "end_ms": (index + 1) * 500,
            "source": "test",
        }
        for index in range(count)
    ]


def _segment(sentence: dict, translation: str) -> dict:
    return {
        "start_ms": sentence["start_ms"],
        "end_ms": sentence["end_ms"],
        "start_token_id": sentence["start_token_id"],
        "end_token_id": sentence["end_token_id"],
        "source_sentence_id": sentence["source_sentence_id"],
        "sub_id": 0,
        "source_text": sentence["source_sentence"],
        "translated_text": translation,
        "quality_flags": [],
        "status": "machine_translated",
    }


def test_translate_saves_part_level_pass2_and_pass3_results(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(project["id"], title="Test video")
    repository.replace_timed_tokens(project["id"], _tokens(100))
    monkeypatch.setattr(
        pipeline_module,
        "generate_global_translation_context",
        lambda *args, **kwargs: (
            {
                "overview": "全局概述",
                "style": "自然简洁",
                "terms": [{"source": "Katie", "target": "Katie", "category": "person", "must_use": True}],
            },
            "codex",
        ),
    )
    monkeypatch.setattr(
        pipeline_module,
        "generate_reference_translation",
        lambda *args, **kwargs: (
            {"source_text": "完整英文原文", "reference_translation": "完整、连贯的中文参考译文。"},
            "codex",
        ),
    )
    pass2_calls = []

    def fake_align(_tokens, sentences, *_args, **kwargs):
        pass2_calls.append((len(sentences), kwargs["providers"]))
        return (
            [_segment(item, "这是一条故意较长的中文父句用于测试第三阶段拆分处理") for item in sentences],
            "codex",
        )

    pass3_calls = []

    def fake_split(_tokens, parents, *_args, **_kwargs):
        pass3_calls.append(len(parents))
        return (
            [
                {
                    **parent,
                    "sub_id": 1,
                    "translated_text": "拆分后的短句",
                    "quality_flags": [],
                    "status": "machine_translated",
                }
                for parent in parents
            ],
            "codex",
        )

    monkeypatch.setattr(pipeline_module, "align_part_sentences", fake_align)
    monkeypatch.setattr(pipeline_module, "split_long_sentences", fake_split)
    monkeypatch.setattr(
        pipeline_module,
        "settings",
        replace(pipeline_module.settings, subtitle_planner_window_tokens=40, data_dir=tmp_path),
    )
    pipeline = Pipeline(repository)
    pipeline.translate(project["id"])
    checkpoint = repository.get_project(project["id"])
    assert checkpoint["status"] == "waiting_context_review"
    context = checkpoint["global_translation_context"]
    context["terms"][0]["target"] = "Katie Approved"
    repository.update_project(
        project["id"], global_translation_context=context, global_context_approved=1,
        status="queued", stage="queued",
    )
    pipeline.translate(project["id"])

    parts = repository.list_subtitle_parts(project["id"])
    source_sentences = repository.list_source_sentences(parts[0]["id"])
    segments = repository.list_segments(project["id"])
    batches = repository.list_subtitle_batches(parts[0]["id"])
    assert parts[0]["status"] == "completed"
    assert parts[0]["reference_translation"] == "完整、连贯的中文参考译文。"
    assert len(pass2_calls) == 1
    assert pass2_calls[0][1] == ("codex", "grok")
    assert pass3_calls == [len(source_sentences)]
    assert len(segments) == len(source_sentences)
    assert all(sentence["chinese_sentence"] for sentence in source_sentences)
    assert all(segment["source_sentence_id"] for segment in segments)
    assert all(batch["status"] == "completed" for batch in batches)
    assert repository.get_project(project["id"])["status"] == "waiting_review"


def test_pass3_retry_reuses_completed_part_level_pass2(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(
        project["id"], title="Test video",
        global_translation_context={"overview": "Context", "style": "Natural", "terms": []},
        global_context_approved=1,
    )
    repository.replace_timed_tokens(project["id"], _tokens(40))
    monkeypatch.setattr(
        pipeline_module,
        "generate_reference_translation",
        lambda *args, **kwargs: (
            {"source_text": "Complete source", "reference_translation": "完整参考译文"}, "codex"
        ),
    )
    pass2_calls = 0

    def fake_align(_tokens, sentences, *_args, **_kwargs):
        nonlocal pass2_calls
        pass2_calls += 1
        return (
            [_segment(item, "这是一条故意较长的中文父句用于验证检查点不会重复运行") for item in sentences],
            "codex",
        )

    pass3_calls = 0

    def flaky_split(_tokens, parents, *_args, **_kwargs):
        nonlocal pass3_calls
        pass3_calls += 1
        if pass3_calls == 1:
            raise RuntimeError("temporary Pass 3 failure")
        return ([{**item, "sub_id": 1} for item in parents], "codex")

    monkeypatch.setattr(pipeline_module, "align_part_sentences", fake_align)
    monkeypatch.setattr(pipeline_module, "split_long_sentences", flaky_split)
    monkeypatch.setattr(
        pipeline_module,
        "settings",
        replace(pipeline_module.settings, subtitle_planner_window_tokens=40, data_dir=tmp_path),
    )
    pipeline = Pipeline(repository)
    with pytest.raises(RuntimeError, match="temporary Pass 3 failure"):
        pipeline.translate(project["id"])
    assert pass2_calls == 1
    assert repository.list_subtitle_parts(project["id"])[0]["status"] == "failed"

    pipeline.translate(project["id"])
    assert pass2_calls == 1
    assert pass3_calls == 2
    assert repository.get_project(project["id"])["status"] == "waiting_review"
