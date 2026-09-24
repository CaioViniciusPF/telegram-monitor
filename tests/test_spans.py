"""Posições (spans) e motivos usados pela tela de teste para destacar o texto colado."""

import pytest

from tgmonitor.matching.matcher import CompiledRule, RuleSpec, evaluate
from tgmonitor.matching.normalize import normalize, normalize_with_map, original_span
from tgmonitor.matching.price import PriceKind, analyze_prices, pick_index

SAMPLES = [
    "Galaxy S25 Ultra",
    "  PELÍCULA   Câmera\n\tAção  ",
    "𝐆𝐚𝐥𝐚𝐱𝐲 𝐒𝟐𝟓 🔥🔥 R$ 5.499",
    "ﬁ ﬂ ½ ²",  # ligaduras e compatibilidade (NFKD expande)
    "é à combinantes soltos",
    "İstanbul ß",
    "",
    "   ",
    " espaço não separável ",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_normalize_with_map_matches_normalize(text: str) -> None:
    norm, index = normalize_with_map(text)
    assert norm == normalize(text)
    assert len(index) == len(norm)
    assert index == sorted(index)
    assert all(0 <= i < max(len(text), 1) for i in index)


def test_original_span_roundtrip_with_accents_and_styled_letters() -> None:
    text = "🔥 𝐆𝐚𝐥𝐚𝐱𝐲 PELÍCULA grátis"
    norm, index = normalize_with_map(text)
    start = norm.index("pelicula")
    s, e = original_span(index, start, start + len("pelicula"))
    assert text[s:e] == "PELÍCULA"
    start = norm.index("galaxy")
    s, e = original_span(index, start, start + len("galaxy"))
    assert text[s:e] == "𝐆𝐚𝐥𝐚𝐱𝐲"


def test_price_candidates_point_to_original_text() -> None:
    text = "🔥 De R$ 5.999 por R$  5.499, com cupom: R$ 5.299 ou 12x de R$ 458"
    cands = analyze_prices(text)
    assert [text[c.start : c.end] for c in cands] == [
        "R$ 5.999",
        "R$  5.499",
        "R$ 5.299",
        "R$ 458",
    ]


@pytest.mark.parametrize(
    ("text", "kind", "reason"),
    [
        ("com cupom: R$ 5.299", PriceKind.FINAL, "com cupom"),
        ("com desconto R$ 5.199", PriceKind.FINAL, "com desconto"),
        ("no Pix R$ 5.199", PriceKind.FINAL, "no Pix"),
        ("R$ 5.199 no Pix", PriceKind.FINAL, "no Pix"),
        ("à vista R$ 5.199", PriceKind.FINAL, "à vista"),
        ("12x de R$ 458", PriceKind.DISCARD, "parcela"),
        ("cupom de R$ 200", PriceKind.DISCARD, "valor do cupom"),
        ("R$ 200 OFF", PriceKind.DISCARD, "valor do desconto"),
        ("desconto de R$ 300", PriceKind.DISCARD, "valor do desconto"),
        ("R$ 300 de desconto", PriceKind.DISCARD, "valor do desconto"),
        ("frete R$ 19,90", PriceKind.DISCARD, "frete"),
        ("cashback de R$ 100", PriceKind.DISCARD, "cashback"),
        ("R$ 100 de volta", PriceKind.DISCARD, "cashback"),
        ("economize R$ 2.500", PriceKind.DISCARD, "economia"),
        ("por R$ 5.499", PriceKind.POR, "por"),
        ("R$ 5.499", PriceKind.NEUTRAL, "sem contexto"),
    ],
)
def test_price_reasons(text: str, kind: PriceKind, reason: str) -> None:
    (c,) = analyze_prices(text)
    assert (c.kind, c.reason) == (kind, reason)


def test_pick_index_marks_used_candidate() -> None:
    cands = analyze_prices("de R$ 5.999 por R$ 5.499, com cupom: R$ 5.299")
    assert pick_index(cands) == 2
    assert pick_index(analyze_prices("12x de R$ 458")) is None


def test_rule_spans_in_explain_mode() -> None:
    rule = CompiledRule.compile(
        RuleSpec(
            id=7,
            name="S25",
            include_terms=[r"/s2[56]\s*ultra/", "galaxy"],
            exclude_terms=["película"],
        )
    )
    text = "𝐆𝐚𝐥𝐚𝐱𝐲 S25   Ultra + PELÍCULA; outro galaxy"
    ev = evaluate(text, [rule], explain=True)
    res = ev.results[0]
    assert sorted(text[s:e] for s, e, _ in res.include_spans) == sorted(
        ["S25   Ultra", "𝐆𝐚𝐥𝐚𝐱𝐲", "galaxy"]
    )
    assert [(text[s:e], t) for s, e, t in res.exclude_spans] == [("PELÍCULA", "película")]


def test_hot_path_has_no_spans() -> None:
    rule = CompiledRule.compile(RuleSpec(id=1, name="r", include_terms=["galaxy"]))
    res = evaluate("galaxy", [rule]).results[0]
    assert res.matched and res.include_spans == []
