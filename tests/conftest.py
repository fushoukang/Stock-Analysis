"""Shared pytest fixtures/helpers for the Stock-Trading test suite."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Ensure the project root is importable regardless of where pytest is
# invoked from (mirrors the sys.path.insert(0, ".") pattern used in this
# project's manual verification scripts throughout development).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def make_ohlcv_df(
    n: int = 100,
    start: str = "2026-01-01",
    freq: str = "5min",
    start_price: float = 100.0,
    drift: float = 0.02,
    noise: float = 0.3,
    seed: int = 0,
) -> pd.DataFrame:
    """Builds a small synthetic OHLCV DataFrame indexed by a UTC
    DatetimeIndex, for feeding into indicator/signal functions in tests.
    Deterministic (fixed seed) so tests are reproducible."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    close = start_price + np.cumsum(rng.normal(drift, noise, n))
    high = close + rng.uniform(0.05, 0.3, n)
    low = close - rng.uniform(0.05, 0.3, n)
    open_ = close + rng.normal(0, 0.1, n)
    volume = rng.integers(1000, 5000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@pytest.fixture
def ohlcv_df():
    """A 200-bar synthetic OHLCV DataFrame — enough history for every
    indicator's default lookback (MACD's 26+9 is the longest)."""
    return make_ohlcv_df(n=200)
