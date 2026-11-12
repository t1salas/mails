"""Avisos por Telegram (opcional). Sin TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no se envía nada."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)
LIMIT = 3900


def send_telegram(token: str, chat_id: str, text: str, http: httpx.Client | None = None) -> bool:
    if not token or not chat_id:
        return False
    client = http or httpx.Client(timeout=15.0)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i : i + LIMIT] for i in range(0, len(text), LIMIT)] or [""]
    try:
        for chunk in chunks:
            response = client.post(url, json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True})
            response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        # No registrar la URL: contiene el token.
        log.warning("No se pudo enviar el aviso por Telegram: %s", type(exc).__name__)
        return False
