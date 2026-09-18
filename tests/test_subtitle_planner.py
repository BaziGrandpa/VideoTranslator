from copy import deepcopy
from itertools import pairwise

import pytest

from app.services.subtitle_planner import (
    GLOBAL_CONTEXT_TERM_LIMIT,
    _add_term_occurrence_counts,
    _build_global_context_prompt,
    _build_prompt,
    _build_reference_translation_prompt,
    _build_semantic_audit_prompt,
    _materialize_plan,
    _normalize_global_context,
    _planning_ranges,
    _select_consistency_terms,
)


def test_global_context_prompt_uses_plain_project_source_and_name_policy() -> None:
    prompt = _build_global_context_prompt(
        "Katie talks about doom scrolling with Bonnie Blue.",
        "zh-CN",
        "Test",
        None,
    )
    transcript = prompt.split("BEGIN TRANSCRIPT\n", 1)[1].split("\nEND TRANSCRIPT", 1)[0]

    assert transcript == "Katie talks about doom scrolling with Bonnie Blue."
    assert "token_id" not in transcript
    assert "start_ms" not in transcript
    assert "Katie stays Katie" in prompt
    assert "Bonnie Blue stays Bonnie Blue" in prompt
    assert "Do not preserve them solely because they are proper terms" in prompt
    assert "channel names and usernames must remain" not in prompt
    assert "Frequency is evidence, not the rule by itself" in prompt
    assert "generic common nouns" in prompt
    assert "Do not pad the list" in prompt


def test_global_context_normalization_preserves_only_person_names() -> None:
    context = _normalize_global_context(
        {
            "overview": "Overview",
            "style": "Natural",
            "terms": [
                {
                    "source": "Katie",
                    "target": "凯蒂",
                    "category": "person",
                    "must_use": False,
                },
                {
                    "source": "doomscrolling",
                    "target": "刷负面新闻",
                    "category": "slang",
                    "must_use": False,
                },
            ],
        }
    )

    assert context["terms"][0]["target"] == "Katie"
    assert context["terms"][0]["must_use"] is True
    assert context["terms"][1]["target"] == "刷负面新闻"


def test_global_context_terms_are_capped_and_counted_across_the_transcript() -> None:
    context = _normalize_global_context(
        {
            "overview": "Overview",
            "style": "Natural",
            "terms": [
                {
                    "source": f"term {index}",
                    "target": f"术语{index}",
                    "category": "technical",
                    "must_use": False,
                }
                for index in range(GLOBAL_CONTEXT_TERM_LIMIT + 5)
            ],
        }
    )
    counted = _add_term_occurrence_counts(
        {
            **context,
            "terms": [
                {
                    "source": "doom scrolling",
                    "target": "刷负面信息",
                    "category": "slang",
                    "must_use": True,
                }
            ],
        },
        "Doom scrolling can become doom-scrolling. I stopped doom scrolling.",
    )

    assert len(context["terms"]) == GLOBAL_CONTEXT_TERM_LIMIT
    assert counted["terms"][0]["occurrence_count"] == 3


def test_consistency_selection_prioritizes_repeated_translation_terms_over_people() -> None:
    context = {
        "overview": "Overview",
        "style": "Natural",
        "terms": [
            {
                "source": "Katie",
                "target": "Katie",
                "category": "person",
                "must_use": True,
                "occurrence_count": 50,
            },
            *[
                {
                    "source": f"candidate {index}",
                    "target": f"候选词{index}",
                    "category": "technical",
                    "must_use": False,
                    "occurrence_count": 1,
                }
                for index in range(GLOBAL_CONTEXT_TERM_LIMIT)
            ],
            {
                "source": "doomscrolling",
                "target": "沉迷刷负面信息",
                "category": "slang",
                "must_use": True,
                "occurrence_count": 7,
            },
        ],
    }

    selected = _select_consistency_terms(context)

    assert [term["source"] for term in selected["terms"]] == ["doomscrolling", "Katie"]
    assert all(term["occurrence_count"] >= 2 for term in selected["terms"])


def test_materializes_complete_token_plan() -> None:
    tokens = [
        {"text": "Many", "start_ms": 760, "end_ms": 1040},
        {"text": "people", "start_ms": 1040, "end_ms": 1400},
        {"text": "agree.", "start_ms": 1400, "end_ms": 2200},
    ]
    segments = _materialize_plan(
        tokens,
        {
            "cues": [
                {
                    "start_token_id": 0,
                    "end_token_id": 2,
                    "translation": "很多人都同意。",
                }
            ]
        },
    )
    assert segments[0]["source_text"] == "Many people agree."
    assert segments[0]["start_ms"] == 760
    assert segments[0]["end_ms"] == 2200


def test_materializer_absorbs_skipped_display_only_token_without_mutating_tokens() -> None:
    tokens = [
        {"text": "rolling.", "start_ms": 65000, "end_ms": 66159},
        {"text": ">>", "start_ms": 66159, "end_ms": 66319},
        {"text": "Lying", "start_ms": 66319, "end_ms": 66600},
        {"text": "down.", "start_ms": 66600, "end_ms": 67000},
    ]
    original_tokens = deepcopy(tokens)

    segments = _materialize_plan(
        tokens,
        {
            "cues": [
                {"start_token_id": 0, "end_token_id": 0, "translation": "开始录制"},
                {"start_token_id": 2, "end_token_id": 3, "translation": "躺下"},
            ]
        },
    )

    assert tokens == original_tokens
    assert segments[1]["start_token_id"] == 1
    assert segments[1]["end_token_id"] == 3
    assert segments[1]["start_ms"] == 66159
    assert segments[1]["source_text"] == "Lying down."
    assert segments[1]["translated_text"] == "躺下"


def test_materializer_rejects_skipped_spoken_token() -> None:
    tokens = [
        {"text": "one", "start_ms": 0, "end_ms": 100},
        {"text": "important", "start_ms": 100, "end_ms": 200},
        {"text": "thought", "start_ms": 200, "end_ms": 300},
    ]

    with pytest.raises(RuntimeError, match="expected start 0"):
        _materialize_plan(
            tokens,
            {"cues": [{"start_token_id": 1, "end_token_id": 2, "translation": "重要想法"}]},
        )


def test_materializer_discards_annotation_only_cue_and_absorbs_its_range() -> None:
    tokens = [
        {"text": "Hello.", "start_ms": 0, "end_ms": 500},
        {"text": "[snorts]", "start_ms": 500, "end_ms": 700},
        {"text": ">>", "start_ms": 700, "end_ms": 750},
        {"text": "Welcome.", "start_ms": 750, "end_ms": 1200},
    ]

    segments = _materialize_plan(
        tokens,
        {
            "cues": [
                {"start_token_id": 0, "end_token_id": 0, "translation": "你好"},
                {"start_token_id": 1, "end_token_id": 2, "translation": "哼笑"},
                {"start_token_id": 3, "end_token_id": 3, "translation": "欢迎"},
            ]
        },
    )

    assert len(segments) == 2
    assert segments[0]["end_token_id"] == 2
    assert segments[0]["source_text"] == "Hello."
    assert segments[0]["translated_text"] == "你好"
    assert segments[1]["start_token_id"] == 3


def test_materializer_absorbs_omitted_trailing_annotations() -> None:
    tokens = [
        {"text": "Done.", "start_ms": 0, "end_ms": 500},
        {"text": "[music]", "start_ms": 500, "end_ms": 900},
    ]

    segments = _materialize_plan(
        tokens,
        {"cues": [{"start_token_id": 0, "end_token_id": 0, "translation": "结束"}]},
    )

    assert segments[0]["end_token_id"] == 1
    assert segments[0]["end_ms"] == 900
    assert segments[0]["source_text"] == "Done."


def test_window_prompt_uses_full_reference_translation() -> None:
    tokens = [
        {"text": f"word-{index}", "start_ms": index * 100, "end_ms": index * 100 + 90}
        for index in range(100)
    ]
    prompt = _build_prompt(
        tokens,
        "zh-CN",
        "Title",
        10,
        19,
        {
            "reference_translation": "这是整段完整、连贯的参考译文。",
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
        [{"source_text": "Previous cue", "translated_text": "上一句。"}],
        5,
    )
    assert "word-10" in prompt
    assert "word-24" in prompt
    assert "word-25" not in prompt
    assert "word-0" not in prompt
    assert "上一句" in prompt
    assert "这是整段完整、连贯的参考译文" in prompt
    assert "authoritative semantic inventory" in prompt
    assert "Never shorten, summarize, generalize or drop content" in prompt
    assert "Use Chinese punctuation in the Pass 1 passage as the first boundary proposal" in prompt
    assert "covered Token range does not prove that its meaning was translated" in prompt
    assert "Omit speaker markers" not in prompt
    assert "standalone punctuation or symbols" in prompt
    assert "Shorter cues are welcome" in prompt
    assert "KEEP CHINESE CUES AT OR BELOW 18 DISPLAY UNITS!" in prompt
    assert "NEVER OUTPUT A CUE ABOVE 24 DISPLAY UNITS" in prompt
    assert "Katie" in prompt


def test_semantic_audit_prompt_uses_reference_tokens_and_current_cues() -> None:
    tokens = [
        {"text": "police", "start_ms": 0, "end_ms": 300},
        {"text": "brutality.", "start_ms": 300, "end_ms": 700},
    ]
    segments = _materialize_plan(
        tokens,
        {"cues": [{"start_token_id": 0, "end_token_id": 1, "translation": "发生了一件事"}]},
    )
    prompt = _build_semantic_audit_prompt(
        tokens,
        segments,
        "zh-CN",
        "Title",
        0,
        1,
        {"reference_translation": "画面变成了警察暴力。"},
        [],
    )
    assert "画面变成了警察暴力" in prompt
    assert "police" in prompt
    assert "发生了一件事" in prompt
    assert "meaning still appears missing" in prompt
    assert 'Return {"issues":[]}' in prompt
    assert "[0,0,300" not in prompt
    assert "start_ms" not in prompt
    assert "duration" not in prompt.lower()


def test_reference_translation_prompt_has_plain_source_without_token_metadata() -> None:
    prompt = _build_reference_translation_prompt(
        "Dopamine. It is a brain chemical.", "zh-CN", "Test", 1, 2
    )
    transcript = prompt.split("BEGIN TRANSCRIPT\n", 1)[1].split("\nEND TRANSCRIPT", 1)[0]
    assert transcript == "Dopamine. It is a brain chemical."
    assert "token_id" not in transcript
    assert "start_ms" not in transcript
    assert "JSON" not in transcript
    assert "Real person names remain in their original Latin spelling" in prompt
    assert "do not preserve a term merely because it is a channel name" in prompt


def test_planning_ranges_cover_requested_part() -> None:
    tokens = [
        {
            "text": "end." if index in {24, 49, 74} else "word",
            "start_ms": index,
            "end_ms": index + 1,
        }
        for index in range(100)
    ]
    ranges = _planning_ranges(tokens, 30, 10, 89)
    assert ranges[0][0] == 10
    assert ranges[-1][1] == 89
    assert all(left[1] + 1 == right[0] for left, right in pairwise(ranges))
