# Layla — Spec

## Repo layout
```
layla/
  AGENTS.md               # Layla's identity + domain registry + mode split
  GOALS.md
  SPEC.md
  requirements.txt
  agents/
    youtube/
      AGENTS.md           # YouTube domain instructions + workflows
      playlist.py         # list playlist/video metadata (yt-dlp)
      run.py              # fetch timestamped transcript (2 sources, fallback)
      index.py            # regenerate the store index
  data/                   # gitignored local cache
    videos/
      INDEX.md
      <video_id>/
        summary.md
        transcript.md
        frames/           # later
```

## Three layers
| Layer | Has an LLM? | Examples |
|---|---|---|
| Tools | No | `playlist.py`, `run.py`, `index.py` |
| Layla + domain instructions | Yes (this session) | triage, discussion, deep dive |
| Ingest subagents | Yes (same login) | one per video, summarize transcript |

"YouTube agent" means Layla wearing `agents/youtube/AGENTS.md` — not a separate
process.

## Tool contract
Every tool module exposes:
- `NAME: str`
- `DESCRIPTION: str`
- `INPUT_SCHEMA: dict`
- `invoke(**kwargs) -> dict` — returns `{"ok": bool, ...}`, never raises for
  expected failures
- a `main()` CLI printing JSON to stdout, exit 0 on success / 1 on failure

## Storage
Markdown on the filesystem. No database.

Rationale: volume is tiny (hundreds of videos ≈ tens of MB), Layla's `grep`,
`glob`, and `read` tools already query files with zero code, and files are
inspectable and editable by hand. Markdown stays the source of truth, so
batch-embedding into pgvector later is a script, not a migration.

Postgres earns its place at structured queries over hundreds of videos;
pgvector at semantic search. At small N, Layla reasoning over `INDEX.md` beats
vector matching.

## Ingest pipeline
```
playlist url
  -> playlist.py            (video ids + title/channel/duration)
  -> skip ids already stored
  -> run.py --out-dir       (transcript.md per video)
  -> subagent per video      (summary.md per video, parallel)
  -> index.py               (INDEX.md)
  -> report to user
```
Transcripts never enter the main session during ingest — that's the whole
reason subagents are used here.

## Triage
Read `INDEX.md`, then `summary.md` for `status: new` videos. Rank against what
the user is working on now. Verdicts are computed at triage time, never frozen
into `summary.md`, because relevance changes week to week.

## Deep dive
Load that video's `transcript.md` into Layla's context. Timestamps let the user
ask about a specific moment. Pasted screenshots carry a visible player
timestamp, which Layla aligns with nearby transcript lines.

## Frames — planned, not built
Strategy is **lazy extraction**: don't pre-extract. The transcript flags where
visuals matter ("as you can see in this diagram" at `[14:22]`), so fetch only
that moment:
```
yt-dlp --download-sections "*14:20-14:25" -f 'bv[height<=720]' <url>
ffmpeg -ss 00:14:22 -i clip.mp4 -frames:v 1 frames/00-14-22.png
```
For unattended batch ingest, where no one can guide it: interval sampling plus
perceptual-hash dedupe (one tunable threshold) rather than ffmpeg scene
detection, which trips on camera cuts and gestures in talking-head footage.

Cost note: each image is ~1–1.5k tokens, so 50 slides ≈ 60k tokens. Text-only
slides should be OCR'd (~100 tokens); only charts and diagrams need pixels.

Flag syntax above is unverified — needs a live test.

## Transcript sources
`run.py` tries `youtube-transcript-api` first, then falls back to `yt-dlp`
subtitles (WebVTT, parsed into the same segment shape). The two fail
independently: transient IP blocks hit the API while yt-dlp keeps working, which
was observed during development. The result reports `source` and, when it fell
back, `fallback_from`.

Neither source needs auth or an API key. A video with no published captions
fails both — a real outcome, reported as a structured error.

Parser guarantees:
- One segment per line; caption line breaks normalized away.
- Rolling auto-captions collapsed into the final sentence of each group,
  keeping the group's first start time.

## Verified
`playlist.py` against a real channel playlist (3 entries, metadata resolved);
both transcript sources against real videos; the fallback path; the
no-captions failure; `index.py` regeneration; the ingest subagent step.
Frame extraction is specified but unbuilt.
