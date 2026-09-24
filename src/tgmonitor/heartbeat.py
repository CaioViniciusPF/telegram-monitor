"""Heartbeat externo opcional (ex: healthchecks.io).

Faz GET na HEALTHCHECK_URL a cada 5 min SOMENTE enquanto o listener está
saudável. Assim o serviço externo avisa tanto se a VM parar quanto se o
listener travar. Nenhum dado é enviado (sem corpo, sem parâmetros).
A URL é secreta (contém o UUID do check) e nunca é logada.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

log = logging.getLogger(__name__)

INTERVAL_S = 300


async def heartbeat_loop(
    http: httpx.AsyncClient,
    url: str,
    is_healthy: Callable[[], bool],
    interval_s: float = INTERVAL_S,
) -> None:
    await asyncio.sleep(60)  # dá tempo do listener conectar
    while True:
        if is_healthy():
            try:
                resp = await http.get(url, timeout=10)
                if resp.status_code >= 400:
                    log.warning("heartbeat: status %s", resp.status_code)
            except httpx.HTTPError as e:
                log.warning("heartbeat: falha (%s)", type(e).__name__)
        else:
            log.info("heartbeat: listener não saudável, ping suprimido")
        await asyncio.sleep(interval_s)
