# Knowledge domain — system overview

*agents/knowledge/ · 8 modules + 1 SQLite file · written 2026-09-20*

Bird's-eye view of the architecture and data flow. Design rationale and the
numbered decision log live in `pipeline/PLAN.md`; the workflow Layla follows
lives in `AGENTS.md`. This file is the map, not the rules.

Diagram boxes are numbered so they can be referenced by number.

---

## 1. The shape of the system

You and Layla hold the conversation; eight plain Python modules hold the
plumbing; one SQLite file holds everything permanent. Nothing runs on its
own — every module is a library call Layla makes during a conversation, and
text only becomes searchable when you say so.

Three stages: **capture → triage → retrieve**, with a chunker hanging off the
middle.

- Capture puts text into `documents` unchanged.
- Triage decides what is worth keeping; only that lands in `snippets`.
- Retrieval reads `snippets` and never touches the archive.

---

## 2. Ingestion flow

```mermaid
flowchart TD
    A1["1. You paste text or a link"] --> A2["2. Layla reads it, gives the gist"]
    A2 --> A3["3. ingest.capture()"]
    A3 --> A4[("4. documents<br/>raw_text, pristine")]
    A2 --> A5["5. Q&A in conversation"]
    A5 --> A6{"6. You decide"}
    A6 -->|keep whole| A7["7. chunk.py --table"]
    A6 -->|keep a part| A8["8. triage.write_excerpt()"]
    A6 -->|answer worth keeping| A9["9. triage.write_synthesis()"]
    A6 -->|claim is wrong| A10["10. triage.write_rejection()"]
    A6 -->|nothing| A11["11. triage.discard_document()"]
    A7 --> A12["12. You confirm the chunk table"]
    A12 --> A13["13. triage.write_chunks()"]
    A8 --> A14["14. embed.py → Voyage voyage-4"]
    A9 --> A14
    A10 --> A14
    A13 --> A14
    A14 --> A15[("15. snippets<br/>+ snippets_fts")]
    A13 --> A16["16. ingest.set_summary()"]
    A16 --> A17["17. triage.set_status(): kept | partial | discarded"]
```

Box 14 is best-effort. A failed embed writes the row with `embedding = NULL`
and reports the error — the snippet stays fully keyword-searchable and can be
backfilled later. It never blocks the write.

---

## 3. Inside the chunker (box 7)

Five steps in one file. It writes nothing; its output is a table you approve.

```mermaid
flowchart LR
    C1["1. segment_units()<br/>cut into blocks"] --> C2["2. heading_agent.insert_headings()<br/>propose structure"]
    C2 --> C3["3. validate_points()<br/>drop bad points, one retry"]
    C3 --> C4["4. split_sections()<br/>cut at every heading"]
    C4 --> C5["5. split_body()<br/>group to ~1,800 chars"]
    C5 --> C6["6. build_prefix()<br/>Title > Section"]
    C6 --> C7["7. candidate chunks<br/>(nothing written)"]
```

- Box 2 is the only LLM call in the ingest path — one call over the whole
  document, up to several minutes on a long transcript. `--no-llm` skips it
  and splits on the document's own headings instead.
- Box 3 is structural validation only: in-range, anchor matches, no
  duplicates. Heading *quality* is never graded here — see section 6.
- Box 6 drops the document's own H1 when a title was supplied; both fill the
  title slot, and repeating it wastes prefix chars in every embedded chunk.

---

## 4. Retrieval flow

```mermaid
flowchart TD
    B1["1. You ask: remind me what we learned about X"] --> B2["2. Layla strips it: query + terms"]
    B2 --> B3["3. search.search(conn, query, terms, limit=10)"]
    B3 --> B4["4. pool = max(limit*5, 50)"]
    B4 --> B5["5. _fts_ranked_ids: keyword side"]
    B4 --> B6["6. _vector_ranked_ids: meaning side"]
    B5 --> B7{"7. AND all terms → rows?"}
    B7 -->|no rows| B8["8. retry with OR"]
    B7 -->|rows| B9["9. BM25 rank order"]
    B8 --> B9
    B6 --> B10["10. embed query via Voyage"]
    B10 --> B11["11. load rows where embedding_model matches"]
    B11 --> B12["12. cosine scan in numpy → rank order"]
    B9 --> B13["13. _rrf_fuse: 1/(60+rank) summed"]
    B12 --> B13
    B13 --> B14["14. top N by fused score"]
    B14 --> B15["15. join documents for title/url/date"]
    B15 --> B16["16. _expand_neighbors: chunk_index ±1"]
    B16 --> B17["17. backend label: hybrid | fts_only | vector_only | none"]
    B17 --> B18["18. Layla synthesizes an answer, cites the source"]
```

- **Two searches, opposite blind spots.** Box 5 finds literal words and is
  blind to paraphrase; box 6 finds meaning and can miss a rare literal token.
- **Boxes 7→8 exist because FTS5 ANDs bare terms.** A sentence-shaped query
  demanded its function words too and matched nothing, so every real query
  silently ran `vector_only`. Layla passes content words as `terms`; all terms
  are required first, any term on the second pass.
- **Box 13 fuses ranks, not scores.** BM25 is unbounded and corpus-dependent,
  cosine is in [-1, 1] — incomparable, so only rank position is used.
- **Box 11 filters by `embedding_model`.** A row from a different model is
  invisible to vector search but still keyword-reachable; vectors from
  different spaces are never compared.
- **Box 16 never reads `raw_text`.** A chunk you discarded leaves a gap, so
  discarded material can never leak back through neighbor expansion.

---

## 5. Module map

| Module | Role | Called by |
|---|---|---|
| `db.py` | Schema + connection helper. Applies schema on first connect. | every module |
| `ingest.py` | Writes the `documents` row. Never fetches, never calls an LLM. | Layla |
| `chunk.py` | Splits a document into ~1,800-char candidate chunks with context prefixes. Only module with a CLI. | Layla |
| `heading_agent.py` | Proposes heading insertion points for unstructured text. | `chunk.py` only |
| `triage.py` | Writes `snippets`, batch-embeds them. The only write path into the corpus. | Layla |
| `embed.py` | Voyage `voyage-4`, float32 blob codec, `.env` key loading. | `triage.py`, `search.py` |
| `search.py` | BM25 + cosine, fused by RRF, neighbor expansion, source metadata. | Layla |
| `judge.py` / `eval.py` | Offline grading of heading quality. Dev-only. | nothing in the workflow |

---

## 6. What is deliberately absent

- **No synchronous quality gate.** `judge.py` never runs on the ingest path
  (decision 20) — it would add a second whole-document LLM call and couple
  two independent failures. You already gate chunk boundaries by eye at
  box 12 of the ingestion flow.
- **No bulk-import subagent.** `summarizer_agent.py` was dropped (decision 7):
  the workflow is one article at a time, and bulk mode's mechanism — keeping
  source text out of Layla's context — is exactly what discussion needs.
- **No scheduler, no daemon, no router process.** Every arrow in every diagram
  above is either you talking or Layla making a library call.

---

## 7. Two invariants that explain most of the design

1. **`documents` is archive, `snippets` is the index — never the same text
   twice.** `raw_text` is never searched or embedded. Search only ever queries
   `snippets` and joins `documents` back for title, url, and date.

2. **Nothing enters the corpus without an explicit yes, and no failure is
   fatal.** A dead Voyage call writes `embedding = NULL`; a dead heading model
   falls back to the document's own headings; a malformed FTS query degrades
   to `vector_only` instead of raising.
