"""Scrapes TradingView's "market movers" screener pages for 52-week high and
52-week low stocks.

TradingView server-renders these pages (confirmed by fetching them with a
plain HTTP GET — the data table is present in the raw HTML, no JS execution
needed), so a simple requests + BeautifulSoup scrape works. That said,
scraping someone else's HTML is inherently fragile: if TradingView changes
their markup, this can break. The table lookup is written defensively (find
the table via header-cell text like "Symbol" / "Mkt cap", not a hard-coded
CSS class name) so a class-name rename in a future TradingView deploy won't
silently break it — a structural rename of the columns themselves still
would, which is unavoidable for scraping.

Known limitation: TradingView's default (non-JS-paginated) view returns a
capped set of rows (observed ~100, alphabetically sorted by symbol in
testing) rather than the full universe of that day's 52-week high/low
stocks. This is a caveat of the plain-HTTP scraping approach, not a bug —
capturing the full list would require driving a real browser to page
through results.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from screeners.cnbc import cnbc_quote_url
from screeners.market_cap import parse_market_cap

HIGH_URL = "https://www.tradingview.com/markets/stocks-usa/market-movers-52wk-high/"
LOW_URL = "https://www.tradingview.com/markets/stocks-usa/market-movers-52wk-low/"

_HEADERS = {
    # A plain default requests UA gets blocked/served an empty shell by some
    # sites; a normal browser UA string avoids that.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_REQUEST_TIMEOUT_SEC = 15
_CACHE_TTL_SEC = 300  # 5 minutes — these lists don't need to be second-fresh
_SYMBOL_HREF_RE = re.compile(r"/symbols/([A-Z0-9.\-]+)-([A-Z0-9.\-]+)/")


@dataclass
class MoverRow:
    symbol: str
    company: str
    price: float | None
    change_pct: float | None
    market_cap: float | None
    cnbc_url: str


class ScreenerError(RuntimeError):
    """Raised when the TradingView page structure doesn't match what the
    parser expects, so callers get a clear error instead of silently wrong
    data."""


_cache: dict[str, tuple[float, list[MoverRow]]] = {}


def _find_data_table(soup: BeautifulSoup):
    """Locate the market-movers table by its header text rather than a
    specific CSS class, since TradingView's generated class names are
    unstable across deploys."""
    for table in soup.find_all("table"):
        header_text = table.get_text(" ", strip=True).lower()
        if "symbol" in header_text and ("mkt cap" in header_text or "market cap" in header_text):
            return table
    return None


def _parse_row(tr) -> MoverRow | None:
    cells = tr.find_all(["td", "th"])
    if not cells:
        return None

    symbol = None
    company = ""
    link = tr.find("a", href=_SYMBOL_HREF_RE)
    if link:
        m = _SYMBOL_HREF_RE.search(link["href"])
        if m:
            symbol = m.group(2)
        company = link.get_text(strip=True)
        # Some layouts put "SYMBOL\nCompany Name" in the same anchor/cell —
        # split the leading token off as the symbol if we didn't already
        # get one from the href, and strip it from the company text either way.
        first_cell_text = cells[0].get_text(" ", strip=True)
        if not symbol:
            parts = first_cell_text.split(" ", 1)
            if parts:
                symbol = parts[0]
        if company == symbol and len(first_cell_text) > len(symbol or ""):
            company = first_cell_text[len(symbol or ""):].strip()
        elif not company:
            company = first_cell_text[len(symbol or ""):].strip() if symbol else first_cell_text

    if not symbol:
        return None
    symbol = symbol.strip().upper()

    row_text_cells = [c.get_text(" ", strip=True) for c in cells]

    # TradingView renders numeric cells like "1.45 USD" (price), "29.58 M USD"
    # (market cap, with a K/M/B/T unit before "USD"), and "+2.11%" / "−6.20%"
    # (change %, using a Unicode minus sign − for negatives, not a plain
    # hyphen). Matching each pattern strictly avoids misclassifying adjacent
    # numeric-looking columns (Vol, Rel vol, P/E, etc.) as one of these three.
    _MARKET_CAP_RE = re.compile(r"^[\d,.]+\s*[KMBT]\s*USD$", re.IGNORECASE)
    _PRICE_RE = re.compile(r"^[\d,]+\.\d+\s*USD$", re.IGNORECASE)
    _CHANGE_RE = re.compile(r"^[+−-]?\d[\d,]*\.\d+%$")

    price = None
    change_pct = None
    market_cap = None
    for text in row_text_cells:
        if market_cap is None and _MARKET_CAP_RE.match(text):
            market_cap = parse_market_cap(text)
        elif change_pct is None and _CHANGE_RE.match(text):
            try:
                negative = text[0] in "−-"
                num = re.sub(r"[+−%\-]", "", text).replace(",", "")
                change_pct = -float(num) if negative else float(num)
            except ValueError:
                pass
        elif price is None and _PRICE_RE.match(text):
            try:
                price = float(text.split()[0].replace(",", ""))
            except ValueError:
                pass

    return MoverRow(
        symbol=symbol,
        company=company,
        price=price,
        change_pct=change_pct,
        market_cap=market_cap,
        cnbc_url=cnbc_quote_url(symbol),
    )


def _fetch_and_parse(url: str) -> list[MoverRow]:
    resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    table = _find_data_table(soup)
    if table is None:
        raise ScreenerError(
            f"Could not locate the market-movers data table at {url} — "
            "TradingView may have changed their page layout."
        )

    rows: list[MoverRow] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        row = _parse_row(tr)
        if row is not None:
            rows.append(row)

    if not rows:
        raise ScreenerError(
            f"Found the data table at {url} but parsed zero rows from it — "
            "TradingView may have changed their row markup."
        )
    return rows


def _fetch_cached(url: str) -> list[MoverRow]:
    now = time.monotonic()
    cached = _cache.get(url)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]
    rows = _fetch_and_parse(url)
    _cache[url] = (now, rows)
    return rows


def fetch_52_week_high(min_market_cap: float = 1e9) -> list[MoverRow]:
    rows = _fetch_cached(HIGH_URL)
    return [r for r in rows if r.market_cap is not None and r.market_cap > min_market_cap]


def fetch_52_week_low(min_market_cap: float = 100e6) -> list[MoverRow]:
    rows = _fetch_cached(LOW_URL)
    return [r for r in rows if r.market_cap is not None and r.market_cap > min_market_cap]
