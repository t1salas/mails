from datetime import date

import pytest

from bot_trader.config import ConfigError, RiskParams, load_settings
from bot_trader.db import Journal
from bot_trader.models import Position
from bot_trader.models import Probability, Regime, Signal
from bot_trader.risk import RiskState, TradeContext, check_guards, position_size, risk_budget, size_trade
from tests.conftest import START


def pos(symbol, amount, leverage=1):
    return Position(symbol, symbol, True, amount, 1, 100, 90, 120, START, leverage=leverage)


def test_size_from_risk_per_trade():
    # 10 000 x 1 % / 5 % de distancia al stop = 2 000
    d = position_size(10_000, 10_000, [], 100, 95, "A", RiskParams(risk_per_trade=0.01, max_asset_exposure=0.5), False)
    assert d.ok and d.amount == pytest.approx(2000)
    assert "100.00 USD" in d.reason


def test_size_caps():
    rp = RiskParams(risk_per_trade=0.01, max_positions=5)
    d = position_size(10_000, 10_000, [], 100, 99, "A", rp, False)  # stop muy cerca -> tope por activo
    assert d.amount == pytest.approx(2000) and "máximo por activo" in d.reason
    d = position_size(10_000, 300, [], 100, 95, "A", rp, False)
    assert d.amount == pytest.approx(300) and "efectivo" in d.reason
    d = position_size(10_000, 10_000, [], 100, 95, "A", rp, True)
    assert d.amount == pytest.approx(250) and "cuenta real" in d.reason
    full = [pos(s, 1500) for s in "BCDEF"]
    assert not position_size(10_000, 10_000, full, 100, 95, "A", rp, False).ok
    near_full = [pos(s, 1950) for s in "BCDE"]  # 7 800 usados de 8 000
    d = position_size(10_000, 10_000, near_full, 100, 95, "A", rp, False)
    assert d.amount == pytest.approx(200) and "exposición total" in d.reason
    assert not position_size(10_000, 10_000, [pos("A", 100)], 100, 95, "A", rp, False).ok
    tiny = position_size(1_000, 1_000, [], 100, 50, "A", rp, False)
    assert not tiny.ok and "mínimo" in tiny.reason


def test_guards():
    rp = RiskParams()
    assert check_guards(10_000, 10_000, 10_000, False, rp).can_open
    daily = check_guards(9_600, 10_000, 9_950, False, rp)
    assert not daily.can_open and not daily.paused
    dd = check_guards(8_400, 10_000, 8_400, False, rp)
    assert not dd.can_open and dd.paused
    assert check_guards(10_000, 10_000, 10_000, True, rp).paused


def test_risk_state_persists(tmp_path):
    j = Journal(tmp_path / "x.db", "papel")
    rs = RiskState(j)
    assert rs.update(10_000, date(2026, 1, 5)) == (10_000, 10_000)
    assert rs.update(10_500, date(2026, 1, 5)) == (10_500, 10_000)
    assert rs.update(10_200, date(2026, 1, 6)) == (10_500, 10_200)
    rs.pause("prueba")
    assert RiskState(Journal(tmp_path / "x.db", "papel")).paused
    assert not RiskState(Journal(tmp_path / "x.db", "demo")).paused  # cada cuenta por separado
    rs.resume(9_000)
    assert not rs.paused and rs.update(9_000, date(2026, 1, 6))[0] == 9_000


def test_load_settings_spanish_keys(tmp_path):
    cfg = tmp_path / "e.toml"
    cfg.write_text(
        """
[general]
cuenta = "papel"
modo = "autonomo"
[riesgo]
riesgo_por_operacion = 0.005
max_posiciones = 3
[estrategia]
objetivo_atr = 3
[[activos]]
simbolo = "aapl"
nombre = "Apple"
[[activos]]
simbolo = "BTC"
simbolo_yahoo = "BTC-USD"
""",
        encoding="utf-8",
    )
    s = load_settings(cfg)
    s.validate()
    assert s.mode == "autonomo" and s.risk.risk_per_trade == 0.005 and s.risk.max_positions == 3
    assert s.strategy.target_atr == 3.0
    assert s.assets[0].symbol == "AAPL" and s.assets[0].news_query == "Apple AAPL stock"
    assert s.assets[1].yahoo_symbol == "BTC-USD" and s.assets[1].etoro_symbol == "BTC"
    assert s.db_path.startswith(str(tmp_path))


@pytest.mark.parametrize(
    "body, message",
    [
        ('[riesgo]\nriesgo_por_operacoin = 0.01', "desconocida"),
        ('[riesgo]\nmax_posiciones = "tres"', "tipo inválido"),
        ('[general]\ncuenta = "real"\n[[activos]]\nsimbolo="A"', "ETORO_API_KEY"),
        ('[riesgo]\nriesgo_por_operacion = 0.2\n[[activos]]\nsimbolo="A"', "riesgo_por_operacion"),
        ('[estrategia]\npeso_noticias = 0.9\n[[activos]]\nsimbolo="A"', "peso_noticias"),
    ],
)
def test_config_errors(tmp_path, body, message):
    cfg = tmp_path / "e.toml"
    cfg.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_settings(cfg).validate()


def test_real_account_needs_explicit_confirmation(tmp_path, monkeypatch):
    cfg = tmp_path / "e.toml"
    cfg.write_text('[general]\ncuenta = "real"\n[[activos]]\nsimbolo="A"', encoding="utf-8")
    monkeypatch.setenv("ETORO_API_KEY", "k")
    monkeypatch.setenv("ETORO_USER_KEY", "u")
    with pytest.raises(ConfigError, match="DINERO REAL"):
        load_settings(cfg).validate()
    monkeypatch.setenv("ETORO_CONFIRMO_DINERO_REAL", "si")
    load_settings(cfg).validate()


def _prob(p=0.5, win=2.5, loss=1.0):
    return Probability(p, p * win - (1 - p) * loss, 100, 0.4, 0.2, 0.4, avg_win_r=win, avg_loss_r=loss)


def test_risk_budget_adjustments():
    rp = RiskParams(risk_per_trade=0.01, kelly_fraction=0)
    assert risk_budget(rp, TradeContext(), None) == (0.01, [])
    risk, notes = risk_budget(rp, TradeContext(drawdown=0.075), None)  # mitad del drawdown máximo
    assert risk == pytest.approx(0.005) and "bajo su máximo" in notes[0]
    defensive = Regime("defensivo", -0.3, 0.5, applies=True)
    assert risk_budget(rp, TradeContext(regime=defensive), None)[0] == pytest.approx(0.005)
    assert risk_budget(rp, TradeContext(regime=Regime("defensivo", -0.3, 0.5, applies=False)), None)[0] == 0.01
    risk, notes = risk_budget(rp, TradeContext(correlations={"SPY": 0.75}), None)
    assert risk == pytest.approx(0.005) and "SPY" in notes[0]
    risk, notes = risk_budget(rp, TradeContext(correlations={"SPY": 0.9}), None)
    assert risk == 0 and "casi la misma apuesta" in notes[0]


def test_kelly_caps_and_vetoes():
    rp = RiskParams(risk_per_trade=0.02, kelly_fraction=0.25)
    weak = _prob(0.30, win=2.5, loss=1.0)  # ventaja 0.05R -> Kelly 0.02 -> 1/4 = 0.5 %
    risk, notes = risk_budget(rp, TradeContext(), weak)
    assert risk == pytest.approx(0.25 * weak.kelly) and "Kelly" in notes[0]
    assert risk_budget(rp, TradeContext(), _prob(0.2, 2.5, 1.0))[0] == 0  # sin ventaja, no se arriesga


def test_volatility_target_shrinks_volatile_assets():
    rp = RiskParams(risk_per_trade=0.01, kelly_fraction=0, max_positions=4, vol_target=0.12)
    sig = Signal("X", "COMPRAR", 100, stop=95, probability=_prob(0.5))
    calm = size_trade(10_000, 10_000, [], sig, rp, TradeContext(annual_vol=0.15), False)
    wild = size_trade(10_000, 10_000, [], sig, rp, TradeContext(annual_vol=0.60), False)
    assert calm.amount == pytest.approx(2000)  # 1 % de riesgo con stop a 5 % (y tope por activo)
    assert wild.amount == pytest.approx(10_000 * 0.12 / (0.60 * 2))  # 1 000: la volatilidad manda
    assert "volatilidad objetivo" in wild.reason


def test_old_journal_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "viejo.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE posiciones (id TEXT NOT NULL, cuenta TEXT NOT NULL, simbolo TEXT NOT NULL, "
        "instrument_id INTEGER, es_compra INTEGER NOT NULL, monto REAL NOT NULL, unidades REAL NOT NULL, "
        "apalancamiento INTEGER NOT NULL DEFAULT 1, precio_entrada REAL NOT NULL, stop REAL, objetivo REAL, "
        "abierta TEXT NOT NULL, cerrada TEXT, precio_salida REAL, pnl REAL, motivo_cierre TEXT, "
        "PRIMARY KEY (id, cuenta));"
        "INSERT INTO posiciones VALUES ('1','papel','SPY',NULL,1,100,1,1,100,90,120,'2026-01-01T00:00:00+00:00',"
        "NULL,NULL,NULL,NULL);"
    )
    conn.commit()
    conn.close()
    j = Journal(path, "papel")
    old = j.open_positions()[0]
    assert old.symbol == "SPY" and not old.trailing and j.initial_stop("1") == 90
    j.add_position(pos("QQQ", 100))
    assert len(j.open_positions()) == 2


def test_short_risk_rules():
    rp = RiskParams(risk_per_trade=0.01, kelly_fraction=0)
    crisis = Regime("crisis", -0.8, 0.0, applies=True)
    # En crisis no se compra lo que sigue al mercado, pero sí se puede vender en corto (a mitad de riesgo)
    assert risk_budget(rp, TradeContext(regime=crisis), None, is_buy=True)[0] == 0
    risk, notes = risk_budget(rp, TradeContext(regime=crisis), None, is_buy=False)
    assert risk == pytest.approx(0.005) and "venta en corto" in notes[0]


def test_bet_correlation_sign():
    from bot_trader.risk import bet_correlation

    assert bet_correlation(0.9, same_side=True) == 0.9  # dos compras parecidas: misma apuesta
    assert bet_correlation(0.9, same_side=False) == -0.9  # compra + corto de algo parecido: cobertura
    rp = RiskParams(risk_per_trade=0.01, kelly_fraction=0)
    hedge = TradeContext(correlations={"SPY": bet_correlation(0.92, False)})
    assert risk_budget(rp, hedge, None, is_buy=False)[0] == pytest.approx(0.005)  # no se bloquea
    duplicate = TradeContext(correlations={"TLT": bet_correlation(-0.9, False)})  # corto de lo contrario a TLT
    assert risk_budget(rp, duplicate, None, is_buy=False)[0] == 0


def test_short_exposure_cap():
    rp = RiskParams(risk_per_trade=0.02, kelly_fraction=0, vol_target=0, max_short_exposure=0.30)
    sig = Signal("X", "VENDER_CORTO", 100, is_buy=False, stop=105, probability=_prob(0.5))
    shorts = [Position("B", "B", False, 2500, 1, 100, 110, None, START)]
    d = size_trade(10_000, 10_000, shorts, sig, rp, TradeContext(), False)
    assert d.ok and d.amount == pytest.approx(500) and "ventas en corto" in d.reason
    longs = [pos("B", 2500)]
    assert size_trade(10_000, 10_000, longs, sig, rp, TradeContext(), False).amount == pytest.approx(2000)
