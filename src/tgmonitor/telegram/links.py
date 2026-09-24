"""Links t.me para a mensagem original."""

from __future__ import annotations

_CHANNEL_PREFIX = -1_000_000_000_000


def internal_channel_id(chat_id: int) -> int | None:
    """Converte o peer id "marcado" (-100XXXXXXXXXX) no id usado em t.me/c/."""
    if chat_id < _CHANNEL_PREFIX:
        return -chat_id + _CHANNEL_PREFIX  # -1001234567890 -> 1234567890
    return None


def message_link(chat_id: int, message_id: int, username: str | None) -> str | None:
    """Link público (t.me/user/id) ou privado (t.me/c/id/msg, só abre para membros).

    Grupos "básicos" (id negativo curto) não têm link de mensagem → None.
    """
    if username:
        return f"https://t.me/{username}/{message_id}"
    internal = internal_channel_id(chat_id)
    if internal is not None:
        return f"https://t.me/c/{internal}/{message_id}"
    return None
