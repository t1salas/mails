"""Envío de mensajes a la WhatsApp Cloud API (Meta)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Protocol

import httpx

from app.config import get_settings
from app.db import session_scope
from app.models import MessageLog
from app.whatsapp.messages import Outbound

log = logging.getLogger(__name__)


class SendError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"WhatsApp API respondió {status}: {body[:500]}")
        self.status = status
        self.body = body


class Sender(Protocol):
    def send(self, phone_number_id: str, message: Outbound) -> str | None:
        """Envía y devuelve el id de mensaje de WhatsApp (o None si no aplica)."""
        ...


class CloudApiSender:
    def __init__(self, token: str, api_version: str, timeout: float = 15.0):
        self._token = token
        self._base = f"https://graph.facebook.com/{api_version}"
        self._client = httpx.Client(timeout=timeout)

    def send(self, phone_number_id: str, message: Outbound) -> str | None:
        resp = self._client.post(
            f"{self._base}/{phone_number_id}/messages",
            json=message.to_payload(),
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if resp.status_code >= 400:
            raise SendError(resp.status_code, resp.text)
        data = resp.json()
        messages = data.get("messages") or [{}]
        return messages[0].get("id")


class LogSender:
    """Modo sin credenciales: imprime en el log lo que se habría enviado."""

    def send(self, phone_number_id: str, message: Outbound) -> str | None:
        log.info("[dry-run] -> %s: %s", phone_number_id, json.dumps(message.to_payload(), ensure_ascii=False))
        return None


class RecordingSender:
    """Guarda los mensajes en memoria (tests y simulador)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Outbound]] = []

    def send(self, phone_number_id: str, message: Outbound) -> str | None:
        self.sent.append((phone_number_id, message))
        return f"wamid.fake.{uuid.uuid4().hex}"


_sender: Sender | None = None


def get_sender() -> Sender:
    global _sender
    if _sender is None:
        s = get_settings()
        _sender = LogSender() if s.dry_run else CloudApiSender(s.wa_access_token, s.wa_api_version)
    return _sender


def set_sender(sender: Sender | None) -> None:
    """Permite inyectar otro sender (tests, simulador)."""
    global _sender
    _sender = sender


def deliver(
    *,
    business_id: int,
    phone_number_id: str | None,
    customer_id: int | None,
    messages: list[Outbound],
    sender: Sender | None = None,
) -> list[bool]:
    """Envía los mensajes en orden y los registra en la bitácora.

    Se llama DESPUÉS de hacer commit del estado, para nunca avisar al cliente de
    algo que no quedó guardado. Devuelve, por mensaje, si se envió bien.
    """
    sender = sender or get_sender()
    results: list[bool] = []
    logs: list[MessageLog] = []
    for msg in messages:
        ok = True
        wa_id = None
        error = None
        try:
            wa_id = sender.send(phone_number_id or "sin-numero", msg)
        except Exception as exc:  # noqa: BLE001 - registrar y seguir con los demás
            ok = False
            error = str(exc)
            log.exception("Error enviando mensaje de WhatsApp")
        results.append(ok)
        body = {"payload": msg.to_payload()}
        if error:
            body["error"] = error
        logs.append(
            MessageLog(
                business_id=business_id,
                customer_id=customer_id,
                direction="out",
                wa_message_id=wa_id,
                kind=msg.kind,
                body=body,
            )
        )
    try:
        with session_scope() as s:
            s.add_all(logs)
    except Exception:  # noqa: BLE001 - la bitácora nunca debe tumbar el envío
        log.exception("No se pudo registrar la bitácora de mensajes salientes")
    return results
