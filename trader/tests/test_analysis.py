from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

from bot_trader.analysis.analyst import Analyst
from bot_trader.analysis.features import FEATURE_NAMES, feature_rows
from bot_trader.analysis.model import Dataset, LogisticModel
from bot_trader.analysis.probability import ProbabilityModel, simulate_trade, wilson_lower
from bot_trader.analysis.trend import compute_features
from bot_trader.config import Asset, RiskParams, Settings, StrategyParams
from bot_trader.models import Candle, NewsView, Position, Probability, Regime
from bot_trader.strategy import evaluate
from tests.conftest import START, make_series, steady_trend
from tests.synthetic import universe

SP = StrategyParams()
FIXED = replace(SP, exit_style="objetivo", stop_atr=2.0, target_atr=4.0, horizon=5)


def flat(n=30):
    return [Candle(START + timedelta(days=i), 100, 101, 99, 100) for i in range(n)]


def test_trend_score_sign():
    up = compute_features(make_series(1, 0.002, 0.01), SP)
    down = compute_features(make_series(2, -0.002, 0.01), SP)
    assert up.score[-1] > 0.3
    assert down.score[-1] < -0.3
    assert up.warmup >= SP.slow_sma - 1


def test_fixed_exit_stop_first_and_gaps():
    feats = compute_features(flat(), SP)
    i = 20
    atr = feats.atr[i]
    assert atr == pytest.approx(2.0)
    # Una vela que toca stop y objetivo: cuenta el stop
    feats.open[i + 1], feats.high[i + 1], feats.low[i + 1] = 100, 100 + 10 * atr, 100 - 10 * atr
    out = simulate_trade(feats, i, True, FIXED, 0.0)
    assert out.r == pytest.approx(-1.0) and out.resolved == i + 1
    # Hueco a la baja más allá del stop: se sale a la apertura, peor que -1R
    feats.open[i + 1], feats.high[i + 1], feats.low[i + 1] = 90, 91, 89
    assert simulate_trade(feats, i, True, FIXED, 0.0).r == pytest.approx((90 - 100) / (2 * atr))
    # Sin datos suficientes hacia adelante y sin salida: no se sabe todavía
    assert simulate_trade(feats, len(feats) - 3, True, FIXED, 0.0) is None


def test_trailing_stop_lets_winners_run():
    bars = flat(30)
    # Sube 10 velas y luego cae fuerte: el stop móvil asegura casi toda la subida
    price = 100.0
    for k in range(10):
        price += 3
        bars.append(Candle(bars[-1].date + timedelta(days=1), price - 3, price + 0.5, price - 3.5, price))
    bars.append(Candle(bars[-1].date + timedelta(days=1), price, price, price - 20, price - 19))
    bars += [Candle(bars[-1].date + timedelta(days=1 + k), 111, 112, 110, 111) for k in range(5)]
    feats = compute_features(bars, SP)
    params = replace(SP, stop_atr=2.0, horizon=30)
    i = 29
    out = simulate_trade(feats, i, True, params, 0.0)
    assert out is not None and out.resolved == 40
    assert out.r > 4  # ganó varias veces lo arriesgado: así paga muchas pérdidas chicas


def test_probability_has_no_lookahead():
    bars = make_series(5, 0.001, 0.015, n=700)
    feats = compute_features(bars, SP)
    model = ProbabilityModel(feats, SP, 0.0015)
    t = 500
    before = model.at(t)
    future = bars[: t + 1] + [Candle(c.date, c.open * 3, c.high * 3, c.low * 3, c.close * 3) for c in bars[t + 1 :]]
    after = ProbabilityModel(compute_features(future, SP), SP, 0.0015).at(t)
    assert before is not None and before == after


def test_probability_favors_uptrend():
    p = ProbabilityModel(compute_features(steady_trend(), SP), SP, 0.0015).at(599)
    assert p is not None
    assert p.p_win > 0.6 and p.expectancy_r > 0.3 and p.n >= 30
    assert p.avg_win_r > 0 and p.avg_loss_r > 0 and p.kelly > 0


def test_wilson_lower_bound():
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(50, 100) < 0.5
    assert wilson_lower(9, 10) < 0.9


def test_features_are_bounded():
    feats = compute_features(make_series(3, 0.001, 0.02, n=400), SP)
    rows = feature_rows(feats)
    valid = [r for r in rows if r is not None]
    assert valid and all(len(r) == len(FEATURE_NAMES) for r in valid)
    assert all(-1.0 <= v <= 1.0 for r in valid for v in r)
    assert rows[0] is None


def test_logistic_model_learns_and_respects_dates():
    rng = np.random.default_rng(0)
    data = Dataset()
    for k in range(600):
        x = rng.normal(size=3)
        won = rng.random() < 1 / (1 + np.exp(-2 * x[0]))
        data.add(date(2020, 1, 1) + timedelta(days=k), list(x), won)
    assert data.fit_until(date(2020, 1, 1) + timedelta(days=100)) is None  # muy pocos casos aún
    model = data.fit_until(date(2030, 1, 1))
    assert isinstance(model, LogisticModel) and model.n == 600
    assert model.weights()[0] > 0.5 and abs(model.weights()[1]) < 0.3
    assert model.predict([2, 0, 0]) > 0.8 > 0.2 > model.predict([-2, 0, 0])


def prob(p=0.6, exp=0.5, n=100, p_hist=None, p_model=None):
    return Probability(p, exp, n, 0.5, 0.2, 0.4, p_hist=p if p_hist is None else p_hist, p_model=p_model,
                       avg_win_r=2.5, avg_loss_r=1.0)


def _evaluate(feats, news=None, position=None, risk=None, probability="auto", regime=None):
    t = len(feats) - 1
    p = ProbabilityModel(feats, SP, 0.0015).at(t) if probability == "auto" else probability
    return evaluate("X", feats, t, p, None, news, position, SP, risk or RiskParams(), regime=regime)


def test_strategy_buys_clean_uptrend_with_trailing_stop():
    feats = compute_features(steady_trend(), SP)
    sig = _evaluate(feats)
    assert sig.action == "COMPRAR", sig.reasons
    assert sig.trailing and sig.target is None
    assert sig.stop == pytest.approx(sig.price - SP.stop_atr * feats.atr[-1])
    assert any("stop móvil" in r for r in sig.reasons)


def test_strategy_waits_in_downtrend_and_without_history():
    assert _evaluate(compute_features(make_series(2, -0.002, 0.01), SP)).action == "ESPERAR"
    sig = _evaluate(compute_features(steady_trend(n=150), SP))
    assert sig.action == "ESPERAR" and "insuficientes" in sig.reasons[0]


def test_models_disagreement_blocks_entry():
    feats = compute_features(steady_trend(), SP)
    sig = _evaluate(feats, probability=prob(p_hist=0.75, p_model=0.40))
    assert sig.action == "ESPERAR" and any("no coinciden" in r for r in sig.reasons)
    assert _evaluate(feats, probability=prob(p_hist=0.62, p_model=0.58)).action == "COMPRAR"


def test_crisis_blocks_only_assets_that_follow_the_market():
    feats = compute_features(steady_trend(), SP)
    crisis = Regime("crisis", -0.6, 0.0, applies=True, detail="SPY bajo su media de 200")
    sig = _evaluate(feats, probability=prob(), regime=crisis)
    assert sig.action == "ESPERAR" and any("crisis" in r for r in sig.reasons)
    independent = replace(crisis, applies=False)
    assert _evaluate(feats, probability=prob(), regime=independent).action == "COMPRAR"


def test_bad_news_vetoes_buy_and_closes_position():
    feats = compute_features(steady_trend(), SP)
    bad = NewsView("X", score=-0.8, impact="alto", risk_event=True, summary="investigación de la SEC", method="ia")
    sig = _evaluate(feats, news=bad)
    assert sig.action == "ESPERAR" and any("evento de riesgo" in r for r in sig.reasons)
    pos = Position("1", "X", True, 1000, 1, 100, 90, None, START, trailing=True, trail_distance=10)
    assert _evaluate(feats, news=bad, position=pos).action == "CERRAR"
    assert _evaluate(feats, position=pos).action == "MANTENER"


def test_good_news_alone_does_not_trigger_buy():
    down = compute_features(make_series(2, -0.002, 0.01), SP)
    great = NewsView("X", score=1.0, impact="alto", risk_event=False, summary="", method="ia")
    assert _evaluate(down, news=great).action == "ESPERAR"


def test_broken_trend_closes_long():
    bars = steady_trend(n=600)
    price = bars[-1].close
    for k in range(60):
        o = price
        price *= 0.985
        bars.append(Candle(bars[-1].date + timedelta(days=1), o, o * 1.002, price * 0.998, price))
    pos = Position("1", "X", True, 1000, 1, 100, 1, None, START)
    assert _evaluate(compute_features(bars, SP), position=pos).action == "CERRAR"


@pytest.fixture(scope="module")
def analyst():
    data = universe(900)
    s = Settings(assets=[Asset(k) for k in data])
    a = Analyst(data, s)
    a.train(max(a.dates["SPY"]))
    return a


def test_analyst_combines_and_calibrates(analyst):
    t = len(analyst.feats["QQQ"]) - 1
    p = analyst.probability("QQQ", t, True)
    assert p is not None and p.p_model is not None and p.calibrated
    assert 0 < p.p_win < 1
    assert p.expectancy_r == pytest.approx(p.p_win * p.avg_win_r - (1 - p.p_win) * p.avg_loss_r)
    sig = analyst.evaluate("QQQ", t, None, None)
    assert sig.regime is not None and sig.regime.label in ("alcista", "neutral", "defensivo", "crisis")


def test_analyst_correlations_and_regime_scope(analyst):
    d = analyst.dates["SPY"][-1]
    assert analyst.correlation("SPY", "SPY", d) == pytest.approx(1.0)
    assert analyst.correlation("SPY", "QQQ", d) > 0.7  # se mueven con el mismo mercado
    assert abs(analyst.correlation("SPY", "GLD", d)) < 0.5
    assert analyst.regime_for("QQQ", d).applies
    assert not analyst.regime_for("GLD", d).applies  # el oro va por su lado
    assert analyst.annual_vol("BTC", len(analyst.feats["BTC"]) - 1) > analyst.annual_vol("SPY", len(analyst.feats["SPY"]) - 1)
