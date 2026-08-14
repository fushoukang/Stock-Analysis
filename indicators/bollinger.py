"""Bollinger Bands (BOLL)."""
from __future__ import annotations

import pandas as pd

from indicators.moving_average import sma


def bollinger_bands(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    """Returns a DataFrame with columns: mid, upper, lower."""
    mid = sma(close, window)
    std = close.rolling(window=window, min_periods=window).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower})
