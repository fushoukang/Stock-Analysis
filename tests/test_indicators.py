"""Sanity/correctness checks for the pure indicator math in indicators/."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from indicators.moving_average import sma, ema, ma, sma_nm
from indicators.rsi import rsi
from indicators.macd import macd
from indicators.bollinger import bollinger_bands
from indicators.kdj import kdj
from indicators.vwap import vwap
from indicators.sar import parabolic_sar
from indicators.mtm import mtm

from conftest import make_ohlcv_df


def test_sma_matches_manual_average():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(s, window=3)
    assert pd.isna(out.iloc[0]) and pd.isna(out.iloc[1])
    assert out.iloc[2] == pytest.approx((1 + 2 + 3) / 3)
    assert out.iloc[4] == pytest.approx((3 + 4 + 5) / 3)


def test_ma_is_alias_for_sma():
    s = pd.Series(np.arange(1, 21, dtype=float))
    pd.testing.assert_series_equal(ma(s, window=5), sma(s, window=5))


def test_ema_matches_pandas_ewm_reference():
    s = pd.Series(np.arange(1, 51, dtype=float))
    out = ema(s, span=10)
    expected = s.ewm(span=10, adjust=False, min_periods=10).mean()
    pd.testing.assert_series_equal(out, expected)


def test_sma_nm_is_recursive_ewm_with_alpha_m_over_n():
    s = pd.Series(np.linspace(10, 20, 30))
    out = sma_nm(s, n=12, m=2)
    expected = s.ewm(alpha=2 / 12, adjust=False).mean()
    pd.testing.assert_series_equal(out, expected)


def test_rsi_pure_uptrend_approaches_100():
    s = pd.Series(np.arange(1, 51, dtype=float))  # strictly increasing, no losses
    out = rsi(s, period=14)
    last = out.iloc[-1]
    assert last == pytest.approx(100.0)


def test_rsi_pure_downtrend_approaches_0():
    s = pd.Series(np.arange(50, 0, -1, dtype=float))  # strictly decreasing, no gains
    out = rsi(s, period=14)
    last = out.iloc[-1]
    assert last == pytest.approx(0.0)


def test_rsi_flat_prices_is_50():
    s = pd.Series([100.0] * 30)
    out = rsi(s, period=14)
    last = out.iloc[-1]
    assert last == pytest.approx(50.0)


def test_macd_columns_and_histogram_equals_macd_minus_signal():
    df = make_ohlcv_df(n=100)
    out = macd(df["close"], fast=12, slow=26, signal=9)
    assert list(out.columns) == ["macd", "signal", "histogram"]
    valid = out.dropna()
    assert len(valid) > 0
    pd.testing.assert_series_equal(
        valid["histogram"], (valid["macd"] - valid["signal"]), check_names=False
    )


def test_bollinger_band_width_matches_num_std():
    df = make_ohlcv_df(n=60)
    out = bollinger_bands(df["close"], window=20, num_std=2.0)
    valid = out.dropna()
    assert len(valid) > 0
    # upper - mid == mid - lower == num_std * rolling std, by construction.
    assert (valid["upper"] - valid["mid"]).round(6).equals((valid["mid"] - valid["lower"]).round(6))


def test_kdj_j_equals_3k_minus_2d():
    df = make_ohlcv_df(n=60)
    out = kdj(df, n=9, k_period=3, d_period=3)
    assert list(out.columns) == ["k", "d", "j"]
    expected_j = 3 * out["k"] - 2 * out["d"]
    pd.testing.assert_series_equal(out["j"], expected_j, check_names=False)


def test_kdj_rsv_bounded_0_100_via_k_d_no_nan():
    # k/d are EWMs of a value bounded in [0, 100] (or 50 on a flat window),
    # so they should never go outside that band either.
    df = make_ohlcv_df(n=60)
    out = kdj(df)
    assert out["k"].between(0, 100).all()
    assert out["d"].between(0, 100).all()


def test_vwap_single_session_matches_manual_cumulative_calc():
    idx = pd.date_range("2026-01-01 09:30", periods=3, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [10, 11, 12],
            "high": [10, 11, 12],
            "low": [10, 11, 12],
            "close": [10, 11, 12],
            "volume": [100, 200, 300],
        },
        index=idx,
    )
    out = vwap(df)
    typical = (df["high"] + df["low"] + df["close"]) / 3  # == close here
    manual = []
    cum_pv, cum_vol = 0.0, 0.0
    for tp, vol in zip(typical, df["volume"]):
        cum_pv += tp * vol
        cum_vol += vol
        manual.append(cum_pv / cum_vol)
    np.testing.assert_allclose(out.to_numpy(), manual)


def test_vwap_resets_on_new_session():
    idx = pd.to_datetime(
        ["2026-01-01 15:59", "2026-01-02 09:30"], utc=True
    )
    df = pd.DataFrame(
        {"open": [10, 50], "high": [10, 50], "low": [10, 50], "close": [10, 50], "volume": [100, 100]},
        index=idx,
    )
    out = vwap(df)
    # Second session's first bar has no history from the first session, so
    # its VWAP is exactly its own typical price (50), not blended with the
    # prior day's 10.
    assert out.iloc[1] == pytest.approx(50.0)


def test_parabolic_sar_length_and_seed_value():
    df = make_ohlcv_df(n=40)
    out = parabolic_sar(df)
    assert len(out) == len(df)
    assert out.iloc[0] == pytest.approx(df["low"].iloc[0])
    assert not out.iloc[1:].isna().any()


def test_parabolic_sar_too_short_returns_all_nan():
    df = make_ohlcv_df(n=1)
    out = parabolic_sar(df)
    assert len(out) == 1
    assert out.isna().all()


def test_mtm_matches_manual_momentum_calc():
    s = pd.Series(np.arange(1, 41, dtype=float))  # strictly +1/step
    out = mtm(s, n=12, m=6)
    assert list(out.columns) == ["mtm", "maMtm"]
    # Constant +1/step series -> MTM(12) is constant 12 once warmed up, and
    # MAMTM (a moving average of a constant) equals that same constant.
    assert out["mtm"].iloc[-1] == pytest.approx(12.0)
    assert out["maMtm"].iloc[-1] == pytest.approx(12.0)


def test_mtm_first_n_values_are_nan_before_theres_a_reference_close():
    s = pd.Series(np.arange(1, 21, dtype=float))
    out = mtm(s, n=12, m=6)
    assert out["mtm"].iloc[:12].isna().all()
    assert out["mtm"].iloc[12:].notna().all()


def test_mtm_default_params_are_12_6():
    s = pd.Series(np.arange(1, 41, dtype=float))
    default = mtm(s)
    explicit = mtm(s, n=12, m=6)
    pd.testing.assert_frame_equal(default, explicit)


def test_mtm_zero_for_flat_price():
    s = pd.Series([100.0] * 30)
    out = mtm(s, n=12, m=6)
    assert out["mtm"].iloc[-1] == pytest.approx(0.0)
    assert out["maMtm"].iloc[-1] == pytest.approx(0.0)
