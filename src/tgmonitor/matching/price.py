"""Extração do preço de uma mensagem de promoção.

Estratégia: encontrar todos os valores "R$ ..." e classificar cada um pelo
contexto imediato (antes e depois), em ordem:

1. FINAL    — preço efetivo: "com cupom: R$ X", "com desconto R$ X",
              "no Pix R$ X", "à vista R$ X", "R$ X no Pix".
2. DESCARTE — não é o preço do produto: valor de cupom/desconto/cashback/frete
              ("cupom de R$ 200", "R$ 200 OFF", "R$ 300 de desconto",
              "frete R$ 19,90") ou parcela ("12x de R$ 458").
3. POR      — "por R$ X", "agora R$ X".
4. NEUTRO   — qualquer outro.

O preço é o MENOR valor do nível de maior prioridade presente. Sem candidatos
válidos → None ("preço não identificado"; o alerta é enviado mesmo assim).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from tgmonitor.matching.normalize import normalize_with_map, original_span


class PriceKind(IntEnum):
    # Valor maior = prioridade maior.
    DISCARD = 0
    NEUTRAL = 1
    POR = 2
    FINAL = 3


@dataclass(frozen=True, slots=True)
class PriceCandidate:
    cents: int
    kind: PriceKind
    raw: str
    reason: str = ""  # motivo curto da classificação ("parcela", "com cupom"...)
    start: int = 0  # posição no texto ORIGINAL (para destacar na tela de teste)
    end: int = 0


_BEFORE_LEN = 40
_AFTER_LEN = 25

_MONEY_RE = re.compile(r"r\$\s*(?P<int>\d{1,3}(?:\.\d{3})+|\d+)(?:,(?P<dec>\d{1,2}))?(?!\d)")

# Código de cupom opcional entre "cupom" e o valor (ex: "com cupom BLACK10: R$ ...").
# "de" nunca é código: "com cupom de R$ 200" é o VALOR do cupom.
_COUPON_CODE = r"(?:\s+(?!de\b)[a-z0-9]{2,20})?"
_SEP = r"[^\w$]*"  # pontuação/espaços entre a palavra-chave e o "R$"


def _before(pattern: str) -> re.Pattern[str]:
    """Palavra-chave imediatamente ANTES do valor."""
    return re.compile(rf"(?:{pattern}){_SEP}$")


def _after(pattern: str) -> re.Pattern[str]:
    """Palavra-chave imediatamente DEPOIS do valor."""
    return re.compile(rf"^\W*(?:{pattern})")


# Ordem = precedência (a primeira que casar decide). "before"/"after" = lado do contexto.
_RULES: list[tuple[PriceKind, str, str, re.Pattern[str]]] = [
    # 1. Preço final escrito antes do valor
    (
        PriceKind.FINAL,
        "com cupom",
        "before",
        _before(rf"\b(?:com|usando)\s+(?:o\s+)?cupom{_COUPON_CODE}"),
    ),
    (PriceKind.FINAL, "com desconto", "before", _before(r"\bcom\s+(?:o\s+)?desconto")),
    (PriceKind.FINAL, "no Pix", "before", _before(r"\b(?:no|via|pelo)\s+pix|\bpix")),
    (PriceKind.FINAL, "à vista", "before", _before(r"\ba\s+vista")),
    # 2. Não é o preço do produto
    (
        PriceKind.DISCARD,
        "valor do cupom",
        "before",
        _before(rf"\bcupo(?:m|ns){_COUPON_CODE}(?:\s+de)?"),
    ),
    (PriceKind.DISCARD, "valor do desconto", "before", _before(r"\b(?:desconto|off)(?:\s+de)?")),
    (PriceKind.DISCARD, "cashback", "before", _before(r"\bcashback(?:\s+de)?")),
    (PriceKind.DISCARD, "frete", "before", _before(r"\bfrete(?:\s+de)?")),
    (PriceKind.DISCARD, "economia", "before", _before(r"\b(?:economize|economia)(?:\s+de)?")),
    (PriceKind.DISCARD, "parcela", "before", _before(r"\d+\s*x(?:\s*de)?|\bparcelas?(?:\s+de)?")),
    (
        PriceKind.DISCARD,
        "valor do desconto",
        "after",
        _after(r"off\b|de\s+desconto\b|(?:em|no|na)\s+desconto\b|a\s+menos\b"),
    ),
    (
        PriceKind.DISCARD,
        "cashback",
        "after",
        _after(r"de\s+(?:cashback|volta)\b|(?:em|no|na)\s+cashback\b"),
    ),
    (PriceKind.DISCARD, "frete", "after", _after(r"de\s+frete\b")),
    # 3. Preço final escrito depois do valor: "R$ 5.199 no Pix".
    #    Mas não "R$ 5.499, com cupom: R$ 5.299" (aí o cupom é do próximo valor).
    (PriceKind.FINAL, "no Pix", "after", _after(r"(?:no|via|pelo)\s+pix\b(?!\W*r\$)")),
    (PriceKind.FINAL, "à vista", "after", _after(r"a\s+vista\b(?!\W*r\$)")),
    (PriceKind.FINAL, "com cupom", "after", _after(r"com\s+(?:o\s+)?cupom\b(?!\W*r\$)")),
    (PriceKind.FINAL, "com desconto", "after", _after(r"com\s+(?:o\s+)?desconto\b(?!\W*r\$)")),
    # 4. "por"
    (
        PriceKind.POR,
        "por",
        "before",
        _before(r"\b(?:por|agora|sai\s+a|sai\s+por)(?:\s+(?:apenas|so|somente))?"),
    ),
]


def _to_cents(int_part: str, dec_part: str | None) -> int:
    reais = int(int_part.replace(".", ""))
    cents = int((dec_part or "0").ljust(2, "0"))
    return reais * 100 + cents


def _classify(before: str, after: str) -> tuple[PriceKind, str]:
    for kind, reason, side, pattern in _RULES:
        if pattern.search(before if side == "before" else after):
            return kind, reason
    return PriceKind.NEUTRAL, "sem contexto"


def analyze_prices(text: str) -> list[PriceCandidate]:
    """Todos os valores encontrados com classificação, motivo e posição no texto original."""
    norm, index = normalize_with_map(text)
    out: list[PriceCandidate] = []
    for m in _MONEY_RE.finditer(norm):
        cents = _to_cents(m.group("int"), m.group("dec"))
        if cents <= 0:
            continue
        before = norm[max(0, m.start() - _BEFORE_LEN) : m.start()]
        after = norm[m.end() : m.end() + _AFTER_LEN]
        kind, reason = _classify(before, after)
        start, end = original_span(index, m.start(), m.end())
        out.append(
            PriceCandidate(
                cents=cents, kind=kind, raw=m.group(0), reason=reason, start=start, end=end
            )
        )
    return out


def pick_index(candidates: list[PriceCandidate]) -> int | None:
    """Índice do candidato usado como preço (menor valor da maior prioridade)."""
    valid = [i for i, c in enumerate(candidates) if c.kind is not PriceKind.DISCARD]
    if not valid:
        return None
    best = max(candidates[i].kind for i in valid)
    return min((i for i in valid if candidates[i].kind == best), key=lambda i: candidates[i].cents)


def pick_price(candidates: list[PriceCandidate]) -> int | None:
    i = pick_index(candidates)
    return None if i is None else candidates[i].cents


def extract_price(text: str) -> int | None:
    """Preço do produto em centavos, ou None se não identificado."""
    return pick_price(analyze_prices(text))


def format_brl(cents: int) -> str:
    reais, cent = divmod(cents, 100)
    return f"R$ {reais:,}".replace(",", ".") + f",{cent:02d}"
