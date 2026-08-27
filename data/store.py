"""
SQLite storage for OHLCV bars.

Schema: one row per (symbol, timeframe, timestamp). Timeframe is a string
like "1Min", "5Min", "1Hour", "1Day" so multiple resolutions can coexist.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger("data.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol    TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts        TEXT NOT NULL,  -- ISO8601 UTC timestamp
    open      REAL NOT NULL,
    high      REAL NOT NULL,
    low       REAL NOT NULL,
    close     REAL NOT NULL,
    volume    REAL NOT NULL,
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_lookup ON bars (symbol, timeframe, ts);

-- One row per KDJ cross alert fired by alerts/kdj_monitor.py. `price_1h`/
-- `price_1d`/`outcome_1h`/`outcome_1d` start NULL and are filled in later by
-- BarStore.backfill_kdj_alert_outcomes(), once enough time has passed for
-- the "1 hour later" / "1 day later" bars to actually exist — this is what
-- lets the KDJ Alert History view show whether each past alert was
-- followed by the price actually moving the direction the cross implied.
CREATE TABLE IF NOT EXISTS kdj_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,  -- 'up' or 'down' (K crossing D)
    k               REAL NOT NULL,
    d               REAL NOT NULL,
    j               REAL NOT NULL,
    timeframe       TEXT NOT NULL,  -- the KDJ timeframe, e.g. "15Min"
    bar_time        TEXT NOT NULL,  -- ISO8601 UTC, the crossing bar's timestamp
    alert_time      TEXT NOT NULL,  -- ISO8601 UTC, when the alert was recorded
    price_at_alert  REAL,           -- last raw close at alert time
    price_1h        REAL,
    price_1d        REAL,
    outcome_1h      TEXT,           -- 'up' / 'down' / 'flat', once backfilled
    outcome_1d      TEXT
);
CREATE INDEX IF NOT EXISTS idx_kdj_alerts_symbol_time ON kdj_alerts (symbol, alert_time);
"""


def _classify_price_move(new_price: float, old_price: float) -> str:
    """Simple up/down/flat classification used for KDJ alert outcomes."""
    if new_price > old_price:
        return "up"
    if new_price < old_price:
        return "down"
    return "flat"


class BarStore:
    """Thread-safe SQLite-backed store for OHLCV bars."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_bar(
        self,
        symbol: str,
        timeframe: str,
        ts,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        ts_iso = pd.Timestamp(ts).tz_convert("UTC").isoformat() if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts, tz="UTC").isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bars (symbol, timeframe, ts, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume
                """,
                (symbol.upper(), timeframe, ts_iso, open_, high, low, close, volume),
            )

    def upsert_bars_df(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        """Bulk upsert from a DataFrame indexed by timestamp with OHLCV columns."""
        with self._lock, self._connect() as conn:
            rows = []
            for ts, row in df.iterrows():
                ts_ = pd.Timestamp(ts)
                ts_iso = (ts_.tz_convert("UTC") if ts_.tzinfo else ts_.tz_localize("UTC")).isoformat()
                rows.append(
                    (
                        symbol.upper(),
                        timeframe,
                        ts_iso,
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row["volume"]),
                    )
                )
            conn.executemany(
                """
                INSERT INTO bars (symbol, timeframe, ts, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume
                """,
                rows,
            )

    def prune_old_bars(self, retention_days: int) -> int:
        """Delete bars older than `retention_days` (by their `ts` column,
        compared against wall-clock UTC now). The `bars` table only ever
        grows otherwise — the live stream inserts a new 1-min bar per
        symbol every minute the market's open, forever, with nothing
        removing old ones. Returns the number of rows deleted, so callers
        can log it.

        `retention_days` must stay comfortably above every local reader's
        own lookback need — currently the largest is
        alerts.kdj_monitor.LOOKBACK_DAYS (5 days) and web/app.py's
        MAX_TRADING_DAYS_DISPLAYED (2 trading days, so well under a week of
        calendar days) — so the default (see config.py's
        BAR_RETENTION_DAYS, 30) leaves plenty of headroom rather than
        cutting it close.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM bars WHERE ts < ?", (cutoff,))
            deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        if deleted:
            logger.info(
                "Pruned %d bar(s) older than %s (retention_days=%d).",
                deleted, cutoff, retention_days,
            )
        return deleted

    def get_bars(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> pd.DataFrame:
        with self._lock, self._connect() as conn:
            df = pd.read_sql_query(
                """
                SELECT ts, open, high, low, close, volume FROM bars
                WHERE symbol = ? AND timeframe = ?
                ORDER BY ts DESC LIMIT ?
                """,
                conn,
                params=(symbol.upper(), timeframe, limit),
            )
        if df.empty:
            return df
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.sort_values("ts").set_index("ts")
        return df

    def get_bar_at_or_after(self, symbol: str, timeframe: str, target_ts) -> dict | None:
        """First stored bar with ts >= target_ts, or None if we don't have
        one yet (either too far in the future, or it fell outside the
        retention window). Used by backfill_kdj_alert_outcomes() to find
        "the price ~1h/~1d after this alert fired"."""
        ts = pd.Timestamp(target_ts)
        ts_iso = (ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT ts, close FROM bars
                WHERE symbol = ? AND timeframe = ? AND ts >= ?
                ORDER BY ts ASC LIMIT 1
                """,
                (symbol.upper(), timeframe, ts_iso),
            ).fetchone()
        if row is None:
            return None
        return {"ts": row[0], "close": row[1]}

    def record_kdj_alert(
        self,
        symbol: str,
        direction: str,
        k: float,
        d: float,
        j: float,
        timeframe: str,
        bar_time,
        price_at_alert: float | None,
    ) -> int:
        """Persist a KDJ cross alert (called from alerts/kdj_monitor.py the
        moment a cross is detected), so the GUI can later show alert
        history and — once backfilled — how price actually moved
        afterwards. Returns the new row's id."""
        bar_ts = pd.Timestamp(bar_time)
        bar_ts_iso = (bar_ts.tz_convert("UTC") if bar_ts.tzinfo else bar_ts.tz_localize("UTC")).isoformat()
        alert_ts_iso = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO kdj_alerts
                    (symbol, direction, k, d, j, timeframe, bar_time, alert_time, price_at_alert)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol.upper(), direction, float(k), float(d), float(j),
                    timeframe, bar_ts_iso, alert_ts_iso,
                    None if price_at_alert is None else float(price_at_alert),
                ),
            )
            return cur.lastrowid

    def get_kdj_alerts(self, symbol: str | None = None, limit: int = 100) -> list[dict]:
        """Most recent KDJ alerts, newest first. Filters by symbol if given."""
        query = (
            "SELECT id, symbol, direction, k, d, j, timeframe, bar_time, alert_time, "
            "price_at_alert, price_1h, price_1d, outcome_1h, outcome_1d FROM kdj_alerts"
        )
        params: tuple = ()
        if symbol:
            query += " WHERE symbol = ?"
            params = (symbol.upper(),)
        query += " ORDER BY alert_time DESC LIMIT ?"
        params = params + (limit,)
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def backfill_kdj_alert_outcomes(self, raw_timeframe: str = "1Min") -> int:
        """For every alert still missing its 1h and/or 1d outcome, check
        whether enough wall-clock time has passed and, if so, look up the
        nearest stored raw bar at/after that offset and record whether
        price was up/down/flat relative to price_at_alert. Returns the
        number of alert rows updated. Safe to call repeatedly/often — rows
        with both outcomes already filled in are skipped entirely."""
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            pending = conn.execute(
                """
                SELECT id, symbol, alert_time, price_at_alert, outcome_1h, outcome_1d
                FROM kdj_alerts WHERE outcome_1h IS NULL OR outcome_1d IS NULL
                """
            ).fetchall()

        updated = 0
        for row in pending:
            if row["price_at_alert"] is None:
                continue
            alert_ts = pd.Timestamp(row["alert_time"])
            if alert_ts.tzinfo is None:
                alert_ts = alert_ts.tz_localize("UTC")
            changes: dict[str, float | str] = {}

            if row["outcome_1h"] is None and now - alert_ts.to_pydatetime() >= timedelta(hours=1):
                bar = self.get_bar_at_or_after(row["symbol"], raw_timeframe, alert_ts + pd.Timedelta(hours=1))
                if bar is not None:
                    changes["price_1h"] = bar["close"]
                    changes["outcome_1h"] = _classify_price_move(bar["close"], row["price_at_alert"])

            if row["outcome_1d"] is None and now - alert_ts.to_pydatetime() >= timedelta(days=1):
                bar = self.get_bar_at_or_after(row["symbol"], raw_timeframe, alert_ts + pd.Timedelta(days=1))
                if bar is not None:
                    changes["price_1d"] = bar["close"]
                    changes["outcome_1d"] = _classify_price_move(bar["close"], row["price_at_alert"])

            if not changes:
                continue
            set_clause = ", ".join(f"{col} = ?" for col in changes)
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE kdj_alerts SET {set_clause} WHERE id = ?",
                    (*changes.values(), row["id"]),
                )
            updated += 1

        if updated:
            logger.info("Backfilled outcomes for %d KDJ alert(s).", updated)
        return updated
