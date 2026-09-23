# Layla

A personal learning system: read an article or watch a talk, discuss it with an
agent that has the full text in context, keep only the parts worth keeping, and
find them again months later by meaning or by word.

Layla is not a chatbot wrapper and not a framework. It is one agent
(`AGENTS.md`), eight plain Python modules, and a single SQLite file. No router
process, no scheduler, no vector database, no dependencies beyond `numpy` and a
caption library.

---

## What it does

```
article URL / pasted text / YouTube URL
  → capture into documents (raw text kept pristine)
  → discuss it in conversation
  → you choose what to keep
  → chunk, embed, index
  → hybrid search returns it later, with sources
```

The design bet is that **curation is the scarce resource, not storage.** Every
system that logs everything ends up with a haystack. Here, nothing enters the
searchable corpus without an explicit decision: the agent proposes exact text,
shows it, and writes only on confirmation.

## What's interesting in here

- **Structural chunking with LLM-proposed headings.** Unstructured text (a raw
  transcript, an article with no headings) goes through a model that emits
  *insertion points only* — never rewritten text — addressed by unit index and
  anchor-verified with bounded repair. The splitter itself is deterministic and
  never splits a code fence or a sentence.
- **Hybrid retrieval, fused by rank.** FTS5/BM25 for literal terms and
  brute-force cosine over local EmbeddingGemma embeddings for meaning, combined
  by Reciprocal Rank Fusion because BM25 and cosine live on incomparable scales.
- **Read-time neighbor expansion instead of stored overlap.** A hit joins its
  adjacent chunks at query time, so the index holds one copy of every passage,
  the window is retunable without re-embedding, and discarded material can
  never leak back in through a neighbor.
- **Honest degradation everywhere.** A failed embed writes the row with a NULL
  vector (still keyword-searchable). A failed heading model falls back to the
  document's own structure. A malformed keyword query degrades to vector-only
  and says so in the response.
- **A decision log that argues with itself.** `docs/decisions.md` records 21
  numbered decisions, including the ones that were reversed and why —
  a temporal-ranking boost cut for guarding a case that never occurs, a bulk
  import path deleted before it was built, a heading grader kept deliberately
  off the production path.

## Quick start

```bash
pip3 install -r requirements.txt
brew install yt-dlp ollama          # video captions + metadata; embedding model host
ollama pull embeddinggemma:300m-qat-q8_0   # ~340MB, once; no API key anywhere

# split a document into candidate snippets and show the confirm table
python3 chunker.py path/to/article.md --kind article --title "Title" --table

# fetch a video's captions and metadata
python3 youtube.py "https://www.youtube.com/watch?v=..." --out /tmp/t.md
```

Everything else is a library call the agent makes during a conversation:

```python
import db, ingest, triage, search

conn = db.connect()
doc = ingest.capture(conn, text, "url", url=..., title=...)
triage.write_excerpt(conn, doc["document_id"], "the exact text you confirmed")
search.search(conn, "how does the chunker decide boundaries",
              terms=["chunker", "boundaries", "split"])
```

Running without an embedding key still works: set `KNOWLEDGE_EMBED=off` and
retrieval degrades to keyword-only rather than failing.

## Layout

| Path | Role |
|---|---|
| `AGENTS.md` | The agent's whole instruction set — tools, workflows, output contract |
| `db.py` | Schema (`documents`, `snippets`, FTS5 index kept in sync by triggers) |
| `ingest.py` | Capture text into a `documents` row; dedupes by URL. Never fetches |
| `chunker.py` | Segment → propose headings → validate → split → context prefix |
| `heading_agent.py` | The heading-insertion model call; pluggable backend |
| `triage.py` | The only write path into `snippets`; batch-embeds |
| `embed.py` | Local EmbeddingGemma via ollama, float32 blob codec, `.env` loader |
| `search.py` | BM25 + cosine, RRF fusion, neighbor expansion |
| `youtube.py` | Captions (two sources with fallback) + video metadata |
| `dev/` | Offline heading-quality grader and its manual tuning harness |
| `docs/` | Decisions, system overview with diagrams, YouTube notes, research |

Start with `docs/system-overview.md` — it has numbered diagrams for ingestion,
chunking, and retrieval.

## Deliberate non-goals

- **No vector database.** Brute-force cosine over a few thousand snippets is a
  single matrix multiply — benchmarked at ~13ms and ~60MB at 5,000 rows.
  `sqlite-vec` is a scale problem this does not have.
- **No agent framework.** LangGraph, Google ADK, and Mem0 were each read and
  rejected for concrete reasons recorded in `docs/research.md` — mostly wrong
  problem shape (many strangers, disposable threads) and mandatory paid API
  calls on every write.
- **No automatic extraction.** Nothing is summarized into memory behind your
  back. Rejections are stored too, as a distinct kind, so a later document
  repeating a claim you disagreed with surfaces the disagreement rather than
  reading back as an endorsement.
- **No scheduler.** Results are only read when you sit down.

## Status

Used for real, single-user, on macOS with Python 3.9. The retrieval path,
capture path, and chunker are all exercised against real documents. Embeddings
run locally — no embedding API key, no network call. Frame extraction from
video is designed but unbuilt; the plan and the schema it needs are in
`docs/youtube.md`.
