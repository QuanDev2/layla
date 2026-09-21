"""Store what the user tells Layla about an entity, and read it back exactly.

entity id + attribute/value (structured) or body (prose) -> one row ->
get() returns current rows, replace() supersedes the old value first.

In: an open db.py connection, an entity id from entities.py, and either
    attribute+value or body.
Out: every function returns a dict with an `ok` flag and never raises for
     expected failures.
State: writes rows to observations; never edits a stored value in place.
"""

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entity_exists(conn, entity_id: int) -> bool:
    return conn.execute("SELECT 1 FROM entities WHERE id = ?", (entity_id,)).fetchone() is not None


def _row_to_dict(row) -> dict:
    return {
        "observation_id": row["id"],
        "entity_id": row["entity_id"],
        "attribute": row["attribute"],
        "value": row["value"],
        "body": row["body"],
        "source": row["source"],
        "observed_at": row["observed_at"],
        "superseded": bool(row["superseded"]),
    }


def write(conn, entity_id: int, attribute: str = None, value: str = None,
          body: str = None, source: str = None) -> dict:
    """Record one observation about an entity.

    In: entity id, then either attribute+value (structured, looked up
        exactly) or body (prose). source names where it came from.
    Out: {"ok": True, "observation_id": int} or {"ok": False, "error": str}
         for an unknown entity, a missing value, or neither form supplied.
    State: inserts one row. Never supersedes anything — use replace() when
           a value is being updated rather than added.
    """
    if not _entity_exists(conn, entity_id):
        return {"ok": False, "error": f"no entity with id {entity_id}"}

    attribute = (attribute or "").strip() or None
    value = (value or "").strip() or None
    body = (body or "").strip() or None

    if attribute and not value:
        return {"ok": False, "error": f"attribute '{attribute}' given without a value"}
    if not attribute and not body:
        return {"ok": False, "error": "need attribute+value or body"}

    cur = conn.execute(
        "INSERT INTO observations (entity_id, attribute, value, body, source, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (entity_id, attribute, value, body, source, _now()),
    )
    conn.commit()
    return {"ok": True, "observation_id": cur.lastrowid}


def replace(conn, entity_id: int, attribute: str, value: str, source: str = None) -> dict:
    """Update a structured value: supersede the old rows, write the new one.

    In: entity id, attribute, the new value, optional source.
    Out: {"ok": True, "observation_id": int, "superseded": int} — the count
         of rows retired. {"ok": False, "error": str} on a bad entity or
         empty attribute/value.
    State: flags previous rows for this entity+attribute, inserts one row.
           A superseded row stays readable but never answers a lookup, so
           two contradictory values can never both come back.
    """
    if not _entity_exists(conn, entity_id):
        return {"ok": False, "error": f"no entity with id {entity_id}"}
    attribute = (attribute or "").strip()
    value = (value or "").strip()
    if not attribute or not value:
        return {"ok": False, "error": "replace needs both attribute and value"}

    cur = conn.execute(
        "UPDATE observations SET superseded = 1 "
        "WHERE entity_id = ? AND attribute = ? AND superseded = 0",
        (entity_id, attribute),
    )
    retired = cur.rowcount
    written = write(conn, entity_id, attribute=attribute, value=value, source=source)
    if not written["ok"]:
        return written
    return {"ok": True, "observation_id": written["observation_id"], "superseded": retired}


def supersede(conn, observation_id: int) -> dict:
    """Retire one observation without replacing it.

    In: observation id.
    Out: {"ok": True} or {"ok": False, "error": str}.
    State: sets superseded = 1; the row is kept for history.
    """
    cur = conn.execute(
        "UPDATE observations SET superseded = 1 WHERE id = ? AND superseded = 0",
        (observation_id,),
    )
    conn.commit()
    if cur.rowcount == 0:
        return {"ok": False, "error": f"no active observation with id {observation_id}"}
    return {"ok": True}


def get(conn, entity_id: int, attribute: str = None,
        include_superseded: bool = False) -> dict:
    """Read an entity's observations, newest first.

    In: entity id, optional attribute filter, whether to include retired
        rows.
    Out: {"ok": True, "observations": [dicts], "count": int}. An empty list
         means nothing is stored — say so rather than inferring a value.
    """
    sql = "SELECT * FROM observations WHERE entity_id = ?"
    params = [entity_id]
    if attribute:
        sql += " AND attribute = ?"
        params.append(attribute)
    if not include_superseded:
        sql += " AND superseded = 0"
    sql += " ORDER BY observed_at DESC, id DESC"
    rows = [_row_to_dict(r) for r in conn.execute(sql, params)]
    return {"ok": True, "observations": rows, "count": len(rows)}
