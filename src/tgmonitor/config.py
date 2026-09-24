"""Configuração lida do .env (nunca do banco ou da web)."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Host fixo no código de propósito: não é configurável para evitar exposição acidental.
WEB_HOST = "127.0.0.1"
MIN_API_TOKEN_LENGTH = 32


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "telegram-monitor"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tg_api_id: int
    tg_api_hash: SecretStr
    bot_token: SecretStr
    owner_user_id: int
    api_token: SecretStr
    data_dir: Path = Field(default_factory=_default_data_dir)
    web_port: int = 8000
    healthcheck_url: SecretStr | None = None
    log_level: str = "INFO"

    @field_validator("api_token")
    @classmethod
    def _api_token_strong(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < MIN_API_TOKEN_LENGTH:
            raise ValueError(
                f"API_TOKEN precisa ter pelo menos {MIN_API_TOKEN_LENGTH} caracteres "
                "(gere com: tgmonitor gen-token)"
            )
        return v

    @field_validator("healthcheck_url", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        return None if v == "" else v

    @property
    def session_path(self) -> Path:
        # Telethon acrescenta ".session" ao nome.
        return self.data_dir / "account"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.db"


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # campos vêm do ambiente/.env


def ensure_data_dir(path: Path) -> None:
    """Cria o diretório de dados com permissão 700."""
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # FS sem suporte a chmod (ex: Windows)
        path.chmod(0o700)


def secure_file(path: Path) -> None:
    """Garante permissão 600 em arquivos sensíveis (sessão, banco)."""
    for p in (path, *path.parent.glob(path.name + "-*")):
        if p.exists():
            with contextlib.suppress(OSError):
                p.chmod(0o600)
