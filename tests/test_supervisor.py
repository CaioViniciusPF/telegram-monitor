import asyncio
import logging

import pytest
from telethon import errors as tg_errors

from tgmonitor.supervisor import FatalSessionError, Supervisor, UpdatesStalledError


class FakeNotifier:
    def __init__(self) -> None:
        self.notices: list[str] = []

    async def send_notice(self, text: str) -> bool:
        self.notices.append(text)
        return True


class FlakyTelegram:
    """Falha N vezes ao conectar, depois fica conectado até ser desconectado.

    Com `auto_reconnect=False` (o modo real do projeto), QUALQUER queda — mesmo a mais
    transitória — sobe como exceção/retorno de `run_until_disconnected()`; não há mais
    reconexão silenciosa por baixo. É exatamente isso que o fake simula: uma vez
    desconectado (por `disconnect()` ou porque `connect()` falhou), fica desconectado até
    o Supervisor chamar `connect()` de novo.
    """

    def __init__(self, failures: int, error: BaseException | None = None) -> None:
        self.failures = failures
        self.error = error or ConnectionError("rede")
        self.connected = False
        self.calls: list[str] = []  # ordem exata de connect()/disconnect() chamados
        self.catch_ups = 0
        self.add_event_handler_calls = 0
        self._disconnected = asyncio.Event()

    async def connect(self) -> None:
        self.calls.append("connect")
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        self.connected = True
        self._disconnected = asyncio.Event()  # nova "conexão"

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.connected = False
        self._disconnected.set()

    def is_connected(self) -> bool:
        return self.connected

    async def is_authorized(self) -> bool:
        return True

    async def run_until_disconnected(self) -> None:
        await self._disconnected.wait()

    async def catch_up(self) -> None:
        self.catch_ups += 1

    async def get_me_id(self) -> int:
        return 1

    def on_message(self, handler: object) -> None:
        self.add_event_handler_calls += 1


def fast_supervisor(
    tg: FlakyTelegram, notifier: FakeNotifier, threshold: int = 3, **kwargs: object
) -> Supervisor:
    return Supervisor(
        tg,
        notifier,
        failure_threshold=threshold,
        backoff_min_s=0.001,
        backoff_max_s=0.002,
        drop_retry_delay_s=0.001,
        update_check_interval_s=0.01,
        **kwargs,  # type: ignore[arg-type]
    )


async def wait_for(cond, limit_s: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    async def _loop() -> None:
        # Sempre cede o loop de eventos ao menos uma vez antes do primeiro check: sem
        # isso, checar logo após uma chamada síncrona (ex: `await tg.disconnect()` de um
        # fake sem nenhum `await` interno) pode ver um estado "antigo" que ainda nem foi
        # processado pela task que estamos esperando, e `wait_for` retornaria na hora,
        # de forma enganosa, achando que já tinha acontecido.
        await asyncio.sleep(0)
        while not cond():  # noqa: ASYNC110 - polling simples em teste
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_loop(), limit_s)


async def test_reconnects_and_notifies_after_repeated_failures() -> None:
    """Falhas repetidas ao RECONECTAR (não meras quedas) contam e avisam no bot."""
    tg, notifier = FlakyTelegram(failures=4), FakeNotifier()
    sup = fast_supervisor(tg, notifier, threshold=3)
    task = asyncio.create_task(sup.run())
    await wait_for(lambda: sup.state == "connected")
    assert sup.total_failures == 4
    assert sup.total_drops == 0
    assert any("falharam" in n for n in notifier.notices)
    assert any("recuperado" in n for n in notifier.notices)
    assert tg.catch_ups == 1
    assert sup.healthy()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_few_failures_do_not_notify() -> None:
    tg, notifier = FlakyTelegram(failures=2), FakeNotifier()
    sup = fast_supervisor(tg, notifier, threshold=5)
    task = asyncio.create_task(sup.run())
    await wait_for(lambda: sup.state == "connected")
    assert notifier.notices == []
    task.cancel()


async def test_revoked_session_is_fatal() -> None:
    tg = FlakyTelegram(failures=1, error=tg_errors.AuthKeyUnregisteredError(request=None))
    notifier = FakeNotifier()
    sup = fast_supervisor(tg, notifier)
    with pytest.raises(FatalSessionError):
        await sup.run()
    assert sup.state == "fatal"
    assert "tgmonitor login" in notifier.notices[0]


async def test_transient_drop_does_not_count_as_failure_or_notify() -> None:
    """Núcleo da correção do incidente: uma queda que reconecta de primeira (o caso comum
    com auto_reconnect=False, ex: "Server closed the connection" a cada poucos minutos)
    não pode contar para o alerta "N falhas" nem gerar mensagem no bot — só um log INFO
    com contador próprio (total_drops)."""
    tg, notifier = FlakyTelegram(failures=0), FakeNotifier()
    sup = fast_supervisor(tg, notifier, threshold=1)
    task = asyncio.create_task(sup.run())
    await wait_for(lambda: sup.state == "connected")

    await tg.disconnect()  # simula "Server closed the connection"
    await wait_for(lambda: sup.total_drops == 1 and sup.state == "connected")

    assert sup.total_failures == 0
    assert notifier.notices == []  # nada de bot para uma queda isolada
    assert tg.catch_ups == 2  # catch_up rodou de novo após a reconexão
    task.cancel()


async def test_many_transient_drops_never_notify() -> None:
    """O cenário real do incidente: uma queda a cada poucos segundos, todas reconectando
    de primeira. Com auto_reconnect=True isso seria invisível (o bug); aqui, cada uma é
    uma queda contada e logada, mas nenhuma delas soma para o alerta de falhas."""
    tg, notifier = FlakyTelegram(failures=0), FakeNotifier()
    sup = fast_supervisor(tg, notifier, threshold=3)
    task = asyncio.create_task(sup.run())
    await wait_for(lambda: sup.state == "connected")

    for _ in range(10):
        await tg.disconnect()
        await wait_for(lambda: sup.state == "connected")

    assert sup.total_drops == 10
    assert sup.total_failures == 0
    assert notifier.notices == []
    task.cancel()


def _assert_connect_always_preceded_by_disconnect(calls: list[str]) -> None:
    for i, call in enumerate(calls):
        if call == "connect":
            assert i > 0 and calls[i - 1] == "disconnect", (
                f"connect() na posição {i} sem disconnect() imediatamente antes: {calls}"
            )


async def test_run_always_disconnects_before_connecting() -> None:
    """Garante o pedido da tarefa 1: nunca conectar sem antes desconectar o estado
    anterior — mesmo na primeiríssima volta do loop (cliente já conectado por fora) e
    mesmo quando quem desconectou foi um evento externo (simulado abaixo)."""
    tg, notifier = FlakyTelegram(failures=0), FakeNotifier()
    sup = fast_supervisor(tg, notifier)
    task = asyncio.create_task(sup.run())
    await wait_for(lambda: sup.state == "connected")
    assert tg.calls == ["disconnect", "connect"]

    await tg.disconnect()  # queda externa
    await wait_for(lambda: sup.state == "connected")
    _assert_connect_always_preceded_by_disconnect(tg.calls)
    assert tg.calls.count("connect") == 2
    task.cancel()


async def test_handlers_registered_only_once_across_reconnects() -> None:
    """on_message é chamado uma única vez (em service.py), antes do supervisor começar;
    reconexões (quedas ou falhas) não devem registrar handlers de novo no cliente."""
    tg, notifier = FlakyTelegram(failures=3), FakeNotifier()
    tg.on_message(lambda msg: None)  # como service.py faz, uma vez, antes do supervisor
    assert tg.add_event_handler_calls == 1

    sup = fast_supervisor(tg, notifier, threshold=10)
    task = asyncio.create_task(sup.run())
    await wait_for(lambda: sup.state == "connected")  # passou por 3 falhas de conexão
    await tg.disconnect()
    await wait_for(lambda: sup.total_drops == 1 and sup.state == "connected")  # +1 queda

    assert tg.add_event_handler_calls == 1  # nunca mais do que a única vez inicial
    task.cancel()


async def test_updates_watchdog_forces_reconnect_once_then_recovers() -> None:
    """Primeira vez que fica 'silencioso': força reconexão (sem bot). Se um update chega
    logo depois, o relógio reinicia e nada mais acontece."""
    clock = [1000.0]
    tg, notifier = FlakyTelegram(failures=0), FakeNotifier()
    sup = fast_supervisor(tg, notifier, clock=lambda: clock[0], update_silence_s=30)
    run_task = asyncio.create_task(sup.run())
    watch_task = asyncio.create_task(sup.updates_watchdog())
    await wait_for(lambda: sup.state == "connected")

    clock[0] += 31  # passou o limite sem nenhum update
    await wait_for(lambda: sup._updates_watchdog_forced)
    await wait_for(lambda: sup.state == "connected")  # reconectou sozinho
    assert notifier.notices == []  # reconexão forçada não avisa no bot

    sup.note_update()  # um update chega
    assert sup._updates_watchdog_forced is False  # relógio reiniciado

    run_task.cancel()
    watch_task.cancel()


async def test_updates_watchdog_raises_if_silence_persists() -> None:
    """Se, mesmo após a reconexão forçada, continuar sem updates: desiste (processo
    reinicia via service.py). Isso também não manda mensagem no bot sozinho."""
    clock = [1000.0]
    tg, notifier = FlakyTelegram(failures=0), FakeNotifier()
    sup = fast_supervisor(tg, notifier, clock=lambda: clock[0], update_silence_s=30)
    run_task = asyncio.create_task(sup.run())
    watch_task = asyncio.create_task(sup.updates_watchdog())
    await wait_for(lambda: sup.state == "connected")

    clock[0] += 31
    await wait_for(lambda: sup._updates_watchdog_forced)
    await wait_for(lambda: sup.state == "connected")

    clock[0] += 31  # silêncio persiste mesmo depois da reconexão forçada
    with pytest.raises(UpdatesStalledError):
        await asyncio.wait_for(watch_task, timeout=2)
    assert notifier.notices == []  # quem decide avisar é o service.py, não o Supervisor

    run_task.cancel()


async def test_updates_flowing_uses_connected_since_before_first_update() -> None:
    """Logo após conectar, sem nenhum update ainda, o heartbeat não deve falsamente achar
    que travou (evita alarme falso no healthchecks.io a cada reinício normal)."""
    clock = [1000.0]
    tg, notifier = FlakyTelegram(failures=0), FakeNotifier()
    # watchdog_interval_s bem maior que o salto de relógio do teste: isolar o que
    # queremos testar (updates_flowing) de healthy() ficar "velho" só porque nada aqui
    # chama o watchdog() de get_me (que, na vida real, roda à parte e mantém last_ok).
    sup = fast_supervisor(
        tg, notifier, clock=lambda: clock[0], update_silence_s=1800, watchdog_interval_s=100_000
    )
    run_task = asyncio.create_task(sup.run())
    await wait_for(lambda: sup.state == "connected")

    assert sup.updates_flowing() is True  # acabou de conectar, ainda dentro da janela
    clock[0] += 1801
    assert sup.updates_flowing() is False  # 30 min sem nada, nem conexão nem update

    sup.note_update()
    assert sup.updates_flowing() is True

    run_task.cancel()


async def test_connection_drop_counter() -> None:
    """Conta ocorrências de 'Server closed the connection' vindas do logger do Telethon,
    sem precisar repetir a varredura do journalctl manualmente."""
    clock = [1000.0]
    tg, notifier = FlakyTelegram(failures=0), FakeNotifier()
    sup = fast_supervisor(tg, notifier, clock=lambda: clock[0])

    conn_log = logging.getLogger("telethon.network.connection.connection")
    conn_log.warning("Server closed the connection: 0 bytes read on a total of 8 expected bytes")
    clock[0] += 10
    conn_log.warning("Server closed the connection: 0 bytes read on a total of 8 expected bytes")
    conn_log.warning("Invalid buffer stuff")  # não deve contar

    assert sup.status()["connection_drops_1h"] == 2
    assert sup._drop_counter.count_since(clock[0] - 1) == 1
