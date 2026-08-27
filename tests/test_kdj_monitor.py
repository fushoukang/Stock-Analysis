"""Tests for alerts/kdj_monitor.py: cross detection and monitor-list
read/write (monitor_list.txt)."""
from __future__ import annotations

import pandas as pd
import pytest

from alerts.kdj_monitor import detect_cross, load_monitor_symbols, save_monitor_symbols


def _kdj_df(k_values: list[float], d_values: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(k_values), freq="15min", tz="UTC")
    return pd.DataFrame({"k": k_values, "d": d_values, "j": [0.0] * len(k_values)}, index=idx)


def test_detect_cross_up_when_k_crosses_above_d():
    df = _kdj_df([20, 30], [25, 25])  # prev: k<d, curr: k>=d
    assert detect_cross(df) == "up"


def test_detect_cross_down_when_k_crosses_below_d():
    df = _kdj_df([30, 20], [25, 25])  # prev: k>d, curr: k<=d
    assert detect_cross(df) == "down"


def test_detect_cross_none_when_no_cross():
    df = _kdj_df([30, 35], [25, 26])  # k>d both times, no cross
    assert detect_cross(df) is None


def test_detect_cross_none_with_fewer_than_two_rows():
    df = _kdj_df([30], [25])
    assert detect_cross(df) is None


def test_detect_cross_none_when_touching_but_not_crossing():
    # prev_diff == 0 (already equal) then still equal — no transition.
    df = _kdj_df([25, 25], [25, 25])
    assert detect_cross(df) is None


def test_load_monitor_symbols_missing_file_returns_empty(tmp_path):
    missing = tmp_path / "does_not_exist.txt"
    assert load_monitor_symbols(missing) == []


def test_save_then_load_monitor_symbols_roundtrip(tmp_path):
    p = tmp_path / "monitor_list.txt"
    saved = save_monitor_symbols(["tqqq", " aapl ", "aapl", "msft"], p)
    assert saved == ["TQQQ", "AAPL", "MSFT"]  # uppercased, deduped, order preserved

    loaded = load_monitor_symbols(p)
    assert loaded == ["TQQQ", "AAPL", "MSFT"]


def test_load_monitor_symbols_accepts_commas_and_whitespace(tmp_path):
    p = tmp_path / "monitor_list.txt"
    p.write_text("TQQQ, AAPL\nMSFT   QQQ,,\n")
    loaded = load_monitor_symbols(p)
    assert loaded == ["TQQQ", "AAPL", "MSFT", "QQQ"]


def test_save_monitor_symbols_drops_blanks():
    saved = None

    def _noop_write(*_a, **_k):
        nonlocal saved

    # Directly exercise the cleanup logic without touching disk by passing
    # a real tmp-backed path isn't necessary here — just check the pure
    # dedupe/clean behavior via the return value on an in-memory call.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "list.txt"
        result = save_monitor_symbols(["", "  ", "spy", "SPY"], p)
        assert result == ["SPY"]
