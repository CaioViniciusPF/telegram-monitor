import time
from typing import Any

import httpx

from tgmonitor.notifier import MAX_ALERT_TEXT, AlertMessage, BotNotifier, format_alert

OWNER = 111


def make_bot(handler: Any = None) -> tuple[BotNotifier, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def _default(request: httpx.Request) -> httpx.Response:
        import json

        calls.append({"path": request.url.path, "body": json.loads(request.content or b"{}")})
        return httpx.Response(200, json={"ok": True, "result": {}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler or _default))
    return BotNotifier(http, "123456:" + "A" * 35, OWNER), calls


def update(from_id: int, text: str, chat_type: str = "private") -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "from": {"id": from_id},
            "chat": {"id": from_id, "type": chat_type},
            "date": int(time.time()),
            "text": text,
        },
    }


async def test_ignores_strangers() -> None:
    bot, calls = make_bot()
    ran: list[str] = []

    async def status() -> str:
        ran.append("status")
        return "ok"

    bot.command("status", status)
    await bot._handle_update(update(999, "/status"))
    await bot._handle_update(update(OWNER, "/status", chat_type="group"))  # dono, mas em grupo
    assert ran == [] and calls == []

    await bot._handle_update(update(OWNER, "/status"))
    assert ran == ["status"]
    assert calls[0]["body"]["chat_id"] == OWNER


async def test_ignores_stale_commands() -> None:
    bot, calls = make_bot()

    async def pause() -> str:
        return "pausado"

    bot.command("pause", pause)
    upd = update(OWNER, "/pause")
    upd["message"]["date"] = int(time.time()) - 3600
    await bot._handle_update(upd)
    assert calls == []


async def test_send_retries_on_429() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                429, json={"ok": False, "description": "Too Many", "parameters": {"retry_after": 0}}
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    bot, _ = make_bot(handler)
    assert await bot.send("oi")
    assert attempts["n"] == 2


async def test_send_gives_up_on_403() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "description": "Forbidden"})

    bot, _ = make_bot(handler)
    assert not await bot.send("oi")


def test_format_alert_escapes_html_and_truncates() -> None:
    a = AlertMessage(
        chat_title="<b>Grupo</b>",
        rule_names=["S25"],
        text="<script>x</script>" + "a" * (MAX_ALERT_TEXT + 100),
        link="https://t.me/c/1/2",
        price_cents=549900,
        price_filter_active=True,
        is_edit=True,
    )
    out = format_alert(a)
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "&lt;b&gt;Grupo&lt;/b&gt;" in out
    assert "R$ 5.499,00" in out
    assert "editada" in out
    assert "…" in out
    assert 'href="https://t.me/c/1/2"' in out


def test_format_price_drop() -> None:
    a = AlertMessage(
        chat_title="Grupo",
        rule_names=["S25"],
        text="S25 Ultra por R$ 4.999",
        link=None,
        price_cents=499900,
        price_filter_active=False,
        is_edit=True,
        previous_price_cents=549900,
    )
    out = format_alert(a)
    assert "Preço baixou:</b> de R$ 5.499,00 para R$ 4.999,00" in out
    assert "💰" not in out
