"""KDJ stochastic oscillator (common in Chinese-market technical analysis)."""
from __future__ import annotations

import pandas as pd


def kdj(
    df: pd.DataFrame, n: int = 9, k_period: int = 3, d_period: int = 3
) -> pd.DataFrame:
    """
    df must have 'high', 'low', 'close' columns.
    Returns a DataFrame with columns: k, d, j.
    """
    low_n = df["low"].rolling(window=n, min_periods=n).min()
    high_n = df["high"].rolling(window=n, min_periods=n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, pd.NA) * 100
    rsv = rsv.fillna(50.0)

    k = rsv.ewm(alpha=1 / k_period, adjust=False).mean()
    d = k.ewm(alpha=1 / d_period, adjust=False).mean()
    j = 3 * k - 2 * d

    return pd.DataFrame({"k": k, "d": d, "j": j})
