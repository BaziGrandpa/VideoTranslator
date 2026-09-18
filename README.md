# Video Translator

A local-first workbench for translating and burning subtitles into YouTube videos.

## MVP workflow

1. Paste a public YouTube video URL.
2. Download the video at up to 1080p and retrieve its English subtitles.
3. Reconstruct a canonical word-level timeline from rolling YouTube captions.
4. Use WhisperX word alignment when no English subtitles are available.
5. Split long videos into balanced, natural-boundary parts of about 30 minutes.
6. Generate project-wide translation guidance and pause for the user to review its overview,
   style, and preferred terminology.
7. After approval, let the local Codex CLI translate each complete Part and create timed
   1,000-token subtitle windows, with Grok Build fallback.
8. Save every completed window to SQLite so interrupted or quota-limited runs resume in place.
9. Normalize display text and validate exact token coverage, timing, duration, and reading speed.
10. Review both languages and adjust subtitle timing in the browser.
11. Confirm the timeline, then use FFmpeg to add gradient masks and burn styled ASS subtitles.

The sidebar provider selector supports `Auto: Codex → Grok`, `Codex only`, and `Grok only`.
The selection is stored per project and is locked while that project is processing. When Codex is
available in the provider path, its model can be selected from the live catalog returned by the
local Codex CLI. `CLI configured default` uses the model from `.env`.

## Requirements

- Python 3.11–3.13 (3.12 recommended)
- FFmpeg and ffprobe; FFmpeg must include `libass`
- Node.js 22+ for yt-dlp's YouTube JavaScript challenge support
- An installed and authenticated Codex CLI as the primary translator
- Optional: an installed and authenticated Grok Build CLI (`grok`) as fallback
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
CODEX_MODEL=gpt-5.6-terra
GROK_MODEL=
CODEX_REASONING_EFFORT=low
SUBTITLE_PLANNER_WINDOW_TOKENS=1000
```

Use manual translation mode when only the review workbench is needed:

```dotenv
TRANSLATION_PROVIDERS=manual
```

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

## Data layout

```text
data/
├─ video_translator.db
└─ projects/{project_id}/
   ├─ metadata.json
   ├─ source/
   └─ render/
output/
└─ {project_id}/
   ├─ final-v001.mp4
   ├─ english-v001.srt
   ├─ chinese-v001.srt
   └─ final-v002.mp4
```

SQLite stores projects, jobs, immutable timed tokens, subtitle parts, translation contexts,
resumable batches, editable subtitle cues, and file paths. Video files stay in their project
directories. Videos up to 40 minutes use one part; longer videos use balanced 20–40 minute parts
centered around 30 minutes.

`Rebuild subtitles` requires browser confirmation. After confirmation, the API synchronously
deletes the old Pass 0 context, timed tokens, Parts, Batches, cues, and review issues before it
queues the new Pass 0–3 run. The downloaded video, source captions, project settings, and subtitle
style are retained.

Subtitle rendering is independently configurable by language. These `.env` values are the
defaults used when a project is created (and the fallback for legacy projects):

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
English subtitles` switches the project between Chinese-only and bilingual preview/render output;
new projects default to bilingual output.

Use `Preview` independently when final visual accuracy matters. It pauses the video and
replaces it with a PNG rendered using the same ASS and filter settings as the final MP4. Clicking the
left or right half moves to the previous or next subtitle midpoint, and selecting a subtitle renders
that cue. Saved style changes refresh the PNG only while exact preview is active. The button becomes
`Exit preview` while active; exiting removes its temporary images and returns to the browser timing
preview.

Subtitle text edits are saved to SQLite only when Enter is pressed or the field loses focus. While
exact FFmpeg preview is active, a successful text save refreshes the current PNG without moving
focus during ordinary preview navigation. The workbench displays `Unsaved`, `Saving...`, and
`Saved`; closing or reloading with unfinished saves triggers the browser's unsaved-change warning.

Every render receives the next available version number, so rendering a project again never
overwrites an earlier MP4. The project page points to the newest completed version.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
node --check app/static/app.js
```

## Current scope

Subtitle generation uses shared rules in `app/services/subtitle_quality.py` for prompt guidance
and Python validation. Cue boundaries prioritize natural Chinese information units; English cue
text may be a clause or other incomplete fragment as long as Token coverage and translation
alignment remain exact. Chinese targets normally stay at or below 18 display units. The 19-24
range is a rare phrase-integrity exception, capped at roughly 2% of Cues; above 24 is unacceptable.

Pass 2's primary job is Chinese-first segmentation. It makes one generation call per approximately
1,000-Token Window, receives `[token_id,text]` pairs with no timing data, and has no repair loop.
Invalid Token coverage fails that Window. After all Windows in a Part are saved, Pass 3 reviews the
complete Part in one CLI call. Pass 3 has exactly three issue types: `too_long`,
`semantic_mismatch`, and `terminology_inconsistency`. A structurally valid `too_long` replacement
is auto-applied; empty or invalid replacements remain visible for human review. Semantic and
terminology issues are marked without automatic rewriting. Python alone computes duration and
reading-speed flags after Pass 3 and exposes them in the workbench.

Generation, context, Part-level review requests, parsed provider responses, validation errors and
final results are recorded under each project's `artifacts/translation-audit/`. Records include
the prompt version, requested model and provider (CLI-default models are not inferred).

To test one saved Window through Pass 2 and Pass 3 without writing SQLite:

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_review_window `
  --project-id PROJECT_UUID --part 1 --window 1
```

The report and raw audit records are written under `artifacts/targeted-window-tests/`.

The browser Rebuild dialog supports two modes. Full rebuild preserves the existing destructive
behavior and clears the old subtitle pipeline state immediately. Selected Windows reuses the
existing Timed Tokens and immutable Part/Window Token envelopes. It reruns Pass 0 for the whole
video, pauses for Pass 0 approval, reruns Pass 1 once per affected Part, Pass 2 once per selected
Window, and Pass 3 once per affected Part. Pass 3 receives the complete Part as context but Python
allows issues and replacements only inside the selected Token ranges; replacements may never cross
a Window boundary. Existing subtitles stay visible and read-only until every selected result is
ready, then all affected Parts are published in one database transaction. A revision guard prevents
manual edits made during processing from being overwritten.

To regenerate an idle project's subtitles while retaining its tokens and part contexts:

```powershell
.\.venv\Scripts\python.exe -m scripts.regenerate_subtitles PROJECT_UUID
```

This backs up the project's database records under `artifacts/backups/`, resets its active
translation batches and queues a translation job. The worker must be running to process it.


- Each project handles one source video, not a playlist.
- Only YouTube URLs are accepted.
- Automatic translation defaults from English to Chinese and uses subscription CLIs, not API keys.
- The browser provides real-time timing previews; FFmpeg/libass generates the final image.
- Bilibili upload and thumbnail generation are not included yet.
