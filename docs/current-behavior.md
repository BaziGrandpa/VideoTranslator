# Current behavior

Verified against the working tree on October 19, 2026.
This is the detailed implementation reference. [README](../README.md) covers setup;
[the audit](audit-2026-10-15.md) tracks unresolved differences and risks. Code references
below identify the implementation to inspect when changing a contract.

## 1. Domain and source of truth

| Object | Identity | Purpose / storage |
| --- | --- | --- |
| Token | `token_id`, project-local, starts at 0 | Original word/time anchor in `tokens` |
| Part | Database `id`; separate project-local `part_number`, starts at 1 | Processing range in `parts` |
| Cue | `cue_id`, Part-local, starts at 1 | Python-cut English range and Pass 2 Chinese in `cues` |
| Sub-cue | `subcue_id`, Cue-local, starts at 1 | Validated Pass 3 split in `subcues` |
| Segment | Database `id`; project-local `segment_order`, starts at 1 | Editable screen subtitle in `segments` |
| Component | Ordered `(part_id, cue_id, subcue_id)` reference | Segment provenance in `segment_components` |

A whole Cue component has a null `subcue_id`. One Segment can reference multiple whole
Cues, multiple Sub-cues, or a whole Cue plus a Sub-cue. Components record origin; they do
not become new translations when a user edits Segment text. A manual edit can therefore
make the displayed words differ from their original Token/Cue provenance.

Tokens retain their original words/timing during a pipeline generation. Punctuation is a
separate annotation layer, returned as a reading view by the repository. A full rebuild
deletes and reconstructs Tokens; "immutable" does not mean preserved across full rebuilds.
SQLite stores editable state; source media, artifacts, and rendered outputs are files.

Implementation: [schema](../app/db.py), [repository](../app/repository.py).

## 2. Pipeline and model-call contracts

### Source preparation and Parts

- One YouTube URL creates one video project; playlists are not imported as projects.
- Download prefers original manual English captions, then original automatic English
  captions. It does not request auto-translated caption tracks.
- Rolling VTT captions are reconstructed into a word timeline. Without a subtitle file,
  WhisperX supplies word timings; failed alignment can fall back to inferred word times.
  Missing WhisperX dependencies produce a normal failed job with an installation hint.
- An empty timeline or a whole project with no classified speech produces
  `status=failed, stage=no_subtitles`. The UI displays **No usable subtitles**, hides
  Retry, and the retry API returns 409. Music-only captions do not trigger a second ASR
  attempt. This guard is project-wide; a music-only Part in a mixed project remains an
  edge case (audit A05).
- Part duration uses the Token timeline, not necessarily the full media duration.
  Up to 40 minutes uses one Part. Longer timelines target balanced Parts of 20–40
  minutes around 30 minutes, preferring chapter boundaries, pauses, and punctuation.
  Sparse sources may prevent these target durations. Part planning precedes punctuation
  restoration and therefore cannot use newly restored punctuation to choose boundaries.

### Passes

Counts below apply to one provider attempt, except the explicitly single-provider
Punctuation Pass. A user-started rebuild is a new run.

| Stage | Input and output | Planned requests |
| --- | --- | --- |
| Punctuation | Original Part Tokens → separate `after_token_id` / mark annotations | One per eligible Part, no fallback |
| Pass 0 | Project transcript → overview, style, recurring terminology, card candidates | One up to 12,000 Tokens; otherwise 6,000-Token chunks plus consolidation |
| Pass 1 | Whole Part + guidance → continuous Chinese text | One per Part |
| Python Cue cutting | English Token timeline → fixed Cue ranges | None |
| Pass 2 | Fixed Cues + Pass 1 reference + guidance → Chinese per Cue | One per Part |
| Pass 3-1 | Python punctuation-split Chinese pieces → Token boundaries | At most one per Part when needed |
| Pass 3-2 | Other long Chinese Cues → Chinese pieces and Token boundaries | At most one per Part when needed |
| Python finalization | Validated ranges → Sub-cues, merged Segments, review flags | None |
| Card Pass | Complete English Part and approved card terms → Chinese names and short background explanations | One per Part with pending candidates |

**Punctuation eligibility is literal:** a Part must contain zero occurrences of
`, . ? ! : ;` in its original normalized Tokens. A decimal point or abbreviation counts;
this is not a test for complete sentence punctuation. Annotations must be ordered, unique,
refer to speech Tokens, and include at least one `.`, `?`, or `!`. Marks commit together
after validation and source-fingerprint checking. A failed/interrupted attempt is not
automatically retried; a full rebuild resets its checkpoint.

**Pass 0 requires approval.** Candidate terminology is limited to 60 before filtering,
and up to 40 recurring terms are retained using exact phrase occurrence counts of at
least two. The user may edit the overview, style, terms, categories, and mandatory-use
flags. Saving a draft remains paused; approval queues translation. Mandatory terminology
is a prompt instruction, not a deterministic check on every final Segment.
Direct and chunk-consolidation Pass 0 prompts request Simplified Chinese `overview` and
`style` values while retaining original English spelling in term `source` values. The
schema validates structure rather than language, so the user still reviews the result.
Existing saved contexts are unchanged until edited or regenerated.
Card candidates are separate from the recurring translation glossary. Pass 0 returns the
subtitle phrase (`source`) and a corrected English display term (`display`); these may be
identical. Python matches each source against original Tokens, keeps the first Token span,
and drops only unmatched candidates. The Pass 0 UI permits removing a candidate or editing
its display term, while its source phrase remains fixed. The display term need not appear
verbatim in the transcript.

**Provider behavior:** project modes are Auto, Codex only, and Grok only. Auto uses the
configured provider order (default Codex → Grok). Punctuation uses the first selected
provider only. Later Passes can fall back after an error caught inside their provider
loop. Validation outside that loop, including Pass 1's empty-text check and some Pass 0
normalization, does not trigger fallback. Pass 2 validation is inside its provider loop.
Pass 3 wraps both subpasses in one provider loop, so fallback can repeat Pass 3-1 after
Pass 3-2 fails. Per-Cue rejected splits may instead retain the parent and flag it.
There are no extra repair calls. Tests must simulate these providers.

**Validation versus intent:** Pass 2 checks exact Cue IDs/order, nonempty normalized
Chinese, and source ranges. Its instruction to preserve Pass 1 Chinese is not enforced
by a text-conservation comparison. The user has confirmed that Pass 2 has discretion to
adjust Chinese: verbatim preservation is not a product invariant, and the lack of that
validator is intentional. Existing prompt wording is stricter guidance to the model.
Pass 3-2 similarly checks ranges/text shape without
proving Chinese conservation. Exact Token coverage does not prove semantic alignment.
Pass 3-1 uses Python-owned Chinese pieces; invalid model boundaries can be replaced with
proportional speech-Token boundaries based on piece length. That provenance is recorded
as `proportional_fallback`, but success does not add a dedicated review suggestion.

Implementation: [pipeline](../app/pipeline.py), [prompts](../app/services/prompts.py),
[planner](../app/services/subtitle_planner.py), [schemas](../app/schemas/),
[punctuation](../app/services/punctuation.py), [Parts](../app/services/parts.py),
[downloader](../app/services/downloader.py), [transcriber](../app/services/transcriber.py).

## 3. Cutting, merging, and display text

### Automatic segmentation

- English Cues respect hard gaps of at least 1,200 ms and speaker boundaries. Sentence
  punctuation, continuation punctuation, short-fragment merging, and a 40-speech-Token
  length cap determine the remaining cuts. Abbreviations/decimals receive special handling.
- Short English fragments cannot merge back across a hard gap. They may merge forward
  into the next sentence. Removing a visible music label does not repair such alignment.
- Chinese parents above 24 display units enter Pass 3. Python first tries Chinese
  punctuation boundaries (`，。！？；`) and keeps following closing quotes/brackets with
  the preceding punctuation. Pieces under 4 display units are absorbed into neighbors.
  Malformed opening quotation marks are not generally reinterpreted as closing marks.
- When Python produces usable pieces, Pass 3-1 assigns boundaries. Otherwise Pass 3-2
  does the splitting. A surviving piece may still exceed 24 units and require review.
- Sub-cues with at most 3 spoken Tokens can merge with a neighbor **inside the same
  Cue** when the combined Chinese stays within 24 units. This can cross a sentence
  boundary inside that Cue; it cannot move words into the preceding Cue.
- A separate final rule can merge a 1–3-character Chinese whole Cue ending in `。` or
  `.` backward: both Segments must be whole Cues in the same Part, their Token ranges
  contiguous, the gap nonnegative and below 1,200 ms, and combined Chinese at most 24
  display units. A one-character subtitle is not guaranteed to satisfy these conditions.

### Stored and displayed text

Known stage directions and speaker labels are removed from visible text, but retained
in source Tokens. Chinese normalization removes layout whitespace while retaining spaces
between Latin letters/digits. Stored Chinese retains sentence punctuation.

`display_target_text` removes trailing `，。,.` and dash variants, including immediately
before closing quotes/brackets. It retains question marks, exclamation marks, semicolons,
and the closing marks themselves. ASS, exact preview, Chinese SRT, and quality measurement
use this helper; the lightweight browser preview mirrors the trailing-mark rule.

Display units count non-whitespace East Asian wide/fullwidth characters as 1 and other
characters as 0.5 after display normalization. This is not a raw character count.

Implementation: [Cues](../app/services/cues.py), [planner](../app/services/subtitle_planner.py),
[text normalization](../app/services/subtitle_text.py), [subtitles](../app/services/subtitles.py).

## 4. Review suggestions

| Type | Current trigger |
| --- | --- |
| `very_short_duration` | Duration < 700 ms AND (English word count > 2 OR Chinese display units > 4) |
| `very_long_duration` | Duration > 5,000 ms AND reading load < 2.5 units/second |
| `high_target_reading_speed` | Reading load > 12 units/second |
| `target_text_too_long` | Display length > 24 units |
| `single_chinese_character` | After display normalization and ignoring whitespace/punctuation, exactly one character in U+3400–U+9FFF remains |
| `pass3_invalid_output` | Persisted flag for a parent whose Pass 3 split could not be used |
| `audio_effect_boundary` | Speech on both sides of a music/applause marker, with the preceding speech Token not ending in `.?!` after closing marks |

The audio rule recognizes runs classified as stage directions containing `music`,
`applause`, `音乐`, `掌声`, or musical-note symbols. It maps the nearest speech Tokens
on each side to current Segment Token ranges. Manual Split halves have no exact Token
range, so their original Cue/Sub-cue links provide conservative source ownership. Both
halves may therefore retain an audio suggestion until their text is reviewed. Both owners
are suggested; if both belong to one Segment, that Segment appears once. Intro/outro effects with speech on only one
side and effects after a completed sentence are excluded. Other effect names are not
included. This is narrower than sending all effect-adjacent Cues to review (audit A01).

The live API recomputes mechanical issues, adds the stored Pass 3 failure flag, and
derives audio issues from Tokens. Audio issues do not rewrite stored `quality_flags`
or Segment status. Existing projects receive the rule immediately if provenance exists.
Manual text changes do not move Token ownership, so an audio warning may survive a repair.

Stored flags/status are stamped after Pass 3; the live list is not simply a query of
`status=needs_review`. Marking **Reviewed** does not dismiss a suggestion. Manual merging
does not restamp flags; the retained left Segment's old flags can remain. The 18-unit
soft target and 2% exception budget are statistics, not an eighth issue or render gate.

**Approve all and render** validates basic times, nonempty visible text, no overlaps,
and completion of existing Parts. It marks Segments Reviewed and queues rendering; it
does not require zero quality suggestions or individual review of every row. An empty
Segment list is rejected with 422 before a render job is queued; the renderer independently
rejects empty input before starting any subprocess (audit A04, fixed).

Implementation: [quality rules](../app/services/subtitle_quality.py),
[review API](../app/main.py), [UI](../app/static/app.js).

## 5. Editing and playback position

- Text saves on Enter or blur, with Unsaved/Saving/Saved feedback and an unload warning
  for unfinished edits. Shift+Enter inserts a manual Chinese line break. Segment edits
  preserve these breaks through browser preview, ASS/FFmpeg, and Chinese SRT; generated
  translations continue to normalize layout whitespace. Ordinary editing changes Segments,
  not Tokens or Cue translations.
- Boundary controls move by 100 ms or accept an explicit time. Neighbor synchronization
  defaults on. Independent boundaries can create overlaps that final validation rejects.
  Subtitle timing edits are not currently capped to the media duration.
- **Line gap +30** stores a boolean on each Segment. Effective bilingual separation is
  `project.layout.line_gap + (extra_line_gap ? 30 : 0)`. A project using 50 gets 80 only
  on checked Segments. English stays at the base bottom margin; Chinese moves upward.
  Chinese-only mode ignores this extra bilingual gap. Other Segments keep their default.
- **Chinese only** is a per-Segment boolean. It suppresses the English ASS event and
  English SRT entry for that Segment, positions its Chinese event at the base bottom
  margin, and allows empty English text through render validation. Chinese text remains
  required. The English source field is retained in storage, and embedded text already
  present in the source video is unaffected. Merge next requires matching modes.
- **Merge next** requires browser confirmation. It merges the next adjacent Segment in
  the same Part, including a whole Cue followed by the next Cue's first Sub-cue. Cross-Part
  merges are rejected. It retains the left ID/start, takes the right end, concatenates
  normalized text, combines ordered component links, and sets status Edited. The extra
  gap is enabled if either input enabled it. Stored review flags are not regenerated.
- **Split** sits to the right of Merge next and cuts the selected Segment at the time
  midpoint, provided each half lasts at least 100 ms and the Segment has Cue/Sub-cue
  links from one Part. The first half keeps its English and Chinese text; the new second
  half has empty text fields and is focused for editing. Both retain the original Part,
  source links, line-gap setting, and Chinese-only mode. Their exact Segment Token ranges are cleared because
  the manual midpoint need not match a Token boundary. Existing timing controls can move
  the new boundary. Empty Chinese text prevents final rendering until it is filled in;
  empty English is allowed when Chinese only is checked.
- **Scroll to current subtitle** positions the list immediately. Pointer clicks on Preview,
  Exit preview, and scroll controls release button focus. The global Space handler skips
  inputs, buttons, and focused video controls so native keyboard handling runs once.
- `resume_time_ms` stores video time only. A five-second wall-clock interval saves changed
  positions while the workspace/player is ready, even when paused. Switching projects
  also attempts a save. Reopening waits for video metadata, restores the time, and selects
  and centers the Segment satisfying `start <= time < end`. A gap selects no Segment.
  Playback is not automatically resumed. No operation-position or last-edited-Cue record
  is stored. Unload does not force a final position write, and hidden tabs can throttle timers.

Implementation: [UI](../app/static/app.js), [repository](../app/repository.py), [API](../app/main.py).

## 6. Rebuilds, checkpoints, and recovery

| Action | Reused | Replaced / publication |
| --- | --- | --- |
| Retry failed translation | Durable source/Part checkpoints as available | Resume appropriate processing; punctuation failure is not automatically resubmitted |
| Full rebuild | Downloaded video, captions, style, cards, playback position | Synchronously delete subtitle state, then reconstruct Tokens, Parts, punctuation, Pass 0–3 |
| Full rebuild + Skip Pass 0 | Above, plus existing guidance and approval state | Same subtitle reset; unapproved guidance still pauses for approval |
| Selected Parts | Approved guidance, Tokens, punctuation, Part/Cue ranges, old stored Segments | Pass 1–3 results publish together after all selected Parts finish, under a revision guard |

Full rebuild also resets the output link/rendered revision and deletes completed/failed
job records. Existing MP4/SRT files and historical artifacts remain. The worker repeats
subtitle clearing when starting the full rebuild. It is not a nondestructive retry.

Selected-Part rebuilding requires existing provenance and complete Token coverage. For
manual Split halves, their shared Cue/Sub-cue source ranges establish coverage; untouched
Cues provide the actual rebuild input. It does
not re-cut English Cues or rerun punctuation, so it cannot repair a bad original Cue boundary.
The UI locks subtitle editing and may hide the workspace while processing. Old database
rows remain until commit. Concurrent API edits cause the revision guard to reject publication.
Failed/interrupted selected rebuilds retain old subtitles; start another Rebuild to retry.

The launcher normally starts one API and one sequential job worker. Startup recovers
interrupted prepare/translate/full-rebuild jobs by queueing them again. Interrupted render
and selected-Part rebuild jobs are failed/restored or recognized as already published/cancelled;
they are not automatically repeated. Multi-worker/concurrent mutation guarantees are limited.

Implementation: [rebuild API](../app/main.py), [pipeline](../app/pipeline.py),
[job recovery](../app/repository.py), [worker](../app/worker.py).

## 7. Preview, rendering, and commentary cards

The browser CSS subtitle overlay is an approximate timing/style preview. **Preview** pauses
video and requests a PNG from FFmpeg using the same subtitle/filter configuration as the
final video. A subtitle need not be selected. Active subtitles/cards are found by timestamp;
card drafts can be previewed without saving. Clicking the image halves navigates subtitle
midpoints. Exit preview discards browser image URLs and cancels outstanding preview work.
Server preview files use temporary directories and normal cleanup.

Final output is MP4 with H.264 High profile, AAC at 192 kbit/s when source audio exists,
and faststart. Encoder choices are:

| Setting | Encoding parameters |
| --- | --- |
| `libx264` (default) | CRF 20, medium, yuv420p |
| `h264_qsv` | global quality 20, medium, nv12 |
| `h264_nvenc` | CQ 20, VBR, preset p4, yuv420p |

GPU choices accelerate encoding only. Decode/filter/ASS/mask work has no configured GPU
pipeline. The gradient is generated once and repeated. Hardware availability is not
automatically probed and there is no automatic CPU fallback. Exact preview still uses
FFmpeg's image path. The FFmpeg build must support ASS and the `-/filter_complex` option.
Actual playback/upload compatibility depends on the target; no platform certification is implied.

Outputs are staged as `.rendering-vNNN.mp4` and published as `final-vNNN.mp4` after FFmpeg
success, together with versioned English/Chinese SRTs. Version is one above the highest
existing nonempty final MP4; cancelled/failed attempts can reuse a number. Earlier completed
versions are preserved. Filesystem publication and SQLite state are separate operations.
Exceptions inside the renderer terminate and wait for a still-running FFmpeg process
(kill after a five-second termination timeout), close its pipes, and remove this attempt's
temporary MP4, attempted SRTs, and newly renamed MP4 if publication did not finish successfully.
Earlier completed versions are not removed. Abrupt process termination and filesystem
cleanup failures remain outside this normal exception-cleanup guarantee.

**Cancel render** requires confirmation. A queued job can be cancelled without starting
FFmpeg; a running job sets a cancellation request, which the worker checks while reading
progress and around publication. Normal cancellation terminates/kills FFmpeg, removes this
attempt's unfinished output, and restores the previous review/completed state as applicable.
It preserves earlier completed videos. Cancellation is not guaranteed immediate if FFmpeg
stops emitting progress; crash cleanup has separate limitations (audit A06).

Progress is based on the last subtitle end, not media duration, so a long outro can leave
the progress display at 99% before the video finishes.

Cards have separate data and render revisions. They support top left/center/right placement,
default right/32% width, and default 800 ms fade-in/out. They cannot overlap or exceed media
duration; combined fade time cannot exceed card duration. Text must fit the safe picture area.
Saving style makes it the cross-project default for new cards. Saved card edits invalidate
the current output. Cards survive subtitle rebuilds and never invoke translation. Card API
create/update/delete operations return 409 during `render_queued`, `rendering`, and
`render_cancelling`; editing resumes when the project leaves those stages (audit A07, fixed).

After all Parts complete Pass 3 and final subtitle validation, Card Pass reads each relevant
complete English Part and its approved candidates. The model supplies a Chinese name and
an explanation aiming for about 40 Chinese characters, with room for more detail when
needed, using general background knowledge. Python supplies the corrected English display
term and maps the first original source occurrence to its Cue,
uses a five-second duration and the latest saved card style, and shifts cards to avoid
overlap. Visual-fit checks may skip a card. Generation errors and skipped cards are reported
without blocking subtitle review. `Generate cards` retries pending candidates, while a
`generated_from` key keeps retries idempotent and preserves manual cards. Generated cards
enter the ordinary card workspace for user review before final rendering.

Implementation: [renderer](../app/services/renderer.py), [preview](../app/services/exact_preview.py),
[cards](../app/cards/), [card UI](../app/static/cards.js).

## 8. Storage, configuration, and deletion

- Default DB: `data/video_translator_v2.db`. The old `video_translator.db` is not migrated.
  Startup adds missing v2 fields for playback position, extra line gap, Chinese-only mode,
  and render cancellation.
- Project media/source/artifacts/render scratch: `data/projects/{project_id}/`.
  Published outputs: `output/{project_id}/`. CLI working files: `data/cli-workspace/`.
- Artifact snapshots and translation audit events support diagnosis, not perfect replay.
  Punctuation directories use Part numbers; later Pass audit directories use database Part IDs.
  Full rebuilds can leave earlier audit events beside later ones; selected rebuilds use run IDs.
  Inspect event timestamps, Token ranges, and metadata rather than assuming one directory is one run.
- `VIDEO_TRANSLATOR_DATA_DIR`, `VIDEO_TRANSLATOR_DB`, and `VIDEO_TRANSLATOR_OUTPUT_DIR`
  are independent settings. Changing data directory alone does not relocate the default DB.
  Prefer absolute custom paths; output-relative paths resolve against the repository root.
- Process environment takes precedence over `.env`. Saved project styles take precedence over
  fallback style values. New projects use the latest saved app style/default bilingual setting;
  `.env` supplies initial/fallback values. The initial line gap is 66, not a global fixed 50.
- `MAX_VIDEO_HEIGHT` is currently read but unused by project creation; the UI/API request
  controls download height (API default 1080). `TRANSLATION_PROVIDERS=manual` is unsupported.
- Python, prompts, and `.env` require an application restart. JS/CSS require a page reload;
  static responses have `Cache-Control: no-store`. The model catalog is cached in the API
  process after retrieval from the local CLI.

Project deletion requires confirmation and no queued/running job. It cancels an active
preview, validates the project UUID and directory containment, removes its project/output
directories, and deletes its database rows with cascading children, including cards.
This also applies to failed/no-subtitle projects. Shared model downloads, CLI caches/logs,
app defaults, and copies outside project directories are not deleted.

Deletion removes files before committing database deletion. A locked file or later directory
failure can leave database rows and a partially removed project directory; release the lock
and retry. SQLite rollback cannot restore removed files. There is no cross-filesystem/DB
atomic deletion guarantee.

Implementation: [configuration](../app/config.py), [deletion API](../app/main.py),
[schema](../app/db.py), [audit writer](../app/services/translation_audit.py).
