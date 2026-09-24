"""CLI: tgmonitor login | run | check-config | gen-token."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import stat
import sys
from pathlib import Path

from pydantic import ValidationError

from tgmonitor.config import MIN_API_TOKEN_LENGTH, Settings, ensure_data_dir
from tgmonitor.log import setup_logging


def _load(env_file: str) -> Settings:
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as e:
        print("Configuração inválida no .env / ambiente:", file=sys.stderr)
        for err in e.errors():
            field = ".".join(str(p) for p in err["loc"]).upper()
            print(f"  - {field}: {err['msg']}", file=sys.stderr)
        raise SystemExit(1) from None


def cmd_gen_token(_: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(max(MIN_API_TOKEN_LENGTH, 32)))
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from tgmonitor.telegram.client import ReadOnlyTelegram

    settings = _load(args.env_file)
    ensure_data_dir(settings.data_dir)

    async def _run() -> str:
        tg = ReadOnlyTelegram(settings)
        try:
            return await tg.interactive_login()
        finally:
            await tg.disconnect()

    print("Login da conta Telegram (o serviço deve estar PARADO).")
    print(
        "Você vai informar o telefone (+55...), o código recebido no app e, se houver, a senha 2FA."
    )
    name = asyncio.run(_run())
    print(f"\nOK: sessão salva para {name} em {settings.session_path}.session (permissão 600).")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from tgmonitor.service import Service

    settings = _load(args.env_file)
    setup_logging(settings.log_level)
    return asyncio.run(Service(settings).run())


def _perm_report(path: Path, expected: int) -> str:
    if not path.exists():
        return "não existe"
    if os.name == "nt":
        return "existe (permissões não verificadas no Windows)"
    mode = stat.S_IMODE(path.stat().st_mode)
    ok = mode & ~expected == 0
    return f"{oct(mode)} {'OK' if ok else f'INSEGURO (esperado {oct(expected)})'}"


def cmd_check_config(args: argparse.Namespace) -> int:
    import httpx

    from tgmonitor.telegram.client import ReadOnlyTelegram

    settings = _load(args.env_file)
    print("Configuração carregada.")
    print(f"  DATA_DIR       {settings.data_dir}  [{_perm_report(settings.data_dir, 0o700)}]")
    session_file = settings.session_path.with_suffix(".session")
    print(f"  sessão         {session_file}  [{_perm_report(session_file, 0o600)}]")
    print(f"  banco          {settings.db_path}  [{_perm_report(settings.db_path, 0o600)}]")
    print(f"  OWNER_USER_ID  {settings.owner_user_id}")
    print(f"  web            http://127.0.0.1:{settings.web_port}")
    print(f"  heartbeat      {'configurado' if settings.healthcheck_url else 'desativado'}")

    ok = True
    try:
        r = httpx.get(
            f"https://api.telegram.org/bot{settings.bot_token.get_secret_value()}/getMe", timeout=15
        )
        data = r.json()
        if data.get("ok"):
            print(f"  bot            @{data['result'].get('username')} OK")
        else:
            ok = False
            print(f"  bot            ERRO: {data.get('description')}")
    except (httpx.HTTPError, ValueError) as e:
        ok = False
        print(f"  bot            ERRO de rede ({type(e).__name__})")

    if session_file.exists():

        async def _check() -> bool:
            tg = ReadOnlyTelegram(settings)
            try:
                await tg.connect()
                return await tg.is_authorized()
            finally:
                await tg.disconnect()

        try:
            authorized = asyncio.run(_check())
        except Exception as e:  # noqa: BLE001
            authorized = False
            print(f"  telegram       ERRO ({type(e).__name__}) — o serviço está rodando?")
        else:
            print(
                f"  telegram       {'sessão autorizada' if authorized else 'sessão NÃO autorizada'}"
            )
        ok = ok and authorized
    else:
        ok = False
        print("  telegram       sem sessão: rode `tgmonitor login`")
    return 0 if ok else 1


def main() -> None:
    os.umask(0o077)  # tudo que o processo criar nasce privado
    parser = argparse.ArgumentParser(prog="tgmonitor", description=__doc__)
    parser.add_argument("--env-file", default=".env", help="caminho do .env (padrão: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="login interativo da conta Telegram (só pela CLI)").set_defaults(
        func=cmd_login
    )
    sub.add_parser("run", help="inicia o monitor").set_defaults(func=cmd_run)
    sub.add_parser("check-config", help="valida .env, permissões, bot e sessão").set_defaults(
        func=cmd_check_config
    )
    sub.add_parser("gen-token", help="gera um API_TOKEN aleatório").set_defaults(func=cmd_gen_token)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
