from dataclasses import replace
from datetime import timedelta

import pytest

from bot_trader.analysis.analyst import Analyst
from bot_trader.analysis.trend import compute_features
from bot_trader.backtest import compare_variants
from bot_trader.config import Asset, ConfigError, RiskParams, Settings, StrategyParams, load_settings
from bot_trader.models import Candle, NewsView, Position, Probability, Signal
from bot_trader.report import variants_report
from bot_trader.risk import TradeContext, risk_budget, size_trade
from bot_trader.strategy import evaluate
from tests.conftest import START, make_series, steady_trend
from tests.synthetic import universe

CORE = StrategyParams(kind="nucleo")


def _core(feats, position=None, news=None):
    t = len(feats) - 1
    return evaluate("X", feats, t, None, None, news, position, CORE, RiskParams())


def test_core_buys_above_the_200_day_average_with_protective_stop():
    feats = compute_features(steady_trend(), CORE)
    sig = _core(feats)
    assert sig.action == "COMPRAR" and sig.is_buy and not sig.trailing
    assert sig.stop == pytest.approx(sig.price * (1 - CORE.core_stop))
    assert any("media de 200" in r for r in sig.reasons)
    assert _core(compute_features(make_series(2, -0.002, 0.01), CORE)).action == "ESPERAR"
    assert "insuficientes" in _core(compute_features(steady_trend(n=150), CORE)).reasons[0]


def test_core_exits_only_below_the_margin():
    bars = steady_trend(n=600)
    pos = Position("1", "X", True, 1000, 1, 100, 80, None, START)
    assert _core(compute_features(bars, CORE), position=pos).action == "MANTENER"
    price = bars[-1].close
    for k in range(80):  # caída sostenida hasta quedar bajo la media
        o = price
        price *= 0.985
        bars.append(Candle(bars[-1].date + timedelta(days=1), o, o, price, price))
    assert _core(compute_features(bars, CORE), position=pos).action == "CERRAR"


def test_core_news_can_only_veto():
    feats = compute_features(steady_trend(), CORE)
    bad = NewsView("X", score=-0.7, impact="alto", risk_event=True, summary="fraude contable", method="ia")
    sig = _core(feats, news=bad)
    assert sig.action == "ESPERAR" and "evento de riesgo" in sig.reasons[0]


def test_agent_without_probability_filter():
    feats = compute_features(steady_trend(), StrategyParams())
    t = len(feats) - 1
    sig = evaluate("X", feats, t, None, None, None, None, StrategyParams(), RiskParams(use_probability=False))
    assert sig.action == "COMPRAR"  # sin historia de probabilidades igual entra por tendencia
    blocked = evaluate("X", feats, t, None, None, None, None, StrategyParams(), RiskParams())
    assert blocked.action == "ESPERAR"
    weak = Probability(0.2, -0.3, 100, 0.3, 0.0, 0.1, avg_win_r=2.0, avg_loss_r=1.0)
    assert risk_budget(RiskParams(), TradeContext(), weak)[0] == 0  # con filtro, Kelly veta
    assert risk_budget(RiskParams(use_probability=False), TradeContext(), weak)[0] > 0


def test_core_sizing_follows_target_weight():
    rp = RiskParams(max_asset_exposure=0.20)
    sig = Signal("X", "COMPRAR", 100, stop=80)
    d = size_trade(10_000, 10_000, [], sig, rp, TradeContext(target_weight=0.12), False)
    assert d.ok and d.amount == pytest.approx(1200) and "volatilidad" in d.reason
    capped = size_trade(10_000, 10_000, [], sig, rp, TradeContext(target_weight=0.5), False)
    assert capped.amount == pytest.approx(2000)
    assert size_trade(10_000, 300, [], sig, rp, TradeContext(target_weight=0.12), False).amount == pytest.approx(300)
    held = [Position("X", "X", True, 1000, 1, 100, 80, None, START)]
    assert not size_trade(10_000, 10_000, held, sig, rp, TradeContext(target_weight=0.12), False).ok


def test_core_weights_are_inverse_to_volatility():
    data = universe(600)
    s = Settings(assets=[Asset(k) for k in data])
    a = Analyst(data, s)
    d = a.dates["SPY"][-1]
    weights = {sym: a.core_weight(sym, d) for sym in data}
    assert sum(weights.values()) == pytest.approx(s.risk.max_total_exposure)
    assert weights["SPY"] > weights["BTC"]  # lo más volátil recibe menos


def test_compare_variants_and_report():
    data = {k: v for k, v in universe(900).items() if k in ("SPY", "QQQ", "GLD")}
    s = Settings(assets=[Asset(k) for k in data])
    results = compare_variants(s, data)
    labels = [label for label, _ in results]
    assert labels[0].startswith("agente (configuración actual)") and "núcleo: media de 200 días" in labels
    assert s.strategy.kind == "agente" and s.risk.use_probability  # la configuración original no cambia
    text = variants_report(results, 0.04)
    assert "comprar y mantener (referencia)" in text and "dinero sin riesgo" in text
    assert "Veredicto" in text or "Mejor por rendimiento" in text
    assert "ninguna versión rinde más" in variants_report(results, 5.0)  # con una vara imposible


def test_core_config_keys_and_validation(tmp_path):
    cfg = tmp_path / "e.toml"
    cfg.write_text('[estrategia]\ntipo = "nucleo"\nstop_nucleo = 0.15\n[riesgo]\nusar_probabilidad = false\n'
                   '[general]\nrendimiento_sin_riesgo = 0.035\n[[activos]]\nsimbolo = "SPY"\n', encoding="utf-8")
    s = load_settings(cfg)
    s.validate()
    assert s.strategy.kind == "nucleo" and s.strategy.core_stop == 0.15
    assert not s.risk.use_probability and s.cash_rate == 0.035
    s.risk.allow_short = True
    with pytest.raises(ConfigError, match="solo compra"):
        s.validate()
    s.risk.allow_short = False
    s.strategy = replace(s.strategy, kind="otro")
    with pytest.raises(ConfigError, match="tipo"):
        s.validate()
