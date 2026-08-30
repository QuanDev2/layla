# YouTube domain

Read this when the request involves YouTube videos: ingesting a playlist,
triaging what's worth watching, or discussing a specific video.

## Tools (plumbing, no LLM)

| Tool | Purpose |
|---|---|
| `python3 -m agents.youtube.playlist <url> [--limit N]` | List video ids + metadata for a playlist or single video |
| `python3 -m agents.youtube.run <url> [--out-dir data/videos]` | Fetch timestamped transcript; writes `transcript.md` with `--out-dir` |
| `python3 -m agents.youtube.index [--root data/videos]` | Regenerate `INDEX.md` from summary frontmatter |

All print JSON to stdout, exit 0 on success, 1 on failure.

`run.py` has two transcript sources and tries them in order:
`youtube-transcript-api` first (structured, fast), then `yt-dlp` subtitles as a
fallback. The result's `source` field says which one produced it, and
`fallback_from` records why the first failed. This matters because the two
sources fail independently — transient IP blocks hit the API while yt-dlp keeps
working.

A video with no published captions fails both. That is a real outcome, not a
bug: report it and move on.

## Store layout

```
data/videos/
  INDEX.md                  # generated; one row per video
  <video_id>/
    summary.md              # frontmatter + summary + key timestamps
    transcript.md           # full timestamped text
    frames/                 # extracted stills (later)
```

Split is deliberate: triage reads `INDEX.md` and `summary.md` only. Never load
`transcript.md` during triage — a 30-video sweep would drag ~450k tokens into
context.

## Workflow: ingest a playlist

1. `playlist.py <playlist-url>` → entries with `video_id`, `title`, `channel`, `duration`.
2. Skip any `video_id` that already has `data/videos/<id>/summary.md`.
3. For each new video: `run.py <url> --out-dir data/videos` → writes `transcript.md`.
4. Fan out **one subagent per video, in parallel** (single `task` batch). Each
   subagent reads only its own `transcript.md` and writes `summary.md`.
   Transcripts must not enter the main session context.
5. `index.py` → regenerate `INDEX.md`.
6. Report to the user: count ingested, titles, anything that failed.

Subagent instructions must specify: read `data/videos/<id>/transcript.md`, write
`data/videos/<id>/summary.md` in the format below, and do nothing else — no
formatters, no tests, no other files.

## summary.md format

```markdown
---
video_id: jNQXAC9IVRw
url: https://www.youtube.com/watch?v=jNQXAC9IVRw
title: Me at the zoo
channel: jawed
duration: 0:19
added: 2026-08-30
status: new
---

## Summary
Two to four sentences. Neutral description of what the video covers.

## Key points
- [02:14] Point, with the timestamp it occurs at.
- [14:22] Another point.

## Visual references
- [14:22] Diagram of the orchestration layers.
- [31:05] Benchmark chart.
```

`status`: `new` | `watched` | `skipped`.

**Never write a worth-watching verdict into `summary.md`.** Whether a video is
worth the user's time depends on what they're working on this week. Store a
neutral summary; apply judgment at triage, when their current context is known.

"Visual references" lists moments where the speaker points at something the
transcript alone can't convey. That list drives frame extraction later.

## Workflow: triage ("what's worth watching?")

1. Read `data/videos/INDEX.md`.
2. Read `summary.md` for videos with `status: new`.
3. Rank against what the user is currently working on — ask if unclear.
4. Report: title, one-line pitch, why it matters to them now, and a skip
   recommendation where relevant.
5. Update `status` when the user says they watched or skipped something.

## Workflow: discuss one video

Load that video's full `transcript.md` into context — the whole point of storing
it. Timestamps let the user ask about a specific moment ("what did they say at
12:34?").

When the user pastes a screenshot, read the timestamp visible in the player UI
and align it with the transcript lines around it.

Do **not** delegate this to a subagent. Discussion needs full detail and
knowledge of the user's setup; a summary handed back through a subagent loses
both.

## Notes

- Playlists must be **public or unlisted**. Private playlists need OAuth, which
  this domain deliberately avoids.
- No auth and no API key for either transcript source.
- Transcript `[MM:SS] text` lines are one segment per line, always. Caption line
  breaks are normalized away so a timestamp is never split across lines.
- Rolling auto-captions restate the previous cue plus new words; the parser
  collapses each rolling group into its final sentence, keeping the group's
  first start time.
- Verified working: `playlist.py` on a real channel playlist, both transcript
  sources on real videos, the fallback path, and the no-captions failure.
