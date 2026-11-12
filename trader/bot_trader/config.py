"""Configuración: estrategia y riesgo en un TOML; llaves y secretos en variables de entorno (.env).

Todas las llaves del TOML están en español. Cualquier llave desconocida es un error, para que
un error de dedo (por ejemplo `riesgo_por_operacoin`) no pase en silencio con el valor por defecto.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 no trae lector de TOML: se usa el paquete tomli
    import tomli as tomllib

ACCOUNTS = ("papel", "demo", "real")
MODES = ("sugerir", "autonomo")
PRICE_SOURCES = ("auto", "etoro", "yahoo", "csv")
INTERVALS = (
    "OneMinute", "FiveMinutes", "TenMinutes", "FifteenMinutes", "ThirtyMinutes",
    "OneHour", "FourHours", "OneDay", "OneWeek",
)
# Frase exacta que debe tener ETORO_CONFIRMO_DINERO_REAL para operar con dinero real.
REAL_MONEY_PHRASE = "si"


class ConfigError(ValueError):
    """Error en la configuración, con un mensaje pensado para la persona que la escribe."""


def load_dotenv(path: Path) -> None:
    """Carga KEY=VALUE de un .env sin pisar variables ya definidas."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Asset:
    symbol: str
    name: str = ""
    news_query: str = ""
    etoro_symbol: str = ""
    yahoo_symbol: str = ""

    def __post_init__(self) -> None:
        self.symbol = self.symbol.strip().upper()
        self.name = self.name or self.symbol
        self.news_query = self.news_query or f"{self.name} {self.symbol} stock"
        self.etoro_symbol = self.etoro_symbol or self.symbol
        self.yahoo_symbol = self.yahoo_symbol or self.symbol


@dataclass
class RiskParams:
    risk_per_trade: float = 0.0075  # fracción del capital que se pierde si salta el stop
    max_positions: int = 6
    max_asset_exposure: float = 0.20
    max_total_exposure: float = 0.80
    max_daily_loss: float = 0.03  # al perder esto en el día, no abre más hasta mañana
    max_drawdown: float = 0.15  # al caer esto desde el máximo, el bot se pausa
    leverage: int = 1
    allow_short: bool = False
    short_mult: float = 0.5  # una venta en corto arriesga la mitad que una compra
    max_short_exposure: float = 0.30  # máximo del capital vendido en corto a la vez
    min_order: float = 50.0
    max_order_real: float = 250.0  # tope duro por orden en la cuenta real
    bot_capital: float = 0.0  # 0 = usa todo el capital de la cuenta
    min_probability: float = 0.35  # con stop móvil se gana menos veces pero más; manda la expectativa
    min_expectancy_r: float = 0.10
    min_samples: int = 30
    cooldown_hours: float = 24.0
    # --- perfil sostenible ---
    vol_target: float = 0.12  # tope por posición según su volatilidad: lo volátil recibe menos (0 = desactivado)
    max_correlation: float = 0.85  # no abrir algo casi idéntico a lo que ya se tiene
    high_correlation: float = 0.70  # con correlación alta, se arriesga la mitad
    kelly_fraction: float = 0.25  # tope de riesgo = 1/4 del criterio de Kelly (0 = desactivado)
    drawdown_scaling: bool = True  # arriesgar menos mientras más se cae desde el máximo
    defensive_mult: float = 0.5  # multiplicador de riesgo con mercado bajista
    decay_check: bool = True  # pausar si el modelo deja de acertar lo que promete
    decay_min_trades: int = 15


@dataclass
class StrategyParams:
    fast_sma: int = 50
    slow_sma: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    exit_style: str = "seguimiento"  # seguimiento (stop móvil, deja correr ganancias) | objetivo (stop y objetivo fijos)
    stop_atr: float = 3.0
    target_atr: float = 6.0  # solo con exit_style = objetivo
    horizon: int = 120  # velas máximas al simular cada operación histórica
    max_disagreement: float = 0.15  # si los dos modelos difieren más que esto, no hay confianza
    news_weight: float = 0.25
    entry_score: float = 0.25
    exit_score: float = -0.15
    rsi_overbought: float = 75.0
    rsi_oversold: float = 25.0


@dataclass
class NewsParams:
    enabled: bool = True
    use_ai: bool = True
    model: str = "claude-opus-5-5"
    max_headlines: int = 8
    hours: int = 72
    language: str = "en"
    refresh_hours: float = 4.0  # reusar la lectura de noticias durante estas horas (ahorra llamadas)


@dataclass
class Settings:
    account: str = "papel"
    mode: str = "sugerir"
    price_source: str = "auto"
    csv_dir: str = "datos"
    interval: str = "OneDay"
    candles: int = 1000
    paper_capital: float = 10_000.0
    cost_per_side: float = 0.0015
    cfd_annual_cost: float = 0.08  # costo anual de mantener un CFD (cortos y apalancadas); supuesto a verificar
    db_path: str = "trader.db"
    suggestion_ttl_hours: float = 24.0
    max_price_drift: float = 0.015  # al aprobar una sugerencia, cuánto se pudo mover el precio
    reference: str = "SPY"  # activo que define el régimen del mercado ("" = sin régimen)
    decision_hour_utc: int = 21  # decide una vez al día desde esta hora UTC (-1 = en cada ciclo)
    assets: list[Asset] = field(default_factory=list)
    risk: RiskParams = field(default_factory=RiskParams)
    strategy: StrategyParams = field(default_factory=StrategyParams)
    news: NewsParams = field(default_factory=NewsParams)
    # Secretos (solo de variables de entorno)
    etoro_api_key: str = ""
    etoro_user_key: str = ""
    real_money_confirmed: bool = False
    anthropic_configured: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @property
    def has_etoro_keys(self) -> bool:
        return bool(self.etoro_api_key and self.etoro_user_key)

    @property
    def reference_asset(self) -> Asset | None:
        if not self.reference:
            return None
        return self.asset(self.reference) or Asset(self.reference)

    @property
    def effective_price_source(self) -> str:
        if self.price_source != "auto":
            return self.price_source
        return "etoro" if self.has_etoro_keys else "yahoo"

    def asset(self, symbol: str) -> Asset | None:
        symbol = symbol.upper()
        return next((a for a in self.assets if a.symbol == symbol), None)

    def validate(self) -> None:
        if self.account not in ACCOUNTS:
            raise ConfigError(f"cuenta debe ser una de {ACCOUNTS}, no {self.account!r}")
        if self.mode not in MODES:
            raise ConfigError(f"modo debe ser uno de {MODES}, no {self.mode!r}")
        if self.price_source not in PRICE_SOURCES:
            raise ConfigError(f"fuente_precios debe ser una de {PRICE_SOURCES}")
        if self.interval not in INTERVALS:
            raise ConfigError(f"intervalo debe ser uno de {INTERVALS}")
        if not self.assets:
            raise ConfigError("agrega al menos un [[activos]] a la configuración")
        symbols = [a.symbol for a in self.assets]
        if len(symbols) != len(set(symbols)):
            raise ConfigError("hay activos repetidos en [[activos]]")
        r, s = self.risk, self.strategy
        if not 0 < r.risk_per_trade <= 0.05:
            raise ConfigError("riesgo_por_operacion debe estar entre 0 y 0.05 (5 %); lo sano es 0.005-0.02")
        if r.leverage < 1 or r.leverage > 5:
            raise ConfigError("apalancamiento debe estar entre 1 y 5")
        if not 0 < r.max_asset_exposure <= 1 or not 0 < r.max_total_exposure <= r.leverage:
            raise ConfigError("revisa max_por_activo y max_exposicion_total")
        if s.fast_sma >= s.slow_sma:
            raise ConfigError("media_rapida debe ser menor que media_lenta")
        if s.stop_atr <= 0 or s.target_atr <= 0 or s.horizon < 1:
            raise ConfigError("stop_atr, objetivo_atr y horizonte deben ser positivos")
        if not 0 < r.short_mult <= 1 or not 0 <= r.max_short_exposure <= r.max_total_exposure:
            raise ConfigError("multiplicador_cortos debe estar entre 0 y 1, y max_exposicion_cortos no puede "
                              "superar max_exposicion_total")
        if not 0 <= self.cfd_annual_cost <= 0.5:
            raise ConfigError("costo_anual_cfd debe estar entre 0 y 0.5 (50 % anual)")
        if s.exit_style not in ("seguimiento", "objetivo"):
            raise ConfigError('estilo_salida debe ser "seguimiento" u "objetivo"')
        if not -1 <= self.decision_hour_utc <= 23:
            raise ConfigError("hora_decision_utc debe estar entre 0 y 23 (o -1 para decidir en cada ciclo)")
        if not 0 <= r.kelly_fraction <= 1 or not 0 <= r.vol_target <= 1:
            raise ConfigError("fraccion_kelly y volatilidad_objetivo deben estar entre 0 y 1")
        if not 0 < r.high_correlation <= r.max_correlation <= 1:
            raise ConfigError("debe cumplirse 0 < correlacion_alta <= correlacion_maxima <= 1")
        if not 0 <= s.news_weight <= 0.5:
            raise ConfigError("peso_noticias debe estar entre 0 y 0.5: las noticias no deben mandar solas")
        if self.account in ("demo", "real") and not self.has_etoro_keys:
            raise ConfigError(
                f"la cuenta {self.account!r} necesita ETORO_API_KEY y ETORO_USER_KEY en el .env"
            )
        if self.account == "real" and not self.real_money_confirmed:
            raise ConfigError(
                "cuenta = \"real\" opera con DINERO REAL. Para permitirlo agrega "
                f"ETORO_CONFIRMO_DINERO_REAL={REAL_MONEY_PHRASE} al .env (y prueba antes en demo)."
            )


# Llave del TOML -> atributo
_GENERAL = {
    "cuenta": "account",
    "modo": "mode",
    "fuente_precios": "price_source",
    "carpeta_csv": "csv_dir",
    "intervalo": "interval",
    "velas": "candles",
    "capital_papel": "paper_capital",
    "costo_por_lado": "cost_per_side",
    "costo_anual_cfd": "cfd_annual_cost",
    "base_de_datos": "db_path",
    "vigencia_sugerencias_horas": "suggestion_ttl_hours",
    "desvio_maximo_precio": "max_price_drift",
    "referencia": "reference",
    "hora_decision_utc": "decision_hour_utc",
}
_RISK = {
    "riesgo_por_operacion": "risk_per_trade",
    "max_posiciones": "max_positions",
    "max_por_activo": "max_asset_exposure",
    "max_exposicion_total": "max_total_exposure",
    "perdida_diaria_maxima": "max_daily_loss",
    "drawdown_maximo": "max_drawdown",
    "apalancamiento": "leverage",
    "permitir_cortos": "allow_short",
    "multiplicador_cortos": "short_mult",
    "max_exposicion_cortos": "max_short_exposure",
    "monto_minimo": "min_order",
    "monto_maximo_real": "max_order_real",
    "capital_maximo_bot": "bot_capital",
    "probabilidad_minima": "min_probability",
    "expectativa_minima": "min_expectancy_r",
    "muestras_minimas": "min_samples",
    "enfriamiento_horas": "cooldown_hours",
    "volatilidad_objetivo": "vol_target",
    "correlacion_maxima": "max_correlation",
    "correlacion_alta": "high_correlation",
    "fraccion_kelly": "kelly_fraction",
    "reducir_con_drawdown": "drawdown_scaling",
    "multiplicador_defensivo": "defensive_mult",
    "detectar_desgaste": "decay_check",
    "operaciones_min_desgaste": "decay_min_trades",
}
_STRATEGY = {
    "estilo_salida": "exit_style",
    "desacuerdo_maximo": "max_disagreement",
    "media_rapida": "fast_sma",
    "media_lenta": "slow_sma",
    "rsi": "rsi_period",
    "atr": "atr_period",
    "stop_atr": "stop_atr",
    "objetivo_atr": "target_atr",
    "horizonte": "horizon",
    "peso_noticias": "news_weight",
    "umbral_entrada": "entry_score",
    "umbral_salida": "exit_score",
    "rsi_sobrecompra": "rsi_overbought",
    "rsi_sobreventa": "rsi_oversold",
}
_NEWS = {
    "activas": "enabled",
    "usar_ia": "use_ai",
    "modelo": "model",
    "max_titulares": "max_headlines",
    "horas": "hours",
    "idioma": "language",
    "refrescar_horas": "refresh_hours",
}
_ASSET = {
    "simbolo": "symbol",
    "nombre": "name",
    "noticias": "news_query",
    "simbolo_etoro": "etoro_symbol",
    "simbolo_yahoo": "yahoo_symbol",
}


def _apply(target: object, section: dict, mapping: dict[str, str], where: str) -> None:
    types = {f.name: f.type for f in fields(target)}  # type: ignore[arg-type]
    for key, value in section.items():
        if key not in mapping:
            raise ConfigError(f"llave desconocida [{where}] {key!r}. Válidas: {', '.join(mapping)}")
        attr = mapping[key]
        current = getattr(target, attr)
        try:
            if isinstance(current, bool):
                if not isinstance(value, bool):
                    raise TypeError
            elif isinstance(current, int) and not isinstance(current, bool):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError
            elif isinstance(current, float):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError
                value = float(value)
            elif isinstance(current, str) and not isinstance(value, str):
                raise TypeError
        except TypeError:
            raise ConfigError(
                f"[{where}] {key} tiene un tipo inválido ({type(value).__name__}); se espera {types[attr]}"
            ) from None
        setattr(target, attr, value)


def load_settings(path: str | Path | None = None, env_file: str | Path | None = None) -> Settings:
    """Lee el TOML de estrategia y el .env. Sin `path` usa solo valores por defecto."""
    load_dotenv(Path(env_file or os.environ.get("ENV_FILE", ".env")))
    settings = Settings()
    if path is not None:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"no encuentro el archivo de configuración {p}")
        with p.open("rb") as fh:
            data = tomllib.load(fh)
        known = {"general", "riesgo", "estrategia", "noticias", "activos"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"secciones desconocidas: {sorted(unknown)}. Válidas: {sorted(known)}")
        _apply(settings, data.get("general", {}), _GENERAL, "general")
        _apply(settings.risk, data.get("riesgo", {}), _RISK, "riesgo")
        _apply(settings.strategy, data.get("estrategia", {}), _STRATEGY, "estrategia")
        _apply(settings.news, data.get("noticias", {}), _NEWS, "noticias")
        assets = []
        for i, raw in enumerate(data.get("activos", []), start=1):
            if "simbolo" not in raw:
                raise ConfigError(f"el activo #{i} no tiene simbolo")
            kwargs = {}
            for key, value in raw.items():
                if key not in _ASSET:
                    raise ConfigError(
                        f"llave desconocida [activos #{i}] {key!r}. Válidas: {', '.join(_ASSET)}"
                    )
                if not isinstance(value, str):
                    raise ConfigError(f"[activos #{i}] {key} debe ser texto")
                kwargs[_ASSET[key]] = value
            assets.append(Asset(**kwargs))
        settings.assets = assets
        # La base de datos es relativa al archivo de configuración, no al directorio actual.
        if not Path(settings.db_path).is_absolute():
            settings.db_path = str((p.parent / settings.db_path).resolve())
        if not Path(settings.csv_dir).is_absolute():
            settings.csv_dir = str((p.parent / settings.csv_dir).resolve())

    env = os.environ
    settings.etoro_api_key = env.get("ETORO_API_KEY", "").strip()
    settings.etoro_user_key = env.get("ETORO_USER_KEY", "").strip()
    settings.real_money_confirmed = (
        env.get("ETORO_CONFIRMO_DINERO_REAL", "").strip().lower() == REAL_MONEY_PHRASE
    )
    settings.anthropic_configured = bool(
        env.get("ANTHROPIC_API_KEY", "").strip() or env.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    )
    settings.telegram_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    settings.telegram_chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    return settings
