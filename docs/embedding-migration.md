# Milestone: Voyage → EmbeddingGemma (local, permanent)

Build spec for replacing the embedding provider. Written to be picked up cold:
every fact below was verified on 2026-09-23 against this machine and this
repo. Read `docs/decisions.md` decisions 5, 6, 15 and 24 first — this
milestone executes the "Future migration tasks" entry in that file.

---

## 1. Goal

Replace Voyage AI with EmbeddingGemma-300M running locally through Ollama, and
re-embed the whole corpus into the new vector space. Voyage is removed, not
kept as a fallback.

**In scope:** `embed.py` provider swap, a re-embed job in `triage.py`, the
corpus migration, docs.

**Out of scope:** changing `snippets.content`, chunk sizes, `search.py` ranking
maths, or the `snippets` schema. `embedding` is a derived, regenerable index;
nothing here touches source text.

---

## 2. Current state (verified 2026-09-23)

- **Corpus:** 46 snippets across 6 documents, 46/46 embedded with `voyage-4`,
  0 NULL. Documents 4 and 5 are `discarded`, 6 is `kept` (22 snippets).
- **`embed.py`:** stdlib `urllib` only, no HTTP dependency. `DEFAULT_MODEL =
  "voyage-4"`, `API_URL` Voyage's REST endpoint, `_KNOWN_BACKENDS = ("voyage",
  "local", "off")` — `"local"` is a named-but-unimplemented slot. `_load_dotenv()`
  exists solely to read `VOYAGE_API_KEY`.
- **`search.py` lines 96–99 already guard mixed spaces:** cosine runs only
  against rows whose `embedding_model` equals the model that embedded the
  query. Decision 15's requirement is therefore already satisfied — during the
  gap, search degrades to `fts_only`, never to wrong rankings.
- **`triage.py`:** `_write_one()` and `write_chunks()` both call
  `embed_mod.embed()` and tolerate failure by writing `embedding NULL`. No
  re-embed or backfill function exists anywhere in the repo.
- **Machine:** `ollama` at `/usr/local/bin/ollama`, daemon responding on
  `127.0.0.1:11434`, **no models pulled**. Python 3.9.6. Installed packages:
  `numpy 2.0.2`, `youtube-transcript-api` only — no `torch`,
  `sentence-transformers`, `transformers`, or `llama-cpp`.
- **Ollama registry tags for the model:** `embeddinggemma:latest`,
  `embeddinggemma`, `embeddinggemma:300m`. No quant-specific tag is published.

---

## 3. Locked decisions

1. **Ollama HTTP, not a Python model runtime.** Keeps the stdlib-urllib shape
   `embed.py` already has and adds zero pip dependencies. `torch` +
   `sentence-transformers` is ~2GB and carries Python 3.9 compatibility risk.
2. **Voyage is deleted, not deprecated.** `_embed_voyage`, `_post`, `API_URL`,
   `_RETRY_STATUSES`, and the `VOYAGE_API_KEY` half of `_load_dotenv()` all go.
   Clean cutover per repo convention; no dead fallback path.
3. **One function serves both migration and backfill.** "Rows with a stale
   provider" and "rows with no embedding" are the same query with one predicate
   difference. Do not write a separate backfill.
4. **`embedding_model` is the migration's unit of work and its guard.** Every
   re-embedded row gets both `embedding` and `embedding_model` overwritten in
   the same statement.

---

## 4. Open items — resolve before or during implementation

| Item | Why it matters | How to settle |
|---|---|---|
| Actual quantization of Ollama's `embeddinggemma` | Decision 6 names Q8_0; Ollama publishes no quant tag. Q8_0 scores 69.49 vs Q4_0's 69.31 on MTEB English — a real but small gap | `ollama show embeddinggemma` after pulling; if it is Q4_0 and Q8_0 is wanted, import a GGUF via a Modelfile |
| Value to store in `embedding_model` | It is the cross-space guard, so it must change when quantization changes | Recommend `embeddinggemma-q8` per decision 6 if Q8_0 is confirmed, else `embeddinggemma-q4`. Never plain `embeddinggemma` |
| Output dimensionality | 768 default, MRL truncation to 512/256/128 available | Stay at 768. 46 rows make storage irrelevant, and truncation costs accuracy |
| Ollama endpoint shape | `/api/embed` takes `{"model", "input"}` and returns `{"embeddings": [[…]]}`; the older `/api/embeddings` takes `{"prompt"}` and returns `{"embedding": […]}` | Verify with one curl before writing the client; prefer `/api/embed` (batch-capable) |

---

## 5. Implementation

### 5.1 Pull the model

```bash
ollama pull embeddinggemma        # ~600MB, one time
ollama show embeddinggemma        # record quantization + context length
```

Requires user approval — it is an install.

### 5.2 `embed.py` — replace the provider

Delete `_embed_voyage`, `_post`, `API_URL`, `_RETRY_STATUSES`, and the dotenv
loader. Add:

```python
DEFAULT_MODEL = "embeddinggemma-q8"      # the stored embedding_model value
OLLAMA_MODEL = "embeddinggemma"          # the tag ollama serves
OLLAMA_URL = "http://127.0.0.1:11434/api/embed"
MAX_BATCH = 64                           # local; tune if throughput is poor
EMBED_DIMS = 768
_KNOWN_BACKENDS = ("local", "off")
```

`_embed_local(texts, model, input_type)`:

- Prefix every text per §6 before sending.
- POST `{"model": OLLAMA_MODEL, "input": [...]}`; parse `embeddings`.
- Return the module's existing contract: `{"ok", "vectors", "model",
  "backend": "local"}`, and `{"ok": False, "error": str, "backend": "local"}`
  on failure. **Never raise** — `triage.py` depends on a failed embed being
  non-fatal.
- Map a connection refusal to a clear error naming the daemon (`ollama serve`),
  not a raw urllib traceback.
- Keep `to_blob`/`from_blob` untouched — the blob is width-agnostic.

`embed()` keeps its signature (`texts, model, input_type`) so `triage.py` and
`search.py` need no changes. `KNOWLEDGE_EMBED=off` must keep working for tests.

### 5.3 `triage.py` — the re-embed job

```python
def reembed(conn, document_id: int = None, model: str = None) -> dict:
    """Re-embed rows whose vector is missing or from another model.

    In: open connection, optional document_id to scope the run, optional
        target model (defaults to embed.DEFAULT_MODEL).
    Out: {"ok", "reembedded": int, "skipped": int, "total": int} or
         {"ok": False, "error": str}. A failed batch leaves rows untouched
         rather than half-written.
    State: batched embed.embed() calls of MAX_BATCH; updates embedding and
           embedding_model together.
    """
```

- Selection: `WHERE embedding IS NULL OR embedding_model != ?` (target model),
  plus `AND document_id = ?` when scoped.
- Batch in `MAX_BATCH` slices; commit per batch so a mid-run failure leaves a
  consistent, partially-migrated corpus (safe — `search.py` filters by model).
- `input_type="document"` for every row. Snippet content already carries its
  context prefix; do not re-prefix with the chunk prefix.

### 5.4 Run the migration

```bash
python3 -c "
import db, triage
print(triage.reembed(db.connect()))
"
```

Expect `{"ok": True, "reembedded": 46, "skipped": 0, "total": 46}`.

---

## 6. Prompt prefixes (required, not optional)

EmbeddingGemma is trained with task prefixes; omitting them measurably degrades
retrieval. Map `embed.py`'s existing `input_type`:

| `input_type` | Prefix applied | Source |
|---|---|---|
| `"document"` | `title: none \| text: {content}` | model card, Retrieval (Document) |
| `"query"` | `task: search result \| query: {content}` | model card, Retrieval (Query) |

- Apply inside `_embed_local()`, never at call sites — one owner, same reason
  `metadata.py` owns the blob shape.
- `title: none` is correct here: our chunk content already begins with its own
  context prefix (`Title — Channel [12:34–15:02] > Section`), so injecting the
  document title again would duplicate it.
- `search.py` already passes `input_type="query"`; confirm before relying on it.

---

## 7. Context window — the one real regression risk

Voyage's window was 32,000 tokens; EmbeddingGemma's is 2,048. `MAX_CHARS = 7000`
in `chunker.py` (~1,750 tokens) sits under it, and `TARGET_CHARS = 1800` is far
under. No current snippet is flagged `over_cap`.

Still: after migration, an `over_cap` chunk silently truncates instead of
embedding whole. If chunk sizing is ever raised, this is the constraint that
binds. Do not raise `MAX_CHARS` without re-checking this.

---

## 8. Verification

Run in order; each is a hard gate.

1. **Daemon + model:** `ollama show embeddinggemma` prints a model card.
2. **Dimensionality:** embed one string, assert `len(vector) == 768` and that
   the vector is not all zeros.
3. **Prefix effect (sanity, not a test file):** embed the same sentence as
   `document` and as `query`; the two vectors must differ.
4. **Semantic sanity:** cosine("wool is warm", "linen breathes well") must
   exceed cosine("wool is warm", "quarterly tax filing deadlines").
5. **Migration:** `reembed()` returns 46/46; then
   `SELECT DISTINCT embedding_model FROM snippets` returns exactly one row,
   the new model string.
6. **Retrieval end to end:** `search.search(conn, "which fabrics last longest
   and why", terms=["fabric","durable","wool","linen"])` returns
   `backend: hybrid` with document 6's Linen chunks on top — the same query
   used to verify the Voyage backfill, so results are directly comparable.
7. **Failure path intact:** stop the daemon, write a snippet, confirm it is
   stored with `embedding NULL` and a reported `embed_error` rather than an
   exception.
8. **`KNOWLEDGE_EMBED=off`** still short-circuits without a network call.

No new permanent test files. These are smoke checks run once, per repo
convention (decision 20's reasoning about synchronous gates applies).

---

## 9. Rollback

Reverting the code restores Voyage, but **not the vectors** — the migration
overwrites them. Recovery is re-running `reembed()` with the old model string
while Voyage code is restored, which costs API tokens but no data: `content` is
untouched throughout. There is no state in which text is at risk.

---

## 10. Docs to update on completion

| File | Change |
|---|---|
| `docs/decisions.md` | Mark the "Re-embed job" migration task done; amend decision 5 (Voyage) and 6 (local swap) to record that the swap happened and Voyage is gone |
| `AGENTS.md` | Credentials section: drop `VOYAGE_API_KEY`; `KNOWLEDGE_EMBED` values become `local` (default) / `off`; Environment section gains `ollama` as a required binary beside `yt-dlp` |
| `docs/system-overview.md` | Module map row for `embed.py`; retrieval-flow box naming Voyage |
| `HANDOFF.md` | Built table row for `embed.py`; a "What changed" entry; the "Voyage `voyage-4`" bullet under key decisions |
| `requirements.txt` | No change — nothing new is pip-installed; note ollama in the existing system-binaries comment |
| `.env` | `VOYAGE_API_KEY` becomes dead; leave the file, remove the key |

---

## 11. Acceptance criteria

- No occurrence of `voyage` remains in any `.py` file.
- `SELECT DISTINCT embedding_model FROM snippets` returns exactly one value,
  and it names EmbeddingGemma with its quantization.
- Hybrid search returns `backend: hybrid` on the §8.6 query with document 6's
  Linen chunks ranked first.
- Embedding works with no network connection and no API key present.
- A stopped daemon degrades to `embedding NULL` plus a reported error, never an
  exception.
