"""Fachada SOMENTE LEITURA sobre o TelegramClient (userbot).

Regra de segurança do projeto: o userbot nunca envia, encaminha, edita ou apaga
nada. Por isso o TelegramClient fica privado aqui dentro e o resto do código só
enxerga os métodos abaixo. `tests/test_readonly.py` garante que:
- a fachada não expõe métodos de escrita;
- nenhum outro módulo importa/instancia o TelegramClient.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, MessageEntityTextUrl, ReplyInlineMarkup

from tgmonitor.config import Settings, secure_file


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """Mensagem já desacoplada do Telethon (o pipeline não conhece o Telethon)."""

    chat_id: int
    message_id: int
    text: str
    date: datetime
    extra_urls: tuple[str, ...]
    is_edit: bool


@dataclass(frozen=True, slots=True)
class DialogInfo:
    id: int
    title: str
    username: str | None
    kind: str  # channel | megagroup | group


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]

# Layers antigos: KeyboardButtonUrl(url=...).
# Layers novos: KeyboardButton(type=InlineButtonTypeUrl(url=...)).
_URL_BUTTON_TYPES = {"KeyboardButtonUrl", "InlineButtonTypeUrl"}


def _button_url(button: Any) -> str | None:
    for obj in (button, getattr(button, "type", None)):
        if obj is not None and type(obj).__name__ in _URL_BUTTON_TYPES:
            url = getattr(obj, "url", None)
            if isinstance(url, str) and url:
                return url
    return None


def _hidden_urls(message: Any) -> tuple[str, ...]:
    """Links em texto clicável (entidades) e em botões inline — comuns em canais de promoção."""
    urls: list[str] = []
    for ent in getattr(message, "entities", None) or ():
        if isinstance(ent, MessageEntityTextUrl) and ent.url:
            urls.append(ent.url)
    markup = getattr(message, "reply_markup", None)
    if isinstance(markup, ReplyInlineMarkup):
        for row in markup.rows:
            for button in row.buttons:
                if url := _button_url(button):
                    urls.append(url)
    return tuple(urls)


def _username(entity: Any) -> str | None:
    if getattr(entity, "username", None):
        return str(entity.username)
    for u in getattr(entity, "usernames", None) or ():
        if getattr(u, "active", False):
            return str(u.username)
    return None


class ReadOnlyTelegram:
    def __init__(self, settings: Settings) -> None:
        self._session_file = settings.session_path.with_suffix(".session")
        self._client = TelegramClient(
            str(settings.session_path),
            settings.tg_api_id,
            settings.tg_api_hash.get_secret_value(),
            device_model="tgmonitor",
            app_version="0.1",
            flood_sleep_threshold=120,
            auto_reconnect=True,
        )

    # --------------------------------------------------------- ciclo de vida

    async def connect(self) -> None:
        await self._client.connect()
        secure_file(self._session_file)

    async def disconnect(self) -> None:
        await self._client.disconnect()

    def is_connected(self) -> bool:
        return bool(self._client.is_connected())

    async def is_authorized(self) -> bool:
        return bool(await self._client.is_user_authorized())

    async def run_until_disconnected(self) -> None:
        await self._client.run_until_disconnected()

    async def catch_up(self) -> None:
        await self._client.catch_up()

    async def get_me_id(self) -> int:
        me = await self._client.get_me()
        return int(me.id)

    async def interactive_login(self) -> str:
        """Login interativo via terminal (telefone, código, senha 2FA). Só usado pela CLI."""
        await self._client.start()  # prompts padrão do Telethon (input/getpass)
        secure_file(self._session_file)
        me = await self._client.get_me()
        name = " ".join(p for p in (me.first_name, me.last_name) if p)
        return name or str(me.id)

    # --------------------------------------------------------------- leitura

    async def list_dialogs(self) -> list[DialogInfo]:
        out: list[DialogInfo] = []
        async for d in self._client.iter_dialogs():
            ent = d.entity
            if isinstance(ent, Channel):
                kind = "megagroup" if ent.megagroup else "channel"
            elif isinstance(ent, Chat):
                kind = "group"
            else:
                continue  # conversas privadas e bots não interessam
            out.append(
                DialogInfo(
                    id=int(d.id), title=d.name or str(d.id), username=_username(ent), kind=kind
                )
            )
        return out

    def on_message(self, handler: MessageHandler) -> None:
        """Registra o handler para mensagens novas e editadas (todos os chats;
        o filtro por chats monitorados é feito no pipeline, que é recarregável)."""

        async def _new(event: events.NewMessage.Event) -> None:
            await handler(self._convert(event, is_edit=False))

        async def _edited(event: events.MessageEdited.Event) -> None:
            await handler(self._convert(event, is_edit=True))

        # incoming=None/outgoing=None: também pega posts de canais em que a conta é admin.
        self._client.add_event_handler(_new, events.NewMessage())
        self._client.add_event_handler(_edited, events.MessageEdited())

    @staticmethod
    def _convert(event: Any, *, is_edit: bool) -> IncomingMessage:
        msg = event.message
        return IncomingMessage(
            chat_id=int(event.chat_id),
            message_id=int(msg.id),
            text=msg.message or "",
            date=msg.date,
            extra_urls=_hidden_urls(msg),
            is_edit=is_edit,
        )
