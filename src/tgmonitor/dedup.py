"""Fingerprints para deduplicação de alertas.

Um alerta candidato gera vários fingerprints; se QUALQUER um já existir dentro
da janela configurada, o alerta é suprimido.

- text:        texto normalizado sem URLs, emojis e pontuação.
- url:         URL normalizada + preço (mesmo link com preço menor gera alerta
               novo); sem preço identificado, só a URL.
- rules_price: ids das regras casadas + preço. Pega o mesmo produto repostado
               com links de afiliado diferentes em cada grupo. Só gerado
               quando há preço (senão colapsaria todo "preço não identificado").
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tgmonitor.matching.normalize import normalize

FingerprintKind = Literal["text", "url", "rules_price"]

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^\w]+")
_TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "igshid", "mc_cid", "mc_eid", "ref_src", "_ga"}
)
_MIN_TEXT_LEN = 10  # textos curtos demais ("oi") não viram fingerprint


@dataclass(frozen=True, slots=True)
class Fingerprint:
    kind: FingerprintKind
    hash: str


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_urls(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;:!?") for m in _URL_RE.finditer(text)]


def normalize_url(url: str) -> str:
    if not re.match(r"https?://", url, re.IGNORECASE):
        url = "http://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    path = parts.path.rstrip("/") or ""
    # Esquema ignorado de propósito: http e https do mesmo link são a mesma promoção.
    return urlunsplit(("", host, path, urlencode(query), "")).lstrip("/")


def text_signature(text: str) -> str:
    """Texto sem URLs, emojis e pontuação — estável entre reposts com enfeites diferentes."""
    without_urls = _URL_RE.sub(" ", text)
    norm = normalize(without_urls)
    return " ".join(_NON_WORD_RE.sub(" ", norm).replace("_", " ").split())


def fingerprints(
    text: str,
    rule_ids: Iterable[int],
    price_cents: int | None,
    extra_urls: Iterable[str] = (),
) -> list[Fingerprint]:
    """extra_urls: links "escondidos" em entidades do Telegram (texto clicável)."""
    out: list[Fingerprint] = []

    sig = text_signature(text)
    if len(sig) >= _MIN_TEXT_LEN:
        out.append(Fingerprint("text", _sha(sig)))

    price_part = "" if price_cents is None else f"|{price_cents}"
    seen: set[str] = set()
    for url in [*extract_urls(text), *extra_urls]:
        nurl = normalize_url(url)
        if nurl and nurl not in seen:
            seen.add(nurl)
            out.append(Fingerprint("url", _sha(nurl + price_part)))

    ids = sorted(set(rule_ids))
    if ids and price_cents is not None:
        out.append(Fingerprint("rules_price", _sha(",".join(map(str, ids)) + price_part)))

    return out
