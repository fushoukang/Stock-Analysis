"""MACD (Moving Average Convergence Divergence)."""
from __future__ import annotations

import pandas as pd

from indicators.moving_average import ema


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """
    close: price series (typically the close column).
    fast/slow: spans of the two EMAs whose difference is the MACD line
        (fast - slow); signal: span of the EMA of the MACD line itself.

    Returns a DataFrame with columns: macd, signal, histogram (macd - signal).
    """
    macd_line = ema(close, span=fast) - ema(close, span=slow)
    signal_line = ema(macd_line, span=signal)
    histogram = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "histogram": histogram})
