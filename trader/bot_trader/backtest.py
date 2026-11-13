"""Backtest: corre el MISMO analista, la MISMA estrategia y el MISMO control de riesgo sobre la historia.

Supuestos (conservadores a propósito):
- La decisión se toma al cierre de la vela y se ejecuta en la apertura de la siguiente.
- Se cobra `costo_por_lado` al entrar y al salir, y `costo_anual_cfd` por cada día que una venta
  en corto (o una posición apalancada) sigue abierta.
- Dentro de una vela se revisa primero el stop y después se mueve el stop móvil con el máximo.
  Los huecos de precio se ejecutan al precio de apertura, no al del stop.
- Probabilidades y modelo estadístico solo usan operaciones que ya habían terminado en esa
  fecha; el modelo se reentrena una vez al mes ("walk-forward").
- Las noticias NO se simulan (no hay titulares históricos confiables): se mide la parte
  técnica y estadística. En vivo las noticias solo pueden frenar o sumar un poco.

Además de cuánto gana, mide si la ganancia es SOSTENIBLE: caídas, tiempo bajo el agua,
consistencia año por año, calidad de las predicciones y robustez ante cambios de parámetros.
Un backtest bueno no garantiza nada. Uno malo sí es razón suficiente para no operar.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

from bot_trader.analysis.analyst import Analyst
from bot_trader.analysis.indicators import max_drawdown
from bot_trader.config import Settings
from bot_trader.models import Candle, Position
from bot_trader.risk import TradeContext, bet_correlation, size_trade

RETRAIN_DAYS = 30


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
class Prediction:
    p: float  # probabilidad final (calibrada)
    p_hist: float | None
    p_model: float | None
    base: float  # tasa de acierto histórica del activo (la referencia "tonta")
    won: bool
    is_buy: bool = True


def _brier(pairs: list[tuple[float, bool]]) -> float | None:
    if not pairs:
        return None
    return sum((p - (1.0 if w else 0.0)) ** 2 for p, w in pairs) / len(pairs)


def _curve_stats(values: list[float], dates: list[date]) -> dict:
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    out = {"vol": 0.0, "sharpe": 0.0, "sortino": 0.0}
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        down = [min(r, 0.0) for r in rets]
        dvar = sum(d * d for d in down) / len(down)
        out["vol"] = math.sqrt(var) * math.sqrt(252)
        out["sharpe"] = mean / math.sqrt(var) * math.sqrt(252) if var > 0 else 0.0
        out["sortino"] = mean / math.sqrt(dvar) * math.sqrt(252) if dvar > 0 else 0.0
    years = max((dates[-1] - dates[0]).days / 365.25, 1 / 365.25) if len(dates) > 1 else 0.0
    out["cagr"] = (values[-1] / values[0]) ** (1 / years) - 1 if years and values[0] > 0 and values[-1] > 0 else 0.0
    out["max_dd"] = max_drawdown(values)
    return out


@dataclass
class BacktestResult:
    initial: float
    final: float
    start: date | None
    end: date | None
    equity: list[tuple[date, float]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    predictions: list[Prediction] = field(default_factory=list)
    benchmark: list[float] = field(default_factory=list)
    would_pause_on: date | None = None
    exposure: float = 0.0  # fracción de días con al menos una posición abierta

    # --- rendimiento ---------------------------------------------------------------------

    @property
    def total_return(self) -> float:
        return self.final / self.initial - 1 if self.initial else 0.0

    @property
    def years(self) -> float:
        if not self.start or not self.end:
            return 0.0
        return max((self.end - self.start).days / 365.25, 1 / 365.25)

    def _stats(self) -> dict:
        if not self.equity:
            return {"vol": 0.0, "sharpe": 0.0, "sortino": 0.0, "cagr": 0.0, "max_dd": 0.0}
        return _curve_stats([e for _, e in self.equity], [d for d, _ in self.equity])

    @property
    def cagr(self) -> float:
        return self._stats()["cagr"]

    @property
    def max_dd(self) -> float:
        return self._stats()["max_dd"]

    @property
    def sharpe(self) -> float:
        return self._stats()["sharpe"]

    @property
    def sortino(self) -> float:
        return self._stats()["sortino"]

    @property
    def annual_vol(self) -> float:
        return self._stats()["vol"]

    @property
    def calmar(self) -> float:
        dd = self.max_dd
        return self.cagr / dd if dd > 0 else 0.0

    @property
    def benchmark_stats(self) -> dict:
        if not self.benchmark or not self.equity:
            return {"vol": 0.0, "sharpe": 0.0, "sortino": 0.0, "cagr": 0.0, "max_dd": 0.0, "total": 0.0}
        stats = _curve_stats(self.benchmark, [d for d, _ in self.equity])
        stats["total"] = self.benchmark[-1] / self.benchmark[0] - 1
        stats["calmar"] = stats["cagr"] / stats["max_dd"] if stats["max_dd"] > 0 else 0.0
        return stats

    @property
    def benchmark_return(self) -> float:
        return self.benchmark_stats["total"]

    @property
    def benchmark_max_dd(self) -> float:
        return self.benchmark_stats["max_dd"]

    # --- sostenibilidad ------------------------------------------------------------------

    @property
    def yearly(self) -> list[tuple[int, float]]:
        out, prev = [], self.initial
        by_year: dict[int, float] = {}
        for d, e in self.equity:
            by_year[d.year] = e
        for year in sorted(by_year):
            out.append((year, by_year[year] / prev - 1 if prev else 0.0))
            prev = by_year[year]
        return out

    @property
    def positive_months(self) -> float:
        by_month: dict[tuple[int, int], float] = {}
        for d, e in self.equity:
            by_month[(d.year, d.month)] = e
        values = [self.initial] + [by_month[k] for k in sorted(by_month)]
        changes = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
        return sum(1 for c in changes if c > 0) / len(changes) if changes else 0.0

    @property
    def longest_underwater_days(self) -> int:
        """Mayor tiempo (días) que la cuenta pasó por debajo de su máximo anterior."""
        peak, peak_date, worst = -math.inf, None, 0
        for d, e in self.equity:
            if e >= peak:
                peak, peak_date = e, d
            elif peak_date is not None:
                worst = max(worst, (d - peak_date).days)
        return worst

    # --- operaciones ---------------------------------------------------------------------

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

    def side_stats(self, is_buy: bool) -> dict | None:
        """Resumen de las compras (True) o de las ventas en corto (False)."""
        trades = [t for t in self.trades if t.is_buy == is_buy]
        if not trades:
            return None
        return {
            "n": len(trades),
            "win_rate": sum(1 for t in trades if t.pnl > 0) / len(trades),
            "pnl": sum(t.pnl for t in trades),
            "avg_r": sum(t.r for t in trades) / len(trades),
        }

    @property
    def avg_days(self) -> float:
        return sum((t.exit_date - t.entry_date).days for t in self.trades) / len(self.trades) if self.trades else 0.0

    # --- calidad de las predicciones -----------------------------------------------------

    def prediction_quality(self) -> dict:
        """Puntaje de Brier (error cuadrático de las probabilidades; menor es mejor) contra la tasa base.

        skill > 0 significa que la predicción es mejor que usar siempre la tasa de acierto
        promedio del activo. En mercados, un skill de +1 % a +5 % ya es una ventaja real.
        """
        preds = self.predictions
        base = _brier([(p.base, p.won) for p in preds])
        out = {"n": len(preds), "base": base}
        for key, side in (("largos", True), ("cortos", False)):
            group = [p for p in preds if p.is_buy == side]
            b0 = _brier([(p.base, p.won) for p in group])
            b1 = _brier([(p.p, p.won) for p in group])
            out[f"{key}_n"] = len(group)
            out[f"{key}_skill"] = (1 - b1 / b0) if b0 and b1 is not None else None
        for key, pairs in (
            ("final", [(p.p, p.won) for p in preds]),
            ("hist", [(p.p_hist, p.won) for p in preds if p.p_hist is not None]),
            ("model", [(p.p_model, p.won) for p in preds if p.p_model is not None]),
        ):
            b = _brier(pairs)
            out[key] = b
            out[f"{key}_skill"] = (1 - b / base) if b is not None and base else None
        buckets = [(0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 1.01)]
        calib = []
        for lo, hi in buckets:
            group = [p for p in preds if lo <= p.p < hi]
            if group:
                calib.append((lo, hi, len(group), sum(p.p for p in group) / len(group),
                              sum(1 for p in group if p.won) / len(group)))
        out["calibration"] = calib
        return out


def _ts(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=timezone.utc)


def run_backtest(
    settings: Settings,
    data: dict[str, list[Candle]],
    initial: float | None = None,
    reference: list[Candle] | None = None,
) -> BacktestResult:
    sp, rp = settings.strategy, settings.risk
    cost = settings.cost_per_side
    initial = initial or settings.paper_capital
    data = {s: c for s, c in data.items() if len(c) >= sp.slow_sma + 60}
    result = BacktestResult(initial=initial, final=initial, start=None, end=None)
    if not data:
        return result
    analyst = Analyst(data, settings, reference)
    feats, index = analyst.feats, analyst.index

    # La medición empieza el primer día en que algún activo ya tiene probabilidades.
    starts = []
    for symbol, f in feats.items():
        t0 = next((t for t in range(len(f)) if analyst.hist[symbol][True].at(t) is not None), None)
        if t0 is not None:
            starts.append(analyst.dates[symbol][t0])
    if not starts:
        return result
    start = min(starts)
    calendar = sorted({d for idx in index.values() for d in idx if d >= start})

    cash = initial
    positions: dict[str, Position] = {}
    risk_at_entry: dict[str, float] = {}
    pending: dict[str, tuple] = {}
    last_exit: dict[str, date] = {}
    last_close: dict[str, float] = {}
    peak, prev_equity = initial, initial
    days_exposed = 0
    cooldown_days = math.ceil(rp.cooldown_hours / 24)

    def close(symbol: str, raw_price: float, d: date, reason: str) -> None:
        nonlocal cash
        p = positions.pop(symbol)
        exit_price = raw_price * (1 - cost) if p.is_buy else raw_price * (1 + cost)
        pnl = ((exit_price - p.entry_price) if p.is_buy else (p.entry_price - exit_price)) * p.units
        carry = analyst.carry(p.is_buy)
        if carry:
            pnl -= p.exposure * carry * (d - p.opened_at.date()).days / 365
        cash += p.amount + pnl
        risk = risk_at_entry.pop(symbol, 0) or 1.0
        result.trades.append(
            Trade(symbol, p.is_buy, p.opened_at.date(), d, p.entry_price, exit_price, p.amount, pnl, pnl / risk, reason)
        )
        last_exit[symbol] = d

    for d in calendar:
        analyst.maybe_train(d, RETRAIN_DAYS)

        # 1. Órdenes del día anterior en la apertura; luego stops dentro de la vela.
        for symbol, idx in index.items():
            t = idx.get(d)
            if t is None:
                continue
            f = feats[symbol]
            o, h, low = f.open[t], f.high[t], f.low[t]
            order = pending.pop(symbol, None)
            if order and order[0] == "close" and symbol in positions:
                close(symbol, o, d, order[1])
            elif order and order[0] == "open" and symbol not in positions:
                _, is_buy, amount, stop, target, trailing, dist = order
                beyond_target = target is not None and (o >= target if is_buy else o <= target)
                gapped = (o <= stop if is_buy else o >= stop) or beyond_target
                if not gapped and amount <= cash:
                    fill = o * (1 + cost) if is_buy else o * (1 - cost)
                    positions[symbol] = Position(
                        id=f"bt-{symbol}-{d}", symbol=symbol, is_buy=is_buy, amount=amount,
                        units=amount * rp.leverage / fill, leverage=rp.leverage, entry_price=fill,
                        stop=stop, target=target, opened_at=_ts(d), trailing=trailing, trail_distance=dist,
                    )
                    risk_at_entry[symbol] = abs(fill - stop) * amount * rp.leverage / fill
                    cash -= amount
            p = positions.get(symbol)
            if p is not None:
                label = "stop móvil" if p.trailing else "stop"
                if p.is_buy:
                    if o <= p.stop:
                        close(symbol, o, d, f"{label} (hueco)")
                    elif low <= p.stop:
                        close(symbol, p.stop, d, label)
                    elif p.target is not None and o >= p.target:
                        close(symbol, o, d, "objetivo (hueco)")
                    elif p.target is not None and h >= p.target:
                        close(symbol, p.target, d, "objetivo")
                    elif p.trailing:
                        p.stop = max(p.stop, h - p.trail_distance)
                else:
                    if o >= p.stop:
                        close(symbol, o, d, f"{label} (hueco)")
                    elif h >= p.stop:
                        close(symbol, p.stop, d, label)
                    elif p.target is not None and o <= p.target:
                        close(symbol, o, d, "objetivo (hueco)")
                    elif p.target is not None and low <= p.target:
                        close(symbol, p.target, d, "objetivo")
                    elif p.trailing:
                        p.stop = min(p.stop, low + p.trail_distance)
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
        daily_halt = equity < prev_equity * (1 - rp.max_daily_loss)
        prev_equity = equity

        # 3. Decisiones al cierre (se ejecutan mañana)
        entries = []
        for symbol, idx in index.items():
            t = idx.get(d)
            if t is None:
                continue
            sig = analyst.evaluate(symbol, t, positions.get(symbol), None)
            for side in analyst.sides:
                prob = analyst.probability(symbol, t, side)
                outcome = analyst.hist[symbol][side].outcomes[t]
                if prob is not None and outcome is not None:
                    result.predictions.append(
                        Prediction(prob.p_win, prob.p_hist, prob.p_model, prob.base_p_win, outcome.r > 0, side)
                    )
            if sig.action == "CERRAR":
                pending[symbol] = ("close", "tesis rota")
            elif sig.opens_position and not daily_halt:
                recent = last_exit.get(symbol)
                if recent is None or (d - recent).days >= cooldown_days:
                    entries.append((sig, t))
        entries.sort(key=lambda e: e[0].probability.expectancy_r if e[0].probability else 0, reverse=True)
        open_now = list(positions.values())
        reserved = 0.0
        drawdown = max(0.0, 1 - equity / peak) if peak > 0 else 0.0
        for sig, t in entries:
            correlations = {}
            for p in open_now:
                c = analyst.correlation(sig.symbol, p.symbol, d)
                if c is not None:
                    correlations[p.symbol] = bet_correlation(c, p.is_buy == sig.is_buy)
            ctx = TradeContext(
                drawdown=drawdown,
                regime=analyst.regime_for(sig.symbol, d),
                annual_vol=analyst.annual_vol(sig.symbol, t),
                correlations=correlations,
                target_weight=analyst.core_weight(sig.symbol, d) if sp.kind == "nucleo" else None,
            )
            size = size_trade(equity, cash - reserved, open_now, sig, rp, ctx, False)
            if not size.ok:
                continue
            dist = abs(sig.price - sig.stop)
            pending[sig.symbol] = ("open", sig.is_buy, size.amount, sig.stop, sig.target, sig.trailing, dist)
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
        if days:
            base = f.close[idx[days[0]]]
            curves.append({d: f.close[idx[d]] / base for d in days})
    if curves:
        last = [1.0] * len(curves)
        for d in calendar:
            for k, curve in enumerate(curves):
                last[k] = curve.get(d, last[k])
            result.benchmark.append(sum(last) / len(last))
    return result


def robustness(
    settings: Settings, data: dict[str, list[Candle]], reference: list[Candle] | None = None
) -> list[tuple[str, BacktestResult]]:
    """Corre el backtest con variaciones razonables de los parámetros.

    Si la estrategia solo funciona con los números exactos de la configuración, está sobreajustada
    al pasado y no es sostenible. Una estrategia robusta da resultados parecidos en todas.
    """
    variants = [("configuración actual", lambda s: None)]
    sp = settings.strategy
    variants += [
        (f"stop {sp.stop_atr * 0.75:g} ATR", lambda s: setattr(s.strategy, "stop_atr", sp.stop_atr * 0.75)),
        (f"stop {sp.stop_atr * 1.25:g} ATR", lambda s: setattr(s.strategy, "stop_atr", sp.stop_atr * 1.25)),
        (f"umbral entrada {sp.entry_score - 0.1:+.2f}", lambda s: setattr(s.strategy, "entry_score", sp.entry_score - 0.1)),
        (f"umbral entrada {sp.entry_score + 0.1:+.2f}", lambda s: setattr(s.strategy, "entry_score", sp.entry_score + 0.1)),
        ("medias 40/160", lambda s: (setattr(s.strategy, "fast_sma", 40), setattr(s.strategy, "slow_sma", 160))),
        ("medias 60/240", lambda s: (setattr(s.strategy, "fast_sma", 60), setattr(s.strategy, "slow_sma", 240))),
        ("costos al doble", lambda s: setattr(s, "cost_per_side", s.cost_per_side * 2)),
    ]
    if settings.risk.allow_short:
        variants.append(("sin ventas en corto", lambda s: setattr(s.risk, "allow_short", False)))
        variants.append(("costo CFD al doble", lambda s: setattr(s, "cfd_annual_cost", s.cfd_annual_cost * 2)))
    else:
        variants.append(("con ventas en corto", lambda s: setattr(s.risk, "allow_short", True)))
    out = []
    for label, change in variants:
        variant = deepcopy(settings)
        change(variant)
        out.append((label, run_backtest(variant, data, reference=reference)))
    return out


def compare_variants(
    settings: Settings, data: dict[str, list[Candle]], reference: list[Candle] | None = None
) -> list[tuple[str, BacktestResult]]:
    """Las versiones de la estrategia que vale la pena comparar, sobre los mismos datos."""

    def no_probability(s: Settings) -> None:
        s.risk.use_probability = False

    def double_risk(s: Settings) -> None:
        s.risk.risk_per_trade = min(s.risk.risk_per_trade * 2, 0.05)

    def core(s: Settings) -> None:
        s.strategy.kind = "nucleo"
        s.risk.allow_short = False

    variants = [
        ("agente (configuración actual)", []),
        ("agente sin filtro de probabilidad", [no_probability]),
        ("agente con el doble de riesgo", [double_risk]),
        ("agente sin filtro y doble riesgo", [no_probability, double_risk]),
        ("núcleo: media de 200 días", [core]),
    ]
    out = []
    for label, changes in variants:
        variant = deepcopy(settings)
        variant.strategy.kind = "agente"
        for change in changes:
            change(variant)
        out.append((label, run_backtest(variant, data, reference=reference)))
    return out
