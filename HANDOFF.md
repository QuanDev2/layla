# Handoff — Layla

## Status
- **YouTube**: ingest pipeline built and verified against real videos. Frame
  extraction is specified but unbuilt. Not yet run against your real playlist.
- **Knowledge**: complete and **used for real** — document 1 (Anthropic's
  "Scaling Managed Agents") was captured, triaged in conversation, and
  closed as `kept`: 12 snippets, all embedded, searchable. Hybrid search
  was found broken during that run and fixed the same day. Nothing is
  unbuilt; `summarizer_agent.py` was dropped (decision 7).
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
  explicitly-named agent wrappers (`agent.py`, `heading_agent.py`) that
  exist specifically to make one.
- **Credentials: tools hold capabilities, Layla holds none.** Secrets go from
  keychain/env/`.env` into the tool process, never into conversation. Narrow
  provider scopes; confirm before mutating.

## Conventions the user cares about
- **Never act without explicit approval.** Propose, then stop. Questions are
  not approval. Stated in `~/.omp/agent/AGENTS.md`, which is the authority.
- **Response format is specified, not a matter of taste.** Same file's "How
  you talk" / "What you say" sections: `##` headings only (the renderer
  prints `###` literally), flat numbering so claims can be cited back, a
  numbered line holds a bold lead and nothing else with detail in
  sub-bullets, three-to-five claims max, one `**Verdict:**` line, no emoji.
- **That file is versioned** at `~/.config/agent/AGENTS.md` (symlinked from
  `~/.omp/agent/`) in the private `QuanDev2/dotconfig` repo. Note that SSH
  port 22 is blocked on this machine — pushes need
  `GIT_SSH_COMMAND='ssh -p 443 -o Hostname=ssh.github.com'`.
- **Docstrings:** imperative one-liner, then `In:` / `Out:` / `State:`
  fragments. No history, no rationale prose.

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

Full design/decision log: `agents/knowledge/pipeline/PLAN.md`. Architecture
and data flow: `agents/knowledge/system-overview.md`. Summary only below.

### Built — the whole core pipeline
| Path | Does |
|---|---|
| `db.py` | Schema (`documents`, `snippets`, `snippets_fts`) + connection helper; FTS5 kept in sync by triggers |
| `chunk.py` | Segments a document, proposes headings via a pluggable LLM backend, splits into ~1,800-char candidate chunks with context prefixes |
| `heading_agent.py` | The heading-proposal call chunk.py wraps (omp \| anthropic \| off backend) |
| `embed.py` | Voyage AI provider (`voyage-4`), float32 blob codec, `.env` loader |
| `ingest.py` | Captures already-fetched text into a `documents` row, dedupes by URL |
| `triage.py` | Writes confirmed excerpts/synthesis/rejections into `snippets`, batch-embeds |
| `search.py` | Hybrid search: FTS5 (BM25) + cosine, fused by Reciprocal Rank Fusion, neighbor expansion. Takes caller-extracted `terms` for the keyword side |
| `judge.py` / `eval.py` | Dev-only grading harness for `heading_agent.py` — not production, not in the original plan |

### Not built, and won't be
- `summarizer_agent.py` — bulk-import subagent, dropped 2026-09-20 with
  decision 7. The workflow is one article at a time, chosen deliberately;
  bulk mode keeps source text out of Layla's context, which is exactly what
  single-article discussion needs, so it would be dead code. Revisit only if
  a real backlog dump ever arrives.

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
- **Keyword terms come from the caller, not a stopword list.** FTS5 ANDs bare
  terms, so a sentence-shaped query demanded its function words too and
  returned nothing — every real query silently ran `vector_only`. Layla holds
  the question, so she passes content words as `search(..., terms=[...])`;
  `_fts_ranked_ids` tries AND, falls back to OR. Rejected alternatives: a
  hardcoded stopword frozenset (measured — BM25's IDF does not neutralize
  stopwords at 12-snippet scale, so the list would be load-bearing and need
  maintaining) and a per-query LLM call (500ms-2s on the interactive read
  path, and it makes search fail when the network does).

### Verified
Every file was verified against real fixtures and the real Voyage API. On
2026-09-20 the domain was exercised for real end-to-end: document 1 captured,
discussed, 10 chunks + 2 syntheses written on confirmation, closed `kept`,
then searched back. All 12 snippets embedded, zero embed errors. Post-fix,
sentence queries report `backend: hybrid`; a query whose terms are absent
from the corpus still reports `vector_only`, which is correct, not a
regression.

Prefix fix (2026-09-20) verified on real fixtures: exact-match title,
near-miss title, no title (H1 becomes the title), and a document with no
H1 all produce the right prefix; `train-llm-from-scratch.md` still splits
into 31 chunks with each prefix 25 chars shorter, and chunk bodies are
text-identical to the source.

### Next steps
- None. The domain is complete for the single-article workflow it serves.
  Document 1's 12 snippets still carry the old doubled-title prefix; they
  are searchable and correct, so re-chunking them is optional cleanup, not
  a fix.

### Open questions
- None.
