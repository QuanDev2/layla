---
status: design
updated: 2026-09-02
schema: drafted
scripts: not-started
---

# Knowledge domain — design log

Continuous-learning system: feed in articles, triage them conversationally,
keep what's worth keeping, retrieve it later ("remind me what we learned
about X," "apply the article from last week to what we're building"). This
file is the "what we decided and why," written as the decisions were made —
read it before re-litigating anything below.

## Confirmed decisions (do not re-litigate)

1. **Domain, not a standalone agent.** Lives at `agents/knowledge/`, same
   shape as `agents/youtube/`. Single-article ingest/triage and all
   retrieval happen in Layla's own context (Discussion mode) — both need the
   live conversation ("what are we building") that a separate blank agent
   wouldn't have. See decision 7 for the one place a bootstrapped subagent
   *does* fit.
2. **Storage: SQLite only.** No markdown mirror of curated snippets. Single
   file, agent-mediated, no sync-drift risk. Export to markdown on request
   is a query, not a storage decision, if ever needed.
3. **Retrieval: hybrid.** FTS5 keyword search (built into `sqlite3`, zero
   new dependency) + vector similarity, fused. Add a temporal boost for
   date-language queries ("last week," "recently") — borrowed from Mem0's
   design, directly relevant given the stated use case.
4. **Vector backend: brute-force cosine in numpy, not an ANN index.** At
   personal scale (low thousands of snippets) a full scan is a single
   matrix-vector multiply, sub-millisecond. `sqlite-vec` or similar is a
   scale problem this system doesn't have — revisit only if the corpus
   reaches tens of thousands of snippets.
5. **Embedding provider: Voyage AI for the PoC.** Free tier (200M tokens ×
   several accounts) removes the billing objection for now. Every embedded
   row records `embedding_model` — vectors from different models/providers
   aren't comparable, so switching providers later is an explicit re-embed
   job, never a silent correctness bug.
6. **Local fallback (post-PoC swap target): EmbeddingGemma-300M, Q8_0.**
   Not fp32 — quality delta vs. full precision is 0.23 MTEB points on
   English v2 (68.36 → 68.13), noise-level, at roughly 4x the disk/RAM of
   Q8. Not Q4 either — Q4's extra savings over Q8 aren't worth its slightly
   larger quality gap (0.45 pts) on hardware with no real RAM constraint.
   `fastembed`'s default (`bge-small-en-v1.5`) was rejected in favor of this
   because `bge-small` caps at 512 input tokens, forcing aggressive chunking
   of whole articles; EmbeddingGemma runs 2,048.
7. **Bulk import gets a bootstrapped subagent; single-article triage does
   not.** Mirrors `agents/youtube/agent.py` exactly: a blank `omp -p`
   subprocess per item, `--tools read,write` only (never `task` — must not
   spawn further subagents), `--no-session`, fixed output contract. Used
   only when many articles arrive at once and summarizing article #7 doesn't
   need to know about article #3 or the current conversation. It produces a
   *candidate* summary only — the keep/discard call always happens in
   Layla's own context, never inside the bootstrapped subagent.
8. **No automatic entity extraction (Mem0-style).** Mem0's entity signal
   compensates for two things this corpus doesn't have: atomic
   one-line auto-extracted facts, and pronoun-heavy conversational text.
   Curated multi-sentence excerpts already have low coreference ambiguity,
   and FTS5 (literal terms) + vector search (concepts) + tags (deliberate
   clustering) already cover what entities would add. Additive later if a
   genuine multi-hop/intersection query need shows up — not needed for v1.
9. **Mem0 (the product) is not adopted.** Wrong extraction unit (auto
   LLM-compressed one-liners from chat turns vs. curated article excerpts),
   no triage/human-selection step in its API at all, no document/provenance
   model, and OSS defaults to paid LLM+embedder calls on every add/search.
   Worth reading its source for the retrieval-fusion and temporal-scoring
   ideas; not worth taking as a dependency.
10. **Triage marking is free-form.** You say "keep the part about X"; the
    agent proposes the exact excerpt and shows it back before writing.
    No numbered-paragraph chunking — stays a natural conversation, same
    shape as everything else in this domain's Discussion-mode work.
11. **Tags are free-form,** not a controlled vocabulary. Cheap to retrofit
    a controlled list later if naming drift becomes a real problem;
    premature to lock one in before the corpus exists.
12. **Synthesis snippets are first-class.** Q&A-derived answers from a
    triage session ("how would this apply to X") are savable as
    `kind='synthesis'`, not just source excerpts (`kind='excerpt'`) — often
    more valuable than the raw quote, schema already supports it at no
    extra cost.

## Schema (drafted, not yet built)

```sql
CREATE TABLE documents (
  id            INTEGER PRIMARY KEY,
  url           TEXT,                    -- nullable: pasted text has no URL
  title         TEXT,
  source_type   TEXT NOT NULL,           -- 'url' | 'pasted' | 'file'
  ingested_at   TEXT NOT NULL,           -- ISO 8601
  raw_text      TEXT NOT NULL,
  agent_summary TEXT,
  status        TEXT NOT NULL DEFAULT 'pending'  -- 'pending' | 'kept' | 'partial' | 'discarded'
);

CREATE TABLE snippets (
  id              INTEGER PRIMARY KEY,
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  content         TEXT NOT NULL,
  kind            TEXT NOT NULL DEFAULT 'excerpt',  -- 'excerpt' | 'synthesis'
  tags            TEXT,                              -- comma-separated, free-form
  created_at      TEXT NOT NULL,
  embedding       BLOB,                               -- raw float32 vector bytes
  embedding_model TEXT NOT NULL                        -- e.g. 'voyage-3', 'embeddinggemma-q8'
);

CREATE VIRTUAL TABLE snippets_fts USING fts5(
  content, tags, content='snippets', content_rowid='id'
);
```

`documents.raw_text` is kept even for discarded documents — provenance
survives triage, and nothing is lost if a session is abandoned before
triage completes (`status` starts at `pending` the moment content is
fetched).

## Flow

```mermaid
flowchart TD
    A[You feed a URL/article] --> B[documents row written, status=pending]
    B --> C[Agent summarizes]
    C --> D[Q&A in conversation]
    D --> E{Triage decision}
    E -->|keep whole/parts| F[Write snippet: excerpt or synthesis]
    E -->|discard| G[documents.status=discarded, no snippets]
    F --> H[Embed: Voyage now, EmbeddingGemma Q8_0 later]
    H --> I[snippets_fts indexed]
    I --> J[documents.status=kept/partial]

    K["remind me about X" / "apply article from last week"] --> L[FTS5 + vector search, fused, temporal boost]
    L --> M[Ranked snippets + source title/date]
    M --> N[Agent synthesizes answer, cites source]
```

## Repo layout (planned, not yet built)

```
agents/knowledge/
  AGENTS.md     — domain instructions Layla reads for knowledge-touching requests
  db.py         — schema + connection helper
  ingest.py     — capture: fetch content, create documents row
  triage.py     — write snippets from triage decisions
  embed.py      — provider abstraction (Voyage now, swappable to local)
  search.py     — hybrid search: FTS + vector + fusion
  summarizer_agent.py  — bulk-import only; bootstrapped subagent, mirrors
                          agents/youtube/agent.py
  data/         — gitignored: knowledge.db
```

## Considered and deferred

- **Consolidation gate** (periodic summarizer pass that distills
  overlapping/redundant snippets into one durable note, pattern seen in a
  reviewed video on general AI agent memory design). Legitimate idea, but
  the video's version runs automatically after a message-count threshold —
  that's the same silent-mutation shape already rejected when Mem0's
  ADD-only auto-extraction was ruled out. If ever added, it must be
  human-gated like everything else here: propose the merge, show the
  result, get approval, never silent. Not needed yet — this corpus is
  curated on the way in (nothing enters `snippets` without an explicit
  keep decision), so it grows into a redundancy problem far slower than a
  system that logs everything by default.

## Not started

Every file under "Repo layout" above. No code has been written yet — this
session is architecture-only.
</content>
