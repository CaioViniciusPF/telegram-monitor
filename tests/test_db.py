"""Migrações do schema."""

import sqlite3
import time
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
        assert (await cur.fetchone())[0] == len(db._MIGRATIONS)
    (alert,) = await Repository(conn).list_alerts()
    assert (alert.price_cents, alert.previous_price_cents) == (549900, None)
    await conn.close()


async def test_v3_adds_restart_events_table(tmp_path: Path) -> None:
    """Banco em v2 (sem restart_events) migra para v3 sem perder o que já tinha."""
    path = tmp_path / "v2.db"
    raw = sqlite3.connect(path)
    raw.executescript("\n".join(db._MIGRATIONS[:2]) + "\nPRAGMA user_version = 2;")
    raw.execute(
        "INSERT INTO alerts (chat_id, chat_title, message_id, link, text, price_cents,"
        " matched_rules, is_edit, delivered, created_at, previous_price_cents)"
        " VALUES (-1001, 'Grupo', 1, NULL, 'x', NULL, '[]', 0, 1, 1700000000, NULL)"
    )
    raw.commit()
    raw.close()

    conn = await db.connect(path)
    async with conn.execute("PRAGMA user_version") as cur:
        assert (await cur.fetchone())[0] == len(db._MIGRATIONS)
    repo = Repository(conn)
    assert len(await repo.list_alerts()) == 1  # dado antigo preservado
    assert await repo.count_restart_events_since("x", 0) == 0  # tabela nova, vazia
    await conn.close()


async def test_restart_events_count_since_and_kind(repo: Repository) -> None:
    await repo.record_restart_event("updates_stalled")
    await repo.record_restart_event("updates_stalled")
    await repo.record_restart_event("outro_tipo")

    now = int(time.time())
    assert await repo.count_restart_events_since("updates_stalled", 0) == 2
    assert await repo.count_restart_events_since("outro_tipo", 0) == 1
    assert await repo.count_restart_events_since("updates_stalled", now + 10) == 0


async def test_purge_prunes_old_restart_events(repo: Repository) -> None:
    old = int(time.time()) - 8 * 86400  # mais de 7 dias (retenção fixa do purge)
    await repo.conn.execute(
        "INSERT INTO restart_events (kind, created_at) VALUES (?, ?)", ("updates_stalled", old)
    )
    await repo.conn.commit()
    await repo.record_restart_event("updates_stalled")  # este é recente

    await repo.purge(retention_days=30, window_hours=6)
    assert await repo.count_restart_events_since("updates_stalled", 0) == 1


async def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    first = await db.connect(path)  # cria e aplica todas as migrações
    await first.close()
    conn = await db.connect(path)  # reabrir não reaplica nada
    await db.migrate(conn)  # nem chamar de novo
    async with conn.execute("PRAGMA user_version") as cur:
        assert (await cur.fetchone())[0] == len(db._MIGRATIONS)
    await conn.close()
