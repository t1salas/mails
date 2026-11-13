"""El agente: datos -> análisis -> riesgo -> sugerir o ejecutar -> bitácora.

Funciona solo, como un gestor de capital disciplinado:
  - VIGILA en cada ciclo (p. ej. cada 30 min): stops, cierres hechos por eToro y frenos de riesgo.
  - DECIDE una vez al día, después del cierre del mercado (`hora_decision_utc`), con las velas del
    día ya cerradas, igual que en el backtest. Operar menos seguido = menos costos y menos ruido.

Orden de cada decisión:
  1. Revisar salidas primero (stops, tesis rotas). Proteger capital va antes que ganar.
  2. Revisar los frenos de la cuenta (pérdida diaria, drawdown, pausa manual) y si el modelo sigue
     acertando lo que promete (si no, se pausa solo: "desgaste del modelo").
  3. Buscar entradas, ordenadas por ganancia esperada, y dimensionarlas con el perfil sostenible
     (volatilidad objetivo, correlaciones, Kelly fraccional, drawdown y régimen de mercado).
  4. En modo "sugerir" solo se anotan y avisan; en modo "autonomo" se ejecutan.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from bot_trader.analysis.analyst import Analyst
from bot_trader.analysis.sentiment import ClaudeNewsAnalyzer, lexicon_view
from bot_trader.brokers import Broker, EtoroBroker, Exit, PaperBroker
from bot_trader.config import Asset, Settings
from bot_trader.db import Journal, from_iso, iso, now_utc
from bot_trader.etoro import EtoroClient
from bot_trader.models import Account, Candle, Headline, NewsView, Position, Regime, Signal
from bot_trader.risk import Guard, RiskState, TradeContext, bet_correlation, check_guards, size_trade
from bot_trader.sources import NewsSource, PriceSource, make_price_source

log = logging.getLogger(__name__)


@dataclass
class Analysis:
    asset: Asset
    signal: Signal | None = None
    error: str | None = None


@dataclass
class Action:
    """Algo que el bot hizo o propone hacer."""

    kind: str  # ejecutada | pendiente | sugerida | bloqueada | fallida
    symbol: str
    action: str
    detail: str
    signal_id: int | None = None
    amount: float | None = None
    new: bool = True  # False si repite una sugerencia que ya estaba pendiente


@dataclass
class CycleResult:
    account: str
    mode: str
    equity: float = 0.0
    cash: float = 0.0
    positions: list[Position] = field(default_factory=list)
    guard: Guard | None = None
    analyses: list[Analysis] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    exits: list[Exit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    newly_paused: bool = False
    decision_round: bool = False
    regime: Regime | None = None
    model_info: str = ""

    @property
    def noteworthy(self) -> bool:
        """¿Vale la pena avisar? Una vez al día (la decisión) o si pasó algo nuevo."""
        fresh = any(
            a.kind in ("ejecutada", "pendiente", "fallida") or (a.kind == "sugerida" and a.new) for a in self.actions
        )
        return bool(self.exits or fresh or self.newly_paused or self.decision_round)


class Bot:
    def __init__(
        self,
        settings: Settings,
        journal: Journal,
        broker: Broker,
        prices: PriceSource,
        news_source: NewsSource | None = None,
        news_analyzer: ClaudeNewsAnalyzer | None = None,
        etoro: EtoroClient | None = None,
        clock=now_utc,
    ):
        self.s = settings
        self.journal = journal
        self.broker = broker
        self.prices = prices
        self.news_source = news_source
        self.news_analyzer = news_analyzer
        self.etoro = etoro
        self.clock = clock
        self.risk = RiskState(journal)

    # --- datos y análisis ----------------------------------------------------------------

    def instrument_id(self, asset: Asset) -> int | None:
        if self.etoro is None:
            return None
        cached = self.journal.instrument_id(asset.symbol)
        if cached is not None:
            return cached
        found = self.etoro.resolve(asset.etoro_symbol)
        iid = int(found.get("instrumentId") or found.get("instrumentID"))
        self.journal.save_instrument(asset.symbol, iid)
        return iid

    def fetch_candles(self, assets: list[Asset]) -> tuple[dict[str, list[Candle]], dict[str, str]]:
        candles, errors = {}, {}
        for asset in assets:
            try:
                bars = self.prices.candles(asset)
                if not bars:
                    raise LookupError("sin datos")
                candles[asset.symbol] = bars
            except Exception as exc:  # un activo con problemas no debe tumbar el ciclo
                errors[asset.symbol] = f"no pude obtener precios: {exc}"
                log.warning("%s: %s", asset.symbol, errors[asset.symbol])
        return candles, errors

    def read_news(self, assets: list[Asset]) -> dict[str, NewsView]:
        if not self.s.news.enabled or self.news_source is None:
            return {}
        cached = self._cached_news({a.symbol for a in assets})
        if cached is not None:
            return cached
        items = []
        for asset in assets:
            headlines = self.news_source.headlines(asset, self.s.news.hours, self.s.news.max_headlines)
            items.append((asset, headlines))
        if self.news_analyzer is not None:
            views = self.news_analyzer.analyze(items)
        else:
            views = {asset.symbol: lexicon_view(asset.symbol, h) for asset, h in items}
        self._store_news(views)
        return views

    def _cached_news(self, symbols: set[str]) -> dict[str, NewsView] | None:
        """Reusa la última lectura de noticias si es reciente y cubre los mismos activos."""
        raw = self.journal.get("noticias_cache")
        if not raw or self.s.news.refresh_hours <= 0:
            return None
        try:
            data = json.loads(raw)
            if self.clock() - from_iso(data["ts"]) > timedelta(hours=self.s.news.refresh_hours):
                return None
            if set(data["views"]) != symbols:
                return None
            views = {}
            for symbol, v in data["views"].items():
                headlines = [
                    Headline(h["title"], h["source"], from_iso(h["published"]) if h["published"] else None, h["url"])
                    for h in v.pop("headlines")
                ]
                views[symbol] = NewsView(headlines=headlines, **v)
            return views
        except (KeyError, TypeError, ValueError):
            return None

    def _store_news(self, views: dict[str, NewsView]) -> None:
        payload = {"ts": iso(self.clock()), "views": {}}
        for symbol, v in views.items():
            d = asdict(v)
            d["headlines"] = [
                {"title": h.title, "source": h.source, "url": h.url,
                 "published": iso(h.published) if h.published else None}
                for h in v.headlines
            ]
            payload["views"][symbol] = d
        self.journal.set("noticias_cache", json.dumps(payload, ensure_ascii=False))

    def fetch_market(self) -> tuple[dict[str, list[Candle]], dict[str, str], list[Candle] | None]:
        """Velas de la lista de activos y del activo de referencia del mercado."""
        candles, errors = self.fetch_candles(self.s.assets)
        ref = self.s.reference_asset
        reference = None
        if ref is not None:
            if ref.symbol in candles:
                reference = candles[ref.symbol]
            else:
                extra, _ = self.fetch_candles([ref])
                reference = extra.get(ref.symbol)
        return candles, errors, reference

    def build_analyst(self, candles: dict[str, list[Candle]], reference: list[Candle] | None) -> Analyst:
        analyst = Analyst(candles, self.s, reference)
        last = max((bars[-1].date.date() for bars in candles.values() if bars), default=None)
        if last is not None:
            analyst.train(last)
        return analyst

    def analyze(
        self,
        analyst: Analyst,
        errors: dict[str, str],
        positions: list[Position],
        news: dict[str, NewsView] | None = None,
    ) -> list[Analysis]:
        by_symbol = {p.symbol: p for p in positions}
        out = []
        for asset in self.s.assets:
            if asset.symbol in errors or asset.symbol not in analyst.feats:
                out.append(Analysis(asset, error=errors.get(asset.symbol, "sin datos")))
                continue
            t = len(analyst.feats[asset.symbol]) - 1
            signal = analyst.evaluate(asset.symbol, t, by_symbol.get(asset.symbol), (news or {}).get(asset.symbol))
            out.append(Analysis(asset, signal=signal))
        return out

    def trade_context(
        self, analyst: Analyst, sig: Signal, positions: list[Position], equity: float, peak: float
    ) -> TradeContext:
        symbol = sig.symbol
        t = len(analyst.feats[symbol]) - 1
        d = analyst.dates[symbol][t]
        correlations = {}
        for p in positions:
            if p.symbol != symbol:
                c = analyst.correlation(symbol, p.symbol, d)
                if c is not None:
                    correlations[p.symbol] = bet_correlation(c, p.is_buy == sig.is_buy)
        return TradeContext(
            drawdown=max(0.0, 1 - equity / peak) if peak > 0 else 0.0,
            regime=analyst.regime_for(symbol, d),
            annual_vol=analyst.annual_vol(symbol, t),
            correlations=correlations,
            target_weight=analyst.core_weight(symbol, d) if self.s.strategy.kind == "nucleo" else None,
        )

    @staticmethod
    def model_summary(analyst: Analyst) -> str:
        model = analyst.models.get(True)
        if model is None:
            return "modelo estadístico: todavía sin datos suficientes (se usan solo los casos parecidos)"
        return f"modelo estadístico entrenado con {model.n:,} operaciones históricas de {len(analyst.feats)} activos"

    # --- autonomía -----------------------------------------------------------------------

    def is_decision_time(self, now: datetime) -> bool:
        hour = self.s.decision_hour_utc
        if hour < 0:
            return True
        return now.hour >= hour and self.journal.get("ultima_decision") != now.date().isoformat()

    def model_decay(self) -> str | None:
        """¿Las operaciones reales ganan mucho menos de lo que el modelo prometía?

        Compara ganadoras reales contra las esperadas (suma de probabilidades). Si la diferencia
        es estadísticamente fuerte (z < -2), el modelo dejó de describir el mercado actual.
        """
        rows = self.journal.closed_trades_with_probability(100)
        if len(rows) < self.s.risk.decay_min_trades:
            return None
        expected = sum(r["probabilidad"] for r in rows)
        actual = sum(1 for r in rows if r["pnl"] > 0)
        var = sum(r["probabilidad"] * (1 - r["probabilidad"]) for r in rows)
        if var <= 0:
            return None
        z = (actual - expected) / math.sqrt(var)
        if z < -2:
            return (
                f"desgaste del modelo: en las últimas {len(rows)} operaciones ganaron {actual} y se esperaban "
                f"{expected:.1f} (z = {z:.1f}). El mercado cambió o la estrategia dejó de funcionar: revisar antes de seguir"
            )
        return None

    # --- capital -------------------------------------------------------------------------

    def bot_equity(self, account: Account) -> tuple[float, float]:
        """(capital, efectivo) que el bot puede usar. Respeta `capital_maximo_bot` si existe."""
        cap = self.s.risk.bot_capital
        if cap <= 0:
            return account.equity, account.cash
        equity = cap + self.journal.realized_pnl() + sum(p.pnl for p in account.positions)
        free = equity - sum(p.amount for p in account.positions)
        return equity, max(min(account.cash, free), 0.0)

    # --- ciclo ---------------------------------------------------------------------------

    def run_cycle(self, force_decision: bool = False) -> CycleResult:
        now = self.clock()
        result = CycleResult(account=self.s.account, mode=self.s.mode)
        expired = self.journal.expire_suggestions(now - timedelta(hours=self.s.suggestion_ttl_hours))
        if expired:
            result.notes.append(f"{expired} sugerencia(s) vencieron sin aprobarse")

        candles, errors, reference = self.fetch_market()
        prices = {sym: bars[-1].close for sym, bars in candles.items()}

        # 1. Salidas automáticas (en papel; en eToro las ejecuta el broker y se detectan abajo)
        result.exits.extend(self.broker.check_exits(candles))
        account = self.broker.account(prices)
        for p in account.closed_externally:
            result.exits.append(Exit(p, None, None, "cerrada en eToro (stop, objetivo o manual)"))
        positions = list(account.positions)

        # 2. Frenos de la cuenta
        equity, cash = self.bot_equity(account)
        peak, day_start = self.risk.update(equity, now.date())
        guard = check_guards(equity, peak, day_start, self.risk.paused, self.s.risk)
        if guard.paused and not self.risk.paused:
            self.risk.pause(guard.reason)
            result.newly_paused = True
        result.guard = guard

        decision = force_decision or self.is_decision_time(now)
        result.decision_round = decision
        if not decision:
            hour = self.s.decision_hour_utc
            when = "hoy" if now.hour < hour else "mañana"
            result.notes.append(f"Ronda de vigilancia. La próxima decisión es {when} desde las {hour:02d}:00 UTC.")
            return self._finish(result, account, prices, changed=bool(result.exits))

        if self.s.risk.decay_check and not self.risk.paused:
            decay = self.model_decay()
            if decay:
                self.risk.pause(decay)
                result.newly_paused = True
                guard = check_guards(equity, peak, day_start, True, self.s.risk)
                result.guard = guard

        # 3. Análisis
        analyst = self.build_analyst(candles, reference)
        news = self.read_news([a for a in self.s.assets if a.symbol in candles])
        result.analyses = self.analyze(analyst, errors, positions, news)
        if analyst.ref is not None:
            result.regime = analyst.market_regime(analyst.ref_dates[-1])
        result.model_info = self.model_summary(analyst)

        # 4. Cierres por tesis rota
        for a in result.analyses:
            sig = a.signal
            if sig is None or sig.action != "CERRAR":
                continue
            position = next(p for p in positions if p.symbol == sig.symbol)
            reason = "; ".join(sig.reasons[:2])
            if self.s.mode == "autonomo":
                exit_ = self._close(position, sig.price, reason)
                if exit_ is not None:
                    result.exits.append(exit_)
                    positions.remove(position)
                    cash += position.amount + (exit_.pnl or 0)
                else:
                    result.actions.append(Action("fallida", sig.symbol, "CERRAR", "no se pudo cerrar; ver log"))
            else:
                result.actions.append(self._suggest(sig, None, reason))

        # 5. Entradas, la de mayor ganancia esperada primero
        entries = [a.signal for a in result.analyses if a.signal and a.signal.opens_position]
        entries.sort(key=lambda s: s.probability.expectancy_r if s.probability else 0, reverse=True)
        for sig in entries:
            if not guard.can_open:
                result.actions.append(Action("bloqueada", sig.symbol, sig.action, guard.reason))
                continue
            last_close = self.journal.last_close_time(sig.symbol)
            if last_close and now - last_close < timedelta(hours=self.s.risk.cooldown_hours):
                result.actions.append(
                    Action("bloqueada", sig.symbol, sig.action, "enfriamiento: se cerró hace poco una posición aquí")
                )
                continue
            if not sig.is_buy:
                allowed, why = self._can_short(sig.symbol)
                if not allowed:
                    result.actions.append(Action("bloqueada", sig.symbol, sig.action, why))
                    continue
            ctx = self.trade_context(analyst, sig, positions, equity, peak)
            size = size_trade(equity, cash, positions, sig, self.s.risk, ctx, real_account=self.broker.real_money)
            if not size.ok:
                result.actions.append(Action("bloqueada", sig.symbol, sig.action, size.reason))
                continue
            if self.s.mode == "autonomo":
                action = self._open(sig, size.amount, size.reason)
            else:
                action = self._suggest(sig, size.amount, size.reason)
            result.actions.append(action)
            if action.kind in ("ejecutada", "pendiente", "sugerida"):
                # Reservar capital y lugar para que las siguientes entradas respeten los límites
                cash -= size.amount
                positions.append(
                    Position(id=f"reserva-{sig.symbol}", symbol=sig.symbol, is_buy=sig.is_buy, amount=size.amount,
                             units=0, entry_price=sig.price, stop=sig.stop, target=sig.target, opened_at=now,
                             leverage=self.s.risk.leverage)
                )

        self.journal.set("ultima_decision", now.date().isoformat())
        changed = bool(result.exits) or any(x.kind in ("ejecutada", "pendiente") for x in result.actions)
        return self._finish(result, account, prices, changed)

    def _finish(self, result: CycleResult, account: Account, prices: dict[str, float], changed: bool) -> CycleResult:
        final = self.broker.account(prices) if changed else account
        result.equity, result.cash = self.bot_equity(final)
        result.positions = final.positions
        self.journal.record_equity(result.equity)
        return result

    # --- ejecución -----------------------------------------------------------------------

    def _can_short(self, symbol: str) -> tuple[bool, str]:
        """¿El broker permite vender en corto este activo? (En eToro depende del activo y del país.)"""
        asset = self.s.asset(symbol)
        try:
            iid = self.instrument_id(asset) if asset and isinstance(self.broker, EtoroBroker) else None
        except Exception as exc:
            return False, f"no encontré el instrumento en eToro: {exc}"
        return self.broker.can_short(symbol, iid, self.s.risk.leverage)

    def _suggest(self, sig: Signal, amount: float | None, note: str) -> Action:
        existing = self.journal.pending_suggestion(sig.symbol, sig.action)
        if existing is not None:
            return Action("sugerida", sig.symbol, sig.action, note, signal_id=existing["id"], amount=amount, new=False)
        signal_id = self.journal.save_signal(sig, amount, "sugerida")
        return Action("sugerida", sig.symbol, sig.action, note, signal_id=signal_id, amount=amount)

    def _open(self, sig: Signal, amount: float, note: str, signal_id: int | None = None) -> Action:
        asset = self.s.asset(sig.symbol)
        try:
            iid = self.instrument_id(asset) if asset and isinstance(self.broker, EtoroBroker) else None
        except Exception as exc:
            return Action("fallida", sig.symbol, sig.action, f"no encontré el instrumento en eToro: {exc}")
        if signal_id is None:
            signal_id = self.journal.save_signal(sig, amount, "enviada")
        fill = self.broker.open(
            sig.symbol, iid, sig.is_buy, amount, self.s.risk.leverage, sig.price, sig.stop, sig.target,
            trailing=sig.trailing,
        )
        state = "ejecutada" if fill.ok else "pendiente" if fill.pending else "rechazada"
        self.journal.save_order(
            "abrir", sig.symbol, amount, state, signal_id=signal_id, reference=fill.reference,
            broker_order=fill.broker_order, position_id=fill.position.id if fill.position else None,
            detail=fill.detail,
        )
        self.journal.set_signal_state(signal_id, state, fill.position.id if fill.position else None)
        kind = "ejecutada" if fill.ok else "pendiente" if fill.pending else "fallida"
        return Action(kind, sig.symbol, sig.action, f"{fill.message}. {note}", signal_id=signal_id, amount=amount)

    def _close(self, position: Position, price: float | None, reason: str) -> Exit | None:
        try:
            exit_ = self.broker.close(position, price, reason)
        except Exception as exc:
            log.error("No pude cerrar %s (%s): %s", position.symbol, position.id, exc)
            self.journal.save_order("cerrar", position.symbol, position.amount, "fallida", position_id=position.id,
                                    detail={"error": str(exc)})
            return None
        self.journal.save_order("cerrar", position.symbol, position.amount, "ejecutada", position_id=position.id,
                                detail={"motivo": reason, "pnl": exit_.pnl})
        return exit_

    def approve(self, signal_id: int) -> str:
        """Ejecuta una sugerencia guardada, si sigue vigente y el precio no se movió demasiado."""
        row = self.journal.signal(signal_id)
        if row is None:
            return f"No existe la sugerencia #{signal_id} en la cuenta {self.s.account}."
        if row["estado"] != "sugerida":
            return f"La sugerencia #{signal_id} está '{row['estado']}', no se puede aprobar."
        age = self.clock() - from_iso(row["ts"])
        if age > timedelta(hours=self.s.suggestion_ttl_hours):
            self.journal.set_signal_state(signal_id, "vencida")
            return f"La sugerencia #{signal_id} venció (tiene {age}). Corre un ciclo nuevo."
        asset = self.s.asset(row["simbolo"])
        if asset is None:
            return f"{row['simbolo']} ya no está en la configuración."
        candles, errors = self.fetch_candles([asset])
        if errors:
            return errors[asset.symbol]
        price = candles[asset.symbol][-1].close
        account = self.broker.account({asset.symbol: price})

        if row["accion"] == "CERRAR":
            position = next((p for p in account.positions if p.symbol == asset.symbol), None)
            if position is None:
                self.journal.set_signal_state(signal_id, "descartada")
                return f"Ya no hay posición abierta en {asset.symbol}."
            exit_ = self._close(position, price, f"aprobada sugerencia #{signal_id}")
            if exit_ is None:
                return f"No se pudo cerrar {asset.symbol}; revisa el log."
            self.journal.set_signal_state(signal_id, "ejecutada", position.id)
            pnl = f"{exit_.pnl:+,.2f} USD" if exit_.pnl is not None else "P&L por confirmar"
            return f"Cerrada la posición de {asset.symbol} ({pnl})."

        # Antes de ejecutar, se vuelve a validar la señal con datos frescos.
        candles, errors, reference = self.fetch_market()
        if asset.symbol not in candles:
            return errors.get(asset.symbol, f"sin precios de {asset.symbol}")
        analyst = self.build_analyst(candles, reference)
        news = self.read_news([a for a in self.s.assets if a.symbol in candles])
        t = len(analyst.feats[asset.symbol]) - 1
        sig = analyst.evaluate(asset.symbol, t, None, news.get(asset.symbol))
        is_buy = bool(row["es_compra"])
        drift = (price / row["precio"] - 1) * (1 if is_buy else -1)
        if drift > self.s.max_price_drift:
            return (
                f"El precio de {asset.symbol} ya se movió {drift:+.1%} a favor desde la sugerencia "
                f"({row['precio']:.2f} -> {price:.2f}); no persigo el precio. Espera otra señal."
            )
        if sig.action != row["accion"]:
            self.journal.set_signal_state(signal_id, "descartada")
            why = "; ".join(sig.reasons[:3])
            return f"La señal de {asset.symbol} ya no es válida con los datos de ahora ({why}). Sugerencia descartada."
        account = self.broker.account({s: b[-1].close for s, b in candles.items()})
        equity, cash = self.bot_equity(account)
        peak, day_start = self.risk.update(equity, self.clock().date())
        guard = check_guards(equity, peak, day_start, self.risk.paused, self.s.risk)
        if not guard.can_open:
            return f"No se puede abrir: {guard.reason}"
        if not sig.is_buy:
            allowed, why = self._can_short(asset.symbol)
            if not allowed:
                return f"No se puede abrir: {why}"
        ctx = self.trade_context(analyst, sig, account.positions, equity, peak)
        size = size_trade(equity, cash, account.positions, sig, self.s.risk, ctx, real_account=self.broker.real_money)
        if not size.ok:
            return f"No se puede abrir: {size.reason}"
        action = self._open(sig, size.amount, size.reason, signal_id=signal_id)
        return f"{action.kind.upper()}: {action.action} {asset.symbol} por {size.amount:,.2f} USD. {action.detail}"

    def close_all(self, reason: str = "cierre manual de todas las posiciones", symbol: str | None = None) -> list[str]:
        """Cierra las posiciones del bot (todas, o solo la de `symbol`). Devuelve una línea por posición."""
        candles, _ = self.fetch_candles([a for a in self.s.assets])
        prices = {sym: bars[-1].close for sym, bars in candles.items()}
        account = self.broker.account(prices)
        out = []
        for p in account.positions:
            if symbol is not None and p.symbol != symbol.upper():
                continue
            exit_ = self._close(p, prices.get(p.symbol), reason)
            if exit_ is None:
                out.append(f"{p.symbol}: ERROR al cerrar")
            else:
                pnl = f"{exit_.pnl:+,.2f} USD" if exit_.pnl is not None else "P&L por confirmar en eToro"
                out.append(f"{p.symbol}: cerrada ({pnl})")
        return out

    def open_position_symbols(self) -> list[str]:
        return [p.symbol for p in self.journal.open_positions()]


def build_bot(settings: Settings) -> Bot:
    """Arma el bot con los componentes que pide la configuración."""
    settings.validate()
    journal = Journal(settings.db_path, settings.account)
    client = None
    if settings.has_etoro_keys:
        client = EtoroClient(settings.etoro_api_key, settings.etoro_user_key, demo=settings.account != "real")
    if settings.account == "papel":
        broker: Broker = PaperBroker(
            journal, settings.paper_capital, settings.cost_per_side, settings.cfd_annual_cost
        )
    else:
        broker = EtoroBroker(journal, client)
    prices = make_price_source(settings, journal, client)
    news_source = NewsSource(settings.news.language) if settings.news.enabled else None
    analyzer = None
    if settings.news.enabled and settings.news.use_ai and settings.anthropic_configured:
        if importlib.util.find_spec("anthropic") is not None:
            analyzer = ClaudeNewsAnalyzer(settings.news.model)
        else:
            log.warning("Falta el paquete anthropic (pip install -e '.[ia]'); uso palabras clave.")
    return Bot(settings, journal, broker, prices, news_source, analyzer, etoro=client)


def describe_age(ts: str, now: datetime) -> str:
    minutes = int((now - from_iso(ts)).total_seconds() // 60)
    if minutes < 60:
        return f"hace {minutes} min"
    if minutes < 48 * 60:
        return f"hace {minutes // 60} h"
    return f"hace {minutes // 1440} días"
