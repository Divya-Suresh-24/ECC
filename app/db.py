"""SQLite storage: users, documents (with versions), passage chunks + full-text index, question log."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name    TEXT NOT NULL,
    email           TEXT,
    role            TEXT NOT NULL CHECK (role IN ('student','faculty')),
    pw_hash         TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    must_change_pw  INTEGER NOT NULL DEFAULT 0,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login      TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'Protocols & SOPs',
    description     TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',
    effective_date  TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
    downloadable    INTEGER NOT NULL DEFAULT 1,
    current_version INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    updated_by      TEXT
);

CREATE TABLE IF NOT EXISTS document_versions (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    filename        TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    file_type       TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    page_count      INTEGER,
    char_count      INTEGER NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    uploaded_by     TEXT,
    uploaded_at     TEXT NOT NULL,
    UNIQUE (document_id, version)
);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_id      INTEGER NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    locator         TEXT NOT NULL DEFAULT '',
    page            INTEGER,
    section         TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id, version_id, seq);

-- rowid of chunks_fts == chunks.id
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, section, text,
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    question        TEXT NOT NULL,
    search_query    TEXT NOT NULL DEFAULT '',
    answer_json     TEXT NOT NULL DEFAULT '{}',
    answer_text     TEXT NOT NULL DEFAULT '',
    found           INTEGER NOT NULL DEFAULT 1,
    model           TEXT NOT NULL DEFAULT '',
    latency_ms      INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    feedback        INTEGER,            -- 1 = helpful, -1 = not helpful
    feedback_note   TEXT,
    reviewed        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_created ON questions(created_at);
CREATE INDEX IF NOT EXISTS idx_questions_user ON questions(user_id, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY,
    actor           TEXT,
    action          TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        _local.conn = conn
    return conn


@contextmanager
def transaction():
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    conn.close()


def audit(actor: str | None, action: str, detail: str = "") -> None:
    get_conn().execute(
        "INSERT INTO audit_log (actor, action, detail, created_at) VALUES (?,?,?,?)",
        (actor, action, detail, now()),
    )


def rows(sql: str, params: tuple | list = ()) -> list[dict]:
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def row(sql: str, params: tuple | list = ()) -> dict | None:
    r = get_conn().execute(sql, params).fetchone()
    return dict(r) if r else None
