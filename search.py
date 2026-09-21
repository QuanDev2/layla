"""Hybrid search over snippets: keyword (BM25) + meaning (cosine), fused by RRF.

query string -> embed the query, run FTS5 + brute-force cosine separately,
fuse the two rank orders (Reciprocal Rank Fusion), expand each hit with its
stored neighbors, attach source metadata. This is the only way anything
written by triage.py comes back out.

In: an open db.py connection, a query string, optionally the query's content
    words for the keyword side.
Out: {"ok": True, "backend": "hybrid" | "fts_only" | "vector_only" | "none",
     "results": [...]} or {"ok": False, "error": str}; never raises. A failed
     or unavailable embed step degrades to "fts_only" rather than failing the
     search — the same never-fatal posture embed.py and triage.py already take.
State: read-only. Calls embed.embed() for the query vector — network,
       best-effort.
"""

import sqlite3

import numpy as np

import embed as embed_mod

RRF_K = 60  # standard dampening constant from the original RRF paper
POOL_MULTIPLIER = 5  # candidates considered per side before fusing, relative to limit
MIN_POOL = 50


def _pool_size(limit: int) -> int:
    return max(limit * POOL_MULTIPLIER, MIN_POOL)


def _fts_terms(query: str, terms) -> list:
    """Reduce a query to MATCH-safe bare terms.

    In: raw query text, caller-supplied content words or None.
    Out: lowercase terms with FTS5 operator characters stripped, order kept,
         duplicates dropped; empty when nothing survives.
    """
    source = terms if terms else query.split()
    out = []
    for raw in source:
        term = "".join(ch for ch in raw if ch.isalnum() or ch in "-_").strip("-_")
        term = term.lower()
        if term and term not in out:
            out.append(term)
    return out


def _fts_ranked_ids(conn, query: str, pool_size: int, terms=None) -> list:
    """Keyword search, ranked by FTS5's built-in BM25 (ascending = better).

    In: connection, raw query, pool size, caller's content words or None.
    Out: snippet ids in rank order, best first. All terms required first, any
         term on a second pass when that matches nothing — a sentence-shaped
         query would otherwise demand its function words too. Empty on a
         malformed MATCH rather than raising.
    """
    words = _fts_terms(query, terms)
    if not words:
        return []
    for joiner in (" AND ", " OR "):
        try:
            rows = conn.execute(
                "SELECT rowid FROM snippets_fts WHERE snippets_fts MATCH ? "
                "ORDER BY bm25(snippets_fts) LIMIT ?",
                (joiner.join(words), pool_size),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        if rows:
            return [r[0] for r in rows]
    return []


def _vector_ranked_ids(conn, query: str, pool_size: int) -> list:
    """Meaning search: embed the query, cosine-rank every comparable row.

    Out: snippet ids in rank order, best first. Empty when the query
         couldn't be embedded at all (KNOWLEDGE_EMBED=off, no key, API
         error) or no row is embedded yet — indistinguishable to the
         caller by design, since either way vector search contributed
         nothing and the backend label only needs to know that.
    State: rows are filtered to embedding_model == the model that embedded
           this query — decision 5/15's guard against comparing vectors
           from incompatible spaces. A row with no embedding yet (NULL) or
           from a stale provider is simply invisible to vector search,
           still fully reachable via FTS5.
    """
    embedded = embed_mod.embed([query], input_type="query")
    if not embedded.get("ok"):
        return []

    model = embedded["model"]
    query_vec = np.asarray(embedded["vectors"][0], dtype=np.float32)
    rows = conn.execute(
        "SELECT id, embedding FROM snippets WHERE embedding IS NOT NULL AND embedding_model = ?",
        (model,),
    ).fetchall()
    if not rows:
        return []

    ids = [r[0] for r in rows]
    matrix = np.stack([embed_mod.from_blob(r[1]) for r in rows])
    matrix_norm = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    query_norm = query_vec / np.linalg.norm(query_vec)
    scores = matrix_norm @ query_norm

    order = np.argsort(-scores)[:pool_size]
    return [ids[i] for i in order]


def _rrf_fuse(*ranked_lists, k: int = RRF_K) -> dict:
    """Combine any number of rank-ordered id lists by Reciprocal Rank Fusion.

    In: one or more lists of ids, best-first. An id absent from a list
        contributes 0 for that list — no penalty beyond simply not adding.
    Out: {id: fused_score}. Rank position is all that matters — BM25 and
         cosine values are never compared directly, since they're on
         incomparable scales.
    """
    scores = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return scores


def _expand_neighbors(conn, document_id: int, chunk_index) -> tuple:
    """Read-time neighbor expansion for one hit (decision 15).

    In: the hit's document_id and chunk_index (may be None for a
        synthesis/rejection snippet, which is never expanded).
    Out: (context_before, context_after) content strings, or None on a
         side with no neighbor — a gap from a discarded chunk simply means
         no expansion there, never a fallback to raw_text. This is why
         discarded material can never leak back in through search.
    """
    if chunk_index is None:
        return None, None

    before = after = None
    if chunk_index > 0:
        row = conn.execute(
            "SELECT content FROM snippets WHERE document_id = ? AND chunk_index = ? AND kind = 'excerpt'",
            (document_id, chunk_index - 1),
        ).fetchone()
        before = row["content"] if row else None
    row = conn.execute(
        "SELECT content FROM snippets WHERE document_id = ? AND chunk_index = ? AND kind = 'excerpt'",
        (document_id, chunk_index + 1),
    ).fetchone()
    after = row["content"] if row else None
    return before, after


def search(conn, query: str, limit: int = 10, terms=None) -> dict:
    """Search snippets by keyword and meaning, fused into one ranked list.

    In: query text, max results, the query's content words for the keyword
        side — the caller holds the question, so it strips function words;
        omitted falls back to the whole query, sanitized.
    Out: {"ok": True, "backend": "hybrid" | "fts_only" | "vector_only" |
         "none", "results": [...]} each result: snippet_id, document_id,
         kind, content, chunk_index, document_title, document_url,
         ingested_at, context_before, context_after, score (the fused RRF
         score, for tuning/debugging — never meaningful in isolation, only
         for ordering). {"ok": False, "error": str} for empty query or
         non-positive limit.
    """
    if not query or not query.strip():
        return {"ok": False, "error": "empty query"}
    if limit <= 0:
        return {"ok": False, "error": "limit must be positive"}

    pool = _pool_size(limit)
    fts_ids = _fts_ranked_ids(conn, query, pool, terms)
    vector_ids = _vector_ranked_ids(conn, query, pool)
    if fts_ids and vector_ids:
        backend = "hybrid"
    elif fts_ids:
        backend = "fts_only"
    elif vector_ids:
        backend = "vector_only"
    else:
        backend = "none"

    fused = _rrf_fuse(fts_ids, vector_ids)
    top_ids = sorted(fused, key=fused.get, reverse=True)[:limit]

    results = []
    for snippet_id in top_ids:
        row = conn.execute(
            "SELECT s.id, s.document_id, s.content, s.kind, s.chunk_index, "
            "d.title, d.url, d.ingested_at "
            "FROM snippets s JOIN documents d ON d.id = s.document_id "
            "WHERE s.id = ?",
            (snippet_id,),
        ).fetchone()
        if row is None:  # defensive: a race with a delete between fuse and fetch
            continue
        before, after = _expand_neighbors(conn, row["document_id"], row["chunk_index"])
        results.append({
            "snippet_id": row["id"],
            "document_id": row["document_id"],
            "kind": row["kind"],
            "content": row["content"],
            "chunk_index": row["chunk_index"],
            "document_title": row["title"],
            "document_url": row["url"],
            "ingested_at": row["ingested_at"],
            "context_before": before,
            "context_after": after,
            "score": fused[snippet_id],
        })

    return {"ok": True, "backend": backend, "results": results}
