"""
Historical crypto bar backfill via Alpaca's REST market data API (alpaca-py).

Mirrors data/historical.py's stock fetch_bars(), but against Alpaca's crypto
market data endpoints instead — a separate data source from stocks (Alpaca
runs its own crypto exchange), reachable with the same account credentials.
Crypto trades 24/7, so unlike the stock path there's no market-hours window
to check here — every caller in web/app.py backfills/refreshes crypto data
unconditionally.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest

from config import settings
from data.historical import TIMEFRAME_MAP  # same TimeFrame objects work for crypto bars

logger = logging.getLogger("data.crypto_historical")

_client: CryptoHistoricalDataClient | None = None


def get_client() -> CryptoHistoricalDataClient:
    global _client
    if _client is None:
        # Alpaca's crypto *historical* data doesn't strictly require API
        # keys, but this app already gates every Alpaca call on real
        # credentials (same account, no separate crypto-only keys needed) —
        # kept consistent with data/historical.py's stock client rather
        # than special-cased just for crypto.
        if not settings.has_credentials():
            raise RuntimeError(
                "Alpaca API credentials are not set. Copy .env.example to .env "
                "and fill in ALPACA_API_KEY / ALPACA_SECRET_KEY."
            )
        _client = CryptoHistoricalDataClient(settings.api_key, settings.secret_key)
    return _client


def fetch_crypto_bars(
    symbol: str, timeframe: str = "1Min", lookback_days: int = 5, limit: int = 1000
) -> pd.DataFrame:
    """Fetch recent historical bars for a crypto pair (e.g. 'BTC/USDT'),
    returned as an OHLCV DataFrame indexed by UTC timestamp — the exact same
    shape as data/historical.py's fetch_bars(), so every downstream
    consumer (indicators, the chart builder) works unchanged on crypto data
    without any special-casing."""
    if timeframe not in TIMEFRAME_MAP:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Options: {list(TIMEFRAME_MAP)}")

    symbol = symbol.strip().upper()
    client = get_client()
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    req = CryptoBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TIMEFRAME_MAP[timeframe],
        start=start,
        limit=limit,
    )
    bar_set = client.get_crypto_bars(req)
    df = bar_set.df
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # bar_set.df has a MultiIndex (symbol, timestamp) when multiple symbols requested.
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level=0)
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df.index.name = "ts"
    return df
