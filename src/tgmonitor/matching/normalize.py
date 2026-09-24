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
