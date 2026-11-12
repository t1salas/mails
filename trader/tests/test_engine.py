from datetime import timedelta

import pytest

from bot_trader.backtest import run_backtest
from bot_trader.brokers import PaperBroker
from bot_trader.db import Journal, now_utc
from bot_trader.engine import Bot
from bot_trader.models import Account, Candle
from bot_trader.report import backtest_report, cycle_report
from bot_trader.sources import PriceSource
from tests.conftest import make_series, steady_trend


class FakePrices(PriceSource):
    name = "prueba"

    def __init__(self, data):
        self.data = data

    def candles(self, asset):
        if asset.symbol not in self.data:
            raise LookupError("no hay")
        return self.data[asset.symbol]


def make_bot(settings, data):
    journal = Journal(settings.db_path, settings.account)
    broker = PaperBroker(journal, settings.paper_capital, settings.cost_per_side)
    return Bot(settings, journal, broker, FakePrices(data))


@pytest.fixture
def data():
    return {"UP": steady_trend(), "DOWN": make_series(2, -0.002, 0.01)}


def test_suggest_mode_does_not_trade_and_dedupes(settings, data):
    bot = make_bot(settings, data)
    r1 = bot.run_cycle()
    sugg = [a for a in r1.actions if a.kind == "sugerida"]
    assert [(a.symbol, a.action) for a in sugg] == [("UP", "COMPRAR")]
    assert sugg[0].amount > 0 and not r1.positions
    assert r1.equity == pytest.approx(10_000)
    assert r1.noteworthy and r1.decision_round
    r2 = bot.run_cycle()
    assert [a.signal_id for a in r2.actions if a.kind == "sugerida"] == [sugg[0].signal_id]
    # Una sola decisión por día: el siguiente ciclo solo vigila y no repite avisos
    bot.s.decision_hour_utc = 0
    r3 = bot.run_cycle()
    assert not r3.decision_round and not r3.actions and not r3.noteworthy
    assert any("Ronda de vigilancia" in n for n in r3.notes)
    text = cycle_report(r1)
    assert "SUGERENCIA #" in text and "aprobar" in text


def test_approve_executes_suggestion(settings, data):
    bot = make_bot(settings, data)
    sid = next(a.signal_id for a in bot.run_cycle().actions if a.kind == "sugerida")
    msg = bot.approve(sid)
    assert msg.startswith("EJECUTADA"), msg
    positions = bot.journal.open_positions()
    assert [p.symbol for p in positions] == ["UP"]
    assert bot.journal.signal(sid)["estado"] == "ejecutada"
    assert "no se puede aprobar" in bot.approve(sid)


def test_approve_refuses_to_chase_price(settings, data):
    bot = make_bot(settings, data)
    sid = next(a.signal_id for a in bot.run_cycle().actions if a.kind == "sugerida")
    last = data["UP"][-1]
    jump = last.close * 1.05
    data["UP"] = data["UP"] + [Candle(last.date + timedelta(days=1), last.close, jump, last.close, jump)]
    assert "no persigo el precio" in bot.approve(sid)
    assert not bot.journal.open_positions()


def test_autonomous_opens_then_stop_and_cooldown(settings, data):
    settings.mode = "autonomo"
    bot = make_bot(settings, data)
    r = bot.run_cycle()
    assert [a.kind for a in r.actions] == ["ejecutada"]
    position = bot.journal.open_positions()[0]
    assert position.symbol == "UP" and position.stop < position.entry_price
    assert r.cash == pytest.approx(10_000 - position.amount)
    # Al día siguiente el precio rompe el stop
    last = data["UP"][-1]
    crash_low = position.stop * 0.98
    data["UP"] = data["UP"] + [
        Candle(now_utc() + timedelta(days=1), last.close, last.close, crash_low, crash_low * 1.01)
    ]
    r2 = bot.run_cycle()
    assert [e.reason for e in r2.exits] == ["stop móvil alcanzado"]
    assert r2.exits[0].pnl < 0
    # Pérdida acotada al ~1 % de riesgo + costos (el stop no tuvo hueco en la apertura)
    assert abs(r2.exits[0].pnl) < 10_000 * 0.01 * 1.3
    assert not bot.journal.open_positions()
    assert all(a.kind == "bloqueada" for a in r2.actions if a.symbol == "UP")


def test_pause_blocks_entries(settings, data):
    settings.mode = "autonomo"
    bot = make_bot(settings, data)
    bot.risk.pause("prueba")
    r = bot.run_cycle()
    assert r.guard.paused and not bot.journal.open_positions()
    assert [a.kind for a in r.actions] == ["bloqueada"]
    assert "FRENO DE RIESGO" in cycle_report(r)


def test_drawdown_pauses_bot(settings, data):
    bot = make_bot(settings, data)
    bot.journal.set("pico", "20000")
    r = bot.run_cycle()
    assert r.guard.paused and bot.risk.paused and "drawdown" in bot.risk.pause_reason


def test_bad_data_for_one_asset_does_not_stop_cycle(settings, data):
    del data["DOWN"]
    r = make_bot(settings, data).run_cycle()
    errors = [a for a in r.analyses if a.error]
    assert [a.asset.symbol for a in errors] == ["DOWN"]
    assert any(a.kind == "sugerida" for a in r.actions)


def test_bot_capital_cap(settings, data):
    settings.risk.bot_capital = 2_000
    bot = make_bot(settings, data)
    equity, cash = bot.bot_equity(Account(equity=50_000, cash=40_000))
    assert equity == 2_000 and cash == 2_000


def test_backtest_runs_and_reports(settings):
    data = {"UP": make_series(1, 0.0008, 0.015, n=900), "FLAT": make_series(3, 0.0, 0.015, n=900)}
    res = run_backtest(settings, data)
    assert res.start is not None and res.trades
    assert res.final > 0 and 0 <= res.max_dd < 1
    # Sin mirar el futuro: las fechas de entrada son posteriores al inicio de la medición
    assert all(t.entry_date >= res.start for t in res.trades)
    assert all(t.exit_date >= t.entry_date for t in res.trades)
    text = backtest_report(res, list(data))
    assert "Veredicto" in text and "Rendimiento anual" in text and "Calidad de las predicciones" in text
    assert "datos suficientes" in backtest_report(run_backtest(settings, {"X": make_series(1, 0, 0.01, n=100)}), ["X"])


class CountingNews:
    def __init__(self):
        self.calls = 0

    def headlines(self, asset, hours, limit):
        self.calls += 1
        from bot_trader.models import Headline

        return [Headline(f"{asset.name} shares plunge after SEC probe", "Wire", now_utc())]


def test_news_veto_and_cache(settings, data):
    settings.news.enabled = True
    bot = make_bot(settings, data)
    bot.news_source = CountingNews()
    r = bot.run_cycle()
    up = next(a for a in r.analyses if a.asset.symbol == "UP")
    assert up.signal.news.method == "lexico" and up.signal.news.risk_event
    assert up.signal.action == "ESPERAR"  # noticias malas con evento de riesgo vetan la compra
    assert not [a for a in r.actions if a.kind == "sugerida"]
    calls = bot.news_source.calls
    bot.run_cycle()
    assert bot.news_source.calls == calls  # segunda lectura sale del caché
    cached = bot.read_news(settings.assets)
    assert cached["UP"].headlines[0].published is not None


def test_suggestions_reserve_capital_between_them(settings):
    # Dos activos con la misma señal y poco efectivo: la segunda sugerencia no debe contar el mismo dinero
    settings.paper_capital = 2_500
    settings.risk.max_asset_exposure = 0.8
    settings.risk.max_total_exposure = 1.0
    settings.risk.risk_per_trade = 0.05
    settings.risk.vol_target = 0  # esta prueba mide solo la reserva de capital
    settings.risk.kelly_fraction = 0
    settings.risk.max_correlation = settings.risk.high_correlation = 1.01
    from bot_trader.config import Asset

    settings.assets = [Asset("UP"), Asset("UP2")]
    bot = make_bot(settings, {"UP": steady_trend(), "UP2": steady_trend(start=50)})
    r = bot.run_cycle()
    amounts = [a.amount for a in r.actions if a.kind == "sugerida"]
    assert len(amounts) == 2 and amounts[0] == pytest.approx(2_000)
    assert sum(amounts) == pytest.approx(2_500)


def test_correlated_duplicate_is_not_bought_twice(settings):
    from bot_trader.config import Asset

    settings.assets = [Asset("UP"), Asset("UP2")]
    bot = make_bot(settings, {"UP": steady_trend(), "UP2": steady_trend(start=50)})
    r = bot.run_cycle()
    assert [a.kind for a in r.actions] == ["sugerida", "bloqueada"]
    assert "se mueve casi igual" in r.actions[1].detail


def test_paper_trailing_stop_locks_in_profit(settings, data):
    settings.mode = "autonomo"
    bot = make_bot(settings, data)
    bot.run_cycle()
    p = bot.journal.open_positions()[0]
    assert p.trailing and p.target is None and p.trail_distance > 0
    # Sube 30 % en los días siguientes y después se desploma
    last = data["UP"][-1]
    high = last.close * 1.30
    data["UP"] = data["UP"] + [
        Candle(now_utc() + timedelta(days=1), last.close, high, last.close, high * 0.99),
        Candle(now_utc() + timedelta(days=2), high * 0.99, high * 0.99, high * 0.70, high * 0.71),
    ]
    r = bot.run_cycle()
    assert [e.reason for e in r.exits] == ["stop móvil alcanzado"]
    assert r.exits[0].pnl > 0  # el stop subió y aseguró ganancia
    assert r.exits[0].price == pytest.approx((high - p.trail_distance) * (1 - settings.cost_per_side))


def test_model_decay_pauses_agent(settings, data):
    from bot_trader.models import Position

    bot = make_bot(settings, data)
    for k in range(20):  # el modelo prometía 70 % y casi todo perdió
        pid = f"x{k}"
        bot.journal.add_position(Position(pid, "UP", True, 100, 1, 100, 95, None, now_utc()))
        bot.journal.close_position(pid, 95, -5 if k < 18 else 5, "prueba")
        from bot_trader.models import Signal

        sid = bot.journal.save_signal(
            Signal("UP", "COMPRAR", 100, probability=_prob(0.7)), 100, "ejecutada"
        )
        bot.journal.set_signal_state(sid, "ejecutada", pid)
    r = bot.run_cycle()
    assert r.newly_paused and bot.risk.paused and "desgaste" in bot.risk.pause_reason
    assert not r.guard.can_open


def _prob(p):
    from bot_trader.models import Probability

    return Probability(p, 0.5, 100, 0.5, 0.1, 0.4)
