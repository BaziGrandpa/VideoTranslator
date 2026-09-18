import json

import pytest

from app.services import subtitle_planner as planner
from app.services.subtitle_quality import (
    RULES,
    analyze_quality,
    summarize_length_budget,
    target_units,
)


def make_tokens(count=24, step_ms=1000):
    return [
        {
            "text": f"word{index}",
            "start_ms": index * step_ms,
            "end_ms": (index + 1) * step_ms,
        }
        for index in range(count)
    ]


def cue(start, end, translation="Test"):
    return {"start_token_id": start, "end_token_id": end, "translation": translation}


def test_pass2_generation_does_not_run_repair_rounds(monkeypatch, tmp_path):
    calls = []

    def fake(provider, prompt, schema, model):
        calls.append((provider, prompt, schema.name, model))
        return {"cues": [cue(0, 7, "这是一条需要人工检查的较长字幕内容")]}

    monkeypatch.setattr(planner, "_call_provider", fake)
    segments, _ = planner.plan_subtitle_window(
        make_tokens(8),
        "zh-CN",
        "Title",
        0,
        7,
        {},
        providers=("codex",),
        codex_model="test-model",
        audit_dir=tmp_path,
    )

    assert len(calls) == 1
    assert calls[0][2] == "subtitle-plan.schema.json"
    assert segments[0]["status"] == "needs_review"
    events = [json.loads(path.read_text(encoding="utf-8")) for path in tmp_path.glob("*.json")]
    assert any(event["kind"] == "draft-validated" for event in events)


def test_invalid_pass2_coverage_fails_without_a_repair_call(monkeypatch):
    calls = []

    def fake(provider, prompt, schema, model):
        calls.append(schema.name)
        return {"cues": [cue(1, 3)]}

    monkeypatch.setattr(planner, "_call_provider", fake)
    with pytest.raises(RuntimeError, match="breaks token coverage"):
        planner.plan_subtitle_window(
            make_tokens(4), "zh-CN", "Title", 0, 3, {}, providers=("codex",)
        )
    assert calls == ["subtitle-plan.schema.json"]


def test_soft_length_limit_is_visible_but_not_mechanically_repaired():
    target = "中" * 19
    tokens = make_tokens(4)
    segments = planner._materialize_plan(tokens, {"cues": [cue(0, 3, target)]})

    assert segments[0]["quality_flags"] == ["target_text_above_normal_limit"]
    assert target_units(target) <= RULES.max_target_units


def test_absolute_length_limit_is_a_quality_issue():
    tokens = make_tokens(4)
    segments = planner._materialize_plan(tokens, {"cues": [cue(0, 3, "中" * 25)]})

    assert "target_text_too_long" in segments[0]["quality_flags"]


def test_pass3_does_not_run_a_second_mechanical_repair(monkeypatch):
    tokens = make_tokens(8)
    draft = planner._materialize_plan(tokens, {"cues": [cue(0, 7, "中" * 25)]})
    calls = []

    def fake(provider, prompt, schema, model, audit_dir, **metadata):
        calls.append((schema.name, metadata.get("stage")))
        return {"issues": []}

    monkeypatch.setattr(planner, "_audited_call", fake)
    result = planner.audit_subtitle_window(tokens, draft, "zh-CN", "Title", 0, 7, {}, [], "codex")

    assert calls == [("subtitle-semantic-audit.schema.json", "semantic-audit")]
    assert [item["translated_text"] for item in result] == ["中" * 25]
    assert result[0]["status"] == "needs_review"


def test_unresolved_mechanical_issue_is_left_for_human_review(monkeypatch):
    tokens = make_tokens(8)
    draft = planner._materialize_plan(tokens, {"cues": [cue(0, 7, "中" * 25)]})
    calls = []

    def fake(provider, prompt, schema, model, audit_dir, **metadata):
        calls.append(schema.name)
        return {"issues": []}

    monkeypatch.setattr(planner, "_audited_call", fake)
    result = planner.audit_subtitle_window(tokens, draft, "zh-CN", "Title", 0, 7, {}, [], "codex")

    assert calls == ["subtitle-semantic-audit.schema.json"]
    assert result[0]["status"] == "needs_review"
    assert "target_text_too_long" in result[0]["quality_flags"]


def test_indivisible_long_token_is_left_for_human_timing_review(monkeypatch):
    tokens = [
        {"text": "Here", "start_ms": 0, "end_ms": 200},
        {"text": "we", "start_ms": 200, "end_ms": 400},
        {"text": "go.", "start_ms": 400, "end_ms": 7000},
    ]
    draft = planner._materialize_plan(tokens, {"cues": [cue(0, 2, "开始吧")]})
    calls = []

    def fake(provider, prompt, schema, model, audit_dir, **metadata):
        calls.append(schema.name)
        return {"issues": []}

    monkeypatch.setattr(planner, "_audited_call", fake)
    result = planner.audit_subtitle_window(tokens, draft, "zh-CN", "Title", 0, 2, {}, [], "codex")

    assert calls == ["subtitle-semantic-audit.schema.json"]
    assert result[0]["source_text"] == "Here we go."
    assert result[0]["quality_flags"] == ["very_long_duration"]
    assert result[0]["status"] == "needs_review"


def test_quality_limits_distinguish_soft_and_absolute_ceiling():
    assert "source_text_too_long" not in analyze_quality(0, 5000, "x" * 200, "短句")
    assert "target_text_above_normal_limit" in analyze_quality(0, 5000, "Short", "中" * 19)
    assert "target_text_too_long" in analyze_quality(0, 5000, "Short", "中" * 25)
    assert target_units("中TikTok，") == 5
    assert "very_short_duration" not in analyze_quality(0, 500, "Yes.", "好")
    assert "very_short_duration" in analyze_quality(0, 500, "one two three", "可以")


def test_project_length_exception_budget_is_measured_without_rewriting_text():
    segments = [{"translated_text": "短句"} for _ in range(99)]
    segments.append({"translated_text": "中" * 19})
    summary = summarize_length_budget(segments)

    assert summary["over_soft_limit"] == 1
    assert summary["allowed_over_soft_limit"] == 2
    assert summary["within_exception_budget"] is True


def test_pass2_prompt_prioritizes_chinese_segmentation_and_length():
    tokens = make_tokens(8)
    prompt = planner._build_prompt(tokens, "zh-CN", "Title", 0, 7, {}, [], 0)

    assert "YOUR PRIMARY TASK IS CHINESE-FIRST CUE SEGMENTATION" in prompt
    assert "KEEP CHINESE CUES AT OR BELOW 18 DISPLAY UNITS!" in prompt
    assert "NEVER OUTPUT A CUE ABOVE 24 DISPLAY UNITS" in prompt
    assert "呢/了/的/吗/吧" in prompt
