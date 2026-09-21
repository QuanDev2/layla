"""Registry of people and things the user stores observations about.

user's words ("mom", "him", "Linda") -> resolve() -> candidate entities ->
Layla confirms which one -> observations.py writes against that id.

In: an open db.py connection plus a name, kind, and optional relation,
    full name, aliases, note.
Out: every function returns a dict with an `ok` flag and never raises for
     expected failures.
State: writes rows to entities via the caller's connection.
"""

from datetime import datetime, timezone

_KINDS = ("self", "person", "pet", "vehicle", "place", "org", "thing")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_aliases(aliases) -> "str | None":
    """Accept a list or comma string; store lowercase comma-separated.

    In: list, comma string, or None.
    Out: deduped comma string, or None when nothing survives.
    """
    if not aliases:
        return None
    if isinstance(aliases, str):
        aliases = aliases.split(",")
    out = []
    for raw in aliases:
        alias = (raw or "").strip().lower()
        if alias and alias not in out:
            out.append(alias)
    return ",".join(out) or None


def _row_to_dict(row) -> dict:
    return {
        "entity_id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "relation": row["relation"],
        "full_name": row["full_name"],
        "aliases": row["aliases"],
        "note": row["note"],
    }


def create(conn, name: str, kind: str = "person", relation: str = None,
           full_name: str = None, aliases=None, note: str = None) -> dict:
    """Register a new entity.

    In: canonical name, kind, optional relation/full name/aliases/note.
    Out: {"ok": True, "entity_id": int, "duplicate": bool} — duplicate is
         True when the name or an alias already resolves, in which case the
         existing id is returned and nothing is written.
         {"ok": False, "error": str} for an empty name or unknown kind.
    State: inserts one row on a non-duplicate.
    """
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    if kind not in _KINDS:
        return {"ok": False, "error": f"unknown kind: {kind}; use one of {', '.join(_KINDS)}"}

    existing = resolve(conn, name)
    if existing["matches"]:
        return {"ok": True, "entity_id": existing["matches"][0]["entity_id"], "duplicate": True}

    cur = conn.execute(
        "INSERT INTO entities (name, kind, relation, full_name, aliases, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, kind, relation, full_name, _normalize_aliases(aliases), note, _now()),
    )
    conn.commit()
    return {"ok": True, "entity_id": cur.lastrowid, "duplicate": False}


def resolve(conn, text: str) -> dict:
    """Find every entity the user's words could mean.

    In: whatever the user called it — "mom", "Linda", "my brother".
    Out: {"ok": True, "matches": [entity dicts], "count": int}. Zero matches
         means it is new; more than one means Layla must ask which, never
         guess. Matching is case-insensitive over name, full_name, relation,
         and the alias list.
    """
    needle = (text or "").strip().lower()
    if not needle:
        return {"ok": True, "matches": [], "count": 0}

    matches = []
    for row in conn.execute("SELECT * FROM entities ORDER BY id"):
        haystack = [
            (row["name"] or "").lower(),
            (row["full_name"] or "").lower(),
            (row["relation"] or "").lower(),
        ]
        haystack += [a for a in (row["aliases"] or "").split(",") if a]
        if any(needle == h for h in haystack if h):
            matches.append(_row_to_dict(row))
            continue
        # a partial hit catches "my brother Tom" against name "Tom"
        if any(h and (needle in h or h in needle) for h in haystack):
            matches.append(_row_to_dict(row))
    return {"ok": True, "matches": matches, "count": len(matches)}


def get(conn, entity_id: int) -> dict:
    """Read one entity by id.

    In: entity id.
    Out: {"ok": True, "entity": dict} or {"ok": False, "error": str}.
    """
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"no entity with id {entity_id}"}
    return {"ok": True, "entity": _row_to_dict(row)}


def add_alias(conn, entity_id: int, alias: str) -> dict:
    """Teach an existing entity another word the user uses for it.

    In: entity id, the new alias.
    Out: {"ok": True, "aliases": str} or {"ok": False, "error": str}.
    State: updates the entity's alias list.
    """
    found = get(conn, entity_id)
    if not found["ok"]:
        return found
    merged = _normalize_aliases((found["entity"]["aliases"] or "").split(",") + [alias])
    conn.execute("UPDATE entities SET aliases = ? WHERE id = ?", (merged, entity_id))
    conn.commit()
    return {"ok": True, "aliases": merged}


def list_all(conn, kind: str = None) -> dict:
    """List every registered entity.

    In: optional kind filter.
    Out: {"ok": True, "entities": [dicts], "count": int}.
    """
    if kind:
        rows = conn.execute("SELECT * FROM entities WHERE kind = ? ORDER BY id", (kind,))
    else:
        rows = conn.execute("SELECT * FROM entities ORDER BY id")
    entities = [_row_to_dict(r) for r in rows]
    return {"ok": True, "entities": entities, "count": len(entities)}
