"""Tendencia: un puntaje de -1 (bajista fuerte) a +1 (alcista fuerte) por vela.

El puntaje junta cinco lecturas que usa cualquier trader de tendencia:

1. ¿El precio está arriba o abajo de la media rápida (50)?
2. ¿La media rápida está arriba o abajo de la lenta (200)? (cruce dorado / de la muerte)
3. ¿Hacia dónde apunta la media rápida en las últimas 20 velas? (pendiente)
4. Momentum de ~6 meses (126 velas), medido contra la volatilidad del activo.
5. Histograma MACD: ¿el impulso de corto plazo acompaña?

La pendiente y el momentum se dividen entre la volatilidad: subir 10 % en un activo que se
mueve 1 % al día no es lo mismo que en uno que se mueve 5 % al día.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from bot_trader.analysis import indicators as ind
from bot_trader.config import StrategyParams
from bot_trader.models import Candle

SLOPE_BARS = 20
MOMENTUM_BARS = 126
VOL_BARS = 60
WEIGHTS = (0.25, 0.25, 0.20, 0.20, 0.10)


@dataclass
class Features:
    """Indicadores de una serie completa de velas, alineados por índice."""

    candles: list[Candle]
    close: list[float]
    high: list[float]
    low: list[float]
    open: list[float]
    sma_fast: ind.Series
    sma_slow: ind.Series
    rsi: ind.Series
    atr: ind.Series
    macd_hist: ind.Series
    vol: ind.Series  # desviación diaria de rendimientos logarítmicos
    score: ind.Series
    warmup: int  # primer índice con puntaje válido

    def __len__(self) -> int:
        return len(self.close)


def _component_score(
    i: int,
    close: Sequence[float],
    sma_fast: ind.Series,
    sma_slow: ind.Series,
    macd_hist: ind.Series,
    vol: ind.Series,
) -> float | None:
    f, s, v, h = sma_fast[i], sma_slow[i], vol[i], macd_hist[i]
    if f is None or s is None or v is None or h is None or i < max(SLOPE_BARS, MOMENTUM_BARS):
        return None
    f_prev = sma_fast[i - SLOPE_BARS]
    if f_prev is None or f_prev <= 0:
        return None
    v = max(v, 1e-4)
    above_fast = 1.0 if close[i] > f else -1.0
    golden = 1.0 if f > s else -1.0
    slope = math.tanh(math.log(f / f_prev) / (v * math.sqrt(SLOPE_BARS)) * 2)
    momentum = math.tanh(math.log(close[i] / close[i - MOMENTUM_BARS]) / (v * math.sqrt(MOMENTUM_BARS)))
    macd = 1.0 if h > 0 else -1.0
    parts = (above_fast, golden, slope, momentum, macd)
    return sum(w * p for w, p in zip(WEIGHTS, parts))


def compute_features(candles: Sequence[Candle], params: StrategyParams) -> Features:
    candles = list(candles)
    close = [c.close for c in candles]
    high = [c.high for c in candles]
    low = [c.low for c in candles]
    opens = [c.open for c in candles]
    sma_fast = ind.sma(close, params.fast_sma)
    sma_slow = ind.sma(close, params.slow_sma)
    macd_hist = ind.macd_histogram(close)
    vol = ind.rolling_std(ind.log_returns(close), VOL_BARS)
    score: ind.Series = [
        _component_score(i, close, sma_fast, sma_slow, macd_hist, vol) for i in range(len(close))
    ]
    warmup = next((i for i, v in enumerate(score) if v is not None), len(close))
    return Features(
        candles=candles,
        close=close,
        high=high,
        low=low,
        open=opens,
        sma_fast=sma_fast,
        sma_slow=sma_slow,
        rsi=ind.rsi(close, params.rsi_period),
        atr=ind.atr(high, low, close, params.atr_period),
        macd_hist=macd_hist,
        vol=vol,
        score=score,
        warmup=warmup,
    )


def trend_label(score: float) -> str:
    if score >= 0.5:
        return "alcista fuerte"
    if score >= 0.2:
        return "alcista"
    if score <= -0.5:
        return "bajista fuerte"
    if score <= -0.2:
        return "bajista"
    return "lateral"


def describe(feats: Features, i: int) -> list[str]:
    """Explica en palabras qué ve el análisis técnico en la vela `i`."""
    out = []
    c, f, s = feats.close[i], feats.sma_fast[i], feats.sma_slow[i]
    if f is not None:
        pct = (c / f - 1) * 100
        out.append(f"precio {'arriba' if c > f else 'abajo'} de la media rápida ({pct:+.1f} %)")
    if f is not None and s is not None:
        out.append("media rápida sobre la lenta (tendencia de fondo alcista)" if f > s
                   else "media rápida bajo la lenta (tendencia de fondo bajista)")
    if i >= MOMENTUM_BARS:
        mom = (c / feats.close[i - MOMENTUM_BARS] - 1) * 100
        out.append(f"momentum 6 meses {mom:+.1f} %")
    r = feats.rsi[i]
    if r is not None:
        tag = " (sobrecompra)" if r >= 70 else " (sobreventa)" if r <= 30 else ""
        out.append(f"RSI {r:.0f}{tag}")
    return out
