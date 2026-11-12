"""Control remoto por Telegram: le escribes al bot y el agente obedece.

Qué se puede hacer desde el chat: ver el estado, pausar, reanudar, cerrar posiciones y aprobar
sugerencias. Qué NO se puede: cambiar la configuración (límites de riesgo, cuenta demo/real,
activos). Eso solo se cambia en el servidor, a propósito: si alguien entrara a tu Telegram,
podría como mucho pausar el agente o cerrar posiciones, no subir el riesgo ni mover dinero.

Medidas de seguridad:
- Solo se obedece al chat configurado en TELEGRAM_CHAT_ID; cualquier otro se ignora en silencio.
- Cerrar posiciones pide confirmar con un código de un solo uso que vence en 2 minutos.
- Un comando que llegó mientras el agente estaba apagado (más de 10 minutos) no se ejecuta.
- Cada comando se ejecuta como mucho una vez, aunque el agente se reinicie a la mitad.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import httpx

from bot_trader.config import Settings
from bot_trader.db import Journal, from_iso, iso, now_utc
from bot_trader.engine import Bot, build_bot
from bot_trader.notify import send_telegram
from bot_trader.report import ACCOUNT_LABEL, analysis_line, journal_text, status_text

log = logging.getLogger(__name__)

POLL_SECONDS = 25
STALE_AFTER = timedelta(minutes=10)
CONFIRM_TTL = timedelta(minutes=2)

# (comando, descripción): también es el menú que Telegram muestra al escribir "/"
COMMANDS = [
    ("estado", "Capital, posiciones y frenos"),
    ("analisis", "Qué ve hoy en cada activo"),
    ("diario", "Últimas señales, órdenes y cierres"),
    ("pausar", "No abrir posiciones nuevas"),
    ("reanudar", "Volver a operar"),
    ("cerrar", "Cerrar una posición: /cerrar SPY"),
    ("cerrar_todo", "Cerrar todas las posiciones y pausar"),
    ("aprobar", "Ejecutar una sugerencia: /aprobar 12"),
    ("ayuda", "Lista de comandos"),
]


class TelegramChannel:
    """Lo mínimo de la API de bots de Telegram: recibir mensajes y contestar."""

    def __init__(self, token: str, chat_id: str, http: httpx.Client | None = None):
        self.token = token
        self.chat_id = str(chat_id)
        self.http = http or httpx.Client(timeout=POLL_SECONDS + 15)

    def _call(self, method: str, payload: dict) -> dict:
        response = self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
        response.raise_for_status()
        return response.json()

    def poll(self, offset: int | None, timeout: int = POLL_SECONDS) -> list[dict]:
        """Espera hasta `timeout` segundos a que llegue algún mensaje."""
        payload: dict = {"timeout": max(int(timeout), 0), "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return list(self._call("getUpdates", payload).get("result") or [])

    def send(self, text: str) -> bool:
        return send_telegram(self.token, self.chat_id, text, http=self.http)

    def register_commands(self) -> None:
        self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})


class Remote:
    def __init__(
        self,
        load_settings: Callable[[], Settings],
        channel: TelegramChannel,
        clock: Callable[[], datetime] = now_utc,
        make_bot: Callable[[Settings], Bot] = build_bot,
    ):
        self.load_settings = load_settings
        self.channel = channel
        self.clock = clock
        self.make_bot = make_bot

    # --- estado guardado en la bitácora ----------------------------------------------------

    def _journal(self) -> Journal:
        s = self.load_settings()
        return Journal(s.db_path, s.account)

    def _get(self, key: str) -> str | None:
        journal = self._journal()
        try:
            return journal.get(key)
        finally:
            journal.close()

    def _set(self, key: str, value: str) -> None:
        journal = self._journal()
        try:
            journal.set(key, value)
        finally:
            journal.close()

    # --- recepción -------------------------------------------------------------------------

    def serve(self, seconds: float) -> None:
        """Atiende comandos durante `seconds` segundos (se usa entre un ciclo y el siguiente)."""
        deadline = time.monotonic() + seconds
        first = True
        failures = 0
        while (remaining := deadline - time.monotonic()) > 0:
            if not first and remaining < 1:  # no vale la pena otra consulta por menos de un segundo
                time.sleep(remaining)
                break
            first = False
            try:
                raw = self._get("telegram_offset")
                updates = self.channel.poll(int(raw) if raw else None, timeout=min(POLL_SECONDS, int(remaining)))
            except Exception as exc:  # Telegram caído o sin red: el agente sigue con lo suyo
                failures += 1
                if failures == 1:
                    log.warning("No pude leer comandos de Telegram (%s); sigo intentando.", type(exc).__name__)
                wait = 15.0 if failures == 1 else 60.0
                time.sleep(min(wait, max(deadline - time.monotonic(), 0.0)))
                continue
            failures = 0
            for update in updates:
                self.process(update)

    def process(self, update: dict) -> None:
        # Primero se marca como recibido: si algo falla a la mitad, el comando NO se repite.
        if "update_id" in update:
            self._set("telegram_offset", str(int(update["update_id"]) + 1))
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = str((message.get("chat") or {}).get("id", ""))
        if not text:
            return
        if chat != self.channel.chat_id:
            log.warning("Mensaje de Telegram ignorado: viene de un chat no autorizado.")
            return
        sent = datetime.fromtimestamp(message.get("date", 0), tz=timezone.utc) if message.get("date") else self.clock()
        age = self.clock() - sent
        if age > STALE_AFTER:
            self.channel.send(
                f"Ignoré «{text[:40]}» porque llegó hace {int(age.total_seconds() // 60)} min, mientras el agente "
                "estaba apagado. Envíalo de nuevo si todavía lo quieres."
            )
            return
        try:
            reply = self.handle(text)
        except Exception as exc:
            log.exception("Falló el comando de Telegram %r", text[:40])
            reply = f"No pude ejecutar «{text[:40]}»: {exc}"
        self.channel.send(reply)

    # --- comandos --------------------------------------------------------------------------

    def handle(self, text: str) -> str:
        """Ejecuta un comando y devuelve la respuesta."""
        if not text.startswith("/"):
            return "Escríbeme un comando. " + self.help_text()
        parts = text.split()
        command = parts[0][1:].split("@")[0].lower()
        args = parts[1:]
        handler = getattr(self, f"cmd_{command}", None)
        if command == "start" or handler is None:
            prefix = "" if command in ("start", "ayuda", "help") else f"No conozco /{command}. "
            return prefix + self.help_text()
        return handler(args)

    def help_text(self) -> str:
        lines = [f"/{c} — {d}" for c, d in COMMANDS]
        return "Comandos:\n" + "\n".join(lines)

    def cmd_ayuda(self, args: list[str]) -> str:
        return self.help_text()

    cmd_help = cmd_ayuda

    def _bot(self) -> Bot:
        return self.make_bot(self.load_settings())

    def cmd_estado(self, args: list[str]) -> str:
        return status_text(self._bot())

    def cmd_diario(self, args: list[str]) -> str:
        return journal_text(self._bot(), 8)

    def cmd_analisis(self, args: list[str]) -> str:
        bot = self._bot()
        candles, errors, reference = bot.fetch_market()
        account = bot.broker.account({s: b[-1].close for s, b in candles.items()})
        analyst = bot.build_analyst(candles, reference)
        news = bot.read_news([a for a in bot.s.assets if a.symbol in candles])
        out = []
        if analyst.ref is not None:
            regime = analyst.market_regime(analyst.ref_dates[-1])
            out.append(f"Mercado: {regime.label} ({regime.detail})")
        out += [analysis_line(a) for a in bot.analyze(analyst, errors, account.positions, news)]
        out.append("Es solo una consulta: no se abrió ni se cerró nada.")
        return "\n".join(out)

    def cmd_pausar(self, args: list[str]) -> str:
        bot = self._bot()
        bot.risk.pause(" ".join(args) or "pausa manual desde Telegram")
        return "Agente en pausa: no abrirá posiciones nuevas. Los stops siguen puestos. /reanudar para seguir."

    def cmd_reanudar(self, args: list[str]) -> str:
        bot = self._bot()
        was = bot.risk.pause_reason
        candles, _ = bot.fetch_candles(bot.s.assets)
        account = bot.broker.account({s: b[-1].close for s, b in candles.items()})
        equity, _ = bot.bot_equity(account)
        bot.risk.resume(equity)
        note = f" Estaba en pausa por: {was}." if was else ""
        return f"Agente reanudado.{note} El máximo para medir la caída se reinicia en {equity:,.2f} USD."

    def cmd_aprobar(self, args: list[str]) -> str:
        if not args or not args[0].lstrip("#").isdigit():
            return "Dime cuál: /aprobar 12 (el número de la sugerencia)."
        return self._bot().approve(int(args[0].lstrip("#")))

    # --- cerrar posiciones: siempre con confirmación ---------------------------------------

    def _ask_confirmation(self, action: str, arg: str, what: str) -> str:
        code = f"{secrets.randbelow(9000) + 1000}"
        pending = {"code": code, "action": action, "arg": arg, "expires": iso(self.clock() + CONFIRM_TTL)}
        self._set("telegram_confirmacion", json.dumps(pending))
        account = ACCOUNT_LABEL[self.load_settings().account]
        return f"{what} en la cuenta {account}.\nPara confirmar envía: /confirmar {code}\n(vale 2 minutos)"

    def cmd_cerrar(self, args: list[str]) -> str:
        bot = self._bot()
        held = bot.open_position_symbols()
        if not args:
            return "Dime cuál: /cerrar SPY. Posiciones abiertas: " + (", ".join(held) or "ninguna") + "."
        symbol = args[0].upper()
        if symbol not in held:
            return f"El bot no tiene posición abierta en {symbol}. Abiertas: " + (", ".join(held) or "ninguna") + "."
        return self._ask_confirmation("cerrar", symbol, f"Voy a cerrar la posición de {symbol}")

    def cmd_cerrar_todo(self, args: list[str]) -> str:
        held = self._bot().open_position_symbols()
        if not held:
            return "El bot no tiene posiciones abiertas."
        what = f"Voy a cerrar las {len(held)} posiciones del bot ({', '.join(held)}) y dejarlo en pausa"
        return self._ask_confirmation("cerrar_todo", "", what)

    def cmd_confirmar(self, args: list[str]) -> str:
        raw = self._get("telegram_confirmacion")
        self._set("telegram_confirmacion", "")  # un solo intento, acierte o no
        if not raw:
            return "No hay nada pendiente de confirmar."
        pending = json.loads(raw)
        if self.clock() > from_iso(pending["expires"]):
            return "El código venció. Vuelve a enviar el comando."
        if not args or args[0] != pending["code"]:
            return "Código incorrecto: cancelé la operación. Vuelve a enviar el comando si la quieres."
        bot = self._bot()
        if pending["action"] == "cerrar":
            lines = bot.close_all("cierre pedido desde Telegram", symbol=pending["arg"])
            return "\n".join(lines) or f"Ya no había posición en {pending['arg']}."
        lines = bot.close_all("cierre total pedido desde Telegram")
        bot.risk.pause("cerrar_todo desde Telegram")
        return "\n".join(lines + ["Agente en pausa. /reanudar para volver a operar."])


def make_remote(load_settings: Callable[[], Settings]) -> Remote | None:
    """El control remoto, o None si Telegram no está configurado."""
    s = load_settings()
    if not s.telegram_token or not s.telegram_chat_id:
        return None
    return Remote(load_settings, TelegramChannel(s.telegram_token, s.telegram_chat_id))
