"""Tests for data/store.py's BarStore: bar upsert/read, pruning, and the
KDJ alert history table (record/get/backfill)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from data.store import BarStore, _classify_price_move


@pytest.fixture
def store(tmp_path):
    return BarStore(tmp_path / "test_store.db")


def _minute_bars_df(n: int, start: datetime, step_price: float = 0.01) -> pd.DataFrame:
    idx = [start + timedelta(minutes=i) for i in range(n)]
    price = [100.0 + i * step_price for i in range(n)]
    return pd.DataFrame(
        {"open": price, "high": price, "low": price, "close": price, "volume": [1000.0] * n},
        index=pd.DatetimeIndex(idx, tz="UTC"),
    )


def test_classify_price_move():
    assert _classify_price_move(101.0, 100.0) == "up"
    assert _classify_price_move(99.0, 100.0) == "down"
    assert _classify_price_move(100.0, 100.0) == "flat"


def test_upsert_and_get_bars_roundtrip(store):
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    df = _minute_bars_df(5, now)
    store.upsert_bars_df("AAPL", "1Min", df)

    out = store.get_bars("aapl", "1Min", limit=100)  # lowercase symbol should still match
    assert len(out) == 5
    assert list(out["close"]) == list(df["close"])


def test_upsert_bar_conflict_updates_existing_row(store):
    ts = datetime.now(timezone.utc)
    store.upsert_bar("AAPL", "1Min", ts, 1, 2, 0.5, 1.5, 100)
    store.upsert_bar("AAPL", "1Min", ts, 1, 2, 0.5, 1.9, 200)  # same key, new close/volume

    out = store.get_bars("AAPL", "1Min")
    assert len(out) == 1  # upsert, not a duplicate row
    assert out["close"].iloc[0] == pytest.approx(1.9)
    assert out["volume"].iloc[0] == pytest.approx(200)


def test_prune_old_bars_removes_only_bars_older_than_retention(store):
    now = datetime.now(timezone.utc)
    old = _minute_bars_df(1, now - timedelta(days=45))
    recent = _minute_bars_df(1, now - timedelta(days=5))
    store.upsert_bars_df("AAPL", "1Min", old)
    store.upsert_bars_df("AAPL", "1Min", recent)

    deleted = store.prune_old_bars(retention_days=30)
    assert deleted == 1

    remaining = store.get_bars("AAPL", "1Min", limit=100)
    assert len(remaining) == 1
    # Only the recent bar (5 days old) should have survived; the 45-day-old
    # one was pruned.
    assert remaining["close"].iloc[0] == pytest.approx(recent["close"].iloc[0])


def test_get_bar_at_or_after_finds_first_matching_bar(store):
    now = datetime.now(timezone.utc) - timedelta(hours=2)
    df = _minute_bars_df(10, now)
    store.upsert_bars_df("AAPL", "1Min", df)

    target = now + timedelta(minutes=5, seconds=30)  # between bar 5 and bar 6
    bar = store.get_bar_at_or_after("AAPL", "1Min", target)
    assert bar is not None
    assert bar["close"] == pytest.approx(100.0 + 6 * 0.01)


def test_get_bar_at_or_after_returns_none_when_nothing_matches(store):
    now = datetime.now(timezone.utc) - timedelta(hours=2)
    df = _minute_bars_df(5, now)
    store.upsert_bars_df("AAPL", "1Min", df)

    far_future = now + timedelta(days=10)
    assert store.get_bar_at_or_after("AAPL", "1Min", far_future) is None


def test_record_and_get_kdj_alerts(store):
    alert_id = store.record_kdj_alert(
        symbol="tqqq", direction="up", k=25.0, d=20.0, j=35.0,
        timeframe="15Min", bar_time=datetime.now(timezone.utc), price_at_alert=100.5,
    )
    assert isinstance(alert_id, int) and alert_id > 0

    alerts = store.get_kdj_alerts("TQQQ")
    assert len(alerts) == 1
    assert alerts[0]["symbol"] == "TQQQ"
    assert alerts[0]["direction"] == "up"
    assert alerts[0]["outcome_1h"] is None
    assert alerts[0]["outcome_1d"] is None


def test_get_kdj_alerts_filters_by_symbol(store):
    store.record_kdj_alert(
        symbol="AAA", direction="up", k=1, d=1, j=1, timeframe="15Min",
        bar_time=datetime.now(timezone.utc), price_at_alert=10.0,
    )
    store.record_kdj_alert(
        symbol="BBB", direction="down", k=1, d=1, j=1, timeframe="15Min",
        bar_time=datetime.now(timezone.utc), price_at_alert=20.0,
    )
    assert len(store.get_kdj_alerts("AAA")) == 1
    assert len(store.get_kdj_alerts()) == 2


def test_backfill_kdj_alert_outcomes_fills_in_1h_and_1d(store):
    now = datetime.now(timezone.utc)

    # Seed enough 1-min bars spanning 2 days for both +1h and +1d lookups.
    df = _minute_bars_df(60 * 24 * 2, now - timedelta(days=2))
    store.upsert_bars_df("AAA", "1Min", df)

    alert_id = store.record_kdj_alert(
        symbol="AAA", direction="up", k=25, d=20, j=35, timeframe="15Min",
        bar_time=now - timedelta(days=2), price_at_alert=100.5,
    )
    # record_kdj_alert always stamps alert_time as "now" — backdate it so
    # the backfill's time gates (>= 1h / >= 1d elapsed) actually trigger.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE kdj_alerts SET alert_time = ? WHERE id = ?",
            ((now - timedelta(days=2)).isoformat(), alert_id),
        )
        conn.commit()

    updated = store.backfill_kdj_alert_outcomes("1Min")
    assert updated == 1

    alerts = store.get_kdj_alerts("AAA")
    assert alerts[0]["outcome_1h"] in ("up", "down", "flat")
    assert alerts[0]["outcome_1d"] in ("up", "down", "flat")
    assert alerts[0]["price_1h"] is not None
    assert alerts[0]["price_1d"] is not None


def test_backfill_skips_alerts_too_recent_to_have_a_1h_bar_yet(store):
    now = datetime.now(timezone.utc)
    df = _minute_bars_df(30, now - timedelta(minutes=30))
    store.upsert_bars_df("BBB", "1Min", df)

    store.record_kdj_alert(
        symbol="BBB", direction="down", k=80, d=85, j=70, timeframe="15Min",
        bar_time=now, price_at_alert=200.0,
    )
    updated = store.backfill_kdj_alert_outcomes("1Min")
    assert updated == 0

    alerts = store.get_kdj_alerts("BBB")
    assert alerts[0]["outcome_1h"] is None
    assert alerts[0]["outcome_1d"] is None
