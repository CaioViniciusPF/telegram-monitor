import pytest

from tgmonitor.matching.matcher import (
    CompiledRule,
    InvalidTermError,
    RuleSpec,
    clean_terms,
    compile_term,
    evaluate,
)

S25_REGEX = r"/s2[56]\s*(\+|plus|ultra)/"


def rule(
    include: list[str],
    exclude: list[str] | None = None,
    max_price: int | None = None,
    rid: int = 1,
    enabled: bool = True,
) -> CompiledRule:
    return CompiledRule.compile(
        RuleSpec(
            id=rid,
            name=f"r{rid}",
            include_terms=include,
            exclude_terms=exclude or [],
            max_price_cents=max_price,
            enabled=enabled,
        )
    )


def matched_ids(text: str, *rules: CompiledRule) -> list[int]:
    return [r.rule_id for r in evaluate(text, rules).matched]


# ------------------------------------------------------------------ termos


def test_plain_term_is_word_bounded() -> None:
    r = rule(["galaxy"], ["fe"])
    assert matched_ids("Galaxy S25 FE 128GB", r) == []
    assert matched_ids("Galaxy S25 perfeito estado", r) == [1]  # "fe" em "perfeito" não exclui
    assert matched_ids("Galaxy S25 com café", r) == [1]


def test_accent_and_case_insensitive() -> None:
    r = rule(["galaxy"], ["película"])
    assert matched_ids("GALAXY S25 + PELICULA grátis", r) == []
    r2 = rule(["galaxy"], ["pelicula"])
    assert matched_ids("Galaxy s25 com Película", r2) == []


def test_multiword_term_flexible_spaces() -> None:
    r = rule(["s25 ultra"])
    assert matched_ids("Samsung S25    Ultra", r) == [1]
    assert matched_ids("S25\nUltra", r) == [1]
    assert matched_ids("S25Ultra", r) == []


def test_regex_term() -> None:
    r = rule([S25_REGEX])
    assert matched_ids("Galaxy S25 Ultra", r) == [1]
    assert matched_ids("Galaxy S26+ 256GB", r) == [1]
    assert matched_ids("Galaxy S25 Plus", r) == [1]
    assert matched_ids("Galaxy S25 128GB", r) == []
    assert matched_ids("Galaxy S24 Ultra", r) == []


def test_regex_with_accents_normalized() -> None:
    r = rule([r"/pel[ií]cula/"])
    assert matched_ids("PELÍCULA de vidro", r) == [1]


def test_include_is_or() -> None:
    r = rule(["s25 ultra", "s26 ultra"])
    assert matched_ids("S26 Ultra", r) == [1]
    assert matched_ids("S25 Ultra", r) == [1]


def test_exclude_wins() -> None:
    r = rule([S25_REGEX], ["capa", "fe", "película"])
    assert matched_ids("Capa para Galaxy S25 Ultra", r) == []
    assert matched_ids("Película Galaxy S26+", r) == []
    assert matched_ids("Galaxy S25 Ultra 512GB", r) == [1]


def test_empty_exclude_discards_nothing() -> None:
    assert matched_ids("Galaxy S25 Ultra capa", rule([S25_REGEX], [])) == [1]


# ------------------------------------------------------------------- preço


def test_max_price() -> None:
    r = rule([S25_REGEX], max_price=550000)
    assert matched_ids("S25 Ultra de R$ 7.999 por R$ 5.499", r) == [1]
    assert matched_ids("S25 Ultra por R$ 5.999", r) == []


def test_max_price_exact_limit_matches() -> None:
    assert matched_ids("S25 Ultra por R$ 5.500", rule([S25_REGEX], max_price=550000)) == [1]


def test_price_not_found_still_alerts() -> None:
    ev = evaluate("S25 Ultra em promoção! 12x de R$ 458", [rule([S25_REGEX], max_price=100000)])
    assert [r.rule_id for r in ev.matched] == [1]
    assert ev.price_cents is None
    assert "não identificado" in ev.matched[0].reason


def test_no_max_price_ignores_price() -> None:
    assert matched_ids("S25 Ultra por R$ 99.999", rule([S25_REGEX])) == [1]


# --------------------------------------------------------------- múltiplas


def test_multiple_rules() -> None:
    r1 = rule(["s25 ultra"], rid=1)
    r2 = rule([S25_REGEX], rid=2)
    r3 = rule(["iphone"], rid=3)
    assert matched_ids("Galaxy S25 Ultra", r1, r2, r3) == [1, 2]


def test_disabled_rule_skipped_unless_explain() -> None:
    r = rule(["galaxy"], enabled=False)
    assert evaluate("galaxy", [r]).results == []
    assert evaluate("galaxy", [r], explain=True).results[0].matched


def test_explain_lists_all_terms() -> None:
    r = rule(["galaxy", "s25", "ultra"], ["capa", "fe"])
    res = evaluate("Capa Galaxy S25 Ultra FE", [r], explain=True).results[0]
    assert res.included_by == ["galaxy", "s25", "ultra"]
    assert res.excluded_by == ["capa", "fe"]
    assert not res.matched


# ---------------------------------------------------------------- validação


def test_invalid_regex() -> None:
    with pytest.raises(InvalidTermError):
        compile_term("/s2[5/")


def test_empty_regex() -> None:
    with pytest.raises(InvalidTermError):
        compile_term("//")


def test_clean_terms() -> None:
    assert clean_terms(["  capa ", "", "capa", "fe"]) == ["capa", "fe"]


def test_special_chars_in_plain_term_are_literal() -> None:
    r = rule(["s25+"])
    assert matched_ids("Galaxy S25+ 256GB", r) == [1]
    assert matched_ids("Galaxy S255", r) == []
