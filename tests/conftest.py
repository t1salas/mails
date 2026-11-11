from __future__ import annotations

import os
from datetime import datetime, time, timezone
from pathlib import Path

import pytest

# Configuración de prueba antes de importar la app
os.environ.setdefault("ENV_FILE", "/nonexistent")
os.environ["WA_APP_SECRET"] = "secreto-de-prueba"
os.environ["WA_VERIFY_TOKEN"] = "token-verificacion"
os.environ["WA_ACCESS_TOKEN"] = ""
os.environ["SESSION_SECRET"] = "sesion-de-prueba"
os.environ["RUN_SCHEDULER"] = "0"

from app.db import init_db, session_scope  # noqa: E402
from app.models import Business, Customer, Service, Staff, WorkingHours  # noqa: E402
from app.whatsapp import client as wa_client  # noqa: E402

TZ = "America/Mexico_City"  # UTC-6 todo el año (sin horario de verano)


def utc(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def mx(y, m, d, hh, mm=0) -> datetime:
    """Hora local de Ciudad de México expresada en UTC (+6 h)."""
    return utc(y, m, d, hh + 6, mm) if hh + 6 < 24 else utc(y, m, d + 1, hh + 6 - 24, mm)


@pytest.fixture
def db(tmp_path: Path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    yield
    wa_client.set_sender(None)


@pytest.fixture
def sender(db):
    rec = wa_client.RecordingSender()
    wa_client.set_sender(rec)
    return rec


@pytest.fixture
def shop(db):
    """Barbería con 2 barberos.

    Juan: lun-sáb 10:00-14:00 y 15:00-20:00, hace todo.
    Pedro: lun-sáb 10:00-18:00, solo corte.
    """
    with session_scope() as s:
        b = Business(
            slug="test", name="Barbería Test", timezone=TZ, wa_phone_number_id="PNID",
            address="Calle 1", phone="555", slot_interval_min=15, min_notice_min=60,
            max_days_ahead=14, change_limit_min=120, reminder_hours=24,
            reminder_template="recordatorio_cita", template_lang="es_MX",
        )
        s.add(b)
        s.flush()
        corte = Service(business_id=b.id, name="Corte", duration_min=30, price_cents=15000, sort_order=0)
        barba = Service(business_id=b.id, name="Barba", duration_min=20, price_cents=10000, sort_order=1)
        s.add_all([corte, barba])
        s.flush()
        juan = Staff(business_id=b.id, name="Juan", sort_order=0, services=[corte, barba])
        pedro = Staff(business_id=b.id, name="Pedro", sort_order=1, services=[corte])
        for wd in range(6):
            juan.working_hours.append(WorkingHours(weekday=wd, start_time=time(10), end_time=time(14)))
            juan.working_hours.append(WorkingHours(weekday=wd, start_time=time(15), end_time=time(20)))
            pedro.working_hours.append(WorkingHours(weekday=wd, start_time=time(10), end_time=time(18)))
        s.add_all([juan, pedro])
        s.flush()
        ids = {"business": b.id, "corte": corte.id, "barba": barba.id, "juan": juan.id, "pedro": pedro.id}
    return ids


@pytest.fixture
def customer(shop):
    with session_scope() as s:
        c = Customer(business_id=shop["business"], phone="5215511111111", name="Ana López")
        s.add(c)
        s.flush()
        return c.id
