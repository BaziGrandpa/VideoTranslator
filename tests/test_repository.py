import sqlite3
from pathlib import Path

from app.db import init_db
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
            "translated_text": "Translated one.",
        },
        {
            "start_ms": 1100,
            "end_ms": 2100,
            "source_text": "Two.",
            "translated_text": "Translated two.",
        },
        {
            "start_ms": 2200,
            "end_ms": 3200,
            "source_text": "Three.",
            "translated_text": "Translated three.",
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


def test_segment_edit_split_and_merge(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_segments(project["id"], sample_segments())
    segments = repository.list_segments(project["id"])

    updated = repository.update_segment(
        project["id"], segments[0]["id"], translated_text="Edited first.", status="edited"
    )
    assert updated["translated_text"] == "Edited first."

    repository.split_segment(project["id"], segments[0]["id"], 500)
    split = repository.list_segments(project["id"])
    assert len(split) == 4
    assert [item["sequence"] for item in split] == [0, 1, 2, 3]
    assert split[0]["end_ms"] == 500
    assert split[1]["start_ms"] == 500

    repository.merge_with_next(project["id"], split[0]["id"])
    merged = repository.list_segments(project["id"])
    assert len(merged) == 3
    assert [item["sequence"] for item in merged] == [0, 1, 2]
    assert merged[0]["end_ms"] == 1000


def test_chinese_terminal_punctuation_survives_segment_storage_and_edit(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_segments(
        project["id"],
        [{
            "start_ms": 0, "end_ms": 1000,
            "source_text": "Look at you.", "translated_text": "看你。",
        }],
    )

    segment = repository.list_segments(project["id"])[0]
    assert segment["translated_text"] == "看你。"
    edited = repository.update_segment(
        project["id"], segment["id"], translated_text="看你，", status="edited"
    )
    assert edited["translated_text"] == "看你，"


def test_manual_merge_keeps_first_source_id_and_extends_token_range(tmp_path: Path) -> None:
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
                "source_sentence_id": index + 6,
                "source_text": f"Cue {index + 6}",
                "translated_text": chinese,
            }
            for index, chinese in enumerate(["前文", "看你。", "后文"])
        ],
    )

    first = repository.list_segments(project["id"])[0]
    repository.merge_with_next(project["id"], first["id"])

    merged = repository.list_segments(project["id"])
    assert [row["source_sentence_id"] for row in merged] == [6, 8]
    assert [row["sequence"] for row in merged] == [0, 1]
    assert (merged[0]["start_token_id"], merged[0]["end_token_id"]) == (0, 3)
    assert merged[0]["translated_text"] == "前文看你。"


def test_persists_timed_tokens_and_segment_token_ranges(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.replace_timed_tokens(
        project["id"],
        [
            {"text": "Hello", "start_ms": 100, "end_ms": 400, "source": "test"},
            {"text": "world.", "start_ms": 400, "end_ms": 900, "source": "test"},
        ],
    )
    repository.replace_segments(
        project["id"],
        [
            {
                "start_ms": 100,
                "end_ms": 900,
                "start_token_id": 0,
                "end_token_id": 1,
                "source_sentence_id": 1,
                "sub_id": 1,
                "source_text": "Hello world.",
                "translated_text": "Translated greeting",
                "quality_flags": ["very_short_duration"],
            }
        ],
    )

    tokens = repository.list_timed_tokens(project["id"])
    segment = repository.list_segments(project["id"])[0]
    assert [token["text"] for token in tokens] == ["Hello", "world."]
    assert segment["start_token_id"] == 0
    assert segment["end_token_id"] == 1
    assert segment["source_sentence_id"] == 1
    assert segment["sub_id"] == 1
    assert segment["quality_flags"] == ["very_short_duration"]


def test_persists_source_sentence_map(tmp_path: Path) -> None:
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
    part = repository.list_subtitle_parts(project["id"])[0]
    repository.replace_source_sentences(
        project["id"],
        part["id"],
        [
            {
                "source_sentence_id": 1,
                "start_token_id": 0,
                "end_token_id": 2,
                "start_ms": 100,
                "end_ms": 900,
                "source_sentence": "Hello there.",
                "chinese_sentence": "你好。",
                "hidden_token_ids": [0],
            }
        ],
    )

    sentences = repository.list_source_sentences(part["id"])
    assert sentences[0]["source_sentence_id"] == 1
    assert sentences[0]["chinese_sentence"] == "你好。"
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
    part = repository.list_subtitle_parts(project["id"])[0]
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
                "translated_text": "第一",
            },
            {
                "start_ms": 3000,
                "end_ms": 7000,
                "start_token_id": 3,
                "end_token_id": 6,
                "source_text": "Cross boundary",
                "translated_text": "跨窗口",
            },
            {
                "start_ms": 7000,
                "end_ms": 10_000,
                "start_token_id": 7,
                "end_token_id": 9,
                "source_text": "Last",
                "translated_text": "最后",
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
    assert [segment["translated_text"] for segment in segments] == ["第一", "跨窗口", "最后"]


def test_init_db_drops_retired_window_tables(tmp_path: Path) -> None:
    database = tmp_path / "old.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY);
        CREATE TABLE subtitle_parts (
            id INTEGER PRIMARY KEY,
            project_id TEXT,
            sequence INTEGER
        );
        CREATE TABLE subtitle_batches (
            id INTEGER PRIMARY KEY,
            project_id TEXT,
            part_id INTEGER,
            sequence INTEGER
        );
        CREATE TABLE subtitle_review_issues (id INTEGER PRIMARY KEY, project_id TEXT);
        CREATE TABLE segments (
            id INTEGER PRIMARY KEY,
            project_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            source_text TEXT NOT NULL,
            translated_text TEXT NOT NULL DEFAULT '',
            batch_id INTEGER,
            status TEXT NOT NULL DEFAULT 'machine_translated',
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, sequence)
        );
        INSERT INTO segments (
            project_id, sequence, start_ms, end_ms, source_text, translated_text,
            batch_id, status, updated_at
        ) VALUES ('p', 0, 0, 1000, 'Hello', '你好', 1, 'machine_translated', 't');
        """
    )
    connection.close()

    init_db(database)

    connection = sqlite3.connect(database)
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    columns = {row[1] for row in connection.execute("PRAGMA table_info(segments)")}
    row = connection.execute(
        "SELECT source_text, translated_text FROM segments"
    ).fetchone()
    connection.close()
    assert "subtitle_batches" not in tables
    assert "subtitle_review_issues" not in tables
    assert "batch_id" not in columns
    assert row == ("Hello", "你好")
