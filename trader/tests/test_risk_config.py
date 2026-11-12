from datetime import date

import pytest

from bot_trader.config import ConfigError, RiskParams, load_settings
from bot_trader.db import Journal
from bot_trader.models import Position
from bot_trader.risk import RiskState, check_guards, position_size
from tests.conftest import START


def pos(symbol, amount, leverage=1):
    return Position(symbol, symbol, True, amount, 1, 100, 90, 120, START, leverage=leverage)


def test_size_from_risk_per_trade():
    # 10 000 x 1 % / 5 % de distancia al stop = 2 000
    d = position_size(10_000, 10_000, [], 100, 95, "A", RiskParams(max_asset_exposure=0.5), False)
    assert d.ok and d.amount == pytest.approx(2000)
    assert "100.00 USD" in d.reason


def test_size_caps():
    rp = RiskParams()
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
