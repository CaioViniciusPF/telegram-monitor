from tgmonitor.dedup import extract_urls, fingerprints, normalize_url, text_signature


def fp_of(
    text: str, rule_ids: list[int], price: int | None, extra: tuple[str, ...] = ()
) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for fp in fingerprints(text, rule_ids, price, extra):
        out.setdefault(fp.kind, set()).add(fp.hash)
    return out


def test_text_signature_ignores_emoji_punctuation_urls() -> None:
    a = "🔥🔥 Galaxy S25 Ultra por R$ 5.499!!! https://amzn.to/abc"
    b = "Galaxy S25 Ultra — por R$ 5.499 👉 https://mercadolivre.com/xyz"
    assert text_signature(a) == text_signature(b)


def test_short_text_has_no_text_fingerprint() -> None:
    assert "text" not in fp_of("oi", [1], None)


def test_normalize_url_strips_tracking() -> None:
    a = normalize_url("https://www.Loja.com/produto/123/?utm_source=tg&utm_medium=x&fbclid=abc#top")
    b = normalize_url("http://loja.com/produto/123")
    assert a == b


def test_normalize_url_keeps_meaningful_query_sorted() -> None:
    assert normalize_url("https://loja.com/p?id=2&cor=azul") == normalize_url(
        "https://loja.com/p?cor=azul&id=2&utm_campaign=z"
    )
    assert normalize_url("https://loja.com/p?id=2") != normalize_url("https://loja.com/p?id=3")


def test_extract_urls_strips_trailing_punctuation() -> None:
    assert extract_urls("veja em https://loja.com/p/1. Corre!") == ["https://loja.com/p/1"]


def test_url_fingerprint_includes_price() -> None:
    """Mesmo link com preço diferente (queda de preço) NÃO pode ser deduplicado."""
    a = fp_of("promo https://loja.com/p/1", [1], 549900)["url"]
    b = fp_of("outra legenda https://loja.com/p/1", [1], 499900)["url"]
    c = fp_of("mais uma https://loja.com/p/1?utm_source=x", [1], 549900)["url"]
    assert a.isdisjoint(b)
    assert a == c


def test_url_fingerprint_without_price_uses_url_only() -> None:
    a = fp_of("promo https://loja.com/p/1", [1], None)["url"]
    b = fp_of("texto bem diferente https://loja.com/p/1", [2], None)["url"]
    assert a == b


def test_hidden_urls_are_fingerprinted() -> None:
    a = fp_of("Clique aqui para comprar", [1], None, ("https://loja.com/p/9",))["url"]
    b = fp_of("Link: https://loja.com/p/9", [1], None)["url"]
    assert a == b


def test_rules_price_fingerprint() -> None:
    """Afiliados diferentes por grupo: mesmo conjunto de regras + mesmo preço = duplicado."""
    a = fp_of("S25 Ultra R$ 5.499 https://amzn.to/grupoA", [2, 1], 549900)
    b = fp_of("Galaxy S25 Ultra 🔥 R$ 5.499 https://amzn.to/grupoB", [1, 2], 549900)
    assert a["rules_price"] == b["rules_price"]
    assert a["url"].isdisjoint(b["url"])


def test_rules_price_only_with_price() -> None:
    assert "rules_price" not in fp_of("S25 Ultra sem preço aqui", [1], None)


def test_rules_price_differs_by_price_or_rules() -> None:
    base = fp_of("x" * 20, [1], 549900)["rules_price"]
    assert base.isdisjoint(fp_of("x" * 20, [1], 529900)["rules_price"])
    assert base.isdisjoint(fp_of("x" * 20, [1, 3], 549900)["rules_price"])
