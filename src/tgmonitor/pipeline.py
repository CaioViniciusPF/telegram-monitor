"""Pipeline de uma mensagem: filtro de chat → idade → regras → dedup → alerta.

Não depende do Telethon (recebe IncomingMessage), o que o torna testável.
Logs só com ids e contadores — nunca texto de mensagem.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from typing import Protocol

from tgmonitor.dedup import fingerprints
from tgmonitor.matching.matcher import CompiledRule, evaluate
from tgmonitor.models import Chat, MatchedRuleRef, RuntimeSettings
from tgmonitor.notifier import AlertMessage
from tgmonitor.repository import Repository
from tgmonitor.telegram.client import IncomingMessage
from tgmonitor.telegram.links import message_link

log = logging.getLogger(__name__)


class AlertSender(Protocol):
    async def send_alert(self, alert: AlertMessage) -> bool: ...


class Pipeline:
    def __init__(
        self,
        repo: Repository,
        sender: AlertSender,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repo = repo
        self.sender = sender
        self.clock = clock
        self.rules: list[CompiledRule] = []
        self.monitored: dict[int, Chat] = {}
        self.settings = RuntimeSettings()
        self.stats: Counter[str] = Counter()
        self.last_message_at: float | None = None
        # Serializa checagem de dedup + gravação (NewMessage e MessageEdited chegam em paralelo).
        self._lock = asyncio.Lock()

    async def reload(self) -> None:
        """Recarrega regras, chats e ajustes do banco (chamado após mudanças na API)."""
        rules = await self.repo.list_rules()
        self.rules = [CompiledRule.compile(r.to_spec()) for r in rules if r.enabled]
        self.monitored = await self.repo.monitored_chats()
        self.settings = await self.repo.get_settings()
        log.info(
            "config recarregada: %d regras ativas, %d chats", len(self.rules), len(self.monitored)
        )

    async def handle(self, msg: IncomingMessage) -> None:
        """Ponto de entrada do listener. Nunca propaga exceção para o Telethon."""
        try:
            await self._handle(msg)
        except Exception:
            self.stats["errors"] += 1
            log.exception("erro processando chat=%s msg=%s", msg.chat_id, msg.message_id)

    async def _handle(self, msg: IncomingMessage) -> str:
        """Retorna o desfecho (útil em testes)."""
        chat = self.monitored.get(msg.chat_id)
        if chat is None:
            return "not_monitored"
        now = self.clock()
        self.last_message_at = now
        self.stats["seen"] += 1

        if self.settings.paused:
            return "paused"

        age_h = (now - msg.date.timestamp()) / 3600
        limit = (
            self.settings.max_edit_age_hours
            if msg.is_edit
            else self.settings.max_new_message_age_hours
        )
        if age_h > limit:
            # Inclui mensagens antigas recuperadas pelo catch_up após reconexão.
            self.stats["too_old"] += 1
            return "too_old"

        if not msg.text.strip() or not self.rules:
            return "no_match"

        ev = evaluate(msg.text, self.rules)
        matched = ev.matched
        if not matched:
            return "no_match"

        rule_ids = [r.rule_id for r in matched]
        fps = fingerprints(msg.text, rule_ids, ev.price_cents, msg.extra_urls)
        since = int(now - self.settings.dedup_window_hours * 3600)

        refs = [MatchedRuleRef(id=r.rule_id, name=r.rule_name) for r in matched]
        link = message_link(msg.chat_id, msg.message_id, chat.username)
        previous_price: int | None = None

        async with self._lock:
            existing = await self.repo.alert_for_message(msg.chat_id, msg.message_id)
            if existing is not None:
                # Post já alertado: só alerta de novo se for uma edição que BAIXOU o preço.
                alert_id, alerted_price = existing
                dropped = (
                    msg.is_edit
                    and ev.price_cents is not None
                    and alerted_price is not None
                    and ev.price_cents < alerted_price
                )
                if not dropped:
                    self.stats["dedup_same_post"] += 1
                    return "dup_same_post"
                # O novo preço pode já ter sido alertado em outro post (ignora o próprio).
                if await self.repo.any_fingerprint_since(fps, since, exclude_alert_id=alert_id):
                    self.stats["dedup"] += 1
                    log.info("dedup (queda de preço) chat=%s msg=%s", msg.chat_id, msg.message_id)
                    return "dup_fingerprint"
                assert ev.price_cents is not None and alerted_price is not None
                previous_price = alerted_price
                await self.repo.record_price_drop(
                    alert_id,
                    text=msg.text,
                    price_cents=ev.price_cents,
                    previous_price_cents=alerted_price,
                    matched=refs,
                    fps=fps,
                    created_at=int(now),
                )
            else:
                if await self.repo.any_fingerprint_since(fps, since):
                    self.stats["dedup"] += 1
                    log.info("dedup chat=%s msg=%s", msg.chat_id, msg.message_id)
                    return "dup_fingerprint"
                new_id = await self.repo.insert_alert(
                    chat_id=msg.chat_id,
                    chat_title=chat.title,
                    message_id=msg.message_id,
                    link=link,
                    text=msg.text,
                    price_cents=ev.price_cents,
                    matched=refs,
                    is_edit=msg.is_edit,
                    fps=fps,
                    created_at=int(now),
                )
                if new_id is None:
                    self.stats["dedup_same_post"] += 1
                    return "dup_same_post"
                alert_id = new_id

        price_filter = any(
            cr.spec.max_price_cents is not None for cr in self.rules if cr.spec.id in rule_ids
        )
        ok = await self.sender.send_alert(
            AlertMessage(
                chat_title=chat.title,
                rule_names=[r.rule_name for r in matched],
                text=msg.text,
                link=link,
                price_cents=ev.price_cents,
                price_filter_active=price_filter,
                is_edit=msg.is_edit,
                previous_price_cents=previous_price,
            )
        )
        if ok:
            await self.repo.mark_delivered(alert_id)
            self.stats["price_drops" if previous_price is not None else "alerts"] += 1
        else:
            self.stats["alert_send_failed"] += 1
        log.info(
            "alerta id=%s chat=%s msg=%s regras=%s entregue=%s queda=%s",
            alert_id,
            msg.chat_id,
            msg.message_id,
            rule_ids,
            ok,
            previous_price is not None,
        )
        if previous_price is not None:
            return "price_drop" if ok else "alert_failed"
        return "alerted" if ok else "alert_failed"
