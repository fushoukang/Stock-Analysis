"""VWAP (Volume-Weighted Average Price) — the running average price paid,
weighted by volume, since the start of each trading session. Resets at the
start of every new day so the first bar of a session always starts VWAP
fresh at that bar's own typical price, rather than dragging in volume/price
history from prior days.
"""
from __future__ import annotations

import pandas as pd


def vwap(df: pd.DataFrame) -> pd.Series:
    """
    df: OHLCV DataFrame indexed by timestamp, one row per bar at whatever
        timeframe was selected (1Min/5Min/15Min/1Hour/1Day). VWAP is
        computed directly from these bars — a 5Min chart's VWAP is the
        cumulative volume-weighted average of 5Min typical prices, not
        recomputed from finer-grained 1Min data — so it naturally follows
        whichever time-interval the user picked.

    Resets each time df.index's calendar date changes (candlestick.py
    converts the index to America/New_York before calling this, so the
    reset lines up with the US trading session). For a 1Day timeframe each
    row is its own session, so VWAP for that row is just its own typical
    price.
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical_price * df["volume"]
    session = pd.Series(df.index.date, index=df.index)
    cum_pv = pv.groupby(session).cumsum()
    cum_vol = df["volume"].groupby(session).cumsum()
    return cum_pv / cum_vol
