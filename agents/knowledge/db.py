"""Schema and connection helper for the knowledge store.

connect() -> sqlite3.Connection, schema applied if missing. Every other
knowledge-domain module (ingest.py, triage.py, search.py) imports connect()
for its database handle; none of them redefine the schema.

In: optional db path override (tests use a throwaway file or ":memory:").
Out: a ready-to-use connection — foreign keys on, Row factory, schema
     present. Never raises for a missing file; sqlite creates it.
State: creates agents/knowledge/data/ and knowledge.db on first connect.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "knowledge.db"

# documents.raw_text is provenance/dedup only, never searched directly.
# snippets is the sole retrievable/embedded unit (PLAN.md decision 13).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY,
    url             TEXT,
    title           TEXT,
    source_type     TEXT NOT NULL,
    ingested_at     TEXT NOT NULL,
    raw_text        TEXT NOT NULL,
    structured_text TEXT,
    agent_summary   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS snippets (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id),
    content         TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'excerpt',
    chunk_index     INTEGER,
    tags            TEXT,
    created_at      TEXT NOT NULL,
    embedding       BLOB,
    embedding_model TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS snippets_fts USING fts5(
    content, tags, content='snippets', content_rowid='id'
);

-- snippets_fts is an external-content FTS5 index: sqlite does not keep it in
-- sync automatically, so every writer (present or future) needs these three
-- triggers, not application-level sync in triage.py.
CREATE TRIGGER IF NOT EXISTS snippets_ai AFTER INSERT ON snippets BEGIN
    INSERT INTO snippets_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS snippets_ad AFTER DELETE ON snippets BEGIN
    INSERT INTO snippets_fts(snippets_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS snippets_au AFTER UPDATE ON snippets BEGIN
    INSERT INTO snippets_fts(snippets_fts, rowid, content, tags)
    VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO snippets_fts(rowid, content, tags)
    VALUES (new.id, new.content, new.tags);
END;
"""


def init_db(conn: sqlite3.Connection) -> None:
    """Apply the schema to a connection.

    In: open connection.
    Out: none. Idempotent — every statement is CREATE-IF-NOT-EXISTS, so
         calling this on an already-initialized database is a no-op.
    """
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def connect(path: "str | Path" = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the knowledge database.

    In: path override — a real file path or ":memory:" for tests.
    Out: connection with foreign_keys enabled and Row factory, schema
         already applied.
    State: creates the parent directory for a real file path.
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn
