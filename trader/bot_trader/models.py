"""Tipos de datos compartidos por todo el bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    """Una vela OHLCV. `date` es el inicio del periodo, en UTC."""

    date: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Headline:
    """Un titular de noticia."""

    title: str
    source: str = ""
    published: datetime | None = None
    url: str = ""


@dataclass
class NewsView:
    """Lectura de las noticias de un activo.

    `score` va de -1 (muy negativas) a +1 (muy positivas). `risk_event` marca noticias
    que cambian el riesgo (resultados trimestrales, demandas, fusiones, investigaciones...).
    """

    symbol: str
    score: float = 0.0
    impact: str = "bajo"  # bajo | medio | alto
    risk_event: bool = False
    summary: str = ""
    method: str = "sin_noticias"  # ia | lexico | sin_noticias
    headlines: list[Headline] = field(default_factory=list)


@dataclass
class Position:
    """Una posición abierta que administra el bot."""

    id: str
    symbol: str
    is_buy: bool
    amount: float  # dinero invertido (USD)
    units: float
    entry_price: float
    stop: float | None
    target: float | None
    opened_at: datetime
    instrument_id: int | None = None
    leverage: int = 1
    current_price: float | None = None
    pnl: float = 0.0
    trailing: bool = False  # stop móvil: sube (o baja, en cortos) detrás del precio
    trail_distance: float | None = None  # distancia fija entre el mejor precio y el stop

    @property
    def exposure(self) -> float:
        """Exposición al mercado (monto x apalancamiento)."""
        return self.amount * self.leverage

    @property
    def value(self) -> float:
        return self.amount + self.pnl


@dataclass
class Account:
    """Foto de la cuenta: capital total, efectivo disponible y posiciones del bot."""

    equity: float
    cash: float
    positions: list[Position] = field(default_factory=list)
    closed_externally: list[Position] = field(default_factory=list)


@dataclass(frozen=True)
class Probability:
    """Estimación de una operación con las mismas reglas de salida que usa el bot.

    - `p_win`: probabilidad de terminar con ganancia (incluye costos).
    - `expectancy_r`: ganancia esperada medida en "R" (1R = lo que se arriesga hasta el stop).
    - `n`: casos históricos parecidos al actual.
    - `p_low`: límite inferior (95 %) de la tasa de acierto en esos casos; con pocos casos baja mucho.
    - `p_hist` / `p_model`: las dos opiniones que se combinan (casos parecidos / regresión logística).
    - `avg_win_r` / `avg_loss_r`: cuánto se gana cuando se gana y cuánto se pierde cuando se pierde.
    """

    p_win: float
    expectancy_r: float
    n: int
    base_p_win: float
    base_expectancy_r: float
    p_low: float
    p_hist: float | None = None
    p_model: float | None = None
    avg_win_r: float = 0.0
    avg_loss_r: float = 1.0
    calibrated: bool = False  # ajustada con el historial de aciertos del propio bot

    @property
    def disagreement(self) -> float:
        if self.p_hist is None or self.p_model is None:
            return 0.0
        return abs(self.p_hist - self.p_model)

    @property
    def kelly(self) -> float:
        """Fracción de Kelly (en unidades de riesgo): ventaja / (ganancia media x pérdida media)."""
        if self.avg_win_r <= 0 or self.avg_loss_r <= 0:
            return 0.0
        return self.expectancy_r / (self.avg_win_r * self.avg_loss_r)


@dataclass(frozen=True)
class Regime:
    """Estado del mercado general (según el activo de referencia, p. ej. SPY)."""

    label: str = "sin referencia"  # alcista | neutral | defensivo | crisis | sin referencia
    score: float = 0.0
    risk_mult: float = 1.0
    applies: bool = False  # True si el activo se mueve junto con el mercado
    detail: str = ""


@dataclass
class Signal:
    """Lo que el bot opina de un activo en este momento."""

    symbol: str
    action: str  # COMPRAR | VENDER_CORTO | CERRAR | MANTENER | ESPERAR
    price: float
    is_buy: bool = True
    stop: float | None = None
    target: float | None = None
    probability: Probability | None = None
    tech_score: float = 0.0
    news_score: float = 0.0
    combined: float = 0.0
    trend: str = "lateral"
    rsi: float | None = None
    atr: float | None = None
    reasons: list[str] = field(default_factory=list)
    news: NewsView | None = None
    regime: Regime | None = None
    trailing: bool = False

    @property
    def opens_position(self) -> bool:
        return self.action in ("COMPRAR", "VENDER_CORTO")
