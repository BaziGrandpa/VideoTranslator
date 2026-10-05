import pytest

from app.services.source_sentences import (
    build_source_sentences,
    classify_token_kinds,
    materialize_aligned_sentences,
    materialize_sentence_splits,
    token_kind,
)


def make_tokens(words: list[str], starts: list[int] | None = None) -> list[dict]:
    starts = starts or [index * 200 for index in range(len(words))]
    return [
        {"text": word, "start_ms": starts[index], "end_ms": starts[index] + 180}
        for index, word in enumerate(words)
    ]


def test_short_fragments_merge_in_punctuation_direction() -> None:
    tokens = make_tokens(
        ["All", "right,", "here", "we", "go.", "Yep.", "So,", "I", "placed", "the", "bet."]
    )

    sentences = build_source_sentences(tokens)

    assert [item["source_sentence"] for item in sentences] == [
        "All right, here we go. Yep.",
        "So, I placed the bet.",
    ]
    assert [(item["start_token_id"], item["end_token_id"]) for item in sentences] == [
        (0, 5),
        (6, 10),
    ]


def test_three_word_continuation_merges_but_complete_sentence_stays() -> None:
    tokens = make_tokens(
        ["Oh", "my", "god,", "that", "was", "wild.", "It", "feels", "amazing."]
    )

    sentences = build_source_sentences(tokens)

    assert [item["source_sentence"] for item in sentences] == [
        "Oh my god, that was wild.",
        "It feels amazing.",
    ]


def test_long_pause_keeps_yes_as_its_own_sentence() -> None:
    tokens = make_tokens(
        ["We", "agree.", "Yes.", "Next", "point."],
        [0, 200, 2000, 4000, 4200],
    )

    sentences = build_source_sentences(tokens)

    assert [item["source_sentence"] for item in sentences] == ["We agree.", "Yes.", "Next point."]


def test_stage_directions_are_hidden_but_token_coverage_is_preserved() -> None:
    tokens = make_tokens(["Hello.", "[music]", ">>", "Welcome", "back."])

    sentences = build_source_sentences(tokens)

    assert token_kind("[music]") == "stage_direction"
    assert token_kind(">>") == "speaker_marker"
    assert [item["source_sentence"] for item in sentences] == ["Hello.", "Welcome back."]
    assert sentences[0]["start_token_id"] == 0
    assert sentences[0]["end_token_id"] == 1
    assert sentences[1]["start_token_id"] == 2
    assert sentences[1]["end_token_id"] == 4
    assert sentences[1]["start_ms"] == tokens[3]["start_ms"]


def test_stage_direction_split_across_tokens_is_hidden() -> None:
    tokens = make_tokens(["Well,", "[clears", "throat]", "that", "worked."])

    sentences = build_source_sentences(tokens)
    kinds = classify_token_kinds(tokens)

    assert sentences[0]["source_sentence"] == "Well, that worked."
    assert sentences[0]["hidden_token_ids"] == [1, 2]
    assert kinds[1:3] == ["stage_direction", "stage_direction"]


def test_unknown_bracketed_text_is_preserved() -> None:
    tokens = make_tokens(["[two", "hours", "later]", "we", "continued."])

    sentences = build_source_sentences(tokens)

    assert sentences[0]["source_sentence"] == "[two hours later] we continued."


def test_pass2_requires_exact_ordered_sentence_id_coverage() -> None:
    tokens = make_tokens(["First", "complete", "idea.", "Second", "complete", "idea."])
    sentences = build_source_sentences(tokens)

    with pytest.raises(ValueError, match="coverage mismatch"):
        materialize_aligned_sentences(
            tokens,
            sentences,
            {"sentences": [{"source_sentence_id": 2, "chinese_sentence": "第二个意思"}]},
        )

    segments = materialize_aligned_sentences(
        tokens,
        sentences,
        {
            "sentences": [
                {"source_sentence_id": 1, "chinese_sentence": "第一个意思。"},
                {"source_sentence_id": 2, "chinese_sentence": "第二个意思。"},
            ]
        },
    )
    assert [item["source_sentence_id"] for item in segments] == [1, 2]
    assert [item["translated_text"] for item in segments] == ["第一个意思。", "第二个意思。"]
    assert all(item["quality_flags"] == [] for item in segments)
    assert all(item["status"] == "machine_translated" for item in segments)


def test_pass3_infers_gap_free_starts_from_end_token_ids() -> None:
    tokens = make_tokens(["one", "two", "three", "four", "five", "six."])
    source = build_source_sentences(tokens)
    parents = materialize_aligned_sentences(
        tokens,
        source,
        {"sentences": [{"source_sentence_id": 1, "chinese_sentence": "第一部分，第二部分。"}]},
    )

    split = materialize_sentence_splits(
        tokens,
        parents,
        {
            "sentences": [
                {
                    "source_sentence_id": 1,
                    "subsentences": [
                        {"end_token_id": 2, "chinese_sentence": "第一部分。"},
                        {"end_token_id": 5, "chinese_sentence": "第二部分。"},
                    ],
                }
            ]
        },
    )

    assert [(item["start_token_id"], item["end_token_id"]) for item in split] == [(0, 2), (3, 5)]
    assert [item["sub_id"] for item in split] == [1, 2]


def test_pass3_rejects_incomplete_parent_coverage() -> None:
    tokens = make_tokens(["one", "two", "three."])
    source = build_source_sentences(tokens)
    parents = materialize_aligned_sentences(
        tokens,
        source,
        {"sentences": [{"source_sentence_id": 1, "chinese_sentence": "一个完整意思。"}]},
    )

    with pytest.raises(ValueError, match="parent boundary"):
        materialize_sentence_splits(
            tokens,
            parents,
            {
                "sentences": [
                    {
                        "source_sentence_id": 1,
                        "subsentences": [{"end_token_id": 1, "chinese_sentence": "没覆盖完。"}],
                    }
                ]
            },
        )
