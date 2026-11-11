"""Reglas de agenda: disponibilidad, reservar, reprogramar y cancelar.

Es la única puerta para modificar citas: el bot de WhatsApp y el panel llaman a estas
mismas funciones, así que las reglas viven en un solo lugar.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.availability import (
    Interval,
    fits_in_ranges,
    free_slots,
    local_day_bounds,
)
from app.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Customer,
    Service,
    Staff,
    TimeOff,
    WorkingHours,
    staff_services,
)


class BookingError(Exception):
    """Error de negocio al reservar o modificar una cita."""


class SlotUnavailable(BookingError):
    """El horario ya no está libre (o no cumple las reglas)."""


class ChangeNotAllowed(BookingError):
    """La cita ya no se puede cambiar (pasó el límite, ya fue cancelada, etc.)."""


def business_tz(business: Business) -> ZoneInfo:
    return ZoneInfo(business.timezone)


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------


def active_services(session: Session, business: Business) -> list[Service]:
    return list(
        session.scalars(
            select(Service)
            .where(Service.business_id == business.id, Service.active.is_(True))
            .order_by(Service.sort_order, Service.id)
        )
    )


def staff_for_service(session: Session, business: Business, service: Service) -> list[Staff]:
    return list(
        session.scalars(
            select(Staff)
            .join(staff_services, staff_services.c.staff_id == Staff.id)
            .where(
                Staff.business_id == business.id,
                Staff.active.is_(True),
                staff_services.c.service_id == service.id,
            )
            .order_by(Staff.sort_order, Staff.id)
        )
    )


def hours_by_staff(session: Session, staff_ids: list[int]) -> dict[int, dict[int, list[tuple[time, time]]]]:
    result: dict[int, dict[int, list[tuple[time, time]]]] = defaultdict(lambda: defaultdict(list))
    if not staff_ids:
        return result
    rows = session.scalars(select(WorkingHours).where(WorkingHours.staff_id.in_(staff_ids)))
    for wh in rows:
        result[wh.staff_id][wh.weekday].append((wh.start_time, wh.end_time))
    for per_day in result.values():
        for ranges in per_day.values():
            ranges.sort()
    return result


def busy_by_staff(
    session: Session,
    staff_ids: list[int],
    start: datetime,
    end: datetime,
    exclude_appointment_id: int | None = None,
) -> tuple[dict[int, list[Interval]], dict[int, list[Interval]]]:
    """Devuelve (citas, bloqueos) por profesional que se cruzan con [start, end)."""
    appts: dict[int, list[Interval]] = defaultdict(list)
    blocks: dict[int, list[Interval]] = defaultdict(list)
    if not staff_ids:
        return appts, blocks
    q = select(Appointment).where(
        Appointment.staff_id.in_(staff_ids),
        Appointment.status == AppointmentStatus.CONFIRMED,
        Appointment.starts_at < end,
        Appointment.ends_at > start,
    )
    if exclude_appointment_id is not None:
        q = q.where(Appointment.id != exclude_appointment_id)
    for a in session.scalars(q):
        appts[a.staff_id].append(Interval(a.starts_at, a.ends_at))
    for t in session.scalars(
        select(TimeOff).where(
            TimeOff.staff_id.in_(staff_ids), TimeOff.starts_at < end, TimeOff.ends_at > start
        )
    ):
        blocks[t.staff_id].append(Interval(t.starts_at, t.ends_at))
    return appts, blocks


def earliest_start(business: Business, now: datetime) -> datetime:
    return now + timedelta(minutes=business.min_notice_min)


def compute_day_slots(
    business: Business,
    service: Service,
    staff_ids: list[int],
    day: date,
    hours: dict[int, dict[int, list[tuple[time, time]]]],
    appts: dict[int, list[Interval]],
    blocks: dict[int, list[Interval]],
    earliest: datetime,
) -> dict[datetime, list[int]]:
    """Huecos de un día: {inicio UTC: [ids de profesionales libres]}.

    Los profesionales de cada hueco van ordenados de menos a más ocupado ese día, para
    repartir la carga cuando el cliente elige "el que esté disponible".
    """
    tz = business_tz(business)
    duration = timedelta(minutes=service.duration_min)
    step = timedelta(minutes=business.slot_interval_min)
    day_start, day_end = local_day_bounds(day, tz)

    def load(sid: int) -> int:
        return sum(1 for i in appts.get(sid, []) if i.overlaps(day_start, day_end))

    ordered = sorted(staff_ids, key=lambda sid: (load(sid), staff_ids.index(sid)))
    result: dict[datetime, list[int]] = defaultdict(list)
    for sid in ordered:
        ranges = hours.get(sid, {}).get(day.weekday(), [])
        if not ranges:
            continue
        busy = appts.get(sid, []) + blocks.get(sid, [])
        for t in free_slots(
            day=day, tz=tz, ranges=ranges, duration=duration, step=step, busy=busy, earliest=earliest
        ):
            result[t].append(sid)
    return dict(sorted(result.items()))


def availability_for_day(
    session: Session,
    business: Business,
    service: Service,
    staff_ids: list[int],
    day: date,
    now: datetime,
    exclude_appointment_id: int | None = None,
) -> dict[datetime, list[int]]:
    tz = business_tz(business)
    start, end = local_day_bounds(day, tz)
    hours = hours_by_staff(session, staff_ids)
    appts, blocks = busy_by_staff(session, staff_ids, start, end, exclude_appointment_id)
    return compute_day_slots(
        business, service, staff_ids, day, hours, appts, blocks, earliest_start(business, now)
    )


def days_with_availability(
    session: Session,
    business: Business,
    service: Service,
    staff_ids: list[int],
    now: datetime,
    exclude_appointment_id: int | None = None,
) -> list[tuple[date, dict[datetime, list[int]]]]:
    """Días (desde hoy hasta `max_days_ahead`) que tienen al menos un hueco."""
    tz = business_tz(business)
    today = now.astimezone(tz).date()
    last = today + timedelta(days=business.max_days_ahead)
    window_start, _ = local_day_bounds(today, tz)
    _, window_end = local_day_bounds(last, tz)
    hours = hours_by_staff(session, staff_ids)
    appts, blocks = busy_by_staff(session, staff_ids, window_start, window_end, exclude_appointment_id)
    earliest = earliest_start(business, now)
    out = []
    day = today
    while day <= last:
        slots = compute_day_slots(business, service, staff_ids, day, hours, appts, blocks, earliest)
        if slots:
            out.append((day, slots))
        day += timedelta(days=1)
    return out


def upcoming_for_customer(session: Session, customer: Customer, now: datetime) -> list[Appointment]:
    return list(
        session.scalars(
            select(Appointment)
            .where(
                Appointment.customer_id == customer.id,
                Appointment.status == AppointmentStatus.CONFIRMED,
                Appointment.starts_at > now,
            )
            .order_by(Appointment.starts_at)
        )
    )


# ---------------------------------------------------------------------------
# Cambios
# ---------------------------------------------------------------------------


def compute_reminder_at(business: Business, starts_at: datetime, now: datetime) -> datetime | None:
    """Cuándo mandar el recordatorio. Si la cita se agenda dentro de la ventana del
    recordatorio (p. ej. con menos de 24 h), no se manda: el cliente la acaba de pedir."""
    if business.reminder_hours <= 0:
        return None
    at = starts_at - timedelta(hours=business.reminder_hours)
    return at if at > now else None


def _has_conflict(
    session: Session, staff_id: int, start: datetime, end: datetime, exclude_appointment_id: int | None
) -> bool:
    appts, blocks = busy_by_staff(session, [staff_id], start, end, exclude_appointment_id)
    return bool(appts.get(staff_id) or blocks.get(staff_id))


def _lock_staff(session: Session, staff_id: int) -> Staff | None:
    # En PostgreSQL bloquea la fila del profesional hasta el commit, serializando
    # las reservas que compiten por su agenda. En SQLite todas las transacciones
    # ya son BEGIN IMMEDIATE (ver app/db.py).
    return session.scalars(select(Staff).where(Staff.id == staff_id).with_for_update()).first()


def _pick_free_staff(
    session: Session,
    business: Business,
    service: Service,
    candidates: list[int],
    start: datetime,
    end: datetime,
    now: datetime,
    exclude_appointment_id: int | None,
    enforce_rules: bool,
) -> int:
    if enforce_rules:
        if start < earliest_start(business, now):
            raise SlotUnavailable("Ese horario ya no se puede reservar (muy pronto).")
        tz = business_tz(business)
        last_day = now.astimezone(tz).date() + timedelta(days=business.max_days_ahead)
        if start.astimezone(tz).date() > last_day:
            raise SlotUnavailable("Ese día todavía no está abierto para reservas.")
    hours = hours_by_staff(session, candidates) if enforce_rules else {}
    for sid in candidates:
        staff = _lock_staff(session, sid)
        if staff is None or staff.business_id != business.id or not staff.active:
            continue
        if enforce_rules:
            if service not in staff.services:
                continue
            if not fits_in_ranges(start, end, business_tz(business), hours.get(sid, {})):
                continue
        if _has_conflict(session, sid, start, end, exclude_appointment_id):
            continue
        return sid
    raise SlotUnavailable("Ese horario ya está ocupado.")


def book(
    session: Session,
    *,
    business: Business,
    customer: Customer,
    service: Service,
    staff_candidates: list[int],
    starts_at: datetime,
    now: datetime,
    source: str = "whatsapp",
    notes: str = "",
    enforce_rules: bool = True,
) -> Appointment:
    """Crea una cita con el primer profesional libre de `staff_candidates`.

    `enforce_rules=False` (panel) permite agendar fuera de horario o con poca
    anticipación, pero nunca encima de otra cita o de un bloqueo.
    """
    if service.business_id != business.id or customer.business_id != business.id:
        raise BookingError("Datos de otro negocio")
    ends_at = starts_at + timedelta(minutes=service.duration_min)
    sid = _pick_free_staff(
        session, business, service, staff_candidates, starts_at, ends_at, now, None, enforce_rules
    )
    appt = Appointment(
        business_id=business.id,
        staff_id=sid,
        service_id=service.id,
        customer_id=customer.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=AppointmentStatus.CONFIRMED,
        source=source,
        notes=notes,
        reminder_at=compute_reminder_at(business, starts_at, now),
        created_at=now,
        updated_at=now,
    )
    session.add(appt)
    session.flush()
    return appt


def ensure_can_change(appt: Appointment, business: Business, now: datetime) -> None:
    if appt.status != AppointmentStatus.CONFIRMED:
        raise ChangeNotAllowed("La cita ya no está activa.")
    if appt.starts_at <= now:
        raise ChangeNotAllowed("La cita ya pasó.")
    if appt.starts_at - now < timedelta(minutes=business.change_limit_min):
        raise ChangeNotAllowed("Falta muy poco para la cita.")


def can_customer_change(appt: Appointment, business: Business, now: datetime) -> bool:
    try:
        ensure_can_change(appt, business, now)
    except ChangeNotAllowed:
        return False
    return True


def reschedule(
    session: Session,
    appt: Appointment,
    *,
    new_start: datetime,
    now: datetime,
    by: str,
    staff_candidates: list[int] | None = None,
    enforce_rules: bool = True,
) -> Appointment:
    business = appt.business
    if by == "customer":
        ensure_can_change(appt, business, now)
    elif appt.status != AppointmentStatus.CONFIRMED:
        raise ChangeNotAllowed("La cita ya no está activa.")
    new_end = new_start + timedelta(minutes=appt.service.duration_min)
    sid = _pick_free_staff(
        session,
        business,
        appt.service,
        staff_candidates or [appt.staff_id],
        new_start,
        new_end,
        now,
        appt.id,
        enforce_rules,
    )
    appt.staff_id = sid
    appt.starts_at = new_start
    appt.ends_at = new_end
    appt.reminder_at = compute_reminder_at(business, new_start, now)
    appt.reminder_sent_at = None
    appt.reminder_attempts = 0
    appt.customer_confirmed_at = None
    appt.updated_at = now
    session.flush()
    session.refresh(appt)
    return appt


def cancel(session: Session, appt: Appointment, *, now: datetime, by: str) -> Appointment:
    if by == "customer":
        ensure_can_change(appt, appt.business, now)
    elif appt.status != AppointmentStatus.CONFIRMED:
        raise ChangeNotAllowed("La cita ya no está activa.")
    appt.status = AppointmentStatus.CANCELLED
    appt.cancelled_at = now
    appt.cancelled_by = by
    appt.reminder_at = None
    appt.updated_at = now
    session.flush()
    return appt


def set_outcome(session: Session, appt: Appointment, status: str, now: datetime) -> Appointment:
    """Marca una cita como atendida o 'no vino' (desde el panel)."""
    if status not in (AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW, AppointmentStatus.CONFIRMED):
        raise BookingError("Estado inválido")
    if appt.status == AppointmentStatus.CANCELLED:
        raise ChangeNotAllowed("La cita está cancelada.")
    appt.status = status
    appt.updated_at = now
    session.flush()
    return appt
