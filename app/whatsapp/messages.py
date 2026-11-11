"""Mensajes salientes y entrantes de WhatsApp, independientes del transporte.

El bot produce objetos de este módulo; el cliente HTTP los convierte al JSON de la
Cloud API de Meta. Los límites de longitud son los de Meta (títulos de botón 20
caracteres, filas de lista 24, etc.); se recortan aquí para no recibir errores 400.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class Text:
    to: str
    body: str

    kind = "text"

    def to_payload(self) -> dict[str, Any]:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.to,
            "type": "text",
            "text": {"body": clip(self.body, 4096), "preview_url": False},
        }


@dataclass
class Buttons:
    """Hasta 3 botones de respuesta rápida."""

    to: str
    body: str
    buttons: list[tuple[str, str]]  # (id, título)
    footer: str | None = None

    kind = "buttons"

    def to_payload(self) -> dict[str, Any]:
        if not 1 <= len(self.buttons) <= 3:
            raise ValueError("WhatsApp admite entre 1 y 3 botones")
        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": clip(self.body, 1024)},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": clip(title, 20)}}
                    for bid, title in self.buttons
                ]
            },
        }
        if self.footer:
            interactive["footer"] = {"text": clip(self.footer, 60)}
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.to,
            "type": "interactive",
            "interactive": interactive,
        }


@dataclass
class Row:
    id: str
    title: str
    description: str = ""


@dataclass
class ListMessage:
    """Lista desplegable: hasta 10 filas en total."""

    to: str
    body: str
    button: str
    rows: list[Row]
    section_title: str = "Opciones"

    kind = "list"

    def to_payload(self) -> dict[str, Any]:
        if not 1 <= len(self.rows) <= 10:
            raise ValueError("WhatsApp admite entre 1 y 10 filas por lista")
        rows = []
        for r in self.rows:
            row = {"id": r.id, "title": clip(r.title, 24)}
            if r.description:
                row["description"] = clip(r.description, 72)
            rows.append(row)
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": clip(self.body, 1024)},
                "action": {
                    "button": clip(self.button, 20),
                    "sections": [{"title": clip(self.section_title, 24), "rows": rows}],
                },
            },
        }


@dataclass
class Template:
    """Plantilla aprobada por Meta (necesaria fuera de la ventana de 24 h)."""

    to: str
    name: str
    lang: str
    body_params: list[str] = field(default_factory=list)
    quick_reply_payloads: list[str] = field(default_factory=list)

    kind = "template"

    def to_payload(self) -> dict[str, Any]:
        components: list[dict[str, Any]] = []
        if self.body_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in self.body_params],
                }
            )
        for i, payload in enumerate(self.quick_reply_payloads):
            components.append(
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": str(i),
                    "parameters": [{"type": "payload", "payload": payload}],
                }
            )
        template: dict[str, Any] = {"name": self.name, "language": {"code": self.lang}}
        if components:
            template["components"] = components
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self.to,
            "type": "template",
            "template": template,
        }


Outbound = Text | Buttons | ListMessage | Template


@dataclass
class Inbound:
    """Mensaje recibido, ya normalizado."""

    wa_message_id: str
    from_phone: str
    timestamp: datetime
    kind: str  # text | choice | unsupported
    text: str = ""
    choice_id: str = ""  # id del botón / fila de lista / payload de plantilla
    choice_title: str = ""
    profile_name: str = ""
    raw_type: str = ""
