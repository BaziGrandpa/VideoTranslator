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
                "windows": [(0, 2)],
            }
        ],
        600,
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


def test_persists_resumable_part_and_batch_checkpoint(tmp_path: Path) -> None:
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
                "windows": [(0, 4), (5, 9)],
            }
        ],
        600,
    )
    part = repository.list_subtitle_parts(project["id"])[0]
    batch = repository.list_subtitle_batches(part["id"])[0]
    repository.save_batch_segments(
        project["id"],
        part["id"],
        batch["id"],
        [
            {
                "start_ms": 0,
                "end_ms": 5000,
                "start_token_id": 0,
                "end_token_id": 4,
                "source_text": "First window.",
                "translated_text": "第一批。",
            }
        ],
        "codex",
    )
    batches = repository.list_subtitle_batches(part["id"])
    assert batches[0]["status"] == "completed"
    assert batches[0]["provider"] == "codex"
    assert batches[1]["status"] == "pending"
    assert repository.list_segments(project["id"])[0]["translated_text"] == "第一批"


def test_failed_large_batch_can_be_replaced_by_two_retry_batches(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    project = repository.create_project("https://youtu.be/example")
    repository.initialize_subtitle_plan(
        project["id"],
        [
            {
                "start_token_id": 0,
                "end_token_id": 599,
                "start_ms": 0,
                "end_ms": 600_000,
                "windows": [(0, 599)],
            }
        ],
        600,
    )
    part = repository.list_subtitle_parts(project["id"])[0]
    original = repository.list_subtitle_batches(part["id"])[0]
    repository.split_subtitle_batch(original["id"], 299)
    batches = repository.list_subtitle_batches(part["id"])
    active = [batch for batch in batches if batch["status"] != "failed"]
    assert [(batch["start_token_id"], batch["end_token_id"]) for batch in active] == [
        (0, 299),
        (300, 599),
    ]


def test_part_audit_can_resegment_across_batch_boundaries(tmp_path: Path) -> None:
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
                "windows": [(0, 4), (5, 9)],
            }
        ],
        5,
    )
    part = repository.list_subtitle_parts(project["id"])[0]
    batches = repository.list_subtitle_batches(part["id"])
    for batch in batches:
        repository.save_batch_segments(
            project["id"],
            part["id"],
            batch["id"],
            [
                {
                    "start_ms": batch["start_token_id"] * 1000,
                    "end_ms": (batch["end_token_id"] + 1) * 1000,
                    "start_token_id": batch["start_token_id"],
                    "end_token_id": batch["end_token_id"],
                    "source_text": f"Batch {batch['sequence']}",
                    "translated_text": f"批次{batch['sequence']}",
                }
            ],
            "codex",
        )

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
    assert segments[1]["batch_id"] == batches[0]["id"]
    assert segments[2]["batch_id"] == batches[1]["id"]
