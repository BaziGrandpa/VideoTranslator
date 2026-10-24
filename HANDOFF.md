# Video Translator Handoff

Last updated: 2026-10-16

This is a local YouTube-to-Chinese-subtitle workbench. The user communicates in Chinese.
Code, identifiers, UI labels, and documentation use English.

## Documentation map

- [README](README.md): setup, operation, overview, and model-call budget.
- [Current behavior](docs/current-behavior.md): implemented contracts and exact review rules.
- [October 15 audit](docs/audit-2026-10-15.md): unresolved defects, requirements gaps, and verification.
- `product/`: editorial references and explicitly dated historical acceptance reports.

Update the current-behavior document when a contract changes. Record unresolved differences
in the audit instead of describing a prompt requirement as a Python-enforced guarantee.

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
new v2 database. Restart the application after Python, prompt, or `.env` changes.
Static assets are served without caching; reload the page after JavaScript/CSS changes.

```powershell
.\.venv\Scripts\python.exe -m ruff check .
node --check app/static/app.js
node --check app/static/cards.js
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
`translation_contexts`, `jobs`, `app_settings`, `commentary_cards`, and
`commentary_card_render_state`. There are no Window or Batch
migration tables. `init_db` creates the schema and applies additive v2 migrations for
`projects.resume_time_ms`, `segments.extra_line_gap`, `segments.chinese_only`, and
`jobs.cancel_requested`. Manual Segment edits preserve Chinese line breaks entered with
Shift+Enter; the per-Segment Chinese-only flag skips its English render/SRT event and
allows an empty English field while Chinese remains required.

## Pipeline ownership

| Stage | Responsibility | Database output |
| --- | --- | --- |
| Token preparation | Reconstruct English words and timing | `tokens` |
| Part planning | Group Tokens into processing ranges | `parts` |
| Punctuation Pass | Add marks only when a Part lacks supported source punctuation | `part_punctuation`, `punctuation_marks` |
| Pass 0 | Build project overview, style, terminology, and commentary card candidates; await approval | `translation_contexts` |
| Pass 1 | Translate the whole Part into natural Chinese with length and punctuation guidance | `parts.source_text`, `parts.target_text` |
| Python Cue cutting | Fix English Token ranges for Pass 2 | `cues` |
| Pass 2 | Place Chinese text on each fixed Cue | `cues.target_text`; Part checkpoint |
| Pass 3-1 | Assign Token boundaries to Chinese punctuation splits | validated `subcues` |
| Pass 3-2 | Split other long Chinese Cues and assign boundaries | validated `subcues` |
| Finalization | Merge when needed, validate coverage and quality, publish screen rows | `segments`, `segment_components` |
| Card Pass | Explain approved card terms after final subtitle validation; Python anchors and schedules cards | `commentary_cards` |

The Punctuation Pass calls only the first selected provider once. Pass 0 uses one
request for up to 12,000 Tokens, or 6,000-Token chunks and one consolidation for
longer projects. Pass 1 and Pass 2 use one request per Part. Pass 3-1 and 3-2 each
use at most one request per Part when needed. A configured provider fallback can
retry the same Pass after a failed provider call. Punctuation has no fallback. Pass 3's
provider loop covers both subpasses, so fallback can repeat Pass 3-1 as well as Pass 3-2.
Card Pass adds one request per Part containing approved candidates after Pass 3 and final
subtitle validation; a card failure leaves subtitles available for review and can be retried
from the card workspace.

Pass 2 requires Chinese preservation in its prompt but does not enforce text conservation.
Pass 3 prioritizes Chinese readability; English Token ranges locate time.
No final Cue-to-Segment one-to-one relationship is required.

## Rebuild and review

A full rebuild synchronously clears subtitle state before enqueueing work. Skip Pass 0
preserves existing guidance and its approval state. A selected-Part rebuild retains stored
Segments, approved guidance, punctuation, and Cue boundaries until all selected Parts finish.
It publishes Cues, Sub-cues, Segments, and links together under a subtitle revision guard.
The UI locks subtitle editing while this runs.

Review suggestions combine current mechanical checks, stored Pass 3 failure flags, and
Token-derived music/applause boundaries. There are seven types; Reviewed does not dismiss
them. Rendering checks basic timeline validity, not whether all suggestions are resolved.
Manual merging does not restamp stored review flags, by design.
Manual Split divides a Segment at its time midpoint. The left row keeps both texts; the
new right row starts empty. Both keep the same Part and Cue/Sub-cue links, with null exact
Token ranges. Selected-Part rebuild validates source coverage through those links; audio
review also uses them. Rendering rejects the empty row until it is filled in.

Full rebuilds preserve cards, style, playback position, and old output files. Deleting a
project removes its own database rows and project/output directories; shared caches remain.

## Code map

- API and UI: `app/main.py`, `app/static/app.js`
- Schema and persistence: `app/db.py`, `app/repository.py`
- Pipeline: `app/pipeline.py`
- Prompt contracts: `app/services/prompts.py`, `app/schemas/`
- Part/Cue cutting: `app/services/parts.py`, `app/services/cues.py`
- Pass calls and final merging: `app/services/subtitle_planner.py`
- Rendering and display punctuation: `app/services/renderer.py`, `app/services/subtitle_text.py`
- Review rules: `app/services/subtitle_quality.py`, `app/main.py:list_review_issues`
- Cards: `app/cards/`, `app/static/cards.js`, `app/static/cards.css`
- Publishing workbench: `app/publication_api.py`, `app/services/publication_copy.py`,
  `app/services/publication_backgrounds.py`, `app/services/publication_cover.py`,
  `app/static/publish.*`
- Process lifecycle: `app/launcher.py`, `app/worker.py`, `scripts/stop.ps1`
