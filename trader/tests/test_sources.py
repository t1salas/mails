import httpx
import pytest

from bot_trader.config import Asset
from bot_trader.sources import CsvPrices, YahooPrices

YAHOO = {
    "chart": {
        "result": [
            {
                "timestamp": [1762732800, 1762819200, 1762905600],
                "indicators": {
                    "quote": [
                        {
                            "open": [100.0, None, 102.0],
                            "high": [101.0, 103.0, 104.0],
                            "low": [99.0, 100.0, 101.0],
                            "close": [100.5, 102.0, 103.5],
                            "volume": [1000, 2000, None],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}


def test_yahoo_prices_skips_incomplete_rows():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=YAHOO)

    src = YahooPrices(http=httpx.Client(transport=httpx.MockTransport(handler)))
    bars = src.candles(Asset("BTC", yahoo_symbol="BTC-USD"))
    assert "/chart/BTC-USD" in seen[0].url.path
    assert [b.close for b in bars] == [100.5, 103.5]
    assert bars[-1].volume == 0.0 and bars[0].date.tzinfo is not None


def test_yahoo_empty_result_is_an_error():
    src = YahooPrices(http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"chart": {"result": None}}))))
    with pytest.raises(LookupError):
        src.candles(Asset("NOPE"))


def test_csv_prices(tmp_path):
    (tmp_path / "AAPL.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "2026-01-06T00:00:00Z,2,3,1,2.5,2.5,10\n"
        "2026-01-05,1,2,0.5,1.5,1.5,null\n"
        "2026-01-07,null,null,null,null,null,null\n",
        encoding="utf-8",
    )
    bars = CsvPrices(tmp_path).candles(Asset("AAPL"))
    assert [b.close for b in bars] == [1.5, 2.5]  # ordenadas y sin filas vacías
    assert bars[0].volume == 0.0
    with pytest.raises(FileNotFoundError):
        CsvPrices(tmp_path).candles(Asset("MSFT"))


def test_default_clients_identify_as_a_browser():
    from bot_trader.sources import BROWSER_UA, NewsSource

    assert "Windows NT" in BROWSER_UA and "Chrome/" in BROWSER_UA
    assert YahooPrices().http.headers["User-Agent"] == BROWSER_UA  # sin esto Yahoo responde 429
    assert NewsSource().http.headers["User-Agent"] == BROWSER_UA
