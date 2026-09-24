import pytest

from tgmonitor.matching.price import PriceKind, analyze_prices, extract_price, format_brl


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # --- casos pedidos: valores que NÃO são o preço do produto
        ("12x de R$ 458", None),
        ("cupom de R$ 200 OFF", None),
        ("frete R$ 19,90", None),
        ("desconto de R$ 300", None),
        ("R$ 300 de desconto", None),
        ("R$ 200 OFF", None),
        # --- casos pedidos: preço final / por
        ("de R$ 7.999 por R$ 5.499", 549900),
        ("com cupom: R$ 5.299", 529900),
        ("com desconto R$ 5.199", 519900),
        ("no Pix R$ 5.199", 519900),
        ("de R$ 5.999 por R$ 5.499, com cupom: R$ 5.299", 529900),
    ],
)
def test_required_cases(text: str, expected: int | None) -> None:
    assert extract_price(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("R$ 1.234,56", 123456),
        ("R$1234", 123400),
        ("R$ 1.234", 123400),
        ("R$ 12,90", 1290),
        ("R$ 12,9", 1290),
        ("R$ 5.499,00", 549900),
        ("r$ 999", 99900),
    ],
)
def test_formats(text: str, expected: int) -> None:
    assert extract_price(text) == expected


def test_full_promo_message() -> None:
    text = """🔥 Galaxy S25 Ultra 256GB
de R$ 7.999 por R$ 5.499 à vista
ou 12x de R$ 458 sem juros
🎟️ Cupom de R$ 200 OFF: GALAXY200
🚚 Frete R$ 19,90
https://amzn.to/abc"""
    assert extract_price(text) == 549900


def test_por_with_coupon_and_installments() -> None:
    text = "De R$ 7.999 por R$ 5.499 ou 12x de R$ 458 + cupom R$ 200 OFF"
    assert extract_price(text) == 549900


def test_coupon_final_beats_por() -> None:
    text = "Galaxy S25+ de R$ 5.999 por R$ 5.499\nUsando o cupom BLACK10: R$ 4.949"
    assert extract_price(text) == 494900


def test_coupon_code_before_value_is_final() -> None:
    assert extract_price("com cupom BLACK10: R$ 4.949") == 494900


def test_coupon_de_is_value_not_code() -> None:
    # "com cupom de R$ 200" = valor do cupom, não preço com cupom.
    assert extract_price("R$ 5.499 com cupom de R$ 200") == 549900


def test_pix_after_value() -> None:
    assert extract_price("Galaxy S25 R$ 5.199 no Pix ou R$ 5.499 em 10x") == 519900


def test_a_vista_with_accent() -> None:
    assert extract_price("À vista: R$ 4.899,90") == 489990


def test_neutral_takes_min() -> None:
    assert extract_price("Antes R$ 7.999 | Agora R$ 5.499") == 549900
    assert extract_price("R$ 6.000 e R$ 5.800") == 580000


def test_installment_variants_ignored() -> None:
    for text in ("10x R$ 549,90", "em até 12 x de R$ 458", "parcelas de R$ 300"):
        assert extract_price(text) is None, text


def test_cashback_and_economize_ignored() -> None:
    assert extract_price("R$ 5.499 + cashback de R$ 300") == 549900
    assert extract_price("R$ 5.499 — economize R$ 2.500") == 549900
    assert extract_price("R$ 5.499 e R$ 100 de volta") == 549900


def test_not_prices() -> None:
    assert extract_price("Galaxy S25 Ultra 256GB 12GB RAM") is None
    assert extract_price("S26 Plus 512 GB") is None
    assert extract_price("") is None


def test_zero_ignored() -> None:
    assert extract_price("R$ 0,00 de frete e R$ 5.000") == 500000


def test_analyze_classification() -> None:
    kinds = {
        c.cents: c.kind for c in analyze_prices("de R$ 5.999 por R$ 5.499, com cupom: R$ 5.299")
    }
    assert kinds == {
        599900: PriceKind.NEUTRAL,
        549900: PriceKind.POR,
        529900: PriceKind.FINAL,
    }


def test_format_brl() -> None:
    assert format_brl(549900) == "R$ 5.499,00"
    assert format_brl(1290) == "R$ 12,90"
    assert format_brl(123456789) == "R$ 1.234.567,89"
