"""Aplicación web: webhook de WhatsApp + panel + tarea de recordatorios, en un solo proceso.

Correr con UN solo worker (uvicorn app.main:app), porque la tarea de recordatorios
vive dentro del proceso.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import init_db
from app.panel.routes import router as panel_router
from app.reminders import run_reminders
from app.whatsapp.webhook import router as webhook_router

log = logging.getLogger("app")


async def _reminder_loop(interval: int) -> None:
    while True:
        try:
            sent = await asyncio.to_thread(run_reminders)
            if sent:
                log.info("Recordatorios enviados: %s", sent)
        except Exception:  # noqa: BLE001 - el ciclo nunca debe morir
            log.exception("Error en la tarea de recordatorios")
        await asyncio.sleep(interval)


def create_app(init_database: bool = True, run_scheduler: bool | None = None) -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings.session_secret == "dev-inseguro-cambiame":
        log.warning("SESSION_SECRET no configurado: usa uno propio en producción")
    scheduler_on = settings.run_scheduler if run_scheduler is None else run_scheduler

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if init_database:
            init_db(settings.database_url)
        task = None
        if scheduler_on:
            task = asyncio.create_task(_reminder_loop(settings.scheduler_interval_seconds))
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Citas por WhatsApp", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="agenda",
        max_age=60 * 60 * 24 * 30,
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "panel" / "static")), name="static")
    app.include_router(webhook_router)
    app.include_router(panel_router)

    @app.get("/salud")
    def salud():
        return {"ok": True}

    return app


app = create_app()
