# Handoff — Layla

## Status
- **YouTube**: ingest pipeline built and verified against real videos. Frame
  extraction is specified but unbuilt. Not yet run against your real playlist.
- **Knowledge**: core pipeline built end-to-end — capture, chunk, embed,
  triage, search — and verified against real fixtures and the real Voyage
  API. `agents/knowledge/AGENTS.md` is written and committed — **usable from
  a live conversation now**. Not yet exercised in a real session (only smoke
  tests so far). `summarizer_agent.py` (bulk import) is the one unbuilt
  piece left in the domain.
  Full decision log (19+ numbered decisions) lives in
  `agents/knowledge/pipeline/PLAN.md` — read that before touching the domain,
  not this file.

## Cross-domain architecture (applies to every domain)
- **Layla is the orchestrator** — the interactive session, not a service.
- **Domains are folders**, each with its own `AGENTS.md` + tools. Layla loads
  only the domain in play, so her baseline stays flat as domains grow.
- **Mode split decides structure per task:** discussion keeps material in
  Layla's context; verdict work delegates to subagents. Storing full source
  alongside summaries means delegation loses nothing — summaries drive triage,
  full text stays on disk/DB for deep dives.
- **"X agent" = Layla wearing `agents/<domain>/AGENTS.md`**, not a process.
- **On-demand, not scheduled.** No cron anywhere. Unattended runs would need a
  paid API key for any reasoning step; session-triggered work rides your
  existing login and costs nothing since results are only read when you sit
  down.
- **Tools are plumbing.** Plain scripts, no LLM calls of their own, except the
  explicitly-named agent wrappers (`agent.py`, `heading_agent.py`,
  `summarizer_agent.py`) that exist specifically to make one.
- **Credentials: tools hold capabilities, Layla holds none.** Secrets go from
  keychain/env/`.env` into the tool process, never into conversation. Narrow
  provider scopes; confirm before mutating.

## YouTube domain

### Decisions made
- No database. Markdown in `agents/youtube/data/videos/` (gitignored).
  Postgres/pgvector only when cross-video semantic search actually matters.
- Playlists must be public or unlisted — avoids OAuth entirely.
- Worth-watching verdicts are computed at triage time, never frozen into
  `summary.md`, because relevance changes week to week.
- Email deferred — verdict work, will use subagent delegation, once you pick
  a provider and auth.

### Built
| Path | Does |
|---|---|
| `agents/youtube/playlist.py` | List playlist/video metadata via yt-dlp |
| `agents/youtube/run.py` | Timestamped transcript, two sources with fallback |
| `agents/youtube/agent.py` | Summarize one transcript via headless omp subagent |
| `agents/youtube/ingest.py` | One-call ingest pipeline: transcript, summarize, reindex |
| `agents/youtube/index.py` | Regenerate `INDEX.md` from summary frontmatter |
| `agents/youtube/AGENTS.md` | Domain workflows: ingest, triage, deep dive |

### Verified against real videos
- Playlist listing on a real channel playlist; channel falls back to the
  playlist owner, which flat mode populates.
- Both transcript sources, plus the fallback path with the primary forced to fail.
- Video with no captions fails both sources with a readable error — correct
  behavior, not a bug.
- Two caption artifacts found by testing and fixed: hard line breaks splitting
  a timestamp across lines, and repeated cues (exact duplicates from the API,
  rolling restatements from auto-captions). One dedupe now covers both sources.
- Ingest subagents wrote contract-conforming summaries; index regenerated.

### Next steps
1. Run ingest on your real playlist; confirm the summaries are useful to triage.
2. Lazy frame extraction — `yt-dlp --download-sections` + single-frame ffmpeg
   grab at timestamps the transcript flags. Flag syntax still unverified.
3. Only if context pain shows up: OCR text-only slides instead of shipping
   images (~100 tokens vs ~1.5k).
4. Email domain, once you pick provider and auth.

### Open questions
- Which playlist is the real one to ingest from?
- Batch frame extraction strategy if unattended ingest ever matters — interval
  sampling plus perceptual-hash dedupe is the current plan, untested.

## Knowledge domain

Full design/decision log: `agents/knowledge/pipeline/PLAN.md`. Summary only below.

### Built — the whole core pipeline
| Path | Does |
|---|---|
| `db.py` | Schema (`documents`, `snippets`, `snippets_fts`) + connection helper; FTS5 kept in sync by triggers |
| `chunk.py` | Segments a document, proposes headings via a pluggable LLM backend, splits into ~1,800-char candidate chunks with context prefixes |
| `heading_agent.py` | The heading-proposal call chunk.py wraps (omp \| anthropic \| off backend) |
| `embed.py` | Voyage AI provider (`voyage-4`), float32 blob codec, `.env` loader |
| `ingest.py` | Captures already-fetched text into a `documents` row, dedupes by URL |
| `triage.py` | Writes confirmed excerpts/synthesis/rejections into `snippets`, batch-embeds |
| `search.py` | Hybrid search: FTS5 (BM25) + cosine, fused by Reciprocal Rank Fusion, neighbor expansion |
| `judge.py` / `eval.py` | Dev-only grading harness for `heading_agent.py` — not production, not in the original plan |

### Not built
- `summarizer_agent.py` — bulk-import bootstrapped subagent (many articles
  at once). Single-article flow (`ingest.py`/`triage.py` called directly in
  conversation) is complete and does not need this to work.

### Key decisions worth knowing before touching this domain
- **SQLite, not a vector database.** Brute-force cosine in numpy beats an ANN
  index at "low thousands of snippets" scale — benchmarked: ~13ms/~60MB at
  5,000 rows, real numbers not estimates. Revisit (`sqlite-vec`) only past
  tens of thousands of rows.
- **Voyage AI (`voyage-4`) for embeddings.** Free tier. `voyage-3.x` lost free-tier
  access under current pricing — never use it. Key lives in `.env`
  (gitignored), loaded by a small stdlib parser in `embed.py`, no `python-dotenv`
  dependency.
- **A failed/missing embed is never fatal.** Every write path stores the row
  with `embedding=NULL` rather than blocking — still fully keyword-searchable,
  backfilled later.
- **Temporal boost was cut** after design review — real query phrasing doesn't
  include date language, so the originally-specced phrase-detection boost
  would have guarded a case that doesn't occur.
- **Nothing is ever silently discarded-then-reachable.** Read-time neighbor
  expansion (search.py) never re-reads `raw_text`, so a chunk you discarded in
  triage can never leak back into a search result through its neighbors.

### Verified
Every file above was verified against real fixtures, and the last three
(`embed.py`, `triage.py`, `search.py`) against the real Voyage API, not just
mocks — full ingest → chunk → embed → triage → search chain confirmed
end-to-end, including correct discrimination between unrelated documents in
a real hybrid search.

### Next steps
1. Actually use it: capture a real article/link and triage it end-to-end in
   a live conversation — everything so far is fixture/smoke-test verified,
   not exercised for real.
2. `summarizer_agent.py` — bulk-import subagent, once single-article flow is
   proven in real use.
3. Decide `judge.py`'s role: synchronous quality gate on chunking, or offline
   audit only. Nothing calls it today.

### Open questions
- None blocking. `summarizer_agent.py`'s exact trigger (how many articles at
  once counts as "bulk") is worth settling before building it, not before now.
