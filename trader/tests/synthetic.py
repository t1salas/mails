import math, random
from datetime import datetime, timedelta, timezone
from bot_trader.models import Candle

def market(n=1000, seed=11):
    """Factor de mercado con regímenes: alcista, bajista, lateral."""
    rnd = random.Random(seed); out = []; regime_drift = 0.0006
    for i in range(n):
        if i % 180 == 0:
            regime_drift = rnd.choice([0.0008, 0.0006, -0.0010, 0.0])
        out.append(rnd.gauss(regime_drift, 0.011))
    return out

def asset(mkt, beta, idio_vol, drift, seed, start=100.0):
    rnd = random.Random(seed); p = start; out = []; d0 = datetime(2022, 1, 3, tzinfo=timezone.utc)
    trend = drift
    for i, m in enumerate(mkt):
        if i % 150 == 0:
            trend = drift + rnd.choice([0.0006, -0.0005, 0.0])
        r = beta * m + rnd.gauss(trend, idio_vol)
        o = p * math.exp(rnd.gauss(0, idio_vol / 4))
        c = o * math.exp(r)
        h = max(o, c) * math.exp(abs(rnd.gauss(0, idio_vol / 2)))
        l = min(o, c) * math.exp(-abs(rnd.gauss(0, idio_vol / 2)))
        out.append(Candle(d0 + timedelta(days=i), o, h, l, c, 1e6)); p = c
    return out

def universe(n=1000):
    m = market(n)
    return {
        "SPY": asset(m, 1.0, 0.003, 0.0, 1),
        "QQQ": asset(m, 1.3, 0.006, 0.0001, 2),
        "AAPL": asset(m, 1.1, 0.012, 0.0002, 3),
        "NVDA": asset(m, 1.6, 0.020, 0.0004, 4),
        "GLD": asset(m, 0.05, 0.009, 0.0002, 5),
        "TLT": asset(m, -0.2, 0.008, -0.0001, 6),
        "BTC": asset(m, 0.4, 0.030, 0.0005, 7),
    }
