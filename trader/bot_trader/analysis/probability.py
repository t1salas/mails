"""Probabilidades empíricas: "¿qué pasó las otras veces que el activo estuvo así?"

Para cada vela pasada se simula la MISMA operación que haría el bot y se anota el resultado en
múltiplos de R (1R = lo arriesgado hasta el stop), ya con costos. Hay dos estilos de salida:

- `seguimiento` (por defecto, el sostenible): stop inicial a `stop_atr` ATR que sube detrás del
  precio a distancia fija (stop móvil). Corta rápido lo que sale mal y deja correr lo que sale
  bien. Gana menos veces, pero las ganancias grandes pagan muchas pérdidas chicas.
- `objetivo`: stop y objetivo fijos.

Reglas para no engañarse:

- Sin mirar el futuro: en la vela `t` solo cuentan operaciones que ya se habían cerrado en `t`.
  (Las que siguen abiertas, que suelen ser las ganadoras largas, no cuentan: sesgo conservador.)
- Dentro de una vela se revisa primero el stop y después se mueve el stop con el máximo.
- Si el precio abre con hueco más allá del stop, se sale al precio de apertura (peor que el stop).
- Con pocos casos parecidos, la estimación se "encoge" hacia el promedio del activo
  (contracción bayesiana), para no creerle a cinco casos con suerte.
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


def trade_levels(
    price: float, atr: float, is_buy: bool, params: StrategyParams
) -> tuple[float, float | None]:
    """(stop, objetivo) absolutos para una entrada a `price`. Con stop móvil no hay objetivo."""
    dist = params.stop_atr * atr
    stop = price - dist if is_buy else price + dist
    if params.exit_style == "seguimiento":
        return stop, None
    t = params.target_atr * atr
    return stop, (price + t if is_buy else price - t)


@dataclass(frozen=True)
class Outcome:
    r: float  # resultado en R, con costos
    resolved: int  # índice de la vela en que terminó la operación


def simulate_trade(
    feats: Features, i: int, is_buy: bool, params: StrategyParams, cost_per_side: float
) -> Outcome | None:
    """Simula entrar al cierre de la vela `i`. None si todavía no terminó o faltan datos."""
    a = feats.atr[i]
    n = len(feats)
    if a is None or a <= 0 or i + 1 >= n:
        return None
    entry = feats.close[i]
    dist = params.stop_atr * a
    stop, target = trade_levels(entry, a, is_buy, params)
    trailing = params.exit_style == "seguimiento"
    last = i + params.horizon
    exit_price = None
    exit_index = None
    for j in range(i + 1, min(last, n - 1) + 1):
        o, h, low = feats.open[j], feats.high[j], feats.low[j]
        if is_buy:
            if o <= stop:
                exit_price = o
            elif low <= stop:
                exit_price = stop
            elif target is not None and o >= target:
                exit_price = o
            elif target is not None and h >= target:
                exit_price = target
            elif trailing:
                stop = max(stop, h - dist)
        else:
            if o >= stop:
                exit_price = o
            elif h >= stop:
                exit_price = stop
            elif target is not None and o <= target:
                exit_price = o
            elif target is not None and low <= target:
                exit_price = target
            elif trailing:
                stop = min(stop, low + dist)
        if exit_price is not None:
            exit_index = j
            break
    if exit_price is None:
        if last > n - 1:
            return None  # sigue abierta: todavía no se sabe cómo termina
        exit_price, exit_index = feats.close[last], last
    pnl = (exit_price - entry) if is_buy else (entry - exit_price)
    costs = 2 * cost_per_side * entry
    return Outcome((pnl - costs) / dist, exit_index)


def wilson_lower(wins: float, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max((center - margin) / denom, 0.0)


@dataclass
class _Stats:
    n: int = 0
    wins: int = 0
    sum_r: float = 0.0
    win_r: float = 0.0
    loss_r: float = 0.0


class _Bucket:
    """Resultados ordenados por la vela en que se resolvieron, con sumas acumuladas."""

    def __init__(self, points: list[tuple[int, float]]):
        points = sorted(points)
        self.resolved = [p[0] for p in points]
        self.cum: list[_Stats] = []
        acc = _Stats()
        for _, r in points:
            acc = _Stats(
                acc.n + 1,
                acc.wins + (1 if r > 0 else 0),
                acc.sum_r + r,
                acc.win_r + (r if r > 0 else 0.0),
                acc.loss_r + (-r if r <= 0 else 0.0),
            )
            self.cum.append(acc)

    def upto(self, t: int) -> _Stats:
        k = bisect_right(self.resolved, t)
        return self.cum[k - 1] if k else _Stats()


class ProbabilityModel:
    """Modelo de "casos parecidos" para un lado (compra o venta en corto) de un activo."""

    def __init__(self, feats: Features, params: StrategyParams, cost_per_side: float, is_buy: bool = True):
        self.feats = feats
        self.params = params
        self.is_buy = is_buy
        self.states = [state_at(feats, i) for i in range(len(feats))]
        self.outcomes: list[Outcome | None] = [
            simulate_trade(feats, i, is_buy, params, cost_per_side) if self.states[i] is not None else None
            for i in range(len(feats))
        ]
        by_state: dict[tuple[int, int], list[tuple[int, float]]] = {}
        everything: list[tuple[int, float]] = []
        for i, (state, out) in enumerate(zip(self.states, self.outcomes)):
            if state is None or out is None:
                continue
            by_state.setdefault(state, []).append((out.resolved, out.r))
            everything.append((out.resolved, out.r))
        self._by_state = {s: _Bucket(points) for s, points in by_state.items()}
        self._all = _Bucket(everything)

    def at(self, t: int) -> Probability | None:
        """Estimación para una entrada en la vela `t`, usando solo operaciones ya cerradas en `t`."""
        state = self.states[t] if 0 <= t < len(self.states) else None
        if state is None:
            return None
        base = self._all.upto(t)
        if base.n < MIN_BASE_SAMPLES:
            return None
        base_p, base_r = base.wins / base.n, base.sum_r / base.n
        bucket = self._by_state.get(state)
        s = bucket.upto(t) if bucket else _Stats()
        p = (s.wins + PRIOR_STRENGTH * base_p) / (s.n + PRIOR_STRENGTH)
        losses = base.n - base.wins
        avg_win = base.win_r / base.wins if base.wins else 0.0
        avg_loss = base.loss_r / losses if losses else 1.0
        return Probability(
            p_win=p,
            expectancy_r=p * avg_win - (1 - p) * avg_loss,
            n=s.n,
            base_p_win=base_p,
            base_expectancy_r=base_r,
            p_low=wilson_lower(s.wins, s.n),
            p_hist=p,
            avg_win_r=avg_win,
            avg_loss_r=avg_loss,
        )
