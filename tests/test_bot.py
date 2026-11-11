from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.bot.engine import Bot
from app.db import session_scope
from app.domain import booking
from app.models import Appointment, AppointmentStatus, Business, Customer, Service
from app.whatsapp.messages import Buttons, Inbound, ListMessage, Text

from .conftest import mx

NOW = mx(2026, 11, 16, 8)  # lunes 08:00 CDMX
_ids = itertools.count()


class Chat:
    def __init__(self, business_id: int, phone="5215522222222", name="Luis Pérez", now: datetime = NOW):
        self.business_id = business_id
        self.phone = phone
        self.name = name
        self.now = now
        self.last: list = []

    def _send(self, **kw):
        inbound = Inbound(wa_message_id=f"m{next(_ids)}", from_phone=self.phone, timestamp=self.now,
                          profile_name=self.name, **kw)
        with session_scope() as s:
            _, out = Bot(s, s.get(Business, self.business_id), self.now).handle(inbound)
        self.last = out
        return out

    def text(self, body: str):
        return self._send(kind="text", text=body)

    def tap(self, choice_id: str):
        return self._send(kind="choice", choice_id=choice_id)

    def options(self) -> list[tuple[str, str]]:
        msg = self.last[-1]
        if isinstance(msg, Buttons):
            return msg.buttons
        if isinstance(msg, ListMessage):
            return [(r.id, r.title) for r in msg.rows]
        return []

    def tap_title(self, startswith: str):
        """Toca la opción cuyo título empieza así; si no está, sigue con "Ver más…"."""
        for _ in range(10):
            options = self.options()
            for cid, title in options:
                if title.startswith(startswith):
                    return self.tap(cid)
            more = [cid for cid, title in options if title == "Ver más…"]
            if not more:
                break
            self.tap(more[0])
        raise AssertionError(f"No hay opción '{startswith}' en {self.options()}")

    def body(self) -> str:
        return "\n".join(m.body for m in self.last)


def _appointments(customer_phone: str) -> list[Appointment]:
    with session_scope() as s:
        c = s.scalars(select(Customer).where(Customer.phone == customer_phone)).one()
        return list(s.scalars(select(Appointment).where(Appointment.customer_id == c.id)))


def test_greeting_shows_menu(shop):
    chat = Chat(shop["business"])
    chat.text("hola")
    assert "¡Hola, Luis!" in chat.body()
    assert [t for _, t in chat.options()] == ["Agendar cita", "Mis citas", "Ubicación y precios"]


def test_full_booking_with_specific_barber(shop):
    chat = Chat(shop["business"])
    chat.text("hola")
    chat.tap("menu:book")
    assert "¿Qué servicio" in chat.body()
    chat.tap_title("Corte")
    assert "¿Con qué barbero" in chat.body()
    chat.tap_title("Pedro")
    chat.tap("day:2026-11-18")
    # Pedro tiene muchos horarios ese día: primero se pregunta la franja
    assert [t for _, t in chat.options()] == ["En la mañana", "En la tarde", "En la noche"]
    chat.tap("per:t")
    chat.tap_title("15:30")
    assert "Revisa tu cita" in chat.body() and "Pedro" in chat.body()
    chat.tap("ok")
    assert "Tu cita quedó agendada" in chat.body()
    [appt] = _appointments(chat.phone)
    assert appt.starts_at == mx(2026, 11, 18, 15, 30)
    assert appt.staff_id == shop["pedro"]
    assert appt.source == "whatsapp"


def test_single_barber_service_skips_barber_question(shop):
    chat = Chat(shop["business"])
    chat.tap("menu:book")
    chat.tap_title("Barba")  # solo Juan hace barba
    assert "¿Qué día" in chat.body()


def test_any_barber_assigns_someone(shop):
    chat = Chat(shop["business"])
    chat.tap("menu:book")
    chat.tap_title("Corte")
    chat.tap("stf:any")
    chat.tap("day:2026-11-18")
    chat.tap("per:m")
    chat.tap_title("10:00")
    chat.tap("ok")
    [appt] = _appointments(chat.phone)
    assert appt.staff_id in (shop["juan"], shop["pedro"])


def test_slot_taken_before_confirming(shop, customer):
    chat = Chat(shop["business"])
    chat.tap("menu:book")
    chat.tap_title("Corte")
    chat.tap_title("Juan")
    chat.tap("day:2026-11-18")
    chat.tap("per:m")
    chat.tap_title("10:00")
    # Otro cliente toma ese horario mientras tanto
    with session_scope() as s:
        b = s.get(Business, shop["business"])
        booking.book(s, business=b, customer=s.get(Customer, customer), service=s.get(Service, shop["corte"]),
                     staff_candidates=[shop["juan"]], starts_at=mx(2026, 11, 18, 10), now=NOW)
    chat.tap("ok")
    assert "alguien acaba de tomar ese horario" in chat.body()
    assert "10:00" not in [t for _, t in chat.options()]
    assert _appointments(chat.phone) == []


def test_stale_button_is_rejected(shop):
    chat = Chat(shop["business"])
    chat.tap("slot:2026-11-18T10:00")
    assert "ya no está disponible" in chat.body()


def test_free_text_mid_flow_repeats_step(shop):
    chat = Chat(shop["business"])
    chat.tap("menu:book")
    chat.text("el de siempre")
    assert "Elige una opción de la lista" in chat.body()
    assert "¿Qué servicio" in chat.body()


def _book_via_chat(chat: Chat, day="2026-11-18", hhmm="10:00"):
    chat.tap("menu:book")
    chat.tap_title("Corte")
    chat.tap_title("Juan")
    chat.tap(f"day:{day}")
    if chat.options() and chat.options()[0][0].startswith("per:"):
        chat.tap("per:m" if hhmm < "12:00" else "per:t")
    chat.tap_title(hhmm)
    chat.tap("ok")


def test_cancel_from_my_appointments(shop):
    chat = Chat(shop["business"])
    _book_via_chat(chat)
    chat.text("quiero cancelar mi cita")
    assert [t for _, t in chat.options()] == ["Reprogramar", "Cancelar cita", "Volver"]
    chat.tap_title("Cancelar cita")
    assert "¿Seguro que quieres cancelar" in chat.body()
    chat.tap_title("Sí, cancelar")
    assert "tu cita fue cancelada" in chat.body()
    [appt] = _appointments(chat.phone)
    assert appt.status == AppointmentStatus.CANCELLED
    assert appt.cancelled_by == "customer"


def test_reschedule(shop):
    chat = Chat(shop["business"])
    _book_via_chat(chat)
    chat.tap("menu:mine")
    chat.tap_title("Reprogramar")
    assert "¿Para qué día movemos tu cita?" in chat.body()
    chat.tap("day:2026-11-19")
    chat.tap("per:t")
    more = chat.options()[-1]
    assert more == ("slotp:1", "Ver más…")
    assert chat.last[-1].rows[-1].description == "De 15:30 a 16:45"
    chat.tap_title("16:00")
    assert "Vamos a mover tu cita" in chat.body()
    chat.tap("ok")
    assert "reprogramada" in chat.body()
    [appt] = _appointments(chat.phone)
    assert appt.starts_at == mx(2026, 11, 19, 16)
    assert appt.status == AppointmentStatus.CONFIRMED


def test_multiple_appointments_are_listed(shop):
    chat = Chat(shop["business"])
    _book_via_chat(chat, hhmm="10:00")
    _book_via_chat(chat, day="2026-11-19", hhmm="11:00")
    chat.tap("menu:mine")
    assert isinstance(chat.last[-1], ListMessage)
    assert len(chat.options()) == 2


def test_too_late_to_change(shop):
    chat = Chat(shop["business"])
    _book_via_chat(chat, day="2026-11-16", hhmm="10:00")
    late = Chat(shop["business"], phone=chat.phone, now=mx(2026, 11, 16, 9))
    late.tap("menu:mine")
    assert "no puedo cambiarla por aquí" in late.body()
    assert "555" in late.body()


def test_reminder_buttons(shop):
    chat = Chat(shop["business"])
    _book_via_chat(chat)
    [appt] = _appointments(chat.phone)
    chat.tap(f"rem:ok:{appt.id}")
    assert "Gracias por confirmar" in chat.body()
    [appt] = _appointments(chat.phone)
    assert appt.customer_confirmed_at is not None
    chat.tap(f"rem:cx:{appt.id}")
    assert "¿Seguro que quieres cancelar" in chat.body()


def test_cannot_touch_someone_elses_appointment(shop):
    owner = Chat(shop["business"])
    _book_via_chat(owner)
    [appt] = _appointments(owner.phone)
    intruder = Chat(shop["business"], phone="5215533333333", name="Otro")
    intruder.tap(f"cxy:{appt.id}")
    assert "ya no está disponible" in intruder.body()
    [appt] = _appointments(owner.phone)
    assert appt.status == AppointmentStatus.CONFIRMED


def test_audio_is_politely_rejected(shop):
    chat = Chat(shop["business"])
    chat._send(kind="unsupported", raw_type="audio")
    assert "no puedo escuchar audios" in chat.body()


def test_info_lists_services_and_prices(shop):
    chat = Chat(shop["business"])
    chat.text("¿cuánto cuesta el corte?")
    assert "Corte (30 min) — $150" in chat.body()


def test_flow_expires_after_two_hours(shop):
    chat = Chat(shop["business"])
    chat.tap("menu:book")
    later = Chat(shop["business"], phone=chat.phone, now=NOW + timedelta(hours=3))
    later.tap(f"svc:{shop['corte']}")
    assert "ya no está disponible" in later.body()


@pytest.mark.parametrize("text", ["Hola", "menú", "buenas tardes"])
def test_greetings_reset_flow(shop, text):
    chat = Chat(shop["business"])
    chat.tap("menu:book")
    chat.text(text)
    assert [t for _, t in chat.options()] == ["Agendar cita", "Mis citas", "Ubicación y precios"]


def test_all_outbound_messages_are_valid_payloads(shop):
    chat = Chat(shop["business"])
    outs = []
    for step in [lambda: chat.text("hola"), lambda: chat.tap("menu:book"), lambda: chat.tap_title("Corte"),
                 lambda: chat.tap("stf:any"), lambda: chat.tap("dayp:1")]:
        outs += step()
    for msg in outs:
        payload = msg.to_payload()
        assert payload["to"] == chat.phone
        if isinstance(msg, Text):
            assert payload["text"]["body"]
