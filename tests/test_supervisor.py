import asyncio

import pytest
from telethon import errors as tg_errors

from tgmonitor.supervisor import FatalSessionError, Supervisor


class FakeNotifier:
    def __init__(self) -> None:
        self.notices: list[str] = []

    async def send_notice(self, text: str) -> bool:
        self.notices.append(text)
        return True


class FlakyTelegram:
    """Falha N vezes ao conectar, depois fica conectado até ser desconectado."""

    def __init__(self, failures: int, error: BaseException | None = None) -> None:
        self.failures = failures
        self.error = error or ConnectionError("rede")
        self.connected = False
        self.catch_ups = 0
        self._disconnected = asyncio.Event()

    async def connect(self) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        self.connected = True
        self._disconnected = asyncio.Event()  # nova "conexão"

    async def disconnect(self) -> None:
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


def fast_supervisor(tg: FlakyTelegram, notifier: FakeNotifier, threshold: int = 3) -> Supervisor:
    return Supervisor(
        tg, notifier, failure_threshold=threshold, backoff_min_s=0.001, backoff_max_s=0.002
    )


async def wait_for(cond, limit_s: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    async def _loop() -> None:
        while not cond():  # noqa: ASYNC110 - polling simples em teste
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_loop(), limit_s)


async def test_reconnects_and_notifies_after_repeated_failures() -> None:
    tg, notifier = FlakyTelegram(failures=4), FakeNotifier()
    sup = fast_supervisor(tg, notifier, threshold=3)
    task = asyncio.create_task(sup.run())
    await wait_for(lambda: sup.state == "connected")
    assert sup.total_failures == 4
    assert any("caiu" in n for n in notifier.notices)
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


async def test_disconnect_triggers_reconnect() -> None:
    tg, notifier = FlakyTelegram(failures=0), FakeNotifier()
    sup = fast_supervisor(tg, notifier)
    task = asyncio.create_task(sup.run())
    await wait_for(lambda: sup.state == "connected")
    await tg.disconnect()  # simula queda
    await wait_for(lambda: sup.total_failures == 1 and sup.state == "connected")
    assert tg.catch_ups == 2
    task.cancel()
