"""Resample 1-minute OHLCV bars into coarser intervals.

Shared by the web GUI (charts/indicators for whatever interval is selected)
and the KDJ monitor, so "what does a 15Min bar look like right now" is
computed the same way everywhere.
"""
from __future__ import annotations

import pandas as pd

# pandas resample rule for each supported interval. "1Day" is deliberately
# excluded — daily bars are fetched from Alpaca's REST API directly rather
# than resampled locally (see data/historical.py), since a meaningful daily
# chart needs far more history than what's practical to keep resampling from
# 1-minute bars.
RESAMPLE_RULES = {
    "1Min": "1min",
    "5Min": "5min",
    "15Min": "15min",
    "30Min": "30min",
    "1Hour": "1h",
}

# How many 1-minute bars make up one bar of each interval — used to size how
# much raw 1-min history to pull before resampling.
BAR_MINUTES = {"1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60}


def resample_bars(df_1min: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample 1-minute OHLCV bars (index = UTC timestamp) into `timeframe`
    bars. Returns df_1min unchanged if timeframe is "1Min".

    The last row may be a still-forming (incomplete) bar, built from
    whatever 1-min bars have arrived so far for the current window — that's
    intentional: it's what lets the chart and KDJ update live within the
    current bar instead of only at the close of every interval.
    """
    if df_1min.empty or timeframe == "1Min":
        return df_1min
    rule = RESAMPLE_RULES.get(timeframe)
    if rule is None:
        raise ValueError(f"Can't resample to timeframe '{timeframe}' locally")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df_1min.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])
