"""Motor de conversación del bot de citas.

Flujo guiado con botones y listas de WhatsApp (sin IA en el piloto, costo cero):

    menú ─┬─ Agendar ─► servicio ─► profesional ─► día ─► (franja) ─► hora ─► confirmar
          ├─ Mis citas ─► cita ─┬─ Reprogramar ─► día ─► (franja) ─► hora ─► confirmar
          │                     └─ Cancelar ─► ¿seguro?
          └─ Ubicación y precios

El estado vive en la tabla `conversations`. Los ids de botones/filas llevan la acción
(`svc:3`, `day:2026-11-12`, `rem:ok:15`...). Las acciones de "paso" solo se aceptan
si la conversación está en ese paso; si el cliente toca un botón viejo se le dice que
la opción ya no está vigente.

El motor no envía nada: devuelve la lista de mensajes; quien lo llama hace commit y
después envía.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bot import texts as t
from app.domain import booking
from app.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Conversation,
    Customer,
    Service,
    Staff,
)
from app.whatsapp.messages import Buttons, Inbound, ListMessage, Outbound, Row, Text

IDLE = "idle"
SERVICE = "service"
STAFF = "staff"
DAY = "day"
PERIOD = "period"
SLOT = "slot"
CONFIRM = "confirm"
CANCEL_CONFIRM = "cancel_confirm"

FLOW_TIMEOUT = timedelta(hours=2)
PAGE_SIZE = 9  # filas por página cuando hay "Ver más" (WhatsApp permite 10 en total)

# Franjas para no mostrar listas enormes de horarios
PERIODS = {
    "m": ("En la mañana", 0, 12),
    "t": ("En la tarde", 12, 17),
    "n": ("En la noche", 17, 24),
}

CANCEL_WORDS = ("cancel", "anular")
RESCHEDULE_WORDS = ("reprogram", "cambiar", "mover", "otro horario", "otra hora", "otro dia")
MINE_WORDS = ("mis citas", "mi cita", "mi turno", "mis turnos")
BOOK_WORDS = ("agend", "cita", "reserv", "turno", "apart", "corte", "quiero")
INFO_WORDS = ("ubicacion", "direccion", "donde", "precio", "cuanto", "costo", "cuesta")
HOURS_WORDS = ("horario", "abren", "cierran")
MENU_WORDS = ("menu", "inicio", "hola", "salir", "volver", "buenas", "buenos", "hey")


def _contains(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


class Bot:
    def __init__(self, session: Session, business: Business, now: datetime | None = None):
        self.s = session
        self.b = business
        self.now = now or datetime.now(timezone.utc)
        self.tz = booking.business_tz(business)
        self.out: list[Outbound] = []
        self._prefix = ""
        self.customer: Customer
        self.conv: Conversation

    # ------------------------------------------------------------------ entrada

    def handle(self, msg: Inbound) -> tuple[Customer, list[Outbound]]:
        self.customer = self._get_customer(msg)
        self.conv = self._get_conversation()
        if self.conv.state != IDLE and self.conv.updated_at < self.now - FLOW_TIMEOUT:
            self._reset()

        if msg.kind == "choice":
            self._on_choice(msg.choice_id)
        elif msg.kind == "text":
            self._on_text(msg.text)
        else:
            self._on_unsupported(msg.raw_type)

        self.conv.updated_at = self.now
        return self.customer, self.out

    def _get_customer(self, msg: Inbound) -> Customer:
        phone = "".join(ch for ch in msg.from_phone if ch.isdigit())
        customer = self.s.scalars(
            select(Customer).where(Customer.business_id == self.b.id, Customer.phone == phone)
        ).first()
        if customer is None:
            customer = Customer(business_id=self.b.id, phone=phone, created_at=self.now)
            self.s.add(customer)
        if msg.profile_name:
            customer.profile_name = msg.profile_name
            if not customer.name:
                customer.name = msg.profile_name
        customer.last_inbound_at = self.now
        self.s.flush()
        return customer

    def _get_conversation(self) -> Conversation:
        conv = self.s.scalars(
            select(Conversation).where(Conversation.customer_id == self.customer.id)
        ).first()
        if conv is None:
            conv = Conversation(
                business_id=self.b.id, customer_id=self.customer.id, state=IDLE, data={},
                updated_at=self.now,
            )
            self.s.add(conv)
            self.s.flush()
        return conv

    def _on_choice(self, choice_id: str) -> None:
        action, _, arg = choice_id.partition(":")
        global_actions: dict[str, Callable[[str], None]] = {
            "menu": self._on_menu,
            "appt": self._show_appointment,
            "rs": self._start_reschedule,
            "cx": self._ask_cancel,
            "cxy": self._do_cancel,
            "cxn": self._keep_appointment,
            "rem": self._on_reminder_reply,
            "abort": self._abort,
        }
        step_actions: dict[str, tuple[str, Callable[[str], None]]] = {
            "svc": (SERVICE, self._on_service),
            "svcp": (SERVICE, lambda a: self._ask_service(int(a))),
            "stf": (STAFF, self._on_staff),
            "stfp": (STAFF, lambda a: self._ask_staff(int(a))),
            "day": (DAY, self._on_day),
            "dayp": (DAY, lambda a: self._ask_day(int(a))),
            "per": (PERIOD, self._on_period),
            "slot": (SLOT, self._on_slot),
            "slotp": (SLOT, lambda a: self._ask_slot(int(a))),
            "ok": (CONFIRM, self._on_confirm),
            "chg": (CONFIRM, lambda a: self._go_to_day()),
        }
        try:
            if action in global_actions:
                global_actions[action](arg)
            elif action in step_actions:
                state, handler = step_actions[action]
                if self.conv.state != state:
                    self._stale_option()
                else:
                    handler(arg)
            else:
                self._menu()
        except (ValueError, KeyError):
            # id mal formado o dato que ya no existe
            self._stale_option()

    def _on_text(self, raw: str) -> None:
        text = t.normalizar(raw)
        if _contains(text, CANCEL_WORDS) or _contains(text, RESCHEDULE_WORDS) or _contains(text, MINE_WORDS):
            self._reset()
            self._my_appointments()
        elif _contains(text, INFO_WORDS):
            self._reset()
            self._info()
        elif _contains(text, BOOK_WORDS):
            self._reset()
            self._start_booking()
        elif _contains(text, HOURS_WORDS):
            self._reset()
            self._info()
        elif _contains(text, MENU_WORDS) or self.conv.state == IDLE:
            self._reset()
            self._menu()
        else:
            # Escribió algo en medio de un flujo: repetimos el paso actual.
            self._prefix = "Elige una opción de la lista 👇 o escribe *menú* para empezar de nuevo.\n\n"
            self._repeat_step()

    def _on_unsupported(self, raw_type: str) -> None:
        if raw_type in ("audio", "voice"):
            intro = "Por ahora no puedo escuchar audios 🙈. Te ayudo con estos botones:"
        else:
            intro = "Por ahora solo entiendo texto y botones. Te ayudo con esto:"
        self._reset()
        self._menu(intro)

    # ------------------------------------------------------------------ utilidades

    def _set(self, state: str, **updates) -> None:
        data = dict(self.conv.data or {})
        data.update(updates)
        self.conv.state = state
        self.conv.data = data

    def _reset(self) -> None:
        self.conv.state = IDLE
        self.conv.data = {}

    @property
    def data(self) -> dict:
        return self.conv.data or {}

    def _body(self, body: str) -> str:
        body, self._prefix = self._prefix + body, ""
        return body

    def _text(self, body: str) -> None:
        self.out.append(Text(self.customer.phone, self._body(body)))

    def _buttons(self, body: str, buttons: list[tuple[str, str]]) -> None:
        self.out.append(Buttons(self.customer.phone, self._body(body), buttons))

    def _list(
        self, body: str, button: str, rows: list[Row], page: int, more_prefix: str, range_hint: bool = False
    ) -> None:
        self.out.append(
            ListMessage(
                self.customer.phone, self._body(body), button,
                self._paginate(rows, page, more_prefix, range_hint),
            )
        )

    @staticmethod
    def _paginate(rows: list[Row], page: int, more_prefix: str, range_hint: bool = False) -> list[Row]:
        if page == 0 and len(rows) <= 10:
            return rows
        start = page * PAGE_SIZE
        chunk = rows[start : start + PAGE_SIZE]
        if not chunk:
            chunk = rows[:PAGE_SIZE]
            start = 0
            page = 0
        next_start = start + PAGE_SIZE
        if next_start < len(rows):
            desc = "Más opciones"
            if range_hint:
                last = rows[min(next_start + PAGE_SIZE, len(rows)) - 1]
                desc = f"De {rows[next_start].title} a {last.title}"
            chunk = chunk + [Row(f"{more_prefix}:{page + 1}", "Ver más…", desc)]
        return chunk

    def _local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def _today(self) -> date:
        return self.now.astimezone(self.tz).date()

    def _staff_label(self) -> str:
        return self.b.staff_label or "profesional"

    def _stale_option(self) -> None:
        self._reset()
        self._menu("Esa opción ya no está disponible. Empecemos de nuevo 👇")

    def _abort(self, _arg: str = "") -> None:
        self._reset()
        self._menu("Listo, no hice ningún cambio. ¿Te ayudo con algo más?")

    def _own_appointment(self, arg: str) -> Appointment | None:
        appt = self.s.get(Appointment, int(arg))
        if appt is None or appt.customer_id != self.customer.id or appt.business_id != self.b.id:
            return None
        return appt

    def _describe(self, appt_or_parts: Appointment | tuple[Service, Staff, datetime]) -> str:
        if isinstance(appt_or_parts, Appointment):
            service, staff, start = appt_or_parts.service, appt_or_parts.staff, appt_or_parts.starts_at
        else:
            service, staff, start = appt_or_parts
        local = self._local(start)
        price = t.precio(service.price_cents, self.b.currency_symbol)
        extra = f", {price}" if price else ""
        return (
            f"✂️ *{service.name}* ({service.duration_min} min{extra})\n"
            f"💈 Con *{staff.name}*\n"
            f"📅 *{t.capitalizar(t.fecha_larga(local))}* a las *{t.hora(local)}*"
        )

    def _call_us(self) -> str:
        return f" Llámanos o escríbenos al {self.b.phone}." if self.b.phone else ""

    # ------------------------------------------------------------------ menú e info

    def _menu(self, intro: str | None = None) -> None:
        if intro is None:
            nombre = t.primer_nombre(self.customer.name)
            saludo = f"¡Hola, {nombre}! 👋" if nombre else "¡Hola! 👋"
            intro = f"{saludo} Soy el asistente de citas de *{self.b.name}*.\n¿Qué quieres hacer?"
        self._buttons(
            intro,
            [("menu:book", "Agendar cita"), ("menu:mine", "Mis citas"), ("menu:info", "Ubicación y precios")],
        )

    def _on_menu(self, arg: str) -> None:
        self._reset()
        if arg == "book":
            self._start_booking()
        elif arg == "mine":
            self._my_appointments()
        elif arg == "info":
            self._info()
        else:
            self._menu()

    def _info(self) -> None:
        lines = [f"📍 *{self.b.name}*"]
        if self.b.address:
            lines.append(self.b.address)
        if self.b.info_text:
            lines += ["", self.b.info_text.strip()]
        services = booking.active_services(self.s, self.b)
        if services:
            lines += ["", "*Servicios*"]
            for s in services:
                price = t.precio(s.price_cents, self.b.currency_symbol)
                lines.append(f"• {s.name} ({s.duration_min} min)" + (f" — {price}" if price else ""))
        if self.b.phone:
            lines += ["", f"📞 {self.b.phone}"]
        self._buttons("\n".join(lines), [("menu:book", "Agendar cita"), ("menu:mine", "Mis citas")])

    # ------------------------------------------------------------------ agendar

    def _start_booking(self) -> None:
        services = booking.active_services(self.s, self.b)
        if not services:
            self._text("Por ahora no hay servicios disponibles para agendar." + self._call_us())
            return
        self._set(SERVICE, mode="book")
        if len(services) == 1:
            self._on_service(str(services[0].id))
        else:
            self._ask_service(0)

    def _ask_service(self, page: int = 0) -> None:
        rows = []
        for s in booking.active_services(self.s, self.b):
            price = t.precio(s.price_cents, self.b.currency_symbol)
            rows.append(Row(f"svc:{s.id}", s.name, f"{s.duration_min} min" + (f" · {price}" if price else "")))
        self._list("¿Qué servicio quieres? ✂️", "Ver servicios", rows, page, "svcp")

    def _on_service(self, arg: str) -> None:
        service = self.s.get(Service, int(arg))
        if service is None or service.business_id != self.b.id or not service.active:
            self._stale_option()
            return
        staff = booking.staff_for_service(self.s, self.b, service)
        if not staff:
            self._reset()
            self._menu(f"Por ahora nadie está dando *{service.name}*. ¿Te ayudo con otra cosa?")
            return
        if len(staff) == 1:
            self._set(DAY, service_id=service.id, staff_id=staff[0].id)
            self._ask_day(0)
        else:
            self._set(STAFF, service_id=service.id)
            self._ask_staff(0)

    def _service(self) -> Service:
        service = self.s.get(Service, int(self.data["service_id"]))
        if service is None or service.business_id != self.b.id:
            raise KeyError("service")
        return service

    def _ask_staff(self, page: int = 0) -> None:
        label = self._staff_label()
        staff = booking.staff_for_service(self.s, self.b, self._service())
        rows = [Row("stf:any", "El primero disponible", f"Cualquier {label}")]
        rows += [Row(f"stf:{m.id}", m.name) for m in staff]
        self._list(f"¿Con qué {label} quieres tu cita? 💈", f"Elegir {label}", rows, page, "stfp")

    def _on_staff(self, arg: str) -> None:
        if arg == "any":
            self._set(DAY, staff_id=None)
        else:
            staff_ids = [m.id for m in booking.staff_for_service(self.s, self.b, self._service())]
            if int(arg) not in staff_ids:
                self._stale_option()
                return
            self._set(DAY, staff_id=int(arg))
        self._ask_day(0)

    def _staff_ids(self) -> list[int]:
        if self.data.get("staff_id"):
            return [int(self.data["staff_id"])]
        return [m.id for m in booking.staff_for_service(self.s, self.b, self._service())]

    def _exclude_id(self) -> int | None:
        return self.data.get("appointment_id")

    def _go_to_day(self) -> None:
        self._set(DAY, day=None, period=None, slot=None, slot_staff_id=None)
        self._ask_day(0)

    def _ask_day(self, page: int = 0) -> None:
        days = booking.days_with_availability(
            self.s, self.b, self._service(), self._staff_ids(), self.now, self._exclude_id()
        )
        if not days:
            self._reset()
            self._menu(
                f"Uy, no encontré horarios libres en los próximos {self.b.max_days_ahead} días 😕."
                + self._call_us()
            )
            return
        today = self._today()
        rows = []
        for d, slots in days:
            first = self._local(next(iter(slots)))
            n = len(slots)
            rows.append(
                Row(f"day:{d.isoformat()}", t.dia_relativo(d, today),
                    f"{n} horario{'s' if n != 1 else ''} · desde las {t.hora(first)}")
            )
        if self.data.get("mode") == "reschedule":
            body = "¿Para qué día movemos tu cita? 📅"
        else:
            body = "¿Qué día te acomoda? 📅"
        self._list(body, "Ver días", rows, page, "dayp")

    def _day_slots(self) -> dict[datetime, list[int]]:
        day = date.fromisoformat(self.data["day"])
        return booking.availability_for_day(
            self.s, self.b, self._service(), self._staff_ids(), day, self.now, self._exclude_id()
        )

    def _period_of(self, dt: datetime) -> str:
        h = self._local(dt).hour
        for key, (_label, start, end) in PERIODS.items():
            if start <= h < end:
                return key
        return "n"

    def _on_day(self, arg: str) -> None:
        day = date.fromisoformat(arg)
        if not (self._today() <= day <= self._today() + timedelta(days=self.b.max_days_ahead)):
            self._stale_option()
            return
        self._set(DAY, day=day.isoformat(), period=None)
        slots = self._day_slots()
        if not slots:
            self._prefix = "Ese día ya no tiene horarios libres.\n\n"
            self._ask_day(0)
            return
        periods = [k for k in PERIODS if any(self._period_of(s) == k for s in slots)]
        if len(slots) > 10 and len(periods) > 1:
            self._set(PERIOD)
            self._ask_period()
        else:
            self._set(SLOT)
            self._ask_slot(0)

    def _ask_period(self) -> None:
        slots = self._day_slots()
        periods = [k for k in PERIODS if any(self._period_of(s) == k for s in slots)]
        day = date.fromisoformat(self.data["day"])
        self._buttons(
            f"Para el *{t.fecha_larga(day)}*, ¿a qué hora te acomoda? 🕐",
            [(f"per:{k}", PERIODS[k][0]) for k in periods],
        )

    def _on_period(self, arg: str) -> None:
        if arg not in PERIODS:
            self._stale_option()
            return
        self._set(SLOT, period=arg)
        self._ask_slot(0)

    def _ask_slot(self, page: int = 0) -> None:
        slots = self._day_slots()
        period = self.data.get("period")
        if period:
            slots = {k: v for k, v in slots.items() if self._period_of(k) == period}
        if not slots:
            self._set(DAY, day=None, period=None)
            self._prefix = "Ya no quedan horarios en ese momento del día.\n\n"
            self._ask_day(0)
            return
        names = {m.id: m.name for m in self.s.scalars(select(Staff).where(Staff.business_id == self.b.id))}
        any_staff = not self.data.get("staff_id")
        rows = []
        for start, staff_ids in slots.items():
            local = self._local(start)
            desc = f"Con {names.get(staff_ids[0], '')}" if any_staff else ""
            rows.append(Row(f"slot:{local:%Y-%m-%dT%H:%M}", t.hora(local), desc))
        day = date.fromisoformat(self.data["day"])
        self._list(f"Horarios libres para el *{t.fecha_larga(day)}*:", "Ver horarios", rows, page, "slotp",
                   range_hint=True)

    def _on_slot(self, arg: str) -> None:
        local = datetime.fromisoformat(arg).replace(tzinfo=self.tz)
        start = local.astimezone(timezone.utc)
        if local.date().isoformat() != self.data.get("day"):
            self._stale_option()
            return
        slots = self._day_slots()
        if start not in slots:
            self._prefix = "Uy, ese horario se acaba de ocupar 😕. Elige otro:\n\n"
            self._ask_slot(0)
            return
        self._set(CONFIRM, slot=start.isoformat(), slot_staff_id=slots[start][0])
        self._ask_confirm()

    def _ask_confirm(self) -> None:
        service = self._service()
        staff = self.s.get(Staff, int(self.data["slot_staff_id"]))
        start = datetime.fromisoformat(self.data["slot"])
        if self.data.get("mode") == "reschedule":
            head = "Vamos a mover tu cita a:"
            question = "¿Confirmo el cambio?"
        else:
            head = "Revisa tu cita:"
            question = "¿La confirmo?"
        body = f"{head}\n\n{self._describe((service, staff, start))}"
        if self.b.address:
            body += f"\n📍 {self.b.address}"
        body += f"\n\n{question}"
        self._buttons(body, [("ok", "Confirmar"), ("chg", "Cambiar horario"), ("abort", "Salir")])

    def _on_confirm(self, _arg: str) -> None:
        start = datetime.fromisoformat(self.data["slot"])
        preferred = int(self.data["slot_staff_id"])
        try:
            if self.data.get("mode") == "reschedule":
                appt = self._own_appointment(str(self.data["appointment_id"]))
                if appt is None:
                    raise KeyError("appointment")
                appt = booking.reschedule(
                    self.s, appt, new_start=start, now=self.now, by="customer",
                    staff_candidates=[appt.staff_id],
                )
                head = "✅ ¡Listo! Tu cita quedó reprogramada:"
            else:
                candidates = [preferred] + [sid for sid in self._staff_ids() if sid != preferred]
                appt = booking.book(
                    self.s, business=self.b, customer=self.customer, service=self._service(),
                    staff_candidates=candidates, starts_at=start, now=self.now, source="whatsapp",
                )
                nombre = t.primer_nombre(self.customer.name)
                head = f"✅ ¡Listo{', ' + nombre if nombre else ''}! Tu cita quedó agendada:"
        except booking.SlotUnavailable:
            self._set(SLOT, slot=None, slot_staff_id=None)
            self._prefix = "Uy, alguien acaba de tomar ese horario 😕. Elige otro:\n\n"
            self._ask_slot(0)
            return
        except booking.ChangeNotAllowed:
            self._reset()
            self._text("Ya no es posible cambiar esa cita por aquí." + self._call_us())
            return

        self._reset()
        body = f"{head}\n\n{self._describe(appt)}"
        if self.b.address:
            body += f"\n📍 {self.b.address}"
        if appt.reminder_at:
            body += "\n\nTe mandaré un recordatorio antes de tu cita."
        body += "\nSi necesitas cambiarla o cancelarla, escríbeme *mis citas*."
        self._text(body)

    # ------------------------------------------------------------------ mis citas

    def _my_appointments(self) -> None:
        appts = booking.upcoming_for_customer(self.s, self.customer, self.now)
        if not appts:
            self._buttons(
                "No tienes citas próximas. ¿Quieres agendar una?",
                [("menu:book", "Agendar cita"), ("menu:info", "Ubicación y precios")],
            )
            return
        if len(appts) == 1:
            self._show_appointment(str(appts[0].id))
            return
        today = self._today()
        rows = []
        for a in appts[:10]:
            local = self._local(a.starts_at)
            rows.append(
                Row(f"appt:{a.id}", f"{t.dia_relativo(local.date(), today)} {t.hora(local)}",
                    f"{a.service.name} con {a.staff.name}")
            )
        self._list("Estas son tus próximas citas. Elige una para cambiarla o cancelarla:",
                   "Ver mis citas", rows, 0, "apptp")

    def _show_appointment(self, arg: str) -> None:
        self._reset()
        appt = self._own_appointment(arg)
        if appt is None or appt.status != AppointmentStatus.CONFIRMED or appt.starts_at <= self.now:
            self._menu("Esa cita ya no está activa. ¿Te ayudo con algo más?")
            return
        body = f"Tu cita:\n\n{self._describe(appt)}"
        if booking.can_customer_change(appt, self.b, self.now):
            self._buttons(
                body + "\n\n¿Qué quieres hacer?",
                [(f"rs:{appt.id}", "Reprogramar"), (f"cx:{appt.id}", "Cancelar cita"), ("menu:home", "Volver")],
            )
        else:
            self._text(body + "\n\nYa falta poco para tu cita, así que no puedo cambiarla por aquí." + self._call_us())

    def _start_reschedule(self, arg: str) -> None:
        appt = self._own_appointment(arg)
        if appt is None or not booking.can_customer_change(appt, self.b, self.now):
            self._reset()
            self._text("Esa cita ya no se puede cambiar por aquí." + self._call_us())
            return
        self._reset()
        self._set(DAY, mode="reschedule", appointment_id=appt.id, service_id=appt.service_id,
                  staff_id=appt.staff_id)
        self._ask_day(0)

    def _ask_cancel(self, arg: str) -> None:
        appt = self._own_appointment(arg)
        if appt is None or not booking.can_customer_change(appt, self.b, self.now):
            self._reset()
            self._text("Esa cita ya no se puede cancelar por aquí." + self._call_us())
            return
        self._set(CANCEL_CONFIRM, appointment_id=appt.id)
        local = self._local(appt.starts_at)
        self._buttons(
            f"¿Seguro que quieres cancelar tu cita del *{t.fecha_larga(local)}* a las *{t.hora(local)}*?",
            [(f"cxy:{appt.id}", "Sí, cancelar"), ("cxn", "No, conservarla")],
        )

    def _do_cancel(self, arg: str) -> None:
        self._reset()
        appt = self._own_appointment(arg)
        if appt is None:
            self._stale_option()
            return
        try:
            booking.cancel(self.s, appt, now=self.now, by="customer")
        except booking.ChangeNotAllowed:
            self._text("Esa cita ya no se puede cancelar por aquí." + self._call_us())
            return
        self._buttons(
            "Listo, tu cita fue cancelada. ¿Quieres agendar otra?",
            [("menu:book", "Agendar otra"), ("menu:home", "Menú")],
        )

    def _keep_appointment(self, _arg: str = "") -> None:
        self._reset()
        self._text("Perfecto, tu cita sigue en pie 👍")

    # ------------------------------------------------------------------ recordatorios

    def _on_reminder_reply(self, arg: str) -> None:
        action, _, appt_id = arg.partition(":")
        if action == "rs":
            self._start_reschedule(appt_id)
            return
        if action == "cx":
            self._ask_cancel(appt_id)
            return
        appt = self._own_appointment(appt_id)
        self._reset()
        if appt is None or appt.status != AppointmentStatus.CONFIRMED or appt.starts_at <= self.now:
            self._menu("Esa cita ya no está activa. ¿Te ayudo con algo más?")
            return
        appt.customer_confirmed_at = self.now
        self._text("¡Gracias por confirmar! Te esperamos 💈")

    # ------------------------------------------------------------------ repetir paso

    def _repeat_step(self) -> None:
        state = self.conv.state
        try:
            if state == SERVICE:
                self._ask_service(0)
            elif state == STAFF:
                self._ask_staff(0)
            elif state == DAY:
                self._ask_day(0)
            elif state == PERIOD:
                self._ask_period()
            elif state == SLOT:
                self._ask_slot(0)
            elif state == CONFIRM:
                self._ask_confirm()
            elif state == CANCEL_CONFIRM:
                self._prefix = ""
                self._ask_cancel(str(self.data["appointment_id"]))
            else:
                self._prefix = ""
                self._menu()
        except (KeyError, ValueError, TypeError):
            self._prefix = ""
            self._reset()
            self._menu()
