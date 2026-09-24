"""Conexão SQLite e schema (migrações por PRAGMA user_version)."""

from __future__ import annotations

import os
from pathlib import Path

import aiosqlite

from tgmonitor.config import secure_file

_MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE chats (
        id          INTEGER PRIMARY KEY,
        title       TEXT NOT NULL,
        username    TEXT,
        kind        TEXT NOT NULL,
        monitored   INTEGER NOT NULL DEFAULT 0,
        updated_at  INTEGER NOT NULL
    );

    CREATE TABLE rules (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT NOT NULL,
        include_terms    TEXT NOT NULL,          -- JSON list
        exclude_terms    TEXT NOT NULL DEFAULT '[]',
        max_price_cents  INTEGER,
        enabled          INTEGER NOT NULL DEFAULT 1,
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL
    );

    CREATE TABLE alerts (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id        INTEGER NOT NULL,
        chat_title     TEXT NOT NULL,
        message_id     INTEGER NOT NULL,
        link           TEXT,
        text           TEXT NOT NULL,
        price_cents    INTEGER,
        matched_rules  TEXT NOT NULL,            -- JSON [{id, name}]
        is_edit        INTEGER NOT NULL DEFAULT 0,
        delivered      INTEGER NOT NULL DEFAULT 0,
        created_at     INTEGER NOT NULL,
        UNIQUE (chat_id, message_id)             -- nunca alertar o mesmo post duas vezes
    );
    CREATE INDEX idx_alerts_created ON alerts (created_at);

    CREATE TABLE fingerprints (
        kind        TEXT NOT NULL,
        hash        TEXT NOT NULL,
        alert_id    INTEGER NOT NULL REFERENCES alerts (id) ON DELETE CASCADE,
        created_at  INTEGER NOT NULL
    );
    CREATE INDEX idx_fp_lookup ON fingerprints (kind, hash, created_at);
    CREATE INDEX idx_fp_created ON fingerprints (created_at);

    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    """,
]


async def connect(path: Path) -> aiosqlite.Connection:
    """Abre o banco com permissões restritas e aplica migrações pendentes."""
    old_umask = os.umask(0o077)
    try:
        conn = await aiosqlite.connect(path)
    finally:
        os.umask(old_umask)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA synchronous = NORMAL")
    await migrate(conn)
    secure_file(path)
    return conn


async def migrate(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current = int(row[0]) if row else 0
    for version, script in enumerate(_MIGRATIONS[current:], start=current + 1):
        await conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
