"""Aggregator: compute a chosen set of indicators over an OHLCV DataFrame."""
from __future__ import annotations

import pandas as pd

from indicators.moving_average import sma, ema
from indicators.bollinger import bollinger_bands
from indicators.rsi import rsi
from indicators.sar import parabolic_sar
from indicators.kdj import kdj

# Indicators whose values live on the same scale as price (overlay on candles)
OVERLAY_INDICATORS = {"sma", "ema", "boll", "sar"}
# Indicators plotted in their own subplot below the candles
OSCILLATOR_INDICATORS = {"rsi", "kdj"}

DEFAULT_PARAMS = {
    "sma": {"window": 20},
    "ema": {"span": 20},
    "boll": {"window": 20, "num_std": 2.0},
    "rsi": {"period": 14},
    "sar": {"af_step": 0.02, "af_max": 0.2},
    "kdj": {"n": 9, "k_period": 3, "d_period": 3},
}


def compute_indicators(
    df: pd.DataFrame, indicators: list[str], params: dict | None = None
) -> dict[str, pd.Series | pd.DataFrame]:
    """
    df: OHLCV DataFrame indexed by timestamp, columns open/high/low/close/volume.
    indicators: subset of {"sma","ema","boll","rsi","sar","kdj"}.
    params: optional per-indicator override dict, e.g. {"sma": {"window": 50}}.

    Returns {indicator_name: Series or DataFrame}, aligned to df's index.
    """
    params = params or {}
    results: dict[str, pd.Series | pd.DataFrame] = {}
    close = df["close"]

    for name in indicators:
        name = name.lower()
        p = {**DEFAULT_PARAMS.get(name, {}), **params.get(name, {})}
        if name == "sma":
            results["sma"] = sma(close, **p)
        elif name == "ema":
            results["ema"] = ema(close, **p)
        elif name == "boll":
            results["boll"] = bollinger_bands(close, **p)
        elif name == "rsi":
            results["rsi"] = rsi(close, **p)
        elif name == "sar":
            results["sar"] = parabolic_sar(df, **p)
        elif name == "kdj":
            results["kdj"] = kdj(df, **p)
        else:
            raise ValueError(f"Unknown indicator '{name}'")

    return results
