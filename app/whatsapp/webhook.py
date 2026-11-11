"""Webhook de la WhatsApp Cloud API.

- GET: verificación que hace Meta al configurar el webhook.
- POST: mensajes entrantes. Se valida la firma (X-Hub-Signature-256), se responde 200
  de inmediato y el procesamiento corre en segundo plano.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.bot.engine import Bot
from app.config import get_settings
from app.db import session_scope
from app.models import Business, MessageLog
from app.whatsapp.client import deliver
from app.whatsapp.messages import Inbound, Text

log = logging.getLogger(__name__)
router = APIRouter()


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict[str, Any]) -> list[tuple[str, Inbound]]:
    """Extrae (phone_number_id, mensaje) de un webhook de Meta. Ignora estados de entrega."""
    events: list[tuple[str, Inbound]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                for c in value.get("contacts", []) or []
            }
            for m in value.get("messages", []) or []:
                inbound = _parse_message(m, names)
                if inbound is not None:
                    events.append((phone_number_id, inbound))
    return events


def _parse_message(m: dict[str, Any], names: dict[str, str]) -> Inbound | None:
    msg_id = m.get("id")
    sender = m.get("from")
    if not msg_id or not sender:
        return None
    try:
        ts = datetime.fromtimestamp(int(m.get("timestamp", "0")), tz=timezone.utc)
    except (TypeError, ValueError):
        ts = datetime.now(timezone.utc)
    mtype = m.get("type", "")
    base = dict(
        wa_message_id=msg_id, from_phone=sender, timestamp=ts,
        profile_name=names.get(sender, ""), raw_type=mtype,
    )
    if mtype == "text":
        return Inbound(kind="text", text=(m.get("text") or {}).get("body", ""), **base)
    if mtype == "interactive":
        inter = m.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply")
        if reply:
            return Inbound(kind="choice", choice_id=reply.get("id", ""),
                           choice_title=reply.get("title", ""), **base)
    if mtype == "button":  # botón de respuesta rápida de una plantilla
        btn = m.get("button") or {}
        return Inbound(kind="choice", choice_id=btn.get("payload", ""),
                       choice_title=btn.get("text", ""), **base)
    return Inbound(kind="unsupported", **base)


def process_inbound(phone_number_id: str, inbound: Inbound) -> None:
    """Procesa un mensaje: estado y bitácora en una transacción; envío después del commit."""
    now = datetime.now(timezone.utc)
    business_ref: tuple[int, str | None] | None = None
    try:
        with session_scope() as s:
            business = s.scalars(
                select(Business).where(
                    Business.wa_phone_number_id == phone_number_id, Business.active.is_(True)
                )
            ).first()
            if business is None:
                log.warning("Mensaje para un número no registrado: %s", phone_number_id)
                return
            exists = s.scalars(
                select(MessageLog.id).where(MessageLog.wa_message_id == inbound.wa_message_id)
            ).first()
            if exists:
                log.info("Mensaje duplicado ignorado: %s", inbound.wa_message_id)
                return
            business_ref = (business.id, business.wa_phone_number_id)
            customer, outbound = Bot(s, business, now).handle(inbound)
            s.add(
                MessageLog(
                    business_id=business.id, customer_id=customer.id, direction="in",
                    wa_message_id=inbound.wa_message_id, kind=inbound.kind,
                    body={"text": inbound.text, "choice_id": inbound.choice_id,
                          "choice_title": inbound.choice_title, "type": inbound.raw_type},
                    created_at=now,
                )
            )
            business_id, pnid, customer_id = business.id, business.wa_phone_number_id, customer.id
    except IntegrityError:
        log.info("Mensaje duplicado (carrera) ignorado: %s", inbound.wa_message_id)
        return
    except Exception:
        # Un error nuestro no debe dejar al cliente sin respuesta.
        log.exception("Error procesando el mensaje %s", inbound.wa_message_id)
        if business_ref is not None:
            deliver(
                business_id=business_ref[0], phone_number_id=business_ref[1], customer_id=None,
                messages=[Text(inbound.from_phone,
                               "Uy, tuve un problema procesando tu mensaje 😓. "
                               "Intenta de nuevo escribiendo *menú*.")],
            )
        return
    deliver(business_id=business_id, phone_number_id=pnid, customer_id=customer_id, messages=outbound)


@router.get("/webhook/whatsapp")
def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    settings = get_settings()
    if mode == "subscribe" and settings.wa_verify_token and hmac.compare_digest(token, settings.wa_verify_token):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Token de verificación inválido")


@router.post("/webhook/whatsapp")
async def receive_webhook(request: Request, background: BackgroundTasks):
    settings = get_settings()
    body = await request.body()
    if settings.wa_app_secret:
        if not verify_signature(settings.wa_app_secret, body, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(status_code=401, detail="Firma inválida")
    elif not settings.allow_unsigned_webhooks:
        log.error("WA_APP_SECRET no configurado: se rechazan webhooks sin firma")
        raise HTTPException(status_code=503, detail="Webhook no configurado")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON inválido") from None
    for phone_number_id, inbound in parse_webhook(payload):
        background.add_task(process_inbound, phone_number_id, inbound)
    return {"ok": True}
