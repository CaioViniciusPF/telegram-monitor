"""Normalização de texto: sem acento, casefold, espaços colapsados."""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Forma canônica usada pelo matcher, pelo extrator de preço e pelo dedup.

    NFKD também converte caracteres "estilizados" (ex: 𝐆𝐚𝐥𝐚𝐱𝐲, comuns em
    canais de promoção) para ASCII.
    """
    return _WS_RE.sub(" ", strip_accents(text).casefold()).strip()


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Mesmo resultado de normalize(), mais o índice no texto original de cada caractere.

    Usado para destacar trechos no texto colado (a normalização muda tamanhos:
    "𝐒" vira "s", "ﬁ" vira "fi", espaços repetidos viram um).
    """
    chars: list[str] = []
    index: list[int] = []
    for i, c in enumerate(text):
        piece = strip_accents(c).casefold()
        for ch in piece:
            if _WS_RE.fullmatch(ch):
                if chars and chars[-1] != " ":
                    chars.append(" ")
                    index.append(i)
            else:
                chars.append(ch)
                index.append(i)
    if chars and chars[-1] == " ":
        chars.pop()
        index.pop()
    return "".join(chars), index


def original_span(index: list[int], start: int, end: int) -> tuple[int, int]:
    """Converte [start, end) do texto normalizado para o texto original."""
    if start >= end or not index:
        return (index[start], index[start]) if 0 <= start < len(index) else (0, 0)
    return index[start], index[end - 1] + 1
