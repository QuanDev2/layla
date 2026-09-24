# Handoff — Layla

## Status

One agent, flat repo root, one SQLite store. Articles and YouTube videos both
go through the same capture → triage → retrieve path. Used for real: document 1
(Anthropic's "Scaling Managed Agents") was captured, triaged in conversation,
and closed `kept` — 12 snippets, all embedded, searchable.

Nothing is unbuilt except frame extraction, which is designed only.

Full decision log: `docs/decisions.md` (24 numbered decisions, do not
re-litigate). Architecture: `docs/system-overview.md`. Read those before
touching a module, not this file.

## What changed on 2026-09-23

- **Embeddings are local and permanent — Voyage is deleted.** `embed.py` now
  talks to EmbeddingGemma-300M Q8_0 over the ollama daemon at
  `127.0.0.1:11434/api/embed`; no `voyage` string survives in any `.py` file,
  and embedding works with no network and no API key. `triage.reembed()` was
  added and run over the whole corpus — 46/46 snippets migrated in 10.4s to a
  single `embedding_model`, `embeddinggemma-q8`. Retrieval re-verified with the
  query used for the original backfill: `backend: hybrid`, document 6's Linen
  chunk first. Two deviations from `docs/embedding-migration.md`: lists longer
  than `MAX_BATCH` now slice across requests instead of being rejected (a
  70-chunk transcript reaches `write_chunks` as one call, so rejection was a
  new failure mode), and `_load_dotenv()` was kept because it is generic and is
  the only loader of `ANTHROPIC_API_KEY` for `heading_agent.py`.

- **Chapter anchoring is enforced, not instructed.** The rule lived only in
  prose, and `chunker.py`'s `if chapters:` was a consumer-side conditional: a
  call without `--url` was legal, exited 0, and produced plausible chunks with
  wrong headings. It happened on a real document. Two changes close it:
  `chunker.invoke_document(conn, document_id)` reads text, title, author, and
  chapters from the row (no flag to forget, no second yt-dlp call, no network
  at chunk time), and `triage.write_chunks()` refuses a transcript batch whose
  `chapters_used` is false when the video published chapters.
- **`documents.source_metadata`, one JSON blob owned by `metadata.py`**
  (decision 24). Common keys flat (`author`), source-specific keys nested per
  kind (`video: {video_id, duration, chapters}`). Chosen over typed columns so
  a new source type costs no migration. `db.py` gained `_migrate_columns()`
  because `CREATE TABLE IF NOT EXISTS` never alters an existing table.
- **Unrecorded chapters and "creator published none" are now different
  facts.** A failed `video_chapters()` lookup omits the key entirely and is
  refused at both the chunker and the write gate; a chapterless video records
  `[]` and is allowed through. Conflating them is what let the bad path look
  legitimate.
- **`--url` removed from chunker.py.** Its only job was refetching chapters
  that capture now stores.
- **Document 6 (Laura VonV, fabric guide) written with the chapter path** —
  22 snippets, `backend=chapters+omp`, status `kept`. Its embeddings failed
  mid-outage and were backfilled afterward; the corpus is now 46/46 embedded.

## What changed on 2026-09-21

- **Chapters now own transcript chunk boundaries.** `youtube.video_chapters()`
  fetches a video's published chapters; `chunker.chunk_transcript(chapters=…)`
  turns each into a fixed level-2 point the heading model cannot move. The
  model's remit shrank to naming and subdividing: one level-3 subheading per
  chapter, plus extra splits inside a chapter over `TARGET_CHARS`.
  `_chapter_complaints()` audits that contract and feeds the existing retry.
- **The prompt had to name each required block.** The first run left six of
  nine chapters unlabeled; listing "heading required at block N" per chapter
  plus a minimum element count fixed it. Prompt change, not code.
- **Document 3 (Laura VonV, quality clothing) rewritten** with that path —
  9 old snippets deleted, 11 written, all embedded, status `kept`. Prefixes
  now read `Button test > Metal buttons beat plastic in durability`.
- **Silenced a false numpy warning in `search.py`.** Apple's math library
  leaves the chip's error flags set while computing padding lanes it throws
  away; numpy reads them afterward and blames the multiply. Reproduced on
  constant data. `np.errstate` around the multiply only, plus a real
  zero-length-vector guard so a genuine case cannot hide behind it. Remove
  the errstate at numpy >= 2.3.1, which needs Python >= 3.10.

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
  claims max, one `**Verdict:**` line, no emoji. A many-section breakdown
  switches to hierarchical numbering (`5.1` under heading `5.`) with `▸`/`◦`
  glyphs — see "Multi-section breakdowns" in that file.
- **Plain terms by default.** Explanations target a tech enthusiast: no
  acronyms or library names where a plain description works ("Apple's math
  library", not "Accelerate"). Real names only when asked to get technical.
  Asked for N sentences means exactly that — no bullets, no trailing offer.
- **Diagrams carry numbered boxes** so they can be referenced by number later.
- **Docstrings:** imperative one-liner, then `In:` / `Out:` / `State:`
  fragments. No history, no rationale prose.

## Built

| Path | Does |
|---|---|
| `db.py` | Schema (`documents`, `snippets`, `snippets_fts`, `entities`, `observations`) + connection helper; FTS5 kept in sync by triggers |
| `ingest.py` | Captures already-fetched text into a `documents` row with its source metadata blob, dedupes by URL |
| `metadata.py` | Owns `documents.source_metadata`: builds it from a fetcher result, reads back author and chapters, keeps "unrecorded" distinct from "none published" |
| `chunker.py` | Segments a document, proposes headings via a pluggable LLM backend, splits into ~1,800-char chunks with context prefixes; `invoke_document()` reads a captured document's chapters from its row and fixes boundaries there |
| `heading_agent.py` | The heading-proposal call chunker.py wraps (omp \| anthropic \| off); works inside fixed chapters when passed them |
| `triage.py` | Writes confirmed excerpts/synthesis/rejections/chunks into `snippets`, batch-embeds; refuses a transcript batch that ignored published chapters |
| `embed.py` | EmbeddingGemma-300M Q8_0 over local ollama HTTP, task prefixes, float32 blob codec, `.env` loader |
| `search.py` | Hybrid FTS5 + cosine, fused by RRF, neighbor expansion; takes caller-extracted `terms` |
| `youtube.py` | Captions with two-source fallback + title/channel/duration + published chapters via yt-dlp |
| `entities.py` | Registry of people/things with alias resolution; `resolve` returns candidates, never picks |
| `observations.py` | Exact-lookup personal memory: structured `attribute`/`value` or prose `body`, superseded rather than overwritten |
| `dev/judge.py`, `dev/eval.py` | Dev-only heading grading harness; nothing in the workflow calls them |

## Key decisions worth knowing before touching this

- **SQLite, not a vector database.** Brute-force cosine in numpy beats an ANN
  index at low-thousands scale — benchmarked ~13ms/~60MB at 5,000 rows.
  Revisit (`sqlite-vec`) only past tens of thousands of rows.
- **Local embeddings, EmbeddingGemma-300M Q8_0 via ollama.** No API key, no
  network. `OLLAMA_MODEL` pins the explicit tag `embeddinggemma:300m-qat-q8_0` —
  bare `embeddinggemma` resolves to the BF16 build and a different vector
  space. Stored `embedding_model` is `embeddinggemma-q8` and names the
  quantization because `search.py` uses it as the cross-space guard. Task
  prefixes (`title: none | text:` / `task: search result | query:`) are applied
  inside `_embed_local()`, never at call sites — omitting them measurably
  degrades retrieval. Context window is 2,048 tokens, down from Voyage's
  32,000. `MAX_CHARS = 7000` does **not** guard it — measured at 2.45
  chars/token on timestamped transcripts, 7,000 chars is ~2,850 tokens.
  `embed.MAX_INPUT_CHARS = 4800` is the real ceiling and `over_cap` uses
  it; `MAX_CHARS` is only the section-subdivision threshold.
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

Every module was verified against real fixtures and a live embedding model. On
2026-09-20, post-flatten: existing document 1 still searches `backend: hybrid`;
`chunker.py` CLI produces 31 chunks on the article fixture with prefixes 25
chars shorter than pre-fix; and a full video round-trip ran end to end —
`youtube.py` fetched real captions plus title/channel, capture → chunk →
`write_chunks` embedded 1/1, and the snippet came back through hybrid search.

Personal memory (2026-09-21) smoke-tested against invented people in an
in-memory database: alias resolution ("mom" → Nora), duplicate detection on
create, ambiguity returning multiple candidates, structured and prose writes,
both validation failures, `replace` superseding exactly one row, history
readable with `include_superseded`, and an empty lookup returning zero rather
than a guess. Schema applied to the live database — 2 documents and 13
snippets intact, existing hybrid search unaffected. Not yet exercised in a
real conversation.

Chapter chunking (2026-09-21) verified against the real video end to end:
`video_chapters()` returned 9 published chapters; boundaries mapped to the
exact caption segments (`Lining` → block 155 → `[05:40]`); the full run came
back `backend=chapters+omp` with 19 points and 11 chunks, every chapter
labeled and only the 3,149-char "Fabric Quality" subdivided. Degradation
checked too: `--no-llm` with chapters gives `backend=chapters` and 10 chunks,
a chapterless transcript still gives 7, the article fixture still gives 8.
After the rewrite, "how to tell if denim buttons are cheap" returns the
`Button test` snippet first, `backend: hybrid`.

## Next steps

- Personal memory is built and holds its first real row — the `self` entity
  and a `preferred_clothing_brands` observation. Otherwise untested in
  conversation.
- Frame extraction remains designed-but-unbuilt: plan and proposed schema in
  `docs/youtube.md`.
- The corpus is 6 documents / 46 snippets (4 documents carry snippets; 2 were
  discarded). Retrieval was measured on the local model 2026-09-23 — 46/46
  self-retrieval at rank 1, 12/12 paraphrase queries in the top 3, 6/8
  cross-document discrimination with both misses being ambiguous labels
  rather than bad ranking. Still untested: a second creator on the same
  topic. `d3` and `d6` are both Laura VonV, so ranking across sources has
  never actually been exercised.
- `dev/judge.py` has never been run against the chapter path; heading quality
  there is unmeasured.

## Parked: identity and identifiers (2026-09-21, paused mid-discussion)

Where the reasoning got to, so it doesn't restart from zero:

- **`full_name` stays nullable and unsplit.** No first/last columns — name
  structure varies by culture and the split is a classic data bug. `name` is
  the label the user says; `full_name` is the written form when known.
  Requiring it would tax the moment of capture, which must stay cheap.
- **Identifiers are join keys, not descriptions.** A shoe size describes a
  person; an email address points at them in another system. Different job,
  so eventually a different table.
- **For now they live as observations** — `attribute='email'`,
  `attribute='phone'`. Zero schema change, handles multiple values, inherits
  supersede when a number changes.
- **Rejected: email/phone columns on `entities`.** Breaks on a second
  address, and invites an endless column list (Signal, GitHub, Instagram).
- **The trigger for a real `identifiers` table is enforcement, not storage.**
  Sketch: `identifiers(entity_id, system, value, is_primary,
  UNIQUE(system, value))`. That unique constraint is what observations
  cannot provide, and it only matters when something inbound — an email, an
  SMS — has to be matched to an entity. Migration then is a read of
  `attribute IN ('email','phone')` and an insert.
- **Do this cheaply even now:** normalize on the way in — emails lowercased,
  phones E.164 (`+14155551234`) — or the future unique index will reject
  duplicates that differ only textually.

Still undecided: whether the `self` entity should carry the user's real
first name (useful once contacts or messaging integration exists) rather
than the placeholder `me` it was created with.

## Open questions

- How prose observations get searched. Structured lookups need no search;
  prose rows will eventually want FTS or embeddings, which reopens the
  second-ranked-index question. Deferred until there are real rows.
- Whether a frame's snippet stores OCR text verbatim, and whether frames get
  their own `chunk_index` ordering or stay `NULL` like syntheses.
- Whether every numbered decision in `docs/decisions.md` should carry a short
  title, so citations read "decision 13 (documents and snippets have distinct
  roles)" rather than a bare number.
