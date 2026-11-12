"""Probabilidades empíricas: "¿qué pasó las otras veces que el activo estuvo así?"

Para cada vela pasada se simula la misma operación que haría el bot (entrar al cierre con un
stop a `stop_atr` ATR y un objetivo a `objetivo_atr` ATR, salir a más tardar en `horizonte`
velas) y se anota el resultado en múltiplos de R (1R = lo arriesgado hasta el stop), ya con
costos. Luego, para la situación actual (tendencia + RSI), se cuentan los casos parecidos.

Reglas para no engañarse:

- Sin mirar el futuro: en la vela `t` solo cuentan operaciones que ya terminaron (`i + horizonte <= t`).
- Si el stop y el objetivo caen en la misma vela, se asume que primero saltó el stop.
- Si el precio abre con hueco más allá del stop, se sale al precio de apertura (peor que el stop).
- Con pocos casos parecidos, la estimación se "encoge" hacia el promedio general del activo
  (contracción bayesiana), para no creerle a 5 casos con suerte.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

from bot_trader.analysis.trend import Features
from bot_trader.config import StrategyParams
from bot_trader.models import Probability

PRIOR_STRENGTH = 20  # cuántos "casos imaginarios" del promedio general se suman
MIN_BASE_SAMPLES = 30


def score_bucket(score: float) -> int:
    if score > 0.5:
        return 2
    if score > 0.2:
        return 1
    if score >= -0.2:
        return 0
    if score >= -0.5:
        return -1
    return -2


def rsi_bucket(rsi: float) -> int:
    if rsi < 35:
        return 0
    if rsi <= 65:
        return 1
    return 2


def state_at(feats: Features, i: int) -> tuple[int, int] | None:
    s, r = feats.score[i], feats.rsi[i]
    if s is None or r is None:
        return None
    return score_bucket(s), rsi_bucket(r)


def trade_levels(price: float, atr: float, is_buy: bool, params: StrategyParams) -> tuple[float, float]:
    """(stop, objetivo) absolutos para una entrada a `price`."""
    if is_buy:
        return price - params.stop_atr * atr, price + params.target_atr * atr
    return price + params.stop_atr * atr, price - params.target_atr * atr


def trade_outcome_r(
    feats: Features, i: int, is_buy: bool, params: StrategyParams, cost_per_side: float
) -> float | None:
    """Resultado en R de entrar en la vela `i`. None si no hay datos para resolverla."""
    a = feats.atr[i]
    n = len(feats)
    if a is None or a <= 0 or i + params.horizon >= n:
        return None
    entry = feats.close[i]
    risk = params.stop_atr * a
    stop, target = trade_levels(entry, a, is_buy, params)
    exit_price = feats.close[i + params.horizon]
    for j in range(i + 1, i + params.horizon + 1):
        o, h, low = feats.open[j], feats.high[j], feats.low[j]
        if is_buy:
            if o <= stop:
                exit_price = o
                break
            if low <= stop:
                exit_price = stop
                break
            if o >= target:
                exit_price = o
                break
            if h >= target:
                exit_price = target
                break
        else:
            if o >= stop:
                exit_price = o
                break
            if h >= stop:
                exit_price = stop
                break
            if o <= target:
                exit_price = o
                break
            if low <= target:
                exit_price = target
                break
    pnl = (exit_price - entry) if is_buy else (entry - exit_price)
    costs = 2 * cost_per_side * entry
    return (pnl - costs) / risk


def wilson_lower(wins: float, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max((center - margin) / denom, 0.0)


@dataclass
class _Bucket:
    idx: list[int]
    cum_wins: list[int]
    cum_r: list[float]

    def upto(self, cutoff: int) -> tuple[int, int, float]:
        k = bisect_right(self.idx, cutoff)
        if k == 0:
            return 0, 0, 0.0
        return k, self.cum_wins[k - 1], self.cum_r[k - 1]


def _bucket(points: list[tuple[int, float]]) -> _Bucket:
    idx, wins, rs = [], [], []
    w, r = 0, 0.0
    for i, outcome in points:
        w += 1 if outcome > 0 else 0
        r += outcome
        idx.append(i)
        wins.append(w)
        rs.append(r)
    return _Bucket(idx, wins, rs)


class ProbabilityModel:
    """Modelo de probabilidad para un lado (compra o venta en corto) de un activo."""

    def __init__(self, feats: Features, params: StrategyParams, cost_per_side: float, is_buy: bool = True):
        self.feats = feats
        self.params = params
        self.is_buy = is_buy
        self.states = [state_at(feats, i) for i in range(len(feats))]
        by_state: dict[tuple[int, int], list[tuple[int, float]]] = {}
        everything: list[tuple[int, float]] = []
        for i, state in enumerate(self.states):
            if state is None:
                continue
            outcome = trade_outcome_r(feats, i, is_buy, params, cost_per_side)
            if outcome is None:
                continue
            by_state.setdefault(state, []).append((i, outcome))
            everything.append((i, outcome))
        self._by_state = {s: _bucket(points) for s, points in by_state.items()}
        self._all = _bucket(everything)

    def at(self, t: int) -> Probability | None:
        """Estimación para una entrada en la vela `t`, usando solo operaciones ya cerradas."""
        state = self.states[t] if 0 <= t < len(self.states) else None
        if state is None:
            return None
        cutoff = t - self.params.horizon
        n_all, wins_all, r_all = self._all.upto(cutoff)
        if n_all < MIN_BASE_SAMPLES:
            return None
        base_p, base_r = wins_all / n_all, r_all / n_all
        bucket = self._by_state.get(state)
        n, wins, r_sum = bucket.upto(cutoff) if bucket else (0, 0, 0.0)
        p = (wins + PRIOR_STRENGTH * base_p) / (n + PRIOR_STRENGTH)
        exp_r = (r_sum + PRIOR_STRENGTH * base_r) / (n + PRIOR_STRENGTH)
        return Probability(
            p_win=p,
            expectancy_r=exp_r,
            n=n,
            base_p_win=base_p,
            base_expectancy_r=base_r,
            p_low=wilson_lower(wins, n),
        )
