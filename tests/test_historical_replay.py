"""Replay saved model output without contacting a translation CLI."""

import json
from pathlib import Path

from app.db import connect, init_db
from app.repository import Repository
from app.services.cues import materialize_aligned_cues

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_pipeline_sample.json"


def test_historical_pass_outputs_replay_into_domain_tables(tmp_path: Path) -> None:
    old = json.loads(FIXTURE.read_text(encoding="utf-8"))
    offset = old["token_range"][0]
    tokens = [
        {
            **token,
            "token_id": token["sequence"] - offset,
            "text": token["normalized_text"],
        }
        for token in old["tokens"]
    ]
    cues = [
        {
            "cue_id": row["source_sentence_id"],
            "start_token_id": row["start_token_id"] - offset,
            "end_token_id": row["end_token_id"] - offset,
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "source_text": row["source_sentence"],
            "target_text": row["chinese_sentence"],
        }
        for row in old["cues"]
    ]
    pass2 = materialize_aligned_cues(
        tokens, cues,
        {"cues": [
            {"cue_id": cue["cue_id"], "target_text": cue["target_text"]}
            for cue in cues
        ]},
    )
    assert [row["cue_id"] for row in pass2] == [3, 4, 5]

    database = tmp_path / "replay.db"
    init_db(database)
    repository = Repository(database)
    project_id = repository.create_project("https://youtu.be/replay")["id"]
    repository.replace_tokens(project_id, tokens)
    repository.initialize_subtitle_plan(project_id, [{
        "start_token_id": 0,
        "end_token_id": len(tokens) - 1,
        "start_ms": tokens[0]["start_ms"],
        "end_ms": tokens[-1]["end_ms"],
    }])
    part_id = repository.list_parts(project_id)[0]["id"]
    repository.update_part(
        part_id,
        source_text=old["pass1"]["source_text"],
        target_text=old["pass1"]["target_text"],
        status="pass2_complete",
    )
    repository.commit_pass2(project_id, part_id, cues)
    assert repository.list_segments(project_id) == []
    assert repository.list_parts(project_id)[0]["target_text"] == old["pass1"]["target_text"]

    final = [
        {
            "cue_id": row["source_sentence_id"],
            "subcue_id": row["sub_id"] or None,
            "start_token_id": row["start_token_id"] - offset,
            "end_token_id": row["end_token_id"] - offset,
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "source_text": row["source_text"],
            "target_text": row["translated_text"],
            "quality_flags": row["quality_flags"],
        }
        for row in old["segments"]
    ]
    raw_subcues = [
        {**row, "split_pass": "pass3_1", "boundary_source": "model"}
        for row in final if row["subcue_id"] is not None
    ]
    repository.replace_part_segments(project_id, part_id, final, subcues=raw_subcues)
    assert [row["target_text"] for row in repository.list_segments(project_id)] == [
        row["translated_text"] for row in old["segments"]
    ]
    assert [(row["cue_id"], row["subcue_id"]) for row in repository.list_subcues(part_id)] == [
        (4, 1), (4, 2)
    ]

    # Final display may merge both Sub-cues while retaining their pre-merge records.
    merged = {
        **final[1],
        "end_token_id": final[2]["end_token_id"],
        "end_ms": final[2]["end_ms"],
        "source_text": f"{final[1]['source_text']} {final[2]['source_text']}",
        "target_text": final[1]["target_text"] + final[2]["target_text"],
    }
    repository.replace_part_segments(project_id, part_id, [final[0], merged, final[3]])
    segments = repository.list_segments(project_id)
    assert [(item["cue_id"], item["subcue_id"]) for item in segments[1]["components"]] == [
        (4, 1), (4, 2)
    ]
    assert len(repository.list_subcues(part_id)) == 2
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM segment_components").fetchone()[0] == 4

    repository.merge_with_next(project_id, segments[0]["id"])
    combined = repository.list_segments(project_id)[0]
    assert [(item["cue_id"], item["subcue_id"]) for item in combined["components"]] == [
        (3, None), (4, 1), (4, 2)
    ]
    assert len(repository.list_subcues(part_id)) == 2
    with connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_context_punctuation_and_split_errors_have_own_storage(tmp_path: Path) -> None:
    database = tmp_path / "domain.db"
    init_db(database)
    repository = Repository(database)
    project_id = repository.create_project("https://youtu.be/domain")["id"]
    context = {"overview": "Topic", "style": "Concise", "terms": []}
    repository.update_project(
        project_id, translation_context=context,
        context_provider="codex", context_approved=True,
    )
    repository.replace_tokens(project_id, [
        {"text": "Hello", "start_ms": 0, "end_ms": 500},
        {"text": "world", "start_ms": 500, "end_ms": 1000},
    ])
    repository.initialize_subtitle_plan(project_id, [{
        "start_token_id": 0, "end_token_id": 1, "start_ms": 0, "end_ms": 1000,
    }])
    part_id = repository.list_parts(project_id)[0]["id"]
    from app.services.punctuation import source_fingerprint

    repository.begin_part_punctuation(
        project_id, part_id, source_fingerprint(repository.list_tokens(project_id)), "codex"
    )
    repository.complete_part_punctuation(part_id, [{"after_token_id": 1, "mark": "."}])
    assert [item["text"] for item in repository.list_tokens(project_id)] == ["Hello", "world."]
    repository.replace_cues(project_id, part_id, [{
        "cue_id": 1, "start_token_id": 0, "end_token_id": 1,
        "start_ms": 0, "end_ms": 1000,
        "source_text": "Hello world.", "target_text": "一条很长的中文。",
    }])
    repository.replace_part_segments(project_id, part_id, [{
        "cue_id": 1, "start_token_id": 0, "end_token_id": 1,
        "start_ms": 0, "end_ms": 1000,
        "source_text": "Hello world.", "target_text": "一条很长的中文。",
        "pass3_error": "invalid boundary",
        "quality_flags": ["pass3_invalid_output"],
    }], subcues=[])
    cue = repository.list_cues(part_id)[0]
    assert (cue["split_status"], cue["split_error"]) == ("failed", "invalid boundary")
    with connect(database) as connection:
        project_columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
        punctuation_columns = {row[1] for row in connection.execute("PRAGMA table_info(part_punctuation)")}
        assert "translation_context" not in project_columns
        assert "marks_json" not in punctuation_columns
        assert connection.execute("SELECT approved FROM translation_contexts").fetchone()[0] == 1
        assert connection.execute("SELECT token_id, mark FROM punctuation_marks").fetchone()[:] == (1, ".")


def test_selected_part_publish_keeps_premerge_subcues_and_links(tmp_path: Path) -> None:
    database = tmp_path / "selected.db"
    init_db(database)
    repository = Repository(database)
    project_id = repository.create_project("https://youtu.be/selected")["id"]
    repository.replace_tokens(project_id, [
        {"text": f"word{i}", "start_ms": i * 500, "end_ms": (i + 1) * 500}
        for i in range(4)
    ])
    repository.initialize_subtitle_plan(project_id, [{
        "start_token_id": 0, "end_token_id": 3, "start_ms": 0, "end_ms": 2000,
    }])
    part_id = repository.list_parts(project_id)[0]["id"]
    cue = {
        "cue_id": 1, "start_token_id": 0, "end_token_id": 3,
        "start_ms": 0, "end_ms": 2000,
        "source_text": "word0 word1 word2 word3", "target_text": "旧中文。",
    }
    repository.replace_cues(project_id, part_id, [cue])
    repository.replace_part_segments(project_id, part_id, [cue])
    assert repository.list_segments(project_id)[0]["target_text"] == "旧中文。"
    first = {
        **cue, "subcue_id": 1, "end_token_id": 1, "end_ms": 1000,
        "source_text": "word0 word1", "target_text": "新的前半，",
        "split_pass": "pass3_1", "boundary_source": "model",
    }
    second = {
        **cue, "subcue_id": 2, "start_token_id": 2, "start_ms": 1000,
        "source_text": "word2 word3", "target_text": "新的后半。",
        "split_pass": "pass3_1", "boundary_source": "model",
    }
    repository.commit_part_rebuild(project_id, 0, [{
        "part_id": part_id,
        "source_text": cue["source_text"],
        "target_text": "新的前半，新的后半。",
        "translation_provider": "codex",
        "cues": [{**cue, "target_text": "新的前半，新的后半。"}],
        "subcues": [first, second],
        "segments": [{**cue, "target_text": "新的前半，新的后半。"}],
    }])
    segment = repository.list_segments(project_id)[0]
    assert segment["target_text"] == "新的前半，新的后半。"
    assert [(row["cue_id"], row["subcue_id"]) for row in segment["components"]] == [
        (1, 1), (1, 2)
    ]
    assert len(repository.list_subcues(part_id)) == 2
    assert repository.get_project(project_id)["subtitle_revision"] == 1
    with connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
