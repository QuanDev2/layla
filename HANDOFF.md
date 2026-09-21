# Handoff — Layla

## Status

One agent, flat repo root, one SQLite store. Articles and YouTube videos both
go through the same capture → triage → retrieve path. Used for real: document 1
(Anthropic's "Scaling Managed Agents") was captured, triaged in conversation,
and closed `kept` — 12 snippets, all embedded, searchable.

Nothing is unbuilt except frame extraction, which is designed only.

Full decision log: `docs/decisions.md` (21 numbered decisions, do not
re-litigate). Architecture: `docs/system-overview.md`. Read those before
touching a module, not this file.

## What changed on 2026-09-20

- **Dropped `summarizer_agent.py`** (decision 7 rewritten as rejected). The
  workflow is one document at a time; bulk mode keeps source text out of the
  conversation, which is exactly what discussion needs.
- **Settled `judge.py`** as an offline audit, never a synchronous gate
  (decision 20). Zero code change — it is the behavior that shipped.
- **Fixed the doubled title** in chunk context prefixes. The document's own H1
  landed at `heading_path[0]`, so every prefix read `Title > Title > Section`.
- **Flattened the repo** (decision 21). `agents/knowledge/*.py` → root,
  `chunk.py` → `chunker.py`, `judge.py`/`eval.py` → `dev/`, docs → `docs/`.
  The YouTube domain folded into `youtube.py`; playlist listing, the summarizer
  subagent, the ingest pipeline, the index regenerator, and the markdown store
  were deleted.

## Conventions the user cares about

- **Never act without explicit approval.** Propose, then stop. Questions are
  not approval. Stated in `~/.omp/agent/AGENTS.md`, which is the authority.
- **Response format is specified, not a matter of taste.** Same file's "How you
  talk" / "What you say" sections: `##` headings only (the renderer prints
  `###` literally), flat numbering so claims can be cited back, a numbered line
  holds a bold lead and nothing else with detail in sub-bullets, three-to-five
  claims max, one `**Verdict:**` line, no emoji.
- **Diagrams carry numbered boxes** so they can be referenced by number later.
- **Docstrings:** imperative one-liner, then `In:` / `Out:` / `State:`
  fragments. No history, no rationale prose.

## Built

| Path | Does |
|---|---|
| `db.py` | Schema (`documents`, `snippets`, `snippets_fts`) + connection helper; FTS5 kept in sync by triggers |
| `ingest.py` | Captures already-fetched text into a `documents` row, dedupes by URL |
| `chunker.py` | Segments a document, proposes headings via a pluggable LLM backend, splits into ~1,800-char chunks with context prefixes |
| `heading_agent.py` | The heading-proposal call chunker.py wraps (omp \| anthropic \| off) |
| `triage.py` | Writes confirmed excerpts/synthesis/rejections/chunks into `snippets`, batch-embeds |
| `embed.py` | Voyage AI (`voyage-4`), float32 blob codec, `.env` loader |
| `search.py` | Hybrid FTS5 + cosine, fused by RRF, neighbor expansion; takes caller-extracted `terms` |
| `youtube.py` | Captions with two-source fallback + title/channel/duration via yt-dlp |
| `dev/judge.py`, `dev/eval.py` | Dev-only heading grading harness; nothing in the workflow calls them |

## Key decisions worth knowing before touching this

- **SQLite, not a vector database.** Brute-force cosine in numpy beats an ANN
  index at low-thousands scale — benchmarked ~13ms/~60MB at 5,000 rows.
  Revisit (`sqlite-vec`) only past tens of thousands of rows.
- **Voyage `voyage-4`.** `voyage-3.x` lost free-tier access under current
  pricing — never use it. Key in `.env` (gitignored), loaded by a stdlib parser
  in `embed.py`.
- **A failed/missing embed is never fatal.** Rows are written with
  `embedding=NULL` rather than blocking — still keyword-searchable.
- **Temporal boost was cut** after design review: real query phrasing doesn't
  include date language.
- **Nothing silently discarded-then-reachable.** Neighbor expansion never
  re-reads `raw_text`, so a discarded chunk can't leak back through a neighbor.
- **Keyword terms come from the caller.** FTS5 ANDs bare terms, so sentence
  queries silently ran `vector_only`. Layla passes content words as
  `search(..., terms=[...])`; `_fts_ranked_ids` tries AND, falls back to OR.

## Verified

Every module was verified against real fixtures and the real Voyage API. On
2026-09-20, post-flatten: existing document 1 still searches `backend: hybrid`;
`chunker.py` CLI produces 31 chunks on the article fixture with prefixes 25
chars shorter than pre-fix; and a full video round-trip ran end to end —
`youtube.py` fetched real captions plus title/channel, capture → chunk →
`write_chunks` embedded 1/1, and the snippet came back through hybrid search.

## Next steps

- None outstanding. Frame extraction is the only designed-but-unbuilt work:
  plan and proposed schema in `docs/youtube.md`.

## Open questions

- Whether a frame's snippet stores OCR text verbatim, and whether frames get
  their own `chunk_index` ordering or stay `NULL` like syntheses.
