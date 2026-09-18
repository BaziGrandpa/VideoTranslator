from dataclasses import replace
from pathlib import Path

import pytest

import app.pipeline as pipeline_module
from app.db import init_db
from app.pipeline import Pipeline
from app.repository import Repository


def test_translate_saves_each_window_and_reference_translation(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(project["id"], title="Test video")
    tokens = [
        {
            "text": f"word-{index}.",
            "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000,
            "source": "test",
        }
        for index in range(100)
    ]
    repository.replace_timed_tokens(project["id"], tokens)

    monkeypatch.setattr(
        pipeline_module,
        "generate_global_translation_context",
        lambda *args, **kwargs: (
            {
                "overview": "全局概述",
                "style": "自然简洁",
                "terms": [
                    {
                        "source": "Katie",
                        "target": "Katie",
                        "category": "person",
                        "must_use": True,
                    }
                ],
            },
            "codex",
        ),
    )

    def fake_reference(*args, **kwargs):
        assert kwargs["global_context"]["terms"][0]["target"] == "Katie Approved"
        return (
            {
                "source_text": "完整英文原文",
                "reference_translation": "完整、连贯的中文参考译文。",
            },
            "codex",
        )

    monkeypatch.setattr(pipeline_module, "generate_reference_translation", fake_reference)

    def fake_window(
        all_tokens,
        _language,
        _title,
        start,
        end,
        _context,
        _previous,
        *,
        providers=None,
        codex_model=None,
        audit_dir=None,
    ):
        assert providers == ("codex", "grok")
        assert codex_model == pipeline_module.settings.codex_model
        assert _context["terms"][0]["target"] == "Katie Approved"
        return (
            [
                {
                    "start_ms": all_tokens[start]["start_ms"],
                    "end_ms": all_tokens[end]["end_ms"],
                    "start_token_id": start,
                    "end_token_id": end,
                    "source_text": " ".join(token["text"] for token in all_tokens[start : end + 1]),
                    "translated_text": "这一批。",
                    "quality_flags": [],
                }
            ],
            "codex",
        )

    monkeypatch.setattr(pipeline_module, "plan_subtitle_window", fake_window)
    audit_calls = []

    def fake_audit(
        _tokens,
        segments,
        _language,
        _title,
        start,
        end,
        _context,
        _previous,
        provider,
        *,
        codex_model=None,
        audit_dir=None,
        include_issues=False,
    ):
        assert _context["terms"][0]["target"] == "Katie Approved"
        audit_calls.append((start, end, provider, codex_model, audit_dir, len(segments)))
        issues = [
            {
                "issue_id": "review-first-cue",
                "action": "suggest_translation",
                "severity": "medium",
                "issue_types": ["wording"],
                "reason": "Review the first cue wording.",
                "start_token_id": segments[0]["start_token_id"],
                "end_token_id": segments[0]["end_token_id"],
                "replacement_cues": [
                    {
                        "start_token_id": segments[0]["start_token_id"],
                        "end_token_id": segments[0]["end_token_id"],
                        "translation": "建议译文",
                    }
                ],
                "auto_applied": False,
            }
        ]
        return (segments, issues) if include_issues else segments

    monkeypatch.setattr(pipeline_module, "audit_subtitle_window", fake_audit)
    monkeypatch.setattr(
        pipeline_module,
        "settings",
        replace(pipeline_module.settings, subtitle_planner_window_tokens=40, data_dir=tmp_path),
    )

    pipeline = Pipeline(repository)
    pipeline.translate(project["id"])

    checkpoint = repository.get_project(project["id"])
    assert checkpoint is not None
    assert checkpoint["status"] == "waiting_context_review"
    assert checkpoint["stage"] == "waiting_context_review"
    assert checkpoint["global_context_approved"] is False
    assert repository.list_subtitle_parts(project["id"]) == []

    approved_context = checkpoint["global_translation_context"]
    approved_context["terms"][0]["target"] = "Katie Approved"
    repository.update_project(
        project["id"],
        global_translation_context=approved_context,
        global_context_approved=1,
        status="queued",
        stage="queued",
    )
    pipeline.translate(project["id"])

    parts = repository.list_subtitle_parts(project["id"])
    batches = repository.list_subtitle_batches(parts[0]["id"])
    assert parts[0]["status"] == "completed"
    assert parts[0]["source_text"] == "完整英文原文"
    assert parts[0]["reference_translation"] == "完整、连贯的中文参考译文。"
    assert parts[0]["translation_provider"] == "codex"
    assert [batch["status"] for batch in batches] == ["completed", "completed", "completed"]
    assert len(repository.list_segments(project["id"])) == 3
    assert len(audit_calls) == 1
    assert audit_calls[0][0:2] == (0, 99)
    assert audit_calls[0][5] == 3
    assert all(call[2] == "codex" for call in audit_calls)
    review_issues = repository.list_review_issues(project["id"])
    assert len(review_issues) == 1
    assert review_issues[0]["batch_id"] is None
    assert review_issues[0]["issue_key"].startswith(f"part-{parts[0]['id']}:")
    assert repository.get_project(project["id"])["status"] == "waiting_review"
    assert (
        repository.get_project(project["id"])["global_translation_context"]["terms"][0]["target"]
        == "Katie Approved"
    )


def test_part_audit_retry_does_not_repeat_completed_pass2_windows(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "pipeline.db"
    init_db(database)
    repository = Repository(database)
    project = repository.create_project("https://youtu.be/example")
    repository.update_project(
        project["id"],
        title="Test video",
        global_translation_context={"overview": "Context", "style": "Natural", "terms": []},
        global_context_approved=1,
    )
    tokens = [
        {
            "text": f"word-{index}.",
            "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000,
            "source": "test",
        }
        for index in range(80)
    ]
    repository.replace_timed_tokens(project["id"], tokens)
    repository.initialize_subtitle_plan(
        project["id"],
        [
            {
                "start_token_id": 0,
                "end_token_id": 79,
                "start_ms": 0,
                "end_ms": 80_000,
                "windows": [(0, 39), (40, 79)],
            }
        ],
        40,
    )
    part = repository.list_subtitle_parts(project["id"])[0]
    repository.update_subtitle_part(
        part["id"],
        status="planning_subtitles",
        source_text="Complete source",
        reference_translation="完整参考译文",
        translation_provider="codex",
    )
    window_calls = []

    def fake_window(all_tokens, _language, _title, start, end, *_args, **_kwargs):
        window_calls.append((start, end))
        return (
            [
                {
                    "start_ms": all_tokens[start]["start_ms"],
                    "end_ms": all_tokens[end]["end_ms"],
                    "start_token_id": start,
                    "end_token_id": end,
                    "source_text": " ".join(
                        token["text"] for token in all_tokens[start : end + 1]
                    ),
                    "translated_text": "这一批",
                    "quality_flags": [],
                }
            ],
            "codex",
        )

    audit_calls = 0

    def flaky_audit(_tokens, segments, *_args, include_issues=False, **_kwargs):
        nonlocal audit_calls
        audit_calls += 1
        if audit_calls == 1:
            raise RuntimeError("temporary Pass 3 failure")
        return (segments, []) if include_issues else segments

    monkeypatch.setattr(pipeline_module, "plan_subtitle_window", fake_window)
    monkeypatch.setattr(pipeline_module, "audit_subtitle_window", flaky_audit)
    monkeypatch.setattr(
        pipeline_module,
        "settings",
        replace(pipeline_module.settings, subtitle_planner_window_tokens=40, data_dir=tmp_path),
    )
    pipeline = Pipeline(repository)

    with pytest.raises(RuntimeError, match="temporary Pass 3 failure"):
        pipeline.translate(project["id"])

    assert window_calls == [(0, 39), (40, 79)]
    assert all(
        batch["status"] == "completed"
        for batch in repository.list_subtitle_batches(part["id"])
    )
    assert repository.list_subtitle_parts(project["id"])[0]["status"] == "failed"

    pipeline.translate(project["id"])

    assert window_calls == [(0, 39), (40, 79)]
    assert audit_calls == 2
    assert repository.get_project(project["id"])["status"] == "waiting_review"
