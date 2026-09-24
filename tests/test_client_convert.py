"""Conversão de mensagens do Telethon (links escondidos em entidades e botões)."""

from types import SimpleNamespace

from telethon.tl import types as t

from tgmonitor.telegram.client import _hidden_urls


def test_entity_and_button_urls() -> None:
    msg = SimpleNamespace(
        entities=[
            t.MessageEntityBold(offset=0, length=4),
            t.MessageEntityTextUrl(offset=5, length=10, url="https://loja.com/p/1"),
        ],
        reply_markup=t.ReplyInlineMarkup(
            rows=[
                t.KeyboardButtonRow(
                    buttons=[
                        t.KeyboardButton(
                            text="Comprar", type=t.InlineButtonTypeUrl(url="https://amzn.to/x")
                        ),
                    ]
                )
            ]
        ),
    )
    assert _hidden_urls(msg) == ("https://loja.com/p/1", "https://amzn.to/x")


def test_legacy_url_button_shape() -> None:
    class KeyboardButtonUrl:  # formato de layers antigos
        def __init__(self, url: str) -> None:
            self.url = url

    markup = t.ReplyInlineMarkup(rows=[])
    markup.rows = [SimpleNamespace(buttons=[KeyboardButtonUrl("https://old.com/p")])]
    assert _hidden_urls(SimpleNamespace(entities=None, reply_markup=markup)) == (
        "https://old.com/p",
    )


def test_no_urls() -> None:
    assert _hidden_urls(SimpleNamespace(entities=None, reply_markup=None)) == ()
