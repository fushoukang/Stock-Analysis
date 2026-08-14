"""Parabolic SAR (stop-and-reverse), Welles Wilder's original algorithm."""
from __future__ import annotations

import numpy as np
import pandas as pd


def parabolic_sar(
    df: pd.DataFrame, af_step: float = 0.02, af_max: float = 0.2
) -> pd.Series:
    """
    df must have 'high' and 'low' columns, sorted ascending by time.
    Returns a Series of SAR values aligned to df's index.
    """
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    n = len(df)
    sar = np.full(n, np.nan)
    if n < 2:
        return pd.Series(sar, index=df.index, name="sar")

    # Initialize: assume uptrend, start SAR at the first low.
    uptrend = True
    af = af_step
    ep = high[0]  # extreme point
    sar[0] = low[0]

    for i in range(1, n):
        prev_sar = sar[i - 1]
        if uptrend:
            new_sar = prev_sar + af * (ep - prev_sar)
            new_sar = min(new_sar, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < new_sar:
                # Trend flips to down.
                uptrend = False
                sar[i] = ep
                ep = low[i]
                af = af_step
            else:
                sar[i] = new_sar
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else:
            new_sar = prev_sar + af * (ep - prev_sar)
            new_sar = max(new_sar, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > new_sar:
                # Trend flips to up.
                uptrend = True
                sar[i] = ep
                ep = high[i]
                af = af_step
            else:
                sar[i] = new_sar
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)

    return pd.Series(sar, index=df.index, name="sar")
