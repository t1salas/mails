from datetime import date, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.domain.availability import Interval, fits_in_ranges, free_slots

from .conftest import utc

MX = ZoneInfo("America/Mexico_City")


def test_slots_respect_ranges_duration_and_step():
    slots = free_slots(
        day=date(2026, 11, 16), tz=MX, ranges=[(time(10), time(11))],
        duration=timedelta(minutes=30), step=timedelta(minutes=15), busy=[],
        earliest=utc(2000, 1, 1, 0),
    )
    # 10:00, 10:15, 10:30 caben; 10:45 + 30 min se pasa de las 11:00
    assert [s.astimezone(MX).strftime("%H:%M") for s in slots] == ["10:00", "10:15", "10:30"]


def test_slots_skip_busy_and_lunch_break():
    busy = [Interval(utc(2026, 11, 16, 16, 30), utc(2026, 11, 16, 17, 0))]  # 10:30-11:00 local
    slots = free_slots(
        day=date(2026, 11, 16), tz=MX, ranges=[(time(10), time(11)), (time(12), time(12, 30))],
        duration=timedelta(minutes=30), step=timedelta(minutes=15), busy=busy,
        earliest=utc(2000, 1, 1, 0),
    )
    assert [s.astimezone(MX).strftime("%H:%M") for s in slots] == ["10:00", "12:00"]


def test_slots_respect_earliest():
    slots = free_slots(
        day=date(2026, 11, 16), tz=MX, ranges=[(time(10), time(12))],
        duration=timedelta(minutes=30), step=timedelta(minutes=30), busy=[],
        earliest=utc(2026, 11, 16, 17, 10),  # 11:10 local
    )
    assert [s.astimezone(MX).strftime("%H:%M") for s in slots] == ["11:30"]


def test_slots_handle_daylight_saving_change():
    ny = ZoneInfo("America/New_York")
    # 8 mar 2026: de 2:00 se salta a 3:00. El tramo 1:00-4:00 dura 2 horas reales.
    slots = free_slots(
        day=date(2026, 3, 8), tz=ny, ranges=[(time(1), time(4))],
        duration=timedelta(hours=1), step=timedelta(hours=1), busy=[],
        earliest=utc(2000, 1, 1, 0),
    )
    assert [s.astimezone(timezone.utc).hour for s in slots] == [6, 7]


def test_fits_in_ranges():
    ranges = {0: [(time(10), time(14))]}  # lunes
    assert fits_in_ranges(utc(2026, 11, 16, 19), utc(2026, 11, 16, 19, 30), MX, ranges)  # 13:00-13:30
    assert not fits_in_ranges(utc(2026, 11, 16, 19, 45), utc(2026, 11, 16, 20, 15), MX, ranges)
    assert not fits_in_ranges(utc(2026, 11, 17, 16), utc(2026, 11, 17, 16, 30), MX, ranges)  # martes
