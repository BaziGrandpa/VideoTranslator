import pytest

from app.services import subtitle_planner as planner


def make_tokens():
    words = [
        "it's", "police", "brutality.", "But", "it's", "kind", "of",
        "exciting", "not", "knowing", "what's", "coming", "next.",
    ]
    return [
        {"text": word, "start_ms": index * 300, "end_ms": (index + 1) * 300}
        for index, word in enumerate(words)
    ]


def test_pass3_flags_semantic_mismatch_without_rewriting(monkeypatch):
    tokens = make_tokens()
    current = planner._materialize_plan(
        tokens,
        {"cues": [{"start_token_id": 0, "end_token_id": 12, "translation": "不知道下一条"}]},
    )
    captured = {}

    def fake_call(provider, prompt, schema, model, audit_dir, **metadata):
        captured.update(prompt=prompt, schema=schema.name, metadata=metadata)
        return {
            "issues": [{
                "issue_id": "semantic-001",
                "type": "semantic_mismatch",
                "start_token_id": 0,
                "end_token_id": 12,
                "reason": "The meaning of police brutality may be missing.",
                "replacement_cues": [],
            }]
        }

    monkeypatch.setattr(planner, "_audited_call", fake_call)
    result, issues = planner.audit_subtitle_window(
        tokens, current, "zh-CN", "Test video", 0, 12,
        {"reference_translation": "下一次滑动就变成了警察暴力。"}, [], "codex",
        include_issues=True,
    )

    assert captured["schema"] == "subtitle-semantic-audit.schema.json"
    assert captured["metadata"]["stage"] == "semantic-audit"
    assert "display_units" in captured["prompt"]
    assert "duration_ms" not in captured["prompt"]
    assert "PAUSE BOUNDARIES" not in captured["prompt"]
    assert [segment["translated_text"] for segment in result] == ["不知道下一条"]
    assert issues[0]["action"] == "review_required"
    assert issues[0]["issue_types"] == ["semantic_mismatch"]
    assert issues[0]["replacement_cues"] == []
    assert result[0]["status"] == "needs_review"


def test_pass3_auto_applies_valid_too_long_resegmentation(monkeypatch):
    tokens = make_tokens()
    current = planner._materialize_plan(
        tokens,
        {"cues": [{"start_token_id": 0, "end_token_id": 12, "translation": "中" * 30}]},
    )
    monkeypatch.setattr(
        planner,
        "_audited_call",
        lambda *args, **kwargs: {
            "issues": [{
                "issue_id": "long-001",
                "type": "too_long",
                "start_token_id": 0,
                "end_token_id": 12,
                "reason": "Split the overlong cue.",
                "replacement_cues": [
                    {"start_token_id": 0, "end_token_id": 2, "translation": "这是警察暴力"},
                    {"start_token_id": 3, "end_token_id": 12,
                     "translation": "但不知道下一条是什么又有点刺激"},
                ],
            }]
        },
    )

    result, issues = planner.audit_subtitle_window(
        tokens, current, "zh-CN", "Test", 0, 12, {}, [], "codex", include_issues=True
    )

    assert [(item["start_token_id"], item["end_token_id"]) for item in result] == [
        (0, 2), (3, 12)
    ]
    assert issues[0]["auto_applied"] is True
    assert issues[0]["action"] == "auto_resegment"


def test_pass3_exposes_empty_or_invalid_too_long_replacement(monkeypatch):
    tokens = make_tokens()
    original = "中" * 30
    current = planner._materialize_plan(
        tokens,
        {"cues": [{"start_token_id": 0, "end_token_id": 12, "translation": original}]},
    )
    responses = iter([
        {"issues": [{
            "issue_id": "empty", "type": "too_long", "start_token_id": 0,
            "end_token_id": 12, "reason": "Cannot split reliably.", "replacement_cues": [],
        }]},
        {"issues": [{
            "issue_id": "still-long", "type": "too_long", "start_token_id": 0,
            "end_token_id": 12, "reason": "Attempted split.",
            "replacement_cues": [
                {"start_token_id": 0, "end_token_id": 12, "translation": "中" * 25}
            ],
        }]},
    ])
    monkeypatch.setattr(planner, "_audited_call", lambda *args, **kwargs: next(responses))

    for expected_text in ("did not provide", "exceeds 24"):
        result, issues = planner.audit_subtitle_window(
            tokens, current, "zh-CN", "Test", 0, 12, {}, [], "codex", include_issues=True
        )
        assert result[0]["translated_text"] == original
        assert issues[0]["auto_applied"] is False
        assert expected_text in issues[0]["reason"]
        assert result[0]["status"] == "needs_review"


def test_pass3_allows_human_issue_to_overlap_auto_resegmentation(monkeypatch):
    tokens = make_tokens()
    current = planner._materialize_plan(
        tokens,
        {"cues": [{"start_token_id": 0, "end_token_id": 12, "translation": "中" * 30}]},
    )
    monkeypatch.setattr(
        planner,
        "_audited_call",
        lambda *args, **kwargs: {
            "issues": [
                {
                    "issue_id": "long", "type": "too_long", "start_token_id": 0,
                    "end_token_id": 12, "reason": "Split it.",
                    "replacement_cues": [
                        {"start_token_id": 0, "end_token_id": 2, "translation": "这是警察暴力"},
                        {"start_token_id": 3, "end_token_id": 12,
                         "translation": "但后文可能漏译"},
                    ],
                },
                {
                    "issue_id": "meaning", "type": "semantic_mismatch",
                    "start_token_id": 0, "end_token_id": 12,
                    "reason": "Review the complete meaning group.", "replacement_cues": [],
                },
            ]
        },
    )

    result, issues = planner.audit_subtitle_window(
        tokens, current, "zh-CN", "Test", 0, 12, {}, [], "codex", include_issues=True
    )

    assert len(result) == 2
    assert len(issues) == 2
    assert all(segment["status"] == "needs_review" for segment in result)


def test_pass3_rejects_issue_outside_reviewed_range(monkeypatch):
    tokens = make_tokens()
    current = planner._materialize_plan(
        tokens,
        {"cues": [{"start_token_id": 0, "end_token_id": 12, "translation": "测试"}]},
    )
    monkeypatch.setattr(
        planner,
        "_audited_call",
        lambda *args, **kwargs: {
            "issues": [{
                "issue_id": "bad-range", "type": "semantic_mismatch",
                "start_token_id": 0, "end_token_id": 13,
                "reason": "Outside the Part.", "replacement_cues": [],
            }]
        },
    )

    with pytest.raises(ValueError, match="outside the reviewed Part"):
        planner.audit_subtitle_window(tokens, current, "zh-CN", "Test", 0, 12, {}, [], "codex")
