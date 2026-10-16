import sqlite3
from pathlib import Path

import pytest

from app.db import SCHEMA, init_db
from app.repository import Repository


def make_repository(tmp_path: Path) -> Repository:
    database = tmp_path / "test.db"
    init_db(database)
    return Repository(database)


def sample_segments() -> list[dict]:
    return [
        {
            "start_ms": 0,
            "end_ms": 1000,
            "source_text": "One.",
            "target_text": "Translated one.",
        },
        {
            "start_ms": 1100,
            "end_ms": 2100,
            "source_text": "Two.",
            "target_text": "Translated two.",
        },
        {
            "start_ms": 2200,
            "end_ms": 3200,
            "source_text": "Three.",
            "target_text": "Translated three.",
        },
    ]


def test_project_and_job_lifecycle(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    job = repository.enqueue_job(project["id"], "prepare")
    duplicate = repository.enqueue_job(project["id"], "prepare")
    assert duplicate["id"] == job["id"]

    claimed = repository.claim_next_job()
    assert claimed is not None
    assert claimed["status"] == "running"
    repository.complete_job(claimed["id"])
    assert repository.get_job(job["id"])["status"] == "completed"


def test_segment_edit_and_merge(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_segments(project["id"], sample_segments())
    segments = repository.list_segments(project["id"])

    updated = repository.update_segment(
        project["id"], segments[0]["id"], target_text="Edited first.",
        extra_line_gap=True, status="edited"
    )
    assert updated["target_text"] == "Edited first."
    assert updated["extra_line_gap"] is True

    repository.merge_with_next(project["id"], segments[0]["id"])
    merged = repository.list_segments(project["id"])
    assert len(merged) == 2
    assert [item["segment_order"] for item in merged] == [1, 2]
    assert merged[0]["end_ms"] == 2100
    assert merged[0]["extra_line_gap"] is True


def test_manual_split_keeps_original_text_and_creates_blank_same_part_segment(
    tmp_path: Path,
) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.replace_tokens(project_id, [
        {"text": "Hello", "start_ms": 44_000, "end_ms": 46_000},
        {"text": "world.", "start_ms": 46_000, "end_ms": 48_000},
    ])
    repository.initialize_subtitle_plan(project_id, [{
        "start_token_id": 0, "end_token_id": 1, "start_ms": 44_000, "end_ms": 48_000,
    }])
    part_id = repository.list_parts(project_id)[0]["id"]
    repository.replace_cues(project_id, part_id, [{
        "cue_id": 1, "start_token_id": 0, "end_token_id": 1,
        "start_ms": 44_000, "end_ms": 48_000,
        "source_text": "Hello world.", "target_text": "你好世界。",
    }])
    repository.replace_part_segments(project_id, part_id, [{
        "start_ms": 44_000, "end_ms": 48_000,
        "start_token_id": 0, "end_token_id": 1, "cue_id": 1,
        "source_text": "Hello world.", "target_text": "你好世界。",
    }])
    original = repository.list_segments(project_id)[0]
    repository.update_segment(project_id, original["id"], extra_line_gap=True, chinese_only=True)
    original = repository.list_segments(project_id)[0]
    revision = repository.get_project(project_id)["subtitle_revision"]

    new_id = repository.split_segment(project_id, original["id"])

    left, right = repository.list_segments(project_id)
    assert [item["segment_order"] for item in (left, right)] == [1, 2]
    assert (left["start_ms"], left["end_ms"]) == (44_000, 46_000)
    assert (right["start_ms"], right["end_ms"]) == (46_000, 48_000)
    assert (left["source_text"], left["target_text"]) == ("Hello world.", "你好世界。")
    assert (right["source_text"], right["target_text"]) == ("", "")
    assert (left["id"], right["id"]) == (original["id"], new_id)
    assert left["part_id"] == right["part_id"] == part_id
    assert left["components"] == right["components"] == original["components"]
    assert repository.segment_component_token_ranges(project_id)[new_id] == [(0, 1)]
    assert left["start_token_id"] is left["end_token_id"] is None
    assert right["start_token_id"] is right["end_token_id"] is None
    assert left["extra_line_gap"] is right["extra_line_gap"] is True
    assert left["chinese_only"] is right["chinese_only"] is True
    assert repository.get_project(project_id)["subtitle_revision"] == revision + 1

    repository.update_segment(project_id, right["id"], source_text="world.", target_text="世界。")
    assert repository.list_segments(project_id)[1]["target_text"] == "世界。"
    repository.update_segment_boundary(project_id, right["id"], "start", value_ms=46_500)
    assert [(row["start_ms"], row["end_ms"]) for row in repository.list_segments(project_id)] == [
        (44_000, 46_500), (46_500, 48_000),
    ]

    repository.merge_with_next(project_id, left["id"])
    restored = repository.list_segments(project_id)
    assert len(restored) == 1
    assert (restored[0]["start_ms"], restored[0]["end_ms"]) == (44_000, 48_000)


def test_manual_split_rejects_missing_provenance_and_short_durations(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_segments(project["id"], sample_segments())
    first = repository.list_segments(project["id"])[0]
    with pytest.raises(ValueError, match="single Part and Cue provenance"):
        repository.split_segment(project["id"], first["id"])
    assert len(repository.list_segments(project["id"])) == 3

    repository.initialize_subtitle_plan(project["id"], [{
        "start_token_id": 0, "end_token_id": 0, "start_ms": 0, "end_ms": 150,
    }])
    part_id = repository.list_parts(project["id"])[0]["id"]
    repository.replace_cues(project["id"], part_id, [{
        "cue_id": 1, "start_token_id": 0, "end_token_id": 0,
        "start_ms": 0, "end_ms": 150, "source_text": "Hi", "target_text": "嗨",
    }])
    repository.replace_part_segments(project["id"], part_id, [{
        "start_ms": 0, "end_ms": 150, "start_token_id": 0, "end_token_id": 0,
        "cue_id": 1, "source_text": "Hi", "target_text": "嗨",
    }])
    short = next(row for row in repository.list_segments(project["id"]) if row["part_id"] == part_id)
    with pytest.raises(ValueError, match="200 ms"):
        repository.split_segment(project["id"], short["id"])


def test_manual_split_accepts_a_segment_merged_from_two_cues(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    project_id = project["id"]
    repository.replace_tokens(project_id, [
        {"text": "Hello", "start_ms": 0, "end_ms": 1000},
        {"text": "world", "start_ms": 1000, "end_ms": 2000},
    ])
    repository.initialize_subtitle_plan(project_id, [{
        "start_token_id": 0, "end_token_id": 1, "start_ms": 0, "end_ms": 2000,
    }])
    part_id = repository.list_parts(project_id)[0]["id"]
    repository.replace_cues(project_id, part_id, [
        {"cue_id": 1, "start_token_id": 0, "end_token_id": 0,
         "start_ms": 0, "end_ms": 1000, "source_text": "Hello", "target_text": "你好"},
        {"cue_id": 2, "start_token_id": 1, "end_token_id": 1,
         "start_ms": 1000, "end_ms": 2000, "source_text": "world", "target_text": "世界"},
    ])
    repository.replace_part_segments(project_id, part_id, [
        {"cue_id": 1, "start_token_id": 0, "end_token_id": 0,
         "start_ms": 0, "end_ms": 1000, "source_text": "Hello", "target_text": "你好"},
        {"cue_id": 2, "start_token_id": 1, "end_token_id": 1,
         "start_ms": 1000, "end_ms": 2000, "source_text": "world", "target_text": "世界"},
    ])
    first = repository.list_segments(project_id)[0]
    repository.merge_with_next(project_id, first["id"])
    merged = repository.list_segments(project_id)[0]
    assert [component["cue_id"] for component in merged["components"]] == [1, 2]

    repository.split_segment(project_id, merged["id"])

    left, right = repository.list_segments(project_id)
    assert left["components"] == right["components"] == merged["components"]
    assert left["part_id"] == right["part_id"] == part_id
    assert (left["start_ms"], left["end_ms"]) == (0, 1000)
    assert (right["start_ms"], right["end_ms"]) == (1000, 2000)
    assert right["source_text"] == right["target_text"] == ""
    assert repository.segment_component_token_ranges(project_id)[right["id"]] == [
        (0, 0), (1, 1),
    ]


def test_init_db_adds_extra_line_gap_to_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    legacy_schema = SCHEMA.replace("    extra_line_gap INTEGER NOT NULL DEFAULT 0,\n", "")
    legacy_schema = legacy_schema.replace("    chinese_only INTEGER NOT NULL DEFAULT 0,\n", "")
    legacy_schema = legacy_schema.replace(
        "    rendered_revision INTEGER, resume_time_ms INTEGER,\n",
        "    rendered_revision INTEGER,\n",
    )
    with sqlite3.connect(database) as connection:
        connection.executescript(legacy_schema)
    init_db(database)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(segments)")}
        project_columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    assert "extra_line_gap" in columns
    assert "chinese_only" in columns
    assert "resume_time_ms" in project_columns


def test_manual_target_breaks_and_chinese_only_persist(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_segments(project["id"], sample_segments())
    segment = repository.list_segments(project["id"])[0]
    assert segment["chinese_only"] is False

    updated = repository.update_segment(
        project["id"], segment["id"], source_text="", target_text="第一行。\r\n第二行。",
        chinese_only=True,
    )
    assert updated["source_text"] == ""
    assert updated["target_text"] == "第一行。\n第二行。"
    assert updated["chinese_only"] is True
    assert repository.list_segments(project["id"])[0]["chinese_only"] is True


def test_merge_rejects_mixed_chinese_only_modes(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_segments(project["id"], sample_segments())
    first = repository.list_segments(project["id"])[0]
    repository.update_segment(project["id"], first["id"], chinese_only=True)
    with pytest.raises(ValueError, match="different English display settings"):
        repository.merge_with_next(project["id"], first["id"])
    assert len(repository.list_segments(project["id"])) == 3


def test_chinese_terminal_punctuation_survives_segment_storage_and_edit(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_segments(
        project["id"],
        [{
            "start_ms": 0, "end_ms": 1000,
            "source_text": "Look at you.", "target_text": "看你。",
        }],
    )

    segment = repository.list_segments(project["id"])[0]
    assert segment["target_text"] == "看你。"
    edited = repository.update_segment(
        project["id"], segment["id"], target_text="看你，", status="edited"
    )
    assert edited["target_text"] == "看你，"


def test_manual_merge_extends_token_range(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_segments(
        project["id"],
        [
            {
                "start_ms": index * 1000,
                "end_ms": (index + 1) * 1000,
                "start_token_id": index * 2,
                "end_token_id": index * 2 + 1,
                "source_text": f"Cue {index + 6}",
                "target_text": chinese,
            }
            for index, chinese in enumerate(["前文", "看你。", "后文"])
        ],
    )

    first = repository.list_segments(project["id"])[0]
    repository.merge_with_next(project["id"], first["id"])

    merged = repository.list_segments(project["id"])
    assert [row["segment_order"] for row in merged] == [1, 2]
    assert (merged[0]["start_token_id"], merged[0]["end_token_id"]) == (0, 3)
    assert merged[0]["target_text"] == "前文看你。"


def test_manual_merge_rejects_cross_part_without_changing_segments(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.initialize_subtitle_plan(project["id"], [
        {"start_token_id": 0, "end_token_id": 0, "start_ms": 0, "end_ms": 1000},
        {"start_token_id": 1, "end_token_id": 1, "start_ms": 1000, "end_ms": 2000},
    ])
    for part in repository.list_parts(project["id"]):
        repository.replace_part_segments(project["id"], part["id"], [{
            "start_ms": part["start_ms"],
            "end_ms": part["end_ms"],
            "start_token_id": part["start_token_id"],
            "end_token_id": part["end_token_id"],
            "source_text": f"Part {part['part_number']}",
            "target_text": f"第{part['part_number']}段",
        }])
    before = repository.list_segments(project["id"])

    with pytest.raises(ValueError, match="different Parts"):
        repository.merge_with_next(project["id"], before[0]["id"])

    after = repository.list_segments(project["id"])
    assert [(row["id"], row["segment_order"]) for row in after] == [
        (row["id"], row["segment_order"]) for row in before
    ]


def test_persists_tokens_and_segment_token_ranges(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_tokens(
        project["id"],
        [
            {"text": "Hello", "start_ms": 100, "end_ms": 400, "source": "test"},
            {"text": "world.", "start_ms": 400, "end_ms": 900, "source": "test"},
        ],
    )
    repository.initialize_subtitle_plan(
        project["id"],
        [{"start_token_id": 0, "end_token_id": 1, "start_ms": 100, "end_ms": 900}],
    )
    part_id = repository.list_parts(project["id"])[0]["id"]
    repository.replace_cues(project["id"], part_id, [{
        "cue_id": 1, "start_token_id": 0, "end_token_id": 1,
        "start_ms": 100, "end_ms": 900, "source_text": "Hello world.",
        "target_text": "Translated greeting",
    }])
    repository.replace_part_segments(
        project["id"],
        part_id,
        [
            {
                "start_ms": 100,
                "end_ms": 900,
                "start_token_id": 0,
                "end_token_id": 1,
                "cue_id": 1,
                "source_text": "Hello world.",
                "target_text": "Translated greeting",
                "quality_flags": ["very_short_duration"],
            }
        ],
    )

    tokens = repository.list_tokens(project["id"])
    segment = repository.list_segments(project["id"])[0]
    assert [token["text"] for token in tokens] == ["Hello", "world."]
    assert segment["start_token_id"] == 0
    assert segment["end_token_id"] == 1
    assert [(item["cue_id"], item["subcue_id"]) for item in segment["components"]] == [(1, None)]
    assert segment["quality_flags"] == ["very_short_duration"]


def test_persists_source_text_map(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.initialize_subtitle_plan(
        project["id"],
        [
            {
                "start_token_id": 0,
                "end_token_id": 2,
                "start_ms": 0,
                "end_ms": 900,
            }
        ],
    )
    part = repository.list_parts(project["id"])[0]
    repository.replace_cues(
        project["id"],
        part["id"],
        [
            {
                "cue_id": 1,
                "start_token_id": 0,
                "end_token_id": 2,
                "start_ms": 100,
                "end_ms": 900,
                "source_text": "Hello there.",
                "target_text": "你好。",
                "hidden_token_ids": [0],
            }
        ],
    )

    sentences = repository.list_cues(part["id"])
    assert sentences[0]["cue_id"] == 1
    assert sentences[0]["target_text"] == "你好。"
    assert sentences[0]["hidden_token_ids"] == [0]


def test_replace_part_segments_keeps_token_ranges(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.initialize_subtitle_plan(
        project["id"],
        [
            {
                "start_token_id": 0,
                "end_token_id": 9,
                "start_ms": 0,
                "end_ms": 10_000,
            }
        ],
    )
    part = repository.list_parts(project["id"])[0]
    repository.replace_part_segments(
        project["id"],
        part["id"],
        [
            {
                "start_ms": 0,
                "end_ms": 3000,
                "start_token_id": 0,
                "end_token_id": 2,
                "source_text": "First",
                "target_text": "第一",
            },
            {
                "start_ms": 3000,
                "end_ms": 7000,
                "start_token_id": 3,
                "end_token_id": 6,
                "source_text": "Cross boundary",
                "target_text": "跨窗口",
            },
            {
                "start_ms": 7000,
                "end_ms": 10_000,
                "start_token_id": 7,
                "end_token_id": 9,
                "source_text": "Last",
                "target_text": "最后",
            },
        ],
    )

    segments = repository.list_segments(project["id"])
    assert [(segment["start_token_id"], segment["end_token_id"]) for segment in segments] == [
        (0, 2),
        (3, 6),
        (7, 9),
    ]
    assert "batch_id" not in segments[0]
    assert [segment["target_text"] for segment in segments] == ["第一", "跨窗口", "最后"]


def test_init_db_uses_only_current_domain_tables(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    init_db(database)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "projects", "tokens", "parts", "part_punctuation", "punctuation_marks",
            "translation_contexts", "cues", "subcues", "segments",
            "segment_components", "jobs", "app_settings",
        } <= tables
        assert "timed_tokens" not in tables
        assert "subtitle_parts" not in tables
        assert "source_sentences" not in tables
        segment_columns = {row[1] for row in connection.execute("PRAGMA table_info(segments)")}
        assert "segment_order" in segment_columns
        assert "sequence" not in segment_columns
