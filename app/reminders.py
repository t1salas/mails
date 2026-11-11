"""Recordatorios de citas.

Cada minuto se buscan citas con `reminder_at` vencido. Para gastar lo mínimo:
- Si el cliente nos escribió en las últimas ~24 h, el recordatorio va como mensaje
  normal con botones (gratis, no requiere plantilla).
- Si no, va como plantilla aprobada por Meta (`Business.reminder_template`), que Meta
  cobra por mensaje según el país.

Cada recordatorio se "reclama" (se marca como enviado) antes de mandarlo, así nunca
sale duplicado aunque el proceso se reinicie a mitad de camino. Si el envío falla, se
libera y se reintenta hasta MAX_ATTEMPTS veces.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.bot import texts as t
from app.db import session_scope
from app.domain.booking import business_tz
from app.models import Appointment, AppointmentStatus, Business
from app.whatsapp.client import Sender, deliver
from app.whatsapp.messages import Buttons, Outbound, Template

log = logging.getLogger(__name__)

FREE_WINDOW = timedelta(hours=23, minutes=30)  # margen sobre la ventana de 24 h de Meta
MAX_ATTEMPTS = 3
MIN_LEAD = timedelta(minutes=30)  # no recordar citas que están por empezar
BATCH = 50


def build_reminder(business: Business, appt: Appointment, now: datetime) -> Outbound | None:
    customer = appt.customer
    local = appt.starts_at.astimezone(business_tz(business))
    when = f"{t.fecha_larga(local)} a las {t.hora(local)}"
    nombre = t.primer_nombre(customer.name)
    payloads = [f"rem:ok:{appt.id}", f"rem:rs:{appt.id}", f"rem:cx:{appt.id}"]

    in_window = customer.last_inbound_at is not None and now - customer.last_inbound_at < FREE_WINDOW
    if in_window:
        body = (
            f"⏰ {nombre + ', t' if nombre else 'T'}e recordamos tu cita en *{business.name}*:\n\n"
            f"✂️ {appt.service.name} con {appt.staff.name}\n"
            f"📅 *{t.capitalizar(when)}*"
        )
        if business.address:
            body += f"\n📍 {business.address}"
        body += "\n\n¿Nos confirmas tu asistencia?"
        return Buttons(
            customer.phone,
            body,
            [(payloads[0], "Confirmo"), (payloads[1], "Reprogramar"), (payloads[2], "Cancelar")],
        )
    if business.reminder_template:
        return Template(
            to=customer.phone,
            name=business.reminder_template,
            lang=business.template_lang,
            body_params=[nombre or "cliente", business.name, appt.service.name, appt.staff.name, when],
            quick_reply_payloads=payloads,
        )
    return None


def run_reminders(now: datetime | None = None, sender: Sender | None = None) -> int:
    """Envía los recordatorios vencidos. Devuelve cuántos se enviaron."""
    now = now or datetime.now(timezone.utc)
    claimed: list[tuple[int, int, str | None, int, Outbound]] = []
    with session_scope() as s:
        due = s.scalars(
            select(Appointment)
            .where(
                Appointment.status == AppointmentStatus.CONFIRMED,
                Appointment.reminder_at.is_not(None),
                Appointment.reminder_at <= now,
                Appointment.reminder_sent_at.is_(None),
                Appointment.reminder_attempts < MAX_ATTEMPTS,
                Appointment.starts_at > now + MIN_LEAD,
            )
            .order_by(Appointment.reminder_at)
            .limit(BATCH)
        ).all()
        for appt in due:
            msg = build_reminder(appt.business, appt, now)
            if msg is None:
                log.warning(
                    "Cita %s: cliente fuera de la ventana de 24 h y sin plantilla configurada; "
                    "no se puede enviar recordatorio", appt.id,
                )
                appt.reminder_attempts = MAX_ATTEMPTS
                continue
            appt.reminder_sent_at = now
            claimed.append((appt.id, appt.business_id, appt.business.wa_phone_number_id, appt.customer_id, msg))

    sent = 0
    for appt_id, business_id, pnid, customer_id, msg in claimed:
        ok = deliver(business_id=business_id, phone_number_id=pnid, customer_id=customer_id,
                     messages=[msg], sender=sender)[0]
        if ok:
            sent += 1
            continue
        with session_scope() as s:
            appt = s.get(Appointment, appt_id)
            if appt is not None:
                appt.reminder_sent_at = None
                appt.reminder_attempts += 1
    return sent
