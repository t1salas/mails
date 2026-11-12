"""Backtest: corre la MISMA estrategia y el MISMO control de riesgo sobre datos históricos.

Supuestos (conservadores a propósito):
- La decisión se toma al cierre de la vela y se ejecuta en la apertura de la siguiente.
- Se cobra `costo_por_lado` al entrar y al salir.
- Si el stop y el objetivo caen en la misma vela, cuenta el stop. Los huecos de precio se
  ejecutan al precio de apertura, no al del stop.
- Las probabilidades solo usan operaciones que ya habían terminado en esa fecha.
- Las noticias NO se simulan (no hay titulares históricos confiables): el backtest mide solo
  la parte técnica. Las noticias en vivo solo pueden frenar o sumar un poco.

Un backtest bueno no garantiza nada. Uno malo sí es razón suficiente para no operar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

from bot_trader.analysis.indicators import max_drawdown
from bot_trader.analysis.probability import ProbabilityModel
from bot_trader.analysis.trend import compute_features
from bot_trader.config import Settings
from bot_trader.models import Candle, Position
from bot_trader.risk import position_size
from bot_trader.strategy import evaluate


@dataclass
class Trade:
    symbol: str
    is_buy: bool
    entry_date: date
    exit_date: date
    entry: float
    exit: float
    amount: float
    pnl: float
    r: float
    reason: str


@dataclass
class BacktestResult:
    initial: float
    final: float
    start: date | None
    end: date | None
    equity: list[tuple[date, float]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    benchmark_return: float = 0.0
    benchmark_max_dd: float = 0.0
    would_pause_on: date | None = None
    exposure: float = 0.0  # fracción de días con al menos una posición abierta

    @property
    def total_return(self) -> float:
        return self.final / self.initial - 1 if self.initial else 0.0

    @property
    def years(self) -> float:
        if not self.start or not self.end:
            return 0.0
        return max((self.end - self.start).days / 365.25, 1 / 365.25)

    @property
    def cagr(self) -> float:
        if self.final <= 0:
            return -1.0
        return (self.final / self.initial) ** (1 / self.years) - 1 if self.years else 0.0

    @property
    def max_dd(self) -> float:
        return max_drawdown([e for _, e in self.equity])

    @property
    def sharpe(self) -> float:
        values = [e for _, e in self.equity]
        rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return mean / math.sqrt(var) * math.sqrt(252) if var > 0 else 0.0

    @property
    def win_rate(self) -> float:
        return sum(1 for t in self.trades if t.pnl > 0) / len(self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        wins = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return wins / losses if losses > 0 else (math.inf if wins > 0 else 0.0)

    @property
    def avg_r(self) -> float:
        return sum(t.r for t in self.trades) / len(self.trades) if self.trades else 0.0



def _ts(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=timezone.utc)


def run_backtest(settings: Settings, data: dict[str, list[Candle]], initial: float | None = None) -> BacktestResult:
    sp, rp = settings.strategy, settings.risk
    cost = settings.cost_per_side
    initial = initial or settings.paper_capital
    feats, longs, shorts, index = {}, {}, {}, {}
    for symbol, candles in data.items():
        if len(candles) < sp.slow_sma + sp.horizon + 50:
            continue
        f = compute_features(candles, sp)
        feats[symbol] = f
        longs[symbol] = ProbabilityModel(f, sp, cost, True)
        shorts[symbol] = ProbabilityModel(f, sp, cost, False) if rp.allow_short else None
        index[symbol] = {c.date.date(): i for i, c in enumerate(candles)}
    result = BacktestResult(initial=initial, final=initial, start=None, end=None)
    if not feats:
        return result

    # Primer día en que algún activo ya tiene probabilidades: ahí empieza la medición.
    start_dates = []
    for symbol, f in feats.items():
        t0 = next((t for t in range(len(f)) if longs[symbol].at(t) is not None), None)
        if t0 is not None:
            start_dates.append(f.candles[t0].date.date())
    if not start_dates:
        return result
    start = min(start_dates)
    calendar = sorted({d for idx in index.values() for d in idx if d >= start})

    cash = initial
    positions: dict[str, Position] = {}
    risk_at_entry: dict[str, float] = {}
    pending: dict[str, tuple] = {}
    last_exit: dict[str, date] = {}
    last_close: dict[str, float] = {}
    peak = initial
    days_exposed = 0
    cooldown_days = math.ceil(rp.cooldown_hours / 24)

    def close(symbol: str, raw_price: float, d: date, reason: str) -> None:
        nonlocal cash
        p = positions.pop(symbol)
        exit_price = raw_price * (1 - cost) if p.is_buy else raw_price * (1 + cost)
        pnl = ((exit_price - p.entry_price) if p.is_buy else (p.entry_price - exit_price)) * p.units
        cash += p.amount + pnl
        risk = risk_at_entry.pop(symbol, 0) or 1.0
        result.trades.append(
            Trade(symbol, p.is_buy, p.opened_at.date(), d, p.entry_price, exit_price, p.amount, pnl, pnl / risk, reason)
        )
        last_exit[symbol] = d

    for d in calendar:
        # 1. Órdenes del día anterior, en la apertura; luego stops/objetivos dentro de la vela.
        for symbol, idx in index.items():
            t = idx.get(d)
            if t is None or symbol not in feats:
                continue
            f = feats[symbol]
            o, h, low = f.open[t], f.high[t], f.low[t]
            order = pending.pop(symbol, None)
            if order and order[0] == "close" and symbol in positions:
                close(symbol, o, d, order[1])
            elif order and order[0] == "open" and symbol not in positions:
                _, is_buy, amount, stop, target = order
                gapped = (o <= stop or o >= target) if is_buy else (o >= stop or o <= target)
                if not gapped and amount <= cash:
                    fill = o * (1 + cost) if is_buy else o * (1 - cost)
                    positions[symbol] = Position(
                        id=f"bt-{symbol}-{d}", symbol=symbol, is_buy=is_buy, amount=amount,
                        units=amount * rp.leverage / fill, leverage=rp.leverage, entry_price=fill,
                        stop=stop, target=target, opened_at=_ts(d),
                    )
                    risk_at_entry[symbol] = abs(fill - stop) * amount * rp.leverage / fill
                    cash -= amount
            p = positions.get(symbol)
            if p is not None:
                if p.is_buy:
                    if o <= p.stop:
                        close(symbol, o, d, "stop (hueco)")
                    elif low <= p.stop:
                        close(symbol, p.stop, d, "stop")
                    elif o >= p.target:
                        close(symbol, o, d, "objetivo (hueco)")
                    elif h >= p.target:
                        close(symbol, p.target, d, "objetivo")
                else:
                    if o >= p.stop:
                        close(symbol, o, d, "stop (hueco)")
                    elif h >= p.stop:
                        close(symbol, p.stop, d, "stop")
                    elif o <= p.target:
                        close(symbol, o, d, "objetivo (hueco)")
                    elif low <= p.target:
                        close(symbol, p.target, d, "objetivo")
            last_close[symbol] = f.close[t]

        # 2. Valor de la cuenta al cierre
        for p in positions.values():
            price = last_close.get(p.symbol, p.entry_price)
            p.pnl = ((price - p.entry_price) if p.is_buy else (p.entry_price - price)) * p.units
        equity = cash + sum(p.value for p in positions.values())
        result.equity.append((d, equity))
        days_exposed += 1 if positions else 0
        peak = max(peak, equity)
        if result.would_pause_on is None and equity < peak * (1 - rp.max_drawdown):
            result.would_pause_on = d

        # 3. Decisiones al cierre (se ejecutan mañana)
        entries = []
        for symbol, idx in index.items():
            t = idx.get(d)
            if t is None or symbol not in feats:
                continue
            short_model = shorts.get(symbol)
            sig = evaluate(
                symbol, feats[symbol], t, longs[symbol].at(t), short_model.at(t) if short_model else None,
                None, positions.get(symbol), sp, rp,
            )
            if sig.action == "CERRAR":
                pending[symbol] = ("close", "tesis rota")
            elif sig.opens_position:
                recent = last_exit.get(symbol)
                if recent is None or (d - recent).days >= cooldown_days:
                    entries.append(sig)
        entries.sort(key=lambda s: s.probability.expectancy_r if s.probability else 0, reverse=True)
        open_now = list(positions.values())
        reserved = 0.0
        for sig in entries:
            if len(open_now) >= rp.max_positions:
                break
            size = position_size(equity, cash - reserved, open_now, sig.price, sig.stop, sig.symbol, rp, False)
            if not size.ok:
                continue
            pending[sig.symbol] = ("open", sig.is_buy, size.amount, sig.stop, sig.target)
            reserved += size.amount
            open_now.append(
                Position(id="tmp", symbol=sig.symbol, is_buy=sig.is_buy, amount=size.amount, units=0,
                         entry_price=sig.price, stop=sig.stop, target=sig.target, opened_at=_ts(d),
                         leverage=rp.leverage)
            )

    end = calendar[-1]
    for symbol in list(positions):
        close(symbol, last_close[symbol], end, "fin del backtest")
    if result.equity:
        result.equity[-1] = (end, cash)
    result.final = cash
    result.start, result.end = start, end
    result.exposure = days_exposed / len(calendar) if calendar else 0.0

    # Referencia: comprar y mantener todos los activos en partes iguales desde el mismo día.
    curves = []
    for symbol, f in feats.items():
        idx = index[symbol]
        days = [d for d in calendar if d in idx]
        if not days:
            continue
        base = f.close[idx[days[0]]]
        curves.append({d: f.close[idx[d]] / base for d in days})
    if curves:
        bench, last = [], [1.0] * len(curves)
        for d in calendar:
            for k, curve in enumerate(curves):
                last[k] = curve.get(d, last[k])
            bench.append(sum(last) / len(last))
        result.benchmark_return = bench[-1] - 1
        result.benchmark_max_dd = max_drawdown(bench)
    return result
