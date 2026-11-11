"""Panel web para la barbería: agenda del día/semana, citas manuales y bloqueos.

HTML renderizado en el servidor (sin build de frontend), pensado para usarse en el celular.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bot import texts as t
from app.db import session_scope
from app.domain import booking
from app.domain.availability import local_day_bounds
from app.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Customer,
    Service,
    Staff,
    TimeOff,
    User,
)
from app.panel.auth import verify_password
from app.reminders import FREE_WINDOW
from app.whatsapp.client import deliver
from app.whatsapp.messages import Text

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter()


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def _csrf_ok(request: Request, token: str) -> bool:
    expected = request.session.get("csrf")
    return bool(expected) and bool(token) and secrets.compare_digest(expected, token)


def _flash(request: Request, msg: str, kind: str = "ok", link: str | None = None, link_text: str = "") -> None:
    flashes = list(request.session.get("flash", []))
    flashes.append({"msg": msg, "kind": kind, "link": link, "link_text": link_text})
    request.session["flash"] = flashes


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _to_login() -> RedirectResponse:
    return _redirect("/login")


def _user(request: Request, s: Session) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = s.get(User, uid)
    if user is None or not user.active:
        return None
    return user


def _render(request: Request, name: str, ctx: dict) -> object:
    ctx = dict(ctx)
    ctx["csrf"] = _csrf_token(request)
    ctx["flashes"] = request.session.pop("flash", [])
    return TEMPLATES.TemplateResponse(request, name, ctx)


def _parse_day(value: str | None, b: Business) -> date:
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return _now().astimezone(booking.business_tz(b)).date()


def _visible_staff(s: Session, user: User) -> list[Staff]:
    q = (
        select(Staff)
        .where(Staff.business_id == user.business_id, Staff.active.is_(True))
        .order_by(Staff.sort_order, Staff.id)
    )
    if user.role == "staff" and user.staff_id:
        q = q.where(Staff.id == user.staff_id)
    return list(s.scalars(q))


def _can_touch(user: User, staff_id: int) -> bool:
    return user.role == "owner" or user.staff_id == staff_id


def _own_appointment(s: Session, user: User, appt_id: int) -> Appointment | None:
    appt = s.get(Appointment, appt_id)
    if appt is None or appt.business_id != user.business_id or not _can_touch(user, appt.staff_id):
        return None
    return appt


def _normalize_phone(raw: str, country_code: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) <= 10:
        digits = country_code + digits
    return digits


def wa_link(phone: str, text: str = "") -> str:
    url = f"https://wa.me/{phone}"
    return f"{url}?text={quote(text)}" if text else url


def _appt_item(appt: Appointment, tz) -> dict:
    start = appt.starts_at.astimezone(tz)
    end = appt.ends_at.astimezone(tz)
    return {
        "kind": "appt",
        "id": appt.id,
        "sort": appt.starts_at,
        "start": t.hora(start),
        "end": t.hora(end),
        "customer": appt.customer.name or appt.customer.profile_name or appt.customer.phone,
        "phone": appt.customer.phone,
        "wa_link": wa_link(appt.customer.phone),
        "service": appt.service.name,
        "staff": appt.staff.name,
        "status": appt.status,
        "status_label": AppointmentStatus.LABELS.get(appt.status, appt.status),
        "source": appt.source,
        "notes": appt.notes,
        "confirmed_by_customer": appt.customer_confirmed_at is not None,
    }


def _notification(appt: Appointment, text: str) -> dict:
    """Datos para avisar al cliente, tomados mientras la sesión sigue abierta."""
    c = appt.customer
    return {"business_id": appt.business_id, "phone_number_id": appt.business.wa_phone_number_id,
            "customer_id": c.id, "phone": c.phone, "last_inbound_at": c.last_inbound_at, "text": text}


def _notify_customer(request: Request, n: dict) -> None:
    """Avisa al cliente gratis si está dentro de la ventana de 24 h; si no, deja un link
    de WhatsApp para que la barbería le escriba desde su propio teléfono (sin costo).

    Se llama con la sesión de base de datos ya cerrada: `deliver` abre la suya.
    """
    if n["last_inbound_at"] and _now() - n["last_inbound_at"] < FREE_WINDOW:
        ok = deliver(
            business_id=n["business_id"], phone_number_id=n["phone_number_id"],
            customer_id=n["customer_id"], messages=[Text(n["phone"], n["text"])],
        )[0]
        if ok:
            _flash(request, "Le avisamos al cliente por WhatsApp.")
            return
    _flash(request, "Avísale al cliente:", "warn", wa_link(n["phone"], n["text"]), "Abrir WhatsApp")


# ---------------------------------------------------------------------------
# Sesión
# ---------------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request):
    return _render(request, "login.html", {"business": None, "user": None})


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    if not _csrf_ok(request, csrf):
        _flash(request, "La sesión expiró, intenta de nuevo.", "error")
        return _to_login()
    with session_scope() as s:
        user = s.scalars(select(User).where(User.username == username.strip().lower())).first()
        if user is None or not user.active or not verify_password(password, user.password_hash):
            _flash(request, "Usuario o contraseña incorrectos.", "error")
            return _to_login()
        request.session.clear()
        request.session["uid"] = user.id
    return _redirect("/agenda")


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    if _csrf_ok(request, csrf):
        request.session.clear()
    return _to_login()


@router.get("/")
def home():
    return _redirect("/agenda")


# ---------------------------------------------------------------------------
# Agenda
# ---------------------------------------------------------------------------


@router.get("/agenda")
def agenda(request: Request, d: str | None = None):
    with session_scope() as s:
        user = _user(request, s)
        if user is None:
            return _to_login()
        b = user.business
        tz = booking.business_tz(b)
        day = _parse_day(d, b)
        start, end = local_day_bounds(day, tz)
        staff = _visible_staff(s, user)
        ids = [m.id for m in staff]
        appts = s.scalars(
            select(Appointment)
            .where(Appointment.staff_id.in_(ids), Appointment.starts_at >= start, Appointment.starts_at < end)
            .order_by(Appointment.starts_at)
        ).all()
        blocks = s.scalars(
            select(TimeOff).where(TimeOff.staff_id.in_(ids), TimeOff.starts_at < end, TimeOff.ends_at > start)
        ).all()
        hours = booking.hours_by_staff(s, ids)
        columns = []
        for m in staff:
            items = [_appt_item(a, tz) for a in appts if a.staff_id == m.id]
            for blk in blocks:
                if blk.staff_id != m.id:
                    continue
                bs, be = max(blk.starts_at, start), min(blk.ends_at, end)
                items.append({
                    "kind": "block", "id": blk.id, "sort": bs,
                    "start": t.hora(bs.astimezone(tz)),
                    "end": "24:00" if be == end else t.hora(be.astimezone(tz)),
                    "reason": blk.reason or "Bloqueado",
                })
            items.sort(key=lambda i: i["sort"])
            ranges = hours.get(m.id, {}).get(day.weekday(), [])
            columns.append({
                "staff": m,
                "hours": ", ".join(f"{a:%H:%M}–{z:%H:%M}" for a, z in ranges) or "Descansa",
                "items": items,
                "active_count": sum(1 for i in items if i["kind"] == "appt" and i["status"] != AppointmentStatus.CANCELLED),
            })
        today = _now().astimezone(tz).date()
        return _render(request, "agenda.html", {
            "user": user, "business": b, "columns": columns, "day": day,
            "day_label": t.capitalizar(t.fecha_larga(day)), "is_today": day == today,
            "prev": (day - timedelta(days=1)).isoformat(), "next": (day + timedelta(days=1)).isoformat(),
            "today": today.isoformat(),
        })


@router.get("/semana")
def semana(request: Request, d: str | None = None):
    with session_scope() as s:
        user = _user(request, s)
        if user is None:
            return _to_login()
        b = user.business
        tz = booking.business_tz(b)
        day = _parse_day(d, b)
        monday = day - timedelta(days=day.weekday())
        start, _ = local_day_bounds(monday, tz)
        _, end = local_day_bounds(monday + timedelta(days=6), tz)
        ids = [m.id for m in _visible_staff(s, user)]
        appts = s.scalars(
            select(Appointment)
            .where(
                Appointment.staff_id.in_(ids), Appointment.starts_at >= start, Appointment.starts_at < end,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .order_by(Appointment.starts_at)
        ).all()
        today = _now().astimezone(tz).date()
        days = []
        for i in range(7):
            d_i = monday + timedelta(days=i)
            items = [_appt_item(a, tz) for a in appts if a.starts_at.astimezone(tz).date() == d_i]
            days.append({"date": d_i, "label": t.capitalizar(t.fecha_larga(d_i)), "items": items,
                         "is_today": d_i == today})
        return _render(request, "semana.html", {
            "user": user, "business": b, "days": days,
            "prev": (monday - timedelta(days=7)).isoformat(), "next": (monday + timedelta(days=7)).isoformat(),
            "today": today.isoformat(),
            "range_label": f"{t.fecha_corta(monday)} – {t.fecha_corta(monday + timedelta(days=6))}",
        })


# ---------------------------------------------------------------------------
# Citas
# ---------------------------------------------------------------------------


def _new_form_ctx(s: Session, user: User, values: dict) -> dict:
    services = booking.active_services(s, user.business)
    return {"user": user, "business": user.business, "services": services,
            "staff": _visible_staff(s, user), "v": values}


@router.get("/citas/nueva")
def nueva_cita_form(request: Request, d: str | None = None, staff_id: int | None = None):
    with session_scope() as s:
        user = _user(request, s)
        if user is None:
            return _to_login()
        day = _parse_day(d, user.business)
        values = {"fecha": day.isoformat(), "hora": "", "staff_id": staff_id or user.staff_id or "",
                  "service_id": "", "telefono": "", "nombre": "", "notas": ""}
        return _render(request, "cita_nueva.html", _new_form_ctx(s, user, values))


@router.post("/citas/nueva")
def nueva_cita(
    request: Request,
    csrf: str = Form(""),
    fecha: str = Form(""),
    hora: str = Form(""),
    staff_id: int = Form(0),
    service_id: int = Form(0),
    telefono: str = Form(""),
    nombre: str = Form(""),
    notas: str = Form(""),
):
    values = {"fecha": fecha, "hora": hora, "staff_id": staff_id, "service_id": service_id,
              "telefono": telefono, "nombre": nombre, "notas": notas}
    with session_scope() as s:
        user = _user(request, s)
        if user is None:
            return _to_login()
        b = user.business

        def fail(msg: str):
            _flash(request, msg, "error")
            return _render(request, "cita_nueva.html", _new_form_ctx(s, user, values))

        if not _csrf_ok(request, csrf):
            return fail("La sesión expiró, intenta de nuevo.")
        service = s.get(Service, service_id)
        staff = s.get(Staff, staff_id)
        if service is None or service.business_id != b.id:
            return fail("Elige un servicio.")
        if staff is None or staff.business_id != b.id or not _can_touch(user, staff.id):
            return fail("Elige un profesional.")
        phone = _normalize_phone(telefono, b.default_country_code)
        if len(phone) < 11:
            return fail("Escribe el teléfono con lada (10 dígitos o más).")
        try:
            local = datetime.combine(date.fromisoformat(fecha), time.fromisoformat(hora))
        except ValueError:
            return fail("Revisa la fecha y la hora.")
        starts_at = local.replace(tzinfo=booking.business_tz(b)).astimezone(timezone.utc)

        customer = s.scalars(
            select(Customer).where(Customer.business_id == b.id, Customer.phone == phone)
        ).first()
        if customer is None:
            customer = Customer(business_id=b.id, phone=phone, name=nombre.strip())
            s.add(customer)
            s.flush()
        elif nombre.strip():
            customer.name = nombre.strip()
        try:
            booking.book(
                s, business=b, customer=customer, service=service, staff_candidates=[staff.id],
                starts_at=starts_at, now=_now(), source="panel", notes=notas.strip(), enforce_rules=False,
            )
        except booking.BookingError as exc:
            s.rollback()
            return fail(f"No se pudo agendar: {exc}")
        msg = (
            f"Hola {t.primer_nombre(customer.name)}, te agendamos en {b.name}: {service.name} con "
            f"{staff.name} el {t.fecha_larga(local)} a las {t.hora(local)}."
        )
        _flash(request, "Cita agendada.", "ok", wa_link(customer.phone, msg), "Avisar al cliente")
        day = local.date().isoformat()
    return _redirect(f"/agenda?d={day}")


@router.post("/citas/{appt_id}/estado")
def cambiar_estado(request: Request, appt_id: int, csrf: str = Form(""), status: str = Form("")):
    with session_scope() as s:
        user = _user(request, s)
        if user is None:
            return _to_login()
        appt = _own_appointment(s, user, appt_id)
        if appt is None or not _csrf_ok(request, csrf):
            _flash(request, "No se pudo actualizar la cita.", "error")
            return _redirect("/agenda")
        try:
            booking.set_outcome(s, appt, status, _now())
            _flash(request, f"Cita marcada como «{AppointmentStatus.LABELS[status]}».")
        except booking.BookingError as exc:
            _flash(request, str(exc), "error")
        day = appt.starts_at.astimezone(booking.business_tz(appt.business)).date().isoformat()
    return _redirect(f"/agenda?d={day}")


@router.post("/citas/{appt_id}/cancelar")
def cancelar_cita(request: Request, appt_id: int, csrf: str = Form("")):
    with session_scope() as s:
        user = _user(request, s)
        if user is None:
            return _to_login()
        appt = _own_appointment(s, user, appt_id)
        if appt is None or not _csrf_ok(request, csrf):
            _flash(request, "No se pudo cancelar la cita.", "error")
            return _redirect("/agenda")
        tz = booking.business_tz(appt.business)
        local = appt.starts_at.astimezone(tz)
        day = local.date().isoformat()
        try:
            booking.cancel(s, appt, now=_now(), by="business")
        except booking.BookingError as exc:
            _flash(request, str(exc), "error")
            return _redirect(f"/agenda?d={day}")
        _flash(request, "Cita cancelada.")
        notice = _notification(
            appt,
            f"Hola {t.primer_nombre(appt.customer.name)}, lamentamos avisarte que tu cita en "
            f"{appt.business.name} del {t.fecha_larga(local)} a las {t.hora(local)} fue cancelada. "
            "Escríbenos *menú* para agendar otro horario.",
        )
    _notify_customer(request, notice)
    return _redirect(f"/agenda?d={day}")


# ---------------------------------------------------------------------------
# Bloqueos de agenda
# ---------------------------------------------------------------------------


@router.get("/bloqueos")
def bloqueos(request: Request):
    with session_scope() as s:
        user = _user(request, s)
        if user is None:
            return _to_login()
        tz = booking.business_tz(user.business)
        staff = _visible_staff(s, user)
        ids = [m.id for m in staff]
        rows = s.scalars(
            select(TimeOff).where(TimeOff.staff_id.in_(ids), TimeOff.ends_at > _now()).order_by(TimeOff.starts_at)
        ).all()
        items = [{
            "id": r.id, "staff": r.staff.name, "reason": r.reason or "Bloqueado",
            "start": r.starts_at.astimezone(tz), "end": r.ends_at.astimezone(tz),
        } for r in rows]
        today = _now().astimezone(tz).date().isoformat()
        return _render(request, "bloqueos.html", {"user": user, "business": user.business,
                                                  "staff": staff, "items": items, "today": today})


@router.post("/bloqueos")
def crear_bloqueo(
    request: Request,
    csrf: str = Form(""),
    staff_id: int = Form(0),
    fecha: str = Form(""),
    desde: str = Form(""),
    hasta: str = Form(""),
    todo_el_dia: str = Form(""),
    motivo: str = Form(""),
):
    with session_scope() as s:
        user = _user(request, s)
        if user is None:
            return _to_login()
        staff = s.get(Staff, staff_id)
        if not _csrf_ok(request, csrf) or staff is None or staff.business_id != user.business_id \
                or not _can_touch(user, staff.id):
            _flash(request, "No se pudo crear el bloqueo.", "error")
            return _redirect("/bloqueos")
        tz = booking.business_tz(user.business)
        try:
            day = date.fromisoformat(fecha)
            if todo_el_dia:
                start, end = local_day_bounds(day, tz)
            else:
                start = datetime.combine(day, time.fromisoformat(desde), tzinfo=tz).astimezone(timezone.utc)
                end = datetime.combine(day, time.fromisoformat(hasta), tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            _flash(request, "Revisa la fecha y las horas.", "error")
            return _redirect("/bloqueos")
        if end <= start:
            _flash(request, "La hora final debe ser después de la inicial.", "error")
            return _redirect("/bloqueos")
        s.add(TimeOff(staff_id=staff.id, starts_at=start, ends_at=end, reason=motivo.strip()))
        appts, _ = booking.busy_by_staff(s, [staff.id], start, end)
        clashes = len(appts.get(staff.id, []))
        _flash(request, f"Horario bloqueado para {staff.name}.")
        if clashes:
            _flash(request, f"Ojo: ya hay {clashes} cita(s) en ese horario. Reprográmalas o cancélalas "
                            "desde la agenda.", "warn")
    return _redirect("/bloqueos")


@router.post("/bloqueos/{block_id}/borrar")
def borrar_bloqueo(request: Request, block_id: int, csrf: str = Form("")):
    with session_scope() as s:
        user = _user(request, s)
        if user is None:
            return _to_login()
        blk = s.get(TimeOff, block_id)
        if blk is None or not _csrf_ok(request, csrf) or blk.staff.business_id != user.business_id \
                or not _can_touch(user, blk.staff_id):
            _flash(request, "No se pudo borrar el bloqueo.", "error")
            return _redirect("/bloqueos")
        s.delete(blk)
        _flash(request, "Bloqueo eliminado.")
    return _redirect("/bloqueos")
