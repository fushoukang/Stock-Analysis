"""Company/asset name lookup via Alpaca's Trading API (Assets endpoint).

Names essentially never change during a run, so results are cached in memory
per symbol — this avoids an extra Alpaca API round trip on every chart load
(the frontend re-fetches the chart on every live bar and on a 30s poll).
"""
from __future__ import annotations

import logging

from alpaca.trading.client import TradingClient

from config import settings, is_within_market_data_window

logger = logging.getLogger("data.company_names")

_client: TradingClient | None = None
_cache: dict[str, str | None] = {}


def _get_client() -> TradingClient | None:
    global _client
    if not settings.has_credentials():
        return None
    if _client is None:
        _client = TradingClient(settings.api_key, settings.secret_key, paper=settings.paper)
    return _client


def get_company_name(symbol: str) -> str | None:
    """Returns the asset's display name (e.g. 'Apple Inc. Common Stock') for
    a symbol, or None if it can't be looked up (no credentials, unknown
    symbol, or an API error) — callers should treat None as "just omit it"."""
    symbol = symbol.strip().upper()
    if not symbol:
        return None
    if symbol in _cache:
        return _cache[symbol]

    # Asset lookups go through Alpaca's API too — no calls to Alpaca at all
    # outside the market data window. An uncached symbol just won't show a
    # company name in the title until the window reopens; the title still
    # falls back to symbol-only in that case.
    if not is_within_market_data_window():
        return None

    client = _get_client()
    if client is None:
        return None

    try:
        asset = client.get_asset(symbol)
        name = (asset.name or "").strip() or None
    except Exception:
        logger.warning("Could not look up company name for %s", symbol, exc_info=True)
        name = None

    _cache[symbol] = name
    return name
