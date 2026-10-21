from __future__ import annotations

from pathlib import Path

import app.main as main_module
from app.cards.model import CardPayload
from app.cards.repository import CardRepository
from app.db import init_db
from app.pipeline import Pipeline
from app.repository import Repository
from app.services import card_generation
from app.services.card_candidates import anchor_candidates


def test_pass0_cards_keep_raw_anchor_and_drop_only_unmatched_candidate() -> None:
    tokens = [
        {"token_id": 0, "normalized_text": "The"},
        {"token_id": 1, "normalized_text": "pairaformis"},
        {"token_id": 2, "normalized_text": "muscle"},
        {"token_id": 3, "normalized_text": "matters."},
    ]
    candidates = [
        {"source": "pairaformis muscle", "display": "Piriformis muscle"},
        {"source": "Nonexistent term", "display": "Nonexistent term"},
    ]

    assert anchor_candidates(candidates, tokens) == [
        {"source": "pairaformis muscle", "display": "Piriformis muscle",
         "start_token_id": 1, "end_token_id": 2}
    ]


def test_pass0_review_saves_only_matched_raw_terms(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "pass0.db"
    init_db(database)
    projects = Repository(database)
    project = projects.create_project("https://youtu.be/example")
    projects.replace_tokens(project["id"], [
        {"text": "pairaformis", "start_ms": 1000, "end_ms": 1500},
        {"text": "muscle", "start_ms": 1500, "end_ms": 2000},
    ])
    projects.update_project(
        project["id"], status="waiting_context_review", stage="waiting_context_review",
        translation_context={"overview": "Review", "style": "", "terms": []},
    )
    monkeypatch.setattr(main_module, "repository", projects)
    payload = {
        "overview": "Review", "style": "", "terms": [],
        "card_candidates": [
            {"source": "pairaformis muscle", "display": "Piriformis muscle"},
            {"source": "imaginary phrase", "display": "Imaginary phrase"},
        ],
    }

    context = main_module.normalize_translation_context(
        main_module.TranslationContextUpdate.model_validate(payload), project["id"],
        projects.get_project(project["id"])["translation_context"],
    )
    projects.update_project(project["id"], translation_context=context)

    assert context["card_candidates"] == [
        {"source": "pairaformis muscle", "display": "Piriformis muscle",
         "start_token_id": 0, "end_token_id": 1}
    ]
    assert projects.get_project(project["id"])["translation_context"]["overview"] == "Review"


def test_refresh_card_candidates_keeps_reviewed_translation_guidance(
    tmp_path: Path, monkeypatch,
) -> None:
    import app.pipeline as pipeline_module

    database = tmp_path / "pass0.db"
    init_db(database)
    projects = Repository(database)
    project = projects.create_project("https://youtu.be/example")
    projects.replace_tokens(project["id"], [
        {"text": "piriformis", "start_ms": 1000, "end_ms": 1500},
        {"text": "muscle", "start_ms": 1500, "end_ms": 2000},
    ])
    original = {"overview": "Reviewed overview", "style": "Reviewed style", "terms": [
        {"source": "muscle", "target": "肌肉", "category": "technical", "must_use": False},
    ]}
    projects.update_project(
        project["id"], status="waiting_context_review", stage="waiting_context_review",
        translation_context=original,
    )
    monkeypatch.setattr(Pipeline, "project_dir", lambda _self, _id: tmp_path)
    monkeypatch.setattr(pipeline_module, "generate_translation_context", lambda *_args, **_kwargs: (
        {"overview": "Discard this", "style": "Discard this", "terms": [],
         "card_candidates": [{"source": "piriformis muscle", "display": "Piriformis muscle",
                              "start_token_id": 0, "end_token_id": 1}]},
        "codex",
    ))

    result = Pipeline(projects).refresh_card_candidates(project["id"])

    assert result == {"count": 1, "provider": "codex"}
    saved = projects.get_project(project["id"])["translation_context"]
    assert {key: saved[key] for key in original} == original
    assert saved["card_candidates"][0]["source"] == "piriformis muscle"


def test_card_copy_reads_full_part_and_keeps_longer_explanation(tmp_path: Path, monkeypatch) -> None:
    candidates = [
        {"source": "pairaformis", "display": "Piriformis"},
        {"source": "second term", "display": "Second term"},
    ]
    captured = {}

    def fake_call(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return {"cards": [
            {"source": "pairaformis", "chinese_term": "梨状肌",
             "explanation": "髋部深层肌肉"},
            {"source": "second term", "chinese_term": "第二术语",
             "explanation": "这是与髋部稳定有关的概念，可以帮助观众理解视频中动作控制与发力方式之间的关系。"},
        ]}, "codex"

    monkeypatch.setattr(card_generation, "_call_providers", fake_call)
    result = card_generation._card_copy(
        "The complete part mentions pairaformis and second term.",
        candidates, 1, 1, ("codex",), "", tmp_path,
    )

    assert "BEGIN COMPLETE PART TRANSCRIPT" in captured["prompt"]
    assert "The complete part mentions pairaformis and second term." in captured["prompt"]
    assert '"display": "Piriformis"' in captured["prompt"]
    assert "about 40 Chinese characters" in captured["prompt"]
    assert result == {
        "pairaformis": {"chinese_term": "梨状肌", "explanation": "髋部深层肌肉"},
        "second term": {
            "chinese_term": "第二术语",
            "explanation": "这是与髋部稳定有关的概念，可以帮助观众理解视频中动作控制与发力方式之间的关系。",
        },
    }


def test_card_pass_uses_first_cue_resolves_overlap_and_keeps_manual_style(
    tmp_path: Path, monkeypatch,
) -> None:
    database = tmp_path / "cards.db"
    init_db(database)
    projects = Repository(database)
    project = projects.create_project("https://youtu.be/example")
    cards = CardRepository(database)
    manual = cards.create(project["id"], CardPayload(
        start_ms=20_000, end_ms=25_000, chinese_term="已有",
        english_term="Manual", explanation="人工卡片",
        style={"side": "left", "width_pct": 41},
    ))
    project.update({
        "video_path": str(tmp_path / "source.mp4"), "duration_ms": 30_000,
        "translation_context": {"card_candidates": [
            {"source": "pairaformis", "display": "Piriformis", "start_token_id": 1},
            {"source": "second term", "display": "Second term", "start_token_id": 3},
        ]},
    })
    part = {"id": 1, "part_number": 1, "start_token_id": 0,
            "end_token_id": 4, "source_text": "pairaformis and second term"}
    cues = {1: [
        {"start_token_id": 0, "end_token_id": 2, "start_ms": 1000},
        {"start_token_id": 3, "end_token_id": 4, "start_ms": 3000},
    ]}
    monkeypatch.setattr(card_generation, "probe_video", lambda _path: (640, 360))
    monkeypatch.setattr(card_generation, "render_card_png", lambda *_args: (b"png", {}))
    monkeypatch.setattr(card_generation, "_card_copy", lambda *_args: {
        "pairaformis": {"chinese_term": "梨状肌", "explanation": "髋部深层的一块肌肉"},
        "second term": {"chinese_term": "第二术语", "explanation": "用于说明第二个概念"},
    })

    result = card_generation.generate_cards_for_project(
        project, [part], cues, cards, ("codex",), "", tmp_path / "audit",
    )

    assert result == {"created": 2, "skipped": []}
    generated = [card for card in cards.list(project["id"]) if card["generated_from"]]
    assert [(card["english_term"], card["start_ms"], card["end_ms"]) for card in generated] == [
        ("Piriformis", 1000, 6000), ("Second term", 6000, 11_000),
    ]
    assert all(card["style"]["side"] == "left" for card in generated)
    assert cards.get(project["id"], manual["id"]) == manual
    assert cards.default_style()["width_pct"] == 41
    assert card_generation.generate_cards_for_project(
        project, [part], cues, cards, ("codex",), "", tmp_path / "audit",
    )["created"] == 0
