# Video Translator Handoff

Last updated: 2026-09-23

This is a local YouTube-to-bilingual-subtitle workbench. The user communicates in Chinese.
Code, filenames, identifiers, UI labels, and documentation stay in English.

## Constraints

- Do not use paid APIs or API keys. Translation uses the locally authenticated Codex CLI,
  with the Grok CLI as the configured fallback.
- Do not silently switch providers during a targeted acceptance test.
- SQLite is the source of truth. FFmpeg burns the final subtitles. The browser is for timing;
  exact visual review uses an FFmpeg PNG.
- Final subtitles are bilingual by default, Chinese above English, with project-specific styling.
- Timed Tokens stay immutable. Edits change Sub-cues, not raw tokens.
- `Save style` stores that project's style and becomes the default for the next new project.
  `.env` subtitle values apply only before any style has been saved.
- Do not commit `ideas.txt`.

## Run

- Start: `start-video-translator.bat`
- Stop: `stop-video-translator.bat`
- URL: `http://127.0.0.1:6866`
- Database: `data/video_translator.db`
- Project files: `data/projects/<project-id>`
- Final video: `output/<project-id>`

Restart the app after JavaScript or prompt changes. Tests must not call the real CLIs.

```powershell
.\.venv\Scripts\python.exe -m ruff check .
node --check app/static/app.js
.\.venv\Scripts\python.exe -m pytest -q
```

## Names

| Word | Meaning | Stored as |
| --- | --- | --- |
| Token | One immutable English word with time | `timed_tokens.sequence`; prompts say `token_id` |
| Part | About 30 minutes. One model call per pass | `subtitle_parts` |
| Cue | Python-cut English range, numbered from 1 inside a Part | `source_sentence_id` |
| Sub-cue | One on-screen subtitle row. `sub_id` 0 if unsplit, else 1, 2, 3 | table `segments`; workbench key `segments.id` |

Prompt version: `sentence-pipeline-v8.4` in `app/services/subtitle_quality.py`.
JSON fields stay `source_sentence_id` and `chinese_sentence`. Bump the version only when
prompt wording changes.

Window and Batch are retired. They are not model-request slices and not rebuild selections.
`init_db` drops `subtitle_batches`, `subtitle_review_issues`, and `segments.batch_id`.
A queued job whose kind is still `partial_rebuild` fails and asks for a Part rebuild.

## Pipeline

1. Download the video and English captions, or use WhisperX when captions are missing.
2. Rebuild Timed Tokens. This layer is never edited.
3. Split the video into Parts of about 30 minutes (20–40). A video of 40 minutes or less is one Part.
4. Pass 0 writes the project overview, style, and terminology, then waits for approval.
   Videos up to 12,000 tokens use one call. Longer videos use 6,000-token chunks plus one
   consolidation call. That chunking is not stored as Parts.
5. Pass 1 translates one whole Part into continuous Chinese.
6. Python cuts that Part into Cues from punctuation, speaker markers, pauses of at least
   1,200 ms, and a 40-word cap.
7. Pass 2 places the Pass 1 Chinese onto every Cue in the Part. One call. It does not
   retranslate from English. Concatenated Cue Chinese should recover Pass 1.
8. Pass 3 runs only for Cues whose Chinese exceeds 24 display units. Punctuation splits go to
   Pass 3-1, which only fills `end_token_id`. Other long Cues go to Pass 3-2. Each kind is at
   most one call per Part. Prefer 18 display units when already splitting.
9. After Pass 3, Python stamps mechanical review flags once.

## Rebuild

- Full rebuild clears Pass 0 (unless Skip Pass 0 is checked), tokens, Parts, and cues, then
  runs the pipeline again. The downloaded video and source captions remain.
- Selected Parts keeps the approved Pass 0 guidance and the current subtitles. Each selected
  Part reruns Pass 1, Pass 2, and Pass 3 for that entire Part. Other Parts stay unchanged.
  Results publish together. If the subtitle revision changed during the run, publication is
  rejected.

## Review

A Sub-cue is listed when its Chinese exceeds 24 display units, its duration is under 700 ms
(except a tiny reply), its duration exceeds 5,000 ms, its reading speed exceeds 12 units per
second, or Pass 3 could not split it. The list is computed from the current rows. There is no
severity and no Apply action.

## Code map

- API: `app/main.py`
- Schema and migration: `app/db.py`
- Persistence: `app/repository.py`
- Pipeline: `app/pipeline.py`
- Worker: `app/worker.py`
- Prompts: `app/services/prompts.py`
- Cue cutting: `app/services/source_sentences.py`
- Pass calls: `app/services/subtitle_planner.py`
- Quality flags: `app/services/subtitle_quality.py`
- Browser: `app/static/index.html`, `app/static/app.js`, `app/static/styles.css`
