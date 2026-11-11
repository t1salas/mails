"""Simulador del bot en la terminal: prueba conversaciones completas sin WhatsApp ni Meta.

Escribe texto como si fueras el cliente, o el número de una opción para "tocar" un
botón. Comandos: /recordatorio (simula el recordatorio de tu próxima cita), /salir.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

from sqlalchemy import select

from app.bot.engine import Bot
from app.db import session_scope
from app.domain import booking
from app.models import Business, Customer
from app.reminders import build_reminder
from app.whatsapp.messages import Buttons, Inbound, ListMessage, Outbound, Template, Text

_ids = itertools.count(1)


def render(msg: Outbound) -> list[tuple[str, str]]:
    """Imprime un mensaje y devuelve sus opciones [(id, título)]."""
    options: list[tuple[str, str]] = []
    print()
    if isinstance(msg, Text):
        print(f"🤖 {msg.body}")
    elif isinstance(msg, Buttons):
        print(f"🤖 {msg.body}")
        options = list(msg.buttons)
    elif isinstance(msg, ListMessage):
        print(f"🤖 {msg.body}\n   [{msg.button}]")
        options = [(r.id, f"{r.title}" + (f" — {r.description}" if r.description else "")) for r in msg.rows]
    elif isinstance(msg, Template):
        print(f"🤖 (plantilla '{msg.name}') {msg.body_params}")
        options = list(zip(msg.quick_reply_payloads, ["Confirmo", "Reprogramar", "Cancelar"], strict=False))
    for i, (_id, title) in enumerate(options, 1):
        print(f"   {i}) {title}")
    return options


def run(slug: str, phone: str, name: str) -> None:
    with session_scope() as s:
        business = s.scalars(select(Business).where(Business.slug == slug)).first()
        if business is None:
            raise SystemExit(f"No existe el negocio '{slug}'. Carga uno con: python -m app.cli cargar-config ...")
        business_id = business.id
        print(f"Simulando a {name} ({phone}) escribiendo a {business.name}. /salir para terminar.")

    options: list[tuple[str, str]] = []
    while True:
        try:
            raw = input("\n👤 ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue
        if raw == "/salir":
            return
        now = datetime.now(timezone.utc)
        with session_scope() as s:
            business = s.get(Business, business_id)
            if raw == "/recordatorio":
                customer = s.scalars(
                    select(Customer).where(Customer.business_id == business_id, Customer.phone == phone)
                ).first()
                upcoming = booking.upcoming_for_customer(s, customer, now) if customer else []
                if not upcoming:
                    print("(no tienes citas próximas)")
                    continue
                outs = [m for m in [build_reminder(business, upcoming[0], now)] if m]
            else:
                if raw.isdigit() and options and 1 <= int(raw) <= len(options):
                    cid, title = options[int(raw) - 1]
                    inbound = Inbound(wa_message_id=f"sim-{next(_ids)}", from_phone=phone, timestamp=now,
                                      kind="choice", choice_id=cid, choice_title=title, profile_name=name)
                else:
                    inbound = Inbound(wa_message_id=f"sim-{next(_ids)}", from_phone=phone, timestamp=now,
                                      kind="text", text=raw, profile_name=name)
                _customer, outs = Bot(s, business, now).handle(inbound)
        options = []
        for msg in outs:
            opts = render(msg)
            if opts:
                options = opts
