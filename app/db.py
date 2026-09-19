from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    youtube_id TEXT,
    title TEXT,
    display_name TEXT,
    duration_ms INTEGER,
    source_language TEXT NOT NULL DEFAULT 'en',
    target_language TEXT NOT NULL DEFAULT 'zh-CN',
    max_height INTEGER NOT NULL DEFAULT 1080,
    bilingual INTEGER NOT NULL DEFAULT 1,
    translation_provider TEXT NOT NULL DEFAULT 'auto',
    codex_model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    pipeline_detail TEXT,
    error TEXT,
    subtitle_source TEXT,
    video_path TEXT,
    output_path TEXT,
    subtitle_style TEXT,
    global_translation_context TEXT,
    global_context_provider TEXT,
    global_context_approved INTEGER NOT NULL DEFAULT 1,
    pending_rebuild_scope TEXT,
    subtitle_revision INTEGER NOT NULL DEFAULT 0,
    rendered_revision INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subtitle_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    start_token_id INTEGER NOT NULL,
    end_token_id INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    overview TEXT NOT NULL DEFAULT '',
    style TEXT NOT NULL DEFAULT '',
    preferred_terms TEXT NOT NULL DEFAULT '[]',
    established_terms TEXT NOT NULL DEFAULT '[]',
    context_provider TEXT,
    source_text TEXT NOT NULL DEFAULT '',
    reference_translation TEXT NOT NULL DEFAULT '',
    translation_provider TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, sequence)
);

CREATE TABLE IF NOT EXISTS subtitle_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES subtitle_parts(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    start_token_id INTEGER NOT NULL,
    end_token_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    parent_batch_id INTEGER REFERENCES subtitle_batches(id) ON DELETE SET NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, sequence)
);

CREATE TABLE IF NOT EXISTS source_sentences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES subtitle_parts(id) ON DELETE CASCADE,
    source_sentence_id INTEGER NOT NULL,
    start_token_id INTEGER NOT NULL,
    end_token_id INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    source_sentence TEXT NOT NULL,
    chinese_sentence TEXT NOT NULL DEFAULT '',
    hidden_token_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(part_id, source_sentence_id)
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    source_text TEXT NOT NULL,
    translated_text TEXT NOT NULL DEFAULT '',
    start_token_id INTEGER,
    end_token_id INTEGER,
    source_sentence_id INTEGER,
    sub_id INTEGER,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    part_id INTEGER REFERENCES subtitle_parts(id) ON DELETE SET NULL,
    batch_id INTEGER REFERENCES subtitle_batches(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'machine_translated',
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, sequence)
);

CREATE TABLE IF NOT EXISTS timed_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    source TEXT NOT NULL,
    confidence REAL,
    timing_inferred INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, sequence)
);

CREATE TABLE IF NOT EXISTS subtitle_review_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    part_id INTEGER REFERENCES subtitle_parts(id) ON DELETE CASCADE,
    batch_id INTEGER REFERENCES subtitle_batches(id) ON DELETE CASCADE,
    issue_key TEXT NOT NULL,
    action TEXT NOT NULL,
    issue_types TEXT NOT NULL DEFAULT '[]',
    severity TEXT NOT NULL DEFAULT 'medium',
    reason TEXT NOT NULL,
    start_token_id INTEGER NOT NULL,
    end_token_id INTEGER NOT NULL,
    replacement_cues TEXT NOT NULL DEFAULT '[]',
    auto_applied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, issue_key)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_segments_project_sequence
ON segments(project_id, sequence);

CREATE INDEX IF NOT EXISTS idx_subtitle_parts_project_sequence
ON subtitle_parts(project_id, sequence);

CREATE INDEX IF NOT EXISTS idx_subtitle_batches_project_sequence
ON subtitle_batches(project_id, sequence);

CREATE INDEX IF NOT EXISTS idx_source_sentences_part_sequence
ON source_sentences(part_id, source_sentence_id);

CREATE INDEX IF NOT EXISTS idx_timed_tokens_project_sequence
ON timed_tokens(project_id, sequence);

CREATE INDEX IF NOT EXISTS idx_review_issues_project_range
ON subtitle_review_issues(project_id, start_token_id, end_token_id);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
ON jobs(status, created_at);
"""


def connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = database_path or settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_db(database_path: Path | None = None) -> None:
    with connect(database_path) as connection:
        connection.executescript(SCHEMA)
        _ensure_column(connection, "segments", "start_token_id", "INTEGER")
        _ensure_column(connection, "segments", "end_token_id", "INTEGER")
        _ensure_column(connection, "segments", "quality_flags", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(connection, "segments", "part_id", "INTEGER")
        _ensure_column(connection, "segments", "batch_id", "INTEGER")
        _ensure_column(connection, "segments", "source_sentence_id", "INTEGER")
        _ensure_column(connection, "segments", "sub_id", "INTEGER")
        _ensure_column(connection, "projects", "subtitle_style", "TEXT")
        _ensure_column(connection, "projects", "display_name", "TEXT")
        _ensure_column(connection, "projects", "pipeline_detail", "TEXT")
        _ensure_column(connection, "projects", "global_translation_context", "TEXT")
        _ensure_column(connection, "projects", "global_context_provider", "TEXT")
        _ensure_column(
            connection,
            "projects",
            "global_context_approved",
            "INTEGER NOT NULL DEFAULT 1",
        )
        _ensure_column(connection, "projects", "pending_rebuild_scope", "TEXT")
        _ensure_column(
            connection, "projects", "translation_provider", "TEXT NOT NULL DEFAULT 'auto'"
        )
        _ensure_column(connection, "projects", "codex_model", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "subtitle_parts", "source_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(
            connection,
            "subtitle_parts",
            "reference_translation",
            "TEXT NOT NULL DEFAULT ''",
        )
        _ensure_column(connection, "subtitle_parts", "translation_provider", "TEXT")


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def transaction(database_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
