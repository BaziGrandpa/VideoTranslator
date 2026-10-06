from app.services.subtitle_quality import (
    RULES,
    analyze_quality,
    segment_review_problems,
    stamp_segment_review_flags,
    summarize_length_budget,
    target_units,
)


def test_soft_length_limit_is_statistical_only_and_not_sent_to_review() -> None:
    target = "中" * 19
    assert analyze_quality(0, 4000, "source text", target) == []
    assert target_units(target) <= RULES.max_target_units


def test_absolute_length_limit_is_a_quality_issue() -> None:
    assert "target_text_too_long" in analyze_quality(0, 4000, "source text", "中" * 25)


def test_quality_limits_distinguish_soft_and_absolute_ceiling() -> None:
    assert "source_text_too_long" not in analyze_quality(0, 5000, "x" * 200, "短句")
    assert "target_text_above_normal_limit" not in analyze_quality(0, 5000, "Short", "中" * 19)
    assert "target_text_too_long" in analyze_quality(0, 5000, "Short", "中" * 25)
    assert target_units("中TikTok，") == 4
    assert "very_short_duration" not in analyze_quality(0, 500, "Yes.", "好")
    assert "very_short_duration" in analyze_quality(0, 500, "one two three", "可以")


def test_reading_speed_review_threshold_is_twelve_units_per_second() -> None:
    assert "high_target_reading_speed" not in analyze_quality(0, 1000, "Source", "中" * 12)
    assert "high_target_reading_speed" in analyze_quality(0, 1000, "Source", "中" * 13)


def test_long_duration_review_requires_low_text_density() -> None:
    assert "very_long_duration" not in analyze_quality(0, 5000, "Source", "中" * 4)
    assert "very_long_duration" in analyze_quality(0, 6000, "Source", "中" * 14)
    assert "very_long_duration" not in analyze_quality(0, 6000, "Source", "中" * 15)
    assert "very_long_duration" in analyze_quality(0, 11000, "Source", "中" * 20)


def test_stamp_segment_review_flags_runs_after_pass3_and_keeps_invalid_splits() -> None:
    long_chinese = "中" * 25
    stamped = stamp_segment_review_flags(
        [
            {
                "start_ms": 0,
                "end_ms": 4000,
                "source_text": "A very long English sentence.",
                "target_text": long_chinese,
                "quality_flags": [],
                "status": "machine_translated",
            },
            {
                "start_ms": 4000,
                "end_ms": 5200,
                "source_text": "Hello there.",
                "target_text": "你好",
                "quality_flags": ["pass3_invalid_output"],
                "status": "needs_review",
            },
        ]
    )

    assert stamped[0]["quality_flags"] == ["target_text_too_long"]
    assert stamped[0]["status"] == "needs_review"
    assert stamped[1]["quality_flags"] == ["pass3_invalid_output"]
    assert stamped[1]["status"] == "needs_review"
    problems = segment_review_problems(stamped[1])
    assert [item["type"] for item in problems] == ["pass3_invalid_output"]


def test_project_length_exception_budget_is_measured_without_rewriting_text() -> None:
    segments = [{"target_text": "短句"} for _ in range(99)]
    segments.append({"target_text": "中" * 19})
    summary = summarize_length_budget(segments)

    assert summary["over_soft_limit"] == 1
    assert summary["allowed_over_soft_limit"] == 2
    assert summary["within_exception_budget"] is True
