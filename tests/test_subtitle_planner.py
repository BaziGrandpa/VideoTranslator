import json

from app.services.cues import build_cues, materialize_aligned_cues
from app.services.prompts import (
    build_cue_alignment_prompt,
    build_part_translation_prompt,
    build_subcue_boundary_prompt,
    build_subcue_split_prompt,
    build_translation_context_prompt,
)
from app.services.subtitle_planner import (
    CUE_ALIGNMENT_SCHEMA_PATH,
    PUNCTUATION_SCHEMA_PATH,
    SUBCUE_BOUNDARY_SCHEMA_PATH,
    SUBCUE_SPLIT_SCHEMA_PATH,
    TRANSLATION_CONTEXT_CHUNK_TERM_LIMIT,
    TRANSLATION_CONTEXT_SCHEMA_PATH,
    TRANSLATION_CONTEXT_TERM_LIMIT,
    _add_term_occurrence_counts,
    _merge_short_token_subcues,
    _normalize_translation_context,
    _select_consistency_terms,
    _split_chinese_on_punctuation,
    generate_translation_context,
    merge_short_chinese_cues,
    split_long_cues,
    translate_part,
)
from app.services.subtitle_quality import RULES
from app.services.translator import _cli_call_label


def test_translation_context_prompt_uses_plain_project_source_and_name_policy() -> None:
    prompt = build_translation_context_prompt(
        "Katie talks about doom scrolling with Bonnie Blue.",
        "zh-CN",
        "Test",
        None,
        term_limit=TRANSLATION_CONTEXT_TERM_LIMIT,
        chunk_term_limit=TRANSLATION_CONTEXT_CHUNK_TERM_LIMIT,
    )
    transcript = prompt.split("BEGIN TRANSCRIPT\n", 1)[1].split("\nEND TRANSCRIPT", 1)[0]

    assert transcript == "Katie talks about doom scrolling with Bonnie Blue."
    assert "token_id" not in transcript
    assert "start_ms" not in transcript
    assert "Katie stays Katie" not in prompt
    assert "Treat person names like any other term" in prompt
    assert "Do not preserve a term solely because it is a proper noun" in prompt
    assert "channel names and usernames must remain" not in prompt
    assert "Frequency is evidence, not the rule by itself" in prompt
    assert "generic common nouns" in prompt
    assert "Do not pad the list" in prompt


def test_translation_context_normalization_keeps_edited_person_targets() -> None:
    context = _normalize_translation_context(
        {
            "overview": "Overview",
            "style": "Natural",
            "terms": [
                {
                    "source": "Marx",
                    "target": "马克思",
                    "category": "person",
                    "must_use": True,
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

    assert context["terms"][0]["target"] == "马克思"
    assert context["terms"][0]["must_use"] is True
    assert context["terms"][1]["target"] == "刷负面新闻"
    assert context["terms"][1]["must_use"] is False


def test_translation_context_terms_are_capped_and_counted_across_the_transcript() -> None:
    context = _normalize_translation_context(
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
                for index in range(TRANSLATION_CONTEXT_TERM_LIMIT + 5)
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

    assert len(context["terms"]) == TRANSLATION_CONTEXT_TERM_LIMIT
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
                for index in range(TRANSLATION_CONTEXT_TERM_LIMIT)
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


def test_target_text_prompt_has_plain_source_without_token_metadata() -> None:
    prompt = build_part_translation_prompt(
        "Dopamine. It is a brain chemical.",
        "zh-CN",
        "Test",
        1,
        2,
        {
            "overview": "Overview",
            "style": "Natural",
            "terms": [{"source": "dopamine", "target": "多巴胺", "must_use": True}],
            "occurrence_count": 9,
            "target_text": "should not appear in Pass 0 payload",
        },
    )
    transcript = prompt.split("BEGIN TRANSCRIPT\n", 1)[1].split("\nEND TRANSCRIPT", 1)[0]
    assert transcript == "Dopamine. It is a brain chemical."
    assert "token_id" not in transcript
    assert "start_ms" not in transcript
    assert "JSON" not in transcript
    assert "Real person names remain in their original Latin spelling" not in prompt
    assert "Follow Pass 0 terms" in prompt
    assert "must_use targets exactly" in prompt
    assert "occurrence_count" not in prompt
    assert "should not appear in Pass 0 payload" not in prompt
    assert "多巴胺" in prompt
    assert "Do not use em dashes" in prompt
    assert "Never begin or end the translation or any clause with a dash" in prompt
    assert (
        f"about {RULES.max_target_units} Chinese characters with no ，。 or ；"
        in prompt
    )
    assert "Half-width , . ; : ? ! do not count as breaks" in prompt


def test_target_text_keeps_provider_out_of_the_cli_label(monkeypatch) -> None:
    labels: list[str] = []

    def fake_grok(_prompt: str) -> str:
        labels.append(_cli_call_label.get())
        return "你好"

    monkeypatch.setattr("app.services.subtitle_planner.command_available", lambda _command: True)
    monkeypatch.setattr("app.services.subtitle_planner.run_grok_text", fake_grok)

    result, provider = translate_part(
        [{"text": "Hello"}],
        "zh-CN",
        "Title",
        1,
        2,
        providers=("grok",),
    )

    assert provider == "grok"
    assert result["target_text"] == "你好"
    assert len(labels) == 1
    assert labels[0].startswith("Pass 1 via grok (part 1/2")


def test_schema_call_keeps_provider_out_of_the_cli_label(monkeypatch) -> None:
    labels: list[str] = []

    def fake_grok(_prompt: str, _schema_path) -> dict:
        labels.append(_cli_call_label.get())
        return {"overview": "Overview", "style": "Natural", "terms": []}

    monkeypatch.setattr("app.services.subtitle_planner.command_available", lambda _command: True)
    monkeypatch.setattr("app.services.subtitle_planner.run_grok_with_schema", fake_grok)

    context, provider = generate_translation_context(
        [{"text": "Hello"}],
        "zh-CN",
        "Title",
        providers=("grok",),
    )

    assert provider == "grok"
    assert context["overview"] == "Overview"
    assert len(labels) == 1
    assert labels[0].startswith("Pass 0 via grok")


def test_part_pass2_prompt_places_pass1_back_with_adjacent_coherence() -> None:
    prompt = build_cue_alignment_prompt(
        [
            {
                "cue_id": 1,
                "source_text": "This fact belongs here.",
                "start_token_id": 0,
                "end_token_id": 3,
            },
            {
                "cue_id": 2,
                "source_text": "The next fact belongs there.",
                "start_token_id": 4,
                "end_token_id": 8,
            },
        ],
        "zh-CN",
        "Title",
        1,
        1,
        {"target_text": "完整参考译文。", "terms": []},
    )

    assert "place that Pass 1 text back onto the English cues" in prompt
    assert "Adjacent cues are one unfolding thought" in prompt
    assert "Do not translate the English from scratch" in prompt
    assert "all of a sudden" not in prompt


def test_pass3_prompt_contains_parent_source_chinese_and_all_token_ids() -> None:
    tokens = [
        {"text": "This", "start_ms": 0, "end_ms": 100},
        {"text": "[music]", "start_ms": 100, "end_ms": 200},
        {"text": "continues.", "start_ms": 200, "end_ms": 400},
    ]
    sentences = build_cues(tokens)
    parents = materialize_aligned_cues(
        tokens,
        sentences,
        {
            "cues": [
                {"cue_id": 1, "target_text": "这是一条需要拆开的中文父句。"}
            ]
        },
    )
    prompt = build_subcue_split_prompt(
        tokens,
        parents,
        sentences,
        "zh-CN",
        "Title",
    )

    assert "This continues." in prompt
    assert "这是一条需要拆开的中文父句" in prompt
    assert '"token_id":0' in prompt
    assert '"token_id":1' in prompt
    assert '"kind":"stage_direction"' in prompt
    assert "end_token_id" in prompt
    assert "CHINESE SEGMENTATION IS YOUR FIRST AND PRIMARY TASK" in prompt
    assert "Start from the Chinese wording and its punctuation" in prompt
    assert "English Token ranges are timing coordinates" in prompt
    assert "Hard max: every Chinese sub-cue must be at most 24 display units" in prompt
    assert "Prefer 18 display units" in prompt
    assert "GLOBAL PROJECT CONTEXT" not in prompt


def test_pass3_1_prompt_prioritizes_chinese_and_balances_speech_tokens() -> None:
    tokens = [
        {"text": "One", "start_ms": 0, "end_ms": 100},
        {"text": "two", "start_ms": 100, "end_ms": 200},
        {"text": "three.", "start_ms": 200, "end_ms": 300},
    ]
    parent = {
        "cue_id": 1,
        "source_text": "One two three.",
        "start_token_id": 0,
        "end_token_id": 2,
    }
    prompt = build_subcue_boundary_prompt(
        tokens, [parent], [parent], {1: ["中文一", "中文二"]}, "zh-CN", "Title"
    )

    assert "The fixed Chinese sub-cues take priority" in prompt
    assert "spoken-Token counts roughly in proportion" in prompt
    assert "avoid assigning only 1–3 spoken Tokens" in prompt
    assert "English grammatical completeness is not required" in prompt
    assert "no elapsed-time information is provided here" in prompt
    assert '"start_ms"' not in prompt


def test_pass3_runs_boundary_only_and_free_split_requests(monkeypatch) -> None:
    words = ["First", "idea.", "Second", "idea.", "Third", "idea."]
    starts = [0, 100, 2000, 2100, 4000, 4100]
    tokens = [
        {"text": word, "start_ms": starts[index], "end_ms": starts[index] + 100}
        for index, word in enumerate(words)
    ]
    sentences = build_cues(tokens)
    parents = materialize_aligned_cues(
        tokens,
        sentences,
        {
            "cues": [
                {"cue_id": 1, "target_text": "好的，这是一句有标点的长中文"},
                {"cue_id": 2, "target_text": "这是一句完全没有任何标点而且需要模型自行拆分的长中文"},
                {"cue_id": 3, "target_text": "短的，第三句"},
            ]
        },
    )
    calls = []

    def fake_call(_provider, prompt, *_args, **_kwargs):
        calls.append(prompt)
        if "Pass 3-1" in prompt:
            return {
                "cues": [
                    {
                        "cue_id": 1,
                        "boundaries": [
                            {"subcue_id": 1, "end_token_id": 1}
                        ],
                    },
                    {
                        "cue_id": 3,
                        "boundaries": [
                            {"subcue_id": 1, "end_token_id": 5}
                        ],
                    },
                ]
            }
        return {
            "cues": [
                {
                    "cue_id": 2,
                    "subcues": [{"end_token_id": 3, "target_text": "模型拆分句"}],
                },
            ]
        }

    monkeypatch.setattr("app.services.subtitle_planner._audited_call", fake_call)

    segments, provider, raw_subcues = split_long_cues(
        tokens,
        parents,
        sentences,
        "zh-CN",
        "Title",
        {"terms": []},
        providers=("codex",),
        include_subcues=True,
    )

    assert provider == "codex"
    assert len(calls) == 2
    assert "Pass 3-1" in calls[0]
    assert "Pass 3-2" in calls[1]
    assert '"cue_id":1' in calls[0]
    assert '"cue_id":1' not in calls[1]
    assert [segment["cue_id"] for segment in segments] == [1, 2, 3]
    assert [(row["cue_id"], row["split_pass"]) for row in raw_subcues] == [
        (1, "pass3_1"), (3, "pass3_1"), (2, "pass3_2"),
    ]
    assert segments[0]["target_text"] == "好的，这是一句有标点的长中文"
    assert segments[1]["target_text"] == "模型拆分句"
    assert segments[2]["target_text"] == "短的，第三句"


def test_pass3_stops_after_one_round_when_all_ids_are_valid(monkeypatch) -> None:
    tokens = [
        {"text": "One", "start_ms": 0, "end_ms": 100},
        {"text": "idea.", "start_ms": 100, "end_ms": 200},
    ]
    sentences = build_cues(tokens)
    parents = materialize_aligned_cues(
        tokens,
        sentences,
        {"cues": [{"cue_id": 1, "target_text": "一句很长的中文。"}]},
    )
    calls = []

    def fake_call(*_args, **_kwargs):
        calls.append(1)
        return {
            "cues": [
                {
                    "cue_id": 1,
                    "subcues": [{"end_token_id": 1, "target_text": "短句。"}],
                }
            ]
        }

    monkeypatch.setattr("app.services.subtitle_planner._audited_call", fake_call)

    segments, _provider = split_long_cues(
        tokens,
        parents,
        sentences,
        "zh-CN",
        "Title",
        {"terms": []},
        providers=("codex",),
    )

    assert len(calls) == 1
    assert segments[0]["target_text"] == "短句。"


def test_pass3_keeps_free_split_output_over_24_units_for_review(
    monkeypatch,
) -> None:
    tokens = [
        {"text": "One", "start_ms": 0, "end_ms": 100},
        {"text": "long", "start_ms": 100, "end_ms": 200},
        {"text": "source", "start_ms": 200, "end_ms": 300},
        {"text": "sentence.", "start_ms": 300, "end_ms": 400},
    ]
    sentences = build_cues(tokens)
    parents = materialize_aligned_cues(
        tokens,
        sentences,
        {"cues": [{"cue_id": 1, "target_text": "一个需要拆分的中文父句"}]},
    )
    calls = []

    def fake_call(*_args, **_kwargs):
        calls.append(1)
        return {
            "cues": [
                {
                    "cue_id": 1,
                    "subcues": [
                        {
                            "end_token_id": 3,
                            "target_text": "这是一个明显超过二十四个显示单位而且必须进入人工审核的中文句子",
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr("app.services.subtitle_planner._audited_call", fake_call)

    segments, _provider = split_long_cues(
        tokens,
        parents,
        sentences,
        "zh-CN",
        "Title",
        {"terms": []},
        providers=("codex",),
    )

    assert len(calls) == 1
    assert segments[0]["target_text"] == "这是一个明显超过二十四个显示单位而且必须进入人工审核的中文句子"
    assert segments[0]["quality_flags"] == []
    assert segments[0]["status"] == "machine_translated"


def test_python_pass3_split_uses_punctuation_and_merges_short_fragments() -> None:
    assert _split_chinese_on_punctuation(
        "只要你能忘记下周的责任确实存在，取消日程、什么都不用做，真的会让人无比自由"
    ) == [
        "只要你能忘记下周的责任确实存在，",
        "取消日程、什么都不用做，",
        "真的会让人无比自由",
    ]
    assert _split_chinese_on_punctuation("好的，长句") == ["好的，长句"]
    assert _split_chinese_on_punctuation(
        "很长的第一句话。我们输了。太蠢了"
    ) == ["很长的第一句话。", "我们输了。太蠢了"]
    assert _split_chinese_on_punctuation("没有任何标点的长句") is None


def test_python_pass3_keeps_closing_quotes_with_sentence_punctuation() -> None:
    assert _split_chinese_on_punctuation(
        "人们：“你得亲自去体验。”但苏菲派有不同的神秘传统，核心在于意识"
    ) == [
        "人们：“你得亲自去体验。”",
        "但苏菲派有不同的神秘传统，",
        "核心在于意识",
    ]
    assert _split_chinese_on_punctuation(
        "他说：“这是很重要的事！”接着大家又开始讨论。"
    ) == [
        "他说：“这是很重要的事！”",
        "接着大家又开始讨论。",
    ]


def test_short_token_tail_merges_with_previous_chinese_subcue() -> None:
    words = ["and", "that", "has", "been", "built", "upon."]
    tokens = [
        {"text": word, "start_ms": index * 100, "end_ms": (index + 1) * 100}
        for index, word in enumerate(words)
    ]
    segments = [
        {
            "cue_id": 82, "subcue_id": 4,
            "start_token_id": 0, "end_token_id": 4,
            "start_ms": 0, "end_ms": 500,
            "source_text": "and that has been built",
            "target_text": "以及围绕它建立的一切", "quality_flags": [],
        },
        {
            "cue_id": 82, "subcue_id": 5,
            "start_token_id": 5, "end_token_id": 5,
            "start_ms": 500, "end_ms": 600,
            "source_text": "upon.", "target_text": "也同样如此", "quality_flags": [],
        },
    ]

    merged, counts = _merge_short_token_subcues(tokens, segments)

    assert counts == {82: 1}
    assert len(merged) == 1
    assert merged[0]["source_text"] == "and that has been built upon."
    assert merged[0]["target_text"] == "以及围绕它建立的一切，也同样如此"
    assert (merged[0]["start_token_id"], merged[0]["end_token_id"]) == (0, 5)
    assert (merged[0]["start_ms"], merged[0]["end_ms"]) == (0, 600)
    assert merged[0]["subcue_id"] == 1


def test_short_token_subcue_stays_when_both_merges_exceed_24_units() -> None:
    tokens = [
        {"text": word, "start_ms": index * 100, "end_ms": (index + 1) * 100}
        for index, word in enumerate(["one", "two", "three", "four", "five", "six"])
    ]
    segments = [
        {
            "cue_id": 1, "subcue_id": index + 1,
            "start_token_id": index * 2, "end_token_id": index * 2 + 1,
            "start_ms": index * 200, "end_ms": (index + 1) * 200,
            "source_text": "", "target_text": chinese,
        }
        for index, chinese in enumerate(["中" * 21, "中文短句", "中" * 21])
    ]

    merged, counts = _merge_short_token_subcues(tokens, segments)

    assert counts == {}
    assert len(merged) == 3
    assert [item["target_text"] for item in merged] == [
        "中" * 21, "中文短句", "中" * 21,
    ]


def test_short_token_subcue_does_not_cross_source_text() -> None:
    tokens = [
        {"text": word, "start_ms": index * 100, "end_ms": (index + 1) * 100}
        for index, word in enumerate(["one", "two.", "three", "four."])
    ]
    segments = [
        {
            "cue_id": index + 1, "subcue_id": 1,
            "start_token_id": index * 2, "end_token_id": index * 2 + 1,
            "start_ms": index * 200, "end_ms": (index + 1) * 200,
            "source_text": "", "target_text": "短句",
        }
        for index in range(2)
    ]

    merged, counts = _merge_short_token_subcues(tokens, segments)

    assert counts == {}
    assert len(merged) == 2


def test_short_chinese_full_cue_merges_backward_and_keeps_source_ids() -> None:
    words = [
        "and", "they're", "going", "to", "be", "patronizing", "you,",
        "kind", "of", "look", "down", "on", "you.",
        "Next", "thought.",
    ]
    tokens = [
        {"text": word, "start_ms": index * 100, "end_ms": (index + 1) * 100}
        for index, word in enumerate(words)
    ]
    segments = [
        {
            "cue_id": 6, "subcue_id": 0,
            "start_token_id": 0, "end_token_id": 6,
            "start_ms": 0, "end_ms": 700,
            "source_text": "and they're going to be patronizing you,",
            "target_text": "他们可能会用居高临下的态度",
        },
        {
            "cue_id": 7, "subcue_id": 0,
            "start_token_id": 7, "end_token_id": 12,
            "start_ms": 700, "end_ms": 1300,
            "source_text": "kind of look down on you.",
            "target_text": "看你。",
        },
        {
            "cue_id": 8, "subcue_id": 0,
            "start_token_id": 13, "end_token_id": 14,
            "start_ms": 1300, "end_ms": 1500,
            "source_text": "Next thought.", "target_text": "下一句",
        },
    ]

    merged, changes = merge_short_chinese_cues(tokens, segments)

    assert [row["cue_id"] for row in merged] == [6, 8]
    assert changes == [{
        "kept_cue_id": 6,
        "absorbed_cue_id": 7,
        "start_token_id": 0,
        "end_token_id": 12,
    }]
    assert merged[0]["target_text"] == "他们可能会用居高临下的态度看你。"
    assert merged[0]["source_text"] == (
        "and they're going to be patronizing you, kind of look down on you."
    )
    assert (merged[0]["start_ms"], merged[0]["end_ms"]) == (0, 1300)
    assert (merged[0]["start_token_id"], merged[0]["end_token_id"]) == (0, 12)
    assert segments[0]["end_token_id"] == 6


def test_short_chinese_full_cue_requires_period_and_24_unit_budget() -> None:
    tokens = [
        {"text": str(index), "start_ms": index * 100, "end_ms": (index + 1) * 100}
        for index in range(3)
    ]
    base = [
        {
            "cue_id": 1, "subcue_id": 0,
            "start_token_id": 0, "end_token_id": 0,
            "start_ms": 0, "end_ms": 100,
            "source_text": "0", "target_text": "中" * 24,
        },
        {
            "cue_id": 2, "subcue_id": 0,
            "start_token_id": 1, "end_token_id": 1,
            "start_ms": 100, "end_ms": 200,
            "source_text": "1", "target_text": "看你。",
        },
        {
            "cue_id": 3, "subcue_id": 0,
            "start_token_id": 2, "end_token_id": 2,
            "start_ms": 200, "end_ms": 300,
            "source_text": "2", "target_text": "后文",
        },
    ]
    assert merge_short_chinese_cues(tokens, base)[1] == []
    base[0]["target_text"] = "前文"
    base[1]["target_text"] = "看你"
    assert merge_short_chinese_cues(tokens, base)[1] == []
    base[1]["target_text"] = "看你。"
    base[0]["subcue_id"] = 1
    assert merge_short_chinese_cues(tokens, base)[1] == []


def test_pass3_1_uses_proportional_fallback_for_non_monotonic_boundaries(
    monkeypatch,
) -> None:
    words = ["Pretty", "crazy", "to", "think", "savory", "food", "because", "past", "days."]
    tokens = [
        {"text": word, "start_ms": index * 100, "end_ms": (index + 1) * 100}
        for index, word in enumerate(words)
    ]
    sentences = build_cues(tokens)
    parents = materialize_aligned_cues(
        tokens,
        sentences,
        {
            "cues": [
                {
                    "cue_id": 1,
                    "target_text": "因为过去两天的经历，咸味食物竟然无法吸引我，这件事实在太疯狂了",
                }
            ]
        },
    )

    def fake_call(*_args, **_kwargs):
        return {
            "cues": [
                {
                    "cue_id": 1,
                    "boundaries": [
                        {"subcue_id": 1, "end_token_id": 7},
                        {"subcue_id": 2, "end_token_id": 6},
                        {"subcue_id": 3, "end_token_id": 8},
                    ],
                }
            ]
        }

    monkeypatch.setattr("app.services.subtitle_planner._audited_call", fake_call)
    segments, _provider = split_long_cues(
        tokens,
        parents,
        sentences,
        "zh-CN",
        "Title",
        {"terms": []},
        providers=("codex",),
    )

    assert [item["target_text"] for item in segments] == [
        "因为过去两天的经历，咸味食物竟然无法吸引我，",
        "这件事实在太疯狂了",
    ]
    assert [item["end_token_id"] for item in segments] == sorted(
        item["end_token_id"] for item in segments
    )
    assert segments[-1]["end_token_id"] == 8
def test_prompt_schema_paths_match_current_output_contracts() -> None:
    for path in (
        PUNCTUATION_SCHEMA_PATH,
        TRANSLATION_CONTEXT_SCHEMA_PATH,
        CUE_ALIGNMENT_SCHEMA_PATH,
        SUBCUE_BOUNDARY_SCHEMA_PATH,
        SUBCUE_SPLIT_SCHEMA_PATH,
    ):
        assert path.is_file(), path
        json.loads(path.read_text(encoding="utf-8"))
    for path in (CUE_ALIGNMENT_SCHEMA_PATH, SUBCUE_BOUNDARY_SCHEMA_PATH, SUBCUE_SPLIT_SCHEMA_PATH):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["required"] == ["cues"]
