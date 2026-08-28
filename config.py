"""
Central configuration, loaded from environment variables / a .env file.

Copy .env.example to .env and fill in your Alpaca API keys before running
anything that talks to Alpaca.
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.data.enums import DataFeed
from market_holidays import is_market_holiday

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("config")

EASTERN = ZoneInfo("America/New_York")


def detect_local_ipv4() -> str:
    """Best-effort detection of this machine's LAN IPv4 address, so the
    server can bind to it without a real address ever needing to be typed
    into .env or read out loud. Uses the standard no-traffic trick: opening
    a UDP socket "connected" to a public address doesn't send any packets,
    it just asks the OS to pick the local interface/IP that would be used
    for that route, which is normally the LAN-facing one (Wi-Fi/Ethernet)
    rather than the loopback interface. Falls back to 127.0.0.1 (local-only)
    if detection fails for any reason, e.g. no network at all.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        logger.warning(
            "Could not auto-detect a LAN IPv4 address — falling back to "
            "127.0.0.1 (only reachable from this machine). Set HOST in .env "
            "explicitly to override."
        )
        return "127.0.0.1"
    finally:
        sock.close()


def _host_env() -> str:
    val = (os.getenv("HOST") or "auto").strip()
    if val.lower() == "auto":
        return detect_local_ipv4()
    return val


def _bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _list_env(name: str, default: list[str]) -> list[str]:
    val = os.getenv(name)
    if not val:
        return default
    return [s.strip().upper() for s in val.split(",") if s.strip()]


def _time_env(name: str, default: str) -> dt_time:
    val = (os.getenv(name) or default).strip()
    hour, minute = val.split(":")
    return dt_time(int(hour), int(minute))


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    paper: bool = field(default_factory=lambda: _bool_env("ALPACA_PAPER", False))
    data_feed: str = field(default_factory=lambda: os.getenv("ALPACA_DATA_FEED", "iex"))
    # Alpaca only trades/streams equities and ETFs, not raw index tickers
    # (e.g. ^GSPC isn't a valid symbol there), so the default watchlist uses
    # the standard ETF proxies for the major US market indexes:
    #   SPY = S&P 500, QQQ = Nasdaq-100, DIA = Dow Jones Industrial Average,
    #   IWM = Russell 2000.
    watchlist: list[str] = field(
        default_factory=lambda: _list_env(
            "WATCHLIST", ["SPY", "QQQ", "DIA", "IWM"]
        )
    )
    # Default crypto pairs backfilled/streamed on startup for the "Focus
    # Crypto Analysis" page — same account credentials as stocks (Alpaca
    # runs its own crypto exchange), but a separate watchlist since crypto
    # symbols use a BASE/QUOTE format (e.g. BTC/USDT) and trade 24/7 rather
    # than during NYSE/Nasdaq hours.
    crypto_watchlist: list[str] = field(
        default_factory=lambda: _list_env(
            "CRYPTO_WATCHLIST", ["BTC/USDT", "ETH/USDT"]
        )
    )
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data_store.db"))
    # How many days of raw 1-min bars to keep in data_store.db before a
    # periodic background job prunes them (see web/app.py's
    # _bar_retention_loop). The bars table otherwise grows forever — every
    # symbol gets a new row every minute the market's open. Must stay well
    # above every local reader's own lookback need (currently the largest
    # is the KDJ monitor's 5-day lookback) — the default leaves a wide
    # margin rather than cutting that close.
    bar_retention_days: int = field(
        default_factory=lambda: int(os.getenv("BAR_RETENTION_DAYS", "30"))
    )
    # JSON file storing the user's named "Watchlists" (name + note + symbols
    # each) shown in the GUI's Watchlists category — NOT the same thing as
    # `watchlist` above, which is just the default set of symbols this app
    # backfills/streams on startup.
    watchlists_path: str = field(
        default_factory=lambda: os.getenv("WATCHLISTS_PATH", "watchlists.json")
    )
    # "auto" (the default) detects this machine's LAN IPv4 at startup so the
    # GUI is reachable from other devices on the network without hardcoding
    # an address in .env. Set HOST explicitly (e.g. 127.0.0.1) to override.
    host: str = field(default_factory=_host_env)
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))

    # --- KDJ cross alerting ---
    # File of symbols (whitespace/comma-separated) to watch for a KDJ K/D cross.
    monitor_list_path: str = field(
        default_factory=lambda: os.getenv("MONITOR_LIST_PATH", "monitor_list.txt")
    )
    # How often (seconds) to recompute KDJ and check for a fresh cross.
    kdj_check_interval_sec: int = field(
        default_factory=lambda: int(os.getenv("KDJ_CHECK_INTERVAL_SEC", "120"))
    )
    # How fresh the underlying raw market data must be (minutes) for a cross
    # to be alerted on — wider than the check interval so thinner symbols
    # (fewer trades on the free IEX feed) still get a real-time alert instead
    # of being suppressed just because the feed hasn't ticked in the last
    # couple of minutes.
    kdj_freshness_window_min: int = field(
        default_factory=lambda: int(os.getenv("KDJ_FRESHNESS_WINDOW_MIN", "5"))
    )
    # Master on/off switch for KDJ cross email alerts. The monitor still runs
    # and detects/logs crosses either way (and still pushes the on-screen
    # WebSocket alert) — this only controls whether the email actually goes
    # out. Set to false to silence emails without disabling the monitor.
    kdj_email_alerts_enabled: bool = field(
        default_factory=lambda: _bool_env("KDJ_EMAIL_ALERTS_ENABLED", True)
    )
    # SMTP credentials used to send the alert email (no MCP mail connector here
    # supports unattended sending — only drafting — so this goes out via plain
    # SMTP). For Gmail: smtp.gmail.com / port 587 / an App Password (not your
    # normal password) at https://myaccount.google.com/apppasswords.
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    smtp_username: str = field(default_factory=lambda: os.getenv("SMTP_USERNAME", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_from_address: str = field(
        default_factory=lambda: os.getenv("SMTP_FROM_ADDRESS") or os.getenv("SMTP_USERNAME", "")
    )
    # Where KDJ cross alerts get emailed.
    alert_email_to: str = field(
        default_factory=lambda: os.getenv("ALERT_EMAIL_TO", "f8yang@hotmail.com")
    )

    # --- Market data window ---
    # Alpaca should only be called (WebSocket stream + REST) during early +
    # regular + post market hours. Outside this window the app must not
    # place any Alpaca API calls at all — the GUI shows "The market has
    # closed" instead. Defaults cover 6:30 AM - 6:00 PM ET, which spans
    # pre-market, the 9:30-16:00 regular session, and after-hours trading.
    market_data_start_et: dt_time = field(
        default_factory=lambda: _time_env("MARKET_DATA_START_ET", "06:30")
    )
    market_data_end_et: dt_time = field(
        default_factory=lambda: _time_env("MARKET_DATA_END_ET", "18:00")
    )

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def has_smtp_credentials(self) -> bool:
        return bool(self.smtp_host and self.smtp_username and self.smtp_password)

    def data_feed_enum(self) -> DataFeed:
        try:
            return DataFeed(self.data_feed.strip().lower())
        except ValueError as exc:
            valid = ", ".join(f.value for f in DataFeed)
            raise ValueError(
                f"Invalid ALPACA_DATA_FEED '{self.data_feed}'. Valid options: {valid}"
            ) from exc

    def db_full_path(self) -> Path:
        p = Path(self.db_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p


settings = Settings()


def is_within_market_data_window(now: datetime | None = None) -> bool:
    """True if Alpaca should be called right now (WebSocket stream or REST),
    False otherwise. This is the single source of truth for that decision —
    data/stream.py and web/app.py both gate their Alpaca calls on it, so the
    app makes zero API calls to Alpaca outside the configured window
    (default 6:30 AM - 6:00 PM ET, Mon-Fri), including on NYSE/Nasdaq market
    holidays (see market_holidays.py — a rule-based calendar, so this stays
    correct in future years without manual updates). Doesn't distinguish
    early-close half-days (e.g. the day after Thanksgiving) — those are
    still treated as a normal trading day; Alpaca just won't have data past
    the actual early close, same as any other day the feed goes quiet.
    """
    now_et = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    if now_et.weekday() >= 5:  # Saturday/Sunday
        return False
    if is_market_holiday(now_et.date()):
        return False
    return settings.market_data_start_et <= now_et.time() <= settings.market_data_end_et
