"""Capture raw content into a documents row. Nothing else.

Layla (or the bulk-import subagent) fetches content with the `read` tool
first; this module never fetches anything itself and never calls an LLM —
it only persists text that's already in hand.

In: already-obtained text, its source_type, and optional url/title.
Out: {"ok": True, "document_id": int, "duplicate": bool} — duplicate is
     True when a document with the same url already exists, in which case
     nothing new is written. {"ok": False, "error": str} for empty text or
     a bad source_type; never raises.
State: writes one row to documents via the caller's connection; does not
       open or close it.
"""

from datetime import datetime, timezone

_SOURCE_TYPES = ("url", "pasted", "file", "transcript")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _document_exists(conn, document_id: int) -> bool:
    return conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is not None


def capture(conn, text: str, source_type: str, url: str = None, title: str = None) -> dict:
    """Create a documents row, or return the existing one for a known url.

    In: text (raw_text — stored pristine, never modified by this module),
        source_type, optional url/title.
    Out: see module docstring. status starts 'pending' regardless of
         source — the triage decision that changes it happens later and
         elsewhere (triage.py).
    """
    if source_type not in _SOURCE_TYPES:
        return {"ok": False, "error": f"unknown source_type {source_type!r}, expected one of {_SOURCE_TYPES}"}
    if not text or not text.strip():
        return {"ok": False, "error": "empty content"}

    if url:
        existing = conn.execute("SELECT id FROM documents WHERE url = ?", (url,)).fetchone()
        if existing is not None:
            return {"ok": True, "document_id": existing["id"], "duplicate": True}

    cur = conn.execute(
        "INSERT INTO documents (url, title, source_type, ingested_at, raw_text, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending')",
        (url, title, source_type, _now(), text),
    )
    conn.commit()
    return {"ok": True, "document_id": cur.lastrowid, "duplicate": False}


def set_summary(conn, document_id: int, summary: str) -> dict:
    """Record the discussion summary for a captured document.

    In: document_id, summary text — written by Layla after discussing the
        document in conversation (single-article path, decision 1) or by
        summarizer_agent.py (bulk path, decision 7). This module has no
        opinion on which; it just stores whatever text it's given.
    Out: {"ok": True} or {"ok": False, "error": str} for a missing
         document or empty summary.
    """
    if not summary or not summary.strip():
        return {"ok": False, "error": "empty summary"}
    if not _document_exists(conn, document_id):
        return {"ok": False, "error": f"no document with id {document_id}"}
    conn.execute("UPDATE documents SET agent_summary = ? WHERE id = ?", (summary, document_id))
    conn.commit()
    return {"ok": True}
