"""Tests for alerts/kdj_monitor.py: cross detection, monitor-list
read/write (monitor_list.txt), and KDJMonitor's injectable symbol source /
email switch (added so a second, independent instance can watch a different
symbol set — e.g. crypto pairs — without touching monitor_list.txt or the
stock email on/off setting)."""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from config import settings
from alerts.kdj_monitor import (
    KDJMonitor,
    detect_cross,
    load_monitor_symbols,
    save_monitor_symbols,
    load_crypto_kdj_email_alerts_enabled,
    save_crypto_kdj_email_alerts_enabled,
)


class _EmptyStore:
    """Minimal store double: always reports no bars, so _check_symbol
    returns immediately without needing real market data — enough to
    exercise run_forever()'s symbol-iteration logic in isolation."""

    def get_bars(self, *args, **kwargs):
        return pd.DataFrame()


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


def test_kdj_monitor_defaults_to_monitor_list_and_global_email_switch():
    mon = KDJMonitor(_EmptyStore())
    assert mon.symbols_provider is load_monitor_symbols
    assert mon.email_alerts_enabled() == settings.kdj_email_alerts_enabled
    assert mon.label == ""


def test_kdj_monitor_honors_injected_symbols_provider_and_email_switch():
    """A second monitor instance (e.g. for crypto) must read symbols from
    its own injected source, not monitor_list.txt, and must consult its own
    email on/off callable, not the global stock switch."""
    mon = KDJMonitor(
        _EmptyStore(),
        symbols_provider=lambda: ["BTC/USDT"],
        email_alerts_enabled=lambda: False,
        label="crypto",
    )
    assert mon.symbols_provider() == ["BTC/USDT"]
    assert mon.email_alerts_enabled() is False
    assert mon.label == "crypto"


def test_run_forever_pulls_symbols_from_the_injected_provider_each_cycle(monkeypatch):
    """Regression guard: run_forever() must call self.symbols_provider(),
    not the module-level load_monitor_symbols(), so an injected provider
    (e.g. a crypto watch list) actually takes effect."""
    calls: list[int] = []

    def _provider():
        calls.append(1)
        return ["BTC/USDT"]

    class _StopLoop(Exception):
        pass

    async def _boom_sleep(*_a, **_k):
        raise _StopLoop()

    monkeypatch.setattr(asyncio, "sleep", _boom_sleep)

    mon = KDJMonitor(_EmptyStore(), symbols_provider=_provider)
    with pytest.raises(_StopLoop):
        asyncio.run(mon.run_forever())

    assert calls == [1]  # provider was called exactly once before the loop stopped


def test_load_crypto_kdj_email_alerts_enabled_falls_back_when_file_missing(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    assert load_crypto_kdj_email_alerts_enabled(missing) == settings.crypto_kdj_email_alerts_enabled


def test_save_then_load_crypto_kdj_email_alerts_enabled_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    assert save_crypto_kdj_email_alerts_enabled(False, p) is False
    assert load_crypto_kdj_email_alerts_enabled(p) is False

    assert save_crypto_kdj_email_alerts_enabled(True, p) is True
    assert load_crypto_kdj_email_alerts_enabled(p) is True


def test_load_crypto_kdj_email_alerts_enabled_falls_back_on_corrupt_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not valid json")
    assert load_crypto_kdj_email_alerts_enabled(p) == settings.crypto_kdj_email_alerts_enabled
