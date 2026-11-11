"""Mantém o listener vivo: reconexão serializada, watchdogs e avisos pelo bot.

Com `auto_reconnect=False` no cliente do Telethon (ver telegram/client.py), TODA queda de
conexão sobe até aqui como uma exceção de `run_until_disconnected()`. Distinguimos dois
tipos de evento:

- "queda": a conexão caiu, mas a PRÓXIMA tentativa de reconectar (a primeira, logo em
  seguida) funciona. É normal e esperado (rede, DC que fecha conexões ociosas); só é
  logado em INFO com um contador, sem avisar no bot.
- "falha": uma tentativa de reconexão em si não funcionou. Só isso conta para o alerta
  "N falhas em M minutos" no bot — exatamente o que indica um problema persistente.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import Callable
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

# Logger de onde vem o aviso "Server closed the connection: ..." (telethon/network/
# connection/connection.py). Confirmado lendo o código-fonte do Telethon 1.45.0.
_CONNECTION_LOGGER = "telethon.network.connection.connection"
_DROP_MESSAGE_PREFIX = "Server closed the connection"


class FatalSessionError(Exception):
    """Sessão do Telegram inválida; precisa de novo `tgmonitor login`."""


class UpdatesStalledError(Exception):
    """Nenhum update de nenhum chat chegou por tempo demais, mesmo após reconexão forçada."""


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


class _DropCounter(logging.Handler):
    """Conta, sem reemitir nada, quantas vezes o Telethon logou "Server closed the
    connection" — útil para o /status, sem precisar repetir a varredura do journalctl."""

    def __init__(self, clock: Callable[[], float]) -> None:
        super().__init__(level=logging.WARNING)
        self._clock = clock
        self._events: deque[float] = deque(maxlen=5000)

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith(_DROP_MESSAGE_PREFIX):
            self._events.append(self._clock())

    def count_since(self, since: float) -> int:
        return sum(1 for t in self._events if t >= since)


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
        drop_retry_delay_s: float = 1,
        update_silence_s: float = 30 * 60,
        update_check_interval_s: float = 60,
        catch_up_interval_s: float = 10 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.tg = tg
        self.notifier = notifier
        self.failure_threshold = failure_threshold
        self.failure_window_s = failure_window_s
        self.watchdog_interval_s = watchdog_interval_s
        self.backoff_min_s = backoff_min_s
        self.backoff_max_s = backoff_max_s
        self.drop_retry_delay_s = drop_retry_delay_s
        self.update_silence_s = update_silence_s
        self.update_check_interval_s = update_check_interval_s
        self.catch_up_interval_s = catch_up_interval_s
        self.clock = clock

        self.state = "starting"
        self.last_ok: float | None = None
        self.connected_since: float | None = None
        self.last_update_at: float | None = None
        self.total_failures = 0
        self.total_drops = 0
        self._failures: deque[float] = deque()
        self._degraded_notified = False
        self._updates_watchdog_forced = False

        self._drop_counter = _DropCounter(clock)
        conn_logger = logging.getLogger(_CONNECTION_LOGGER)
        for h in list(conn_logger.handlers):  # no máx. 1 instância ativa (útil em testes)
            if isinstance(h, _DropCounter):
                conn_logger.removeHandler(h)
        conn_logger.addHandler(self._drop_counter)

    # ---------------------------------------------------------------- estado

    def _update_reference(self) -> float | None:
        """Instante do sinal de vida mais recente: um update ou uma (re)conexão."""
        candidates = [t for t in (self.last_update_at, self.connected_since) if t is not None]
        return max(candidates) if candidates else None

    def note_update(self) -> None:
        """Chamado para TODO evento recebido, de qualquer chat (ver service.py)."""
        self.last_update_at = self.clock()
        self._updates_watchdog_forced = False

    def healthy(self) -> bool:
        return (
            self.state == "connected"
            and self.last_ok is not None
            and self.clock() - self.last_ok < self.watchdog_interval_s * 2 + 60
        )

    def updates_flowing(self) -> bool:
        """Para o heartbeat (ver heartbeat.py): além de "conectado", exige sinal de vida
        recente de updates — a própria falha que este módulo existe para detectar."""
        if not self.healthy():
            return False
        ref = self._update_reference()
        return ref is not None and self.clock() - ref < self.update_silence_s

    def status(self) -> dict[str, object]:
        return {
            "state": self.state,
            "healthy": self.healthy(),
            "last_ok": self.last_ok,
            "connected_since": self.connected_since,
            "last_update_at": self.last_update_at,
            "recent_failures": len(self._failures),
            "total_failures": self.total_failures,
            "total_drops": self.total_drops,
            "connection_drops_1h": self._drop_counter.count_since(self.clock() - 3600),
        }

    async def _record_failure(self, reason: str) -> None:
        """Uma tentativa de RECONEXÃO falhou (não uma simples queda). Conta para o aviso."""
        now = self.clock()
        self.total_failures += 1
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.failure_window_s:
            self._failures.popleft()
        log.warning("falha ao reconectar (%s); %d falhas recentes", reason, len(self._failures))
        if len(self._failures) >= self.failure_threshold and not self._degraded_notified:
            self._degraded_notified = True
            await self.notifier.send_notice(
                f"⚠️ tgmonitor: {len(self._failures)} tentativas de reconexão falharam nos "
                f"últimos {int(self.failure_window_s // 60)} min (último erro: {reason}). "
                "Continuo tentando reconectar."
            )

    def _record_drop(self, reason: str, exc: BaseException | None) -> None:
        """A conexão caiu, mas isso sozinho não indica problema (reconectamos na hora em
        seguida). Log em INFO com contador e o traceback completo anexado (útil para
        diagnóstico), mas sem o alarde de um ERROR e sem avisar no bot."""
        self.total_drops += 1
        log.info(
            "conexão caiu (%s); %d quedas desde o início", reason, self.total_drops, exc_info=exc
        )

    async def _mark_ok(self) -> None:
        self.last_ok = self.clock()
        if self._degraded_notified:
            self._degraded_notified = False
            self._failures.clear()
            await self.notifier.send_notice("✅ tgmonitor: listener recuperado.")

    # ------------------------------------------------------------------ loops

    async def run(self) -> None:
        """Loop principal: (re)conecta de forma serializada e escuta até cair.

        Cada volta sempre desconecta antes de conectar (garante que nunca ficamos com
        estado de conexão ambíguo) e sempre roda catch_up logo após conectar.
        """
        backoff = self.backoff_min_s
        while True:
            self.state = "connecting"
            try:
                await self.tg.disconnect()
            except asyncio.CancelledError:
                self.state = "stopped"
                raise
            except Exception as e:  # noqa: BLE001 - disconnect de um cliente morto não deve parar nada
                log.debug("disconnect preventivo falhou (%s); seguindo", type(e).__name__)

            try:
                await self.tg.connect()
                if not await self.tg.is_authorized():
                    raise FatalSessionError("sessão não autorizada")
                await self.tg.get_me_id()
            except asyncio.CancelledError:
                self.state = "stopped"
                raise
            except FATAL_SESSION_ERRORS as e:
                await self._fatal(type(e).__name__)
            except FatalSessionError as e:
                await self._fatal(str(e))
            except Exception as e:
                log.exception("falha ao conectar/autenticar")
                self.state = "reconnecting"
                self.connected_since = None
                await self._record_failure(type(e).__name__)
                wait = backoff + random.uniform(0, backoff / 2)  # noqa: S311
                log.info("tentando reconectar em %.0fs", wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, self.backoff_max_s)
                continue

            # Conectado com sucesso (de primeira ou após algumas falhas).
            self.state = "connected"
            self.connected_since = self.clock()
            backoff = self.backoff_min_s
            await self._mark_ok()
            log.info("listener conectado")
            try:
                # Recupera o que chegou enquanto esteve fora; o pipeline descarta > 3h.
                await self.tg.catch_up()
            except Exception as e:  # noqa: BLE001
                log.warning("catch_up falhou: %s", type(e).__name__)

            drop_exc: Exception | None = None
            try:
                await self.tg.run_until_disconnected()
                reason = "desconectado"  # retornou sem erro (ex: keepalive sem resposta)
            except asyncio.CancelledError:
                self.state = "stopped"
                raise
            except FATAL_SESSION_ERRORS as e:
                await self._fatal(type(e).__name__)
                continue  # _fatal sempre levanta; evita cair nas linhas de "queda" abaixo
            except FatalSessionError as e:
                await self._fatal(str(e))
                continue  # idem
            except Exception as e:
                reason = type(e).__name__
                drop_exc = e

            self.state = "reconnecting"
            self.connected_since = None
            self._record_drop(reason, drop_exc)
            # Tenta reconectar logo em seguida (é só uma queda); um atraso pequeno evita
            # um loop apertado no caso raro de cair de novo instantaneamente.
            delay = self.drop_retry_delay_s
            await asyncio.sleep(delay + random.uniform(0, delay))  # noqa: S311

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

    async def updates_watchdog(self) -> None:
        """Se nenhum update (de QUALQUER chat, monitorado ou não) chegar por
        `update_silence_s`, força uma reconexão completa, sem avisar no bot — é o próprio
        mecanismo de defesa contra o caso em que a conexão "parece viva" mas os updates
        pararam de chegar silenciosamente (ver supervisor.py, docstring do módulo).

        Se o silêncio persistir por mais `update_silence_s` mesmo depois dessa reconexão
        forçada, levanta `UpdatesStalledError` para o processo ser encerrado (quem decide
        se isso vale um aviso no bot é o service.py, olhando o histórico de reinícios).
        """
        while True:
            await asyncio.sleep(self.update_check_interval_s)
            if self.state != "connected":
                continue
            ref = self._update_reference()
            if ref is None:
                continue
            silence = self.clock() - ref
            if silence < self.update_silence_s:
                continue
            if not self._updates_watchdog_forced:
                self._updates_watchdog_forced = True
                log.warning(
                    "nenhum update de nenhum chat há %.0f min; forçando reconexão completa",
                    silence / 60,
                )
                await self._force_reconnect()
            else:
                log.error(
                    "nenhum update de nenhum chat há %.0f min mesmo após reconexão forçada; "
                    "encerrando processo para o systemd reiniciar",
                    silence / 60,
                )
                raise UpdatesStalledError(f"sem updates por {silence:.0f}s mesmo após reconexão")

    async def periodic_catch_up(self) -> None:
        """Roda catch_up a cada `catch_up_interval_s` enquanto conectado, independente dos
        watchdogs: uma tentativa leve e não disruptiva de ressincronizar updates sem
        precisar de uma reconexão completa. Mensagens recuperadas ainda passam pelo limite
        de idade normal do pipeline (settings.max_new_message_age_hours)."""
        while True:
            await asyncio.sleep(self.catch_up_interval_s)
            if self.state != "connected":
                continue
            try:
                await self.tg.catch_up()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("catch_up periódico falhou: %s", type(e).__name__)

    async def _force_reconnect(self) -> None:
        try:
            await self.tg.disconnect()  # faz run_until_disconnected retornar/levantar
        except Exception as e:  # noqa: BLE001
            log.warning("erro ao forçar desconexão (%s)", type(e).__name__)
