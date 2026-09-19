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
7. After approval, translate each complete Part (Pass 1), map that Chinese onto Python-cut
   English cues (Pass 2), and split Chinese above 24 display units (Pass 3), with Grok Build
   fallback.
8. Checkpoint completed Parts in SQLite so interrupted or quota-limited runs resume in place.
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

Python first divides each Part into fixed `source_sentence_id` ranges using English punctuation,
speaker changes, and word-level pauses. Short comma/colon/semicolon fragments merge right; short
complete sentences normally merge left. A 1,200 ms pause prevents merging. Known stage directions
and speaker markers retain their Token IDs but are hidden from visible subtitles.

Pass 2 makes one call for the complete Part. It receives ordered IDs and clean English, the full
Pass 1 translation, and project context, but no word-level Token IDs or timestamps. It places the
Pass 1 Chinese back onto those English cues. Adjacent cues must stay one unfolding thought:
concatenating their Chinese should recover Pass 1, without extra, missing, or repeated facts.
Python rejects missing, duplicate, reordered, or empty ID rows.

Python sends only Chinese parents above 24 display units to Pass 3. If Chinese punctuation can
split a parent, Python does that first and Pass 3-1 only fills `end_token_id`. Otherwise Pass 3-2
splits the Chinese and assigns ends. Prefer 18 display units when already splitting; 24 is the hard
max. Python infers every start, requires strictly increasing ends and exact parent coverage, then
stamps length, duration, and reading-speed flags once on the Part's final cues. Pass 3 does not
perform semantic or terminology auditing.

The review workbench lists cues that are over 24 units, shorter than 700 ms, longer than 5000 ms,
faster than 12 units/s, or that Pass 3 failed to split. There is no severity and no Apply
suggestion path.

Generation, context, Part-level review requests, parsed provider responses, validation errors and
final results are recorded under each project's `artifacts/translation-audit/`. Records include
the prompt version, requested model and provider (CLI-default models are not inferred).

The browser Rebuild dialog supports two modes. Full rebuild preserves the existing destructive
behavior and clears the old subtitle pipeline state immediately. Selected Windows reuses the
existing Timed Tokens and legacy Part/Window selection envelopes. A project must complete one new
full rebuild before Selected Windows is available. Python expands the selection to complete
intersecting source sentences. Pass 2 reads the complete Part and existing unselected Chinese but
returns only the selected IDs; Pass 3 splits only selected long parents. Existing subtitles stay
visible and read-only until all affected Parts are atomically published. A revision guard prevents
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
