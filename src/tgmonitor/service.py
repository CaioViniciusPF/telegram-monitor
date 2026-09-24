"""Orquestra tudo em um único event loop: listener, supervisor, bot, web, tarefas."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Iterator
from typing import Any

import httpx
import uvicorn

from tgmonitor import db
from tgmonitor.config import WEB_HOST, Settings, ensure_data_dir
from tgmonitor.heartbeat import heartbeat_loop
from tgmonitor.notifier import BotNotifier
from tgmonitor.pipeline import Pipeline
from tgmonitor.repository import Repository
from tgmonitor.supervisor import FATAL_EXIT_CODE, FatalSessionError, Supervisor
from tgmonitor.telegram.client import ReadOnlyTelegram
from tgmonitor.web.api import AppDeps, create_app

log = logging.getLogger(__name__)

JANITOR_INTERVAL_S = 3600


class _Server(uvicorn.Server):
    """uvicorn sem tratamento próprio de sinais: o Service cuida disso para poder
    fechar Telethon e SQLite de forma limpa (o uvicorn re-dispara o SIGTERM ao sair)."""

    def install_signal_handlers(self) -> None:  # uvicorn < 0.29
        pass

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:  # uvicorn >= 0.29
        yield


class Service:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = time.time()

    async def run(self) -> int:
        s = self.settings
        ensure_data_dir(s.data_dir)
        conn = await db.connect(s.db_path)
        repo = Repository(conn)
        tg = ReadOnlyTelegram(s)

        await tg.connect()
        if not await tg.is_authorized():
            log.error("sessão do Telegram ausente ou inválida: rode `tgmonitor login` antes")
            await tg.disconnect()
            await conn.close()
            return FATAL_EXIT_CODE

        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as http:
            bot = BotNotifier(http, s.bot_token.get_secret_value(), s.owner_user_id)
            pipeline = Pipeline(repo, bot)
            await pipeline.reload()
            tg.on_message(pipeline.handle)
            supervisor = Supervisor(tg, bot)

            async def refresh_chats() -> int:
                if supervisor.state != "connected":
                    raise ConnectionError("listener desconectado")
                dialogs = await tg.list_dialogs()
                await repo.upsert_chats((d.id, d.title, d.username, d.kind) for d in dialogs)
                await pipeline.reload()
                log.info("lista de chats atualizada: %d", len(dialogs))
                return len(dialogs)

            def health() -> dict[str, Any]:
                return {
                    "listener": supervisor.status(),
                    "uptime_s": int(time.time() - self.started_at),
                    "paused": pipeline.settings.paused,
                    "active_rules": len(pipeline.rules),
                    "monitored_chats": len(pipeline.monitored),
                    "last_message_at": pipeline.last_message_at,
                    "stats": dict(pipeline.stats),
                    "heartbeat_enabled": s.healthcheck_url is not None,
                }

            self._register_commands(bot, repo, pipeline, health)

            app = create_app(
                AppDeps(
                    api_token=s.api_token.get_secret_value(),
                    repo=repo,
                    pipeline=pipeline,
                    refresh_chats=refresh_chats,
                    health=health,
                )
            )
            server = _Server(
                uvicorn.Config(
                    app,
                    host=WEB_HOST,
                    port=s.web_port,
                    log_level="warning",
                    access_log=False,
                    proxy_headers=False,
                    server_header=False,
                    date_header=False,
                    lifespan="off",
                )
            )

            background = [
                asyncio.create_task(supervisor.run(), name="supervisor"),
                asyncio.create_task(supervisor.watchdog(), name="watchdog"),
                asyncio.create_task(bot.poll_commands(), name="bot-commands"),
                asyncio.create_task(self._janitor(repo, pipeline), name="janitor"),
                asyncio.create_task(self._initial_chat_sync(repo, supervisor, refresh_chats)),
            ]
            if s.healthcheck_url is not None:
                background.append(
                    asyncio.create_task(
                        heartbeat_loop(
                            http, s.healthcheck_url.get_secret_value(), supervisor.healthy
                        ),
                        name="heartbeat",
                    )
                )
            web = asyncio.create_task(server.serve(), name="web")
            log.info("tgmonitor iniciado; interface em http://%s:%d", WEB_HOST, s.web_port)

            def _stop() -> None:
                server.should_exit = True

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):  # Windows
                    loop.add_signal_handler(sig, _stop)

            # Encerra quando o web server para (sinal) ou o supervisor morre (erro fatal).
            exit_code = 0
            sup_task = background[0]
            done, _ = await asyncio.wait([web, sup_task], return_when=asyncio.FIRST_COMPLETED)
            if sup_task in done:
                exc = sup_task.exception()
                if isinstance(exc, FatalSessionError):
                    exit_code = FATAL_EXIT_CODE
                elif exc is not None:
                    log.error("supervisor terminou com erro: %s", type(exc).__name__)
                    exit_code = 1
                server.should_exit = True
            elif web.exception() is not None:
                log.error("servidor web falhou: %s", type(web.exception()).__name__)
                exit_code = 1

            for t in background:
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(web, timeout=10)
            await asyncio.gather(*background, return_exceptions=True)

        with contextlib.suppress(Exception):
            await tg.disconnect()
        await conn.close()
        log.info("tgmonitor encerrado (código %d)", exit_code)
        return exit_code

    @staticmethod
    async def _initial_chat_sync(repo: Repository, supervisor: Supervisor, refresh: Any) -> None:
        """Na primeira execução (tabela vazia), popula a lista de chats automaticamente."""
        if await repo.list_chats():
            return
        for _ in range(60):
            if supervisor.state == "connected":
                with contextlib.suppress(Exception):
                    await refresh()
                return
            await asyncio.sleep(2)

    @staticmethod
    async def _janitor(repo: Repository, pipeline: Pipeline) -> None:
        while True:
            try:
                st = pipeline.settings
                alerts, fps = await repo.purge(
                    retention_days=st.alert_retention_days, window_hours=st.dedup_window_hours
                )
                if alerts or fps:
                    log.info("limpeza: %d alertas e %d fingerprints removidos", alerts, fps)
            except Exception as e:  # noqa: BLE001
                log.warning("limpeza falhou: %s", type(e).__name__)
            await asyncio.sleep(JANITOR_INTERVAL_S)

    @staticmethod
    def _register_commands(
        bot: BotNotifier, repo: Repository, pipeline: Pipeline, health: Any
    ) -> None:
        async def status() -> str:
            h = health()
            li = h["listener"]
            st = h["stats"]
            last = h["last_message_at"]
            ago = f"{int((time.time() - last) / 60)} min atrás" if last else "nenhuma ainda"
            return (
                f"📊 tgmonitor\n"
                f"Listener: {li['state']} ({'ok' if li['healthy'] else 'com problemas'})\n"
                f"Pausado: {'sim' if h['paused'] else 'não'}\n"
                f"Regras ativas: {h['active_rules']} | Chats: {h['monitored_chats']}\n"
                f"Última mensagem monitorada: {ago}\n"
                f"Alertas: {st.get('alerts', 0)} | Quedas de preço: {st.get('price_drops', 0)} | "
                f"Deduplicados: "
                f"{st.get('dedup', 0) + st.get('dedup_same_post', 0)} | "
                f"Falhas de conexão: {li['total_failures']}\n"
                f"Uptime: {h['uptime_s'] // 3600}h{(h['uptime_s'] % 3600) // 60:02d}m"
            )

        async def set_paused(value: bool) -> str:
            st = await repo.get_settings()
            st.paused = value
            await repo.save_settings(st)
            await pipeline.reload()
            return "⏸️ Alertas pausados." if value else "▶️ Alertas retomados."

        async def pause() -> str:
            return await set_paused(True)

        async def resume() -> str:
            return await set_paused(False)

        async def help_() -> str:
            return (
                "Comandos:\n/status — estado do monitor\n/pause — pausar alertas\n"
                "/resume — retomar alertas\n\nA configuração é feita pela interface web "
                "(túnel SSH)."
            )

        bot.command("status", status)
        bot.command("pause", pause)
        bot.command("resume", resume)
        bot.command("help", help_)
        bot.command("start", help_)
