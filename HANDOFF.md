# Video Translator Handoff

Last updated: 2026-10-06

This is a local YouTube-to-Chinese-subtitle workbench. The user communicates in Chinese.
Code, identifiers, UI labels, and documentation use English. Do not commit `ideas.txt`.

## Constraints

- Translation uses authenticated local Codex and optional Grok CLIs. There are no paid API keys.
- Tests for the translation pipeline must simulate provider output; never run the real CLIs.
- SQLite is the source of truth. FFmpeg burns the final subtitles.
- The final product is readable Chinese subtitles. English Tokens primarily locate time.
- Store original Token text and timing unchanged. Manual text and timing edits affect Segments.

## Run and verify

- Start: `start-video-translator.bat`; stop: `stop-video-translator.bat`
- URL: `http://127.0.0.1:6866`
- Database: `data/video_translator_v2.db`
- Project files: `data/projects/<project-id>`
- Final video: `output/<project-id>`

The previous `data/video_translator.db` is intentionally not migrated. Start with a
new v2 database. Restart the application after JavaScript or prompt changes.

```powershell
.\.venv\Scripts\python.exe -m ruff check .
node --check app/static/app.js
.\.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_renderer_integration.py
```

The excluded renderer integration test starts real FFmpeg. Translation tests use
saved responses and simulated provider calls. `tests/test_historical_replay.py`
replays Pass 1/2/3 data extracted read-only from the old database.

## Domain language

| Concept | Identity | Table |
| --- | --- | --- |
| Token | Original English word; `token_id` is project-local and 0-based | `tokens` |
| Part | Processing range; `part_number` is project-local and 1-based | `parts` |
| Cue | Python-cut English range; `cue_id` is Part-local and 1-based | `cues` |
| Sub-cue | Optional validated Pass 3 split; `subcue_id` is Cue-local and 1-based | `subcues` |
| Segment | Final on-screen subtitle; `segment_order` is project-local and 1-based | `segments` |

`segment_components` records the ordered Cue/Sub-cue sources of each Segment.
A whole Cue uses a null `subcue_id`. One Segment may contain several Sub-cues or
whole Cues. The `subcues` table keeps validated pre-merge splits for diagnosis.
`cues.split_status` and `cues.split_error` record the outcome of Pass 3.
Segment `target_text` keeps punctuation; display/render text removes trailing
punctuation through `display_target_text`.

Other tables: `projects`, `part_punctuation`, `punctuation_marks`,
`translation_contexts`, `jobs`, `app_settings`. There are no Window or Batch
migration tables. `init_db` creates the current schema only.

## Pipeline ownership

| Stage | Responsibility | Database output |
| --- | --- | --- |
| Token preparation | Reconstruct English words and timing | `tokens` |
| Part planning | Group Tokens into processing ranges | `parts` |
| Punctuation Pass | Add marks only when a Part lacks supported source punctuation | `part_punctuation`, `punctuation_marks` |
| Pass 0 | Build project overview, style, and terminology; await approval | `translation_contexts` |
| Pass 1 | Translate the whole Part into natural Chinese with length and punctuation guidance | `parts.source_text`, `parts.target_text` |
| Python Cue cutting | Fix English Token ranges for Pass 2 | `cues` |
| Pass 2 | Place Chinese text on each fixed Cue | `cues.target_text`; Part checkpoint |
| Pass 3-1 | Assign Token boundaries to Chinese punctuation splits | validated `subcues` |
| Pass 3-2 | Split other long Chinese Cues and assign boundaries | validated `subcues` |
| Finalization | Merge when needed, validate coverage and quality, publish screen rows | `segments`, `segment_components` |

The Punctuation Pass calls only the first selected provider once. Pass 0 uses one
request for up to 12,000 Tokens, or 6,000-Token chunks and one consolidation for
longer projects. Pass 1 and Pass 2 use one request per Part. Pass 3-1 and 3-2 each
use at most one request per Part when needed. A configured provider fallback can
retry the same Pass after a failed provider call.

Pass 2 prompt behavior is intentionally unchanged apart from output field names.
Pass 3 prioritizes Chinese readability; English Token ranges locate time.
No final Cue-to-Segment one-to-one relationship is required.

## Rebuild and review

A full rebuild clears subtitle state and may preserve approved Pass 0 guidance.
A selected-Part rebuild keeps visible Segments and Pass 0 until all selected Parts
finish. It publishes Cues, Sub-cues, Segments, and links together under a subtitle
revision guard. Review issues are computed from current Segments.

## Code map

- API and UI: `app/main.py`, `app/static/app.js`
- Schema and persistence: `app/db.py`, `app/repository.py`
- Pipeline: `app/pipeline.py`
- Prompt contracts: `app/services/prompts.py`, `app/schemas/`
- Part/Cue cutting: `app/services/parts.py`, `app/services/cues.py`
- Pass calls and final merging: `app/services/subtitle_planner.py`
- Rendering and display punctuation: `app/services/renderer.py`, `app/services/subtitle_text.py`
