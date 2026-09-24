"""Acesso ao banco. Todas as queries do projeto ficam aqui."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Sequence

import aiosqlite

from tgmonitor.dedup import Fingerprint
from tgmonitor.models import Alert, Chat, MatchedRuleRef, Rule, RuleIn, RuntimeSettings


def now() -> int:
    return int(time.time())


class Repository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    # ---------------------------------------------------------------- chats

    async def upsert_chats(self, chats: Iterable[tuple[int, str, str | None, str]]) -> None:
        """(id, title, username, kind). Preserva o flag monitored existente."""
        ts = now()
        await self.conn.executemany(
            """
            INSERT INTO chats (id, title, username, kind, monitored, updated_at)
            VALUES (?, ?, ?, ?, 0, ?)
            ON CONFLICT (id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                kind = excluded.kind,
                updated_at = excluded.updated_at
            """,
            [(cid, title, username, kind, ts) for cid, title, username, kind in chats],
        )
        await self.conn.commit()

    async def list_chats(self) -> list[Chat]:
        async with self.conn.execute(
            "SELECT * FROM chats ORDER BY monitored DESC, title COLLATE NOCASE"
        ) as cur:
            rows = await cur.fetchall()
        return [
            Chat(
                id=r["id"],
                title=r["title"],
                username=r["username"],
                kind=r["kind"],
                monitored=bool(r["monitored"]),
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def set_monitored(self, chat_id: int, monitored: bool) -> bool:
        cur = await self.conn.execute(
            "UPDATE chats SET monitored = ? WHERE id = ?", (int(monitored), chat_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def monitored_chats(self) -> dict[int, Chat]:
        return {c.id: c for c in await self.list_chats() if c.monitored}

    # ---------------------------------------------------------------- rules

    @staticmethod
    def _row_to_rule(r: aiosqlite.Row) -> Rule:
        return Rule(
            id=r["id"],
            name=r["name"],
            include_terms=json.loads(r["include_terms"]),
            exclude_terms=json.loads(r["exclude_terms"]),
            max_price_cents=r["max_price_cents"],
            enabled=bool(r["enabled"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    async def list_rules(self) -> list[Rule]:
        async with self.conn.execute("SELECT * FROM rules ORDER BY id") as cur:
            return [self._row_to_rule(r) for r in await cur.fetchall()]

    async def get_rule(self, rule_id: int) -> Rule | None:
        async with self.conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)) as cur:
            r = await cur.fetchone()
        return self._row_to_rule(r) if r else None

    async def create_rule(self, data: RuleIn) -> Rule:
        ts = now()
        cur = await self.conn.execute(
            """
            INSERT INTO rules (name, include_terms, exclude_terms, max_price_cents, enabled,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.name,
                json.dumps(data.include_terms, ensure_ascii=False),
                json.dumps(data.exclude_terms, ensure_ascii=False),
                data.max_price_cents,
                int(data.enabled),
                ts,
                ts,
            ),
        )
        await self.conn.commit()
        rule = await self.get_rule(int(cur.lastrowid or 0))
        assert rule is not None
        return rule

    async def update_rule(self, rule_id: int, data: RuleIn) -> Rule | None:
        cur = await self.conn.execute(
            """
            UPDATE rules SET name = ?, include_terms = ?, exclude_terms = ?,
                             max_price_cents = ?, enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                data.name,
                json.dumps(data.include_terms, ensure_ascii=False),
                json.dumps(data.exclude_terms, ensure_ascii=False),
                data.max_price_cents,
                int(data.enabled),
                now(),
                rule_id,
            ),
        )
        await self.conn.commit()
        return await self.get_rule(rule_id) if cur.rowcount else None

    async def delete_rule(self, rule_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------------- alerts

    async def alert_for_message(
        self, chat_id: int, message_id: int
    ) -> tuple[int, int | None] | None:
        """(id, preço alertado) do alerta deste post, ou None se o post nunca gerou alerta."""
        async with self.conn.execute(
            "SELECT id, price_cents FROM alerts WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ) as cur:
            r = await cur.fetchone()
        return (int(r["id"]), r["price_cents"]) if r else None

    async def any_fingerprint_since(
        self, fps: Sequence[Fingerprint], since: int, exclude_alert_id: int | None = None
    ) -> bool:
        """Algum fingerprint já visto na janela? exclude_alert_id ignora os do próprio post."""
        for fp in fps:
            async with self.conn.execute(
                "SELECT 1 FROM fingerprints"
                " WHERE kind = ? AND hash = ? AND created_at >= ? AND alert_id IS NOT ? LIMIT 1",
                (fp.kind, fp.hash, since, exclude_alert_id),
            ) as cur:
                if await cur.fetchone() is not None:
                    return True
        return False

    async def record_price_drop(
        self,
        alert_id: int,
        *,
        text: str,
        price_cents: int,
        previous_price_cents: int,
        matched: Sequence[MatchedRuleRef],
        fps: Sequence[Fingerprint],
        created_at: int,
    ) -> None:
        """Atualiza o alerta de um post editado cujo preço baixou (uma linha por post)."""
        await self.conn.execute(
            """
            UPDATE alerts SET text = ?, price_cents = ?, previous_price_cents = ?,
                              matched_rules = ?, is_edit = 1, delivered = 0, created_at = ?
            WHERE id = ?
            """,
            (
                text,
                price_cents,
                previous_price_cents,
                json.dumps([m.model_dump() for m in matched], ensure_ascii=False),
                created_at,
                alert_id,
            ),
        )
        await self.conn.executemany(
            "INSERT INTO fingerprints (kind, hash, alert_id, created_at) VALUES (?, ?, ?, ?)",
            [(fp.kind, fp.hash, alert_id, created_at) for fp in fps],
        )
        await self.conn.commit()

    async def insert_alert(
        self,
        *,
        chat_id: int,
        chat_title: str,
        message_id: int,
        link: str | None,
        text: str,
        price_cents: int | None,
        matched: Sequence[MatchedRuleRef],
        is_edit: bool,
        fps: Sequence[Fingerprint],
        created_at: int | None = None,
    ) -> int | None:
        """Grava alerta + fingerprints atomicamente. None se (chat, msg) já alertado."""
        ts = now() if created_at is None else created_at
        try:
            cur = await self.conn.execute(
                """
                INSERT INTO alerts (chat_id, chat_title, message_id, link, text, price_cents,
                                    matched_rules, is_edit, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    chat_title,
                    message_id,
                    link,
                    text,
                    price_cents,
                    json.dumps([m.model_dump() for m in matched], ensure_ascii=False),
                    int(is_edit),
                    ts,
                ),
            )
            alert_id = int(cur.lastrowid or 0)
            await self.conn.executemany(
                "INSERT INTO fingerprints (kind, hash, alert_id, created_at) VALUES (?, ?, ?, ?)",
                [(fp.kind, fp.hash, alert_id, ts) for fp in fps],
            )
            await self.conn.commit()
        except sqlite3.IntegrityError:
            await self.conn.rollback()
            return None
        return alert_id

    async def mark_delivered(self, alert_id: int) -> None:
        await self.conn.execute("UPDATE alerts SET delivered = 1 WHERE id = ?", (alert_id,))
        await self.conn.commit()

    async def list_alerts(self, limit: int = 100) -> list[Alert]:
        async with self.conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [
            Alert(
                id=r["id"],
                chat_id=r["chat_id"],
                chat_title=r["chat_title"],
                message_id=r["message_id"],
                link=r["link"],
                text=r["text"],
                price_cents=r["price_cents"],
                matched_rules=[MatchedRuleRef(**m) for m in json.loads(r["matched_rules"])],
                is_edit=bool(r["is_edit"]),
                delivered=bool(r["delivered"]),
                created_at=r["created_at"],
                previous_price_cents=r["previous_price_cents"],
            )
            for r in rows
        ]

    async def purge(self, *, retention_days: int, window_hours: float) -> tuple[int, int]:
        ts = now()
        cur_fp = await self.conn.execute(
            "DELETE FROM fingerprints WHERE created_at < ?", (ts - int(window_hours * 3600),)
        )
        cur_al = await self.conn.execute(
            "DELETE FROM alerts WHERE created_at < ?", (ts - retention_days * 86400,)
        )
        await self.conn.commit()
        return cur_al.rowcount, cur_fp.rowcount

    # ------------------------------------------------------------- settings

    async def get_settings(self) -> RuntimeSettings:
        async with self.conn.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
        stored = {r["key"]: json.loads(r["value"]) for r in rows}
        known = {k: v for k, v in stored.items() if k in RuntimeSettings.model_fields}
        return RuntimeSettings(**known)

    async def save_settings(self, s: RuntimeSettings) -> None:
        await self.conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in s.model_dump().items()],
        )
        await self.conn.commit()
