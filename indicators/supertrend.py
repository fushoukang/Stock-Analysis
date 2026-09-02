"""SuperTrend — an ATR-based trend-following overlay. Plots a single line
that flips sides of price (below when trending up, above when trending
down) and is commonly used as a trailing stop / trend-direction indicator,
in the same family as Parabolic SAR (see indicators/sar.py) but built off
ATR (Average True Range) volatility instead of an acceleration factor.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's Average True Range — same smoothing style as RSI's
    avg_gain/avg_loss (indicators/rsi.py): an EMA with alpha = 1/period."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def supertrend(
    df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
) -> pd.DataFrame:
    """
    df must have 'high', 'low', 'close' columns, sorted ascending by time.

    Returns a DataFrame aligned to df's index with columns:
      - 'supertrend': the indicator's line value.
      - 'trend': 1 while in an uptrend (line sits below price), -1 while in
        a downtrend (line sits above price).

    Classic recipe: start from a basic upper/lower band centered on the bar's
    midpoint and offset by multiplier * ATR, then "ratchet" each band so it
    only ever tightens toward price (never loosens) until price closes
    through it, at which point the trend flips and the opposite band takes
    over as the new SuperTrend line. Path-dependent like Parabolic SAR, so
    it's computed with an explicit loop rather than vectorized.
    """
    n = len(df)
    st = np.full(n, np.nan)
    trend = np.zeros(n, dtype=int)
    if n == 0:
        return pd.DataFrame({"supertrend": st, "trend": trend}, index=df.index)

    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    atr = _atr(df, period).to_numpy(dtype=float)
    mid = (high + low) / 2.0

    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)

    for i in range(n):
        if np.isnan(atr[i]):
            # Not enough bars yet for ATR to have a value — leave this bar's
            # SuperTrend unset, same as SAR/RSI leave their warm-up NaN.
            trend[i] = trend[i - 1] if i > 0 else 1
            continue

        basic_upper = mid[i] + multiplier * atr[i]
        basic_lower = mid[i] - multiplier * atr[i]

        prev_final_upper = final_upper[i - 1] if i > 0 and not np.isnan(final_upper[i - 1]) else basic_upper
        prev_final_lower = final_lower[i - 1] if i > 0 and not np.isnan(final_lower[i - 1]) else basic_lower
        prev_close = close[i - 1] if i > 0 else close[i]

        # Bands only ever tighten toward price; a close beyond the previous
        # band resets it so the new band can widen again to fit the breakout.
        if basic_upper < prev_final_upper or prev_close > prev_final_upper:
            final_upper[i] = basic_upper
        else:
            final_upper[i] = prev_final_upper

        if basic_lower > prev_final_lower or prev_close < prev_final_lower:
            final_lower[i] = basic_lower
        else:
            final_lower[i] = prev_final_lower

        prev_trend = trend[i - 1] if i > 0 else 1
        if prev_trend == 1:
            trend[i] = -1 if close[i] < final_lower[i] else 1
        else:
            trend[i] = 1 if close[i] > final_upper[i] else -1

        st[i] = final_lower[i] if trend[i] == 1 else final_upper[i]

    return pd.DataFrame({"supertrend": st, "trend": trend}, index=df.index)
