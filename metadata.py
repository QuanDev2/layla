"""Source metadata blob for documents: build it, serialize it, read it back.

youtube.invoke() result -> from_youtube() -> documents.source_metadata (JSON)
-> load() -> author() / chapters() for chunker.py and triage.py.

In: a fetcher's result dict, or an open db.py connection and a document_id.
Out: plain dicts. chapters() returns None for "never recorded" and [] for
     "creator published none" — the two are never conflated, because only
     the second one licenses the chapterless split.
State: load() reads one documents row; nothing here writes.
"""

import json

# Common keys (author, published_at) sit at the top level for every source;
# source-specific keys nest under one key per source_type.
KIND_KEY = {"transcript": "video"}


def from_youtube(result: dict) -> dict:
    """Build the blob from a youtube.invoke() result.

    In: youtube.py's result — title/channel/duration/chapters already in hand.
    Out: {"author", "video": {"video_id", "duration", "chapters"}}. The
         chapters key is omitted entirely when the fetch errored, so a
         failed lookup never reads back as "creator published none".
    """
    if not result or not result.get("ok"):
        return {}
    video = {
        "video_id": result.get("video_id") or "",
        "duration": result.get("duration") or "",
    }
    if not result.get("chapters_error"):
        video["chapters"] = result.get("chapters") or []
    meta = {"video": video}
    author = (result.get("channel") or "").strip()
    if author:
        meta["author"] = author
    return meta


def dumps(meta: dict) -> str:
    """Serialize a blob for the source_metadata column.

    In: blob dict.
    Out: compact JSON, or None for an empty/missing blob so the column
         stays NULL rather than holding "{}".
    """
    if not meta:
        return None
    return json.dumps(meta, ensure_ascii=False)


def load(conn, document_id: int) -> dict:
    """Read one document's stored blob.

    In: open connection, document_id.
    Out: parsed blob, or {} when the row is missing, the column is NULL, or
         the stored text is not valid JSON.
    """
    row = conn.execute(
        "SELECT source_metadata FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None or not row["source_metadata"]:
        return {}
    try:
        parsed = json.loads(row["source_metadata"])
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def author(meta: dict) -> str:
    """Read who published the document.

    In: blob.
    Out: channel for a video, byline for an article, "" when unrecorded.
    """
    return (meta or {}).get("author") or ""


def chapters(meta: dict, source_type: str = "transcript") -> list:
    """Read the creator's published chapters.

    In: blob, source_type naming which nested key holds them.
    Out: list of {"title", "start", "end"}; [] when the creator published
         none; None when chapters were never recorded for this document.
    """
    key = KIND_KEY.get(source_type)
    if not key:
        return None
    nested = (meta or {}).get(key)
    if not isinstance(nested, dict) or "chapters" not in nested:
        return None
    found = nested.get("chapters")
    return found if isinstance(found, list) else None


def require_chapters(meta: dict, source_type: str) -> tuple:
    """Resolve the chapters a transcript must be split on.

    In: blob, the document's source_type.
    Out: (chapters, error). A non-transcript yields (None, None). A
         transcript with no recorded chapters yields an error instead of
         silently licensing the chapterless split — an unrecorded lookup
         and a chapterless video are different facts.
    """
    if source_type != "transcript":
        return None, None
    found = chapters(meta, source_type)
    if found is None:
        return None, ("no chapters recorded for this transcript — re-capture "
                      "it with youtube.py metadata before chunking")
    return found, None
