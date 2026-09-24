"""Avaliação de regras de alerta contra o texto de uma mensagem.

Termos:
- Simples ("capa", "fe", "s25 ultra"): casam com limite de palavra, sem acento
  e sem caixa. "fe" NÃO casa com "feito" nem "café".
- Regex, entre barras ("/s2[56]\\s*(\\+|plus|ultra)/"): aplicada sobre o texto
  normalizado (minúsculo e sem acentos).

Semântica: inclusão = qualquer termo (OR); exclusão = qualquer termo descarta.
Princípio "na dúvida, alertar": exclusões e preço máximo são opcionais; vazios
não descartam nada, e preço não identificado não descarta.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from tgmonitor.matching.normalize import (
    normalize,
    normalize_with_map,
    original_span,
    strip_accents,
)
from tgmonitor.matching.price import PriceCandidate, analyze_prices, pick_index

_MAX_TERM_LEN = 500


class InvalidTermError(ValueError):
    def __init__(self, term: str, reason: str) -> None:
        super().__init__(f"termo inválido {term!r}: {reason}")
        self.term = term
        self.reason = reason


def is_regex_term(term: str) -> bool:
    return len(term) >= 2 and term.startswith("/") and term.endswith("/")


def compile_term(term: str) -> re.Pattern[str]:
    raw = term.strip()
    if not raw:
        raise InvalidTermError(term, "vazio")
    if len(raw) > _MAX_TERM_LEN:
        raise InvalidTermError(term, "longo demais")
    if is_regex_term(raw):
        body = raw[1:-1]
        if not body:
            raise InvalidTermError(term, "regex vazia")
        try:
            # Texto já vem normalizado; normalizar a regex aceita "pelicula" e "película".
            return re.compile(normalize_regex(body))
        except re.error as e:
            raise InvalidTermError(term, str(e)) from e
    words = normalize(raw).split(" ")
    pattern = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){pattern}(?!\w)")


def normalize_regex(body: str) -> str:
    # Só remove acentos e casefold; não colapsa espaços (poderiam ser significativos no padrão).
    return strip_accents(body).casefold()


def clean_terms(terms: Iterable[str]) -> list[str]:
    """Remove vazios/duplicados preservando a ordem; valida cada termo."""
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        t = t.strip()
        if not t or t in seen:
            continue
        compile_term(t)
        seen.add(t)
        out.append(t)
    return out


@dataclass(frozen=True, slots=True)
class RuleSpec:
    id: int
    name: str
    include_terms: Sequence[str]
    exclude_terms: Sequence[str] = ()
    max_price_cents: int | None = None
    enabled: bool = True


@dataclass(slots=True)
class CompiledRule:
    spec: RuleSpec
    include: list[tuple[str, re.Pattern[str]]]
    exclude: list[tuple[str, re.Pattern[str]]]

    @classmethod
    def compile(cls, spec: RuleSpec) -> CompiledRule:
        return cls(
            spec=spec,
            include=[(t, compile_term(t)) for t in spec.include_terms if t.strip()],
            exclude=[(t, compile_term(t)) for t in spec.exclude_terms if t.strip()],
        )


@dataclass(slots=True)
class RuleResult:
    rule_id: int
    rule_name: str
    matched: bool
    included_by: list[str] = field(default_factory=list)
    excluded_by: list[str] = field(default_factory=list)
    price_over_limit: bool = False
    reason: str = ""
    # Só no modo explain: trechos [início, fim, termo] no texto ORIGINAL.
    include_spans: list[tuple[int, int, str]] = field(default_factory=list)
    exclude_spans: list[tuple[int, int, str]] = field(default_factory=list)


@dataclass(slots=True)
class Evaluation:
    price_cents: int | None
    price_candidates: list[PriceCandidate]
    results: list[RuleResult]
    price_index: int | None = None  # qual candidato virou o preço

    @property
    def matched(self) -> list[RuleResult]:
        return [r for r in self.results if r.matched]


def _evaluate_rule(
    rule: CompiledRule, norm: str, price_cents: int | None, stop_early: bool
) -> RuleResult:
    spec = rule.spec
    res = RuleResult(rule_id=spec.id, rule_name=spec.name, matched=False)

    for term, pat in rule.include:
        if pat.search(norm):
            res.included_by.append(term)
            if stop_early:
                break
    if not res.included_by:
        res.reason = "nenhum termo de inclusão casou"
        return res

    for term, pat in rule.exclude:
        if pat.search(norm):
            res.excluded_by.append(term)
            if stop_early:
                break
    if res.excluded_by:
        res.reason = "termo de exclusão casou"
        return res

    if (
        spec.max_price_cents is not None
        and price_cents is not None
        and price_cents > spec.max_price_cents
    ):
        res.price_over_limit = True
        res.reason = "preço acima do máximo"
        return res

    res.matched = True
    if spec.max_price_cents is not None and price_cents is None:
        res.reason = "casou (preço não identificado)"
    else:
        res.reason = "casou"
    return res


def evaluate(text: str, rules: Iterable[CompiledRule], *, explain: bool = False) -> Evaluation:
    """Avalia o texto contra as regras ativas.

    Com explain=True, lista todos os termos que casaram (tela de teste);
    sem ele, para no primeiro termo (caminho quente do listener).
    """
    candidates = analyze_prices(text)
    price_index = pick_index(candidates)
    price = None if price_index is None else candidates[price_index].cents
    if not explain:
        norm = normalize(text)
        results = [_evaluate_rule(r, norm, price, stop_early=True) for r in rules if r.spec.enabled]
        return Evaluation(price_cents=price, price_candidates=candidates, results=results)

    norm, index = normalize_with_map(text)
    results = []
    for r in rules:
        res = _evaluate_rule(r, norm, price, stop_early=False)
        res.include_spans = _spans(r.include, res.included_by, norm, index)
        res.exclude_spans = _spans(r.exclude, res.excluded_by, norm, index)
        results.append(res)
    return Evaluation(
        price_cents=price, price_candidates=candidates, results=results, price_index=price_index
    )


def _spans(
    patterns: list[tuple[str, re.Pattern[str]]], terms: list[str], norm: str, index: list[int]
) -> list[tuple[int, int, str]]:
    wanted = set(terms)
    out: list[tuple[int, int, str]] = []
    for term, pat in patterns:
        if term not in wanted:
            continue
        for m in pat.finditer(norm):
            if m.end() > m.start():  # ignora casamentos vazios (ex: /a*/)
                start, end = original_span(index, m.start(), m.end())
                out.append((start, end, term))
    return out
