from __future__ import annotations

import math
import os
import random
from datetime import datetime, timedelta, timezone

import pytest

# Que un .env local no se cuele en las pruebas
os.environ["ENV_FILE"] = "/nonexistent"
for var in ("ETORO_API_KEY", "ETORO_USER_KEY", "ETORO_CONFIRMO_DINERO_REAL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(var, None)

from bot_trader.config import Asset, Settings  # noqa: E402
from bot_trader.models import Candle  # noqa: E402

START = datetime(2022, 1, 3, tzinfo=timezone.utc)


def make_series(seed: int, drift: float, vol: float, n: int = 600, start: float = 100.0) -> list[Candle]:
    """Caminata aleatoria geométrica con tendencia `drift` por vela."""
    rnd = random.Random(seed)
    price = start
    out = []
    for i in range(n):
        o = price
        c = o * math.exp(rnd.gauss(drift, vol))
        h = max(o, c) * math.exp(abs(rnd.gauss(0, vol / 2)))
        low = min(o, c) * math.exp(-abs(rnd.gauss(0, vol / 2)))
        out.append(Candle(START + timedelta(days=i), o, h, low, c, 1e6))
        price = c
    return out


def steady_trend(n: int = 600, start: float = 100.0, step: float = 0.004, wiggle: float = 0.01) -> list[Candle]:
    """Tendencia alcista limpia con retrocesos regulares (para pruebas deterministas)."""
    out = []
    price = start
    for i in range(n):
        o = price
        c = o * (1 + step + wiggle * math.sin(i / 3))
        out.append(Candle(START + timedelta(days=i), o, max(o, c) * 1.003, min(o, c) * 0.997, c, 1e6))
        price = c
    return out


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(
        assets=[Asset("UP", name="Up Corp"), Asset("DOWN", name="Down Inc")],
        db_path=str(tmp_path / "bot.db"),
    )
    s.news.enabled = False
    return s
