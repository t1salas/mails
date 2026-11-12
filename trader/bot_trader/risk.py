"""Gestión de riesgo: cuánto meter en cada operación y cuándo dejar de operar.

La regla principal de cualquier trader que sobrevive: decidir primero cuánto se puede perder.
El tamaño sale de ahí, no de cuánto "se siente" bien la operación:

    monto = capital x riesgo_por_operacion / (distancia al stop en % x apalancamiento)

Con 10 000 USD, 1 % de riesgo y el stop a 5 %, el monto es 2 000 USD: si salta el stop se
pierden ~100 USD (más costos y huecos de precio). Diez pérdidas seguidas dejan la cuenta en ~90 %.

Frenos automáticos:
- Pérdida diaria máxima: si el capital cae X % en el día, no abre nada más hasta mañana.
- Drawdown máximo: si cae X % desde su máximo, el bot se PAUSA hasta que lo reanudes a mano.
- Límites de posiciones, exposición por activo y exposición total.
- En la cuenta real, tope duro por orden (`monto_maximo_real`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from bot_trader.config import RiskParams
from bot_trader.models import Position


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
    amount = equity * params.risk_per_trade / (stop_pct * lev)
    limits = [(amount, "riesgo por operación")]
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
