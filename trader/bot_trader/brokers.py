"""Brokers: dónde se ejecutan las órdenes.

- `PaperBroker`: simulación local (cuenta "papel"), sin eToro. Ideal para empezar.
- `EtoroBroker`: cuenta demo (dinero virtual) o real de eToro, vía la API pública.

El bot SOLO administra las posiciones que él mismo abrió (las registra en la bitácora).
Nunca toca posiciones que hayas abierto a mano ni las de copy trading.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from bot_trader.db import Journal, now_utc
from bot_trader.etoro import (
    FINAL_FAILURES,
    ORDER_FILLED,
    ORDER_PARTIAL,
    EtoroClient,
    EtoroError,
    ci_get,
)
from bot_trader.models import Account, Candle, Position

log = logging.getLogger(__name__)


@dataclass
class Fill:
    ok: bool
    position: Position | None = None
    message: str = ""
    pending: bool = False
    reference: str | None = None
    broker_order: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Exit:
    position: Position
    price: float | None
    pnl: float | None
    reason: str


class Broker(ABC):
    name = "base"
    real_money = False

    def __init__(self, journal: Journal):
        self.journal = journal

    @abstractmethod
    def account(self, prices: dict[str, float]) -> Account: ...

    @abstractmethod
    def open(
        self,
        symbol: str,
        instrument_id: int | None,
        is_buy: bool,
        amount: float,
        leverage: int,
        price: float,
        stop: float | None,
        target: float | None,
        trailing: bool = False,
    ) -> Fill: ...

    @abstractmethod
    def close(self, position: Position, price: float | None, reason: str) -> Exit: ...

    def check_exits(self, candles: dict[str, list[Candle]]) -> list[Exit]:
        """Stops y objetivos alcanzados. En eToro los ejecuta el propio broker."""
        return []


def _pnl(p: Position, price: float) -> float:
    move = (price - p.entry_price) if p.is_buy else (p.entry_price - price)
    return move * p.units


class PaperBroker(Broker):
    """Cuenta simulada. Cobra `costo_por_lado` en cada entrada y salida (spread + deslizamiento)."""

    name = "papel"

    def __init__(self, journal: Journal, capital: float, cost_per_side: float):
        super().__init__(journal)
        self.cost = cost_per_side
        if journal.get("efectivo") is None:
            journal.set("efectivo", f"{capital:.2f}")

    @property
    def cash(self) -> float:
        return float(self.journal.get("efectivo", "0") or 0)

    def _set_cash(self, value: float) -> None:
        self.journal.set("efectivo", f"{value:.2f}")

    def account(self, prices: dict[str, float]) -> Account:
        positions = self.journal.open_positions()
        for p in positions:
            p.current_price = prices.get(p.symbol, p.entry_price)
            p.pnl = _pnl(p, p.current_price)
        equity = self.cash + sum(p.value for p in positions)
        return Account(equity=equity, cash=self.cash, positions=positions)

    def open(self, symbol, instrument_id, is_buy, amount, leverage, price, stop, target, trailing=False) -> Fill:
        if amount > self.cash:
            return Fill(False, message="efectivo insuficiente en la cuenta de papel")
        fill_price = price * (1 + self.cost) if is_buy else price * (1 - self.cost)
        position = Position(
            id=f"P-{uuid.uuid4().hex[:10]}",
            symbol=symbol,
            instrument_id=instrument_id,
            is_buy=is_buy,
            amount=round(amount, 2),
            units=amount * leverage / fill_price,
            leverage=leverage,
            entry_price=fill_price,
            stop=stop,
            target=target,
            opened_at=now_utc(),
            trailing=trailing and stop is not None,
            trail_distance=abs(price - stop) if trailing and stop is not None else None,
        )
        self.journal.add_position(position)
        self._set_cash(self.cash - position.amount)
        return Fill(True, position=position, message=f"abierta en papel a {fill_price:.4f}")

    def close(self, position: Position, price: float | None, reason: str) -> Exit:
        price = price if price is not None else position.current_price or position.entry_price
        exit_price = price * (1 - self.cost) if position.is_buy else price * (1 + self.cost)
        pnl = _pnl(position, exit_price)
        self._set_cash(self.cash + position.amount + pnl)
        self.journal.close_position(position.id, exit_price, pnl, reason)
        return Exit(position, exit_price, pnl, reason)

    def check_exits(self, candles: dict[str, list[Candle]]) -> list[Exit]:
        exits = []
        for p in self.journal.open_positions():
            bars = candles.get(p.symbol) or []
            later = [c for c in bars if c.date.date() > p.opened_at.date()]
            if bars:
                last = bars[-1]  # el precio actual cuenta aunque la vela sea la del día de entrada
                later.append(Candle(last.date, last.close, last.close, last.close, last.close))
            # Se recorre desde el stop inicial cada vez: así el resultado no depende de cuántas
            # veces corra el ciclo (el stop móvil se recalcula igual que en el backtest).
            stop = self.journal.initial_stop(p.id) if p.trailing else p.stop
            label = "stop móvil alcanzado" if p.trailing else "stop alcanzado"
            hit = None
            for c in later:
                if p.is_buy:
                    if stop is not None and c.low <= stop:
                        hit = (min(c.open, stop), label)
                    elif p.target is not None and c.high >= p.target:
                        hit = (max(c.open, p.target), "objetivo alcanzado")
                    elif p.trailing and p.trail_distance:
                        stop = max(stop, c.high - p.trail_distance)
                else:
                    if stop is not None and c.high >= stop:
                        hit = (max(c.open, stop), label)
                    elif p.target is not None and c.low <= p.target:
                        hit = (min(c.open, p.target), "objetivo alcanzado")
                    elif p.trailing and p.trail_distance:
                        stop = min(stop, c.low + p.trail_distance)
                if hit:
                    break
            if hit:
                exits.append(self.close(p, hit[0], hit[1]))
            elif p.trailing and stop is not None and stop != p.stop:
                self.journal.update_stop(p.id, stop)
        return exits


class EtoroBroker(Broker):
    """Cuenta demo o real de eToro. El stop y el objetivo quedan puestos en eToro al abrir."""

    def __init__(self, journal: Journal, client: EtoroClient):
        super().__init__(journal)
        self.client = client
        self.name = client.env
        self.real_money = not client.demo

    def account(self, prices: dict[str, float]) -> Account:
        self._reconcile_pending()
        portfolio = self.client.portfolio()
        raw_positions = ci_get(portfolio, "positions", default=[]) or []
        credit = float(ci_get(portfolio, "credit", default=0) or 0)
        by_id: dict[str, dict] = {}
        invested = unrealized = 0.0
        for raw in raw_positions:
            pid = str(ci_get(raw, "positionID", "positionId"))
            by_id[pid] = raw
            invested += float(ci_get(raw, "amount", default=0) or 0)
            unrealized += float(ci_get(ci_get(raw, "unrealizedPnL", default={}), "pnL", "pnl", default=0) or 0)
        mirrors = 0.0
        for m in ci_get(portfolio, "mirrors", default=[]) or []:
            mirrors += float(ci_get(m, "availableAmount", default=0) or 0)
            mirrors += sum(float(ci_get(p, "amount", default=0) or 0) for p in ci_get(m, "positions", default=[]) or [])
        account_pnl = ci_get(portfolio, "unrealizedPnL")
        if isinstance(account_pnl, (int, float)):
            unrealized = float(account_pnl)
        equity = credit + invested + mirrors + unrealized

        mine, gone = [], []
        for p in self.journal.open_positions():
            raw = by_id.get(p.id)
            if raw is None:
                gone.append(p)
                continue
            pnl_info = ci_get(raw, "unrealizedPnL", default={}) or {}
            p.pnl = float(ci_get(pnl_info, "pnL", "pnl", default=0) or 0)
            live_stop = ci_get(raw, "stopLossRate")
            if live_stop and not ci_get(raw, "isNoStopLoss", default=False) and live_stop != p.stop:
                p.stop = float(live_stop)  # con stop móvil, eToro lo va moviendo
                self.journal.update_stop(p.id, p.stop)
            close_rate = ci_get(pnl_info, "closeRate")
            p.current_price = float(close_rate) if close_rate else prices.get(p.symbol)
            mine.append(p)
        for p in gone:  # eToro la cerró (stop/objetivo) o la cerraste a mano
            price = prices.get(p.symbol)
            pnl = _pnl(p, price) if price else None
            self.journal.close_position(p.id, price, pnl, "cerrada en eToro (stop, objetivo o manual)")
        return Account(equity=equity, cash=credit, positions=mine, closed_externally=gone)

    def _reconcile_pending(self) -> None:
        """Órdenes cuyo resultado no se conoció al enviarlas (p. ej. se cortó la conexión)."""
        for order in self.journal.pending_orders():
            try:
                info = self.client.order_info(reference_id=order["referencia"])
            except EtoroError as exc:
                log.warning("No pude consultar la orden pendiente %s: %s", order["referencia"], exc)
                continue
            self._apply_order_info(order["simbolo"], info, order_row=order)

    def _apply_order_info(
        self, symbol: str, info: dict, order_row=None, stop=None, target=None, trailing: bool = False
    ) -> Fill:
        status = ci_get(ci_get(info, "status", default={}), "id")
        if status in (ORDER_FILLED, ORDER_PARTIAL):
            execs = ci_get(info, "positionExecutions", default=[]) or []
            if not execs:
                return Fill(False, pending=True, message="orden ejecutada pero sin posición todavía", detail=info)
            ex = execs[0]
            opening = ci_get(ex, "openingData", default={}) or {}
            asset = ci_get(info, "asset", default={}) or {}
            price = float(ci_get(opening, "avgPrice", default=0) or 0)
            units = ci_get(opening, "units") or ci_get(ex, "remainingUnits") or 0
            amount = float(ci_get(ex, "investedAmountCurrency", default=0) or ci_get(info, "requestedAmount", default=0) or 0)
            position = Position(
                id=str(ci_get(ex, "positionId", "positionID")),
                symbol=symbol,
                instrument_id=ci_get(asset, "instrumentId"),
                is_buy=str(ci_get(info, "transaction", default="buy")).lower() in ("buy", "buytocover"),
                amount=amount,
                units=float(units),
                leverage=int(ci_get(asset, "leverage", default=1) or 1),
                entry_price=price,
                stop=ci_get(ex, "stopLossRate") if stop is None else stop,
                target=ci_get(ex, "takeProfitRate") if target is None else target,
                opened_at=now_utc(),
                trailing=trailing,
            )
            if trailing and position.stop is not None and price:
                position.trail_distance = abs(price - float(position.stop))
            self.journal.add_position(position)
            if order_row is not None:
                self.journal.update_order(order_row["id"], "ejecutada", position.id, info)
            return Fill(True, position=position, message=f"ejecutada en eToro {self.name} a {price:.4f}", detail=info)
        if status in FINAL_FAILURES:
            st = ci_get(info, "status", default={}) or {}
            msg = ci_get(st, "errorMessage") or ci_get(st, "name") or f"estado {status}"
            if order_row is not None:
                self.journal.update_order(order_row["id"], "rechazada", None, info)
            return Fill(False, message=f"eToro rechazó la orden: {msg}", detail=info)
        return Fill(False, pending=True, message="orden enviada; esperando ejecución", detail=info)

    def open(self, symbol, instrument_id, is_buy, amount, leverage, price, stop, target, trailing=False) -> Fill:
        if instrument_id is None:
            return Fill(False, message=f"no conozco el id de eToro de {symbol}")
        reference = str(uuid.uuid4())
        try:
            ack = self.client.open_market_order(
                instrument_id, is_buy, amount, leverage, stop_loss=stop, take_profit=target, request_id=reference,
                trailing=trailing,
            )
        except EtoroError as exc:
            return Fill(False, message=str(exc), reference=reference)
        except Exception as exc:  # red caída: no sabemos si llegó; se reconcilia después
            log.warning("Error enviando la orden de %s: %s", symbol, exc)
            return Fill(False, pending=True, message=f"no hubo respuesta de eToro ({exc})", reference=reference)
        order_id = ci_get(ack, "orderId", "orderID")
        try:
            info = self.client.wait_for_fill(int(order_id)) if order_id is not None else {}
        except EtoroError as exc:
            log.warning("No pude confirmar la orden %s: %s", order_id, exc)
            info = {}
        fill = self._apply_order_info(symbol, info, stop=stop, target=target, trailing=trailing)
        fill.reference = reference
        fill.broker_order = str(order_id) if order_id is not None else None
        return fill

    def close(self, position: Position, price: float | None, reason: str) -> Exit:
        if position.instrument_id is None:
            raise ValueError(f"la posición {position.id} no tiene instrument_id")
        self.client.close_position(int(position.id), int(position.instrument_id))
        price = position.current_price if position.current_price is not None else price
        pnl = position.pnl if position.current_price is not None else (_pnl(position, price) if price else None)
        self.journal.close_position(position.id, price, pnl, reason)
        return Exit(position, price, pnl, reason)
