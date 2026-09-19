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
from data.users import User
import alerts.kdj_monitor as kdj_module
from alerts.kdj_monitor import (
    KDJMonitor,
    all_monitored_symbols,
    delete_user_monitor_list_file,
    detect_cross,
    load_monitor_symbols,
    load_user_monitor_symbols,
    save_monitor_symbols,
    save_user_monitor_symbols,
    users_watching_symbol,
    load_crypto_kdj_email_alerts_enabled,
    save_crypto_kdj_email_alerts_enabled,
)


def _user(email: str) -> User:
    return User(email=email, salt_hex="", hash_hex="")


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


# --- Per-user monitor lists (Focus Stock Analysis page's Symbol list) ---

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture(autouse=True)
def _redirect_user_monitor_list_path(tmp_path, monkeypatch):
    d = tmp_path / "user_monitor_lists"

    def _fake_path(email, path=None):
        return d / f"{email.replace('@', '_at_')}.txt"

    monkeypatch.setattr(kdj_module, "user_monitor_list_path", _fake_path)
    yield d


def test_load_user_monitor_symbols_falls_back_to_default_template(tmp_path, monkeypatch):
    default = tmp_path / "monitor_list.txt"
    monkeypatch.setattr(kdj_module, "monitor_list_path", lambda path=None: default)
    save_monitor_symbols(["SPY", "QQQ"], default)

    assert load_user_monitor_symbols(ALICE) == ["SPY", "QQQ"]
    # Reading never persists anything for the user — no per-user file yet.
    assert not kdj_module.user_monitor_list_path(ALICE).exists()


def test_save_user_monitor_symbols_forks_independently_of_default_and_other_users(tmp_path, monkeypatch):
    default = tmp_path / "monitor_list.txt"
    monkeypatch.setattr(kdj_module, "monitor_list_path", lambda path=None: default)
    save_monitor_symbols(["SPY"], default)

    save_user_monitor_symbols(ALICE, ["aapl", "msft", "aapl"])
    assert load_user_monitor_symbols(ALICE) == ["AAPL", "MSFT"]  # uppercased + deduped
    # Bob never saved his own list, so he still just sees the template.
    assert load_user_monitor_symbols(BOB) == ["SPY"]
    # The template itself is untouched by Alice's save.
    assert load_monitor_symbols(default) == ["SPY"]


def test_delete_user_monitor_list_file_resets_to_default_template(tmp_path, monkeypatch):
    default = tmp_path / "monitor_list.txt"
    monkeypatch.setattr(kdj_module, "monitor_list_path", lambda path=None: default)
    save_monitor_symbols(["SPY"], default)
    save_user_monitor_symbols(ALICE, ["TQQQ"])
    assert load_user_monitor_symbols(ALICE) == ["TQQQ"]

    delete_user_monitor_list_file(ALICE)
    assert load_user_monitor_symbols(ALICE) == ["SPY"]


def test_delete_user_monitor_list_file_missing_file_is_a_noop():
    delete_user_monitor_list_file(ALICE)  # never created — should not raise


def test_all_monitored_symbols_unions_every_registered_users_effective_list(tmp_path, monkeypatch):
    default = tmp_path / "monitor_list.txt"
    monkeypatch.setattr(kdj_module, "monitor_list_path", lambda path=None: default)
    save_monitor_symbols(["SPY"], default)
    save_user_monitor_symbols(ALICE, ["AAPL"])
    # Bob never customized his — his effective list is still the template (SPY).

    monkeypatch.setattr(kdj_module.user_store, "list_users", lambda: [_user(ALICE), _user(BOB)])
    assert all_monitored_symbols() == ["AAPL", "SPY"]


def test_all_monitored_symbols_falls_back_to_default_when_no_users_registered(tmp_path, monkeypatch):
    default = tmp_path / "monitor_list.txt"
    monkeypatch.setattr(kdj_module, "monitor_list_path", lambda path=None: default)
    save_monitor_symbols(["SPY"], default)

    monkeypatch.setattr(kdj_module.user_store, "list_users", lambda: [])
    assert all_monitored_symbols() == ["SPY"]


def test_users_watching_symbol_returns_only_matching_accounts(tmp_path, monkeypatch):
    default = tmp_path / "monitor_list.txt"
    monkeypatch.setattr(kdj_module, "monitor_list_path", lambda path=None: default)
    save_monitor_symbols(["SPY"], default)
    save_user_monitor_symbols(ALICE, ["AAPL"])
    # Bob's effective list is the template (SPY) — he's watching SPY, not AAPL.

    monkeypatch.setattr(kdj_module.user_store, "list_users", lambda: [_user(ALICE), _user(BOB)])
    assert users_watching_symbol("AAPL") == [ALICE]
    assert users_watching_symbol("SPY") == [BOB]
    assert users_watching_symbol("tsla") == []  # nobody watching, and lowercase input still matches


# --- KDJMonitor's recipients_provider (per-symbol email routing) ---

def test_kdj_monitor_defaults_recipients_provider_to_fixed_alert_email():
    mon = KDJMonitor(_EmptyStore())
    assert mon.recipients_provider("AAPL") == [settings.alert_email_to]


def test_kdj_monitor_honors_injected_recipients_provider():
    mon = KDJMonitor(_EmptyStore(), recipients_provider=lambda symbol: [f"{symbol}@watchers.example"])
    assert mon.recipients_provider("AAPL") == ["AAPL@watchers.example"]


class _RawStore:
    """Minimal store double returning a fixed raw-bars DataFrame regardless
    of symbol — used together with monkeypatched resample_bars/kdj (below)
    to drive _check_symbol through a controlled, deterministic cross
    without needing real OHLC data or KDJ math."""

    def __init__(self, df):
        self._df = df

    def get_bars(self, *args, **kwargs):
        return self._df


def _kdj_df_at(start: str, k_values: list[float], d_values: list[float]) -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(k_values), freq="15min", tz="UTC")
    return pd.DataFrame({"k": k_values, "d": d_values, "j": [0.0] * len(k_values)}, index=idx)


def test_check_symbol_emails_every_recipient_on_a_detected_cross(monkeypatch):
    """End-to-end (within _check_symbol) regression guard for the new
    per-recipient email fan-out: the first check only seeds a baseline (no
    email — see module docstring), the second sees a real K/D cross and
    must email every address recipients_provider(symbol) returns, one
    send_email_alert call each."""
    raw_idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=3, freq="1min")
    raw_df = pd.DataFrame({"c": [1.0, 1.0, 1.0]}, index=raw_idx)
    resampled_stub = pd.DataFrame({"c": [1.0] * 25})  # length >= MIN_BARS_FOR_SIGNAL

    baseline_kdj = _kdj_df_at("2026-01-01", [20, 20], [20, 20])  # no transition — just the seed
    crossed_kdj = _kdj_df_at("2026-01-02", [15, 25], [20, 20])   # prev k<d, curr k>d -> "up"
    kdj_returns = [baseline_kdj, crossed_kdj]

    monkeypatch.setattr(kdj_module, "resample_bars", lambda df, tf: resampled_stub)
    monkeypatch.setattr(kdj_module, "kdj", lambda df: kdj_returns.pop(0))

    sent: list[str | None] = []
    monkeypatch.setattr(
        kdj_module,
        "send_email_alert",
        lambda subject, body, to_address=None: sent.append(to_address) or True,
    )

    mon = KDJMonitor(
        _RawStore(raw_df),
        email_alerts_enabled=lambda: True,
        recipients_provider=lambda symbol: [ALICE, BOB],
    )

    asyncio.run(mon._check_symbol("AAPL"))  # baseline — no email yet
    assert sent == []

    asyncio.run(mon._check_symbol("AAPL"))  # real cross now
    assert sent == [ALICE, BOB]


def test_check_symbol_skips_email_when_nobody_is_watching(monkeypatch):
    """If recipients_provider comes back empty (shouldn't normally happen
    for the real union-based provider, but is possible with a custom one),
    _check_symbol must not blow up and must simply send no email."""
    raw_idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=3, freq="1min")
    raw_df = pd.DataFrame({"c": [1.0, 1.0, 1.0]}, index=raw_idx)
    resampled_stub = pd.DataFrame({"c": [1.0] * 25})

    baseline_kdj = _kdj_df_at("2026-01-01", [20, 20], [20, 20])
    crossed_kdj = _kdj_df_at("2026-01-02", [15, 25], [20, 20])
    kdj_returns = [baseline_kdj, crossed_kdj]

    monkeypatch.setattr(kdj_module, "resample_bars", lambda df, tf: resampled_stub)
    monkeypatch.setattr(kdj_module, "kdj", lambda df: kdj_returns.pop(0))

    sent: list[str | None] = []
    monkeypatch.setattr(
        kdj_module,
        "send_email_alert",
        lambda subject, body, to_address=None: sent.append(to_address) or True,
    )

    mon = KDJMonitor(
        _RawStore(raw_df),
        email_alerts_enabled=lambda: True,
        recipients_provider=lambda symbol: [],
    )

    asyncio.run(mon._check_symbol("AAPL"))
    asyncio.run(mon._check_symbol("AAPL"))
    assert sent == []

