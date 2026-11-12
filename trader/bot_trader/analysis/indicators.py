"""Indicadores técnicos sobre series completas.

Cada función devuelve una lista del mismo largo que la entrada, con `None` donde todavía no
hay datos suficientes. El valor en la posición `i` solo usa datos hasta `i` (nunca del futuro),
así el backtest y el bot en vivo calculan exactamente lo mismo.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Series = list[float | None]


def sma(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """EMA sembrada con la SMA de los primeros `period` valores."""
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    """RSI de Wilder."""
    out: Series = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period

    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = value(avg_gain, avg_loss)
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    tr = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[0] - lows[0])
        else:
            prev = closes[i - 1]
            tr.append(max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev)))
    return tr


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Series:
    """ATR de Wilder: cuánto se mueve el precio en una vela "normal"."""
    out: Series = [None] * len(closes)
    tr = true_range(highs, lows, closes)
    if len(tr) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def macd_histogram(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Series:
    fast_ema, slow_ema = ema(closes, fast), ema(closes, slow)
    line = [
        (f - s) if f is not None and s is not None else None for f, s in zip(fast_ema, slow_ema)
    ]
    out: Series = [None] * len(closes)
    start = next((i for i, v in enumerate(line) if v is not None), None)
    if start is None:
        return out
    valid = [v for v in line[start:] if v is not None]
    sig = ema(valid, signal)
    for j, s in enumerate(sig):
        if s is not None:
            out[start + j] = valid[j] - s
    return out


def log_returns(closes: Sequence[float]) -> list[float]:
    """Rendimientos logarítmicos; el primero es 0."""
    out = [0.0]
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        out.append(math.log(cur / prev) if prev > 0 and cur > 0 else 0.0)
    return out


def rolling_std(values: Sequence[float], period: int) -> Series:
    """Desviación estándar móvil (muestral)."""
    out: Series = [None] * len(values)
    if period < 2:
        return out
    s = s2 = 0.0
    for i, v in enumerate(values):
        s += v
        s2 += v * v
        if i >= period:
            old = values[i - period]
            s -= old
            s2 -= old * old
        if i >= period - 1:
            mean = s / period
            var = max((s2 - period * mean * mean) / (period - 1), 0.0)
            out[i] = math.sqrt(var)
    return out


def max_drawdown(equity: Sequence[float]) -> float:
    """Peor caída desde un máximo, como fracción positiva (0.25 = -25 %)."""
    peak = -math.inf
    worst = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst
