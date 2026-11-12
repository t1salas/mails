import json
import re
from datetime import timedelta

import httpx
import pytest

from bot_trader.db import Journal, now_utc
from bot_trader.remote import COMMANDS, Remote, TelegramChannel, make_remote
from tests.conftest import make_series, steady_trend
from tests.test_engine import make_bot

CHAT = "4242"


class FakeTelegram:
    """Servidor falso de Telegram: entrega los mensajes en cola y guarda lo que el bot contesta."""

    def __init__(self):
        self.queue: list[dict] = []
        self.sent: list[str] = []
        self.commands: list[dict] = []
        self.next_id = 100
        self.fail = False

    def message(self, text: str, chat: str = CHAT, when=None) -> dict:
        self.next_id += 1
        update = {
            "update_id": self.next_id,
            "message": {"text": text, "chat": {"id": int(chat)}, "date": int((when or now_utc()).timestamp())},
        }
        self.queue.append(update)
        return update

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(502)
        body = json.loads(request.content or b"{}")
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            offset = body.get("offset", 0)
            pending = [u for u in self.queue if u["update_id"] >= offset]
            self.queue = pending  # pedir con offset confirma los anteriores
            return httpx.Response(200, json={"ok": True, "result": pending})
        if method == "sendMessage":
            assert str(body["chat_id"]) == CHAT
            self.sent.append(body["text"])
            return httpx.Response(200, json={"ok": True})
        if method == "setMyCommands":
            self.commands = body["commands"]
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def data():
    return {"UP": steady_trend(), "DOWN": make_series(2, -0.002, 0.01)}


@pytest.fixture
def remote(tg, settings, data):
    channel = TelegramChannel("token", CHAT, http=httpx.Client(transport=httpx.MockTransport(tg.handler)))
    clock = {"now": None}
    r = Remote(lambda: settings, channel, clock=lambda: clock["now"] or now_utc(), make_bot=lambda s: make_bot(s, data))
    r.test_clock = clock
    return r


def say(remote, tg, text, **kw) -> str:
    """Envía un mensaje como el usuario y devuelve lo que contestó el bot."""
    before = len(tg.sent)
    remote.process(tg.message(text, **kw))
    return "\n".join(tg.sent[before:])


def paused(settings) -> bool:
    return Journal(settings.db_path, settings.account).get("pausado", "0") == "1"


def open_symbols(settings) -> list[str]:
    return [p.symbol for p in Journal(settings.db_path, settings.account).open_positions()]


def buy_up(settings, data):
    settings.mode = "autonomo"
    make_bot(settings, data).run_cycle()
    assert open_symbols(settings) == ["UP"]


def test_help_and_unknown_commands(remote, tg):
    reply = say(remote, tg, "/ayuda")
    assert all(f"/{c}" in reply for c, _ in COMMANDS)
    assert "No conozco /volar" in say(remote, tg, "/volar")
    assert "Comandos" in say(remote, tg, "hola")
    assert "Capital del bot" in say(remote, tg, "/ESTADO@MiBotTrader")  # como llega desde un grupo


def test_only_the_configured_chat_is_obeyed(remote, tg, settings):
    assert say(remote, tg, "/pausar", chat="999") == ""
    assert not paused(settings)


def test_status_pause_and_resume(remote, tg, settings):
    status = say(remote, tg, "/estado")
    assert "PAPEL" in status and "Frenos: OK" in status and "Posiciones del bot (0)" in status
    assert "en pausa" in say(remote, tg, "/pausar revisando")
    assert paused(settings)
    assert "revisando" in say(remote, tg, "/estado")
    reply = say(remote, tg, "/reanudar")
    assert "reanudado" in reply and "revisando" in reply and not paused(settings)


def test_analysis_and_journal_do_not_trade(remote, tg, settings):
    reply = say(remote, tg, "/analisis")
    assert "UP" in reply and "DOWN" in reply and "no se abrió ni se cerró nada" in reply
    assert "Señales recientes" in say(remote, tg, "/diario")
    assert open_symbols(settings) == []


def test_close_all_needs_the_confirmation_code(remote, tg, settings, data):
    buy_up(settings, data)
    ask = say(remote, tg, "/cerrar_todo")
    code = re.search(r"/confirmar (\d{4})", ask).group(1)
    assert "UP" in ask and "PAPEL" in ask and open_symbols(settings) == ["UP"]  # todavía no cierra nada
    wrong = "0000" if code != "0000" else "1111"
    assert "incorrecto" in say(remote, tg, f"/confirmar {wrong}")
    assert "No hay nada pendiente" in say(remote, tg, f"/confirmar {code}")  # un solo intento
    assert open_symbols(settings) == ["UP"]
    code = re.search(r"/confirmar (\d{4})", say(remote, tg, "/cerrar_todo")).group(1)
    done = say(remote, tg, f"/confirmar {code}")
    assert "UP: cerrada" in done and "en pausa" in done
    assert open_symbols(settings) == [] and paused(settings)
    assert "no tiene posiciones" in say(remote, tg, "/cerrar_todo")


def test_confirmation_expires(remote, tg, settings, data):
    buy_up(settings, data)
    code = re.search(r"/confirmar (\d{4})", say(remote, tg, "/cerrar_todo")).group(1)
    remote.test_clock["now"] = now_utc() + timedelta(minutes=3)
    late = tg.message(f"/confirmar {code}", when=remote.test_clock["now"])
    remote.process(late)
    assert "venció" in tg.sent[-1] and open_symbols(settings) == ["UP"]


def test_close_one_position(remote, tg, settings, data):
    buy_up(settings, data)
    assert "no tiene posición abierta en DOWN" in say(remote, tg, "/cerrar down")
    assert "Posiciones abiertas: UP" in say(remote, tg, "/cerrar")
    code = re.search(r"/confirmar (\d{4})", say(remote, tg, "/cerrar up")).group(1)
    assert "UP: cerrada" in say(remote, tg, f"/confirmar {code}")
    assert open_symbols(settings) == [] and not paused(settings)


def test_approve_a_suggestion(remote, tg, settings, data):
    result = make_bot(settings, data).run_cycle()  # modo sugerir
    sid = next(a.signal_id for a in result.actions if a.kind == "sugerida")
    assert "Dime cuál" in say(remote, tg, "/aprobar")
    assert say(remote, tg, f"/aprobar {sid}").startswith("EJECUTADA")
    assert open_symbols(settings) == ["UP"]


def test_stale_command_is_not_executed(remote, tg, settings):
    reply = say(remote, tg, "/pausar", when=now_utc() - timedelta(minutes=45))
    assert "Ignoré" in reply and "45 min" in reply and not paused(settings)


def test_a_failing_command_is_never_repeated(remote, tg, settings):
    def boom(_settings):
        raise RuntimeError("eToro no responde")

    remote.make_bot = boom
    update = tg.message("/estado")
    remote.process(update)
    assert "No pude ejecutar" in tg.sent[-1] and "eToro no responde" in tg.sent[-1]
    offset = Journal(settings.db_path, settings.account).get("telegram_offset")
    assert offset == str(update["update_id"] + 1)  # quedó marcado como recibido antes de ejecutarse


def test_serve_answers_between_cycles_and_survives_outages(remote, tg, settings):
    tg.message("/pausar")
    remote.serve(0.05)
    assert paused(settings) and any("en pausa" in t for t in tg.sent)
    remote.serve(0.05)
    assert sum("en pausa" in t for t in tg.sent) == 1  # el mismo mensaje no se atiende dos veces
    tg.fail = True
    remote.serve(0.05)  # Telegram caído: no revienta


def test_register_commands_and_factory(remote, tg, settings):
    remote.channel.register_commands()
    assert [c["command"] for c in tg.commands] == [c for c, _ in COMMANDS]
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", c["command"]) for c in tg.commands)
    assert make_remote(lambda: settings) is None  # sin token no hay control remoto
    settings.telegram_token, settings.telegram_chat_id = "t", CHAT
    assert isinstance(make_remote(lambda: settings), Remote)
