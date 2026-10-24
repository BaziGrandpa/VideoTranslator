from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.cards.schema import SCHEMA as CARD_SCHEMA
from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, source_url TEXT NOT NULL, youtube_id TEXT,
    title TEXT, display_name TEXT, duration_ms INTEGER,
    source_language TEXT NOT NULL DEFAULT 'en',
    target_language TEXT NOT NULL DEFAULT 'zh-CN',
    max_height INTEGER NOT NULL DEFAULT 1080,
    bilingual INTEGER NOT NULL DEFAULT 1,
    translation_provider TEXT NOT NULL DEFAULT 'auto',
    codex_model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, stage TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0, pipeline_detail TEXT, error TEXT,
    subtitle_source TEXT, video_path TEXT, output_path TEXT, subtitle_style TEXT,
    punctuation_enabled INTEGER NOT NULL DEFAULT 1,
    pending_rebuild_scope TEXT, subtitle_revision INTEGER NOT NULL DEFAULT 0,
    rendered_revision INTEGER, resume_time_ms INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    token_id INTEGER NOT NULL CHECK(token_id >= 0),
    raw_text TEXT NOT NULL, normalized_text TEXT NOT NULL,
    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, source TEXT NOT NULL,
    confidence REAL, timing_inferred INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(project_id, token_id)
);
CREATE TABLE IF NOT EXISTS parts (
    id INTEGER PRIMARY KEY, project_id TEXT NOT NULL
        REFERENCES projects(id) ON DELETE CASCADE,
    part_number INTEGER NOT NULL CHECK(part_number >= 1), start_token_id INTEGER NOT NULL,
    end_token_id INTEGER NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', source_text TEXT NOT NULL DEFAULT '',
    target_text TEXT NOT NULL DEFAULT '', translation_provider TEXT, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id, part_number)
);
CREATE TABLE IF NOT EXISTS part_punctuation (
    part_id INTEGER PRIMARY KEY REFERENCES parts(id) ON DELETE CASCADE,
    source_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
    provider TEXT, error TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS punctuation_marks (
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    token_id INTEGER NOT NULL CHECK(token_id >= 0), mark TEXT NOT NULL,
    PRIMARY KEY(part_id, token_id)
);
CREATE TABLE IF NOT EXISTS translation_contexts (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    overview TEXT NOT NULL, style TEXT NOT NULL, terms_json TEXT NOT NULL DEFAULT '[]',
    card_candidates_json TEXT NOT NULL DEFAULT '[]',
    provider TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cues (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    cue_id INTEGER NOT NULL CHECK(cue_id >= 1), start_token_id INTEGER NOT NULL,
    end_token_id INTEGER NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
    source_text TEXT NOT NULL, target_text TEXT NOT NULL DEFAULT '',
    hidden_token_ids TEXT NOT NULL DEFAULT '[]',
    split_status TEXT NOT NULL DEFAULT 'not_needed', split_error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(part_id, cue_id)
);
CREATE TABLE IF NOT EXISTS subcues (
    part_id INTEGER NOT NULL, cue_id INTEGER NOT NULL,
    subcue_id INTEGER NOT NULL CHECK(subcue_id >= 1),
    start_token_id INTEGER NOT NULL, end_token_id INTEGER NOT NULL,
    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
    source_text TEXT NOT NULL, target_text TEXT NOT NULL,
    split_pass TEXT NOT NULL, boundary_source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(part_id, cue_id, subcue_id),
    FOREIGN KEY(part_id, cue_id) REFERENCES cues(part_id, cue_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    segment_order INTEGER NOT NULL CHECK(segment_order >= 1),
    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
    start_token_id INTEGER, end_token_id INTEGER,
    part_id INTEGER REFERENCES parts(id),
    source_text TEXT NOT NULL, target_text TEXT NOT NULL,
    quality_flags TEXT NOT NULL DEFAULT '[]',
    extra_line_gap INTEGER NOT NULL DEFAULT 0,
    chinese_only INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'machine_translated',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, segment_order)
);
CREATE TABLE IF NOT EXISTS segment_components (
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    component_order INTEGER NOT NULL CHECK(component_order >= 1),
    part_id INTEGER NOT NULL, cue_id INTEGER NOT NULL,
    subcue_id INTEGER, PRIMARY KEY(segment_id, component_order),
    FOREIGN KEY(part_id, cue_id) REFERENCES cues(part_id, cue_id),
    FOREIGN KEY(part_id, cue_id, subcue_id)
        REFERENCES subcues(part_id, cue_id, subcue_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL
        REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    return_status TEXT, error TEXT, created_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_workspaces (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    copy_status TEXT NOT NULL DEFAULT 'queued', copy_error TEXT,
    source_srt TEXT NOT NULL, source_url TEXT NOT NULL,
    titles_json TEXT NOT NULL DEFAULT '[]', intros_json TEXT NOT NULL DEFAULT '[]',
    selected_title TEXT NOT NULL DEFAULT '', selected_intro TEXT NOT NULL DEFAULT '',
    thumbnail_titles_json TEXT NOT NULL DEFAULT '[]',
    thumbnail_status TEXT NOT NULL DEFAULT 'queued', thumbnail_error TEXT,
    selected_thumbnail_title TEXT NOT NULL DEFAULT '',
    provider TEXT, reference_path TEXT, reference_revision INTEGER NOT NULL DEFAULT 0,
    background_status TEXT NOT NULL DEFAULT 'idle', background_error TEXT,
    background_generation_id TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_covers (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ratio TEXT NOT NULL CHECK(ratio IN ('16x9', '4x3')),
    image_path TEXT, design_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
    exported_path TEXT, exported_revision INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, ratio)
);
CREATE TABLE IF NOT EXISTS publication_cover_styles (
    project_id TEXT NOT NULL,
    ratio TEXT NOT NULL CHECK(ratio IN ('16x9', '4x3')),
    line_count INTEGER NOT NULL CHECK(line_count BETWEEN 1 AND 4),
    style_json TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, ratio, line_count),
    FOREIGN KEY(project_id, ratio) REFERENCES publication_covers(project_id, ratio)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_parts_project_number ON parts(project_id, part_number);
CREATE INDEX IF NOT EXISTS idx_segments_project_order ON segments(project_id, segment_order);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_segment_components_cue ON segment_components(part_id, cue_id);
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
        connection.executescript(CARD_SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        try:
            project_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(projects)")
            }
            if "resume_time_ms" not in project_columns:
                connection.execute("ALTER TABLE projects ADD COLUMN resume_time_ms INTEGER")
            segment_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(segments)")
            }
            if "extra_line_gap" not in segment_columns:
                connection.execute(
                    "ALTER TABLE segments ADD COLUMN extra_line_gap INTEGER NOT NULL DEFAULT 0"
                )
            if "chinese_only" not in segment_columns:
                connection.execute(
                    "ALTER TABLE segments ADD COLUMN chinese_only INTEGER NOT NULL DEFAULT 0"
                )
            job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "cancel_requested" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
                )
            context_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(translation_contexts)")
            }
            if "card_candidates_json" not in context_columns:
                connection.execute(
                    "ALTER TABLE translation_contexts ADD COLUMN card_candidates_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            card_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(commentary_cards)")
            }
            if "generated_from" not in card_columns:
                connection.execute("ALTER TABLE commentary_cards ADD COLUMN generated_from TEXT")
            publication_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(publication_workspaces)")
            }
            publication_additions = {
                "reference_path": "TEXT",
                "reference_revision": "INTEGER NOT NULL DEFAULT 0",
                "background_status": "TEXT NOT NULL DEFAULT 'idle'",
                "background_error": "TEXT",
                "background_generation_id": "TEXT",
                "thumbnail_titles_json": "TEXT NOT NULL DEFAULT '[]'",
                "thumbnail_status": "TEXT NOT NULL DEFAULT 'queued'",
                "thumbnail_error": "TEXT",
                "selected_thumbnail_title": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in publication_additions.items():
                if name not in publication_columns:
                    connection.execute(
                        f"ALTER TABLE publication_workspaces ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_generated_card_source "
                "ON commentary_cards(project_id, generated_from)"
            )
            connection.commit()
        except Exception:
            connection.rollback()
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
