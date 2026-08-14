"""Simple / exponential moving averages."""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, window: int = 20) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int = 20) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def ma(series: pd.Series, window: int = 20) -> pd.Series:
    """Generic 'MA' — alias for the simple moving average."""
    return sma(series, window)


def sma_nm(series: pd.Series, n: int = 12, m: int = 2) -> pd.Series:
    """Chinese technical-analysis style weighted moving average, written
    SMA(X, N, M) in formula language (e.g. 通达信/同花顺), distinct from the
    plain arithmetic `sma()` above despite the shared name. Recursive
    definition: Y = (M*X + (N-M)*Y_prev) / N — mathematically an EMA with
    alpha = M/N. This is the same recursive-smoothing style already used for
    KDJ's K/D lines elsewhere in this codebase (indicators/kdj.py)."""
    alpha = m / n
    return series.ewm(alpha=alpha, adjust=False).mean()
