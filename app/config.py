"""Configuración leída de variables de entorno (y de un archivo .env si existe)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Carga KEY=VALUE de un .env sin pisar variables ya definidas."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    # WhatsApp Cloud API (Meta)
    wa_access_token: str
    wa_app_secret: str
    wa_verify_token: str
    wa_api_version: str
    # Si es True y no hay app secret, se aceptan webhooks sin firma (solo desarrollo).
    allow_unsigned_webhooks: bool
    # Panel
    session_secret: str
    secure_cookies: bool
    # Recordatorios
    run_scheduler: bool
    scheduler_interval_seconds: int

    @property
    def dry_run(self) -> bool:
        """Sin token de Meta no se envía nada: los mensajes salientes solo se registran en el log."""
        return not self.wa_access_token


@lru_cache
def get_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("ENV_FILE", ".env")))
    env = os.environ
    return Settings(
        database_url=env.get("DATABASE_URL", "sqlite:///citas.db"),
        wa_access_token=env.get("WA_ACCESS_TOKEN", ""),
        wa_app_secret=env.get("WA_APP_SECRET", ""),
        wa_verify_token=env.get("WA_VERIFY_TOKEN", ""),
        wa_api_version=env.get("WA_API_VERSION", "v24.0"),
        allow_unsigned_webhooks=_bool(env.get("ALLOW_UNSIGNED_WEBHOOKS"), False),
        session_secret=env.get("SESSION_SECRET", "dev-inseguro-cambiame"),
        secure_cookies=_bool(env.get("SECURE_COOKIES"), False),
        run_scheduler=_bool(env.get("RUN_SCHEDULER"), True),
        scheduler_interval_seconds=int(env.get("SCHEDULER_INTERVAL_SECONDS", "60")),
    )
