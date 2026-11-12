import math

import pytest

from bot_trader.analysis import indicators as ind


def test_sma_and_ema():
    values = [1, 2, 3, 4, 5, 6]
    assert ind.sma(values, 3) == [None, None, 2.0, 3.0, 4.0, 5.0]
    e = ind.ema(values, 3)
    assert e[:2] == [None, None]
    assert e[2] == pytest.approx(2.0)
    assert e[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5


def test_rsi_extremes_and_range():
    up = [float(i) for i in range(1, 40)]
    assert ind.rsi(up, 14)[-1] == 100.0
    down = list(reversed(up))
    assert ind.rsi(down, 14)[-1] == pytest.approx(0.0)
    mixed = [10, 11, 10.5, 11.5, 11, 12, 11.2, 12.4, 12, 13, 12.1, 13.5, 13, 14, 13.2, 14.4]
    r = ind.rsi(mixed, 14)
    assert r[13] is None and 0 < r[-1] < 100


def test_atr_constant_range():
    highs = [11.0] * 20
    lows = [9.0] * 20
    closes = [10.0] * 20
    a = ind.atr(highs, lows, closes, 14)
    assert a[12] is None
    assert a[-1] == pytest.approx(2.0)


def test_rolling_std_and_drawdown():
    s = ind.rolling_std([1, 1, 1, 1], 3)
    assert s[-1] == pytest.approx(0.0)
    assert ind.max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
    assert ind.log_returns([100, 110])[1] == pytest.approx(math.log(1.1))


def test_macd_histogram_sign_follows_trend():
    up = [100 * 1.01**i for i in range(80)]
    h = ind.macd_histogram(up)
    assert h[-1] is not None and h[-1] >= 0
    assert h[0] is None
