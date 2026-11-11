import threading
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.db import session_scope
from app.domain import booking
from app.models import Appointment, AppointmentStatus, Business, Customer, Service, TimeOff

from .conftest import mx, utc

NOW = mx(2026, 11, 16, 8)  # lunes 16 nov 2026, 08:00 en CDMX


def _book(shop, customer, start, staff=("juan",), service="corte", now=NOW, **kw):
    with session_scope() as s:
        b = s.get(Business, shop["business"])
        appt = booking.book(
            s, business=b, customer=s.get(Customer, customer), service=s.get(Service, shop[service]),
            staff_candidates=[shop[x] for x in staff], starts_at=start, now=now, **kw,
        )
        return appt.id, appt.staff_id


def test_book_sets_end_and_reminder(shop, customer):
    appt_id, staff_id = _book(shop, customer, mx(2026, 11, 18, 10))
    with session_scope() as s:
        a = s.get(Appointment, appt_id)
        assert staff_id == shop["juan"]
        assert a.ends_at - a.starts_at == timedelta(minutes=30)
        assert a.reminder_at == mx(2026, 11, 17, 10)
        assert a.status == AppointmentStatus.CONFIRMED


def test_no_double_booking(shop, customer):
    _book(shop, customer, mx(2026, 11, 18, 10))
    with pytest.raises(booking.SlotUnavailable):
        _book(shop, customer, mx(2026, 11, 18, 10, 15))  # se cruza con 10:00-10:30


def test_back_to_back_is_allowed(shop, customer):
    _book(shop, customer, mx(2026, 11, 18, 10))
    _book(shop, customer, mx(2026, 11, 18, 10, 30))


def test_any_staff_falls_back_to_next_free(shop, customer):
    _book(shop, customer, mx(2026, 11, 18, 10))
    _, staff_id = _book(shop, customer, mx(2026, 11, 18, 10), staff=("juan", "pedro"))
    assert staff_id == shop["pedro"]


def test_rules_min_notice_hours_and_service(shop, customer):
    with pytest.raises(booking.SlotUnavailable):
        _book(shop, customer, mx(2026, 11, 16, 8, 30))  # menos de 1 h de anticipación
    with pytest.raises(booking.SlotUnavailable):
        _book(shop, customer, mx(2026, 11, 18, 14))  # hora de comida de Juan
    with pytest.raises(booking.SlotUnavailable):
        _book(shop, customer, mx(2026, 11, 18, 10), staff=("pedro",), service="barba")  # Pedro no hace barba
    with pytest.raises(booking.SlotUnavailable):
        _book(shop, customer, mx(2026, 12, 10, 10))  # fuera de los 14 días


def test_panel_can_book_outside_hours_but_not_on_top(shop, customer):
    _book(shop, customer, mx(2026, 11, 18, 14), enforce_rules=False, source="panel")
    with pytest.raises(booking.SlotUnavailable):
        _book(shop, customer, mx(2026, 11, 18, 14), enforce_rules=False, source="panel")


def test_time_off_blocks_slots(shop, customer):
    with session_scope() as s:
        s.add(TimeOff(staff_id=shop["juan"], starts_at=mx(2026, 11, 18, 0), ends_at=mx(2026, 11, 19, 0)))
    with pytest.raises(booking.SlotUnavailable):
        _book(shop, customer, mx(2026, 11, 18, 10))
    with session_scope() as s:
        b = s.get(Business, shop["business"])
        slots = booking.availability_for_day(
            s, b, s.get(Service, shop["corte"]), [shop["juan"]], date(2026, 11, 18), NOW
        )
        assert slots == {}


def test_availability_lists_free_staff_and_excludes_booked(shop, customer):
    _book(shop, customer, mx(2026, 11, 18, 10))
    with session_scope() as s:
        b = s.get(Business, shop["business"])
        slots = booking.availability_for_day(
            s, b, s.get(Service, shop["corte"]), [shop["juan"], shop["pedro"]], date(2026, 11, 18), NOW
        )
    assert slots[mx(2026, 11, 18, 10)] == [shop["pedro"]]
    # A las 10:30 ambos están libres; Pedro primero porque tiene menos citas ese día
    assert slots[mx(2026, 11, 18, 10, 30)] == [shop["pedro"], shop["juan"]]


def test_days_with_availability_skips_sunday_and_respects_window(shop):
    with session_scope() as s:
        b = s.get(Business, shop["business"])
        days = booking.days_with_availability(s, b, s.get(Service, shop["corte"]), [shop["juan"]], NOW)
    dates = [d for d, _ in days]
    assert dates[0] == date(2026, 11, 16)
    assert date(2026, 11, 22) not in dates  # domingo
    assert dates[-1] <= date(2026, 11, 30)


def test_reschedule_can_overlap_its_own_old_slot(shop, customer):
    appt_id, _ = _book(shop, customer, mx(2026, 11, 18, 10))
    with session_scope() as s:
        a = s.get(Appointment, appt_id)
        a.reminder_sent_at = NOW
        booking.reschedule(s, a, new_start=mx(2026, 11, 18, 10, 15), now=NOW, by="customer")
    with session_scope() as s:
        a = s.get(Appointment, appt_id)
        assert a.starts_at == mx(2026, 11, 18, 10, 15)
        assert a.reminder_sent_at is None
        assert a.reminder_at == mx(2026, 11, 17, 10, 15)


def test_customer_cannot_change_too_late_but_business_can(shop, customer):
    appt_id, _ = _book(shop, customer, mx(2026, 11, 16, 10))
    late = mx(2026, 11, 16, 9)  # falta 1 h; el límite es 2 h
    with session_scope() as s:
        a = s.get(Appointment, appt_id)
        with pytest.raises(booking.ChangeNotAllowed):
            booking.cancel(s, a, now=late, by="customer")
        booking.cancel(s, a, now=late, by="business")
        assert a.status == AppointmentStatus.CANCELLED
        assert a.cancelled_by == "business"


def test_cancelled_slot_becomes_free(shop, customer):
    appt_id, _ = _book(shop, customer, mx(2026, 11, 18, 10))
    with session_scope() as s:
        booking.cancel(s, s.get(Appointment, appt_id), now=NOW, by="customer")
    _book(shop, customer, mx(2026, 11, 18, 10))


def test_booking_inside_reminder_window_has_no_reminder(shop, customer):
    appt_id, _ = _book(shop, customer, mx(2026, 11, 16, 12))
    with session_scope() as s:
        assert s.get(Appointment, appt_id).reminder_at is None


def test_concurrent_bookings_only_one_wins(shop, customer):
    start = mx(2026, 11, 18, 11)
    results: list[str] = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        try:
            _book(shop, customer, start)
            results.append("ok")
        except booking.SlotUnavailable:
            results.append("ocupado")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count("ok") == 1
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(Appointment).where(Appointment.starts_at == start))
    assert n == 1


def test_utc_helper_sanity():
    assert mx(2026, 11, 16, 8) == utc(2026, 11, 16, 14)
