"""Cálculo de huecos libres. Funciones puras (sin base de datos) para poder probarlas a fondo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end


def local_range_to_utc(day: date, start: time, end: time, tz: ZoneInfo) -> tuple[datetime, datetime]:
    s = datetime.combine(day, start, tzinfo=tz).astimezone(timezone.utc)
    e = datetime.combine(day, end, tzinfo=tz).astimezone(timezone.utc)
    return s, e


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Inicio y fin (UTC) del día local `day`."""
    start = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz).astimezone(timezone.utc)
    return start, end


def free_slots(
    *,
    day: date,
    tz: ZoneInfo,
    ranges: list[tuple[time, time]],
    duration: timedelta,
    step: timedelta,
    busy: list[Interval],
    earliest: datetime,
) -> list[datetime]:
    """Horas de inicio (UTC) en las que cabe un servicio de `duration`.

    - `ranges`: tramos de trabajo de ese día en hora local (puede haber varios).
    - Los candidatos salen cada `step` desde el inicio de cada tramo.
    - Se descartan los que chocan con `busy` (citas y bloqueos) o empiezan antes de `earliest`.
    - La aritmética se hace en UTC para no fallar en los cambios de horario de verano.
    """
    out: set[datetime] = set()
    for r_start, r_end in ranges:
        start, end = local_range_to_utc(day, r_start, r_end, tz)
        t = start
        while t + duration <= end:
            if t >= earliest and not any(b.overlaps(t, t + duration) for b in busy):
                out.add(t)
            t += step
    return sorted(out)


def fits_in_ranges(
    start: datetime, end: datetime, tz: ZoneInfo, ranges_by_weekday: dict[int, list[tuple[time, time]]]
) -> bool:
    """¿El intervalo [start, end) cae completo dentro de un tramo de trabajo?"""
    local_day = start.astimezone(tz).date()
    for r_start, r_end in ranges_by_weekday.get(local_day.weekday(), []):
        s, e = local_range_to_utc(local_day, r_start, r_end, tz)
        if s <= start and end <= e:
            return True
    return False
