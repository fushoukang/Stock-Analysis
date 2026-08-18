"""
Background monitor: every `settings.kdj_check_interval_sec` seconds (default
120 = 2 minutes), resamples the live 1-minute bar stream into rolling
15-minute bars for each symbol in the monitor list, recomputes KDJ, and
emails an alert the moment K and D cross.

K crossing D is exactly the moment K, D, and J are all equal: J = 3K - 2D,
so if K == D then J = 3K - 2K = K = D too. Detecting a K/D cross therefore
also detects the "K, D, J are the same" condition.

Only genuinely real-time crosses are alerted on:
  - Stale data is ignored: we only alert if the underlying raw market data
    is itself no older than FRESHNESS_WINDOW (2 minutes) — i.e. the cross
    reflects what just happened, not a stale/old feed (checking
    overnight/weekend, or the feed is behind). This is checked against the
    raw 1-minute data's own last timestamp, not the resampled 15Min bin's
    bin-start label (which can lag "now" by up to a full 15-minute bar
    width even when the feed is perfectly live) — otherwise a 2-minute
    window would almost never pass.
  - The first check per symbol after the monitor starts never alerts, even
    if the two most recent bars already show a cross. That state existed
    before we started watching, so it isn't something we caught happening
    live — we just record it as a baseline and only alert on transitions
    observed from the next check onward.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

import pandas as pd

from config import settings, PROJECT_ROOT
from data.store import BarStore
from data.stream import RAW_TIMEFRAME
from data.resample import resample_bars
from indicators.kdj import kdj
from alerts.email_alert import send_email_alert

logger = logging.getLogger("alerts.kdj_monitor")

MONITOR_TIMEFRAME = "15Min"
MONITOR_BAR_MINUTES = 15
LOOKBACK_DAYS = 5  # 1-min history to pull per check, enough to seed a stable KDJ
MIN_BARS_FOR_SIGNAL = 20  # resampled 15Min bars needed before trusting a cross

# Only alert if the underlying raw market data is itself this fresh — i.e.
# the KDJ cross happened recently, not somewhere back in older data.
# Configurable via KDJ_FRESHNESS_WINDOW_MIN in .env (default 5 minutes) —
# kept wider than the check interval so thinner symbols on the free IEX feed
# (fewer trades/minute) still get a real-time alert instead of being
# suppressed just because the feed hasn't ticked very recently.
def _freshness_window() -> pd.Timedelta:
    return pd.Timedelta(minutes=settings.kdj_freshness_window_min)


def monitor_list_path(path: str | Path | None = None) -> Path:
    p = Path(path or settings.monitor_list_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def load_monitor_symbols(path: str | Path | None = None) -> list[str]:
    """Read the watch list. Symbols may be separated by whitespace, commas,
    or newlines — re-read on every cycle so editing the file takes effect
    without restarting the app."""
    p = monitor_list_path(path)
    if not p.exists():
        logger.warning("Monitor list file not found: %s", p)
        return []
    text = p.read_text()
    symbols = [s.strip().upper() for s in text.replace(",", " ").split()]
    return [s for s in symbols if s]


def save_monitor_symbols(symbols: list[str], path: str | Path | None = None) -> list[str]:
    """Write the watch list back to monitor_list.txt, one symbol per line —
    used by the GUI's "Edit List" popup (see web/app.py's
    /api/monitor-list POST endpoint). Dedupes while preserving order,
    uppercases, and drops blanks so a messy paste still saves cleanly."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for s in symbols:
        sym = s.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            cleaned.append(sym)
    p = monitor_list_path(path)
    p.write_text("\n".join(cleaned) + ("\n" if cleaned else ""))
    return cleaned


def detect_cross(kdj_df: pd.DataFrame) -> str | None:
    """Returns 'up' or 'down' if K crossed D between the two most recent
    rows, else None."""
    if len(kdj_df) < 2:
        return None
    prev, curr = kdj_df.iloc[-2], kdj_df.iloc[-1]
    prev_diff = prev["k"] - prev["d"]
    curr_diff = curr["k"] - curr["d"]
    if prev_diff < 0 and curr_diff >= 0:
        return "up"
    if prev_diff > 0 and curr_diff <= 0:
        return "down"
    return None


class KDJMonitor:
    def __init__(
        self,
        store: BarStore,
        on_alert: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.store = store
        # Optional async callback invoked with a small dict describing the
        # cross, in addition to the email — used to push a live on-screen
        # notification over the WebSocket (see web/app.py).
        self.on_alert = on_alert
        self._last_alert_bar: dict[str, pd.Timestamp] = {}
        # Symbols we've seen at least one check for — used to skip alerting
        # on the very first check (see module docstring).
        self._seeded: set[str] = set()

    async def _check_symbol(self, symbol: str) -> None:
        df = self.store.get_bars(symbol, RAW_TIMEFRAME, limit=60 * 24 * LOOKBACK_DAYS)
        if df.empty:
            return
        raw_last_ts = df.index[-1]  # most recent raw 1-min trade, used for freshness

        resampled = resample_bars(df, MONITOR_TIMEFRAME)
        if len(resampled) < MIN_BARS_FOR_SIGNAL:
            return

        kdj_df = kdj(resampled)
        last_ts = kdj_df.index[-1]

        # First check for this symbol: record where things stand without
        # alerting, so we never report a cross that happened before the
        # monitor started watching.
        if symbol not in self._seeded:
            self._seeded.add(symbol)
            self._last_alert_bar[symbol] = last_ts
            logger.debug(
                "%s: KDJ monitor baseline set at %s (K=%.2f D=%.2f) — future "
                "checks will alert on real-time crosses only",
                symbol, last_ts, kdj_df["k"].iloc[-1], kdj_df["d"].iloc[-1],
            )
            return

        # Ignore stale data — checking overnight/weekend, or the feed is
        # behind — so we don't report an old, already-past cross as if it
        # just happened. Compared against the raw 1-minute feed's own last
        # timestamp (not the resampled 15Min bin's bin-start label, which
        # lags "now" by up to 15 minutes even when perfectly live).
        now_utc = pd.Timestamp.now(tz="UTC")
        if now_utc - raw_last_ts > _freshness_window():
            return

        direction = detect_cross(kdj_df)
        if direction is None:
            return

        if self._last_alert_bar.get(symbol) == last_ts:
            return  # already alerted for this bar
        self._last_alert_bar[symbol] = last_ts

        k, d, j = kdj_df["k"].iloc[-1], kdj_df["d"].iloc[-1], kdj_df["j"].iloc[-1]
        display_ts = last_ts.tz_convert("America/New_York") if last_ts.tzinfo else last_ts
        arrow = "crossing UP" if direction == "up" else "crossing DOWN"
        subject = f"KDJ cross: {symbol} ({arrow})"
        body = (
            f"{symbol} KDJ ({MONITOR_TIMEFRAME}) {arrow} at {display_ts.strftime('%Y-%m-%d %H:%M %Z')}.\n\n"
            f"K = {k:.2f}\nD = {d:.2f}\nJ = {j:.2f}\n\n"
            f"K and D have crossed, which means K, D, and J are (momentarily) equal."
        )
        logger.info("KDJ cross detected: %s", subject)
        if settings.kdj_email_alerts_enabled:
            await asyncio.to_thread(send_email_alert, subject, body)
        else:
            logger.info(
                "KDJ_EMAIL_ALERTS_ENABLED is off — skipping email for %s (on-screen alert still fires)",
                symbol,
            )

        if self.on_alert is not None:
            alert_payload = {
                "symbol": symbol,
                "direction": direction,
                "k": round(float(k), 2),
                "d": round(float(d), 2),
                "j": round(float(j), 2),
                "timeframe": MONITOR_TIMEFRAME,
                "bar_time": display_ts.isoformat(),
            }
            try:
                await self.on_alert(alert_payload)
            except Exception:
                logger.exception("KDJ on_alert callback failed for %s", symbol)

    async def run_forever(self) -> None:
        while True:
            symbols = load_monitor_symbols()
            if not symbols:
                logger.warning("No symbols in monitor list — KDJ monitor is idle.")
            for symbol in symbols:
                try:
                    await self._check_symbol(symbol)
                except Exception:
                    logger.exception("KDJ check failed for %s", symbol)
            await asyncio.sleep(settings.kdj_check_interval_sec)
