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

from tgmonitor.matching.normalize import normalize


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


_BEFORE_LEN = 40
_AFTER_LEN = 25

_MONEY_RE = re.compile(r"r\$\s*(?P<int>\d{1,3}(?:\.\d{3})+|\d+)(?:,(?P<dec>\d{1,2}))?(?!\d)")

# Código de cupom opcional entre "cupom" e o valor (ex: "com cupom BLACK10: R$ ...").
# "de" nunca é código: "com cupom de R$ 200" é o VALOR do cupom.
_COUPON_CODE = r"(?:\s+(?!de\b)[a-z0-9]{2,20})?"
_SEP = r"[^\w$]*"  # pontuação/espaços entre a palavra-chave e o "R$"

_BEFORE_FINAL_RE = re.compile(
    r"(?:"
    rf"\b(?:com|usando)\s+(?:o\s+)?cupom{_COUPON_CODE}"
    r"|\bcom\s+(?:o\s+)?desconto"
    r"|\b(?:no|via|pelo)\s+pix|\bpix"
    r"|\ba\s+vista"
    rf"){_SEP}$"
)

_BEFORE_DISCARD_RE = re.compile(
    r"(?:"
    rf"\bcupo(?:m|ns){_COUPON_CODE}(?:\s+de)?"
    r"|\b(?:desconto|cashback|frete|economize|economia|off)(?:\s+de)?"
    r"|\d+\s*x(?:\s*de)?"  # parcela: "12x de", "12 x", "10x"
    r"|\bparcelas?(?:\s+de)?"
    rf"){_SEP}$"
)

_AFTER_DISCARD_RE = re.compile(
    r"^\W*(?:"
    r"off\b"
    r"|de\s+(?:desconto|cashback|frete|volta)\b"
    r"|(?:em|no|na)\s+(?:cashback|desconto)\b"
    r"|a\s+menos\b"
    r")"
)

# "R$ 5.199 no Pix" — mas não "R$ 5.499, com cupom: R$ 5.299" (aí o cupom é do próximo valor).
_AFTER_FINAL_RE = re.compile(
    r"^\W*(?:(?:no|via|pelo)\s+pix|a\s+vista|com\s+(?:o\s+)?(?:cupom|desconto))\b(?!\W*r\$)"
)

_BEFORE_POR_RE = re.compile(
    r"\b(?:por|agora|sai\s+a|sai\s+por)(?:\s+(?:apenas|so|somente))?" + _SEP + "$"
)


def _to_cents(int_part: str, dec_part: str | None) -> int:
    reais = int(int_part.replace(".", ""))
    cents = int((dec_part or "0").ljust(2, "0"))
    return reais * 100 + cents


def _classify(before: str, after: str) -> PriceKind:
    if _BEFORE_FINAL_RE.search(before):
        return PriceKind.FINAL
    if _BEFORE_DISCARD_RE.search(before) or _AFTER_DISCARD_RE.search(after):
        return PriceKind.DISCARD
    if _AFTER_FINAL_RE.search(after):
        return PriceKind.FINAL
    if _BEFORE_POR_RE.search(before):
        return PriceKind.POR
    return PriceKind.NEUTRAL


def analyze_prices(text: str) -> list[PriceCandidate]:
    """Todos os valores encontrados com sua classificação (útil para a tela de teste)."""
    norm = normalize(text)
    out: list[PriceCandidate] = []
    for m in _MONEY_RE.finditer(norm):
        cents = _to_cents(m.group("int"), m.group("dec"))
        if cents <= 0:
            continue
        before = norm[max(0, m.start() - _BEFORE_LEN) : m.start()]
        after = norm[m.end() : m.end() + _AFTER_LEN]
        out.append(PriceCandidate(cents=cents, kind=_classify(before, after), raw=m.group(0)))
    return out


def pick_price(candidates: list[PriceCandidate]) -> int | None:
    valid = [c for c in candidates if c.kind is not PriceKind.DISCARD]
    if not valid:
        return None
    best = max(c.kind for c in valid)
    return min(c.cents for c in valid if c.kind == best)


def extract_price(text: str) -> int | None:
    """Preço do produto em centavos, ou None se não identificado."""
    return pick_price(analyze_prices(text))


def format_brl(cents: int) -> str:
    reais, cent = divmod(cents, 100)
    return f"R$ {reais:,}".replace(",", ".") + f",{cent:02d}"
