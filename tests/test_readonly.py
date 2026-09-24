"""Garantias estáticas de que o userbot só lê."""

import ast
import re
from pathlib import Path

from tgmonitor.telegram.client import ReadOnlyTelegram

SRC = Path(__file__).resolve().parents[1] / "src" / "tgmonitor"
CLIENT_MODULE = SRC / "telegram" / "client.py"

WRITE_PREFIXES = (
    "send",
    "forward",
    "edit",
    "delete",
    "pin",
    "unpin",
    "kick",
    "ban",
    "upload",
    "reply",
    "respond",
    "mark_read",
    "send_read_acknowledge",
    "join",
    "leave",
    "log_out",
    "invoke",
    "call",
    "action",
)

ALLOWED_CLIENT_CALLS = {
    "connect",
    "disconnect",
    "is_connected",
    "is_user_authorized",
    "run_until_disconnected",
    "catch_up",
    "get_me",
    "start",
    "iter_dialogs",
    "add_event_handler",
}


def test_facade_exposes_no_write_methods() -> None:
    public = [n for n in dir(ReadOnlyTelegram) if not n.startswith("_")]
    offending = [n for n in public if n.lower().startswith(WRITE_PREFIXES)]
    assert offending == []


def test_facade_only_calls_allowed_client_methods() -> None:
    tree = ast.parse(CLIENT_MODULE.read_text(encoding="utf-8"))
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_client"
    }
    assert used <= ALLOWED_CLIENT_CALLS, used - ALLOWED_CLIENT_CALLS


def test_telegram_client_only_used_in_facade() -> None:
    pattern = re.compile(r"\bTelegramClient\b")
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if p != CLIENT_MODULE and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_no_module_touches_private_client() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if p != CLIENT_MODULE and "._client" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
