from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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
    punctuation_enabled INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS part_punctuation (
    part_id INTEGER PRIMARY KEY REFERENCES subtitle_parts(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    marks_json TEXT,
    provider TEXT NOT NULL,
    error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    return_status TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_segments_project_sequence
ON segments(project_id, sequence);

CREATE INDEX IF NOT EXISTS idx_subtitle_parts_project_sequence
ON subtitle_parts(project_id, sequence);

CREATE INDEX IF NOT EXISTS idx_source_sentences_part_sequence
ON source_sentences(part_id, source_sentence_id);

CREATE INDEX IF NOT EXISTS idx_timed_tokens_project_sequence
ON timed_tokens(project_id, sequence);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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
        _ensure_column(connection, "jobs", "return_status", "TEXT")
        _ensure_column(connection, "segments", "start_token_id", "INTEGER")
        _ensure_column(connection, "segments", "end_token_id", "INTEGER")
        _ensure_column(connection, "segments", "quality_flags", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(connection, "segments", "part_id", "INTEGER")
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
        _ensure_column(
            connection,
            "projects",
            "punctuation_enabled",
            "INTEGER NOT NULL DEFAULT 0",
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
        _retire_window_storage(connection)
        _seed_saved_subtitle_style(connection)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _retire_window_storage(connection: sqlite3.Connection) -> None:
    """Drop the retired Window/Batch tables and the segment column that pointed at them."""
    connection.execute("PRAGMA foreign_keys = OFF")
    if "subtitle_review_issues" in _table_names(connection):
        connection.execute("DROP TABLE subtitle_review_issues")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(segments)")}
    if "batch_id" in columns:
        connection.execute("DROP TABLE IF EXISTS segments_new")
        connection.execute(
            """
            CREATE TABLE segments_new (
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
                status TEXT NOT NULL DEFAULT 'machine_translated',
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, sequence)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO segments_new (
                id, project_id, sequence, start_ms, end_ms, source_text, translated_text,
                start_token_id, end_token_id, source_sentence_id, sub_id, quality_flags,
                part_id, status, updated_at
            )
            SELECT id, project_id, sequence, start_ms, end_ms, source_text, translated_text,
                start_token_id, end_token_id, source_sentence_id, sub_id,
                COALESCE(quality_flags, '[]'), part_id,
                COALESCE(status, 'machine_translated'), updated_at
            FROM segments
            """
        )
        connection.execute("DROP TABLE segments")
        connection.execute("ALTER TABLE segments_new RENAME TO segments")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_segments_project_sequence
            ON segments(project_id, sequence)
            """
        )
    if "subtitle_batches" in _table_names(connection):
        connection.execute("DROP TABLE subtitle_batches")
    connection.execute("PRAGMA foreign_keys = ON")


def _seed_saved_subtitle_style(connection: sqlite3.Connection) -> None:
    """Use the saved Ancient Humans style until the user saves another style."""
    existing = connection.execute(
        "SELECT 1 FROM app_settings WHERE key = 'default_subtitle_style'"
    ).fetchone()
    if existing is not None:
        return
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(projects)")}
    if not {"title", "subtitle_style", "bilingual", "updated_at"} <= columns:
        return
    row = connection.execute(
        """
        SELECT subtitle_style, bilingual
        FROM projects
        WHERE title = 'What Did Ancient Humans Actually Do All Day?'
          AND subtitle_style IS NOT NULL
          AND subtitle_style != ''
        ORDER BY updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return
    timestamp = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("default_subtitle_style", row["subtitle_style"], timestamp),
    )
    connection.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("default_bilingual", "1" if row["bilingual"] else "0", timestamp),
    )


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        try:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            # The API and worker may both run this migration at startup.
            current = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if column not in current:
                raise


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
