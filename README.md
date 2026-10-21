# Video Translator

A local-first workbench for translating and burning subtitles into YouTube videos.

## Documentation and review

Read this page for setup, then [Current behavior](docs/current-behavior.md) for the
implemented pipeline, review rules, editing, rendering, and storage contracts.
[Audit: October 15, 2026](docs/audit-2026-10-15.md) lists confirmed gaps and decisions
that remain open. [HANDOFF](HANDOFF.md) is the maintenance entry point.

The current-behavior document describes implementation, including its limits; prompt
instructions are not guarantees of translation accuracy. Historical acceptance reports
and channel editorial material under `product/` are not current application specifications.

## Workflow

1. Paste a public YouTube video URL.
2. Download at the selected height limit (1080p by default; UI also offers 720p/2160p)
   and retrieve the video's English subtitles.
3. Reconstruct a canonical word-level timeline from rolling YouTube captions.
4. Use WhisperX word alignment when no English subtitles are available.
5. Split long videos into balanced, natural-boundary parts of about 30 minutes.
6. Before Pass 0, restore punctuation once for each Part whose original English tokens contain
   zero supported punctuation marks. Store token-ID annotations without changing source words
   or timestamps; Parts that already have punctuation skip this model request.
7. Generate project-wide translation guidance and pause for the user to review its overview,
   style, and preferred terminology.
8. After approval, translate each complete Part (Pass 1), map that Chinese onto Python-cut
   English cues (Pass 2), and attempt to split Chinese above 24 display units (Pass 3).
   Failed splits remain reviewable. Provider fallback depends on the selected mode and Pass.
9. Checkpoint completed Parts in SQLite so interrupted or quota-limited runs resume in place.
10. Normalize display text and validate exact token coverage, timing, duration, and reading speed.
11. Review both languages and adjust subtitle timing in the browser.
12. Confirm the timeline, then use FFmpeg to add gradient masks and burn styled ASS subtitles.

## Manual commentary cards

Each project has a separate **Commentary cards** workspace beside Subtitle review. Add a card at
the player's current time, then edit its Chinese term, English name, explanation, start/end times,
fade durations, left/center/right position, width, font, text colors and sizes, padding, and translucent
background. Cards appear along the top of the actual video picture. The initial
position is right and width is 32% of the video; the most recently saved card style becomes the
default for new cards across projects. Fade-in and fade-out default to 0.8 seconds each. Cards
cannot overlap, and text that would exceed the safe picture area is rejected with an explanation
instead of being cut off.

The player shows cards while video plays. **Preview** renders the selected video time with FFmpeg,
including whichever subtitles and cards are active at that instant; no subtitle selection is
required. The card editor can preview unsaved style changes. Saving a card marks an existing final
video as outdated. **Render new version** burns both subtitles and cards into the next numbered
video file. Cards remain in the project when subtitles are rebuilt and never trigger translation.

Pass 2 Chinese punctuation is retained in SQLite and in editable subtitle text. The browser
preview, exact preview, ASS render, and exported Chinese SRT remove trailing commas and periods
and dash marks from displayed Segments, including before closing quotes/brackets. Question marks,
exclamation marks, and semicolons remain. Length and reading-speed checks use this displayed text.

The sidebar provider selector supports `Auto: Codex → Grok`, `Codex only`, and `Grok only`.
The selection is stored per project and is locked while that project is processing. When Codex is
available in the provider path, its model can be selected from the catalog returned by the
local Codex CLI and cached by the API process. `CLI configured default` uses `CODEX_MODEL`
from the environment/`.env`, or the CLI default when that value is blank.

## Requirements

- Python 3.11–3.13 (3.12 recommended)
- FFmpeg and ffprobe; FFmpeg must include `libass`
- Node.js 22+ for yt-dlp's YouTube JavaScript challenge support
- At least one installed and authenticated translation CLI: Codex or Grok Build (`grok`)
- For the default Auto fallback order, authenticate both (Codex → Grok)
- Optional: an NVIDIA GPU and WhisperX

Check FFmpeg subtitle support:

```powershell
ffmpeg -filters | Select-String subtitles
```

## Installation

```powershell
.\scripts\setup.ps1
Copy-Item .env.example .env
```

No API key is required. Authenticate the subscription CLIs first:

```powershell
codex login
grok login
```

The default translation order is shown below. Leave model names blank to use the CLI defaults:

```dotenv
TRANSLATION_PROVIDERS=codex,grok
CODEX_MODEL=
GROK_MODEL=
CODEX_REASONING_EFFORT=high
```

There is no `manual` translation provider. Existing projects can be reviewed without starting
a translation job; creating or rebuilding translations requires a supported authenticated CLI.

Install the optional transcription fallback when videos may not contain subtitles:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[asr]"
```

WhisperX installs large PyTorch dependencies and downloads models on first use, so it remains an
optional dependency.

## Start

Double-click `start-video-translator.bat`. It starts the API and pipeline worker, then opens the
browser after the service becomes ready. The first run installs the environment automatically.

Double-click `stop-video-translator.bat` to stop the API and its pipeline worker without using Task
Manager.

To start from a terminal instead:

```powershell
.\.venv\Scripts\python.exe -m app.launcher
```

Open <http://127.0.0.1:6866>.

## Pass 0 translation guidance

After the global context pass completes, the project pauses before translating any Part. The
Translation guidance panel lets you edit the project overview, translation style, preferred term
translations, categories, and mandatory-use flags. Save a draft to keep the project paused, or
choose `Save and continue translation` to approve the guidance and queue Pass 1. The approved
SQLite value is used by every later translation pass.
Pass 0 asks for the overview and style in Simplified Chinese in both direct and long-video
consolidation requests. Term sources keep their original English spelling. Existing saved
guidance is not rewritten; edit it in this panel or rebuild Pass 0 to regenerate it.
Pass 0 also suggests up to 40 separate commentary card terms. Each keeps the original
subtitle phrase for timing and a corrected English display term. Candidates whose original
phrase cannot be found in source Tokens are dropped individually. At the Pass 0 checkpoint,
reviewers can edit the display term or remove candidates before continuing.

## Pass responsibilities and LLM call budget

Each model request must belong to a named Pass with a defined input and output. Fix a failure
in the Pass that owns it: improve that Pass's prompt, input contract, or validation instead of
adding an unplanned repair call elsewhere. Do not make an extra LLM call to fill an empty Cue,
retry a long sentence with different instructions, or silently patch a rejected response.

| Stage | Responsibility | Planned model requests |
| --- | --- | --- |
| Punctuation Pass | Before Pass 0, add punctuation after specific original Token IDs in a Part whose original English has zero `, . ? ! : ;` marks. Store the marks separately; never edit Token text or timing. | One request per eligible Part, with no provider fallback or repair call. |
| Pass 0 | Produce the project overview, style, terminology, and card term candidates for user approval. | One request for up to 12,000 tokens; for longer videos, one per 6,000-token chunk plus one consolidation request. |
| Pass 1 | Translate one complete Part into continuous Chinese. | One request per Part. |
| Python Cue cutting | Divide the English Timed Tokens into fixed Cues using explicit punctuation, speaker, pause, and length rules. | None. |
| Pass 2 | Assign the existing Pass 1 Chinese to every ordered Cue without retranslation or omission. | One request per Part. |
| Pass 3-1 | Assign token boundaries to long Cues already split at Chinese punctuation by Python. | At most one request per Part when needed. |
| Pass 3-2 | Split other long Chinese Cues and assign their token boundaries. | At most one request per Part when needed. |
| Python validation | Check IDs, nonempty Chinese, source coverage, boundaries, timing, and review flags. | None. |
| Card Pass | After final subtitle validation, write a Chinese name and brief background explanation for approved card terms using the complete English Part. | One request per Part containing ungenerated card candidates. |

Card Pass does not choose timestamps. Python locates the first source-term occurrence in
immutable Tokens, maps it to a Cue, and creates a five-second card using the most recently
saved card style. Overlaps shift later cards to the next available slot. Existing cards are
preserved, and a repeated run does not duplicate generated cards. An invalid or missing
card is skipped and reported without blocking subtitle review; the card workspace can retry
generation. Generated cards still need visual review before rendering.

These counts describe the planned requests for one run. The Punctuation Pass uses only the
first selected provider. For later Passes, `Auto: Codex → Grok` may attempt the next configured
provider after a failed request; this is provider fallback for the same Pass, not permission
to add a new repair step. A user-started rebuild is a new run. Any deliberate change to the
request budget must be documented here alongside its trigger and owning Pass.

The Punctuation Pass prompt includes both the complete Part transcript and its indexed Token
list. Its response contains only `after_token_id` and one punctuation mark per selected Token.
Python validates every annotation before committing the whole Part. A failed or interrupted
request reports its Part and cause, stops before Pass 0, and is not automatically sent again.
The stored marks form a punctuated reading view for all later Passes and English subtitles;
the immutable `tokens` rows retain their original words and timestamps. A full subtitle
rebuild clears this stage and enters the new flow.

Pass 2's no-retranslation/no-omission requirement is a prompt instruction. Python checks
Cue IDs, order, nonempty text, and Token coverage; it does not prove that the Chinese is
identical to Pass 1. Pass 2 is allowed to adjust Chinese by product design; verbatim
preservation is not a hard requirement. Pass 3-2 also lacks a Chinese text-conservation check. See the
[Pass contracts](docs/current-behavior.md#2-pipeline-and-model-call-contracts) for fallback boundaries.

Python must apply only explicit, deterministic rules to model output. It may cut source Cues,
split at Chinese punctuation, calculate timing and flags, and reject invalid results. It must
not invent, paraphrase, redistribute, or silently fill Chinese text to make a Pass appear to
succeed. Mechanical boundary estimates are recorded in Sub-cue provenance and audit files;
successful proportional estimates have no dedicated review suggestion today. They do not
establish semantic alignment. If Pass 2 returns an empty `target_text`, reject that provider's
output; Auto may try the next provider for the same Pass. Do not add a repair call or Python
translation to fill the missing Cue. Apply the same ownership rule to other Passes.

## Data layout

```text
data/
├─ video_translator_v2.db
├─ cli-workspace/
└─ projects/{project_id}/
   ├─ metadata.json
   ├─ source/
   ├─ artifacts/
   └─ render/
output/
└─ {project_id}/
   ├─ final-v001.mp4
   ├─ english-v001.srt
   ├─ chinese-v001.srt
   └─ final-v002.mp4
```

SQLite stores projects, jobs, immutable Tokens, Parts, punctuation marks, translation
contexts, Cues, optional Sub-cues, final Segments, commentary cards, saved defaults, and their
provenance links. Video files stay in their project directories. A Token timeline up to 40
minutes uses one Part; longer timelines target balanced 20–40 minute Parts centered around 30
minutes. Sparse sources and available split boundaries can prevent those target durations.
A Part is the unit for the Punctuation Pass and each Pass 1, Pass 2, and Pass 3 model call.

Each project in the sidebar has a Delete button beside Rename. Confirming deletion removes its
database records, downloaded files, artifacts, and versioned files in `output/{project_id}`.
Projects with queued or running jobs cannot be deleted; renders can be cancelled first.
Shared caches/settings remain. Filesystem removal and database deletion are not one atomic
transaction; see [cleanup limits](docs/current-behavior.md#8-storage-configuration-and-deletion).

`Rebuild subtitles` requires browser confirmation. Full rebuild synchronously deletes the old
Pass 0 context, timed tokens, Parts, Cues, and Segments before it queues source reconstruction,
punctuation preparation, and Pass 0–3. Skip Pass 0 keeps the existing guidance and its approval
state; unapproved guidance still needs approval. The downloaded video, source captions, project
settings, subtitle style, playback position, cards, and existing output files are retained.
The project's output link is reset. Selected Parts keeps approved guidance and current subtitle
rows until the chosen Parts finish Pass 1–3, then publishes them together. It reuses existing
Cue boundaries and punctuation; it does not re-cut English Cues.

Subtitle rendering is independently configurable by language. A new project copies the style
from the most recent `Save style`, including whether English subtitles are shown. These `.env`
values are used only before any style has been saved, and as the fallback for a project with
no stored style:

```dotenv
CHINESE_SUBTITLE_FONT=Microsoft YaHei
CHINESE_SUBTITLE_SIZE=46
CHINESE_SUBTITLE_COLOR=#FFFFFF
CHINESE_SUBTITLE_OUTLINE=3
ENGLISH_SUBTITLE_FONT=Arial
ENGLISH_SUBTITLE_SIZE=28
ENGLISH_SUBTITLE_COLOR=#D8D8D8
ENGLISH_SUBTITLE_OUTLINE=2
SUBTITLE_BOTTOM_MARGIN=46
SUBTITLE_LINE_GAP=66
SUBTITLE_HORIZONTAL_MARGIN=90
BOTTOM_MASK_HEIGHT=0.34
BOTTOM_MASK_OPACITY=0.72
```

Open `Subtitle style` in the project toolbar to change sizes, colors, outlines, margins, line spacing, and
the bottom mask without restarting the application. The panel no longer changes the player mode:
`Save style` stores the project values and refreshes the lightweight CSS preview. Pressing Enter in
any style input performs the same save. `Copy` stores every visible style value in the local
workbench clipboard, while `Paste` applies and immediately saves it to another project. `Show
English subtitles` switches the project between Chinese-only and bilingual preview/render output.
New projects copy the most recently saved English-subtitle setting; before the first save, they
default to bilingual output.

Use `Preview` independently when final visual accuracy matters. It pauses the video and
replaces it with a PNG rendered using the same ASS and filter settings as the final MP4. Clicking the
left or right half moves to the previous or next subtitle midpoint, and selecting a subtitle renders
that Segment. Saved style changes refresh the PNG only while exact preview is active. The button becomes
`Exit preview` while active; exiting removes its temporary images and returns to the browser timing
preview.

Subtitle text edits are saved to SQLite only when Enter is pressed or the field loses focus.
Shift+Enter inserts a manual line break in Chinese text; browser preview, exact preview,
Chinese SRT, and final video preserve it. Automatically generated subtitles still normalize
layout whitespace. While
exact FFmpeg preview is active, a successful text save refreshes the current PNG without moving
focus during ordinary preview navigation. The workbench displays `Unsaved`, `Saving...`, and
`Saved`; closing or reloading with unfinished saves triggers the browser's unsaved-change warning.

Per-Segment `Line gap +30` adds 30 to the project's current line gap in bilingual mode.
Per-Segment `Chinese only` hides that Segment's English subtitle and puts its Chinese text
at the base bottom margin. The stored English text stays available for later editing; an
empty English field is allowed for rendering only while `Chinese only` is checked. English
SRT export omits these Segments. The source video's own embedded text is unaffected.
`Merge next` requires confirmation and permits adjacent Segments in the same Part, including
a whole Cue followed by a Sub-cue. `Scroll to current subtitle` positions the list immediately.
`Split`, to the right of `Merge with next`, cuts a Segment at its time midpoint. The first
half keeps both texts; the second starts with empty English and Chinese fields for manual
editing. Both halves stay in the original Part, and empty text blocks final rendering.
Video position is saved every five wall-clock seconds while the workspace is ready, even when
paused; reopening restores that time and selects the covering Segment, if there is one.

There are seven review suggestion types. They are advisory: marking a Segment Reviewed does
not dismiss a matching rule, and rendering does not require an empty suggestion list. See the
[exact review rules](docs/current-behavior.md#4-review-suggestions).

Rendering uses H.264/AAC MP4. `RENDER_VIDEO_ENCODER` accepts `libx264` (default), `h264_qsv`,
or `h264_nvenc`; the GPU choices accelerate encoding, while subtitle/mask compositing remains
on the CPU. Unsupported hardware fails without automatic CPU fallback. `Cancel render`
requests cancellation and removes the unfinished output when the worker handles it.

A successful render publishes the next version after existing nonempty `final-v*.mp4` files;
failed/cancelled attempts can reuse an uncompleted version number. Earlier completed MP4s remain.
The project page points to its latest published output.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_renderer_integration.py
.\.venv\Scripts\python.exe -m ruff check .
node --check app/static/app.js
node --check app/static/cards.js
```

To also exercise real FFmpeg with small generated videos, run
`.\.venv\Scripts\python.exe -m pytest -q tests/test_renderer_integration.py`.
Tests simulate translation providers and must not make live translation requests.

## Domain model and storage

| Concept | Identity and purpose | Storage |
| --- | --- | --- |
| Token | Original English word and time anchor; `token_id` starts at 0 per project. | `tokens` |
| Part | Processing range; `part_number` starts at 1 per project. | `parts` |
| Cue | Python-cut English range; `cue_id` starts at 1 inside a Part. Pass 2 stores Chinese here. | `cues` |
| Sub-cue | Optional validated Pass 3 split; `subcue_id` starts at 1 inside a Cue. | `subcues` |
| Segment | Final editable on-screen subtitle; `segment_order` starts at 1 per project. | `segments` |

`segment_components` links each Segment to one or more Cues or Sub-cues. A whole Cue
has a component with a null `subcue_id`. The same Segment can contain several
Sub-cues or several whole Cues after final merging. The Sub-cue rows remain stored
before final merging, so a bad split can be inspected. Cues also record split status
and errors. Segment text retains its final punctuation in SQLite. Display and render
strip trailing punctuation according to the subtitle display rules.

Pass 1 writes the full Part `target_text` and controls natural Chinese length and
punctuation across the Part. The final Chinese subtitle is the primary product;
English Tokens provide the timing coordinates. Pass 2 assigns Chinese text to every
fixed Cue and writes those rows to `cues`. Pass 3-1 assigns Token boundaries to
Chinese pieces already split at punctuation. Pass 3-2 splits the other long Chinese
Cues and assigns boundaries. Python validates and stores the pre-merge Sub-cues,
then stores final Segments and their component links. Final Segments need not match
source Cues one to one. Manual edits change Segments, not Tokens or Cues.
Manual Split keeps the original Cue/Sub-cue links on both halves, but clears their exact
Segment Token ranges because the midpoint is not a Token boundary. Selected-Part rebuild
uses those source links to validate coverage and regenerates the Part from its Cues.

Pass 0 writes `translation_contexts`; the Punctuation Pass writes `part_punctuation`
and `punctuation_marks`. The old `data/video_translator.db` is deliberately not
migrated. The default database is `data/video_translator_v2.db`. Existing v2 databases receive
additive migrations for playback position, per-Segment line gap and Chinese-only mode,
and render cancellation.

The browser Rebuild dialog supports a full rebuild or selected Parts. Selected Parts
keep approved Pass 0 guidance and current stored Segments while Pass 1–3 run;
the UI locks subtitle editing and may hide the workspace during processing.
After all selected Parts pass validation, their Cues, Sub-cues, and Segments publish
together under a revision guard.

- Each project handles one source video, not a playlist.
- Only YouTube URLs are accepted.
- Translation uses authenticated local CLIs, not API keys.
- The browser previews timing; FFmpeg/libass generates the final image.
