"""Gestión de riesgo: cuánto meter en cada operación y cuándo dejar de operar.

La regla principal de cualquier trader que sobrevive: decidir primero cuánto se puede perder.
El tamaño sale de ahí, no de cuánto "se siente" bien la operación:

    monto = capital x riesgo_por_operacion / (distancia al stop en % x apalancamiento)

Con 10 000 USD, 1 % de riesgo y el stop a 5 %, el monto es 2 000 USD: si salta el stop se
pierden ~100 USD (más costos y huecos de precio). Diez pérdidas seguidas dejan la cuenta en ~90 %.

Ajustes para que la ganancia sea sostenible (no la más rápida):
- Volatilidad objetivo: los activos más volátiles reciben montos más chicos, para que cada
  posición aporte un riesgo parecido y la cartera completa se mueva ~12 % al año.
- Correlación: si el nuevo activo se mueve casi igual que uno que ya se tiene, no se abre
  (sería duplicar la misma apuesta); si se parece bastante, se arriesga la mitad.
- Kelly fraccional: nunca se arriesga más de 1/4 de lo que la ventaja estimada justificaría.
- Drawdown: mientras más se cae desde el máximo, menos se arriesga (de 100 % a 25 %).
- Régimen: con mercado bajista se arriesga la mitad al COMPRAR lo que se mueve con el mercado.
- Ventas en corto: arriesgan la mitad que una compra y tienen un tope propio de exposición.
  Perder en corto no tiene techo (el precio puede subir sin límite y abrir con hueco), así
  que se tratan con más cuidado.

Frenos automáticos:
- Pérdida diaria máxima: si el capital cae X % en el día, no abre nada más hasta mañana.
- Drawdown máximo: si cae X % desde su máximo, el bot se PAUSA hasta que lo reanudes a mano.
- Límites de posiciones, exposición por activo y exposición total.
- En la cuenta real, tope duro por orden (`monto_maximo_real`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from bot_trader.config import RiskParams
from bot_trader.models import Position, Probability, Regime, Signal


@dataclass
class SizeDecision:
    ok: bool
    amount: float = 0.0
    reason: str = ""


@dataclass
class Guard:
    """Resultado de revisar los frenos de la cuenta."""

    can_open: bool
    paused: bool
    reason: str = ""


def check_guards(
    equity: float,
    peak: float,
    day_start: float,
    paused: bool,
    params: RiskParams,
) -> Guard:
    if paused:
        return Guard(False, True, "el bot está en pausa (usa `reanudar` cuando lo hayas revisado)")
    if peak > 0 and equity < peak * (1 - params.max_drawdown):
        dd = 1 - equity / peak
        return Guard(
            False,
            True,
            f"drawdown de {dd:.1%} desde el máximo ({peak:,.2f}) supera el límite de {params.max_drawdown:.0%}: "
            "bot pausado para revisión",
        )
    if day_start > 0 and equity < day_start * (1 - params.max_daily_loss):
        loss = 1 - equity / day_start
        return Guard(False, False, f"pérdida del día {loss:.1%} supera {params.max_daily_loss:.0%}: no abro más hoy")
    return Guard(True, False)


def position_size(
    equity: float,
    cash: float,
    positions: list[Position],
    price: float,
    stop: float,
    symbol: str,
    params: RiskParams,
    real_account: bool,
    risk_fraction: float | None = None,
    extra_limits: list[tuple[float, str]] | None = None,
) -> SizeDecision:
    if equity <= 0 or price <= 0:
        return SizeDecision(False, reason="capital o precio inválido")
    if len(positions) >= params.max_positions:
        return SizeDecision(False, reason=f"ya hay {len(positions)} posiciones (máximo {params.max_positions})")
    if any(p.symbol == symbol for p in positions):
        return SizeDecision(False, reason=f"ya hay una posición abierta en {symbol}")
    stop_pct = abs(price - stop) / price
    if stop_pct <= 0:
        return SizeDecision(False, reason="stop inválido")
    lev = params.leverage
    risk = params.risk_per_trade if risk_fraction is None else risk_fraction
    if risk <= 0:
        return SizeDecision(False, reason="sin presupuesto de riesgo para esta operación")
    amount = equity * risk / (stop_pct * lev)
    limits = [(amount, "riesgo por operación")]
    limits += extra_limits or []
    limits.append((params.max_asset_exposure * equity / lev, "máximo por activo"))
    used = sum(p.exposure for p in positions)
    limits.append((max(params.max_total_exposure * equity - used, 0.0) / lev, "exposición total"))
    limits.append((max(cash, 0.0), "efectivo disponible"))
    if real_account:
        limits.append((params.max_order_real, "tope por orden en cuenta real"))
    amount, binding = min(limits, key=lambda x: x[0])
    amount = round(amount, 2)
    if amount < params.min_order:
        return SizeDecision(
            False, reason=f"el monto ({amount:,.2f}) queda bajo el mínimo ({params.min_order:,.2f}) por {binding}"
        )
    note = "" if binding == "riesgo por operación" else f" (limitado por {binding})"
    risk_usd = amount * lev * stop_pct
    return SizeDecision(True, amount, f"arriesga ~{risk_usd:,.2f} USD si salta el stop{note}")


@dataclass
class TradeContext:
    """Lo que el gestor de riesgo necesita saber de la cartera para dimensionar una entrada."""

    drawdown: float = 0.0  # caída actual desde el máximo (0.05 = -5 %)
    regime: Regime | None = None
    annual_vol: float | None = None  # volatilidad anual del activo
    # Correlación "de apuesta" con cada posición abierta: ya trae el signo según el lado.
    # Comprar QQQ teniendo SPY comprado es duplicar (+0.9); venderlo en corto es cubrirse (-0.9).
    correlations: dict[str, float] = field(default_factory=dict)
    target_weight: float | None = None  # núcleo: fracción del capital que le toca a este activo


def bet_correlation(corr: float, same_side: bool) -> float:
    """Correlación entre dos apuestas: positiva si ganan y pierden juntas."""
    return corr if same_side else -corr


def risk_budget(
    params: RiskParams, ctx: TradeContext, prob: Probability | None, is_buy: bool = True
) -> tuple[float, list[str]]:
    """Fracción del capital a arriesgar en esta entrada, con la explicación de cada ajuste."""
    risk = params.risk_per_trade
    notes: list[str] = []
    if params.drawdown_scaling and ctx.drawdown > 0 and params.max_drawdown > 0:
        mult = max(0.25, 1 - ctx.drawdown / params.max_drawdown)
        if mult < 0.999:
            risk *= mult
            notes.append(f"riesgo x{mult:.2f} porque la cuenta está {ctx.drawdown:.1%} bajo su máximo")
    if is_buy:
        # Un mercado bajista es un viento en contra para comprar, no para vender en corto.
        if ctx.regime is not None and ctx.regime.applies and ctx.regime.risk_mult < 1:
            risk *= ctx.regime.risk_mult
            notes.append(f"riesgo x{ctx.regime.risk_mult:.2f} por mercado {ctx.regime.label}")
    elif params.short_mult < 1:
        risk *= params.short_mult
        notes.append(f"riesgo x{params.short_mult:.2f} por ser venta en corto")
    if ctx.correlations:
        symbol, corr = max(ctx.correlations.items(), key=lambda kv: kv[1])
        if corr >= params.max_correlation:
            return 0.0, [f"es casi la misma apuesta que {symbol}, que ya está en cartera (correlación {corr:.2f})"]
        if corr >= params.high_correlation:
            risk *= 0.5
            notes.append(f"riesgo a la mitad: correlación {corr:.2f} con {symbol}")
    if params.kelly_fraction > 0 and params.use_probability and prob is not None:
        kelly = prob.kelly
        if kelly <= 0:
            return 0.0, ["la ventaja estimada no alcanza para arriesgar (Kelly <= 0)"]
        cap = params.kelly_fraction * kelly
        if cap < risk:
            risk = cap
            notes.append(f"limitado a {cap:.2%} del capital por Kelly fraccional")
    return risk, notes


def size_trade(
    equity: float,
    cash: float,
    positions: list[Position],
    sig: Signal,
    params: RiskParams,
    ctx: TradeContext,
    real_account: bool,
) -> SizeDecision:
    """Tamaño de una entrada con todos los ajustes del perfil sostenible."""
    if ctx.target_weight is not None:
        return size_core(equity, cash, positions, sig, params, ctx, real_account)
    risk, notes = risk_budget(params, ctx, sig.probability, sig.is_buy)
    if risk <= 0:
        return SizeDecision(False, reason="; ".join(notes) or "sin presupuesto de riesgo")
    extra = []
    if not sig.is_buy:
        used_short = sum(p.exposure for p in positions if not p.is_buy)
        room = max(params.max_short_exposure * equity - used_short, 0.0)
        extra.append((room / params.leverage, "máximo en ventas en corto"))
    if params.vol_target > 0 and ctx.annual_vol and ctx.annual_vol > 0:
        weight = params.vol_target / (ctx.annual_vol * math.sqrt(max(params.max_positions, 1)))
        extra.append((weight * equity / params.leverage, f"volatilidad objetivo (activo con {ctx.annual_vol:.0%} anual)"))
    decision = position_size(
        equity, cash, positions, sig.price, sig.stop, sig.symbol, params, real_account,
        risk_fraction=risk, extra_limits=extra,
    )
    if decision.ok and notes:
        decision.reason += "; " + "; ".join(notes)
    return decision


def size_core(
    equity: float,
    cash: float,
    positions: list[Position],
    sig: Signal,
    params: RiskParams,
    ctx: TradeContext,
    real_account: bool,
) -> SizeDecision:
    """Núcleo: el monto sale del reparto por volatilidad, no de la distancia al stop."""
    if any(p.symbol == sig.symbol for p in positions):
        return SizeDecision(False, reason=f"ya hay una posición abierta en {sig.symbol}")
    weight = min(ctx.target_weight or 0.0, params.max_asset_exposure)
    lev = params.leverage
    used = sum(p.exposure for p in positions)
    limits = [
        (weight * equity / lev, "reparto por volatilidad"),
        (max(params.max_total_exposure * equity - used, 0.0) / lev, "exposición total"),
        (max(cash, 0.0), "efectivo disponible"),
    ]
    if real_account:
        limits.append((params.max_order_real, "tope por orden en cuenta real"))
    amount, binding = min(limits, key=lambda x: x[0])
    amount = round(amount, 2)
    if amount < params.min_order:
        return SizeDecision(False, reason=f"el monto ({amount:,.2f}) queda bajo el mínimo ({params.min_order:,.2f}) por {binding}")
    note = "" if binding == "reparto por volatilidad" else f" (limitado por {binding})"
    return SizeDecision(True, amount, f"{weight:.1%} del capital según su volatilidad{note}")


class RiskState:
    """Máximo histórico, capital al inicio del día y pausa; se guardan en la bitácora."""

    def __init__(self, journal):
        self.journal = journal

    @property
    def paused(self) -> bool:
        return self.journal.get("pausado", "0") == "1"

    @property
    def pause_reason(self) -> str:
        return self.journal.get("motivo_pausa", "") or ""

    def pause(self, reason: str) -> None:
        self.journal.set("pausado", "1")
        self.journal.set("motivo_pausa", reason)

    def resume(self, equity: float | None = None) -> None:
        self.journal.set("pausado", "0")
        self.journal.set("motivo_pausa", "")
        if equity is not None:  # se reinicia el máximo para no volver a pausar de inmediato
            self.journal.set("pico", f"{equity:.2f}")

    def update(self, equity: float, today: date) -> tuple[float, float]:
        """Registra el capital actual. Devuelve (máximo histórico, capital al inicio del día)."""
        peak = max(float(self.journal.get("pico", "0") or 0), equity)
        self.journal.set("pico", f"{peak:.2f}")
        if self.journal.get("dia") != today.isoformat():
            self.journal.set("dia", today.isoformat())
            self.journal.set("inicio_dia", f"{equity:.2f}")
        day_start = float(self.journal.get("inicio_dia", f"{equity:.2f}") or equity)
        return peak, day_start
