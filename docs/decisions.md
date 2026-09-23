---
status: built
updated: 2026-09-20
schema: built
scripts: chunker.py, heading_agent.py, db.py, embed.py, triage.py, ingest.py,
  search.py, youtube.py built; dev/judge.py + dev/eval.py are a dev-only
  grading harness (not in the original plan — see "Grading harness" under
  Step 3); summarizer_agent.py dropped (decision 7)
---

# Design log

Continuous-learning system: feed in articles and video transcripts, triage
them conversationally, keep what's worth keeping, retrieve it later ("remind
me what we learned about X," "apply the article from last week to what we're
building"). This file is the "what we decided and why," written as the
decisions were made — read it before re-litigating anything below.

Paths below predate the 2026-09-20 flatten in places: `agents/knowledge/X.py`
is now `X.py` at the repo root, `chunk.py` is `chunker.py`, and the YouTube
domain folded into `youtube.py` (decision 21). The reasoning is unchanged; a
decision log that edits its own history is worthless.

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
   new dependency, ranked by its built-in BM25) + vector cosine similarity,
   fused by Reciprocal Rank Fusion (RRF — combines by rank position, not
   raw score, since BM25 and cosine are on incomparable scales).
   **Revised (2026-09-06, `search.py` design):** the temporal boost
   originally specced here — detecting date language like "last week" and
   boosting recent snippets — is cut. It assumed that phrasing would be
   common in real queries; the actual usage pattern doesn't mention time
   at all, so the boost would be dead code guarding a case that doesn't
   occur. If recency ties ever become a real problem, the fix is sorting
   ties by `created_at`, not phrase detection — smaller, and only worth
   adding if it's actually noticed in practice.
4. **Vector backend: brute-force cosine in numpy, not an ANN index.** At
   personal scale (low thousands of snippets) a full scan is a single
   matrix-vector multiply, sub-millisecond. `sqlite-vec` or similar is a
   scale problem this system doesn't have — revisit only if the corpus
   reaches tens of thousands of snippets.
5. **Embedding provider: Voyage AI for the PoC, model pinned to `voyage-4`.**
   Free tier (200M tokens) removes the billing objection for now. Every
   embedded row records `embedding_model` — vectors from different
   models/providers aren't comparable, so switching providers later is an
   explicit re-embed job, never a silent correctness bug.
   **Correction (2026-09-06, `embed.py` build):** the original `voyage-3`
   example in the schema comment is stale — Voyage's current pricing
   dropped the free-tier allocation from the voyage-3.x line entirely;
   only the voyage-4 series (plus voyage-context-3/4, voyage-code-3) still
   gets the 200M free tokens this decision relies on. `voyage-4`'s
   32,000-token context also makes `over_cap` chunks (decision 14) a
   non-issue for this provider — the cap exists for the EmbeddingGemma
   local swap target below, whose 2,048-token window is the real
   constraint it guards against.
   **Superseded (2026-09-23, embedding migration):** Voyage is gone. The
   swap to the local model in decision 6 happened, `embed.py` no longer
   contains a Voyage code path, and the corpus was re-embedded. This
   decision is retained as the record of why the PoC started on a hosted
   provider, not as a description of the current system.
6. **Local embedding model: EmbeddingGemma-300M, Q8_0.** Not full
   precision — quality delta is 0.18 MTEB points on English v2 (69.67 →
   69.49), noise-level, at roughly 2x the disk and RAM of Q8. Not Q4
   either — Q4's extra savings over Q8 aren't worth its larger quality
   gap (0.36 pts on English, 0.77 on code) on hardware with no real RAM
   constraint. `fastembed`'s default (`bge-small-en-v1.5`) was rejected in
   favor of this because `bge-small` caps at 512 input tokens, forcing
   aggressive chunking of whole articles; EmbeddingGemma runs 2,048.
   **Correction (2026-09-23):** the figures originally recorded here
   (0.23 pts, 68.36 → 68.13) were wrong — 68.37 is full precision at 256
   dimensions, a row from the Matryoshka truncation table misread as a
   quantization row. Google's published QAT numbers at 768d are 69.49
   (Q8_0), 69.31 (Q4_0), 69.32 (mixed precision) against 69.67 full
   precision. The conclusion is unchanged and the margin is smaller than
   claimed.
   **Executed (2026-09-23):** served by the local ollama daemon, tag
   `embeddinggemma:300m-qat-q8_0` — always the explicit tag, never bare
   `embeddinggemma`, which resolves to the BF16 build and a different
   vector space. These are quantization-aware-trained checkpoints, not
   post-training quantization, which is why the delta is fractions of a
   point. Stored `embedding_model` is `embeddinggemma-q8`; it names the
   quantization because it is the cross-space guard, so it must change if
   the quantization ever does. Mixed precision is strictly dominated by
   Q8_0 on all three benchmarks and is not published as an ollama tag.
7. **Bulk import via bootstrapped subagent: rejected, not deferred.**
   Original decision: many articles at once get a blank `omp -p` subprocess
   each, mirroring `agents/youtube/agent.py`, producing candidate summaries
   only. **Dropped 2026-09-20** — the user's workflow is one article at a
   time, chosen deliberately and discussed live. Bulk mode's whole mechanism
   is keeping source text *out* of Layla's context, which is precisely what
   single-article discussion needs, so the trigger condition never fires and
   `summarizer_agent.py` would be dead code. Single-article ingest/triage and
   all retrieval stay in Layla's own context (decision 1). Revisit only if a
   real backlog dump ever arrives — material the user never chose one at a
   time, wanting a "which of these matter" verdict rather than a
   conversation.
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
13. **`documents` and `snippets` have distinct roles — never redundant.**
    `documents.raw_text` is pure archive/provenance (audit trail, dedup
    check on re-fetching a known URL); it is never searched directly.
    `snippets` is the only retrievable/embedded unit. "Keep the whole
    article" does not mean copying `raw_text` into one giant snippet — an
    entire long article embedded as a single vector produces a mushy,
    undiscriminating representation. It means chunking the whole document
    into several coherent snippets (proposed conversationally, same
    free-form flow as a partial keep), just more of them. Retrieval
    (`search.py`) only ever queries `snippets`; `documents` is joined back
    afterward solely to attach source `title`/`url`/`ingested_at` to a hit.
14. **Chunking is structural, target ~1,800 chars, hard cap 7,000 chars —
    characters, not tokens.** Boundaries follow the document's own
    structure (headings, paragraph groups, complete code examples) — never
    split mid-code-block or mid-sentence. The cap exists because it is
    EmbeddingGemma's input limit (2,048 tokens); 7,000 chars ≈ 2,000 tokens
    at a deliberately conservative 3.5 chars/token estimate, so it
    over-counts and never overshoots the real limit — no tokenizer, no new
    dependency. No minimum size — a one-paragraph snippet is fine if it's a
    complete, useful excerpt. Code fences are atomic: a fenced block is
    never split, and a single unit over the cap (one huge fence or one
    enormous sentence) is never truncated either — it is stored whole as one
    chunk flagged `over_cap`, so FTS5 indexes all of it even though the
    embedding only covers what fits the model's window. Layla proposes the
    boundaries and shows them before writing, same free-form/confirm flow as
    decision 10 — chunking is never silent, even for a "keep whole" article.
    Full build spec: "Chunking implementation plan" below.
15. **Zero stored overlap; read-time neighbor expansion.** For whoever
    builds `search.py`: a hit on a snippet with a non-NULL `chunk_index` is
    joined with `chunk_index - 1` and `+ 1` from the same `document_id`
    where those rows exist and have `kind='excerpt'`. A gap — from a chunk
    the user discarded — simply means no expansion on that side, never a
    re-read of `raw_text`. This is why discarded material can never leak
    back in through expansion. Rationale: duplicated text in FTS5 returns
    the same passage twice in top-k; a read-time join keeps one copy, sharp
    scores, and a window retunable without re-embedding.
16. **Structure normalization precedes splitting**, via a cheap LLM emitting
    insertion points only (never document text), addressed by unit index
    rather than source line, anchor-verified with ±5-unit repair, one
    scoped retry, then deterministic fallback. This removes any
    structured-vs-unstructured routing decision from the splitter — one
    splitter handles every document, article or transcript.
17. **Backend pluggability.** `KNOWLEDGE_LLM` = `omp` (default) | `anthropic`
    | `off`; the deterministic splitter runs whenever no backend is usable,
    so the domain works off an omp harness with degraded boundaries rather
    than not at all.
18. **Context prefix prepended into `content`** using the per-source
    templates in the build spec below, so a chunk carries its subject into
    both the vector and the FTS index.
19. **Rejections are storable knowledge.** When the user rejects part of a
    document, that text is never stored as an `excerpt`, but Layla offers
    to store the *judgment* as `kind='rejection'` holding the reason. A
    distinct kind rather than prose inside a `synthesis` snippet, because
    polarity must be structural — a retrieval hit must never read back as
    an endorsement — and `search.py` can then surface it when a later
    document repeats the rejected claim. Offered and confirmed, never
    automatic, consistent with decisions 10 and 14.
20. **`judge.py` is an offline audit, never a synchronous gate.** Decided
    2026-09-20, closing the open question under Step 3. `chunk.py` never
    calls it: `_resolve_points` keeps its structural `validate_points`
    check plus one scoped retry, and nothing grades heading *quality* on
    the ingest path. A gate would add a second whole-document LLM call to
    a pass that already runs up to 7 minutes, and would couple two
    independent failures — a judge timeout would fail chunking for a
    document whose headings were fine. It would also automate a judgment
    the user already makes: `chunk.py --table` shows every proposed chunk
    before a row is written, so a human gate exists at the only moment it
    matters. Judge runs only from `eval.py`, by hand, when the heading
    prompt or model changes. Choosing this changed zero lines of code —
    it is the behavior that already shipped.
21. **One agent, flat repo root; the YouTube domain folded in.** Decided
    2026-09-20. The Layla-orchestrator-plus-domain-registry structure was
    dropped: `agents/knowledge/*.py` and `agents/youtube/run.py` moved to
    the repo root, `chunk.py` became `chunker.py` (the old name shadows a
    Python 3.9 stdlib module), and `judge.py`/`eval.py` moved to `dev/`.
    Rationale: the registry's founding principle — the discussion/verdict
    mode split — had no remaining instance once `summarizer_agent.py`
    (decision 7) and the YouTube summarizer subagent were both cut, so it
    routed nothing. Email and calendar, the domains it was built to
    accommodate, were never designed past a wish list.
    What was deleted with the YouTube domain: playlist listing, the
    per-video summarizer subagent, the one-call playlist ingest pipeline,
    the `INDEX.md` regenerator, and the markdown store under `data/videos/`
    — playlist triage was never run against a real playlist, and the
    markdown store duplicated text `knowledge.db` already holds. What was
    kept, because it was won against real videos rather than designed:
    the two-source caption fetch with fallback, the rolling-caption dedupe,
    and the hard-line-break normalization. `playlist.py`'s single-video
    metadata lookup survives as `youtube.video_metadata()`, which supplies
    the title and channel `chunk_transcript` needs for its prefix.
    A video is now just a document with `source_type='transcript'`.
22. **Frame images live on disk; the database holds a path.** Decided
    2026-09-20, ahead of any frame code, so the `assets` table can be built
    without re-opening it. Not decided on performance: SQLite is ~35% faster
    than the filesystem for ~10KB blobs and loses somewhere between 250KiB
    and 1MiB (sqlite.org/fasterthanfs.html and the Jim Gray paper it cites),
    and a 720p JPEG at ~100–300KB sits on that crossover. The reasons are
    operational: a vision model is handed a path, so a BLOB would be exported
    to a temp file on every look; incremental backup copies only new frames
    rather than rewriting a multi-GB database; and `knowledge.db` stays small
    enough to move and inspect. The cost accepted is orphan risk in both
    directions — a deleted row leaves a file, a deleted file leaves a
    dangling path — mitigated by `assets.sha256` and a periodic sweep, never
    by moving bytes into the database. Retrieval is unaffected either way: a
    frame is found through its LLM description, stored as a snippet with
    `kind='frame'` and `asset_id`. Full shape and the two still-open options
    (OCR text, ordering) in `docs/youtube.md`.
23. **Personal memory is `entities` + `observations`, exact-lookup, same
    agent, same database.** Decided 2026-09-21. Facts about the user's life
    — a family member's shoe size, a standing preference, something said on
    a phone call — are not document-derived knowledge and do not belong in
    `snippets`: they have no source document (`snippets.document_id` is NOT
    NULL), they change over time, and they are keyed by an entity rather
    than by topic.
    **Why a second store rather than a second index.** The query shapes
    genuinely differ. `search.py` is a *ranker*: it returns relevant
    material to reason over, and an imperfect result is still useful.
    "What is Nora's shoe size" is a *lookup*: a confidently wrong neighbor
    is worse than nothing, and "Nora wears 8" / "Tomas wears 10" embed
    almost identically, so RRF would happily rank them adjacent. Layla
    routes by question shape, the same way she already decides which content
    words to pass as `terms`.
    **Naming.** `entities` over `subjects`/`things` — it is the word every
    future reader recognizes, and the table is a registry of things you can
    say something about. `observations` over `facts` — half of what gets
    stored is not a fact but something someone said, true *as of* when it
    was said, which is exactly the staleness semantics a shoe size needs.
    **Shape.** `entities(name, kind, relation, full_name, aliases, note)`:
    `name` is the canonical label, `aliases` is every word the user actually
    uses, because "mom"/"mum"/"Linda" must resolve to one row or the data
    fragments silently. Identity fields are deliberately minimal and mostly
    nullable — demanding a last name for a cousin you only ever call "my
    cousin" creates friction at the moment of writing, which is the moment
    that must stay cheap. `observations(entity_id, attribute, value, body,
    source, observed_at, superseded)`: nullable `attribute` is the trick
    that lets structured rows (exactly queryable) and prose rows (no clean
    key) share one table and one supersede rule.
    **Never edited in place.** A changed value supersedes the old row rather
    than overwriting it, so history survives and a lookup can never return
    two contradictory values. Same reasoning as decision 19's polarity
    argument: correctness must be structural, not a matter of reading care.
    **Preferences are not a special case.** The user is entity `kind='self'`;
    a standing preference is an observation about them, with `source` citing
    the document that produced it.
    **Rejected: a graph database.** A graph pays for itself on multi-hop
    traversal over typed edges; these are one-hop attribute lookups over
    tens of rows. `entity → attribute → value` *is* a triple, stored as
    rows, without a second engine, a second query language, or an extraction
    step to keep in sync. Consistent with decision 8's rejection of
    automatic entity extraction. Revisit if intersection queries across many
    entities ever become routine — and even then, a three-way join in SQLite
    comes first.
    **Rejected: a separate domain or subagent.** Decision 21 killed the
    domain layer one day earlier; re-adding it for the second feature would
    re-litigate it with no new evidence. Subagents exist to keep bulk text
    out of the conversation and to do work that needs no conversation —
    an observation is twenty characters, and resolving "him" to an entity
    requires the live conversation. Two new modules, not a new agent. If
    `AGENTS.md` outgrows comfort, split the *instructions* into an on-demand
    file, never the agent.
    **Left open deliberately.** How prose observations get searched. A
    structured lookup needs no search at all; prose rows will eventually
    want FTS or embeddings, which reopens the second-ranked-index question.
    Thirty real rows will answer it better than a guess made at zero.

24. **Source metadata is one JSON blob, and chapter-anchoring is enforced at
    the write boundary.** `documents.source_metadata` holds a single blob
    owned by `metadata.py`: common keys flat (`author`), source-specific keys
    nested under one key per source type (`video: {video_id, duration,
    chapters}`). `youtube.py` already fetches chapters alongside the
    captions, so `ingest.capture(..., metadata=...)` stores them once and
    `chunker.invoke_document(conn, document_id)` reads text, title, author,
    and chapters back from the row — no `--url` flag, no second yt-dlp call,
    no network at chunk time. `triage.write_chunks()` then refuses a
    transcript batch whose `chapters_used` is false when the video published
    chapters.
    **Why the blob, not columns.** A column per source kind widens the table
    with mostly-NULL fields and costs a migration per new source type; the
    nested kind key keeps each source's contract explicit and lets the
    accessor validate per `source_type`.
    **Why the gate, not an instruction.** The rule previously lived only in
    prose, and `chunker.py`'s `if chapters:` branch is a consumer-side
    conditional — a chapterless call was legal, exited 0, and produced
    plausible-looking output with the wrong headings. A conditional with a
    legitimate else-branch is not enforcement.
    **Unrecorded is not "none published".** A failed `video_chapters()`
    lookup omits the `chapters` key entirely, while a chapterless video
    records `[]`. Only the second licenses the chapterless split; the first
    is an error telling the operator to re-capture.
    **Documents captured before this decision carry no blob** and are
    refused at both the chunker and the gate rather than silently degraded.


## Schema (built; see db.py)

```sql
CREATE TABLE documents (
  id              INTEGER PRIMARY KEY,
  url             TEXT,                    -- nullable: pasted text has no URL
  title           TEXT,
  source_type     TEXT NOT NULL,           -- 'url' | 'pasted' | 'file' | 'transcript'
  ingested_at     TEXT NOT NULL,           -- ISO 8601
  raw_text        TEXT NOT NULL,
  structured_text TEXT,                    -- heading-annotated derived copy; raw_text stays pristine
  agent_summary   TEXT,
  status          TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'kept' | 'partial' | 'discarded'
  source_metadata TEXT                             -- JSON blob owned by metadata.py (decision 24)
);

CREATE TABLE snippets (
  id              INTEGER PRIMARY KEY,
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  content         TEXT NOT NULL,
  kind            TEXT NOT NULL DEFAULT 'excerpt',  -- 'excerpt' | 'synthesis' | 'rejection'
  chunk_index     INTEGER,                           -- position in document order; NULL for synthesis/rejection
  tags            TEXT,                              -- comma-separated, free-form
  created_at      TEXT NOT NULL,
  embedding       BLOB,                               -- raw float32 vector bytes
  embedding_model TEXT                                 -- 'embeddinggemma-q8'; NULL only alongside a NULL embedding
);

CREATE VIRTUAL TABLE snippets_fts USING fts5(
  content, tags, content='snippets', content_rowid='id'
);

CREATE TABLE entities (                     -- decision 23
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,                -- canonical label
  kind        TEXT NOT NULL,                -- 'self' | 'person' | 'pet' | 'vehicle' | 'place' | 'org' | 'thing'
  relation    TEXT,                         -- 'mother', 'brother'; NULL for objects
  full_name   TEXT,
  aliases     TEXT,                         -- comma-separated: every word the user uses
  note        TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE observations (                 -- decision 23
  id          INTEGER PRIMARY KEY,
  entity_id   INTEGER NOT NULL REFERENCES entities(id),
  attribute   TEXT,                         -- 'shoe_size'; NULL for prose
  value       TEXT,                         -- required when attribute is set
  body        TEXT,                         -- required when attribute is NULL
  source      TEXT,
  observed_at TEXT NOT NULL,
  superseded  INTEGER NOT NULL DEFAULT 0
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
    F --> H[Embed: EmbeddingGemma Q8_0, local via ollama]
    H --> I[snippets_fts indexed]
    I --> J[documents.status=kept/partial]

    K["remind me about X" / "apply article from last week"] --> L[FTS5 + vector search, fused, temporal boost]
    L --> M[Ranked snippets + source title/date]
    M --> N[Agent synthesizes answer, cites source]
```

## Repo layout (current, post-flatten)

```
layla/
  AGENTS.md         — the agent's whole instruction set: tools, workflows, style
  README.md         — public front door
  db.py             — schema + connection helper
  ingest.py         — capture: create documents row, no fetching
  chunker.py        — unit segmentation, validation/repair, structural split,
                       context prefixes, public API + CLI
  heading_agent.py  — heading-insertion call; pluggable backend (omp | anthropic | off)
  triage.py         — write snippets from triage decisions
  embed.py          — provider abstraction (local EmbeddingGemma via ollama)
  search.py         — hybrid search: BM25 + cosine, fused by RRF
  youtube.py        — captions (two sources, fallback) + video metadata
  dev/
    judge.py        — NOT in original plan. Grades heading_agent's points for
                       essence-vs-topic-label quality (score, grade, reasoning,
                       excerpt per heading). Offline audit only — called by
                       eval.py, never by chunker.py (decision 20).
    eval.py         — NOT in original plan, NOT production. Dev-only manual
                       tuning harness: runs heading_agent + judge once at
                       whatever model/effort heading_agent.py currently
                       defaults to, across a fixed doc set, appends a
                       human-readable report to eval.md. No config sweep — it
                       exists to eyeball prompt/model changes by hand, not to
                       auto-pick a winner.
    eval.md         — gitignored, NOT committed. Local log of eval.py runs
                       (model/effort tried, per-heading scores, findings).
                       Exists only in this checkout, not on a fresh clone.
  docs/
    decisions.md      — this file
    system-overview.md — architecture + numbered diagrams
    youtube.md        — caption sources, prerequisites, frame plan + schema
    goals.md          — what this is for; what is deliberately not here
    research.md       — evidence behind the architecture decisions
  data/             — gitignored: knowledge.db plus verification fixtures.
                       Current set: data/articles/ (3 articles —
                       train-llm-from-scratch.md is densely headed end to end,
                       the other two have partial or no structure). The four
                       transcript fixtures were discarded in the flatten;
                       refetch one with youtube.py when grading transcripts.
```

`summarizer_agent.py` appears throughout the older sections below. It was
never built and never will be (decision 7).

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

## Completed migration tasks

- **Re-embed job: Voyage → EmbeddingGemma-300M Q8_0. Done 2026-09-23.**
  Built as `triage.reembed(conn, document_id=None, model=None)` and run
  over the whole corpus: 46/46 snippets re-embedded in 10.4s,
  `SELECT DISTINCT embedding_model` now returns `embeddinggemma-q8` alone.
  - **What it does:** for every row where `embedding IS NULL OR
    embedding_model IS NOT 'embeddinggemma-q8'`, re-run the already-stored
    `content` (untouched by this job) through EmbeddingGemma and overwrite
    `embedding` + `embedding_model` on that row. Backfill and migration are
    the same query — a missing vector and a stale one need identical work,
    so there is no separate backfill function. `IS NOT` rather than `!=`
    because SQLite's `!=` evaluates to NULL against a NULL column, silently
    skipping exactly the rows that most need the work.
  - **Why it's safe:** `content` is the source of truth; `embedding` is a
    derived, regenerable index. Nothing about switching providers touches
    the actual text, so no data is at risk — only the vectors go stale
    until this job runs. Batches commit individually, so an interrupted
    run leaves a consistent part-old/part-new corpus rather than rows whose
    vector and model label disagree.
  - **Why it's cheap:** the destination model is local and free — CPU/GPU
    time on-device, not a paid API bill, unlike a hypothetical reverse
    migration.
  - **What still works before the migration runs:** FTS5 keyword search
    over `content` is completely unaffected by which embedding model is
    active — old rows stay fully keyword-searchable the entire time. Only
    vector/semantic search on unmigrated rows is degraded in the gap
    between switching the active provider and running this job.
  - **Guard while both embedding spaces briefly coexist:** `search.py`
    only runs cosine similarity against rows whose `embedding_model`
    matches the currently active provider — comparing vectors across
    incompatible spaces produces silently wrong rankings, not an error, so
    this is an explicit filter, verified present before the swap.

## Chunking implementation plan

Build spec for decisions 14-19 above: `agents/knowledge/chunk.py` (unit
segmentation and structural splitting, character-based sizing) plus
`agents/knowledge/heading_agent.py` (heading-insertion call with a
pluggable backend), verifiable standalone against two real fixtures in the
repo, with no database. Heterogeneous input makes this load-bearing:
articles carry markdown headings, YouTube transcripts carry none.

Scope boundary: `db.py`, `search.py`, and read-time neighbor expansion are
not built here (those files do not exist); expansion is *specified* in
decision 15 above for whoever builds `search.py`.

### Approach

#### Step 1 — Package init

Create empty `agents/knowledge/__init__.py` (0 bytes), matching
`agents/__init__.py` and `agents/youtube/__init__.py`, so
`python3 -m agents.knowledge.chunk` resolves.

**Verification**

Manual: `python3 -c "import agents.knowledge; print('ok')"` from the repo
root — prints `ok`, no `ImportError`. Nothing else to check; an empty
`__init__.py` has no behavior beyond importability.

#### Step 2 — `agents/knowledge/chunk.py`: unit segmentation

Boundaries are never derived from the file's own line breaks. A transcript may
arrive as one unbroken line, and prose is often hard-wrapped at a column width
that has nothing to do with its structure. So `chunk.py` first segments the
document into **atomic units it constructs itself**, and every downstream position
— model-facing and stored — refers to those units, never to source lines.

```python
NAME = "knowledge_chunker"
DESCRIPTION = "Split a document into retrieval-sized candidate snippets with context prefixes."

TARGET_CHARS = 1800
MAX_CHARS = 7000
ANCHOR_WINDOW = 5
```

`segment_units(text: str, source_kind: str) -> list[dict]`

Each unit is `{"index": int, "text": str, "start": int, "end": int, "stamp": str | None}`
where `start`/`end` are character offsets into `text` such that
`text[start:end] == unit["text"]`. `index` is 0-based and contiguous. The character
offsets are the real position record; the index is only the handle the model is
given. Offsets make a boundary exact and independent of line breaks, and they are
what `split_body` slices with.

Unit granularity by shape, chosen in this order:

1. **Transcript** (`source_kind == "transcript"`): one unit per `[MM:SS]` segment,
   `stamp` set. Guaranteed one segment per line by
   `agents/youtube/AGENTS.md:118-119` (caption line breaks normalized away, rolling
   captions collapsed by `run.py`), so this parse is safe for `transcript.md`.
2. **Article with blank lines**: one unit per blank-line-delimited paragraph.
3. **Article with no blank line, or any paragraph over `TARGET_CHARS`**: split into
   sentences — `[.?!]` followed by whitespace or end of string. This is the branch
   that handles a single-long-line document; the absence of newlines is irrelevant
   because units come from sentence punctuation, not layout.

A markdown heading line (`^#{1,6} `) is always its own unit with its `#` markers
intact, so a document that already has structure keeps it through segmentation.
A fenced code block (` ``` ` to its closing fence) is always exactly one unit,
however long — this is what makes fences unsplittable later.

If sentence splitting still yields a unit over `MAX_CHARS` (one enormous sentence,
or a single fenced block), keep it as one unit and let step 5 flag it `over_cap`.
Never split a unit at an arbitrary character position.

**Transcript parsing** (`parse_transcript(text) -> tuple[dict, list[dict]]`,
returning frontmatter plus units):

- Skip a leading `---` YAML block, parsed with the same simple loop as
  `agents/youtube/index.py:parse_frontmatter` (`key: value` per line).
- Each segment matches `^\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.*)$`. Scan with
  `re.finditer` over the whole body rather than line-by-line, so a transcript
  collapsed onto one line — several `[MM:SS]` markers in a single line — still
  segments correctly. Each match starts a unit and runs to the next match or end.
- `_parse_stamp(s) -> int` converts `MM:SS` and `H:MM:SS` to seconds by splitting on
  `:` and folding right-to-left. Local helper: `run.py`'s `_parse_vtt_timestamp`
  parses WebVTT `HH:MM:SS.mmm`, a different format.
- Text before the first `[MM:SS]` match is dropped; a body with zero matches falls
  back to the article path (rule 2/3 above) with every `stamp` left `None`, so a
  mislabeled or caption-free file still chunks instead of crashing.
- Each chunk's `start`/`end` timestamps are the `stamp` of its first and last unit
  carrying one.

**Verification**

Manual — run `segment_units` against both fixtures and read the unit
boundaries directly, no assertions:

```
python3 -c "
from agents.knowledge.chunk import segment_units
text = open('agents/knowledge/data/articles/3-years-of-graph-engineering-with-langgraph.md').read()
units = segment_units(text, 'article')
print(len(units), 'units')
for u in units[:8]:
    print(u['index'], u.get('stamp'), repr(u['text'][:70]))
"

then the same against
`agents/youtube/data/videos/ow1we5PzK-o/transcript.md` (body only, after
the frontmatter) with `source_kind='transcript'`.

Read for: article units break at paragraph/heading boundaries with no unit
spanning a blank line; any `#`-heading line is isolated as its own unit; a
fenced code block (if the fixture has one) prints as a single unit even
though it contains embedded newlines; transcript units number ~534, `stamp`
is set on every one, and each unit's text starts right after its `[MM:SS]`
marker with no marker leaking into two units.

#### Step 3 — `agents/knowledge/heading_agent.py`: heading-insertion call

Named for the sibling convention (`agents/youtube/agent.py`) — a module that wraps a
headless `omp -p` subprocess. Module names cannot contain hyphens and stay
importable, so the underscore form is the available one.

Conventions mirror `agents/youtube/agent.py`: module docstring in the repo's
`In:`/`Out:`/`State:` style, `NAME`, `DESCRIPTION`, `INPUT_SCHEMA`, `invoke()`
returning a dict that never raises for expected failures, `main()` printing JSON
and returning 0/1.

```python
NAME = "knowledge_heading_inserter"
DESCRIPTION = "Propose heading insertion points for a document that lacks usable structure."

DEFAULT_MODEL = "anthropic/claude-haiku-5"
DEFAULT_EFFORT = "medium"
TOOL_NAMES = ["read", "write"]
```

**Built and committed with two changes discovered after this spec was written**
(see `eval.md` for the runs behind these, gitignored/local only):

- `DEFAULT_EFFORT` shipped as `"medium"`, not `"low"` — compared across the
  fixture set (`eval.py`, dev-only harness, see Repo layout above); `low`
  produced comparable essence-quality scores but consistently worse anchor
  placement (near-0 accuracy vs. the same doc under `medium`), and `medium`'s
  extra thinking budget is cheap relative to the failure mode it avoids.
- The abstain rule (below, "if the document already has clear headings...")
  was under-specified and got fixed. The original wording let the model treat
  *any* existing heading — including a trailing FAQ or "Key Takeaways"
  section covering a fraction of the document — as license to write `[]` for
  the whole thing, reproduced 3x on the flowtivity-guide fixture (0 headings
  every time despite ~90% of the doc being one unheaded block). Fixed by
  tying "already covered" to the same 1800/7000-char segmentation rule that
  already governs insertion density, so abstaining is the *result* of
  applying that rule everywhere and finding nothing to do, not a separate
  gate a model can satisfy with one token heading. Current prompt text:
  "Existing headings do not excuse you from segmenting the rest of the
  document — apply the topic-change and ~1800/7000-character rules above to
  every unheaded stretch, including everything before the first existing
  heading. An existing heading only covers the content it directly
  introduces, not what precedes it. Write `[]` only if that process finds
  nothing left to insert." Confirmed fixed: the same fixture went from 0
  headings (3 separate runs) to a real split spread across the previously
  unheaded body, not one heading for the whole blob.

**`[]` is real but not deterministic, even on a maximally-covered document —
calibrate expectations accordingly.** Confirmed by taking a densely-headed
fixture (`data/articles/train-llm-from-scratch.md`, 30 real headings) and
running `insert_headings` on it 3 times with nothing changed between runs:
`[]` twice, one heading once (splitting a legitimately marginal sub-topic —
a diagram color-legend paragraph — out of a longer section). This is
expected variance, not a bug: "does this count as a genuine topic change"
is a judgment call the model can land on either side of at the margin. Do
not write a check that asserts `points == []` on any live LLM call without
a retry or tolerance band — assert instead that `headings_inserted` stays
small (0-1 on this fixture) and none of the points duplicate/overlap
existing headings. `[]` still matters even though it's not guaranteed
every run: it's what makes the retry loop and any future periodic re-audit
of already-chunked content terminate, rather than always finding "just one
more" heading to add.

`insert_headings(units: list[dict], source_kind: str, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT, feedback=None) -> dict`

- Takes the units from step 2, not raw text — the caller owns segmentation.
- `source_kind` is `"article"` or `"transcript"`; it selects prompt wording only.
- Returns `{"ok": True, "points": [{"before_unit": int, "anchor": str, "heading": str}], "backend": str}`
  or `{"ok": False, "error": str, "backend": str}`. `points` MAY be empty — that is
  the "already well structured, changed nothing" result, not a failure.
- Backend from `os.environ.get("KNOWLEDGE_LLM", "omp")`. Value `off` returns
  `{"ok": False, "error": "KNOWLEDGE_LLM=off", "backend": "off"}` without a call.
  An unrecognized value returns `ok: False` with `error` naming the bad value.

**Numbered input.** `render_units(units) -> str` builds one block per unit as
`f"[{u['index']}] {u['text']}"`, joined by blank lines. A unit's own internal
newlines are preserved, so a code block still reads as a code block. Indices are the
only positional vocabulary the model is given — source line numbers never appear in
its input, so it cannot reference them.

**`omp` backend.** Write the rendered units to a temp file and the model's JSON to a
second temp path, then read that file — do not parse stdout. This copies
`agents/youtube/agent.py:133-158` exactly, including its
`if not Path(out).exists()` failure check, because file-output is already this
repo's proven contract and stdout parsing is fragile.

```python
cmd = [
    "omp", "-p",
    "--tools", ",".join(TOOL_NAMES),
    "--no-extensions", "--no-skills", "--no-rules",
    "--approval-mode", "yolo",
    "--model", model,
    "--thinking", effort,
    "--no-session",
    "--system-prompt", prompt,
    f"Read {units_path}. Write the JSON array to {out_path}.",
]
subprocess.run(cmd, capture_output=True, text=True, timeout=180)
```

Handle `FileNotFoundError` → `{"ok": False, "error": "omp not found on PATH"}` and
`subprocess.TimeoutExpired` → `{"ok": False, "error": "omp headless call timed out after 180s"}`,
matching the sibling's two guards. Use `tempfile.TemporaryDirectory()` for both paths.

**`anthropic` backend.** Import `anthropic` lazily inside the branch so the package
stays optional and absent-import returns
`{"ok": False, "error": "anthropic package not installed"}`. Call
`client.messages.create(model="claude-haiku-4-5", max_tokens=2000, system=prompt, messages=[{"role": "user", "content": rendered_units}])`,
then `json.loads` the first content block's text. Missing `ANTHROPIC_API_KEY` →
`{"ok": False, "error": "ANTHROPIC_API_KEY not set"}`.

**System prompt** (`build_system_prompt(source_kind, feedback=None)`, pure, mirroring
`agent.py:100-118` including its `"# Previous attempt failed\n" + feedback +
"\nFix exactly these issues. Change nothing else."` retry block):

**Stale as originally drafted — this is the actual, committed prompt** (heading_agent.py
`_BASE_PROMPT`), which is meaningfully more detailed than the first draft below it used
to be: it adds the essence-vs-topic-label rubric with examples, the per-list-item
heading rule, and the abstain-rule fix described above.

```
You mark section boundaries in a document. You never rewrite, summarize, or
reproduce its text.

The document is given as numbered blocks, each starting with "[N]". A block is one
paragraph, one sentence, one transcript segment, or one code block.

Write a JSON array. Each element marks one heading to insert:
  {"before_unit": N, "anchor": "first six words of block N", "heading": "Section title"}

Rules:
- before_unit is the number of the block the heading goes immediately BEFORE.
- anchor must copy the first six words of block N verbatim, so placement is verifiable.
- heading distils the essence of the section: the specific insight, argument, or
  decision it makes — not just the subject it discusses. Positional labels
  ("Section 3", "Continued", "More details") always fail. A topic label also
  fails even though it names the right subject, because it says nothing about
  it — only an essence-capturing heading, stating what the section actually
  concludes or argues, passes. Examples:
    - weak topic label: "Vector Backend" -> strong essence: "Why brute-force
      cosine beats an ANN index here"
    - weak topic label: "Creator-Verifier Pattern" -> strong essence: "One
      agent writes, another checks — catches errors the writer can't see in
      itself"
  heading is your own words, under 80 characters, never a verbatim quote from
  the text.
- Insert a heading at every genuine topic change, including a new item in a
  named list (one of several patterns, steps, or techniques) — each gets its
  own heading even if the resulting section is far shorter than 1800
  characters. A later search for one specific item must land on a heading
  naming that item, not one naming the whole list.
- The ~1800 character target describes how much of ONE topic to cover before
  the next heading; it is not a floor to hit by merging distinct topics
  together. Never leave a section longer than 7000 characters.
- Existing headings do not excuse you from segmenting the rest of the document —
  apply the topic-change and ~1800/7000-character rules above to every unheaded
  stretch, including everything before the first existing heading. An existing
  heading only covers the content it directly introduces, not what precedes it.
  Write [] only if that process finds nothing left to insert.
- Write only the JSON array. No prose, no code fence, no document text.
```

For `source_kind == "transcript"`, append: `"Blocks are transcript segments prefixed with [MM:SS]. Topic changes are where the speaker moves to a new subject."`

**Verification**

Manual, three runs — this step wraps a live LLM call, so "success" is
read-and-judge, not asserted:

```
python3 -c "
from agents.knowledge.chunk import segment_units
from agents.knowledge.heading_agent import insert_headings
text = open('agents/youtube/data/videos/ow1we5PzK-o/transcript.md').read().split('---', 2)[2]
units = segment_units(text, 'transcript')
r = insert_headings(units, 'transcript')
print(r['backend'], len(r.get('points', [])))
for p in r.get('points', []):
    print(p['before_unit'], p['heading'], '|', units[p['before_unit']]['text'][:70])
"
```

1. Against the transcript fixture: `backend == 'omp'`, points list
   non-empty, and for each printed point the unit text actually matches
   the anchor and heading topic — read a handful, not all.
2. Against `data/articles/train-llm-from-scratch.md` (densely headed, 30
   real headings covering it end to end): expect `points` to stay small —
   observed `[]` on 2 of 3 identical runs, one heading (a genuinely
   marginal sub-topic split) on the third. Do not assert `points == []`
   outright; assert `len(points) <= 1` and, if non-empty, that the point's
   `before_unit` doesn't land right before an existing markdown heading
   (that would mean duplicating structure that's already there, the actual
   failure mode this check exists to catch). See the "`[]` is real but not
   deterministic" note under Step 3's build summary above for why.
3. `KNOWLEDGE_LLM=off` re-run of either: `r['ok'] is False`,
   `r['backend'] == 'off'`.

#### Step 4 — Validation and repair

In `chunk.py`: `validate_points(points, units) -> tuple[list, str | None]`

Applied in order; returns repaired points plus a scoped failure message (`None` when
clean). The message is fed straight back into one retry, so it must name the exact
offenders like `agents/youtube/PLAN.md:109-110` requires — never a blanket string.

1. Drop non-dict elements and elements missing `before_unit` or `heading`.
2. Coerce `before_unit` to `int`; reject out of `0..len(units)`.
3. Anchor repair: normalize both sides before comparing — casefold, strip a
   leading `[MM:SS]` marker, and strip markdown/punctuation decoration
   (`**bold**`, quotes, stray commas) per word. **Confirmed necessary, not
   speculative**: `eval.py`'s first cut compared raw whitespace-split words
   and got a ~0% match rate on nearly every fixture, purely from counting
   `[MM:SS]` and `**`/quote characters as part of word one — see `_normalize_anchor`
   in `eval.py` for the working implementation to port. Two real (non-bug)
   patterns remain after normalizing, both confirmed against live output:
     - **Truncated but correct prefix**: the model sometimes gives fewer than
       six words (e.g. 5 of 6) when a caption unit is very short — every word
       given is a verbatim-correct prefix, just incomplete. Compare as a
       prefix match, not exact equality, or these get wrongly dropped.
     - **Boundary splice**: auto-generated captions fragment sentences across
       many short `[MM:SS]` units, so the natural six-word anchor phrase
       often starts mid-unit or spans into the next one (e.g. unit N ends
       "...one of the first", unit N+1 begins "benchmarks that..."; anchor is
       "one of the first benchmarks that"). This is what `ANCHOR_WINDOW` is
       for — the true target is usually within a few units either way — but
       don't expect prefix-matching alone to fix it; the window scan is load
       -bearing for this case specifically.
   On mismatch after normalizing, scan `before_unit ± ANCHOR_WINDOW` for a
   unit whose first six words match (prefix-tolerant) and move the point
   there. No match in the window → drop the point and record it. Unit
   indices drift far less than line numbers did, since a unit is a whole
   paragraph or segment, but the echo stays as the correctness check.
4. Reject empty headings and headings over 80 chars; strip any leading `#` the model
   added, since `chunk.py` writes the `##` marker itself.
5. Sort by `before_unit`; drop duplicates at the same index, keeping the first.
6. Drop any point landing immediately before a unit that is already a markdown
   heading — the document had structure there and a second heading would create an
   empty section.
7. Sum unit lengths between consecutive points. Any resulting section over
   `MAX_CHARS` → failure message naming it, e.g.
   `"section starting at block 210 is 9,240 chars, over the 7,000 cap — insert a heading inside it"`.

Retry policy: one retry via `heading_agent.insert_headings(..., feedback=message)`.
If the second attempt still fails validation, discard the LLM result entirely and
split on whatever headings the document already had, reporting
`backend: "fallback"`.

Applying accepted points produces `structured_text`: units re-joined in order with
`f"## {heading}"` inserted before the unit named by each point. Because units are
exact slices of the source, re-joining cannot alter, drop, or duplicate text —
Verification check 4 asserts this byte-for-byte.

**Verification**

Manual — hand-build a bad `points` list against a real fixture's units and
read what comes back, no assertions:

```
python3 -c "
from agents.knowledge.chunk import segment_units, validate_points
units = segment_units(open('agents/knowledge/data/articles/3-years-of-graph-engineering-with-langgraph.md').read(), 'article')
def first6(i): return ' '.join(units[i]['text'].split()[:6])
bad = [
    {'before_unit': 9999, 'anchor': 'nonsense', 'heading': 'Out of range'},
    {'before_unit': 5, 'anchor': first6(8), 'heading': 'Drifted anchor'},
    {'before_unit': 3, 'anchor': first6(3), 'heading': 'Dup A'},
    {'before_unit': 3, 'anchor': first6(3), 'heading': 'Dup B'},
]
points, msg = validate_points(bad, units)
for p in points: print(p)
print('message:', msg)
"
```

Read for: the `before_unit: 9999` point is gone; the drifted one now reads
`before_unit: 8` (moved to match its anchor, not left at 5); only one of
the two `before_unit: 3` duplicates remains.

Separately, force the two failure paths that produce a retry message, not
just a silent repair: an anchor with no match anywhere in `±5` units, and a
hand-built `points` list whose gaps make one section exceed `MAX_CHARS`.
Read that `msg` names the specific unit/section, e.g. contains `"section
starting at block"` or the dropped anchor's text — a generic string here is
a bug per decision 16 / `agents/youtube/PLAN.md:109-110`.

**Built, with two deliberate divergences from the spec above** — both found
by running the live pipeline, both recorded here rather than silently coded:

- **The section-cap check measures boundaries, not just points.** Rule 7 as
  written ("sum unit lengths between consecutive points") reports one
  30,000-char section on a densely-headed article whenever the model
  correctly returns `[]`, because the document's own 30 headings aren't
  counted as boundaries. That fires a pointless retry on exactly the
  documents that need no work. The implementation unions the inserted points
  with the document's own heading units before measuring, so a section the
  document already delimits is never reported as the model's failure.
- **A second validation failure keeps the validated points instead of
  discarding them.** The retry policy above discards the whole LLM result
  and reports `backend: "fallback"`. That is strictly worse output: points
  that survive `validate_points` are already sound — offenders were dropped,
  not kept — so the only complaint that can outlive a retry is an
  under-dense section, and on a heading-less transcript falling back means
  zero boundaries instead of ~30 good ones. The cap is never breached in
  the output either way, because `split_body` caps every chunk at
  `TARGET_CHARS` regardless. Implementation: retry once on any message, keep
  whichever attempt validated more points, and report `"fallback"` only when
  no LLM result was usable at all. `"passthrough"` is now reserved for an
  empty *and* clean result — empty-with-a-complaint is a fallback, not a
  well-structured document.

#### Step 5 — Structural split

`split_sections(units, points) -> list[dict]` cuts at every heading — both the
document's own heading units and the inserted points — producing
`{heading_path, units}` where `heading_path` is the list of enclosing headings from
the document's hierarchy (an `###` under an `##` inherits both; an inserted heading
is always `##`).

`split_body(section_units) -> list[list[dict]]` handles a section over
`TARGET_CHARS` by accumulating units until adding the next would exceed it, then
cutting. Units are already the finest legal boundary from step 2, so no further
splitting rule is needed and no cut can land mid-sentence or mid-segment. A section
at or under `TARGET_CHARS` yields exactly one chunk and is never merged with a
neighbor — decision 14 sets no minimum size, and merging would blur two topics.

A chunk's body is `text[first_unit["start"]:last_unit["end"]]`, sliced from the
source by character offset, so whitespace between units is preserved exactly as
written.

**Code fences are atomic** because step 2 makes a fenced block one unit — there is no
parity tracking to get wrong here. A single unit over `MAX_CHARS` (a huge fenced
block or one enormous sentence) becomes one chunk with `over_cap: True`. It is
stored whole so FTS5 indexes all of it, and the embedding covers only what fits the
model's window. Truncating would lose code and splitting a code example arbitrarily
is worse than one blunt vector; the flag makes it a known limitation rather than a
silent one, and `format_candidates` surfaces it.

**Verification**

Manual — run the full chunker on an available fixture and read the per-chunk
table:

```
python3 -c "
from agents.knowledge import chunk
r = chunk.invoke('agents/knowledge/data/articles/3-years-of-graph-engineering-with-langgraph.md', kind='article', title='LangGraph retrospective')
for c in r['chunks']:
    print(c['chunk_index'], c['chars'], c['over_cap'], c['prefix'])
"
```

Read for: `chars` clusters near 1,800 with none over 7,000 unless
`over_cap` is `True`; chunk count and prefixes line up with the fixture's
real heading structure in document order (none skipped, none repeated);
add `c['content'][:80]` and confirm no chunk body starts or ends
mid-sentence. If the fixture has a fenced code block, confirm it sits
wholly inside exactly one chunk, never split across two.

#### Step 6 — Context prefix

`build_prefix(source_kind, doc_title, heading_path, start=None, end=None, channel=None) -> str`

|`source_kind`|Prefix|
|---|---|
|`article`|`Title > Section > Subsection`|
|`transcript`|`Video Title — Channel [12:34–15:02] > Section`|

Rules: join `heading_path` with ` > `; omit any empty component and its separator, so
an untitled document or an unnamed channel degrades cleanly instead of emitting
`" > "`. Stored `content` is `f"{prefix}\n\n{body}"`. `chars` and the `MAX_CHARS`
check both count the full `content` including the prefix, since that is what gets
embedded.

**Revised (2026-09-20): the document's own H1 never repeats the title.**
`split_sections` puts a document's level-1 heading at `heading_path[0]`, so with
a `doc_title` supplied every prefix read `Title > Title > Section` — the title
slot filled twice, ~25 wasted chars inside every embedded chunk. `_build_chunks`
now drops `heading_path[0]` when it is the document's own H1 (`_own_h1(units)`)
*and* a `doc_title` was given. Without a `doc_title` the H1 stays, since it is
then the only title there is. Done in `_build_chunks`, not `build_prefix`: the
prefix builder joins whatever path it is handed and has no way to know which
entry came from an H1. Near-miss titles (`"Train an LLM from scratch"` vs the
file's `"Train LLM From Scratch"`) are also collapsed, because the test is
structural — did this come from the H1 — not string equality.

`video_title` and `channel` are optional parameters, not read from disk —
`transcript.md`'s frontmatter carries `video_id`/`url`/`language` only, while
`title`/`channel` live in the sibling `summary.md` (`agents/youtube/index.py:_FIELDS`).
Whichever caller has that metadata passes it in.

**Verification**

Manual — call it directly with the cases the template table has to cover,
and read each string:

```
python3 -c "
from agents.knowledge.chunk import build_prefix
print(repr(build_prefix('article', 'Doc Title', ['Intro', 'Sub'])))
print(repr(build_prefix('article', '', ['Intro'])))
print(repr(build_prefix('transcript', 'Video', ['Section'], start='12:34', end='15:02', channel='Chan')))
print(repr(build_prefix('transcript', 'Video', [], start='0:00', end='2:00', channel='')))
"
```

Read for: the full article case reads `Doc Title > Intro > Sub`; missing
title degrades to `Intro`, not `' > Intro'`; the full transcript case reads
`Video — Chan [12:34–15:02] > Section`; missing channel and empty heading
path degrades to `Video [0:00–2:00]` with no stray ` — ` or ` > `.

#### Step 7 — Public API and candidate table

```python
def chunk_article(text: str, doc_title: str = "", *, use_llm: bool = True) -> dict
def chunk_transcript(text: str, *, video_title: str = "", channel: str = "", use_llm: bool = True) -> dict
def format_candidates(result: dict) -> str
def invoke(path: str, kind: str = "article", title: str = "", channel: str = "", use_llm: bool = True, **_kwargs) -> dict
def main() -> int
```

Both chunkers return:

```python
{"ok": True,
 "source_kind": "article" | "transcript",
 "backend": "omp" | "anthropic" | "fallback" | "passthrough",
 "headings_inserted": int,
 "structured_text": str,
 "chunks": [{"chunk_index": int,      # 0-based, position in document order
             "prefix": str,
             "content": str,          # prefix + "\n\n" + body, exactly what gets stored
             "chars": int,
             "over_cap": bool,
             "start": str | None,     # transcript only
             "end": str | None}]}
```

`backend: "passthrough"` means the LLM ran and returned `[]` — already well
structured. `"fallback"` means no LLM result was usable.

`format_candidates` renders the table Layla shows before any write: index, chars, an
`!` marker when `over_cap`, the prefix, and the body's first ~60 chars. This is the
confirm step — `chunk.py` never writes to a database and has no write path at all.

CLI matches every sibling module (`agents/youtube/AGENTS.md:16`: prints JSON to
stdout, exit 0 on success, 1 on failure), with `--kind`, `--title`, `--channel`,
`--no-llm`, and `--table` to print `format_candidates` instead of JSON.

Empty or whitespace-only input returns
`{"ok": False, "error": "empty document"}`; a missing path returns
`{"ok": False, "error": "file not found: <path>"}`. Neither raises.

**Verification**

Manual — this is the full pipeline through the public API and CLI, what
everything above ships to. The steps above verify their own internals;
this verifies the assembled result. Run from the repo root. Fixture
confirmed present: the transcript is 544 lines / 22,106 chars with 534
`[MM:SS]` segments (auto-generated captions — the hardest boundary case).
`omp` must be on PATH for checks 1 and 3.

`data/articles/train-llm-from-scratch.md` (30 real headings, densely
covers the whole document) is the fixture for checks 1-2, replacing the
removed `LESSONS-ai-native-sdlc-playbook.md`. One difference: `points`
isn't guaranteed to be exactly `[]` on this fixture even when nothing's
wrong — 2 of 3 identical runs gave `[]`, one gave a single extra heading
splitting a legitimately marginal sub-topic (see Step 3's "`[]` is real
but not deterministic" note above). Calibrate check 1 accordingly:

1. **Already-structured article passes through, with at most trivial
   additions.**
   `python3 -m agents.knowledge.chunk agents/knowledge/data/articles/train-llm-from-scratch.md --kind article --title "Train LLM From Scratch" --table`
   Expect `backend: "passthrough"` (if `headings_inserted: 0`) or
   `"llm"` with `headings_inserted` of 1 (its real headings already cover
   essentially all of it — treat 2+ as a real regression, not noise),
   every `chars` ≤ 7,000, and each prefix reading `Train LLM From Scratch >
   <one of its real headings>`.

2. **Deterministic path produces a superset split.**
   `KNOWLEDGE_LLM=off python3 -m agents.knowledge.chunk agents/knowledge/data/articles/train-llm-from-scratch.md --kind article --title "Train LLM From Scratch"`
   Expect `backend: "fallback"` and a chunk count equal to or one more
   than check 1's (depending on whether check 1's run added the marginal
   heading) — the article's own headings drive the split either way, so
   the LLM step changing nothing beyond that one optional heading must be
   observable, not assumed.

3. **Structureless transcript gains headings and timestamps.**
   `python3 -m agents.knowledge.chunk agents/youtube/data/videos/ow1we5PzK-o/transcript.md --kind transcript --table`
   Expect `headings_inserted` ≥ 5, roughly 12-16 chunks from 22,106 chars,
   every chunk carrying `start`/`end` `[MM:SS]` strings with `start`
   strictly increasing across `chunk_index`, and each prefix ending in an
   inserted section title. No chunk body may begin or end
   mid-`[MM:SS]`-line — grep each `content` for `\[\d+:\d\d\]` and confirm
   every match sits at a line start.

4. **No text lost, added, or duplicated** (proves both the
   anti-hallucination guarantee of step 2 and zero overlap):
   ```
   python3 -c "
   from agents.knowledge import chunk
   r = chunk.invoke('agents/youtube/data/videos/ow1we5PzK-o/transcript.md', kind='transcript')
   bodies = ''.join(c['content'].split(chr(10)+chr(10), 1)[1] for c in r['chunks'])
   import re
   norm = lambda s: re.sub(r'\s+', ' ', re.sub(r'(?m)^#{1,6} .*$', '', s)).strip()
   src = norm(open('agents/youtube/data/videos/ow1we5PzK-o/transcript.md').read().split('---',2)[2])
   print('MATCH' if norm(bodies) == src else 'MISMATCH')
   "
   ```
   Must print `MATCH`. A mismatch means the splitter dropped or duplicated
   source text, or the model's output leaked into content.

5. **Transcript survives with no backend.**
   `KNOWLEDGE_LLM=off python3 -m agents.knowledge.chunk agents/youtube/data/videos/ow1we5PzK-o/transcript.md --kind transcript --table`
   Expect `backend: "fallback"`, still-valid chunks under the cap with
   correct timestamp ranges, and prefixes carrying the timestamp range
   with no section title. Check 4's identity must still print `MATCH`
   under this backend.

6. **Bad model output is caught, not trusted.** Call
   `chunk.validate_points` directly against the units of any fixture, with
   a hand-built list containing an out-of-range `before_unit`, a point
   whose `anchor` matches the unit three positions below its stated
   `before_unit`, and a duplicate index. Expect the out-of-range point
   dropped, the drifted point silently moved to the matching unit, the
   duplicate collapsed, and a returned message naming the dropped point
   specifically rather than a generic failure string.

### Critical files & anchors

|Path|Anchor|Why|
|---|---|---|
|`agents/youtube/agent.py`|`invoke()` at 121-158, `build_system_prompt()` at 100-118|The exact `omp -p` flag set, timeout/guard pattern, file-output contract, and retry-feedback wording to copy in `heading_agent.py`.|
|This file|Decisions 1-13 vs. 14-19 above|Decisions 1-13 predate this build spec; 14-19 settle and extend decision 14 for it. None of decisions 1-19 are open for re-litigation.|
|`agents/youtube/AGENTS.md`|Lines 116-122|Transcript format guarantees: one `[MM:SS]` segment per line, rolling captions already collapsed. The transcript parser depends on both.|
|`agents/youtube/index.py`|`parse_frontmatter()` at 32-38, `_FIELDS` at 29|Frontmatter loop to mirror, and confirmation that `title`/`channel` live in `summary.md`, not `transcript.md`.|
|`agents/youtube/PLAN.md`|Lines 94-113|Established scoped-feedback retry contract that `validate_points` messages must satisfy.|

### Assumptions & contingencies

- **Haiku-tier model string is `anthropic/claude-haiku-5`** for the `omp` backend
  (`agents/youtube/agent.py:30` uses `anthropic/claude-sonnet-5`, so the tier naming
  is inferred, not verified). If `omp` rejects it, set
  `DEFAULT_MODEL = "anthropic/claude-sonnet-5"` and keep `DEFAULT_EFFORT = "low"`;
  the call is one small JSON emission, so effort matters more than tier.
- **The `anthropic` SDK model id is `claude-haiku-4-5`.** If the SDK rejects it, list
  available ids and take the cheapest haiku-tier one. Do not add `anthropic` to
  `requirements.txt` — the import is lazy and the backend is opt-in, so the repo's
  single-dependency, API-key-free default is preserved.
- **`omp` accepts `--tools read,write` with a temp-file output contract**, verified in
  use at `agents/youtube/agent.py:135`. If the heading model proves able to write
  valid JSON only to stdout, parse stdout by scanning from the first `[` to its
  matching `]` and keep the one-retry-then-fallback policy unchanged.
- **Target ~1,800 chars** is tuned for retrieval sharpness over context richness. If
  check 3 yields chunks that read as fragments mid-argument, raise `TARGET_CHARS` to
  2,600 (~650 tokens); do not add stored overlap, since read-time neighbor expansion
  (decision 15) is the mechanism for that problem.
- **Resolved (2026-09-06, `embed.py` build), revised (2026-09-23,
  embedding migration): over-limit input is truncated by the model, never
  an error.** Voyage's `truncation` parameter defaulted to `True` and
  `embed.py` left it there. EmbeddingGemma behaves the same way: input past
  its 2,048-token window is truncated by the model, so no manual
  pre-truncation guard is needed. `content` in the database is untouched
  either way — only the embedding call ever sees a truncated copy. The
  margin is now much thinner: the window fell from 32,000 tokens to 2,048,
  and `MAX_CHARS = 7000` (~1,750 tokens) is what keeps this from
  triggering. `MAX_CHARS` cannot be raised without rechecking this.

## Status

`chunk.py` is complete: steps 1-2 (unit segmentation), 4 (validation and
repair), 5 (structural split), 6 (context prefix), and 7 (public API, CLI,
candidate table) are built and verified against real fixtures.
`heading_agent.py` (step 3), `judge.py`, and `eval.py` were built earlier —
see the Repo layout note under Step 3 for why the latter two exist.
`db.py`, `embed.py`, `triage.py`, `ingest.py`, and `search.py` are also
built (schema/connection helper, local embedding provider, snippet
writer, document capture, hybrid retrieval), as is `agents/knowledge/AGENTS.md`.
`summarizer_agent.py` was dropped with decision 7 and will not be built.

**Verification results (2026-09-06, all seven Step 7 / Step 4-6 checks):**

| Check | Result |
|---|---|
| Step 4 repair | Out-of-range point dropped and named in the message; drifted anchor moved 5 → 8; duplicate collapsed |
| Step 4 failure paths | Unmatched anchor and oversized section both produce scoped messages naming the offender, never a generic string |
| Step 5 fences | 37 real fences in `train-llm-from-scratch.md`, each wholly inside exactly one chunk |
| Step 6 prefixes | All four template cases exact, including the degraded ones (`Intro`, `Video [0:00–2:00]`) |
| Step 7 check 1 | `backend: "omp"`, `headings_inserted: 1`, 32 chunks — the one addition is the diagram color-legend paragraph the note above predicted |
| Step 7 check 2 | `backend: "fallback"`, 31 chunks — exactly one fewer than check 1, as calibrated |
| Step 7 checks 3-5 | Transcript: `backend: "omp"`, 32 headings, 33 chunks, no chunk over the cap (max 1,572 chars), `start` strictly increasing, every `[MM:SS]` at a line start; `KNOWLEDGE_LLM=off` gives 13 valid fallback chunks |
| Step 7 check 4 | Text identity `MATCH` on both fixtures, both backends, for `structured_text` *and* the concatenated chunk bodies |

**Two bugs found and fixed by these runs, not by review:**

- `heading_agent.TIMEOUT_SECONDS` — the hardcoded 180s subprocess timeout
  killed the call on the 534-unit / 22,106-char transcript, which then
  cascaded into a `fallback` with zero headings. The whole document is one
  call, so the ceiling scales with document length; now 600s, and the
  observed run takes 100-270s.
- Inserted headings nested under each other. `split_sections` built its path
  as `path[:1] + [heading]`, so the first insertion became `path[0]` and
  every later one hung beneath it (`Goal: ... > Human attention ...`).
  Inserted headings are always level 2, so the path is now composed from the
  document's *own* heading path (`own_path[:1] + [heading]`), tracked
  separately. Verified: transcript insertions are siblings, and an insertion
  interrupting a `###` section still reports depth 2 under the `#` title.

**Naming note for whoever reads Step 7 check 1:** it says to expect
`backend: "llm"`. That value does not exist — the result schema in the same
step lists `"omp" | "anthropic" | "fallback" | "passthrough"`, and the
implementation returns the actual backend id (`"omp"`). The check's intent is
unchanged: a live model run that inserted something.

**`db.py` built** (schema + connection helper; three FTS5 sync triggers not
spelled out in the schema decisions were added — external-content FTS5
tables don't self-maintain). **`embed.py` built** (Voyage provider via
stdlib `urllib`, no new HTTP dependency; `numpy` added as the project's
second pip dependency, pinned to 2.0.2 for this environment's Python 3.9 —
2.1+ needs 3.10+). Verified: schema idempotent, FTS insert/update/delete
sync, FK enforcement, blob round-trip, all non-network failure paths, and
the HTTP request/retry/mismatch-detection logic against a mocked Voyage
response. **Not verified: a real Voyage API call** — no `VOYAGE_API_KEY` in
this environment yet.

**`triage.py` built** — `write_excerpt`/`write_synthesis`/`write_rejection`
for a single confirmed snippet, `write_chunks` for the "keep the whole
document" path straight from `chunk.py`'s `chunks` list (one batched
`embed.embed()` call per document, not one per chunk), `set_status`/
`discard_document` for the document-level triage call. Verified: missing
document and empty content both rejected without writing or embedding;
document existence is checked before embedding, so a doomed write never
spends an API call; a failed embed (mocked and via `KNOWLEDGE_EMBED=off`)
still writes the row with `embedding=NULL`, immediately FTS-searchable;
`write_chunks` makes exactly one embed call for N chunks with vectors
landing on the correct rows; end-to-end check with real `chunk.py` output
(8 chunks from the LangGraph fixture) round-tripped through `write_chunks`
with matching row count and FTS hits.

**`ingest.py` built** — `capture()` creates a `documents` row from
already-obtained text (it does not fetch anything itself — see the design
note in "Repo layout" above), dedupes by `url` before inserting, and
`set_summary()` records the discussion summary later. Verified: bad
`source_type` and empty text both rejected without writing; a second
`capture()` with a known `url` returns the existing `document_id` with
`duplicate: True`, writes no second row, and leaves the original
`raw_text` untouched even when called with different text; `set_summary`
rejects a missing document and an empty summary; full chain verified
end-to-end — `ingest.capture()` → `triage.write_excerpt()` → FTS hit.

**`search.py` built** — `search(conn, query, limit=10)` runs FTS5 (BM25)
and brute-force cosine separately, fuses the two rank orders with
Reciprocal Rank Fusion, expands each hit with its stored neighbors
(decision 15), attaches source title/url/date. `backend` reports
`"hybrid"` / `"fts_only"` / `"vector_only"` / `"none"` — honestly, per
which side(s) actually contributed, not just whether vector search ran.

**Revised (2026-09-06):** decision 3's temporal boost is cut — see the
revision note under decision 3 itself. Real query phrasing doesn't
include date language, so phrase-detection would guard a case that
doesn't occur.

**One bug found and fixed by testing, not review:** the first cut labeled
`backend` `"hybrid"` whenever vector search found anything, regardless of
whether FTS contributed — a malformed FTS query (stray quote) silently
failed, vector search alone carried the result, and it was still reported
as `"hybrid"`. Fixed: the label now checks both sides' actual candidate
lists, not just vector's.

**Verified against real data and the real Voyage API** (not mocked):
ingested and chunked a real article, embedded all 8 chunks, searched
against it plus an unrelated second document — results correctly excluded
the unrelated document; RRF fusion matches the hand-worked example from
design discussion exactly; a natural-language query sharing no exact
words with the source text still found it via `vector_only` (proof
hybrid search does something FTS alone can't); a malformed FTS query
degrades to `vector_only` instead of crashing; `KNOWLEDGE_EMBED=off`
degrades to `fts_only`; a query matching nothing anywhere returns `"none"`
with zero results, not an error; neighbor expansion attaches real
adjacent-chunk content; a discarded neighbor's gap never falls back to
`raw_text`; a snippet with a stale `embedding_model` is correctly
invisible to vector search while staying keyword-searchable.

**Next: nothing unbuilt.** `agents/knowledge/AGENTS.md` (the domain workflow
instructions Layla actually reads) is written, and `summarizer_agent.py` was
dropped with decision 7. The domain is complete for the single-article
workflow it serves.

**Settled (2026-09-20):** `judge.py` is an offline audit, never a
synchronous gate — see decision 20. Nothing in `chunk.py` calls it, and
nothing should.

**Fixture gap closed**: `data/articles/train-llm-from-scratch.md` (30 real
headings, densely covers the whole document) now serves Step 3 check 2 and
Step 7 checks 1-2 — see those checks and Step 3's "`[]` is real but not
deterministic" note for the calibrated (not exact-`[]`) expectation.

**Embedding migration executed (2026-09-23):** Voyage → EmbeddingGemma-300M
Q8_0, local via the ollama daemon. `embed.py` rewritten — `_embed_voyage`,
`_post`'s retry loop, `API_URL`, `_RETRY_STATUSES` and the `VOYAGE_API_KEY`
read all deleted; no `voyage` string survives in any `.py` file.
`_load_dotenv()` was kept against the migration plan's instruction: it is
generic, not provider-specific, and is the only loader of
`ANTHROPIC_API_KEY` from `.env` for `heading_agent.py`'s optional backend.
`triage.reembed()` added and run: 46/46 snippets migrated in 10.4s, one
distinct `embedding_model`, zero NULLs. Verified end to end: 768-dim
non-zero vectors; document and query prefixes produce different vectors for
the same sentence (cosine 0.85, so the prefixes are load-bearing);
`cos(wool, linen) = 0.577` against `cos(wool, tax) = 0.249`; lists longer
than `MAX_BATCH` slice across requests with a text at index 0 and index 65
embedding identically; a stopped daemon returns a named error and leaves
rows untouched rather than half-written; `KNOWLEDGE_EMBED=off` still
short-circuits with no network call. Retrieval re-verified with the same
query used for the original Voyage backfill — `backend: hybrid`, document
6's Linen chunk ranked first. Absolute RRF scores are not comparable across
providers; ordering is.