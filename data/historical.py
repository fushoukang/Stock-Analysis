"""
Historical bar backfill via Alpaca's REST market data API (alpaca-py).

Used to (a) seed the local DB with history on startup so charts aren't empty
before the live stream produces new bars, and (b) serve arbitrary time
ranges the user asks for in the GUI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from config import settings

# Map friendly interval strings (also used in the GUI) to alpaca-py TimeFrame objects.
TIMEFRAME_MAP: dict[str, TimeFrame] = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "30Min": TimeFrame(30, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}


def get_client() -> StockHistoricalDataClient:
    if not settings.has_credentials():
        raise RuntimeError(
            "Alpaca API credentials are not set. Copy .env.example to .env "
            "and fill in ALPACA_API_KEY / ALPACA_SECRET_KEY."
        )
    return StockHistoricalDataClient(settings.api_key, settings.secret_key)


def fetch_bars(
    symbol: str, timeframe: str = "1Min", lookback_days: int = 5, limit: int = 1000
) -> pd.DataFrame:
    """Fetch recent historical bars for a symbol, returned as an OHLCV DataFrame
    indexed by UTC timestamp (columns: open, high, low, close, volume)."""
    if timeframe not in TIMEFRAME_MAP:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Options: {list(TIMEFRAME_MAP)}")

    client = get_client()
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol.upper()],
        timeframe=TIMEFRAME_MAP[timeframe],
        start=start,
        limit=limit,
        feed=settings.data_feed_enum(),
    )
    bar_set = client.get_stock_bars(req)
    df = bar_set.df
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # bar_set.df has a MultiIndex (symbol, timestamp) when multiple symbols requested.
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol.upper(), level=0)
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df.index.name = "ts"
    return df
