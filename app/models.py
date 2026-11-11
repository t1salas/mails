"""Modelo de datos.

Todo cuelga de `Business` (el negocio/tenant) para que el mismo sistema sirva a muchas
barberías, peluquerías, salones de uñas o spas. Internamente se habla de "staff"
(profesional) y "service"; el texto que ve el cliente se configura por negocio
(`staff_label`: "barbero", "estilista", "manicurista"...).

Todas las fechas/horas se guardan en UTC; se convierten a la zona del negocio al mostrar.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64))  # IANA, p. ej. "America/Mexico_City"
    currency_symbol: Mapped[str] = mapped_column(String(8), default="$")
    default_country_code: Mapped[str] = mapped_column(String(4), default="52")
    # phone_number_id del número de WhatsApp (panel de Meta). Enruta los mensajes al negocio.
    wa_phone_number_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    address: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    info_text: Mapped[str] = mapped_column(Text, default="")
    staff_label: Mapped[str] = mapped_column(String(30), default="barbero")
    # Reglas de agenda
    slot_interval_min: Mapped[int] = mapped_column(Integer, default=15)
    min_notice_min: Mapped[int] = mapped_column(Integer, default=60)
    max_days_ahead: Mapped[int] = mapped_column(Integer, default=14)
    change_limit_min: Mapped[int] = mapped_column(Integer, default=120)
    # Recordatorios
    reminder_hours: Mapped[int] = mapped_column(Integer, default=24)
    reminder_template: Mapped[str | None] = mapped_column(String(80))
    template_lang: Mapped[str] = mapped_column(String(10), default="es_MX")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    staff: Mapped[list[Staff]] = relationship(back_populates="business", order_by="Staff.sort_order")
    services: Mapped[list[Service]] = relationship(
        back_populates="business", order_by="Service.sort_order"
    )


staff_services = Table(
    "staff_services",
    Base.metadata,
    Column("staff_id", ForeignKey("staff.id", ondelete="CASCADE"), primary_key=True),
    Column("service_id", ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
)


class Staff(Base):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    business: Mapped[Business] = relationship(back_populates="staff")
    services: Mapped[list[Service]] = relationship(secondary=staff_services, back_populates="staff")
    working_hours: Mapped[list[WorkingHours]] = relationship(
        back_populates="staff", cascade="all, delete-orphan"
    )


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    duration_min: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    business: Mapped[Business] = relationship(back_populates="services")
    staff: Mapped[list[Staff]] = relationship(secondary=staff_services, back_populates="services")


class WorkingHours(Base):
    """Un tramo de trabajo semanal. Puede haber varios por día (p. ej. antes y después de comer)."""

    __tablename__ = "working_hours"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 0 = lunes ... 6 = domingo
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)

    staff: Mapped[Staff] = relationship(back_populates="working_hours")


class TimeOff(Base):
    """Bloqueo de agenda: vacaciones, día libre, trámite, etc."""

    __tablename__ = "time_off"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), index=True)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    reason: Mapped[str] = mapped_column(String(120), default="")

    staff: Mapped[Staff] = relationship()


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    phone: Mapped[str] = mapped_column(String(20))  # solo dígitos, con código de país (wa_id)
    name: Mapped[str] = mapped_column(String(120), default="")
    profile_name: Mapped[str] = mapped_column(String(120), default="")
    # Último mensaje recibido: define la ventana de 24 h en la que responder es gratis.
    last_inbound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppointmentStatus:
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"

    LABELS = {
        CONFIRMED: "Confirmada",
        CANCELLED: "Cancelada",
        COMPLETED: "Atendida",
        NO_SHOW: "No vino",
    }


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (Index("ix_appointments_staff_start", "staff_id", "starts_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(20), default=AppointmentStatus.CONFIRMED)
    source: Mapped[str] = mapped_column(String(20), default="whatsapp")  # whatsapp | panel
    notes: Mapped[str] = mapped_column(Text, default="")
    reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reminder_attempts: Mapped[int] = mapped_column(Integer, default=0)
    customer_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_by: Mapped[str | None] = mapped_column(String(20))  # customer | business
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    staff: Mapped[Staff] = relationship()
    service: Mapped[Service] = relationship()
    customer: Mapped[Customer] = relationship()
    business: Mapped[Business] = relationship()


class Conversation(Base):
    """Estado del flujo del bot para cada cliente."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), unique=True)
    state: Mapped[str] = mapped_column(String(30), default="idle")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MessageLog(Base):
    """Bitácora de mensajes. `wa_message_id` único evita procesar dos veces un reintento de Meta."""

    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class User(Base):
    """Usuario del panel. `owner` ve toda la agenda; `staff` solo la suya."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(80), default="")
    role: Mapped[str] = mapped_column(String(10), default="owner")  # owner | staff
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    business: Mapped[Business] = relationship()
