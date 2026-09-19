# Video Translator Handoff

Last updated: 2026-09-19

## Read This First

This is a local YouTube-to-Bilibili subtitle translation workbench. The user communicates in
Chinese, but all code, filenames, identifiers, UI labels, and documentation must remain in
English.

Non-negotiable product constraints:

- Do not use paid APIs or API keys.
- Translation uses the user's locally authenticated subscription CLIs.
- Prefer Codex CLI, with Grok CLI as the configured fallback.
- Do not silently switch providers during a targeted acceptance test.
- Use FFmpeg for final subtitle burn-in; DaVinci Resolve and Jianying are not part of the stack.
- SQLite is the source of truth.
- Browser rendering is for timing review. Exact visual review uses an FFmpeg-generated PNG.
- Final subtitles are bilingual, Chinese above English, with project-specific styling.
- Avoid full-video or full-Part LLM validation when a single problematic Window is sufficient.
- Preserve the raw Timed Token layer. User edits affect Segment/Cue data, not raw tokens.

The workspace is a Git repository whose `main` branch tracks
`https://github.com/BaziGrandpa/VideoTranslator.git`.

## Product Goal

The main workflow is:

1. User submits a YouTube URL.
2. The pipeline downloads video and captions, or uses ASR when captions are unavailable.
3. Raw caption timing is reconstructed into Timed Tokens and stored in SQLite.
4. LLM passes produce coherent Chinese translation and subtitle Cues.
5. The browser workbench lets the user review translation and timing against video playback.
6. FFmpeg burns bilingual subtitles and a bottom mask into a versioned MP4 under the project's
   output directory.
7. The project output-folder button opens that project's folder directly.

The highest-priority quality objective is Chinese readability. English Cue boundaries may be
grammatically incomplete when that produces better Chinese subtitles.

## Running the Application

- Start: `start-video-translator.bat`
- Stop: `stop-video-translator.bat`
- URL: `http://127.0.0.1:6866`
- Database: `data/video_translator.db`
- Project data: `data/projects/<project-id>`
- Final video output: `output/<project-id>`

The start script is expected to stop an existing server on port 6866, launch the worker/server,
open the browser automatically, and keep normal successful HTTP polling quiet. The stop script
uses the port/process ownership relationship rather than killing arbitrary Python processes.

## Current Architecture

### Backend

- FastAPI: `app/main.py`
- SQLite schema and migration: `app/db.py`
- Persistence: `app/repository.py`
- Pipeline orchestration: `app/pipeline.py`
- Worker: `app/worker.py`
- Local CLI integration: `app/services/translator.py`
- Live Pass 0–3 prompts: `app/services/prompts.py`
- Subtitle planning: `app/services/subtitle_planner.py`
- Subtitle mechanical quality rules: `app/services/subtitle_quality.py`
- FFmpeg final rendering: `app/services/renderer.py`
- FFmpeg exact-frame preview: `app/services/exact_preview.py`

### Frontend

- Markup: `app/static/index.html`
- Logic: `app/static/app.js`
- Styles: `app/static/styles.css`

The frontend is deliberately plain HTML/CSS/JavaScript. There is no frontend build step.

## Shared Terminology

- **Timed Token**: approximately one English word/token with `token_id`, `start_ms`, and `end_ms`.
- **Cue / Segment / Sequence**: one displayed subtitle unit after segmentation. Use "Cue" in product
  discussions when possible.
- **Window**: a ~1,000-token rebuild-selection envelope. Pass 2 is one LLM call per Part, not per
  Window. Selected rebuild still uses Window rows in the UI, then expands them to intersecting
  English cues.
- **Part**: approximately 30 minutes of a long video. Parts are independently translated.
- **Pass**: a major LLM stage, not an internal retry.
- **Raw layer**: Timed Tokens. Must remain recoverable and unmodified by subtitle editing.
- **Editing layer**: Cues/Segments with source, translation, timing, quality flags, and status.

## Current Translation Pipeline

### Pass 0: Global Project Context

Purpose:

- Create project-wide overview/style guidance.
- Create globally consistent terminology. Later Parts follow these items; there is no extra
  hardcoded person-name policy.
- Prevent terminology drift across Parts, such as inconsistent translations of "doomscrolling".

For videos up to 12,000 Timed Tokens, Pass 0 is direct. Longer videos use chunking and
consolidation.

### Pass 0 Human Review Checkpoint

Pass 0 now pauses the pipeline before Pass 1. The project enters
`waiting_context_review` at 68% and the browser displays a project-wide Translation guidance
panel. The user can edit:

- Project overview.
- Translation style instructions.
- Terminology source/preferred-target pairs.
- Each term's category and mandatory-use flag.
- Whole-video occurrence counts for generated terms.

Pass 0 returns at most 40 final terminology entries with whole-video occurrence counts of at least
2, ranked for cross-video consistency value
rather than raw frequency. Repetition across separated sections, translation ambiguity, and topic
centrality are the main signals. Function words, generic high-frequency vocabulary, and incidental
one-off details are excluded. Videos up to 12,000 Timed Tokens nominate up to 60 semantically
filtered candidates; Python counts exact whole-video phrase occurrences and selects up to 40 of the
strongest recurring terms. Long videos nominate up to 60 candidates per chunk, then consolidate
them using exact whole-video phrase counts and the number of chunks that nominated each term.

`Save draft` persists edits without resuming. `Save and continue translation` approves the
current context and queues Pass 1. All later passes consume the approved SQLite value. New
projects and full subtitle Rebuilds require this checkpoint; existing projects were migrated as
already approved so they do not become unexpectedly blocked. The reviewed version is written to
`artifacts/global-translation-context-reviewed.json` when translation resumes.

Relevant locations:

- Pipeline checkpoint: `app/pipeline.py`
- Migration/persistence: `app/db.py`, `app/repository.py`
- Edit/approve API: `app/main.py`
- Panel: `app/static/index.html`, `app/static/app.js`, `app/static/styles.css`

### Pass 1: Coherent Part Translation

Input:

- Plain reconstructed English for an entire Part.
- No token IDs, timestamps, SPAN labels, or other alignment metadata.
- Pass 0 global context.

Output:

- One coherent Chinese reference translation for the full Part.

This pass exists to separate translation quality from subtitle segmentation. The user explicitly
wants a real "whole English text -> whole Chinese text" stage.

### Current v7 Python Source-Sentence Layer

After Pass 1, Python divides each Part's immutable Timed Tokens into fixed, gap-free
`source_sentence_id` ranges. English order and timing remain Python-owned.

- Candidate boundaries are terminal `. ? ! , : ;` punctuation.
- Fragments with fewer than three spoken Tokens merge by punctuation direction:
  comma/colon/semicolon fragments normally merge right; short complete sentences normally merge
  left.
- `>>` or a pause of at least 1,200 ms is a hard boundary. An isolated `Yes.` stays independent
  when surrounded by long pauses.
- Punctuation-free speech has a 40-spoken-Token fallback cap.
- Known stage directions such as `[music]` and speaker markers retain their original Token IDs but
  do not appear in visible source text. Unknown bracketed text is preserved.
- Display timing comes from the first and last speech Token in a range, so hidden music Tokens do
  not keep a subtitle visible through a music gap.

The `source_sentences` table persists the Part-local ID, inclusive Token range, clean English,
speech-based times, hidden Token IDs, and Pass 2 Chinese. Final `segments` persist
`source_sentence_id` and `sub_id` for traceability.

### Current Pass 2: Place Pass 1 onto English cues

Pass 2 receives all ordered `{source_sentence_id, source_sentence}` rows for one complete Part, the
full Pass 1 reference translation, and Pass 0 context. It receives no word-level Token IDs or
timestamps.

Pass 1 is the translation. Pass 2's first job is to place that Pass 1 text back onto the
Python-owned English cue IDs. It does not translate the English from scratch. The Chinese for a cue
is whatever Pass 1 said for that English: a fragment, a clause, or more than one sentence.

Adjacent cues are one unfolding thought. Concatenating the Chinese rows in order should recover the
Pass 1 passage: no extra facts, no missing facts, and no repeated facts. A row must not become
self-contained by borrowing the next cue's meaning, and two neighboring rows must not paraphrase
the same meaning. Light rephrasing is allowed only to sit on a cue boundary. Pass 2 does no length
control.

Python rejects missing, duplicate, reordered, unknown, or empty ID rows. A full rebuild makes one
Pass 2 call per Part. A selected rebuild supplies the whole Part as context but outputs only source
sentences intersecting the selected range. Live prompt: `app/services/prompts.py`. Version:
`sentence-pipeline-v8.2`.

### Current Pass 3: Overlong Chinese splitting

Python sends only Pass 2 parents above 24 display units to Pass 3. Every input parent contains its
complete clean English, Pass 2 Chinese, and all raw Tokens in its parent range as
`{token_id,text,kind}`.

If Chinese punctuation can split a parent, Python does that first and Pass 3-1 only fills
`end_token_id`. Otherwise Pass 3-2 splits the Chinese and assigns `end_token_id`. This is the only
stage where English and Chinese inside a sub-cue may not match as meaning groups. Chinese
readability is primary; Token IDs stay strictly increasing. Prefer 18 display units when already
splitting; 24 is the hard max. It does not perform semantic or terminology auditing.

Python infers starts from the parent start and the previous end plus one. The final end must equal
the parent end, and every subrange must contain speech. Invalid structure fails visibly; a valid
unsplit long parent is retained and flagged for human review.

After Pass 3, Python restores speech-based times and runs one quality stamp over the Part's final
cues. Pass 2 materialize and Pass 3 splits do not flag review problems on their own. Prompt
version: `sentence-pipeline-v8.2`. Live prompts: `app/services/prompts.py`. Schemas:

- `app/schemas/subtitle-sentence-alignments.schema.json`
- `app/schemas/subtitle-sentence-boundaries.schema.json`
- `app/schemas/subtitle-sentence-splits.schema.json`

### Current review workbench

A cue enters the workbench only after that Part's Pass 3 step finishes (including the no-LLM path
when nothing is over 24). The stamp writes `quality_flags` and `needs_review`. The browser then
recomputes types 1–4 from current text and timing, and keeps `pass3_invalid_output` from stored
flags.

A cue is listed if it has one or more of:

- `target_text_too_long` — more than 24 display units
- `very_short_duration` — under 700 ms and not a tiny reply
- `very_long_duration` — over 5000 ms
- `high_target_reading_speed` — over 12 display units per second
- `pass3_invalid_output` — Pass 3 could not split the parent

There is no severity. There are no LLM semantic/terminology suggestions and no Apply path.
19–24 units stay unflagged. `GET /api/projects/{id}/review-issues` returns one row per problem cue.
The main list “Check readability” count uses the same live set. The `subtitle_review_issues` table
is leftover; the live path no longer reads or writes it, but a rebuild still deletes old rows.

The sections immediately below describe the retired v6 implementation and are kept only as
historical diagnostic context.

### Historical v6 Pass 2: Timed Cue Draft

Input:

- A Window of English Timed Tokens.
- The full Pass 1 Part reference translation.
- Pass 0 context.
- A few preceding Cues.

Output:

- Cue boundaries expressed with `start_token_id` and `end_token_id`.
- Chinese translation per Cue.

Important rules:

- Chinese viewing experience is primary.
- Prefer short, independently readable Chinese units.
- 18 display units is the normal maximum.
- 19-24 is a very rare phrase-integrity exception; at most about 2% of Cues should use it.
- Above 24 is unacceptable.
- Protect actual names, product models, and fixed expressions. Ordinary noun phrases, verb phrases,
  clauses, and English sentence boundaries are not reasons to retain a long Chinese Cue.
- Pass 2 does not run iterative repair loops anymore.
- Pass 2 receives `[token_id,text]` only. Timing is not part of the LLM task.
- Token coverage is a hard output contract. The first Cue starts at the Window start; every later
  Cue must satisfy `current.start_token_id = previous.end_token_id + 1`; the final Cue ends at the
  Window end. Token IDs may not repeat, overlap, go backwards, or disappear.
- Prompt v6.1 includes an explicit wrong duplicated-range example and a correct adjacent-range
  example. Python still rejects invalid coverage immediately and never guesses a semantic repair.
- There is deliberately no automatic Pass 2 correction call. An invalid model result remains a
  visible pipeline failure.

### Historical v6 Pass 3: AI First Review

This stage was redesigned in the latest implementation.

Pass 2 first completes the required Windows, then Pass 3 runs once per affected Part. During a full
rebuild it reviews the complete Part. During a Selected-Window Rebuild it receives only the selected
Window or Windows from that Part, plus a few neighboring Cues as read-only boundary context without
output-capable Token IDs. Pass 3 must inspect the complete supplied editable range and has exactly
three responsibilities:
resegment long Chinese Cues, mark semantic mismatches across complete nearby meaning groups, and
mark terminology inconsistencies. It must not assume that every individual Chinese Cue mirrors
the English Cue word-for-word.

Pass 3 actions:

1. `too_long`
   - Normally returns replacement Cues with exact, gap-free Token coverage.
   - A structurally valid replacement is auto-applied.
   - If the LLM cannot split reliably, it returns an empty replacement; the old Cue is retained and
     the issue remains visible in the workbench.
2. `semantic_mismatch`
   - Marks the complete nearby meaning group.
   - Never rewrites translation; `replacement_cues` is empty.
3. `terminology_inconsistency`
   - Marks the affected Cue range.
   - Never rewrites translation; `replacement_cues` is empty.

Pass 3 no longer receives absolute per-Token timestamps. Its prompt contains:

- `[token_id, text]` pairs for the editable range only during a targeted rebuild.
- Editable current Cues with mechanically calculated `display_units`.
- A few previous/boundary Cues as read-only context without output Token IDs.
- Full Part reference translation only when the complete Part is editable.
- Global project context.

Python accepts and applies issues only when they are wholly inside an editable range. A model issue
outside the selected range is recorded as an `ignored_scope_violation` audit diagnostic and skipped;
it does not fail the whole rebuild or enter the workbench. A `too_long` replacement must additionally
remain inside one original Window boundary.

The model receives no timestamps, durations, pause boundaries, reading-speed calculations, or
timing concerns. Python restores times from Token IDs and computes all timing/readability flags
after Pass 3.

Prompt/schema locations:

- `app/services/subtitle_planner.py`
- `app/schemas/subtitle-semantic-audit.schema.json`
- Prompt version: `part-review-v6.3`

For the 7,604-Token dopamine video, the normal full-rebuild no-retry call budget is 13: one Pass 0,
two Pass 1 calls, eight Pass 2 Window calls, and two Pass 3 Part calls. The original 600-Token,
per-Window Pass 3 design made fourteen Pass 2 and fourteen Pass 3 calls, for 31 total. A failed
Part-level Pass 3 in the full pipeline retains completed Pass 2 batches in SQLite. Selected-Window
Rebuild intentionally has no stage checkpoint: it publishes atomically at the end, and Retry reruns
its Pass 1/2/3 work so errors remain exposed rather than being hidden behind resume behavior.

## Latest Review-Issue Implementation

The live workbench is described under Current review workbench. The `subtitle_review_issues` table
and the old Apply/suggestion UI are retired. Rebuild still deletes leftover table rows when it
clears subtitle state.

Relevant locations:

- Quality stamp and live problem list: `app/services/subtitle_quality.py`
- Stamp after Pass 3: `app/pipeline.py`
- API: `GET /api/projects/{project_id}/review-issues` in `app/main.py`
- Frontend: `renderReviewIssues` and `toggleReviewMode` in `app/static/app.js`

## Subtitle Editing and Rendering Behavior

- Segment text edits are written to SQLite on Enter or blur.
- Applying a review suggestion writes immediately.
- Existing automatic per-keystroke save behavior was intentionally removed because it caused focus
  loss and editing problems.
- Exact FFmpeg Preview is independent from the Subtitle Style panel.
- Exact Preview replaces the video with an FFmpeg-rendered PNG for visual fidelity.
- Clicking the left/right half of the PNG moves to the previous/next Cue.
- Exact Preview PNGs are temporary and should not remain in project/output directories.
- Final render locks all interactions for the current project, while other projects remain usable.
- Final outputs are versioned rather than overwritten.

Subtitle style is project-specific and editable without restarting. It includes:

- Chinese/English font size.
- Chinese/English color.
- Chinese/English outline.
- Bottom margin.
- Line gap.
- Horizontal margin.
- Bottom-mask height and opacity.
- Show/hide English subtitles.
- Copy/paste a complete style between projects through browser local storage.

The exact visual source of truth remains FFmpeg; CSS preview is intended primarily for timing.

## Current Project Data

Important projects in `data/video_translator.db`:

### Codex dopamine project

- ID: `c6220602-8850-4141-948d-cd7dfbeb226f`
- Display name: `codex dopamine`
- Title: `I Doubled My Dopamine Everyday For 7 Days`
- Provider: `codex`
- Status/stage: `waiting_review` / `waiting_review`.
- Current saved Cues: 351.
- Timed Tokens: 7,604.
- Parts: Part 1 complete with four Batches; Part 2 pending with four Batches.
- Persisted review issues: 34.
- Subtitle revision: 1.
- A real Selected-Window Rebuild of Part 1 / Window 1 completed successfully through Pass 0 review,
  Pass 1, Pass 2, the narrowed Pass 3, and atomic publication on 2026-09-18.

### Grok dopamine project

- ID: `53accdce-a3d6-4da1-9d85-df5970ceaf43`
- Display name: `grok dopamine`
- Title: `I Doubled My Dopamine Everyday For 7 Days`
- Provider: `grok`.
- Status/stage: `failed` / `failed` at 94%.
- Last pipeline detail: Pass 3, Part 2/2, Windows 1-5.
- Latest error: `CLI call timed out after 600 seconds`.
- Current saved Cues: 872; Timed Tokens: 7,604; persisted review issues: 56.
- Part 1 is complete with six Batches. Part 2 has five completed Batches but its Part-level Pass 3
  failed, so the Part status is `failed`.
- The Grok plain-text integration bug described below is fixed; the latest full rebuild progressed
  past Pass 1 and all Pass 2 Windows. The current blocker is a separate Pass 3 timeout.

### Insecurities project

- ID: `b11bec68-39cc-4c02-915f-3bce1d672f39`
- Title: `Why Looking Better Won't Fix Your Insecurities`
- Provider: `grok`.
- Status: `completed`

### Ancient humans project

- ID: `0d5e8c06-de4a-4618-938f-e0c2f9e1f1c7`
- Title: `What Did Ancient Humans Actually Do All Day?`
- Provider: `grok`.
- Status/stage: `completed` / `completed`.
- Timed Tokens: 2,516; saved Cues: 397; persisted review issues: 5.

## Known P0 Semantic-Shift Example

At approximately 32:53 in the old dopamine project:

```text
English: I just did something crazy.
Chinese: 那双700美元的鞋还在袋子里
```

The raw Timed Tokens are correct. Pass 1's coherent Part translation was correct. Pass 2 created a
continuous semantic phase shift while assigning the Part translation to timed Cue ranges. The old
Pass 3 detected only part of the shifted range and repaired only through Token 5636, leaving the
shift from Token 5637 onward.

The relevant sequence is:

```text
5621-5628 Those $700 shoes are still in the bag,
5629-5636 and I feel the most disconnected from everyone.
5637-5641 I just did something crazy.
5642-5650 I'm in a bit of a rut right now,
5651-5661 and I just feel like I need to leave the country.
5662-5671 So, I just booked a flight to go to Miami
5672-5675 first thing tomorrow morning.
5676-5681 It's 9:15 p.m. right now.
```

This example motivated the new Pass 3 design. Semantic alignment is subtle because individual Cue
pairs may look misaligned while neighboring Cues are correct as a group. Python must not make this
semantic decision.

## Latest Verification Status

### 2026-09-19 sentence-pipeline-v8.2

Current live contract: Pass 2 places Pass 1 onto English cues with adjacent coherence; Pass 3
splits only Chinese above 24; Python stamps review flags once after each Part's Pass 3; the
workbench is live mechanical problems only. Prompts live in `app/services/prompts.py`.

### 2026-09-19 sentence-pipeline-v7.0 implementation

- Full rebuild now runs one Pass 2 call per Part over fixed Python-built source sentences.
- Pass 3 receives only parents above 18 display units and returns Chinese subsentences plus
  `end_token_id` values.
- Selected-Window rebuild is available only after one v7 full rebuild. Its requested Window ranges
  expand to complete intersecting source sentences, and publication remains atomic.
- Pass 2 results are checkpointed before Pass 3. A failed full-rebuild Pass 3 retry reuses completed
  Pass 2 output.
- No subscription CLI was called for acceptance in this implementation turn. Tests used mocked
  Pass1/2/3 outputs and the existing dopamine Timed Tokens.
- Existing dopamine data produced 913 source sentences from 7,604 Tokens, with 240 hidden stage or
  speaker Tokens and a maximum of 36 visible English words per sentence. The 2:23 omitted-meaning
  region became four consecutive fixed sentence IDs; the 9:46-10:40 sample respected comma
  direction and long-pause boundaries.
- Its two current Parts contain 442 and 471 source sentences; their complete Pass 2 prompt payloads
  are about 57 KB and 59 KB before provider wrapping. Real CLI response capacity remains for the
  user's manual full-flow acceptance test.
- Python simulation verified exact Pass 2 ID coverage and Pass 3 start inference from strictly
  increasing end IDs without Token gaps or overlaps.

The last broad automated suite run, before the latest Selected-Window payload, progress-detail, and
Grok-wrapper changes, completed successfully:

```text
Ruff: passed
JavaScript syntax: passed
Pytest: 61 passed, 2 skipped
SQLite integrity_check: ok
```

Commands:

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests scripts
node --check app/static/app.js
.\.venv\Scripts\python.exe -m pytest -q
```

The two skipped tests are environment-dependent. The only reported warnings are Starlette/httpx
deprecation warnings. Per the user's explicit instruction, the newest changes were not rerun through
automated tests by the coding agent; the user is performing manual subscription-CLI acceptance.

Subsequent real manual evidence:

- Codex Selected-Window Rebuild for `c622...`, Part 1 / Window 1, completed through Pass 3 and
  atomic publication.
- Grok full rebuild for `53acc...` completed Pass 0, Pass 1, and all Pass 2 Windows. It later timed
  out during Pass 3 for Part 2, which is the current exposed failure.

## Historical Targeted Real-Model Validation Attempt

This section is retained as diagnostic history. Later manual runs supersede it for current
acceptance status; see `Latest Verification Status` and `Latest Integration Changes`.

The user explicitly requested that acceptance validation use only the problematic Window, not the
whole Part.

Targeted script:

- `scripts/validate_review_window.py`

Target:

- Old dopamine project.
- Token 5637.
- Window 5584-6192.
- Codex model `gpt-5.6-luna`.
- Existing Pass 1 reference extracted from the old Pass 3 audit snapshot.
- No SQLite writes.

The valid targeted Codex CLI call reached the configured 600-second timeout without returning JSON.
It did not fail schema validation because it returned no response at all. Do not claim that the
real-model acceptance test passed.

Diagnostic artifacts:

```text
data/projects/53accdce-a3d6-4da1-9d85-df5970ceaf43/artifacts/targeted-review-window/
```

The last files include a request and `call-error` record. All temporary validation Python processes
were stopped. SQLite was not changed by the validation script.

Do not automatically retry the full Part. If another real-model test is necessary, keep it to this
Window and first diagnose why Codex CLI did not return. Do not silently change to Grok merely to
obtain a passing result.

## Important Existing Audit Evidence

Old dopamine audit directory:

```text
data/projects/53accdce-a3d6-4da1-9d85-df5970ceaf43/artifacts/translation-audit/batch-11/
```

Important files from the original phase-shift diagnosis:

- Pass 2 response that first introduced the shift:
  `20260917T122222404867Z-response-75e9c44a.json`
- Old Pass 3 response that detected only part of it:
  `20260917T122407159913Z-response-72a778a2.json`
- Old final semantic-applied result that still contains the bad mapping:
  `20260917T122451410096Z-semantic-audit-applied-d00d8c14.json`
- Old Pass 3 request reused for its Part reference during targeted validation:
  `20260917T122337720654Z-request-fc9990de.json`

## Immediate Follow-Up Notes

1. Restart the application before browser testing so the new database migration, endpoint, static
   JavaScript, and CSS are loaded.
2. Existing projects can immediately show current mechanical quality issues in Review suggestions.
3. Persisted semantic suggestions only exist after a project runs the new Pass 3 during Rebuild.
4. Do not run a full dopamine Rebuild merely to smoke-test the UI unless the user explicitly asks.
5. If testing persistence without LLM cost, use repository/API unit tests or insert a fixture into a
   temporary test database.
6. If changing Pass 3, preserve the separation:
   - Python handles structure, timing arithmetic, storage, and exact Token coverage.
   - LLM resegments overlong Cues and marks semantic/terminology suspicion.
   - Semantic and terminology issues go to the human without an LLM rewrite.
7. Review mode intentionally ignores Reviewed state for filtering in this version.
8. The pre-translation terminology editor is implemented as the Pass 0 Human Review Checkpoint
   described above.
9. The Codex Selected-Window path has now completed one real subscription-CLI run for Part 1 /
   Window 1. Do not generalize that result to a complete full-video Codex or Grok acceptance run.

## Historical 2026-09-18 Prompt v6 Targeted Script Attempt

This section records an earlier isolated script attempt and is superseded for current acceptance
status by the later successful v6.1 Selected-Window Rebuild documented above.

Implemented:

- Pass 2 now strongly prioritizes Chinese-first segmentation, normally at or below 18 display
  units; 19-24 is rare and above 24 is forbidden by the prompt.
- Pass 2 input contains Token IDs and text only. The obsolete Pass 2 repair functions and
  `subtitle-repairs.schema.json` were removed.
- Pass 3 input contains Token IDs/text, current Cues, and Python-calculated `display_units`; it has
  no timestamps, duration, pause, or reading-speed concepts.
- Pass 3's external schema accepts only `too_long`, `semantic_mismatch`, and
  `terminology_inconsistency`.
- Valid `too_long` replacements are auto-applied. Empty or invalid replacements retain the old Cue
  and create a review issue. The other two types never rewrite translation.
- Python recalculates timing and length flags after Pass 3; those mechanical issues are exposed by
  the review API.
- `scripts.validate_review_window` now runs Pass 2 then Pass 3 for `--part` / `--window`, writes
  isolated audit/report files, and never mutates SQLite.
- Automated result: 64 passed, 2 skipped; Ruff and `node --check app/static/app.js` passed.

Real-model attempt against current dopamine Part 1 / Window 1 (Token 0-994):

- Project model/provider: Codex `gpt-5.6-luna`, reasoning effort `low`.
- The first Pass 2 request started at 13:42:39 local time and timed out after 600 seconds.
- One identical retry started at 13:53:18 and also timed out after 600 seconds.
- Neither attempt returned stdout/JSON, so neither reached structural validation or Pass 3.
- Each attempt produced exactly one request and one timeout record. SQLite was not modified.
- The v6 request was about 43 KB. The earlier v5 request for the same Window was about 59 KB and
  had completed in about 53 seconds, so request size alone does not explain the new timeouts.
- A separate two-Token `Hello. World.` schema health check against the same Codex model also
  returned no stdout and timed out at a deliberately reduced 60-second limit. At that time this
  pointed to the Codex CLI/model call path rather than Window size or Prompt v6 content.

Artifacts:

```text
data/projects/c6220602-8850-4141-948d-cd7dfbeb226f/artifacts/targeted-window-tests/
  20260918T114239Z-part-01-window-01/
  20260918T115318Z-part-01-window-01/
```

## 2026-09-18 Selected-Window Rebuild

The Rebuild button now opens a scope dialog with `Selected Windows` and `Full rebuild` modes.

Selected-Window behavior:

- Reuses existing Timed Tokens, Parts, Batches, and immutable Window Token envelopes.
- Stores the selected ranges plus the starting `subtitle_revision` in
  `projects.pending_rebuild_scope`.
- Regenerates Pass 0 from the whole video and pauses at the existing human review panel.
- After approval, regenerates Pass 1 once for every affected Part.
- Runs Pass 2 exactly for the selected Windows without deleting their existing Cues first.
- Runs one Pass 3 call per affected Part. Only selected Windows are supplied as editable Cues and
  Tokens; nearby Cues are separate read-only boundary context without output Token IDs. Python
  enforces editable ranges and separate replacement ranges. A replacement cannot cross a Window.
- Ignores and audits an LLM issue outside the editable ranges instead of failing or modifying an
  unselected Window.
- Keeps existing subtitles visible but read-only while the selected rebuild is pending.
- Publishes all affected Part results in one SQLite transaction only after all selected work
  succeeds. A revision mismatch aborts publication instead of overwriting concurrent edits.
- Retains review issues outside the selected ranges and replaces issues intersecting selected
  ranges.
- Rejects selection when an affected Part lacks exact Token coverage or an existing Cue crosses the
  selection boundary; selecting the adjacent Window resolves the latter case.
- Writes per-run audit artifacts under `artifacts/partial-rebuilds/<run_id>/`.
- Does not use a Pass-level checkpoint. If the atomic selected rebuild fails, Retry reruns Pass 1,
  Pass 2, and Pass 3 for the selected scope; it does not silently resume past the exposed failure.

Full rebuild retains the earlier behavior: the API immediately clears old translation state and
queues reconstruction of the complete pipeline.

Implementation locations:

- Dialog: `app/static/index.html`, `app/static/app.js`, `app/static/styles.css`
- API and range preflight: `app/main.py`
- Durable scope and atomic publication: `app/db.py`, `app/repository.py`
- Pipeline orchestration: `app/pipeline.py`, `app/worker.py`
- Pass 3 prompt plus Python range enforcement: `app/services/subtitle_planner.py`

Per user instruction, this feature was not run through automated tests or a real CLI call in this
implementation turn. It has since completed one real Codex Selected-Window run as recorded above.

## 2026-09-18 Latest Integration Changes

### Pass 2 coverage failure and Prompt v6.1

A real Codex Retry returned syntactically valid JSON but reused source ranges across multiple Cues,
including `17-26` twice and overlapping `27-41`/`35-41`. Python correctly rejected it with
`expected start 27, received 17-26`; there were 25 continuity breaks in that response. No database
publication occurred.

The Pass 2 prompt now states the adjacency equations in uppercase hard-contract language, includes
wrong/correct Token-range examples, and requires an adjacent-pair self-check. The user explicitly
rejected an automatic repair call: invalid coverage must continue to fail visibly.

### Targeted Pass 3 scope correction

The first Selected-Window Pass 3 request accidentally included the complete Part's Tokens and
current Cues while marking only Token `0-994` editable. The model returned 54 issues, including
`issue_24` at `1045-1068`, and Python aborted correctly. The implementation now constructs
`EDITABLE_WINDOWS` containing only the selected ranges. Boundary context is separate and has no
output Token IDs. Out-of-scope model issues are logged under `ignored_scope_violations` and skipped.

### Pipeline progress detail panel

Projects now have a JSON `pipeline_detail` column. The pipeline writes the current Pass, Part,
Window, total counts, selected-Window ordinal, and Token range. The browser progress card renders
details such as:

```text
Pass 2 · Part 1/2 · Window 1/4 · Tokens 0-994
```

Pass 3 shows the affected Part and Window list. A failed job deliberately retains its last detail,
which makes the failure location visible beside the error. Successful review/render completion
clears the detail. The migration is applied by `init_db()` when the application restarts.

### Provider switching and Grok text output

Provider choice is project-specific and may be changed whenever the project is not queued or
running. Python-owned Token/Part/Window boundaries and validation do not change between providers.
A full rebuild clears Pass 0 and runs all passes with the newly selected provider. Switching a
provider before Retry uses the new provider for subsequent calls while an already approved Pass 0
may remain reused.

Grok CLI 1.0.34 defaults to plain stdout for text calls. `run_grok_text()` incorrectly parsed that
plain Chinese as a JSON envelope, producing `Expecting value: line 1 column 1 (char 0)` even though
the Grok session contained a complete valid Pass 1 translation. The command now explicitly passes
`--output-format json` and continues reading `envelope["text"]`. Structured calls already use
`--json-schema`, which implies JSON output.

The application runs Uvicorn with `reload=False`; restart with `start-video-translator.bat` after
code, static-asset, or schema changes. The start script stops the prior owned process tree first.

## Dependencies

Core runtime dependencies include:

- Python virtual environment under `.venv`
- FastAPI/Uvicorn
- SQLite
- FFmpeg/FFprobe
- yt-dlp
- Node.js
- Codex CLI
- Grok CLI
- WhisperX only when caption transcription is needed

See `.env.example` and `README.md` for configuration. Current application port is 6866.
