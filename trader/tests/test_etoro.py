import json

import httpx
import pytest

from bot_trader.brokers import EtoroBroker
from bot_trader.db import Journal
from bot_trader.etoro import EtoroClient, EtoroError, parse_datetime, short_allowed
from bot_trader.models import Position
from tests.conftest import START


class FakeEtoro:
    """Servidor falso de eToro que registra lo que recibe."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.positions = []
        self.order_status = 3
        self.fail_429_once = False
        self.eligibility = {
            "allowOpenPosition": True, "allowTrailingStopLoss": True,
            "leverageConfigs": [
                {"settlementType": "real", "direction": "long", "leverageValues": [1]},
                {"settlementType": "cfd", "direction": "short", "leverageValues": [1, 2]},
            ],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.fail_429_once:
            self.fail_429_once = False
            return httpx.Response(429, headers={"Retry-After": "0"})
        if path == "/api/v1/me":
            return httpx.Response(200, json={"username": "prueba"})
        if path.endswith("/market-data/search"):
            return httpx.Response(200, json={"items": [
                {"instrumentId": 1001, "internalSymbolFull": "AAPL"},
                {"instrumentId": 9999, "internalSymbolFull": "AAPL.24-7"},
            ]})
        if "/history/candles/" in path:
            return httpx.Response(200, json={"interval": "OneDay", "candles": [{"instrumentId": 1001, "candles": [
                {"instrumentID": 1001, "fromDate": "2026-11-10T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
                {"instrumentID": 1001, "fromDate": "2026-11-11T00:00:00.1234567Z", "open": 1.5, "high": 2, "low": 1, "close": 1.8, "volume": 11},
            ]}]})
        if path.endswith("/pnl"):
            return httpx.Response(200, json={"clientPortfolio": {
                "credit": 5000, "unrealizedPnL": 25.0, "mirrors": [],
                "positions": self.positions,
            }})
        if path.endswith("/orders") and request.method == "POST":
            return httpx.Response(200, json={"token": "t", "orderId": 555, "referenceId": request.headers["x-request-id"]})
        if path.endswith("orders:lookup"):
            return httpx.Response(200, json={
                "orderId": 555, "transaction": "buy",
                "status": {"id": self.order_status, "name": "x", "errorCode": 0, "errorMessage": "fondos insuficientes"},
                "asset": {"instrumentId": 1001, "leverage": 1},
                "positionExecutions": [{"positionId": 777, "investedAmountCurrency": 200,
                                        "openingData": {"avgPrice": 150.0, "units": 1.3333}}],
            })
        if path.endswith("/eligibility"):
            iid = json.loads(request.content)["instrumentIds"][0]
            if self.eligibility is None:
                return httpx.Response(500, json={"title": "caído"})
            return httpx.Response(200, json={"currency": "USD", "eligibilities": [{"instrumentId": iid, **self.eligibility}]})
        if path.endswith("/costs"):
            return httpx.Response(200, json={"instrumentId": 1001, "costs": [
                {"costType": "marketSpread", "amount": 1.5, "currency": "USD"},
                {"costType": "overnightFee", "amount": 0.21, "currency": "USD"},
            ]})
        if "market-close-orders/positions/" in path:
            return httpx.Response(200, json={"token": "c", "orderForClose": {"positionID": 777}})
        return httpx.Response(404, json={"errorMessage": f"no existe {path}"})


@pytest.fixture
def fake():
    return FakeEtoro()


def client(fake, demo=True):
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return EtoroClient("api", "user", demo=demo, http=http, sleep=lambda s: None)


def test_headers_and_resolve(fake):
    c = client(fake)
    inst = c.resolve("aapl")
    assert inst["instrumentId"] == 1001
    req = fake.requests[0]
    assert req.headers["x-api-key"] == "api" and req.headers["x-user-key"] == "user"
    assert len(req.headers["x-request-id"]) == 36
    assert "fields=instrumentId,internalSymbolFull" in str(req.url)  # comas sin codificar
    with pytest.raises(LookupError):
        c.resolve("MSFT")


def test_candles_parsing(fake):
    bars = client(fake).candles(1001, "OneDay", 5000)
    assert "/candles/asc/OneDay/1000" in fake.requests[0].url.path
    assert bars[-1].close == 1.8 and bars[-1].date.microsecond == 123456
    assert parse_datetime("2026-01-01T00:00:00").tzinfo is not None
    # Formatos que Python 3.10 no entiende por sí solo
    assert parse_datetime("2026-01-01T10:20:30.5Z").microsecond == 500000
    assert parse_datetime("2026-01-01 10:20:30.12+00:00").microsecond == 120000
    assert parse_datetime("2026-01-05").day == 5


def test_open_order_body_demo_vs_real(fake):
    c = client(fake)
    c.open_market_order(1001, True, 200.456, 1, stop_loss=140.0, take_profit=180.0, request_id="ref-1")
    req = fake.requests[-1]
    assert req.url.path == "/api/v2/trading/execution/demo/orders"
    body = json.loads(req.content)
    assert body == {
        "action": "open", "transaction": "buy", "instrumentId": 1001, "orderType": "mkt",
        "settlementType": "real", "leverage": 1, "amount": 200.46, "orderCurrency": "usd",
        "stopLossRate": 140.0, "stopLossType": "fixed", "takeProfitRate": 180.0,
    }
    c.open_market_order(1001, True, 100, 1, stop_loss=140.0, trailing=True)
    body = json.loads(fake.requests[-1].content)
    assert body["stopLossType"] == "trailing" and "takeProfitRate" not in body
    assert req.headers["x-request-id"] == "ref-1"
    real = client(fake, demo=False)
    real.open_market_order(1001, False, 100, 1, stop_loss=160.0, trailing=True)
    req = fake.requests[-1]
    assert req.url.path == "/api/v2/trading/execution/orders"
    body = json.loads(req.content)
    # Vender en corto es un CFD, aunque sea sin apalancamiento
    assert body["transaction"] == "sellShort" and body["settlementType"] == "cfd" and body["leverage"] == 1
    assert body["stopLossRate"] == 160.0 and body["stopLossType"] == "trailing"


def test_close_body_casing(fake):
    client(fake).close_position(777, 1001)
    req = fake.requests[-1]
    assert req.url.path == "/api/v1/trading/execution/demo/market-close-orders/positions/777"
    assert json.loads(req.content) == {"InstrumentID": 1001, "UnitsToDeduct": None}
    client(fake, demo=False).close_position(777, 1001)
    assert json.loads(fake.requests[-1].content) == {"InstrumentId": 1001, "UnitsToDeduct": None}


def test_retry_on_429_keeps_request_id(fake):
    fake.fail_429_once = True
    client(fake).me()
    assert len(fake.requests) == 2
    assert fake.requests[0].headers["x-request-id"] == fake.requests[1].headers["x-request-id"]


def test_errors_are_raised(fake):
    with pytest.raises(EtoroError) as exc:
        client(fake).request("GET", "/api/v1/nada")
    assert exc.value.status == 404 and "no existe" in exc.value.message


def test_broker_open_tracks_only_its_positions(fake, tmp_path):
    journal = Journal(tmp_path / "b.db", "demo")
    broker = EtoroBroker(journal, client(fake))
    fill = broker.open("AAPL", 1001, True, 200, 1, 150, 140, 170)
    assert fill.ok and fill.position.id == "777" and fill.position.entry_price == 150.0
    assert fill.position.stop == 140 and fill.position.target == 170
    # La cartera tiene la del bot y una abierta a mano: el bot solo ve la suya
    fake.positions = [
        {"positionID": 777, "instrumentID": 1001, "isBuy": True, "amount": 200, "unrealizedPnL": {"pnL": 12.5, "closeRate": 159.4}},
        {"positionID": 1, "instrumentID": 5, "isBuy": True, "amount": 1000, "unrealizedPnL": {"pnL": 12.5}},
    ]
    acc = broker.account({})
    assert [p.id for p in acc.positions] == ["777"]
    assert acc.positions[0].pnl == 12.5 and acc.positions[0].current_price == 159.4
    assert acc.equity == pytest.approx(5000 + 1200 + 25)
    # eToro la cerró (p. ej. por stop): se marca cerrada en la bitácora
    fake.positions = []
    acc = broker.account({"AAPL": 140.0})
    assert acc.closed_externally and journal.open_positions() == []


def test_broker_rejected_and_close(fake, tmp_path):
    journal = Journal(tmp_path / "b.db", "demo")
    broker = EtoroBroker(journal, client(fake))
    fake.order_status = 4
    fill = broker.open("AAPL", 1001, True, 200, 1, 150, 140, 170)
    assert not fill.ok and "fondos insuficientes" in fill.message
    p = Position("777", "AAPL", True, 200, 1.3, 150, 140, 170, START, instrument_id=1001)
    journal.add_position(p)
    exit_ = broker.close(p, 155.0, "prueba")
    assert exit_.pnl == pytest.approx(6.5)
    assert journal.open_positions() == []


def test_eligibility_and_short_allowed(fake):
    c = client(fake)
    elig = c.eligibility(1001)
    assert fake.requests[-1].url.path == "/api/v2/trading/info/demo/eligibility"
    assert json.loads(fake.requests[-1].content) == {"instrumentIds": [1001]}
    assert short_allowed(elig, 1) is True and short_allowed(elig, 5) is False
    only_long = {"leverageConfigs": [{"settlementType": "real", "direction": "long", "leverageValues": [1]}]}
    assert short_allowed(only_long) is False
    assert short_allowed({}) is None  # eToro no lo informó
    costs = c.cost_estimate(1001, False, 1000)
    assert fake.requests[-1].url.path == "/api/v2/trading/info/demo/costs"
    assert json.loads(fake.requests[-1].content)["transaction"] == "sellShort"
    assert {x["costType"] for x in costs} == {"marketSpread", "overnightFee"}


def test_broker_can_short(fake, tmp_path):
    broker = EtoroBroker(Journal(tmp_path / "b.db", "demo"), client(fake))
    assert broker.can_short("AAPL", 1001) == (True, "")
    fake.eligibility = {"leverageConfigs": [{"settlementType": "real", "direction": "long", "leverageValues": [1]}]}
    ok, why = EtoroBroker(Journal(tmp_path / "c.db", "demo"), client(fake)).can_short("AAPL", 1001)
    assert not ok and "no permite vender en corto" in why
    # Si eToro no responde la consulta, no se bloquea: decide eToro al recibir la orden
    fake.eligibility = None
    assert EtoroBroker(Journal(tmp_path / "d.db", "demo"), client(fake)).can_short("AAPL", 1001)[0]


def test_broker_falls_back_to_fixed_stop_when_trailing_not_allowed(fake, tmp_path):
    fake.eligibility = {"allowTrailingStopLoss": False, "leverageConfigs": []}
    broker = EtoroBroker(Journal(tmp_path / "b.db", "demo"), client(fake))
    fill = broker.open("AAPL", 1001, True, 200, 1, 150, 140, None, trailing=True)
    order = next(r for r in fake.requests if r.url.path.endswith("/orders") and r.method == "POST")
    assert json.loads(order.content)["stopLossType"] == "fixed"
    assert fill.ok and not fill.position.trailing and "stop fijo" in fill.message
