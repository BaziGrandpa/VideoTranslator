# Subtitle quality acceptance: September 16, 2026

Project: `c6220602-8850-4141-948d-cd7dfbeb226f`

Video: I Doubled My Dopamine Everyday For 7 Days (45:15)

The project has been regenerated and is waiting for human review. No final MP4 was rendered.
The requested provider/model remained Codex / `gpt-5.6-luna`, with low reasoning effort.
All 15 active windows completed; all 7,604 tokens have continuous segment coverage.
The saved token records and both part contexts are identical to the pre-run backup.
The separate `old dopamine` project still contains 798 segments.

## Comparable measurements

Both versions below were evaluated using the new shared Python rules.

| Metric | Before | After |
| --- | ---: | ---: |
| Segments | 659 | 855 |
| Average duration (seconds) | 4.106 | 3.164 |
| Duration above 6.5 seconds | 88 | 10 |
| Source text above 90 characters | 94 | 0 |
| Target text length violations | 27 | 0 |
| Target reading-speed violations | 7 | 2 |
| Very short content-heavy cues | 0 | 1 |
| Cues with any readability issue | 142 | 12 |
| All cues shorter than 0.7 seconds, including brief responses | 3 | 5 |

There are 843 machine-translated cues without readability flags and 12 cues marked
`needs_review`. Passing these checks does not certify semantic translation accuracy.

## Opening example

The previous cue covering tokens 17-41 lasted 7.52 seconds. It is now:

| Time (seconds) | English |
| --- | --- |
| 5.520-7.919 | Most people get little hits of dopamine throughout the day, |
| 7.919-9.825 | like your morning cup of coffee, a burger, |
| 9.825-11.679 | scrolling Tik Tok, |
| 11.679-13.040 | a shopping spree. |

All four pass the current readability checks. The raw music marker remains in the token
records and coverage but is omitted from the displayed English and Chinese text.

## Remaining review locations

Cue numbers are one-based; timestamps are milliseconds from the beginning of the video.

| Cue | Start | End | English | Issue |
| --- | ---: | ---: | --- | --- |
| 192 | 609360 | 617030 | Come on, bro. | Long duration |
| 279 | 894959 | 910389 | Let's do it. | Long duration |
| 284 | 923600 | 940629 | All right, we're about to do a highway merge. | Long duration |
| 322 | 1058240 | 1070789 | My inner child is thriving right now. | Long duration |
| 455 | 1504320 | 1512149 | It's literally culinary crack. | Long duration |
| 533 | 1733919 | 1746279 | Maybe this could be me. | Long duration |
| 550 | 1788880 | 1789430 | No, no, no. | Very short; high reading speed |
| 629 | 2020080 | 2021509 | So, I'm going to call my friend Patrick | High reading speed |
| 640 | 2045200 | 2054790 | I mean, I already booked your ticket, so I mean, if you can't go, I I I lose the money. | Long duration |
| 716 | 2264000 | 2280710 | Let's go. Last line. | Long duration |
| 728 | 2312160 | 2320550 | And we're going to win big. | Long duration |
| 770 | 2440400 | 2453190 | There's way too many lights going off right now. | Long duration |

## Verification and audit

- Automated checks: 53 passed, 2 skipped; Ruff and JavaScript syntax checks passed.
- The final `subtitle-cues.json` matches the database segments.
- Translation job `926f5155-1fbc-4982-8555-97f26565d1c3` completed successfully.
- During acceptance, repairs of unusually long individual tokens were found to create
  fragments without fixing timing. Version v1.1 protects those phrases for timing review and
  rejects equal-score fragmentation. The affected completed window was regenerated; the worker
  resumed the remaining windows. Earlier passing windows retain their v1 audit history.
- Requests, parsed provider responses, validation results and accepted cues are under
  `data/projects/c6220602-8850-4141-948d-cd7dfbeb226f/artifacts/translation-audit/`.
- The original project database records, including subtitles, tokens and contexts, are backed
  up in `data/projects/c6220602-8850-4141-948d-cd7dfbeb226f/artifacts/backups/before-quality-regeneration-20260916T084607940035Z.json`.

The quality rules are centralized in `app/services/subtitle_quality.py`. Readability flags are
displayed as `Check readability` in the workbench; hovering shows the individual reasons.
