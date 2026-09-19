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
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import pandas as pd

from config import settings, PROJECT_ROOT
from data import users as user_store
from data.user_paths import user_slug
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


# --- Per-user monitor lists (Focus Stock Analysis page's Symbol list) ---
# monitor_list.txt above stays in place unchanged as the *default template*
# every new account's own list is seeded from the first time they touch it
# (mirrors data/watchlists.py's per-user watchlists) — after that, each
# account's list is independent, and also determines which symbols the
# background KDJ monitor watches on that account's behalf and where it
# emails a detected cross (see all_monitored_symbols/users_watching_symbol
# and KDJMonitor.recipients_provider below).

def user_monitor_list_path(email: str, path: str | Path | None = None) -> Path:
    d = Path(path or settings.user_monitor_lists_dir)
    if not d.is_absolute():
        d = PROJECT_ROOT / d
    return d / f"{user_slug(email)}.txt"


def load_user_monitor_symbols(email: str) -> list[str]:
    """The signed-in account's own Symbol list. If they haven't got a
    private copy yet (never saved one via the "Edit List" popup), this
    reads the shared default template directly — read-only, nothing is
    written to disk yet — so a brand-new account sees the same starting
    symbols as everyone else until they actually change something."""
    p = user_monitor_list_path(email)
    if not p.exists():
        return load_monitor_symbols()
    text = p.read_text()
    symbols = [s.strip().upper() for s in text.replace(",", " ").split()]
    return [s for s in symbols if s]


def save_user_monitor_symbols(email: str, symbols: list[str]) -> list[str]:
    """Write the signed-in account's own Symbol list, forking it off the
    default template (if this is their first save) — used by the GUI's
    "Edit List" popup (see web/app.py's /api/monitor-list POST endpoint)."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for s in symbols:
        sym = s.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            cleaned.append(sym)
    p = user_monitor_list_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(cleaned) + ("\n" if cleaned else ""))
    return cleaned


def delete_user_monitor_list_file(email: str) -> None:
    """Removes an account's private Symbol list file entirely — called
    when an admin deletes that account (see web/app.py's admin delete
    route), so no orphaned per-user data is left behind."""
    p = user_monitor_list_path(email)
    if p.exists():
        p.unlink()


def all_monitored_symbols() -> list[str]:
    """Union of every registered account's effective Symbol list (their own
    saved list if they have one, else the shared default template) — this
    is what the background KDJ monitor actually watches now that the Focus
    Stock Analysis page's list is per-user. Re-read on every call, so it's
    safe to pass directly as a KDJMonitor's symbols_provider (see
    run_forever) and stay current as accounts edit their lists, with no
    restart needed."""
    symbols: set[str] = set()
    for u in user_store.list_users():
        symbols.update(load_user_monitor_symbols(u.email))
    if not symbols:
        # No accounts yet (or none with any symbols) — fall back to the
        # template so the monitor still watches something sensible.
        symbols.update(load_monitor_symbols())
    return sorted(symbols)


def users_watching_symbol(symbol: str) -> list[str]:
    """Which registered accounts currently have `symbol` in their effective
    Symbol list — used to route a detected KDJ cross's email to exactly the
    account(s) actually watching that symbol, in place of the single fixed
    ALERT_EMAIL_TO address. Pass as a KDJMonitor's recipients_provider."""
    sym = symbol.strip().upper()
    return [u.email for u in user_store.list_users() if sym in load_user_monitor_symbols(u.email)]


def crypto_kdj_alert_state_path(path: str | Path | None = None) -> Path:
    p = Path(path or settings.crypto_kdj_alert_state_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def load_crypto_kdj_email_alerts_enabled(path: str | Path | None = None) -> bool:
    """Live on/off switch for the crypto KDJ monitor's email alerts (the
    "Email Alerts" toggle on the Focus Crypto Analysis page, next to the KDJ
    Alerts chip strip). Re-read on every check cycle (see this module's
    run_forever loop / _check_symbol, and web/app.py's crypto KDJMonitor
    construction) so flipping the switch in the GUI takes effect on the
    monitor's next cycle, no restart needed. Falls back to
    settings.crypto_kdj_email_alerts_enabled (the .env default) the first
    time this file doesn't exist yet, or if it's ever unreadable/corrupt."""
    p = crypto_kdj_alert_state_path(path)
    if not p.exists():
        return settings.crypto_kdj_email_alerts_enabled
    try:
        data = json.loads(p.read_text())
        return bool(data.get("email_alerts_enabled", settings.crypto_kdj_email_alerts_enabled))
    except Exception:
        logger.warning(
            "Failed to read %s — falling back to CRYPTO_KDJ_EMAIL_ALERTS_ENABLED",
            p, exc_info=True,
        )
        return settings.crypto_kdj_email_alerts_enabled


def save_crypto_kdj_email_alerts_enabled(enabled: bool, path: str | Path | None = None) -> bool:
    """Persists the GUI toggle's new state — see web/app.py's
    POST /api/crypto-kdj-email-alerts."""
    p = crypto_kdj_alert_state_path(path)
    p.write_text(json.dumps({"email_alerts_enabled": bool(enabled)}))
    return bool(enabled)


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
        symbols_provider: Callable[[], list[str]] | None = None,
        email_alerts_enabled: Callable[[], bool] | None = None,
        recipients_provider: Callable[[str], list[str]] | None = None,
        label: str = "",
    ):
        self.store = store
        # Optional async callback invoked with a small dict describing the
        # cross, in addition to the email — used to push a live on-screen
        # notification over the WebSocket (see web/app.py).
        self.on_alert = on_alert
        # Where to read the watch list from on every cycle. Defaults to
        # monitor_list.txt (the original stock behavior) — pass a different
        # callable (e.g. `lambda: settings.crypto_kdj_monitor_symbols`) to run
        # a second, independent monitor over a different symbol set, such as
        # the crypto pairs on the Focus Crypto Analysis page.
        self.symbols_provider = symbols_provider or load_monitor_symbols
        # Whether to actually send the email for a detected cross (the
        # on-screen alert and persisted history always fire regardless).
        # Defaults to the global stock switch; pass a different callable to
        # decouple email on/off between separate monitor instances.
        self.email_alerts_enabled = email_alerts_enabled or (
            lambda: settings.kdj_email_alerts_enabled
        )
        # Who to email when a cross is detected for a given symbol.
        # Defaults to the single fixed ALERT_EMAIL_TO address (the original
        # behavior, still used by the crypto monitor) — pass a different
        # callable to route each alert to whichever account(s) actually
        # have that symbol on their own Symbol list, e.g.
        # alerts.kdj_monitor.users_watching_symbol for the per-user stock
        # monitor (see web/app.py's startup).
        self.recipients_provider = recipients_provider or (lambda symbol: [settings.alert_email_to])
        # Purely cosmetic — included in log lines so multiple concurrent
        # monitor instances (e.g. "stock" and "crypto") are distinguishable.
        self.label = label
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

        prefix = f"[{self.label}] " if self.label else ""
        if self.email_alerts_enabled():
            recipients = [addr for addr in self.recipients_provider(symbol) if addr]
            if recipients:
                for to_address in recipients:
                    await asyncio.to_thread(send_email_alert, subject, body, to_address)
            else:
                logger.info(
                    "%sNo one is currently watching %s — skipping email (on-screen alert still fires)",
                    prefix, symbol,
                )
        else:
            logger.info(
                "%semail alerts are off — skipping email for %s (on-screen alert still fires)",
                prefix,
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
        prefix = f"[{self.label}] " if self.label else ""
        while True:
            symbols = self.symbols_provider()
            if not symbols:
                logger.warning("%sNo symbols in monitor list — KDJ monitor is idle.", prefix)
            for symbol in symbols:
                try:
                    await self._check_symbol(symbol)
                except Exception:
                    logger.exception("%sKDJ check failed for %s", prefix, symbol)
            await asyncio.sleep(settings.kdj_check_interval_sec)
