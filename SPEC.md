# Layla — Spec

## Prerequisites

Two kinds of dependency, deliberately kept separate.

**Python packages** — `pip3 install -r requirements.txt`

| Package | Pin | Used by |
|---|---|---|
| `youtube-transcript-api` | `==1.2.4` | `run.py` primary transcript source |

**System binaries** — installed outside pip, invoked as subprocesses

| Binary | Required? | Used by | Install |
|---|---|---|---|
| `yt-dlp` | Yes | `playlist.py`; `run.py` fallback transcript source | `brew install yt-dlp` |
| `ffmpeg` | Not yet | frame extraction (specified, unbuilt) | `brew install ffmpeg` |

`yt-dlp` is deliberately **not** in `requirements.txt` even though it is
pip-installable. Reasons:
- The code invokes it as a PATH binary, not an import. On this machine it
  resolves to `/opt/homebrew/bin/yt-dlp`; a pip copy's console script lands in
  `~/Library/Python/3.9/bin`, which is not on PATH, so the pin would describe a
  binary that never runs.
- YouTube changes break yt-dlp regularly. `brew upgrade yt-dlp` keeps it
  current; a pinned pip version goes stale and fails silently.

Version floor matters more than an exact pin — keep it recent. Verified against
yt-dlp 2026.06.09 and ffmpeg 8.1.1.

If `yt-dlp` is missing, `run.py`'s fallback returns
`{"ok": false, ... "yt-dlp not found on PATH"}` rather than crashing, and the
primary source still works on its own.

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
      agent.py            # summarize a transcript via headless omp subagent
      ingest.py           # one-call ingest pipeline
      index.py            # regenerate the store index
      data/               # gitignored local cache
        videos/
          INDEX.md
          <video_id>/
            summary.md
            transcript.md
            frames/       # later
```

| Layer | Has an LLM? | Examples |
|---|---|---|
| Tools | No | `playlist.py`, `run.py`, `index.py`, `ingest.py` |
| Layla + domain instructions | Yes (this session) | triage, discussion, deep dive |
| Summarizer subagent | Yes (same login) | `agent.py` → headless omp subagent, one per video |

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
One command — `python3 -m agents.youtube.ingest <playlist-url>`:
```
playlist url
  -> playlist.py             (video ids + title/channel/duration)
  -> skip ids already stored
  -> per new video: run.py (transcript.md) -> agent.py (summary.md)
  -> index.py                (INDEX.md)
  -> report to user
```
Videos run sequentially. Transcripts never enter the main session during
ingest — each summarizer subagent reads only its own transcript.

## Triage
Read `INDEX.md`, then `summary.md` for `status: new` videos. Rank against what
the user is working on now. Verdicts are computed at triage time, never frozen
into `summary.md`, because relevance changes week to week.

## Deep dive
Load that video's `transcript.md` into Layla's context. Timestamps let the user
ask about a specific moment. Pasted screenshots carry a visible player
timestamp, which Layla aligns with nearby transcript lines.

## Frames — designed, not built

### Workflow

Frames are fetched only for a video the user has picked for discussion, never
at ingest. Triage uses summary + transcript only. Pick a video -> extract
frames for that video alone -> discuss with transcript + relevant frames
aligned by timestamp. Unpicked videos: dropped from the shortlist; their
store entries stay.

### Pipeline (cheap stages first)

1. Sample one frame every 5s to disk.
2. Dedupe consecutive runs — hash the slide region only (ROI), never the
   whole frame.
3. Duration filter: slide held >= 10s (dedupe run length x 5s). Relevance
   router (cheap, local OCR): OCR words in the nearby transcript window ->
   send to LLM; no overlap + long hold -> send (silent reading slide); no
   overlap + short hold -> drop; charts (little/no text) -> send. Drops only
   obvious trash, never replaces the LLM's judgment.
4. LLM pass on survivors only: judge relevance + write the one-line
   description in one call. Includes a "no visual content — discard"
   verdict.

### Solved: hybrid layouts (speaker + slide)

Whole-frame hashing breaks when the speaker's face changes while the slide
stays static. Solution: detect the slide ROI — the part of the frame that
doesn't move — via temporal variance, hash only that. Speaker-only stretches
have no static region -> no ROI -> no frames.

### Solved: changing ROI mid-video

Layouts change (full-slide -> side-by-side -> picture-in-picture). Two
passes: (A) segment the video at persistent structural changes; (B) compute a
fresh ROI per segment. Over-segmentation is safe, under-segmentation is
fatal. Short segments fall back: neighbor ROI -> whole frame -> LLM decides.

### Cost

Extraction to disk is cheap; only loading frames into context costs. Each
image is ~1–1.5k tokens; OCR text is ~100 tokens. Text-only slides may route
through OCR-only description (deferred knob, v2).

### Decided

- Relevance: cheap OCR router + LLM judgment, in that order; the router drops
  only obvious trash.
- LLM verdicts include "no visual content — discard" (speaker-only frames).
- Calibrate the 10s + variance thresholds on `ow1we5PzK-o` before trusting
  them.
- Text-slide OCR-only routing: deferred to v2; measure v1 spend first.
- `frames.py` is its own tool; `agent.py` stays narrow.
- Duration estimate = dedupe run length x 5s (±5s accepted); exact durations
  later if stage 1 upgrades to slide-change detection.
- No-slides outcome: talking-head video -> "no frames", zero LLM calls.

The yt-dlp `--download-sections` flag syntax is unverified — the calibration
run doubles as that test.

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
