"""Comandos de administración.

    python -m app.cli cargar-config config/negocio.toml
    python -m app.cli crear-usuario --negocio barberia-piloto --usuario dueno --rol owner
    python -m app.cli simular --negocio barberia-piloto
    python -m app.cli recordatorios
    python -m app.cli respaldo
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
import tomllib
from datetime import datetime, time, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import init_db, session_scope
from app.models import Business, Service, Staff, User, WorkingHours

WEEKDAYS = {
    "lun": 0, "lunes": 0, "mar": 1, "martes": 1, "mie": 2, "mié": 2, "miercoles": 2, "miércoles": 2,
    "jue": 3, "jueves": 3, "vie": 4, "viernes": 4, "sab": 5, "sáb": 5, "sabado": 5, "sábado": 5,
    "dom": 6, "domingo": 6,
}

BUSINESS_FIELDS = {
    "name": str, "timezone": str, "currency_symbol": str, "default_country_code": str,
    "wa_phone_number_id": str, "address": str, "phone": str, "info_text": str, "staff_label": str,
    "slot_interval_min": int, "min_notice_min": int, "max_days_ahead": int, "change_limit_min": int,
    "reminder_hours": int, "reminder_template": str, "template_lang": str,
}


def _parse_range(text: str) -> tuple[time, time]:
    start, end = (p.strip() for p in text.split("-"))
    s, e = time.fromisoformat(start), time.fromisoformat(end)
    if e <= s:
        raise ValueError(f"Rango inválido: {text}")
    return s, e


def load_config(path: Path) -> str:
    """Crea o actualiza un negocio con sus servicios, profesionales y horarios."""
    from zoneinfo import ZoneInfo

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    b = data["business"]
    ZoneInfo(b["timezone"])  # valida la zona horaria
    with session_scope() as s:
        business = s.scalars(select(Business).where(Business.slug == b["slug"])).first()
        if business is None:
            business = Business(slug=b["slug"], name=b["name"], timezone=b["timezone"])
            s.add(business)
        for key, cast in BUSINESS_FIELDS.items():
            if key in b:
                value = cast(b[key])
                if key in ("wa_phone_number_id", "reminder_template") and value == "":
                    value = None
                setattr(business, key, value)
        s.flush()

        services_by_name: dict[str, Service] = {}
        existing = {sv.name: sv for sv in s.scalars(select(Service).where(Service.business_id == business.id))}
        for order, sv in enumerate(data.get("services", [])):
            service = existing.get(sv["name"]) or Service(business_id=business.id, name=sv["name"])
            service.duration_min = int(sv["duration_min"])
            price = sv.get("price")
            service.price_cents = None if price is None else round(float(price) * 100)
            service.active = bool(sv.get("active", True))
            service.sort_order = order
            s.add(service)
            services_by_name[service.name] = service
        for name, service in existing.items():
            if name not in services_by_name:
                service.active = False
        s.flush()

        existing_staff = {m.name: m for m in s.scalars(select(Staff).where(Staff.business_id == business.id))}
        seen = set()
        for order, st in enumerate(data.get("staff", [])):
            member = existing_staff.get(st["name"]) or Staff(business_id=business.id, name=st["name"])
            member.active = bool(st.get("active", True))
            member.sort_order = order
            wanted = st.get("services", "*")
            if wanted == "*":
                member.services = [sv for sv in services_by_name.values() if sv.active]
            else:
                missing = [n for n in wanted if n not in services_by_name]
                if missing:
                    raise SystemExit(f"{member.name}: servicios desconocidos {missing}")
                member.services = [services_by_name[n] for n in wanted]
            member.working_hours.clear()
            for day_key, ranges in (st.get("hours") or {}).items():
                weekday = WEEKDAYS.get(day_key.strip().lower())
                if weekday is None:
                    raise SystemExit(f"{member.name}: día desconocido '{day_key}'")
                for rng in ranges:
                    start, end = _parse_range(rng)
                    member.working_hours.append(WorkingHours(weekday=weekday, start_time=start, end_time=end))
            s.add(member)
            seen.add(member.name)
        for name, member in existing_staff.items():
            if name not in seen:
                member.active = False
        slug = business.slug
    return slug


def create_user(slug: str, username: str, role: str, staff_name: str | None, password: str, display: str) -> None:
    from app.panel.auth import hash_password

    with session_scope() as s:
        business = s.scalars(select(Business).where(Business.slug == slug)).first()
        if business is None:
            raise SystemExit(f"No existe el negocio '{slug}'")
        staff_id = None
        if staff_name:
            member = s.scalars(
                select(Staff).where(Staff.business_id == business.id, Staff.name == staff_name)
            ).first()
            if member is None:
                raise SystemExit(f"No existe '{staff_name}' en {business.name}")
            staff_id = member.id
        if role == "staff" and staff_id is None:
            raise SystemExit("Un usuario con rol 'staff' necesita --profesional")
        user = s.scalars(select(User).where(User.username == username.lower())).first()
        if user is None:
            user = User(business_id=business.id, username=username.lower())
            s.add(user)
        user.password_hash = hash_password(password)
        user.role = role
        user.staff_id = staff_id
        user.display_name = display or username
        user.active = True


def backup(dest_dir: Path, keep: int = 14) -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise SystemExit("El respaldo automático solo aplica a SQLite (en PostgreSQL usa pg_dump)")
    src = url.removeprefix("sqlite:///")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"citas-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(src) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    old = sorted(dest_dir.glob("citas-*.db"))[:-keep]
    for f in old:
        f.unlink()
    return dest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("cargar-config", help="Crear/actualizar un negocio desde un archivo TOML")
    p.add_argument("archivo", type=Path)

    p = sub.add_parser("crear-usuario", help="Crear o actualizar un usuario del panel")
    p.add_argument("--negocio", required=True)
    p.add_argument("--usuario", required=True)
    p.add_argument("--rol", choices=["owner", "staff"], default="owner")
    p.add_argument("--profesional", help="Nombre del profesional (para rol staff)")
    p.add_argument("--nombre", default="")

    p = sub.add_parser("simular", help="Probar el bot en la terminal, sin WhatsApp")
    p.add_argument("--negocio", required=True)
    p.add_argument("--telefono", default="5215500000000")
    p.add_argument("--nombre", default="Cliente Prueba")

    sub.add_parser("recordatorios", help="Enviar ahora los recordatorios pendientes")

    p = sub.add_parser("respaldo", help="Copia de seguridad de la base SQLite")
    p.add_argument("--carpeta", type=Path, default=Path("backups"))
    p.add_argument("--conservar", type=int, default=14)

    args = parser.parse_args(argv)
    init_db(get_settings().database_url)

    if args.cmd == "cargar-config":
        slug = load_config(args.archivo)
        print(f"Negocio '{slug}' cargado.")
    elif args.cmd == "crear-usuario":
        password = os.environ.get("PANEL_PASSWORD") or getpass.getpass("Contraseña: ")
        if len(password) < 8:
            raise SystemExit("La contraseña debe tener al menos 8 caracteres")
        create_user(args.negocio, args.usuario, args.rol, args.profesional, password, args.nombre)
        print(f"Usuario '{args.usuario}' listo.")
    elif args.cmd == "simular":
        from app.simulator import run

        run(args.negocio, args.telefono, args.nombre)
    elif args.cmd == "recordatorios":
        from app.reminders import run_reminders

        print(f"Recordatorios enviados: {run_reminders(datetime.now(timezone.utc))}")
    elif args.cmd == "respaldo":
        print(f"Respaldo: {backup(args.carpeta, args.conservar)}")


if __name__ == "__main__":
    main(sys.argv[1:])
