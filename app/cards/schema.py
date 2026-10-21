SCHEMA = """
CREATE TABLE IF NOT EXISTS commentary_cards (
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
    chinese_term TEXT NOT NULL,
    english_term TEXT NOT NULL DEFAULT '',
    explanation TEXT NOT NULL,
    generated_from TEXT,
    style_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commentary_cards_project_time
    ON commentary_cards(project_id, start_ms, end_ms);
CREATE TABLE IF NOT EXISTS commentary_card_render_state (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0,
    rendered_revision INTEGER NOT NULL DEFAULT 0
);
"""
