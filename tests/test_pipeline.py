"""Pipeline completo com SQLite real e um bot falso."""

from datetime import UTC, datetime

import pytest

from tgmonitor.models import RuleIn, RuntimeSettings
from tgmonitor.notifier import AlertMessage, format_alert
from tgmonitor.pipeline import Pipeline
from tgmonitor.repository import Repository
from tgmonitor.telegram.client import IncomingMessage

NOW = 1_800_000_000.0
CHAT_A = -1001111111111
CHAT_B = -1002222222222
CHAT_OFF = -1003333333333


class FakeSender:
    def __init__(self, ok: bool = True) -> None:
        self.sent: list[AlertMessage] = []
        self.ok = ok

    async def send_alert(self, alert: AlertMessage) -> bool:
        self.sent.append(alert)
        format_alert(alert)  # garante que formata sem erro
        return self.ok


def msg(
    text: str,
    chat: int = CHAT_A,
    mid: int = 1,
    age_h: float = 0,
    is_edit: bool = False,
    urls: tuple[str, ...] = (),
) -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat,
        message_id=mid,
        text=text,
        date=datetime.fromtimestamp(NOW - age_h * 3600, tz=UTC),
        extra_urls=urls,
        is_edit=is_edit,
    )


@pytest.fixture
async def setup(repo: Repository) -> tuple[Pipeline, FakeSender, Repository]:
    await repo.upsert_chats(
        [
            (CHAT_A, "Promos A", "promosa", "channel"),
            (CHAT_B, "Promos B", None, "megagroup"),
            (CHAT_OFF, "Não monitorado", None, "channel"),
        ]
    )
    await repo.set_monitored(CHAT_A, True)
    await repo.set_monitored(CHAT_B, True)
    await repo.create_rule(
        RuleIn(
            name="S25/S26 Plus/Ultra",
            include_terms=[r"/s2[56]\s*(\+|plus|ultra)/"],
            exclude_terms=["capa", "fe", "película"],
            max_price_cents=600000,
        )
    )
    sender = FakeSender()
    p = Pipeline(repo, sender, clock=lambda: NOW)
    await p.reload()
    return p, sender, repo


PROMO = "Galaxy S25 Ultra de R$ 7.999 por R$ 5.499 https://amzn.to/aaa"


async def test_alerts_and_records(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    p, sender, repo = setup
    assert await p._handle(msg(PROMO)) == "alerted"
    assert len(sender.sent) == 1
    a = sender.sent[0]
    assert a.price_cents == 549900
    assert a.link == "https://t.me/promosa/1"
    alerts = await repo.list_alerts()
    assert alerts[0].delivered and alerts[0].matched_rules[0].name == "S25/S26 Plus/Ultra"


async def test_filters(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    p, sender, _ = setup
    assert await p._handle(msg(PROMO, chat=CHAT_OFF)) == "not_monitored"
    assert await p._handle(msg("Capa para S25 Ultra R$ 50", mid=2)) == "no_match"
    assert await p._handle(msg("S25 Ultra por R$ 6.999", mid=3)) == "no_match"  # acima do máximo
    assert sender.sent == []


async def test_price_not_identified_alerts_with_flag(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    p, sender, _ = setup
    assert await p._handle(msg("S25 Ultra 12x de R$ 458 sem juros")) == "alerted"
    a = sender.sent[0]
    assert a.price_cents is None and a.price_filter_active
    assert "preço não identificado" in format_alert(a)


async def test_same_text_other_group_deduped(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    p, sender, _ = setup
    assert await p._handle(msg(PROMO, chat=CHAT_A, mid=1)) == "alerted"
    assert await p._handle(msg("🔥 " + PROMO, chat=CHAT_B, mid=77)) == "dup_fingerprint"
    assert len(sender.sent) == 1


async def test_affiliate_links_deduped_by_rules_price(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    p, sender, _ = setup
    assert await p._handle(msg("S25 Ultra só R$ 5.499 https://amzn.to/grupoA", mid=1)) == "alerted"
    assert (
        await p._handle(
            msg("Oferta! Galaxy S25 Ultra R$ 5.499 👉 https://amzn.to/grupoB", chat=CHAT_B)
        )
        == "dup_fingerprint"
    )


async def test_price_drop_same_link_alerts_again(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    p, sender, _ = setup
    assert await p._handle(msg("S25 Ultra R$ 5.499 https://loja.com/p/1", mid=1)) == "alerted"
    assert (
        await p._handle(msg("BAIXOU! S25 Ultra R$ 4.999 https://loja.com/p/1", mid=2)) == "alerted"
    )
    assert len(sender.sent) == 2


async def test_dedup_window_expires(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    p, sender, repo = setup
    assert await p._handle(msg(PROMO, mid=1)) == "alerted"
    p.clock = lambda: NOW + 7 * 3600  # janela padrão = 6h
    later = IncomingMessage(
        chat_id=CHAT_A,
        message_id=2,
        text=PROMO,
        date=datetime.fromtimestamp(NOW + 7 * 3600, tz=UTC),
        extra_urls=(),
        is_edit=False,
    )
    assert await p._handle(later) == "alerted"


# ------------------------------------------------ edições de post já alertado
# PROMO sai por R$ 5.499. Só uma edição que BAIXA o preço gera alerta de novo.


def edit(text: str, mid: int = 10) -> IncomingMessage:
    return msg(text, mid=mid, is_edit=True, age_h=0.5)


async def test_edit_price_drop_realerts(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    p, sender, repo = setup
    assert await p._handle(msg(PROMO, mid=10)) == "alerted"
    dropped = "Galaxy S25 Ultra BAIXOU: de R$ 7.999 por R$ 4.999 https://amzn.to/aaa"
    assert await p._handle(edit(dropped)) == "price_drop"

    assert len(sender.sent) == 2
    drop = sender.sent[1]
    assert (drop.previous_price_cents, drop.price_cents, drop.is_edit) == (549900, 499900, True)
    assert "Preço baixou" in format_alert(drop)

    (row,) = await repo.list_alerts()  # continua uma linha por post
    assert (row.price_cents, row.previous_price_cents, row.delivered) == (499900, 549900, True)
    assert row.text == dropped


async def test_edit_price_increase_ignored(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    p, sender, _ = setup
    assert await p._handle(msg(PROMO, mid=10)) == "alerted"
    assert await p._handle(edit("Galaxy S25 Ultra por R$ 5.899")) == "dup_same_post"
    assert len(sender.sent) == 1


async def test_edit_same_price_ignored(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    p, sender, _ = setup
    assert await p._handle(msg(PROMO, mid=10)) == "alerted"
    assert (
        await p._handle(edit("Galaxy S25 Ultra, ÚLTIMAS UNIDADES por R$ 5.499")) == "dup_same_post"
    )
    assert len(sender.sent) == 1


async def test_edit_price_not_identified_ignored(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    p, sender, _ = setup
    assert await p._handle(msg(PROMO, mid=10)) == "alerted"
    assert await p._handle(edit("Galaxy S25 Ultra ESGOTOU")) == "dup_same_post"
    assert len(sender.sent) == 1


async def test_edit_when_first_alert_had_no_price_ignored(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    """Sem preço anterior não há como falar em queda: a edição é ignorada."""
    p, sender, _ = setup
    assert await p._handle(msg("Galaxy S25 Ultra preço só no app", mid=10)) == "alerted"
    assert await p._handle(edit("Galaxy S25 Ultra por R$ 4.999")) == "dup_same_post"
    assert len(sender.sent) == 1


async def test_price_drop_only_once_per_price(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    p, sender, _ = setup
    assert await p._handle(msg(PROMO, mid=10)) == "alerted"
    assert await p._handle(edit("S25 Ultra por R$ 4.999")) == "price_drop"
    assert await p._handle(edit("S25 Ultra por R$ 4.999 (corrigido)")) == "dup_same_post"
    assert await p._handle(edit("S25 Ultra por R$ 4.799")) == "price_drop"
    assert [a.price_cents for a in sender.sent] == [549900, 499900, 479900]


async def test_price_drop_deduped_against_other_posts(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    """Se o preço novo já foi alertado em outro grupo, a queda não gera alerta repetido."""
    p, sender, _ = setup
    assert await p._handle(msg("S25 Ultra por R$ 4.999", chat=CHAT_B, mid=1)) == "alerted"
    assert await p._handle(msg(PROMO, mid=10)) == "alerted"
    assert await p._handle(edit("Galaxy S25 Ultra agora por R$ 4.999")) == "dup_fingerprint"
    assert len(sender.sent) == 2


async def test_price_drop_still_needs_rule_match(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    p, sender, _ = setup
    assert await p._handle(msg(PROMO, mid=10)) == "alerted"
    assert await p._handle(edit("Capa para S25 Ultra por R$ 49")) == "no_match"
    assert len(sender.sent) == 1


async def test_new_message_with_same_id_never_realerts(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    """Mensagem nova (não edição) repetida pelo catch_up: nunca realerta, mesmo mais barata."""
    p, sender, _ = setup
    assert await p._handle(msg(PROMO, mid=10)) == "alerted"
    assert await p._handle(msg("S25 Ultra por R$ 4.999", mid=10)) == "dup_same_post"
    assert len(sender.sent) == 1


async def test_edit_that_starts_matching_alerts(
    setup: tuple[Pipeline, FakeSender, Repository],
) -> None:
    p, sender, _ = setup
    assert await p._handle(msg("Em breve oferta imperdível", mid=20)) == "no_match"
    assert await p._handle(msg(PROMO, mid=20, is_edit=True, age_h=1)) == "alerted"
    assert sender.sent[0].is_edit


async def test_old_messages_ignored(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    """catch_up após reconexão entrega mensagens antigas: > 3h são ignoradas."""
    p, sender, _ = setup
    assert await p._handle(msg(PROMO, age_h=3.5)) == "too_old"
    assert await p._handle(msg(PROMO, mid=2, age_h=2.5)) == "alerted"


async def test_old_edit_limit(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    p, _, _ = setup
    assert await p._handle(msg(PROMO, mid=5, is_edit=True, age_h=10)) == "alerted"
    assert await p._handle(msg("S26 Plus R$ 4.000", mid=6, is_edit=True, age_h=25)) == "too_old"


async def test_paused(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    p, sender, repo = setup
    await repo.save_settings(RuntimeSettings(paused=True))
    await p.reload()
    assert await p._handle(msg(PROMO)) == "paused"
    assert sender.sent == []


async def test_failed_delivery_recorded(repo: Repository) -> None:
    await repo.upsert_chats([(CHAT_A, "A", None, "channel")])
    await repo.set_monitored(CHAT_A, True)
    await repo.create_rule(RuleIn(name="r", include_terms=["galaxy"]))
    p = Pipeline(repo, FakeSender(ok=False), clock=lambda: NOW)
    await p.reload()
    assert await p._handle(msg("Galaxy S25 barato")) == "alert_failed"
    assert not (await repo.list_alerts())[0].delivered


async def test_handle_never_raises(setup: tuple[Pipeline, FakeSender, Repository]) -> None:
    p, _, _ = setup

    class Boom:
        async def send_alert(self, alert: AlertMessage) -> bool:
            raise RuntimeError("x")

    p.sender = Boom()
    await p.handle(msg(PROMO))  # não propaga
    assert p.stats["errors"] == 1
