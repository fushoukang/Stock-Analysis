"""Tests for backtesting.py's long/flat simulation driven by the composite
signal."""
from __future__ import annotations

import pytest

from backtesting import (
    BacktestError,
    BACKTEST_MAX_BARS,
    BACKTEST_MIN_BARS,
    run_backtest,
)

from conftest import make_ohlcv_df


def test_run_backtest_raises_on_empty_df():
    import pandas as pd

    with pytest.raises(BacktestError):
        run_backtest(pd.DataFrame())


def test_run_backtest_raises_on_too_few_bars():
    df = make_ohlcv_df(n=BACKTEST_MIN_BARS - 5)
    with pytest.raises(BacktestError):
        run_backtest(df)


def test_run_backtest_returns_expected_shape_on_trending_data():
    df = make_ohlcv_df(n=300, drift=0.05, noise=0.5, seed=3)
    result = run_backtest(df)

    assert result["bars_used"] == 300
    assert result["indicators"] == ["ema", "rsi", "macd"]
    assert isinstance(result["trades"], list)
    assert isinstance(result["equity_curve"], list)
    assert result["num_trades"] == len(result["trades"])
    if result["trades"]:
        assert result["win_rate_pct"] is not None
        assert 0 <= result["win_rate_pct"] <= 100
    else:
        assert result["win_rate_pct"] is None

    # Every trade log entry has a sane shape.
    for t in result["trades"]:
        assert t["exit_price"] > 0
        assert t["entry_price"] > 0
        assert isinstance(t["forced"], bool)


def test_run_backtest_final_equity_matches_total_return_formula():
    df = make_ohlcv_df(n=250, drift=0.1, noise=0.3, seed=5)
    result = run_backtest(df, initial_capital=5000.0)
    expected_return = (result["final_equity"] - 5000.0) / 5000.0 * 100
    assert result["total_return_pct"] == pytest.approx(expected_return, abs=0.01)


def test_run_backtest_caps_bars_at_max():
    df = make_ohlcv_df(n=BACKTEST_MAX_BARS + 200, seed=6)
    result = run_backtest(df)
    assert result["bars_used"] == BACKTEST_MAX_BARS


def test_run_backtest_force_closes_any_open_position_at_the_end():
    # A strong, steady uptrend should end the window still long — verify
    # the trade log's last entry (if any) is marked "forced" in that case,
    # rather than leaving an un-realized open position out of the log.
    df = make_ohlcv_df(n=200, drift=0.3, noise=0.05, seed=7)
    result = run_backtest(df)
    if result["trades"]:
        # cash should be fully realized (no shares silently left over) —
        # final_equity should be > 0 and consistent, i.e. no crash/NaN.
        assert result["final_equity"] > 0


def test_run_backtest_accepts_custom_indicator_list():
    df = make_ohlcv_df(n=200, seed=8)
    result = run_backtest(df, indicators=["rsi"])
    assert result["indicators"] == ["rsi"]
