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
        replace(pipeline_module.settings, data_dir=tmp_path),
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
    assert parts[0]["status"] == "completed"
    assert parts[0]["reference_translation"] == "完整、连贯的中文参考译文。"
    assert len(pass2_calls) == 1
    assert pass2_calls[0][1] == ("codex", "grok")
    assert pass3_calls == [len(source_sentences)]
    assert len(segments) == len(source_sentences)
    assert all(sentence["chinese_sentence"] for sentence in source_sentences)
    assert all(segment["source_sentence_id"] for segment in segments)
    assert repository.get_project(project["id"])["status"] == "waiting_review"


def test_pass3_finalize_merges_short_chinese_cue_before_storage(
    tmp_path: Path, monkeypatch
) -> None:
    repository = Repository(tmp_path / "pipeline.db")
    init_db(repository.database_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_timed_tokens(
        project["id"],
        [
            {"text": "before,", "start_ms": 0, "end_ms": 1000, "source": "test"},
            {"text": "after.", "start_ms": 1000, "end_ms": 2000, "source": "test"},
        ],
    )
    repository.initialize_subtitle_plan(
        project["id"],
        [{"start_token_id": 0, "end_token_id": 1, "start_ms": 0, "end_ms": 2000}],
    )
    part = repository.list_subtitle_parts(project["id"])[0]
    sentences = [
        {
            "source_sentence_id": index + 1,
            "start_token_id": index,
            "end_token_id": index,
            "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000,
            "source_sentence": word,
            "chinese_sentence": chinese,
        }
        for index, (word, chinese) in enumerate(
            [("before,", "前文"), ("after.", "看你。")]
        )
    ]
    repository.replace_source_sentences(project["id"], part["id"], sentences)
    repository.replace_part_segments(
        project["id"], part["id"],
        [_segment(sentence, sentence["chinese_sentence"]) for sentence in sentences],
    )
    monkeypatch.setattr(
        pipeline_module, "settings", replace(pipeline_module.settings, data_dir=tmp_path)
    )

    Pipeline(repository)._split_part_long_sentences(
        repository.get_project(project["id"]),
        part,
        repository.list_timed_tokens(project["id"]),
        repository.list_source_sentences(part["id"]),
        {},
        ("codex",),
        "test-model",
    )

    saved = repository.list_segments(project["id"])
    assert len(saved) == 1
    assert saved[0]["translated_text"] == "前文看你。"
    assert (saved[0]["start_token_id"], saved[0]["end_token_id"]) == (0, 1)
    assert saved[0]["source_sentence_id"] == 1
    assert [item["source_sentence_id"] for item in repository.list_source_sentences(part["id"])] == [1, 2]


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
        replace(pipeline_module.settings, data_dir=tmp_path),
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


def test_translate_repairs_legacy_pass2_sentences_without_segments(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.update_project(
        project_id,
        title="Test video",
        global_translation_context={"overview": "Context", "style": "Natural", "terms": []},
        global_context_approved=1,
    )
    repository.replace_timed_tokens(project_id, _tokens(4))
    repository.initialize_subtitle_plan(
        project_id,
        [{"start_token_id": 0, "end_token_id": 3, "start_ms": 0, "end_ms": 2000}],
    )
    part_id = repository.list_subtitle_parts(project_id)[0]["id"]
    repository.update_subtitle_part(
        part_id, source_text="Complete source", reference_translation="完整参考译文"
    )
    sentence = {
        "source_sentence_id": 1,
        "start_token_id": 0,
        "end_token_id": 3,
        "start_ms": 0,
        "end_ms": 2000,
        "source_sentence": "word-0 word-1 word-2 word-3.",
        "chinese_sentence": "旧版半成品中文",
    }
    repository.replace_source_sentences(project_id, part_id, [sentence])
    calls = []

    def fake_align(_tokens, sentences, *_args, **_kwargs):
        calls.append(1)
        return ([_segment(sentences[0], "修复后的中文")], "codex")

    monkeypatch.setattr(pipeline_module, "align_part_sentences", fake_align)
    monkeypatch.setattr(
        pipeline_module, "settings", replace(pipeline_module.settings, data_dir=tmp_path)
    )
    Pipeline(repository).translate(project_id)

    assert calls == [1]
    assert repository.list_segments(project_id)[0]["translated_text"] == "修复后的中文"
    assert repository.list_source_sentences(part_id)[0]["chinese_sentence"] == "修复后的中文"


def test_part_rebuild_reruns_only_the_selected_part(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    context = {"overview": "Keep", "style": "Natural", "terms": []}
    repository.update_project(
        project["id"],
        title="Test video",
        global_translation_context=context,
        global_context_approved=1,
    )
    repository.replace_timed_tokens(project["id"], _tokens(20))
    repository.initialize_subtitle_plan(
        project["id"],
        [
            {"start_token_id": 0, "end_token_id": 9, "start_ms": 0, "end_ms": 5000},
            {"start_token_id": 10, "end_token_id": 19, "start_ms": 5000, "end_ms": 10000},
        ],
    )
    parts = repository.list_subtitle_parts(project["id"])
    for part in parts:
        source_rows = (
            [
                {
                    "source_sentence_id": index + 1,
                    "start_token_id": start,
                    "end_token_id": end,
                    "start_ms": start * 500,
                    "end_ms": (end + 1) * 500,
                    "source_sentence": f"Part {part['sequence']} cue {index + 1}",
                    "chinese_sentence": "旧译文",
                }
                for index, (start, end) in enumerate([(10, 15), (16, 19)])
            ]
            if part["sequence"] == 1
            else [
                {
                    "source_sentence_id": 1,
                    "start_token_id": part["start_token_id"],
                    "end_token_id": part["end_token_id"],
                    "start_ms": part["start_ms"],
                    "end_ms": part["end_ms"],
                    "source_sentence": f"Part {part['sequence']}",
                    "chinese_sentence": "旧译文",
                }
            ]
        )
        repository.replace_source_sentences(
            project["id"], part["id"], source_rows,
        )
        repository.replace_part_segments(
            project["id"],
            part["id"],
            [_segment(sentence, f"旧译文{part['sequence']}") for sentence in source_rows],
        )
    selected = parts[1]
    repository.begin_part_rebuild(
        project["id"],
        {
            "version": 2,
            "run_id": "test-run",
            "base_subtitle_revision": repository.get_project(project["id"])["subtitle_revision"],
            "parts": [
                {
                    "part_id": selected["id"],
                    "part_sequence": selected["sequence"],
                    "start_token_id": selected["start_token_id"],
                    "end_token_id": selected["end_token_id"],
                }
            ],
        },
    )
    pass1_calls = []

    def fake_reference(*_args, **_kwargs):
        pass1_calls.append(1)
        return ({"source_text": "Selected part", "reference_translation": "新的整段译文"}, "codex")

    def fake_align(_tokens, sentences, *_args, **_kwargs):
        return (
            [
                _segment(item, "他们可能会用居高临下的态度" if index == 0 else "看你。")
                for index, item in enumerate(sentences)
            ],
            "codex",
        )

    def fail_split(*_args, **_kwargs):
        raise AssertionError("Pass 3 should not run for a short cue")

    monkeypatch.setattr(pipeline_module, "generate_reference_translation", fake_reference)
    monkeypatch.setattr(pipeline_module, "align_part_sentences", fake_align)
    monkeypatch.setattr(pipeline_module, "split_long_sentences", fail_split)
    monkeypatch.setattr(
        pipeline_module, "settings", replace(pipeline_module.settings, data_dir=tmp_path)
    )

    Pipeline(repository).part_rebuild(project["id"])

    saved = repository.get_project(project["id"])
    segments = repository.list_segments(project["id"])
    by_part = {int(segment["part_id"]): segment["translated_text"] for segment in segments}
    assert pass1_calls == [1]
    assert by_part[parts[0]["id"]] == "旧译文0"
    assert by_part[parts[1]["id"]] == "他们可能会用居高临下的态度看你。"
    assert [item["source_sentence_id"] for item in repository.list_source_sentences(parts[1]["id"])] == [1, 2]
    assert [item["sequence"] for item in segments] == [0, 1]
    assert saved["global_translation_context"] == context
    assert saved["global_context_approved"] is True
    assert saved["pending_rebuild_scope"] is None
    assert saved["status"] == "waiting_review"
