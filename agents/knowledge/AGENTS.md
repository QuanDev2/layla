# Knowledge domain

Read this when the user shares something to learn from — article, link,
pasted text, transcript — or asks what was already learned ("remind me
what we learned about X"). Design/decision log:
`agents/knowledge/pipeline/PLAN.md` — read it before changing any module;
never re-litigate its decisions.

## Tools

| Tool | Purpose |
|---|---|
| `python3 -m agents.knowledge.chunk <path> --kind article\|transcript [--title T] [--channel C] [--no-llm] [--table]` | Split a document into candidate snippets with context prefixes. `--table` prints the confirm table; `--no-llm` skips heading proposal (deterministic split, instant) |
| `ingest.capture(conn, text, source_type, url=None, title=None)` | Create a `documents` row from text already in hand. Dedupes by url — a known url returns the existing `document_id` with `duplicate: True` and writes nothing (dedupe is url-only; pasted text never dedupes) |
| `ingest.set_summary(conn, document_id, summary)` | Record the discussion summary once the conversation concludes |
| `triage.write_excerpt(conn, document_id, content, tags=None)` | Store one confirmed verbatim excerpt |
| `triage.write_synthesis(conn, document_id, content, tags=None)` | Store a Q&A-derived answer from the triage conversation |
| `triage.write_rejection(conn, document_id, reason, tags=None)` | Store the judgment behind a rejected claim — the reason, never the claim text |
| `triage.write_chunks(conn, document_id, chunks, tags=None)` | Store every chunk from a "keep the whole document" call; takes chunk.py's `chunks` list, one batched embed |
| `triage.set_status(conn, document_id, status)` / `discard_document(conn, document_id)` | Close the document out: `kept` / `partial` / `discarded` |
| `search.search(conn, query, limit=10)` | Hybrid keyword + meaning search over snippets, fused ranking, neighbor context, source metadata attached |

`source_type`: `url` | `pasted` | `file` | `transcript`. Snippet `kind`:
`excerpt` | `synthesis` | `rejection`.

## Invocation pattern

Only chunk.py is a CLI. Everything else is a library call through one
pattern, run from the repo root:

```bash
python3 -c "
from agents.knowledge import db, ingest
c = db.connect()
print(ingest.capture(c, open('/tmp/knowledge-in.md').read(), 'url',
                     url='https://example.com/post', title='Post title'))
"
```

- Never paste long text inline in the one-liner — write it to a temp
  file first, then `open(<path>).read()` as shown.
- Each one-liner is its own process; every write commits inside the
  call, so state persists between invocations.
- Every function returns a JSON dict and never raises — print it, check
  `ok` before continuing.
- `db.connect()` applies the schema and creates the database on first
  use. Pass `':memory:'` for a throwaway (checks, demos).

## Storage

`agents/knowledge/data/knowledge.db` — gitignored, single SQLite file.

- `documents` — archive + provenance: url, title, source_type,
  ingested_at, raw_text, agent_summary, status. `raw_text` is stored
  pristine and never searched.
- `snippets` — the only retrievable unit: content, kind, chunk_index,
  free-form tags, embedding. FTS index kept in sync by triggers.
- Document `status`: `pending` from capture until triage closes it —
  `kept` (everything worth keeping written), `partial` (some kept),
  `discarded` (nothing kept; raw_text retained for provenance).

## Workflow: learn a document

1. Get the text. For a url, fetch it yourself with the read tool —
   ingest.py never fetches. Pasted text: use as given. A YouTube
   transcript: read `agents/youtube/data/videos/<id>/transcript.md`;
   title and channel live in the sibling `summary.md`, not the
   transcript.
2. Capture with `ingest.capture()` and the right `source_type`. On
   `duplicate: True`, stop — the document is already known; read its
   summary and status, ask the user how to proceed. Never re-triage
   silently.
3. Give the gist. Read the full text into your own context, then report
   three or four numbered claims — no more. Layout: heading with the
   document title, italic title line (`source · date · size · document
   N`), rule, blockquote holding the one-line thesis, the numbered
   claims, a single `**Verdict:**` line on relevance, rule, closing
   question. Never open with a section-by-section breakdown; that is one
   possible answer in step 4, not part of this step.
4. Triage by conversation. The user asks questions, you answer from the
   text in your context, in the same layout as step 3 — a detail answer
   looks like a small gist. Never delegate this; the keep/discard call
   needs the live conversation (decision 1). Offer a `synthesis` write
   when an answer here is worth keeping on its own.
5. The user decides: discard, or keep and name which parts. Never
   pre-empt this — proposing candidate keeps is fine, selecting is not.
6. Write the confirmed snippets per "Workflow: triage writes" below,
   then close: `ingest.set_summary()` with a few sentences, then
   `triage.set_status('kept' | 'partial')` — or `discard_document()`
   when nothing was worth keeping.

## Workflow: triage writes

One rule across all writes: propose the exact content, show it, wait
for confirmation, then write. Never auto-select text.

| User says | Write |
|---|---|
| "keep this part" | `write_excerpt` — the exact text shown back |
| "keep the whole thing" | Run chunk.py with `--table` first; on confirm `write_chunks` with the result's `chunks` list |
| rejects a claim | Offer `write_rejection` — content is the judgment ("X doesn't hold because…"), never the rejected text itself |
| asks something whose answer is worth keeping | Offer `write_synthesis` — your synthesized answer, not source text |

- "Keep the whole document" never means one giant snippet — always the
  chunked split (decision 13).
- Tags: free-form, the user's words; a list or comma-string both work.
- A failed embed is never fatal: the row is written with `embedding
  NULL`, fully keyword-searchable, backfilled later. Report the
  `embed_error`; don't retry silently.

## Workflow: search

"Remind me what we learned about X" → `search.search(conn, query)`.

- Results carry snippet content, kind, source title/url/date, and
  adjacent-chunk context. Synthesize the answer yourself and cite the
  source — don't dump raw rows.
- `backend` reports what actually contributed: `hybrid`, `fts_only`,
  `vector_only`, or `none`. `none` with zero results means nothing
  matched — say so; don't guess around it.
- A hit with `kind: rejection` is a recorded disagreement — never read
  it back as an endorsement.

## Notes

- Env: `KNOWLEDGE_LLM` = `omp` (default) | `anthropic` | `off` selects
  chunk.py's heading backend. `KNOWLEDGE_EMBED` = `voyage` (default) |
  `off` (no API call, keyword search still works). `VOYAGE_API_KEY`
  lives in the repo-root `.env`, loaded by embed.py — never read it
  into conversation.
- Never search or chunk from `documents.raw_text` directly; snippets are
  the only retrievable unit.
- Never delegate single-article triage or search to a subagent. Bulk
  import (summarizer_agent.py) is the one delegated path — not built
  yet; handle articles one at a time.
- `heading_agent.py` is wrapped by chunk.py, never called directly.
  `judge.py` / `eval.py` are a dev-only grading harness; no workflow
  here calls them.
- chunk.py's LLM pass is one call over the whole document — up to a few
  minutes on a long transcript. `--no-llm` is instant with degraded
  boundaries.
- Verified working (2026-09-09): every one-liner pattern above —
  capture/dedupe, excerpt + status writes under `KNOWLEDGE_EMBED=off`,
  chunk CLI `--table`, keep-whole `write_chunks` → FTS search chain,
  live Voyage embed (voyage-4), and honest `backend: none` on an empty
  corpus.
