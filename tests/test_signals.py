"""Tests for indicators/signals.py: compute_signals()'s per-indicator
textbook rules, its fault-isolation guarantee (one indicator's exception
must never wipe out signals already computed for the others — this is the
exact bug that made the Trend panel vanish once 3+ indicators were
selected), and compute_composite_signal()'s majority-vote rollup."""
from __future__ import annotations

import pandas as pd
import pytest

import indicators.signals as signals_module
from indicators.signals import (
    BULLISH,
    BEARISH,
    NEUTRAL,
    compute_signals,
    compute_composite_signal,
)

from conftest import make_ohlcv_df


def _flat_df(n: int, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": price, "high": price, "low": price, "close": price, "volume": 1000.0},
        index=idx,
    )


def _rising_df(n: int = 80) -> pd.DataFrame:
    return make_ohlcv_df(n=n, drift=0.3, noise=0.05, seed=1)  # strong steady uptrend


def _falling_df(n: int = 80) -> pd.DataFrame:
    return make_ohlcv_df(n=n, drift=-0.3, noise=0.05, seed=2)  # strong steady downtrend


def test_sma_signal_bullish_on_uptrend():
    df = _rising_df()
    out = compute_signals(df, [])
    assert out["sma"]["label"] == BULLISH


def test_sma_signal_bearish_on_downtrend():
    df = _falling_df()
    out = compute_signals(df, [])
    assert out["sma"]["label"] == BEARISH


def test_ema_signal_present_and_correct_direction():
    df = _rising_df()
    out = compute_signals(df, ["ema"])
    assert out["ema"]["label"] == BULLISH


def test_rsi_signal_bearish_overbought_on_strong_sustained_uptrend():
    # A strong, near-uninterrupted uptrend pushes RSI toward 100 —
    # "overbought", which the textbook rule reads as BEARISH (pullback
    # risk), not bullish. This is distinct from EMA/VWAP's simpler
    # price-vs-indicator rule.
    df = _rising_df()
    out = compute_signals(df, ["rsi"])
    assert out["rsi"]["label"] == BEARISH
    assert "overbought" in out["rsi"]["reason"]


def test_rsi_signal_bullish_oversold_on_strong_sustained_downtrend():
    # Symmetric case: a strong sustained downtrend pushes RSI toward 0 —
    # "oversold", read as BULLISH (bounce potential).
    df = _falling_df()
    out = compute_signals(df, ["rsi"])
    assert out["rsi"]["label"] == BULLISH
    assert "oversold" in out["rsi"]["reason"]


def test_macd_signal_present_on_trending_data():
    df = _rising_df(n=80)
    out = compute_signals(df, ["macd"])
    assert "macd" in out
    assert out["macd"]["label"] in (BULLISH, BEARISH, NEUTRAL)


def test_mtm_signal_present_on_trending_data():
    df = _rising_df(n=40)
    out = compute_signals(df, ["mtm"])
    assert "mtm" in out
    assert out["mtm"]["label"] in (BULLISH, BEARISH, NEUTRAL)
    assert "MTM" in out["mtm"]["reason"] and "MAMTM" in out["mtm"]["reason"]


def test_mtm_signal_omitted_when_not_enough_bars():
    # MTM(12,6) needs 12+6=18 bars before MAMTM has a value.
    df = _rising_df(n=15)
    out = compute_signals(df, ["mtm"])
    assert "mtm" not in out


def test_boll_signal_overbought_when_price_pierces_upper_band():
    # Flat history, then one big spike up on the last bar — should read as
    # above the upper band (bearish/overbought), not just "above the mid".
    df = _flat_df(30, price=100.0)
    df.loc[df.index[-1], ["open", "high", "low", "close"]] = 200.0
    out = compute_signals(df, ["boll"])
    assert out["boll"]["label"] == BEARISH
    assert "overbought" in out["boll"]["reason"]


def test_kdj_signal_present_and_bounded_label():
    df = _rising_df()
    out = compute_signals(df, ["kdj"])
    assert out["kdj"]["label"] in (BULLISH, BEARISH, NEUTRAL)


def test_vwap_signal_bullish_when_price_above_vwap():
    df = _rising_df()
    out = compute_signals(df, ["vwap"])
    assert out["vwap"]["label"] == BULLISH


def test_sar_signal_present_and_bounded_label():
    df = _rising_df()
    out = compute_signals(df, ["sar"])
    assert out["sar"]["label"] in (BULLISH, BEARISH, NEUTRAL)


def test_unknown_indicator_name_is_silently_ignored():
    df = _rising_df()
    out = compute_signals(df, ["not_a_real_indicator"])
    assert "not_a_real_indicator" not in out
    assert "sma" in out  # unaffected


def test_compute_signals_omits_indicators_without_enough_lookback():
    # Only 3 bars — not enough for ema/rsi/macd's min_periods (span/period
    # requirements), so those are omitted. SMA (sma_nm, an alpha-based EWM
    # with no min_periods) has no such warm-up requirement and is always
    # on, so it's still present even this early.
    df = _rising_df(n=3)
    out = compute_signals(df, ["ema", "rsi", "macd"])
    assert "ema" not in out
    assert "rsi" not in out
    assert "macd" not in out
    assert "sma" in out


def test_compute_signals_deduplicates_and_lowercases_indicator_names():
    df = _rising_df()
    out = compute_signals(df, ["RSI", "rsi", "Rsi"])
    assert "rsi" in out
    assert len(out) == 2  # sma (always on) + rsi


def test_one_indicator_exception_does_not_wipe_out_the_others(monkeypatch):
    """Regression test for the bug where selecting 3+ indicators made the
    whole Trend panel vanish: one indicator's exception must only drop that
    indicator's own signal, never the ones already computed before it."""
    df = _rising_df()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated indicator failure")

    monkeypatch.setattr(signals_module, "_rsi", _boom)
    out = compute_signals(df, ["ema", "rsi", "macd"])

    assert "rsi" not in out  # the failing indicator is dropped...
    assert "ema" in out  # ...but its neighbors are unaffected
    assert "macd" in out
    assert "sma" in out  # and the always-on SMA reading survives too


def test_sma_signal_exception_does_not_take_out_the_rest(monkeypatch):
    df = _rising_df()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SMA failure")

    monkeypatch.setattr(signals_module, "sma_nm", _boom)
    out = compute_signals(df, ["ema", "rsi"])

    assert "sma" not in out
    assert "ema" in out
    assert "rsi" in out


def test_compute_composite_signal_none_when_no_signals():
    assert compute_composite_signal({}) is None
    assert compute_composite_signal(None) is None


def test_compute_composite_signal_majority_bullish():
    signals = {
        "sma": {"label": BULLISH},
        "ema": {"label": BULLISH},
        "rsi": {"label": BEARISH},
    }
    out = compute_composite_signal(signals)
    assert out["label"] == BULLISH
    assert out["bullish_count"] == 2
    assert out["bearish_count"] == 1
    assert out["neutral_count"] == 0
    assert out["total"] == 3


def test_compute_composite_signal_tie_is_neutral():
    signals = {"sma": {"label": BULLISH}, "ema": {"label": BEARISH}}
    out = compute_composite_signal(signals)
    assert out["label"] == NEUTRAL


def test_compute_composite_signal_neutral_entries_count_toward_total_only():
    signals = {
        "sma": {"label": BULLISH},
        "ema": {"label": BULLISH},
        "rsi": {"label": NEUTRAL},
    }
    out = compute_composite_signal(signals)
    assert out["label"] == BULLISH
    assert out["neutral_count"] == 1
    assert out["total"] == 3


def test_compute_composite_signal_reports_the_winning_labels_own_count():
    """Regression test for a bug caught during development: the displayed
    count must match whichever label actually won the vote, not always
    bullish_count — a bearish read with a stray bullish_count looked like
    'Bearish (3/8)' where 3 was really the bullish count."""
    signals = {
        "sma": {"label": BEARISH},
        "ema": {"label": BEARISH},
        "rsi": {"label": BEARISH},
        "macd": {"label": BULLISH},
    }
    out = compute_composite_signal(signals)
    assert out["label"] == BEARISH
    winning_count = out[out["label"] + "_count"]
    assert winning_count == 3
