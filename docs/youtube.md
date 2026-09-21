# YouTube support

*captions today · frames designed, unbuilt · last updated 2026-09-20*

`youtube.py` exists so a video can enter the knowledge pipeline exactly like an
article: fetch captions, capture, chunk, triage, embed, search. It is the only
module in the repo that touches YouTube.

---

## 1. Prerequisites

Two kinds of dependency, deliberately kept separate.

**Python packages** — `pip3 install -r requirements.txt`

| Package | Pin | Used by |
|---|---|---|
| `youtube-transcript-api` | `==1.2.4` | primary caption source |
| `numpy` | `==2.0.2` | cosine search in `search.py` |

**System binaries** — installed outside pip, invoked as subprocesses

| Binary | Required? | Used by | Install |
|---|---|---|---|
| `yt-dlp` | Yes | `video_metadata()`; caption fallback | `brew install yt-dlp` |
| `ffmpeg` | Not yet | frame extraction (designed, unbuilt) | `brew install ffmpeg` |

`yt-dlp` is deliberately **not** in `requirements.txt` even though it is
pip-installable:

- The code invokes it as a PATH binary, not an import. A pip copy's console
  script lands in `~/Library/Python/3.9/bin`, which is not on PATH, so the pin
  would describe a binary that never runs.
- YouTube changes break yt-dlp regularly. `brew upgrade yt-dlp` keeps it
  current; a pinned pip version goes stale and fails silently.

Version floor matters more than an exact pin. Verified against yt-dlp
2026.06.09 and ffmpeg 8.1.1.

---

## 2. Caption sources

`youtube.py` tries `youtube-transcript-api` first, then falls back to `yt-dlp`
subtitles (WebVTT, parsed into the same segment shape). The two fail
independently: transient IP blocks hit the API while yt-dlp keeps working,
observed during development. The result reports `source` and, when it fell
back, `fallback_from`.

Neither source needs auth or an API key. A video with no published captions
fails both — a real outcome, reported as a structured error, not a bug.

Parser guarantees:

- One segment per line; caption line breaks normalized away, so a `[MM:SS]`
  marker is never split across lines.
- Rolling auto-captions collapsed into the final sentence of each group,
  keeping the group's first start time.
- Exact repeated cues deduped. One dedupe policy covers both sources.

Metadata (`title`, `channel`, `duration`) comes from a separate `yt-dlp
--flat-playlist --dump-json` call and is **best-effort**: if yt-dlp is missing
or fails, captions still return, with `metadata_error` set and the three fields
empty. The chunk prefix then degrades to `[00:00–02:00] > Section`.

---

## 3. Chapters as chunk boundaries

`video_chapters()` reads the creator's published chapters through a second
`yt-dlp --skip-download --dump-json --playlist-items 1` call. `--flat-playlist`
cannot serve here: it omits `chapters` entirely, which is why metadata and
chapters are two calls rather than one. `invoke()` returns them as
`chapters: [{title, start, end}]`, seconds, empty when the creator published
none, with `chapters_error` set if yt-dlp failed.

`chunker.chunk_transcript(..., chapters=[...])` then splits on them:

- **Chapter starts are fixed level-2 points.** Each maps to the first caption
  segment at or after its `start_time`, so a boundary never lands mid-segment.
  The heading model cannot move, merge, or replace one.
- **The model's only jobs are naming and subdividing.** It writes one level-3
  subheading at every chapter's first block, and extra ones inside a chapter
  whose body exceeds `TARGET_CHARS`. A creator's "Button test" becomes
  `Button test > Metal buttons outlast plastic ones` — a subject label plus a
  retrievable claim.
- **Compliance is checked, not hoped for.** `_chapter_complaints()` names every
  chapter left unlabeled or every long chapter left undivided; the message
  feeds the existing single retry.
- **Degradation is stepwise.** Model unusable → chapters alone carry the split
  (`backend: chapters`). No chapters → today's whole-document heading pass,
  unchanged.

Prefix and backend label say which path ran: `chapters+omp` for the full path,
`chapters` for chapter-only, `omp`/`fallback` for a chapterless video.

---


## 4. What was deliberately dropped

The repo previously carried a YouTube "domain" with playlist ingest, per-video
summarizer subagents, a markdown store under `data/videos/`, and an `INDEX.md`
triage view. All of it was deleted in the 2026-09-20 flatten (decision 21):

- Playlist triage — "which of these 30 videos is worth watching" — was never
  run against a real playlist in two weeks of use.
- The markdown store duplicated text that `knowledge.db` already holds, so a
  video ingested both ways existed twice.
- The summarizer subagent was the last consumer of the discussion/verdict mode
  split, which had already lost its other instance (decision 7).

What survived is the part that was hard: the two-source fetch with fallback,
the caption dedupe, and the line-break normalization — all found by running
against real videos, not by review.

---

## 5. Frames — designed, not built

Screenshots are a future goal: slides, charts, and diagrams a transcript cannot
convey. Nothing below is implemented.

### Workflow

Frames are fetched only for a video the user has picked for discussion, never
at capture time. Pick a video → extract frames for that video alone → discuss
with transcript and relevant frames aligned by timestamp.

### Pipeline (cheap stages first)

1. Sample one frame every 5s to disk.
2. Dedupe consecutive runs — hash the slide region only (ROI), never the whole
   frame.
3. Duration filter: slide held >= 10s (dedupe run length x 5s). Relevance
   router (cheap, local OCR): OCR words overlap the nearby transcript window →
   send to LLM; no overlap + long hold → send (silent reading slide); no
   overlap + short hold → drop; charts (little/no text) → send. Drops only
   obvious trash, never replaces the LLM's judgment.
4. LLM pass on survivors only: judge relevance and write the one-line
   description in one call. Includes a "no visual content — discard" verdict.

### Solved: hybrid layouts (speaker + slide)

Whole-frame hashing breaks when the speaker's face changes while the slide
stays static. Solution: detect the slide ROI — the part of the frame that
doesn't move — via temporal variance, and hash only that. Speaker-only
stretches have no static region, so no ROI and no frames.

### Solved: changing ROI mid-video

Layouts change (full-slide → side-by-side → picture-in-picture). Two passes:
(A) segment the video at persistent structural changes; (B) compute a fresh ROI
per segment. Over-segmentation is safe, under-segmentation is fatal. Short
segments fall back: neighbor ROI → whole frame → LLM decides.

### Cost

Extraction to disk is cheap; only loading frames into context costs. Each image
is ~1–1.5k tokens; OCR text is ~100 tokens. Text-only slides may route through
OCR-only description (deferred knob, v2).

### Decided

- Relevance: cheap OCR router, then LLM judgment, in that order.
- LLM verdicts include "no visual content — discard" (speaker-only frames).
- Calibrate the 10s and variance thresholds on a real slide-heavy video before
  trusting them.
- Text-slide OCR-only routing: deferred to v2; measure v1 spend first.
- `frames.py` is its own tool.
- Duration estimate = dedupe run length x 5s (±5s accepted).
- No-slides outcome: talking-head video → "no frames", zero LLM calls.
- The yt-dlp `--download-sections` flag syntax is unverified — the calibration
  run doubles as that test.

### Schema when frames arrive

Frames need a home in `knowledge.db` without breaking decision 13 (`snippets`
is the only retrievable unit). The shape that preserves it:

```sql
CREATE TABLE assets (
  id            INTEGER PRIMARY KEY,
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  kind          TEXT NOT NULL,      -- 'frame' for now
  path          TEXT NOT NULL,      -- data/frames/<video_id>/<seconds>.jpg
  stamp         TEXT,               -- '[12:34]', aligns with transcript units
  stamp_seconds REAL,
  width         INTEGER,
  height        INTEGER,
  sha256        TEXT,               -- dedupe across re-extraction runs
  created_at    TEXT NOT NULL
);
```

Then one column on `snippets`:

```sql
ALTER TABLE snippets ADD COLUMN asset_id INTEGER REFERENCES assets(id);
```

Why this and not an image column or a parallel search path:

- **Image bytes stay on disk; the row holds a path.** Not for the reason you
  might assume — SQLite keeps large BLOBs in overflow pages, so a frame column
  would not slow down `SELECT id, embedding` scans. Measured reality is that
  SQLite beats the filesystem for ~10KB blobs and loses somewhere between
  250KiB and 1MiB (sqlite.org/fasterthanfs.html, and the Jim Gray paper it
  cites); a 720p JPEG frame at ~100–300KB sits on that crossover, so
  performance does not decide it. The operational arguments do: a vision model
  is handed a *path*, so a BLOB would have to be exported to a temp file on
  every look; `rsync`/Time Machine move only changed frames instead of
  rewriting a multi-GB database file; and the database stays small enough to
  copy around. The cost accepted in exchange is orphan risk — a deleted row
  leaves a file behind, and a deleted file leaves a dangling path. `sha256`
  plus a periodic sweep is the answer, not a schema change.
- **A frame becomes searchable by becoming a snippet.** The LLM's one-line
  description (and OCR text, if kept) is written as a snippet with
  `kind='frame'` and `asset_id` pointing at the image. It embeds and indexes
  through the existing path — no second retrieval mechanism, no change to RRF
  fusion.
- **`kind` stays the polarity/type switch it already is.** `excerpt`,
  `synthesis`, `rejection`, `frame` — a search hit tells you what it is, and a
  frame hit carries a path the agent can read as an image.
- **Provenance survives deletion.** Dropping a frame's snippet leaves the
  `assets` row, so re-extraction can skip work it already did (`sha256`), the
  same way `raw_text` survives a discarded document.
- **`stamp_seconds` makes alignment a join, not a parse.** Transcript chunks
  already carry `start`/`end` stamps, so "show me the slide he was on at 14:22"
  is a range query, not string matching.

## 6. Open options — decide when frames are actually built

Both of these are recorded as options, not decisions. Neither should be
settled on paper; settle them against real extracted frames.

### OCR text in the frame snippet

The description is a retrieval ceiling: words the one-liner omits are not
indexed, so a search for a component name written inside a diagram finds
nothing. Appending the frame's OCR text to the snippet `content` would index
every word on the slide.

| Option | Buys | Costs |
|---|---|---|
| Description only | Clean, high-signal snippets; ~100 tokens per hit | Anything not in the one-liner is unfindable |
| Description + OCR verbatim | Every on-slide word is keyword-searchable | Slide boilerplate ("Confidential", footer names, page numbers) matches constantly; dilutes the embedding |
| Description + filtered OCR | Most of the recall, less noise | Needs a filter that is itself a judgment call — boilerplate detection across decks |
| OCR in a separate column, FTS-indexed but not embedded | Keyword recall without polluting the vector | `snippets_fts` is external-content over `content`; needs a second indexed column and a trigger change |

Leaning: the fourth, because it separates the two indexes' jobs — but it is
the only one that touches the schema, so it needs real frames to justify.

### Ordering

Whether frames get their own `chunk_index` sequence (so neighbor expansion can
return the slide before and after) or stay `NULL` like syntheses. `chunk_index`
currently means "position in document order" for transcript chunks; frames
share the timeline but not the sequence, so reusing the column may overload it.
`stamp_seconds` on `assets` may make ordering a join rather than a column.
