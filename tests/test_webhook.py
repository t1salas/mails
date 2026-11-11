from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.main import create_app
from app.models import MessageLog
from app.whatsapp.messages import Buttons
from app.whatsapp.webhook import parse_webhook

SECRET = b"secreto-de-prueba"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def _payload(message: dict, phone_number_id: str = "PNID") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "5215500000000", "phone_number_id": phone_number_id},
                    "contacts": [{"profile": {"name": "María"}, "wa_id": "5215544444444"}],
                    "messages": [message],
                },
            }],
        }],
    }


def _text(msg_id: str, body: str) -> dict:
    return {"from": "5215544444444", "id": msg_id, "timestamp": "1763300000", "type": "text",
            "text": {"body": body}}


@pytest.fixture
def client(shop, sender):
    app = create_app(init_database=False, run_scheduler=False)
    with TestClient(app) as c:
        yield c


def _post(client, payload: dict, sign: bool = True):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(body)
    return client.post("/webhook/whatsapp", content=body, headers=headers)


def test_verification_handshake(client):
    ok = client.get("/webhook/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "token-verificacion", "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhook/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "otro", "hub.challenge": "12345"})
    assert bad.status_code == 403


def test_rejects_unsigned_or_badly_signed(client, sender):
    assert _post(client, _payload(_text("wamid.1", "hola")), sign=False).status_code == 401
    body = json.dumps(_payload(_text("wamid.1", "hola"))).encode()
    r = client.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 401
    assert sender.sent == []


def test_message_gets_menu_reply_and_is_logged(client, sender):
    r = _post(client, _payload(_text("wamid.1", "hola")))
    assert r.status_code == 200
    [(pnid, msg)] = sender.sent
    assert pnid == "PNID"
    assert isinstance(msg, Buttons) and "María" in msg.body
    with session_scope() as s:
        logs = s.scalars(select(MessageLog).order_by(MessageLog.id)).all()
        assert [(lg.direction, lg.kind) for lg in logs] == [("in", "text"), ("out", "buttons")]


def test_duplicate_delivery_is_processed_once(client, sender):
    _post(client, _payload(_text("wamid.dup", "hola")))
    _post(client, _payload(_text("wamid.dup", "hola")))
    assert len(sender.sent) == 1


def test_unknown_number_is_ignored(client, sender):
    assert _post(client, _payload(_text("wamid.2", "hola"), phone_number_id="OTRO")).status_code == 200
    assert sender.sent == []


def test_status_updates_are_ignored(client, sender):
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PNID"},
        "statuses": [{"id": "wamid.x", "status": "delivered"}]}}]}]}
    assert _post(client, payload).status_code == 200
    assert sender.sent == []


def test_parse_interactive_and_template_buttons():
    list_reply = {"from": "52155", "id": "a", "timestamp": "1", "type": "interactive",
                  "interactive": {"type": "list_reply", "list_reply": {"id": "svc:1", "title": "Corte"}}}
    button_reply = {"from": "52155", "id": "b", "timestamp": "1", "type": "interactive",
                    "interactive": {"type": "button_reply", "button_reply": {"id": "menu:book", "title": "Agendar"}}}
    template_button = {"from": "52155", "id": "c", "timestamp": "1", "type": "button",
                       "button": {"payload": "rem:ok:7", "text": "Confirmo"}}
    audio = {"from": "52155", "id": "d", "timestamp": "1", "type": "audio", "audio": {"id": "x"}}
    events = []
    for m in (list_reply, button_reply, template_button, audio):
        events += parse_webhook(_payload(m))
    kinds = [(e.kind, e.choice_id, e.raw_type) for _, e in events]
    assert kinds == [
        ("choice", "svc:1", "interactive"),
        ("choice", "menu:book", "interactive"),
        ("choice", "rem:ok:7", "button"),
        ("unsupported", "", "audio"),
    ]


def test_health(client):
    assert client.get("/salud").json() == {"ok": True}
