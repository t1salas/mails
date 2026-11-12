from dataclasses import replace
from datetime import timedelta

import pytest

from bot_trader.analysis.probability import ProbabilityModel, trade_outcome_r, wilson_lower
from bot_trader.analysis.trend import compute_features
from bot_trader.config import RiskParams, StrategyParams
from bot_trader.models import Candle, NewsView, Position
from bot_trader.strategy import evaluate
from tests.conftest import START, make_series, steady_trend

SP = StrategyParams()


def test_trend_score_sign():
    up = compute_features(make_series(1, 0.002, 0.01), SP)
    down = compute_features(make_series(2, -0.002, 0.01), SP)
    assert up.score[-1] > 0.3
    assert down.score[-1] < -0.3
    assert up.warmup >= SP.slow_sma - 1


def test_outcome_stop_first_and_gaps():
    # Vela de entrada y luego una vela que toca stop y objetivo: cuenta el stop.
    bars = [Candle(START, 100, 101, 99, 100)] * 30
    bars = list(bars)
    feats = compute_features(bars, SP)
    i = 20
    atr = feats.atr[i]
    assert atr == pytest.approx(2.0)
    wide = Candle(START, 100, 100 + 10 * atr, 100 - 10 * atr, 100)
    feats.open[i + 1], feats.high[i + 1], feats.low[i + 1] = wide.open, wide.high, wide.low
    params = replace(SP, horizon=5)
    assert trade_outcome_r(feats, i, True, params, 0.0) == pytest.approx(-1.0)
    # Hueco a la baja más allá del stop: se sale a la apertura, peor que -1R
    feats.open[i + 1], feats.high[i + 1], feats.low[i + 1] = 90, 91, 89
    assert trade_outcome_r(feats, i, True, params, 0.0) == pytest.approx((90 - 100) / (2 * atr))
    # Sin datos suficientes hacia adelante: no hay resultado
    assert trade_outcome_r(feats, len(bars) - 2, True, params, 0.0) is None


def test_probability_has_no_lookahead():
    bars = make_series(5, 0.001, 0.015, n=700)
    feats = compute_features(bars, SP)
    model = ProbabilityModel(feats, SP, 0.0015)
    t = 500
    before = model.at(t)
    # Cambiar radicalmente el futuro no debe cambiar la estimación en t
    future = bars[: t + 1] + [Candle(c.date, c.open * 3, c.high * 3, c.low * 3, c.close * 3) for c in bars[t + 1 :]]
    feats2 = compute_features(future, SP)
    after = ProbabilityModel(feats2, SP, 0.0015).at(t)
    assert before is not None and after is not None
    assert before == after


def test_probability_favors_uptrend():
    model = ProbabilityModel(compute_features(steady_trend(), SP), SP, 0.0015)
    p = model.at(599)
    assert p is not None
    assert p.p_win > 0.6 and p.expectancy_r > 0.3 and p.n >= 30


def test_wilson_lower_bound():
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(50, 100) < 0.5
    assert wilson_lower(9, 10) < 0.9


def _evaluate(feats, news=None, position=None, risk=None):
    t = len(feats) - 1
    prob = ProbabilityModel(feats, SP, 0.0015).at(t)
    return evaluate("X", feats, t, prob, None, news, position, SP, risk or RiskParams())


def test_strategy_buys_clean_uptrend_with_levels():
    feats = compute_features(steady_trend(), SP)
    sig = _evaluate(feats)
    assert sig.action == "COMPRAR", sig.reasons
    assert sig.stop < sig.price < sig.target
    assert sig.target - sig.price == pytest.approx(2 * (sig.price - sig.stop))


def test_strategy_waits_in_downtrend_and_without_history():
    down = compute_features(make_series(2, -0.002, 0.01), SP)
    assert _evaluate(down).action == "ESPERAR"
    short_history = compute_features(steady_trend(n=150), SP)
    sig = _evaluate(short_history)
    assert sig.action == "ESPERAR" and "insuficientes" in sig.reasons[0]


def test_bad_news_vetoes_buy_and_closes_position():
    feats = compute_features(steady_trend(), SP)
    bad = NewsView("X", score=-0.8, impact="alto", risk_event=True, summary="investigación de la SEC", method="ia")
    sig = _evaluate(feats, news=bad)
    assert sig.action == "ESPERAR"
    assert any("evento de riesgo" in r for r in sig.reasons)
    pos = Position("1", "X", True, 1000, 1, 100, 90, 120, START)
    assert _evaluate(feats, news=bad, position=pos).action == "CERRAR"
    assert _evaluate(feats, position=pos).action == "MANTENER"


def test_good_news_alone_does_not_trigger_buy():
    down = compute_features(make_series(2, -0.002, 0.01), SP)
    great = NewsView("X", score=1.0, impact="alto", risk_event=False, summary="", method="ia")
    assert _evaluate(down, news=great).action == "ESPERAR"


def test_broken_trend_closes_long():
    bars = steady_trend(n=600)
    crash = []
    price = bars[-1].close
    for k in range(60):
        o = price
        price = price * 0.985
        crash.append(Candle(bars[-1].date + timedelta(days=k + 1), o, o * 1.002, price * 0.998, price))
    feats = compute_features(bars + crash, SP)
    pos = Position("1", "X", True, 1000, 1, 100, 1, 10_000, START)
    assert _evaluate(feats, position=pos).action == "CERRAR"
