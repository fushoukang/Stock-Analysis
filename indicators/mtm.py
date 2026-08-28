"""MTM (Momentum) — a Chinese technical-analysis staple, written MTM(N, M)
in formula language (e.g. 通达信/同花顺): how much price has moved over the
last N bars, smoothed by an M-period moving average of that momentum.

    MTM   = CLOSE - REF(CLOSE, N)   -- today's close minus the close N bars ago
    MAMTM = MA(MTM, M)              -- M-period simple moving average of MTM

Default N=12, M=6 — the conventional MTM(12,6) parameterization.
"""
from __future__ import annotations

import pandas as pd


def mtm(close: pd.Series, n: int = 12, m: int = 6) -> pd.DataFrame:
    """
    close: price series (typically the close column).
    n: momentum lookback — MTM is today's close minus the close n bars ago.
    m: window for MAMTM, the simple moving average of the MTM line itself.

    Returns a DataFrame with columns: mtm, maMtm.
    """
    mtm_line = close - close.shift(n)
    ma_mtm = mtm_line.rolling(window=m, min_periods=m).mean()
    return pd.DataFrame({"mtm": mtm_line, "maMtm": ma_mtm})
