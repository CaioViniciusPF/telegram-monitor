"""Migrações do schema."""

import sqlite3
from pathlib import Path

from tgmonitor import db
from tgmonitor.repository import Repository


async def test_fresh_database_is_latest_version(tmp_path: Path) -> None:
    conn = await db.connect(tmp_path / "new.db")
    async with conn.execute("PRAGMA user_version") as cur:
        assert (await cur.fetchone())[0] == len(db._MIGRATIONS)
    await conn.close()


async def test_v1_database_migrates_keeping_alerts(tmp_path: Path) -> None:
    """Banco criado por uma versão anterior (v1) ganha a coluna nova sem perder dados."""
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(db._MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
    raw.execute(
        "INSERT INTO alerts (chat_id, chat_title, message_id, link, text, price_cents,"
        " matched_rules, is_edit, delivered, created_at)"
        " VALUES (-1001, 'Grupo', 5, NULL, 'S25 Ultra por R$ 5.499', 549900,"
        ' \'[{"id": 1, "name": "S25"}]\', 0, 1, 1700000000)'
    )
    raw.commit()
    raw.close()

    conn = await db.connect(path)
    async with conn.execute("PRAGMA user_version") as cur:
        assert (await cur.fetchone())[0] == 2
    (alert,) = await Repository(conn).list_alerts()
    assert (alert.price_cents, alert.previous_price_cents) == (549900, None)
    await conn.close()


async def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    first = await db.connect(path)  # cria e aplica todas as migrações
    await first.close()
    conn = await db.connect(path)  # reabrir não reaplica nada
    await db.migrate(conn)  # nem chamar de novo
    async with conn.execute("PRAGMA user_version") as cur:
        assert (await cur.fetchone())[0] == len(db._MIGRATIONS)
    await conn.close()
