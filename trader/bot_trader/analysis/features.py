"""Variables para el modelo predictivo, todas medidas contra la volatilidad del activo.

Medir contra la volatilidad permite juntar activos distintos en un solo modelo: subir 10 % en
seis meses es mucho para un bono y poco para Bitcoin. Cada variable queda entre -1 y 1.

Las variables recogen lo que la evidencia académica muestra que más se repite en los mercados:
el momentum de 3 a 12 meses (lo que viene subiendo tiende a seguir subiendo), la cercanía al
máximo de 52 semanas, el régimen de volatilidad y el estado del mercado en general.
"""

from __future__ import annotations

import math
from collections import deque

from bot_trader.analysis import indicators as ind
from bot_trader.analysis.trend import Features

FEATURE_NAMES = [
    "tendencia",
    "momentum 1 mes",
    "momentum 3 meses",
    "momentum 6 meses",
    "momentum 12 meses",
    "distancia al máximo de 52 semanas",
    "RSI",
    "volatilidad reciente vs. normal",
    "mercado general",
    "precio vs. media de 200",
]


def _rolling_max(values: list[float], period: int) -> list[float]:
    out, dq = [], deque()
    for i, v in enumerate(values):
        while dq and values[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - period:
            dq.popleft()
        out.append(values[dq[0]])
    return out


def _mom(close: list[float], i: int, bars: int, vol: float) -> float:
    j = max(i - bars, 0)
    if close[j] <= 0 or i == j:
        return 0.0
    return math.tanh(math.log(close[i] / close[j]) / (vol * math.sqrt(i - j)))


def feature_rows(feats: Features, market: list[float | None] | None = None) -> list[list[float] | None]:
    """Una fila de variables por vela (None mientras no haya datos suficientes)."""
    n = len(feats)
    close = feats.close
    rets = ind.log_returns(close)
    vol20 = ind.rolling_std(rets, 20)
    vol120 = ind.rolling_std(rets, 120)
    high252 = _rolling_max(feats.high, 252)
    rows: list[list[float] | None] = []
    for i in range(n):
        score, rsi, v, slow = feats.score[i], feats.rsi[i], feats.vol[i], feats.sma_slow[i]
        if score is None or rsi is None or v is None or slow is None or vol20[i] is None or vol120[i] is None:
            rows.append(None)
            continue
        v = max(v, 1e-4)
        mom12 = _mom(close, i, 252, v) if i >= 252 else _mom(close, i, 126, v)
        vol_regime = math.tanh(math.log(max(vol20[i], 1e-5) / max(vol120[i], 1e-5)))
        mkt = market[i] if market is not None and market[i] is not None else 0.0
        rows.append([
            score,
            _mom(close, i, 21, v),
            _mom(close, i, 63, v),
            _mom(close, i, 126, v),
            mom12,
            math.tanh(math.log(close[i] / high252[i]) / (v * math.sqrt(20))),
            (rsi - 50) / 50,
            vol_regime,
            mkt,
            math.tanh(math.log(close[i] / slow) / (v * math.sqrt(50))),
        ])
    return rows
