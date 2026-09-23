"""Write snippets from a triage decision made in conversation.

Layla proposes an excerpt/synthesis/rejection, you confirm it, this module
writes the row. It never decides what to keep — that call is always made in
conversation, never here.

In: an open db.py connection, a document_id, and the confirmed text.
Out: {"ok": True, "snippet_id": int, "embedded": bool, "embed_error": str |
     None} — a snippet is written even when embedding fails; the row just
     carries embedding=NULL until a later backfill. {"ok": False, "error":
     str} for a missing document, empty content, or a bad status value;
     never raises.
State: writes to snippets/documents via the caller's connection; does not
       open or close it. Calls embed.embed() — network, best-effort.
"""

from datetime import datetime, timezone

import embed as embed_mod
import metadata

_KINDS = ("excerpt", "synthesis", "rejection")
_STATUSES = ("pending", "kept", "partial", "discarded")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_tags(tags) -> "str | None":
    """Accept a list or a comma-separated string; store comma-separated.

    In: list[str], str, or None.
    Out: comma-separated str, or None. A list is the ergonomic path for a
         caller building tags programmatically; schema (decision 11) only
         ever sees free-form comma-separated text either way.
    """
    if not tags:
        return None
    if isinstance(tags, str):
        return tags
    return ",".join(t.strip() for t in tags if t and t.strip()) or None


def _document_exists(conn, document_id: int) -> bool:
    row = conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone()
    return row is not None


def _embed_one(content: str) -> tuple:
    """Best-effort single embed.

    Out: (blob | None, model | None, error | None). Never raises — a
         failure here must not block the snippet write.
    """
    result = embed_mod.embed([content])
    if not result.get("ok"):
        return None, None, result.get("error")
    return embed_mod.to_blob(result["vectors"][0]), result["model"], None


def _insert_snippet(conn, document_id: int, content: str, kind: str,
                     chunk_index, tags, blob, model) -> int:
    cur = conn.execute(
        "INSERT INTO snippets (document_id, content, kind, chunk_index, tags, "
        "created_at, embedding, embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (document_id, content, kind, chunk_index, tags, _now(), blob, model),
    )
    conn.commit()
    return cur.lastrowid


def _write_one(conn, document_id: int, content: str, kind: str,
                chunk_index=None, tags=None) -> dict:
    """Shared path for a single-snippet write (excerpt/synthesis/rejection).

    Out: see module docstring. Embeds before checking document_id so a
         real embed failure and a bad document_id are distinguishable;
         document_id is checked first specifically to avoid spending an
         API call on a write that can never land.
    """
    if not content or not content.strip():
        return {"ok": False, "error": "empty content"}
    if not _document_exists(conn, document_id):
        return {"ok": False, "error": f"no document with id {document_id}"}

    blob, model, embed_error = _embed_one(content)
    snippet_id = _insert_snippet(conn, document_id, content, kind, chunk_index,
                                  _normalize_tags(tags), blob, model)
    return {"ok": True, "snippet_id": snippet_id, "embedded": blob is not None,
            "embed_error": embed_error}


def write_excerpt(conn, document_id: int, content: str, chunk_index: int = None,
                   tags=None) -> dict:
    """Store a verbatim excerpt the user chose to keep.

    In: the exact text Layla proposed and the user confirmed (decision 10 —
        never auto-selected). chunk_index is set only when this excerpt is
        one piece of a whole-document split (see write_chunks); an ad hoc
        "keep this paragraph" excerpt leaves it None, which is a normal
        state per decision 15 — read-time neighbor expansion simply does
        not apply to it.
    Out: see module docstring.
    """
    return _write_one(conn, document_id, content, "excerpt", chunk_index, tags)


def write_synthesis(conn, document_id: int, content: str, tags=None) -> dict:
    """Store a Q&A-derived answer from a triage conversation.

    In: Layla's own synthesized text (decision 12), not source text —
        content is never a chunk.py output here, so chunk_index is always
        None.
    Out: see module docstring.
    """
    return _write_one(conn, document_id, content, "synthesis", None, tags)


def write_rejection(conn, document_id: int, reason: str, tags=None) -> dict:
    """Store the judgment behind a rejected claim, not the claim itself.

    In: the reason the user rejected something — content IS the judgment
        (decision 19). The rejected source text is never stored as an
        excerpt; only this reason is retrievable, so a later document
        repeating the same claim can be flagged instead of endorsed.
    Out: see module docstring.
    """
    return _write_one(conn, document_id, reason, "rejection", None, tags)


def _chapter_gate(conn, document_id: int, source_type: str, result: dict) -> str:
    """Refuse a transcript split that ignored the creator's chapters.

    In: connection, document_id, the document's source_type, chunker result.
    Out: an error string when the write must be refused, else None. A
         transcript with no recorded chapters is refused too: unrecorded
         and "creator published none" are different facts, and only the
         second licenses a chapterless split.
    """
    if source_type != "transcript":
        return None
    meta = metadata.load(conn, document_id)
    published = metadata.chapters(meta, source_type)
    if published is None:
        return ("no chapters recorded for document "
                f"{document_id} — re-capture it with youtube.py metadata before writing")
    if not published:
        return None
    if not result.get("chapters_used"):
        return (f"document {document_id} published {len(published)} chapters but the split "
                f"was not anchored to them (backend={result.get('backend')!r}) — re-chunk "
                "with chunker.invoke_document()")
    return None


def write_chunks(conn, document_id: int, result: dict, tags=None) -> dict:
    """Store every chunk from a "keep the whole document" decision.

    In: a chunker.py chunk result — its `chunks` list (each already
        carrying `content` and `chunk_index`) plus the `backend` and
        `chapters_used` provenance the gate below reads. This is decision
        13's "chunking the whole document into several coherent snippets"
        path — same excerpt kind, just more of them, all still shown to
        the user via format_candidates before this is ever called.
    Out: {"ok": True, "snippet_ids": [int...], "embedded": int, "total": int}
         or {"ok": False, "error": str}. A transcript whose video published
         chapters is rejected unless the split was anchored to them, so a
         chapterless fallback can never reach the corpus. One partial embed
         failure does not fail the whole batch — each row's own embedded
         flag is folded into the "embedded" count instead.
    State: one batched embed.embed() call for every chunk's content, not
           one call per chunk — matches embed.py's batching contract.
    """
    if not isinstance(result, dict):
        return {"ok": False, "error": "expected a chunker result dict, not a bare chunks list"}
    chunks = result.get("chunks")
    if not chunks:
        return {"ok": False, "error": "no chunks to write"}
    row = conn.execute(
        "SELECT source_type FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"no document with id {document_id}"}

    gate_error = _chapter_gate(conn, document_id, row["source_type"], result)
    if gate_error:
        return {"ok": False, "error": gate_error}

    contents = [c["content"] for c in chunks]
    embed_result = embed_mod.embed(contents)
    if embed_result.get("ok"):
        vectors = [embed_mod.to_blob(v) for v in embed_result["vectors"]]
        model = embed_result["model"]
    else:
        vectors = [None] * len(chunks)
        model = None

    normalized_tags = _normalize_tags(tags)
    snippet_ids = []
    embedded = 0
    for chunk, blob in zip(chunks, vectors):
        snippet_id = _insert_snippet(conn, document_id, chunk["content"], "excerpt",
                                      chunk["chunk_index"], normalized_tags, blob, model)
        snippet_ids.append(snippet_id)
        embedded += blob is not None

    return {"ok": True, "snippet_ids": snippet_ids, "embedded": embedded, "total": len(chunks)}


def reembed(conn, document_id: int = None, model: str = None) -> dict:
    """Re-embed rows whose vector is missing or from another model.

    In: open connection, optional document_id to scope the run, optional
        target model (defaults to embed.DEFAULT_MODEL). Backfill and
        provider migration are the same query — a missing vector and a
        stale one both need the same work.
    Out: {"ok", "reembedded": int, "skipped": int, "total": int} or
         {"ok": False, "error": str} carrying the same counts for the
         batches that did land. A failed batch leaves its rows untouched
         rather than half-written.
    State: embed.embed() per MAX_BATCH slice, committed per batch, so a
           mid-run failure leaves a consistent partially-migrated corpus —
           search.py filters by embedding_model, so mixed spaces never
           compare.
    """
    target = model or embed_mod.DEFAULT_MODEL
    where = "embedding IS NULL OR embedding_model IS NOT ?"
    params = [target]
    if document_id is not None:
        if not _document_exists(conn, document_id):
            return {"ok": False, "error": f"no document with id {document_id}"}
        where = f"({where}) AND document_id = ?"
        params.append(document_id)

    scope = "WHERE document_id = ?" if document_id is not None else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM snippets {scope}",
        (document_id,) if document_id is not None else (),
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT id, content FROM snippets WHERE {where} ORDER BY id", params
    ).fetchall()

    reembedded = 0
    for start in range(0, len(rows), embed_mod.MAX_BATCH):
        batch = rows[start:start + embed_mod.MAX_BATCH]
        result = embed_mod.embed([r["content"] for r in batch], model=target,
                                 input_type="document")
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error"), "reembedded": reembedded,
                    "skipped": total - len(rows), "total": total}
        conn.executemany(
            "UPDATE snippets SET embedding = ?, embedding_model = ? WHERE id = ?",
            [(embed_mod.to_blob(v), target, r["id"])
             for r, v in zip(batch, result["vectors"])],
        )
        conn.commit()
        reembedded += len(batch)

    return {"ok": True, "reembedded": reembedded, "skipped": total - len(rows),
            "total": total}



def set_status(conn, document_id: int, status: str) -> dict:
    """Set a document's triage status once the conversation concludes.

    In: "kept" (everything worth keeping was written), "partial" (some
        excerpts kept, rest discarded), or "discarded" (nothing kept).
        Which one applies is a human call made in conversation, not
        derived here from snippet counts.
    Out: {"ok": True} or {"ok": False, "error": str} for a bad status or
         missing document.
    """
    if status not in _STATUSES:
        return {"ok": False, "error": f"unknown status {status!r}, expected one of {_STATUSES}"}
    if not _document_exists(conn, document_id):
        return {"ok": False, "error": f"no document with id {document_id}"}
    conn.execute("UPDATE documents SET status = ? WHERE id = ?", (status, document_id))
    conn.commit()
    return {"ok": True}


def discard_document(conn, document_id: int) -> dict:
    """Mark a document discarded — no snippets, raw_text kept for provenance.

    In: document_id.
    Out: see set_status(). raw_text is never deleted (decision: provenance
         survives triage even for a fully discarded document).
    """
    return set_status(conn, document_id, "discarded")
