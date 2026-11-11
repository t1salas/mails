from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session_scope
from app.domain import booking
from app.main import create_app
from app.models import Appointment, AppointmentStatus, Business, Customer, Service, TimeOff, User
from app.panel.auth import hash_password, verify_password


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def users(shop):
    with session_scope() as s:
        s.add(User(business_id=shop["business"], username="dueno", password_hash=hash_password("clave-segura"),
                   role="owner"))
        s.add(User(business_id=shop["business"], username="pedro", password_hash=hash_password("clave-pedro"),
                   role="staff", staff_id=shop["pedro"]))


@pytest.fixture
def client(users, sender):
    with TestClient(create_app(init_database=False, run_scheduler=False)) as c:
        yield c


def _login(client, username="dueno", password="clave-segura"):
    token = _csrf(client.get("/login").text)
    return client.post("/login", data={"username": username, "password": password, "csrf": token})


def _future_appt(shop, customer_id, days=1, hour=11, staff="juan"):
    with session_scope() as s:
        b = s.get(Business, shop["business"])
        tz = booking.business_tz(b)
        now = datetime.now(timezone.utc)
        day = (now.astimezone(tz) + timedelta(days=days)).date()
        start = datetime(day.year, day.month, day.day, hour, tzinfo=tz).astimezone(timezone.utc)
        appt = booking.book(s, business=b, customer=s.get(Customer, customer_id),
                            service=s.get(Service, shop["corte"]), staff_candidates=[shop[staff]],
                            starts_at=start, now=now, enforce_rules=False)
        return appt.id, day


def test_password_hashing():
    h = hash_password("hola-mundo")
    assert verify_password("hola-mundo", h)
    assert not verify_password("otra", h)


def test_requires_login(client):
    r = client.get("/agenda", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_wrong_password(client):
    r = _login(client, password="mala")
    assert "Usuario o contraseña incorrectos" in r.text


def test_login_without_csrf_fails(client):
    r = client.post("/login", data={"username": "dueno", "password": "clave-segura"})
    assert "La sesión expiró" in r.text


def test_agenda_shows_appointments(client, shop, customer):
    _, day = _future_appt(shop, customer)
    assert "Barbería Test" in _login(client).text
    html = client.get(f"/agenda?d={day.isoformat()}").text
    assert "Ana López" in html and "11:00–11:30" in html
    assert "Juan" in html and "Pedro" in html
    assert "https://wa.me/5215511111111" in html


def test_staff_user_sees_only_own_column(client, shop, customer):
    _, day = _future_appt(shop, customer)
    _login(client, "pedro", "clave-pedro")
    html = client.get(f"/agenda?d={day.isoformat()}").text
    assert "<h2>Pedro</h2>" in html and "<h2>Juan</h2>" not in html
    assert "Ana López" not in html


def test_create_appointment_from_panel(client, shop):
    _login(client)
    page = client.get("/citas/nueva")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    r = client.post("/citas/nueva", data={
        "csrf": _csrf(page.text), "fecha": tomorrow, "hora": "12:00", "staff_id": shop["pedro"],
        "service_id": shop["corte"], "telefono": "55 1234 5678", "nombre": "Carlos", "notas": "Llamó",
    })
    assert "Cita agendada" in r.text
    with session_scope() as s:
        appt = s.scalars(select(Appointment)).one()
        assert appt.source == "panel" and appt.notes == "Llamó"
        assert appt.customer.phone == "525512345678"
    # Encima de la misma cita no se puede
    r = client.post("/citas/nueva", data={
        "csrf": _csrf(page.text), "fecha": tomorrow, "hora": "12:15", "staff_id": shop["pedro"],
        "service_id": shop["corte"], "telefono": "5598765432", "nombre": "Otro",
    })
    assert "No se pudo agendar" in r.text


def test_cancel_from_panel_offers_whatsapp_link(client, shop, customer):
    appt_id, day = _future_appt(shop, customer)
    html = _login(client).text
    r = client.post(f"/citas/{appt_id}/cancelar", data={"csrf": _csrf(html)})
    assert "Cita cancelada" in r.text
    assert "https://wa.me/5215511111111?text=" in r.text  # cliente fuera de la ventana de 24 h
    with session_scope() as s:
        a = s.get(Appointment, appt_id)
        assert a.status == AppointmentStatus.CANCELLED and a.cancelled_by == "business"


def test_cancel_from_panel_messages_customer_inside_window(client, shop, customer, sender):
    with session_scope() as s:
        s.get(Customer, customer).last_inbound_at = datetime.now(timezone.utc)
    appt_id, _ = _future_appt(shop, customer)
    html = _login(client).text
    r = client.post(f"/citas/{appt_id}/cancelar", data={"csrf": _csrf(html)})
    assert "Le avisamos al cliente por WhatsApp" in r.text
    assert len(sender.sent) == 1 and "fue cancelada" in sender.sent[0][1].body


def test_mark_no_show_and_undo(client, shop, customer):
    appt_id, _ = _future_appt(shop, customer)
    html = _login(client).text
    client.post(f"/citas/{appt_id}/estado", data={"csrf": _csrf(html), "status": "no_show"})
    with session_scope() as s:
        assert s.get(Appointment, appt_id).status == AppointmentStatus.NO_SHOW
    client.post(f"/citas/{appt_id}/estado", data={"csrf": _csrf(html), "status": "confirmed"})
    with session_scope() as s:
        assert s.get(Appointment, appt_id).status == AppointmentStatus.CONFIRMED


def test_staff_cannot_touch_other_staff_appointments(client, shop, customer):
    appt_id, _ = _future_appt(shop, customer, staff="juan")
    html = _login(client, "pedro", "clave-pedro").text
    r = client.post(f"/citas/{appt_id}/cancelar", data={"csrf": _csrf(html)})
    assert "No se pudo cancelar" in r.text
    with session_scope() as s:
        assert s.get(Appointment, appt_id).status == AppointmentStatus.CONFIRMED


def test_block_time_warns_about_existing_appointments(client, shop, customer):
    _, day = _future_appt(shop, customer)
    html = _login(client).text
    r = client.post("/bloqueos", data={
        "csrf": _csrf(html), "staff_id": shop["juan"], "fecha": day.isoformat(), "todo_el_dia": "1",
        "motivo": "Vacaciones",
    })
    assert "Horario bloqueado para Juan" in r.text
    assert "ya hay 1 cita(s)" in r.text
    assert "Vacaciones" in r.text
    with session_scope() as s:
        blk = s.scalars(select(TimeOff)).one()
    r = client.post(f"/bloqueos/{blk.id}/borrar", data={"csrf": _csrf(r.text)})
    assert "Bloqueo eliminado" in r.text


def test_week_view(client, shop, customer):
    _, day = _future_appt(shop, customer)
    _login(client)
    html = client.get(f"/semana?d={day.isoformat()}").text
    assert "Ana López" in html and "Corte · Juan" in html
