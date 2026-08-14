"""
SQLite storage for OHLCV bars.

Schema: one row per (symbol, timeframe, timestamp). Timeframe is a string
like "1Min", "5Min", "1Hour", "1Day" so multiple resolutions can coexist.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

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
"""


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
