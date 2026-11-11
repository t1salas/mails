"""Formato de fechas, precios y textos en español (sin depender del locale del servidor)."""

from __future__ import annotations

import unicodedata
from datetime import date, datetime

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
DIAS_CORTOS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
MESES_CORTOS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def fecha_larga(d: date | datetime) -> str:
    """'jueves 12 de noviembre'"""
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


def fecha_corta(d: date | datetime) -> str:
    """'jue 12 nov'"""
    return f"{DIAS_CORTOS[d.weekday()]} {d.day} {MESES_CORTOS[d.month - 1]}"


def hora(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def dia_relativo(d: date, hoy: date) -> str:
    """'Hoy · jue 12 nov', 'Mañana · vie 13 nov' o 'sáb 14 nov'."""
    delta = (d - hoy).days
    if delta == 0:
        return f"Hoy · {fecha_corta(d)}"
    if delta == 1:
        return f"Mañana · {fecha_corta(d)}"
    return fecha_corta(d)


def capitalizar(s: str) -> str:
    return s[:1].upper() + s[1:]


def precio(cents: int | None, simbolo: str = "$") -> str:
    if cents is None:
        return ""
    if cents % 100 == 0:
        return f"{simbolo}{cents // 100:,}"
    return f"{simbolo}{cents / 100:,.2f}"


def normalizar(texto: str) -> str:
    """Minúsculas y sin acentos, para reconocer palabras clave."""
    sin_acentos = unicodedata.normalize("NFKD", texto)
    sin_acentos = "".join(c for c in sin_acentos if not unicodedata.combining(c))
    return " ".join(sin_acentos.lower().split())


def primer_nombre(nombre: str) -> str:
    partes = nombre.strip().split()
    return partes[0] if partes else ""
