"""Bot do BotFather via Bot API (httpx): envia alertas e aceita comandos SÓ do dono.

Qualquer update que não venha do OWNER_USER_ID, em chat privado, é ignorado
silenciosamente (sem resposta, sem log de conteúdo).
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from tgmonitor.matching.price import format_brl

log = logging.getLogger(__name__)

MAX_ALERT_TEXT = 3500  # limite do Telegram é 4096 após parse; sobra espaço p/ cabeçalho
_API = "https://api.telegram.org"
_COMMAND_MAX_AGE_S = 300  # comandos enfileirados enquanto o serviço estava fora são ignorados


@dataclass(frozen=True, slots=True)
class AlertMessage:
    chat_title: str
    rule_names: Sequence[str]
    text: str
    link: str | None
    price_cents: int | None
    price_filter_active: bool  # alguma regra casada tem preço máximo
    is_edit: bool
    previous_price_cents: int | None = None  # post editado cujo preço baixou


def format_alert(a: AlertMessage) -> str:
    e = html.escape
    lines = [f"🔔 <b>{e(', '.join(a.rule_names))}</b>"]
    lines.append(f"👥 {e(a.chat_title)}")
    if a.previous_price_cents is not None and a.price_cents is not None:
        old, new = format_brl(a.previous_price_cents), format_brl(a.price_cents)
        lines.append(f"📉 <b>Preço baixou:</b> de {old} para {new}")
    elif a.price_cents is not None:
        lines.append(f"💰 {format_brl(a.price_cents)}")
    elif a.price_filter_active:
        lines.append("⚠️ <i>preço não identificado</i>")
    if a.is_edit:
        lines.append("✏️ <i>mensagem editada</i>")
    body = a.text if len(a.text) <= MAX_ALERT_TEXT else a.text[:MAX_ALERT_TEXT] + "…"
    lines.append("")
    lines.append(e(body))
    if a.link:
        lines.append("")
        lines.append(f'🔗 <a href="{e(a.link, quote=True)}">Abrir mensagem original</a>')
    return "\n".join(lines)


CommandHandler = Callable[[], Awaitable[str]]


class BotNotifier:
    def __init__(self, http: httpx.AsyncClient, token: str, owner_id: int) -> None:
        self._http = http
        self._base = f"{_API}/bot{token}"
        self._owner_id = owner_id
        self._commands: dict[str, CommandHandler] = {}

    # ---------------------------------------------------------------- envio

    async def _call(self, method: str, payload: dict[str, Any], http_timeout: float = 20) -> Any:
        resp = await self._http.post(f"{self._base}/{method}", json=payload, timeout=http_timeout)
        data = resp.json()
        if not data.get("ok"):
            raise BotApiError(
                resp.status_code,
                str(data.get("description", "")),
                int((data.get("parameters") or {}).get("retry_after", 0)),
            )
        return data["result"]

    async def send(self, text_html: str, *, attempts: int = 5) -> bool:
        """Envia ao dono com retry. Respeita retry_after (429). Nunca levanta."""
        payload = {
            "chat_id": self._owner_id,
            "text": text_html,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                await self._call("sendMessage", payload)
                return True
            except BotApiError as e:
                if e.status == 429:
                    wait = float(e.retry_after)
                elif e.status >= 500:
                    wait = delay
                else:
                    # 400/403: erro permanente (ex: dono nunca deu /start no bot).
                    log.error("bot: envio rejeitado status=%s (%s)", e.status, e.description)
                    return False
            except (httpx.HTTPError, ValueError) as e:
                wait = delay
                log.warning(
                    "bot: falha de rede ao enviar (%s), tentativa %d", type(e).__name__, attempt
                )
            if attempt < attempts:
                await asyncio.sleep(wait + random.uniform(0, 1))  # noqa: S311
                delay = min(delay * 2, 60)
        log.error("bot: desistindo do envio após %d tentativas", attempts)
        return False

    async def send_alert(self, alert: AlertMessage) -> bool:
        return await self.send(format_alert(alert))

    async def send_notice(self, text: str) -> bool:
        """Aviso operacional (healthcheck, erro fatal)."""
        return await self.send(html.escape(text))

    # ------------------------------------------------------------- comandos

    def command(self, name: str, handler: CommandHandler) -> None:
        self._commands[name] = handler

    async def poll_commands(self) -> None:
        """Long polling de getUpdates. Roda para sempre; erros só geram backoff."""
        offset: int | None = None
        backoff = 5.0
        while True:
            try:
                payload: dict[str, Any] = {"timeout": 50, "allowed_updates": ["message"]}
                if offset is not None:
                    payload["offset"] = offset
                updates = await self._call("getUpdates", payload, http_timeout=65)
                backoff = 5.0
                for upd in updates:
                    offset = int(upd["update_id"]) + 1
                    await self._handle_update(upd)
            except asyncio.CancelledError:
                raise
            except BotApiError as e:
                log.warning("bot: getUpdates falhou status=%s", e.status)
                await asyncio.sleep(max(backoff, float(e.retry_after)))
                backoff = min(backoff * 2, 300)
            except Exception as e:  # noqa: BLE001 - loop precisa sobreviver a qualquer erro
                log.warning("bot: getUpdates erro %s", type(e).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def is_owner_message(self, msg: dict[str, Any]) -> bool:
        sender = (msg.get("from") or {}).get("id")
        chat = msg.get("chat") or {}
        return sender == self._owner_id and chat.get("type") == "private"

    async def _handle_update(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message")
        if not msg or not self.is_owner_message(msg):
            return  # ignora qualquer outra pessoa, silenciosamente
        if time.time() - int(msg.get("date", 0)) > _COMMAND_MAX_AGE_S:
            return
        text = str(msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        name = text.split()[0][1:].split("@")[0].lower()
        handler = self._commands.get(name) or self._commands.get("help")
        if handler is None:
            return
        try:
            reply = await handler()
        except Exception as e:  # noqa: BLE001
            reply = f"Erro ao executar /{name}: {type(e).__name__}"
        await self.send_notice(reply)


class BotApiError(Exception):
    def __init__(self, status: int, description: str, retry_after: int = 0) -> None:
        super().__init__(f"Bot API {status}: {description}")
        self.status = status
        self.description = description
        self.retry_after = retry_after
