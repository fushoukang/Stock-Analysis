"""Company/asset name lookup via Alpaca's Trading API (Assets endpoint).

Names essentially never change during a run, so results are cached in memory
per symbol — this avoids an extra Alpaca API round trip on every chart load
(the frontend re-fetches the chart on every live bar and on a 30s poll).
"""
from __future__ import annotations

import logging

from alpaca.trading.client import TradingClient

from config import settings

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

    # Deliberately NOT gated by is_within_market_data_window(): that gate
    # exists to stop streaming/bar-data calls outside trading hours (the
    # thing the user actually asked to restrict), but an asset's display
    # name is static reference data, not real-time market data — looking it
    # up via the Trading API's /assets endpoint any time of day doesn't
    # violate that intent, and it's the fix for company names not showing
    # while the market is closed. Still a single Alpaca call per symbol
    # ever, since the result is cached above for the life of the process.
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
