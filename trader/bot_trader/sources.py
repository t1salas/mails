"""Fuentes de datos: precios (eToro, Yahoo Finance o CSV) y noticias (RSS)."""

from __future__ import annotations

import csv
import logging
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import httpx

from bot_trader.config import Asset, Settings
from bot_trader.db import Journal
from bot_trader.etoro import EtoroClient, parse_datetime
from bot_trader.models import Candle, Headline

log = logging.getLogger(__name__)
# Yahoo responde 429 a clientes que no parecen un navegador común (comprobado desde un VPS:
# el User-Agent por defecto de httpx da 429 y el de Chrome en Windows da 200).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MAX_FEED_BYTES = 2_000_000


# --- Precios ----------------------------------------------------------------------------


class PriceSource(ABC):
    name = "base"

    @abstractmethod
    def candles(self, asset: Asset) -> list[Candle]:
        """Velas de la más antigua a la más reciente."""


class EtoroPrices(PriceSource):
    name = "etoro"

    def __init__(self, client: EtoroClient, journal: Journal, interval: str = "OneDay", count: int = 1000):
        self.client = client
        self.journal = journal
        self.interval = interval
        self.count = count

    def instrument_id(self, asset: Asset) -> int:
        cached = self.journal.instrument_id(asset.symbol)
        if cached is not None:
            return cached
        found = self.client.resolve(asset.etoro_symbol)
        iid = int(found.get("instrumentId") or found.get("instrumentID"))
        self.journal.save_instrument(asset.symbol, iid)
        return iid

    def candles(self, asset: Asset) -> list[Candle]:
        return self.client.candles(self.instrument_id(asset), self.interval, self.count)


class YahooPrices(PriceSource):
    """Velas diarias de Yahoo Finance (endpoint público no oficial; sirve para papel y backtests)."""

    name = "yahoo"
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self, http: httpx.Client | None = None, range_: str = "5y"):
        self.http = http or httpx.Client(timeout=30.0, headers={"User-Agent": BROWSER_UA})
        self.range = range_

    def candles(self, asset: Asset) -> list[Candle]:
        response = self.http.get(
            self.URL.format(symbol=quote_plus(asset.yahoo_symbol)),
            params={"range": self.range, "interval": "1d", "includePrePost": "false"},
        )
        response.raise_for_status()
        result = (response.json().get("chart") or {}).get("result") or []
        if not result:
            raise LookupError(f"Yahoo no tiene datos de {asset.yahoo_symbol}")
        data = result[0]
        stamps = data.get("timestamp") or []
        quote = ((data.get("indicators") or {}).get("quote") or [{}])[0]
        out = []
        for k, ts in enumerate(stamps):
            try:
                o, h, l, c = (quote[f][k] for f in ("open", "high", "low", "close"))
            except (KeyError, IndexError):
                continue
            if None in (o, h, l, c):
                continue
            v = (quote.get("volume") or [0] * len(stamps))[k] or 0
            out.append(Candle(datetime.fromtimestamp(ts, tz=timezone.utc), o, h, l, c, float(v)))
        return out


class CsvPrices(PriceSource):
    """Lee `<carpeta>/<SIMBOLO>.csv` con columnas date/fecha, open, high, low, close[, volume]."""

    name = "csv"

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def candles(self, asset: Asset) -> list[Candle]:
        path = self.folder / f"{asset.symbol}.csv"
        if not path.is_file():
            raise FileNotFoundError(f"no existe {path}")
        out = []
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                date = r.get("date") or r.get("fecha")
                try:
                    values = [float(r[k]) for k in ("open", "high", "low", "close")]
                except (KeyError, ValueError):
                    continue  # filas con "null" o vacías
                dt = parse_datetime(date)
                try:
                    vol = float(r.get("volume") or 0)
                except ValueError:
                    vol = 0.0
                out.append(Candle(dt, *values, vol))
        out.sort(key=lambda c: c.date)
        return out


def make_price_source(settings: Settings, journal: Journal, client: EtoroClient | None) -> PriceSource:
    kind = settings.effective_price_source
    if kind == "etoro":
        if client is None:
            raise ValueError("fuente_precios = etoro necesita ETORO_API_KEY y ETORO_USER_KEY")
        return EtoroPrices(client, journal, settings.interval, settings.candles)
    if kind == "csv":
        return CsvPrices(settings.csv_dir)
    return YahooPrices()


# --- Noticias ---------------------------------------------------------------------------


def parse_rss(xml_text: str | bytes) -> list[Headline]:
    """Extrae titulares de un RSS 2.0 (Google News, Yahoo Finance...)."""
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        # Google News termina el título con " - Fuente"
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        published = None
        raw_date = item.findtext("pubDate")
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        out.append(Headline(title=title, source=source, published=published, url=(item.findtext("link") or "").strip()))
    return out


class NewsSource:
    """Titulares recientes de Google News (búsqueda) y del RSS de Yahoo Finance (por símbolo)."""

    GOOGLE = "https://news.google.com/rss/search"
    YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline"

    def __init__(self, language: str = "en", http: httpx.Client | None = None):
        self.language = language
        self.http = http or httpx.Client(timeout=20.0, headers={"User-Agent": BROWSER_UA}, follow_redirects=True)

    def _fetch(self, url: str, params: dict) -> list[Headline]:
        try:
            response = self.http.get(url, params=params)
            response.raise_for_status()
            if len(response.content) > MAX_FEED_BYTES:
                raise ValueError("feed demasiado grande")
            return parse_rss(response.content)
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            log.warning("No pude leer noticias de %s: %s", url, exc)
            return []

    def headlines(self, asset: Asset, hours: int = 72, limit: int = 8) -> list[Headline]:
        days = max(1, round(hours / 24))
        if self.language == "es":
            locale = {"hl": "es-419", "gl": "MX", "ceid": "MX:es-419"}
        else:
            locale = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
        items = self._fetch(self.GOOGLE, {"q": f"{asset.news_query} when:{days}d", **locale})
        items += self._fetch(self.YAHOO, {"s": asset.yahoo_symbol, "region": "US", "lang": "en-US"})
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        seen, fresh = set(), []
        for h in sorted(items, key=lambda h: h.published or cutoff, reverse=True):
            key = h.title.lower()[:80]
            if key in seen or (h.published is not None and h.published < cutoff):
                continue
            seen.add(key)
            fresh.append(h)
        return fresh[:limit]
