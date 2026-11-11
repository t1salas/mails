from __future__ import annotations

from sqlalchemy import select

from app.db import session_scope
from app.domain import booking
from app.models import Appointment, Business, Customer, MessageLog, Service
from app.reminders import MAX_ATTEMPTS, run_reminders
from app.whatsapp.client import RecordingSender
from app.whatsapp.messages import Buttons, Template

from .conftest import mx

BOOKED_AT = mx(2026, 11, 16, 8)
START = mx(2026, 11, 19, 10)  # jueves 10:00
DUE = mx(2026, 11, 18, 10, 1)  # 24 h antes (+1 min)


def _book(shop, customer_id, last_inbound=None):
    with session_scope() as s:
        b = s.get(Business, shop["business"])
        c = s.get(Customer, customer_id)
        c.last_inbound_at = last_inbound
        appt = booking.book(s, business=b, customer=c, service=s.get(Service, shop["corte"]),
                            staff_candidates=[shop["juan"]], starts_at=START, now=BOOKED_AT)
        return appt.id


def test_template_when_outside_free_window(shop, customer):
    appt_id = _book(shop, customer, last_inbound=BOOKED_AT)
    rec = RecordingSender()
    assert run_reminders(mx(2026, 11, 18, 9), sender=rec) == 0  # todavía no toca
    assert run_reminders(DUE, sender=rec) == 1
    [(pnid, msg)] = rec.sent
    assert pnid == "PNID"
    assert isinstance(msg, Template)
    assert msg.name == "recordatorio_cita"
    assert msg.body_params == ["Ana", "Barbería Test", "Corte", "Juan", "jueves 19 de noviembre a las 10:00"]
    assert msg.quick_reply_payloads == [f"rem:ok:{appt_id}", f"rem:rs:{appt_id}", f"rem:cx:{appt_id}"]
    payload = msg.to_payload()
    assert payload["template"]["components"][1]["parameters"][0]["payload"] == f"rem:ok:{appt_id}"
    # No se repite
    assert run_reminders(mx(2026, 11, 18, 11), sender=rec) == 0
    with session_scope() as s:
        assert s.get(Appointment, appt_id).reminder_sent_at == DUE
        logs = s.scalars(select(MessageLog).where(MessageLog.direction == "out")).all()
        assert len(logs) == 1 and logs[0].kind == "template"


def test_free_buttons_when_customer_wrote_recently(shop, customer):
    _book(shop, customer, last_inbound=mx(2026, 11, 18, 9))
    rec = RecordingSender()
    assert run_reminders(DUE, sender=rec) == 1
    [(_, msg)] = rec.sent
    assert isinstance(msg, Buttons)
    assert [title for _, title in msg.buttons] == ["Confirmo", "Reprogramar", "Cancelar"]


def test_without_template_and_outside_window_nothing_is_sent(shop, customer):
    with session_scope() as s:
        s.get(Business, shop["business"]).reminder_template = None
    appt_id = _book(shop, customer, last_inbound=BOOKED_AT)
    rec = RecordingSender()
    assert run_reminders(DUE, sender=rec) == 0
    assert rec.sent == []
    with session_scope() as s:
        assert s.get(Appointment, appt_id).reminder_attempts == MAX_ATTEMPTS


class FailingSender:
    def send(self, phone_number_id, message):
        raise RuntimeError("Meta caído")


def test_failed_send_is_retried_then_given_up(shop, customer):
    appt_id = _book(shop, customer)
    for _ in range(MAX_ATTEMPTS):
        assert run_reminders(DUE, sender=FailingSender()) == 0
    with session_scope() as s:
        a = s.get(Appointment, appt_id)
        assert a.reminder_sent_at is None
        assert a.reminder_attempts == MAX_ATTEMPTS
    rec = RecordingSender()
    assert run_reminders(DUE, sender=rec) == 0  # ya se rindió


def test_cancelled_appointments_get_no_reminder(shop, customer):
    appt_id = _book(shop, customer)
    with session_scope() as s:
        booking.cancel(s, s.get(Appointment, appt_id), now=BOOKED_AT, by="customer")
    rec = RecordingSender()
    assert run_reminders(DUE, sender=rec) == 0
