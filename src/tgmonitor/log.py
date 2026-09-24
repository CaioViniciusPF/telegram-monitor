"""Logging sem texto de mensagens nem dados pessoais.

Regra do projeto: logs só carregam ids numéricos, contadores e estados.
Como defesa extra, um filtro mascara tokens de bot e URLs que escapem em
mensagens de exceção de bibliotecas (ex: httpx inclui a URL, que contém o token).
"""

from __future__ import annotations

import logging
import re
import traceback

_BOT_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
_URL_RE = re.compile(r"https?://\S+")


def redact(text: str) -> str:
    text = _BOT_TOKEN_RE.sub("<bot-token>", text)
    return _URL_RE.sub("<url>", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # Traceback formatado e redigido (a mensagem da exceção pode ter URL/token).
            record.exc_text = redact("".join(traceback.format_exception(record.exc_info[1])))
            record.exc_info = None
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx loga cada request com a URL completa (inclui o token do bot).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
