"""Mantém o listener vivo: reconexão com backoff, watchdog e avisos pelo bot."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from typing import Protocol

from telethon import errors as tg_errors

log = logging.getLogger(__name__)

# Erros que não se resolvem reconectando: a sessão precisa de novo login.
FATAL_SESSION_ERRORS: tuple[type[BaseException], ...] = (
    tg_errors.AuthKeyUnregisteredError,
    tg_errors.AuthKeyDuplicatedError,
    tg_errors.SessionRevokedError,
    tg_errors.SessionExpiredError,
    tg_errors.UserDeactivatedError,
    tg_errors.UserDeactivatedBanError,
)

FATAL_EXIT_CODE = 2  # systemd: RestartPreventExitStatus=2 (evita loop de restart)


class FatalSessionError(Exception):
    pass


class TelegramLike(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    async def is_authorized(self) -> bool: ...
    async def run_until_disconnected(self) -> None: ...
    async def catch_up(self) -> None: ...
    async def get_me_id(self) -> int: ...


class Notifier(Protocol):
    async def send_notice(self, text: str) -> bool: ...


class Supervisor:
    def __init__(
        self,
        tg: TelegramLike,
        notifier: Notifier,
        *,
        failure_threshold: int = 5,
        failure_window_s: float = 15 * 60,
        watchdog_interval_s: float = 5 * 60,
        backoff_min_s: float = 5,
        backoff_max_s: float = 300,
    ) -> None:
        self.tg = tg
        self.notifier = notifier
        self.failure_threshold = failure_threshold
        self.failure_window_s = failure_window_s
        self.watchdog_interval_s = watchdog_interval_s
        self.backoff_min_s = backoff_min_s
        self.backoff_max_s = backoff_max_s

        self.state = "starting"
        self.last_ok: float | None = None
        self.connected_since: float | None = None
        self.total_failures = 0
        self._failures: deque[float] = deque()
        self._degraded_notified = False

    # ---------------------------------------------------------------- estado

    def healthy(self) -> bool:
        return (
            self.state == "connected"
            and self.last_ok is not None
            and time.time() - self.last_ok < self.watchdog_interval_s * 2 + 60
        )

    def status(self) -> dict[str, object]:
        return {
            "state": self.state,
            "healthy": self.healthy(),
            "last_ok": self.last_ok,
            "connected_since": self.connected_since,
            "recent_failures": len(self._failures),
            "total_failures": self.total_failures,
        }

    async def _record_failure(self, reason: str) -> None:
        now = time.time()
        self.total_failures += 1
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.failure_window_s:
            self._failures.popleft()
        log.warning("listener falhou (%s); %d falhas recentes", reason, len(self._failures))
        if len(self._failures) >= self.failure_threshold and not self._degraded_notified:
            self._degraded_notified = True
            await self.notifier.send_notice(
                f"⚠️ tgmonitor: listener caiu {len(self._failures)} vezes nos últimos "
                f"{int(self.failure_window_s // 60)} min (último erro: {reason}). "
                "Continuo tentando reconectar."
            )

    async def _mark_ok(self) -> None:
        self.last_ok = time.time()
        if self._degraded_notified:
            self._degraded_notified = False
            self._failures.clear()
            await self.notifier.send_notice("✅ tgmonitor: listener recuperado.")

    # ------------------------------------------------------------------ loops

    async def run(self) -> None:
        """Loop principal: conecta, escuta até cair, reconecta com backoff."""
        backoff = self.backoff_min_s
        while True:
            try:
                self.state = "connecting"
                if not self.tg.is_connected():
                    await self.tg.connect()
                if not await self.tg.is_authorized():
                    raise FatalSessionError("sessão não autorizada")
                await self.tg.get_me_id()
                self.state = "connected"
                self.connected_since = time.time()
                await self._mark_ok()
                log.info("listener conectado")
                try:
                    # Recupera o que chegou enquanto esteve fora; o pipeline descarta > 3h.
                    await self.tg.catch_up()
                except Exception as e:  # noqa: BLE001
                    log.warning("catch_up falhou: %s", type(e).__name__)
                await self.tg.run_until_disconnected()
                raise ConnectionError("desconectado")
            except asyncio.CancelledError:
                self.state = "stopped"
                raise
            except FATAL_SESSION_ERRORS as e:
                await self._fatal(type(e).__name__)
            except FatalSessionError as e:
                await self._fatal(str(e))
            except Exception as e:  # noqa: BLE001
                stable = self.connected_since and time.time() - self.connected_since > 120
                if stable:
                    backoff = self.backoff_min_s
                self.state = "reconnecting"
                self.connected_since = None
                await self._record_failure(type(e).__name__)
                wait = backoff + random.uniform(0, backoff / 2)  # noqa: S311
                log.info("reconectando em %.0fs", wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, self.backoff_max_s)

    async def _fatal(self, reason: str) -> None:
        self.state = "fatal"
        log.error("sessão inválida (%s); é preciso novo login", reason)
        await self.notifier.send_notice(
            f"🛑 tgmonitor parou: sessão do Telegram inválida ({reason}). "
            "Na VM: pare o serviço e rode `tgmonitor login`."
        )
        raise FatalSessionError(reason)

    async def watchdog(self) -> None:
        """Detecta cliente "vivo porém surdo": get_me periódico; se falhar, força reconexão."""
        while True:
            await asyncio.sleep(self.watchdog_interval_s)
            if self.state != "connected":
                continue
            try:
                await asyncio.wait_for(self.tg.get_me_id(), timeout=30)
                await self._mark_ok()
            except asyncio.CancelledError:
                raise
            except FATAL_SESSION_ERRORS:
                # O loop principal vai receber o mesmo erro ao reconectar e tratar como fatal.
                await self._force_reconnect()
            except Exception as e:  # noqa: BLE001
                log.warning("watchdog: get_me falhou (%s); forçando reconexão", type(e).__name__)
                await self._force_reconnect()

    async def _force_reconnect(self) -> None:
        try:
            await self.tg.disconnect()  # faz run_until_disconnected retornar
        except Exception as e:  # noqa: BLE001
            log.warning("watchdog: erro ao desconectar (%s)", type(e).__name__)
