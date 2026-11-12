"""Cliente de la API pública de eToro (public-api.etoro.com).

Autenticación: cada solicitud lleva `x-api-key`, `x-user-key` y un `x-request-id` único (UUID).
Las llaves se crean en eToro (Configuración > API) y ahí se elige el entorno (Demo/Virtual o Real)
y los permisos (lectura o escritura).

Demo y real comparten host: solo cambia un segmento de la ruta en los endpoints de trading.
Las rutas están todas juntas aquí abajo para ajustarlas en un solo lugar si eToro las cambia.

Notas verificadas del API:
- La búsqueda compara por "contiene": buscar AAPL también trae AAPL.24-7, AAPL.EUR...
- Las velas llegan como máximo 1000 y siempre terminan en la vela más reciente.
- Un id de instrumento desconocido devuelve 200 con "candles": null, no un error.
- Abrir orden: POST v2 .../orders con cuerpo en camelCase. Cerrar posición: POST v1 con
  cuerpo en PascalCase (y la llave del instrumento cambia de mayúsculas entre demo y real).
- El x-request-id de una orden sirve como llave de idempotencia (vuelve como referenceId).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from bot_trader.models import Candle

log = logging.getLogger(__name__)

BASE_URL = "https://public-api.etoro.com"
USER_AGENT = "Mozilla/5.0 (compatible; bot-trader/0.1)"
MAX_CANDLES = 1000
MAX_RETRY_WAIT = 30.0

# Estados de orden (status.id en orders:lookup)
ORDER_FILLED = 3
ORDER_REJECTED = 4
ORDER_PARTIAL = 5
ORDER_CANCELED = 7
ORDER_EXPIRED = 8
FINAL_FAILURES = {ORDER_REJECTED, ORDER_CANCELED, ORDER_EXPIRED, 9, 10}

PATHS = {
    "me": "/api/v1/me",
    "search": "/api/v1/market-data/search",
    "candles": "/api/v1/market-data/instruments/{iid}/history/candles/asc/{interval}/{count}",
    "rates": "/api/v1/market-data/instruments/rates",
    "pnl": {"demo": "/api/v1/trading/info/demo/pnl", "real": "/api/v1/trading/info/real/pnl"},
    "order": {"demo": "/api/v2/trading/execution/demo/orders", "real": "/api/v2/trading/execution/orders"},
    "order_lookup": {
        "demo": "/api/v2/trading/info/demo/orders:lookup",
        "real": "/api/v2/trading/info/orders:lookup",
    },
    "close": {
        "demo": "/api/v1/trading/execution/demo/market-close-orders/positions/{pid}",
        "real": "/api/v1/trading/execution/market-close-orders/positions/{pid}",
    },
}


class EtoroError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"eToro HTTP {status}: {message}")
        self.status = status
        self.message = message


def ci_get(data: Any, *names: str, default: Any = None) -> Any:
    """Lee una llave sin importar mayúsculas (la API mezcla positionID / positionId / PositionId)."""
    if not isinstance(data, dict):
        return default
    for name in names:
        if name in data:
            return data[name]
    lowered = {k.lower(): v for k, v in data.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return default


_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_datetime(value: str) -> datetime:
    """ISO 8601 tolerante (fracciones de 7 dígitos, 'Z', sin zona = UTC)."""
    value = _FRACTION.sub(r"\1", value.strip()).replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        for key in ("errorMessage", "message", "detail", "title"):
            if isinstance(body, dict) and body.get(key):
                return str(body[key])
    except ValueError:
        pass
    text = response.text.strip()
    return text[:300] if text else "sin detalle"


class EtoroClient:
    def __init__(
        self,
        api_key: str,
        user_key: str,
        demo: bool = True,
        base_url: str = BASE_URL,
        http: httpx.Client | None = None,
        sleep=time.sleep,
    ):
        if not api_key or not user_key:
            raise ValueError("faltan ETORO_API_KEY / ETORO_USER_KEY")
        self._api_key = api_key
        self._user_key = user_key
        self.demo = demo
        self.base_url = base_url.rstrip("/")
        # Nunca seguir redirecciones: evita reenviar las llaves a otro host.
        self.http = http or httpx.Client(timeout=30.0, follow_redirects=False)
        self._sleep = sleep

    @property
    def env(self) -> str:
        return "demo" if self.demo else "real"

    # --- transporte -------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        request_id: str | None = None,
    ) -> Any:
        request_id = request_id or str(uuid.uuid4())
        url = self.base_url + path
        if params:
            # La API ignora listas separadas por comas si las comas llegan codificadas (%2C).
            url += "?" + urlencode(params, safe=",")
        headers = {
            "x-api-key": self._api_key,
            "x-user-key": self._user_key,
            "x-request-id": request_id,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        for attempt in range(2):
            response = self.http.request(method, url, headers=headers, json=body)
            if response.status_code == 429 and attempt == 0:
                try:
                    wait = float(response.headers.get("Retry-After", "2"))
                except ValueError:
                    wait = 2.0
                self._sleep(min(max(wait, 0.0), MAX_RETRY_WAIT))
                continue  # mismo x-request-id: es la llave de idempotencia
            if not 200 <= response.status_code < 300:
                raise EtoroError(response.status_code, _error_message(response))
            if not response.content:
                return {}
            return response.json()
        raise EtoroError(429, "límite de solicitudes")  # pragma: no cover

    # --- cuenta y mercado ---------------------------------------------------------------

    def me(self) -> dict:
        return self.request("GET", PATHS["me"])

    def search(self, symbol: str) -> list[dict]:
        data = self.request(
            "GET",
            PATHS["search"],
            params={
                "internalSymbolFull": symbol,
                "fields": "instrumentId,internalSymbolFull,displayname,isCurrentlyTradable,isDelisted,currentRate",
            },
        )
        return list(ci_get(data, "items", default=[]) or [])

    def resolve(self, symbol: str) -> dict:
        """Busca el instrumento cuyo símbolo coincide exactamente (sin importar mayúsculas)."""
        items = self.search(symbol)
        exact = [i for i in items if str(ci_get(i, "internalSymbolFull", default="")).upper() == symbol.upper()]
        if len(exact) == 1:
            return exact[0]
        candidates = ", ".join(str(ci_get(i, "internalSymbolFull")) for i in items[:15])
        if not exact:
            raise LookupError(f"eToro no tiene el símbolo exacto {symbol!r}. Encontré: {candidates or 'nada'}")
        raise LookupError(f"el símbolo {symbol!r} es ambiguo en eToro: {candidates}")

    def candles(self, instrument_id: int, interval: str = "OneDay", count: int = MAX_CANDLES) -> list[Candle]:
        count = max(1, min(int(count), MAX_CANDLES))
        path = PATHS["candles"].format(iid=int(instrument_id), interval=interval, count=count)
        data = self.request("GET", path)
        groups = ci_get(data, "candles") or []
        if not groups or not ci_get(groups[0], "candles"):
            raise LookupError(f"eToro no devolvió velas para el instrumento {instrument_id}")
        out = []
        for c in ci_get(groups[0], "candles"):
            out.append(
                Candle(
                    date=parse_datetime(str(ci_get(c, "fromDate"))),
                    open=float(ci_get(c, "open")),
                    high=float(ci_get(c, "high")),
                    low=float(ci_get(c, "low")),
                    close=float(ci_get(c, "close")),
                    volume=float(ci_get(c, "volume", default=0) or 0),
                )
            )
        return out

    def rates(self, instrument_ids: list[int]) -> list[dict]:
        if not instrument_ids:
            return []
        data = self.request(
            "GET", PATHS["rates"], params={"instrumentIds": ",".join(str(int(i)) for i in instrument_ids)}
        )
        return list(ci_get(data, "rates", default=[]) or [])

    def portfolio(self) -> dict:
        """Cartera con P&L en vivo: {positions, credit, unrealizedPnL, ...}."""
        data = self.request("GET", PATHS["pnl"][self.env])
        return ci_get(data, "clientPortfolio", default=data) or {}

    # --- órdenes ------------------------------------------------------------------------

    def open_market_order(
        self,
        instrument_id: int,
        is_buy: bool,
        amount: float,
        leverage: int = 1,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        request_id: str | None = None,
    ) -> dict:
        """Abre una posición a mercado por monto en USD. Devuelve {token, orderId, referenceId}."""
        if amount <= 0:
            raise ValueError("el monto debe ser positivo")
        # Comprar sin apalancamiento = acción/cripto real; corto o apalancado = CFD.
        settlement = "real" if is_buy and leverage == 1 else "cfd"
        body: dict[str, Any] = {
            "action": "open",
            "transaction": "buy" if is_buy else "sell",
            "instrumentId": int(instrument_id),
            "orderType": "mkt",
            "settlementType": settlement,
            "leverage": int(leverage),
            "amount": round(float(amount), 2),
            "orderCurrency": "usd",
        }
        if stop_loss is not None:
            body["stopLossRate"] = float(stop_loss)
        if take_profit is not None:
            body["takeProfitRate"] = float(take_profit)
        request_id = request_id or str(uuid.uuid4())
        data = self.request("POST", PATHS["order"][self.env], body=body, request_id=request_id)
        data.setdefault("referenceId", request_id)
        return data

    def order_info(self, order_id: int | None = None, reference_id: str | None = None) -> dict:
        params = {"orderId": order_id} if order_id is not None else {"referenceId": reference_id}
        return self.request("GET", PATHS["order_lookup"][self.env], params=params)

    def wait_for_fill(self, order_id: int, attempts: int = 8, delay: float = 1.5) -> dict:
        """Consulta la orden hasta que se ejecute o falle. Devuelve el último estado."""
        info: dict = {}
        for _ in range(attempts):
            info = self.order_info(order_id=order_id)
            status = ci_get(ci_get(info, "status", default={}), "id")
            if status in (ORDER_FILLED, ORDER_PARTIAL) or status in FINAL_FAILURES:
                return info
            self._sleep(delay)
        return info

    def close_position(self, position_id: int, instrument_id: int, units: float | None = None) -> dict:
        """Cierra una posición a mercado (toda si `units` es None)."""
        path = PATHS["close"][self.env].format(pid=int(position_id))
        key = "InstrumentID" if self.demo else "InstrumentId"
        return self.request("POST", path, body={key: int(instrument_id), "UnitsToDeduct": units})
