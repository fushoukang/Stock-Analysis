"""Fetches current trading halts from Nasdaq's official Trade Halt RSS feed.

The raw https://www.nasdaqtrader.com/trader.aspx?id=tradehalts page loads its
table via a mechanism not present in a plain HTML fetch, so this uses
Nasdaq's structured RSS/XML feed instead — same underlying data, far more
reliable to parse.

Per Nasdaq's own guidance on that feed: "Data is updated, each trading day
once a minute. Please do not query the data more than once a minute." — this
module enforces a minimum 60s gap between real network fetches via an
in-memory cache.

Only today's (US Eastern date) halts are returned, capped at 5, most-recent
first — per the user's request to keep this list short and current rather
than showing historical halts.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from screeners.cnbc import cnbc_quote_url

FEED_URL = "http://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

_NS = {"ndaq": "http://www.nasdaqtrader.com/"}
_REQUEST_TIMEOUT_SEC = 15
_MIN_FETCH_INTERVAL_SEC = 60  # Nasdaq's stated rate-limit guidance
_MAX_RESULTS = 5
_EASTERN = ZoneInfo("America/New_York")


@dataclass
class HaltRow:
    symbol: str
    company: str
    halt_date: str
    halt_time: str
    market: str
    reason_code: str
    resumption_date: str
    resumption_time: str
    currently_halted: bool
    cnbc_url: str


class ScreenerError(RuntimeError):
    """Raised when the Nasdaq feed structure doesn't match what the parser
    expects, so callers get a clear error instead of silently wrong data."""


_cache: dict[str, object] = {"ts": 0.0, "rows": []}


def _field(item: ET.Element, name: str) -> str:
    el = item.find(f"ndaq:{name}", _NS)
    return (el.text or "").strip() if el is not None and el.text else ""


def _fetch_and_parse() -> list[HaltRow]:
    resp = requests.get(FEED_URL, timeout=_REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        raise ScreenerError(f"Nasdaq halts feed did not return valid XML: {e}") from e

    items = root.findall(".//item")
    if not items:
        raise ScreenerError(
            "Nasdaq halts feed returned no <item> entries — feed format may have changed."
        )

    rows: list[HaltRow] = []
    for item in items:
        symbol = _field(item, "IssueSymbol")
        if not symbol:
            continue
        resumption_date = _field(item, "ResumptionDate")
        rows.append(
            HaltRow(
                symbol=symbol.upper(),
                company=_field(item, "IssueName"),
                halt_date=_field(item, "HaltDate"),
                halt_time=_field(item, "HaltTime"),
                market=_field(item, "Market"),
                reason_code=_field(item, "ReasonCode"),
                resumption_date=resumption_date,
                resumption_time=_field(item, "ResumptionTradeTime"),
                currently_halted=not resumption_date,
                cnbc_url=cnbc_quote_url(symbol),
            )
        )
    return rows


def _fetch_cached() -> list[HaltRow]:
    now = time.monotonic()
    if now - _cache["ts"] < _MIN_FETCH_INTERVAL_SEC and _cache["rows"]:
        return _cache["rows"]  # type: ignore[return-value]
    rows = _fetch_and_parse()
    _cache["ts"] = now
    _cache["rows"] = rows
    return rows


def _is_today(halt_date: str, today: datetime) -> bool:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(halt_date, fmt)
            return parsed.date() == today.date()
        except ValueError:
            continue
    return False


def fetch_current_halts() -> list[HaltRow]:
    """Today's (US Eastern) halts only, most-recent first, capped at 5."""
    rows = _fetch_cached()
    today = datetime.now(_EASTERN)

    todays_rows = [r for r in rows if _is_today(r.halt_date, today)]

    def sort_key(r: HaltRow) -> str:
        return r.halt_time or ""

    todays_rows.sort(key=sort_key, reverse=True)
    return todays_rows[:_MAX_RESULTS]
