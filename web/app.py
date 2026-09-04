"""
FastAPI web application: REST endpoints for symbols/bars/charts, a WebSocket
for live bar updates, and the static GUI.

This app is read-only market-data analysis. It never places, modifies, or
cancels orders — there is intentionally no trading endpoint.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings, is_within_market_data_window
from data.store import BarStore
from data.historical import fetch_bars, fetch_latest_prices, fetch_previous_closes, TIMEFRAME_MAP
from data.stream import LiveStreamManager, RAW_TIMEFRAME
from data.crypto_historical import fetch_crypto_bars
from data.crypto_stream import CryptoLiveStreamManager
from data.crypto_info import crypto_display_name, binance_quote_url
from data.resample import resample_bars, BAR_MINUTES
from charts.candlestick import build_candlestick_figure
from indicators.signals import compute_signals, compute_composite_signal
from data.company_names import get_company_name
from data.watchlists import (
    create_watchlist,
    delete_watchlist,
    get_watchlist,
    load_watchlists,
    update_watchlist,
)
from alerts.kdj_monitor import (
    KDJMonitor,
    load_monitor_symbols,
    save_monitor_symbols,
    load_crypto_kdj_email_alerts_enabled,
    save_crypto_kdj_email_alerts_enabled,
)
from screeners.cnbc import cnbc_quote_url
from screeners.tradingview import fetch_52_week_high, fetch_52_week_low, ScreenerError as TVScreenerError
from screeners.halts import (
    fetch_current_halts,
    is_volatility_halt,
    reason_label,
    compute_halt_direction,
    ScreenerError as HaltsScreenerError,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Stock Trading Analysis")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

store = BarStore(settings.db_full_path())
stream_manager: LiveStreamManager | None = None
crypto_stream_manager: CryptoLiveStreamManager | None = None
_ws_clients: set[WebSocket] = set()

# Tracks the market data window's open/closed state across polls, so
# _market_hours_loop can detect the closed->open transition and trigger
# exactly one backfill when it happens (not a fresh backfill on every poll).
_market_window_open: bool = False


async def _backfill_all_symbols(symbols: list[str]) -> None:
    """REST-fetch recent history for each symbol and seed the local store.
    Only ever called while inside the market data window (see call sites) —
    this is the only place besides the live stream that talks to Alpaca."""
    for symbol in symbols:
        try:
            df = fetch_bars(symbol, timeframe="1Min", lookback_days=5)
            if not df.empty:
                store.upsert_bars_df(symbol, RAW_TIMEFRAME, df)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Historical backfill failed for %s: %s", symbol, exc)


async def _backfill_all_crypto_symbols(symbols: list[str]) -> None:
    """REST-fetch recent history for each crypto pair and seed the local
    store — unlike _backfill_all_symbols, not gated by
    is_within_market_data_window() at all, since crypto trades 24/7."""
    for symbol in symbols:
        try:
            df = fetch_crypto_bars(symbol, timeframe="1Min", lookback_days=5)
            if not df.empty:
                store.upsert_bars_df(symbol, RAW_TIMEFRAME, df)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Historical crypto backfill failed for %s: %s", symbol, exc)


_BAR_PRUNE_INTERVAL_SEC = 6 * 60 * 60  # every 6 hours


async def _bar_retention_loop() -> None:
    """Periodically deletes bars older than settings.bar_retention_days from
    the local SQLite store (see BarStore.prune_old_bars) — without this,
    data_store.db only ever grows, since the live stream inserts a new row
    per symbol every minute the market's open and nothing else removes old
    ones. Runs once immediately (in case the app's been down a while and a
    backlog built up before this loop existed), then on a fixed interval —
    deliberately not tied to the market data window, since pruning is cheap
    local disk I/O, not an Alpaca API call."""
    while True:
        try:
            await asyncio.to_thread(store.prune_old_bars, settings.bar_retention_days)
        except Exception:
            logger.exception("Bar retention prune failed")
        await asyncio.sleep(_BAR_PRUNE_INTERVAL_SEC)


_KDJ_ALERT_BACKFILL_INTERVAL_SEC = 15 * 60  # every 15 minutes


async def _kdj_alert_backfill_loop() -> None:
    """Periodically fills in each KDJ alert's price_1h/price_1d/outcome_1h/
    outcome_1d once enough wall-clock time has passed (see
    BarStore.backfill_kdj_alert_outcomes) — lets the KDJ Alert History view
    show whether price actually moved the direction each past cross
    implied. Independent of Alpaca credentials: it only reads/writes bars
    and alerts already sitting in the local SQLite store."""
    while True:
        try:
            await asyncio.to_thread(store.backfill_kdj_alert_outcomes, RAW_TIMEFRAME)
        except Exception:
            logger.exception("KDJ alert outcome backfill failed")
        await asyncio.sleep(_KDJ_ALERT_BACKFILL_INTERVAL_SEC)


async def _market_hours_loop(all_symbols: list[str]) -> None:
    """Polls the market data window and reacts to transitions: backfills
    once right when it opens (covering the case where the app was started,
    or has been sitting, outside the window — the live stream will connect
    on its own, but a fresh REST backfill fills in whatever happened since
    the local store was last updated). Outside the window this loop makes no
    Alpaca calls itself, it just watches the clock."""
    global _market_window_open
    while True:
        await asyncio.sleep(30)
        now_open = is_within_market_data_window()
        if now_open and not _market_window_open:
            logger.info("Market data window opened — backfilling from Alpaca REST.")
            await _backfill_all_symbols(all_symbols)
        elif not now_open and _market_window_open:
            logger.info(
                "Market data window closed (%s ET) — the market has closed; "
                "no further Alpaca API calls until %s ET.",
                settings.market_data_end_et.strftime("%H:%M"),
                settings.market_data_start_et.strftime("%H:%M"),
            )
        _market_window_open = now_open


@app.on_event("startup")
async def on_startup() -> None:
    global stream_manager, crypto_stream_manager, _market_window_open

    # Local DB maintenance, independent of Alpaca credentials — runs
    # regardless of whether the rest of startup bails out below.
    asyncio.create_task(_bar_retention_loop())
    asyncio.create_task(_kdj_alert_backfill_loop())

    if not settings.has_credentials():
        logger.warning(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY not set — running with no live "
            "stream or historical fetch. Add them to .env and restart."
        )
        return

    # Seed the DB with recent history so the chart isn't empty on first load.
    # Also backfill/stream the KDJ monitor-list symbols even if they aren't in
    # the GUI watchlist, so the monitor has live data to check every minute.
    monitor_symbols = load_monitor_symbols()
    all_symbols = sorted(set(settings.watchlist) | set(monitor_symbols))

    _market_window_open = is_within_market_data_window()
    if _market_window_open:
        await _backfill_all_symbols(all_symbols)
    else:
        logger.info(
            "Starting outside the market data window (%s-%s ET) — skipping "
            "initial backfill; the live stream stays idle and will connect, "
            "and a backfill will run automatically, once the window opens.",
            settings.market_data_start_et.strftime("%H:%M"),
            settings.market_data_end_et.strftime("%H:%M"),
        )

    # LiveStreamManager self-gates on the market data window (see
    # data/stream.py) — safe to start unconditionally here even outside the
    # window; it'll simply stay idle and make no Alpaca calls until it opens.
    stream_manager = LiveStreamManager(store, all_symbols)
    stream_manager.start()
    asyncio.create_task(_broadcast_loop())
    asyncio.create_task(_market_hours_loop(all_symbols))

    if monitor_symbols:
        if not settings.has_smtp_credentials():
            logger.warning(
                "SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD not set — the KDJ monitor "
                "will detect crosses but can't email alerts until these are set."
            )
        kdj_monitor = KDJMonitor(store, on_alert=_broadcast_kdj_alert)
        asyncio.create_task(kdj_monitor.run_forever())
        logger.info(
            "KDJ monitor started (every %ss) for: %s",
            settings.kdj_check_interval_sec,
            ", ".join(monitor_symbols),
        )

    # --- Crypto ("Focus Crypto Analysis" page) ---
    # Same account credentials as stocks — Alpaca just runs a separate
    # crypto exchange/data path. Unlike the stock backfill/stream above,
    # deliberately NOT gated by the market data window: crypto trades 24/7,
    # so there's no "closed" state to wait out here.
    crypto_symbols = settings.crypto_watchlist
    if crypto_symbols:
        await _backfill_all_crypto_symbols(crypto_symbols)
        crypto_stream_manager = CryptoLiveStreamManager(store, crypto_symbols)
        crypto_stream_manager.start()
        logger.info("Crypto live stream started for: %s", ", ".join(crypto_symbols))

    # Crypto KDJ cross monitor — same K/D-cross detection and email alerting
    # as the stock monitor above, but watching CRYPTO_KDJ_MONITOR_SYMBOLS
    # (default BTC/USDT) via its own independent watch-list source and email
    # on/off switch, and running unconditionally since crypto trades 24/7 (no
    # market-data-window gating here, unlike the stock backfill above).
    crypto_kdj_symbols = settings.crypto_kdj_monitor_symbols
    if crypto_kdj_symbols:
        if not settings.has_smtp_credentials():
            logger.warning(
                "SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD not set — the crypto KDJ "
                "monitor will detect crosses but can't email alerts until these are set."
            )
        crypto_kdj_monitor = KDJMonitor(
            store,
            on_alert=_broadcast_kdj_alert,
            symbols_provider=lambda: settings.crypto_kdj_monitor_symbols,
            email_alerts_enabled=load_crypto_kdj_email_alerts_enabled,
            label="crypto",
        )
        asyncio.create_task(crypto_kdj_monitor.run_forever())
        logger.info(
            "Crypto KDJ monitor started (every %ss) for: %s",
            settings.kdj_check_interval_sec,
            ", ".join(crypto_kdj_symbols),
        )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if stream_manager is not None:
        stream_manager.stop()
    if crypto_stream_manager is not None:
        crypto_stream_manager.stop()


async def _broadcast(payload: dict) -> None:
    """Fan a JSON-serializable payload out to every connected WebSocket client."""
    if not _ws_clients:
        return
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(payload)
        except Exception:  # noqa: BLE001
            dead.add(ws)
    _ws_clients.difference_update(dead)


async def _broadcast_loop() -> None:
    """Poll the stream managers' update queues and fan out to WebSocket
    clients: heavier "bars" updates (drive a full chart redraw, roughly once
    a minute per symbol) and lightweight "price" updates (drive an in-place
    title update only — see index.html — from individual trade ticks, so the
    displayed price stays continuously current between bar closes).

    Drains both the stock stream_manager and the crypto_stream_manager into
    the same broadcast messages — stock tickers never contain "/" and
    crypto pairs always do (e.g. "BTC/USDT"), so the two symbol spaces
    never collide and the frontend's existing symbol-matching logic (see
    index.html) works unchanged for either asset class without needing a
    separate message type."""
    while True:
        await asyncio.sleep(1.0)

        if stream_manager is not None:
            updates = stream_manager.drain_updates()
            if updates:
                await _broadcast({"type": "bars", "data": [u.to_dict() for u in updates]})

            price_updates = stream_manager.drain_price_updates()
            if price_updates:
                await _broadcast(
                    {
                        "type": "price",
                        "data": [
                            {"symbol": sym, "price": price, "ts": ts}
                            for sym, (price, ts) in price_updates.items()
                        ],
                    }
                )

        if crypto_stream_manager is not None:
            crypto_updates = crypto_stream_manager.drain_updates()
            if crypto_updates:
                await _broadcast({"type": "bars", "data": [u.to_dict() for u in crypto_updates]})

            crypto_price_updates = crypto_stream_manager.drain_price_updates()
            if crypto_price_updates:
                await _broadcast(
                    {
                        "type": "price",
                        "data": [
                            {"symbol": sym, "price": price, "ts": ts}
                            for sym, (price, ts) in crypto_price_updates.items()
                        ],
                    }
                )


async def _broadcast_kdj_alert(alert: dict) -> None:
    """KDJMonitor's on_alert callback — pushes the cross to the GUI live,
    alongside the email alert, so it shows up on screen without a reload."""
    await _broadcast({"type": "kdj_alert", **alert})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {
            "app_version": settings.app_version,
            "has_credentials": settings.has_credentials(),
            "paper": settings.paper,
            "data_feed": settings.data_feed,
            "streaming": stream_manager is not None,
            "stream_connected": stream_manager.is_connected() if stream_manager is not None else False,
            "market_open": is_within_market_data_window(),
            "market_data_start_et": settings.market_data_start_et.strftime("%H:%M"),
            "market_data_end_et": settings.market_data_end_et.strftime("%H:%M"),
            "kdj_monitor_symbols": load_monitor_symbols(),
            "kdj_monitor_email_configured": settings.has_smtp_credentials(),
            "crypto_streaming": crypto_stream_manager is not None,
            "crypto_stream_connected": crypto_stream_manager.is_connected() if crypto_stream_manager is not None else False,
            "crypto_watchlist": settings.crypto_watchlist,
            "crypto_kdj_monitor_symbols": settings.crypto_kdj_monitor_symbols,
            "crypto_kdj_email_alerts_enabled": load_crypto_kdj_email_alerts_enabled(),
        }
    )


@app.get("/api/symbols")
async def get_symbols() -> JSONResponse:
    # Dropdown suggestions for the Symbol box come from monitor_list.txt (the
    # symbols already being watched/streamed) — re-read live so editing the
    # file takes effect without restarting the app. The Symbol field stays a
    # free-text box too, so any other ticker can still be typed directly.
    return JSONResponse({"symbols": load_monitor_symbols()})


@app.get("/api/crypto-symbols")
async def get_crypto_symbols() -> JSONResponse:
    # Dropdown suggestions for the Focus Crypto Analysis page's Symbol box —
    # CRYPTO_WATCHLIST from .env. Like the stock Symbol field, this stays a
    # free-text box too, so any other pair (e.g. "SOL/USDT") can be typed
    # directly even if it's not in the configured list.
    return JSONResponse({"symbols": settings.crypto_watchlist})


@app.post("/api/monitor-list")
async def update_monitor_list(request: Request) -> JSONResponse:
    """Saves the Symbol dropdown's "Edit List" popup back to
    monitor_list.txt. Expects {"symbols": ["AAPL", "MSFT", ...]}. This is
    the same file the KDJ monitor watches, so newly added symbols also pick
    up KDJ cross alerts on the monitor's next cycle without a restart."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Request body must be JSON"}, status_code=400)

    symbols = body.get("symbols") if isinstance(body, dict) else None
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        return JSONResponse(
            {"error": 'Expected a JSON body like {"symbols": ["AAPL", "MSFT"]}'},
            status_code=400,
        )

    try:
        saved = save_monitor_symbols(symbols)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save monitor_list.txt")
        return JSONResponse({"error": f"Failed to save: {exc}"}, status_code=500)

    # Start streaming any newly added symbols immediately, so the chart and
    # KDJ monitor don't have to wait for a restart to see live data for them.
    if stream_manager is not None:
        for sym in saved:
            stream_manager.subscribe_symbol(sym)

    return JSONResponse({"symbols": saved})


@app.post("/api/crypto-kdj-email-alerts")
async def update_crypto_kdj_email_alerts(request: Request) -> JSONResponse:
    """The "Email Alerts" toggle next to the KDJ Alerts chip strip on the
    Focus Crypto Analysis page. Expects {"enabled": true|false}. Persisted
    to crypto_kdj_alert_state.json (see alerts/kdj_monitor.py) so it
    survives a restart, and takes effect on the crypto KDJ monitor's next
    check cycle without one — the on-screen alert chips and persisted
    history keep working either way; this only silences the email."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Request body must be JSON"}, status_code=400)

    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"error": 'Expected a JSON body like {"enabled": true}'}, status_code=400
        )

    try:
        saved = save_crypto_kdj_email_alerts_enabled(enabled)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save crypto_kdj_alert_state.json")
        return JSONResponse({"error": f"Failed to save: {exc}"}, status_code=500)

    return JSONResponse({"enabled": saved})


@app.get("/api/timeframes")
async def get_timeframes() -> JSONResponse:
    return JSONResponse({"timeframes": list(TIMEFRAME_MAP.keys())})


# Intraday charts (1Min/5Min/15Min/30Min/1Hour) only show this many of the
# most recent actual trading days' worth of data — older bars are trimmed
# regardless of how many the `limit` query param would otherwise allow.
# "1Day" is exempt: a daily chart limited to 2 candles defeats its purpose,
# so it keeps its normal longer lookback.
MAX_TRADING_DAYS_DISPLAYED = 2


def _limit_to_recent_trading_days(df: pd.DataFrame, n_days: int = MAX_TRADING_DAYS_DISPLAYED) -> pd.DataFrame:
    """Keep only bars from the most recent `n_days` distinct trading days
    present in df, by US Eastern calendar date. Since df only ever contains
    actual session bars (no data on weekends/holidays), the distinct dates
    present are automatically real trading days — no market calendar needed."""
    if df.empty:
        return df
    et_dates = df.index.tz_convert("America/New_York").normalize()
    recent_dates = sorted(et_dates.unique())[-n_days:]
    return df[et_dates.isin(recent_dates)]


def _load_symbol_df(symbol: str, timeframe: str, limit: int):
    """Load OHLCV bars for `symbol` at `timeframe`.

    For 1Min/5Min/15Min/30Min/1Hour: resample locally from the live-streamed
    1-min bars already in the store, so the chart reflects the actual
    selected interval (including a live-updating still-forming current bar)
    instead of always showing raw 1-minute data regardless of the dropdown.
    Trimmed to the last MAX_TRADING_DAYS_DISPLAYED trading days.

    If the resulting data lags behind (e.g. the live stream had a hiccup,
    or you're checking after today's most recent bars never streamed in),
    top up from Alpaca's REST API on the spot so the chart still reaches
    the latest available bar instead of silently going stale — the
    WebSocket stream is best-effort, this is the guarantee.

    For 1Day, or if there isn't enough local 1-min history yet, fall back to
    Alpaca's historical REST API directly.
    """
    if timeframe != "1Day":
        bar_minutes = BAR_MINUTES.get(timeframe, 1)
        # Pull enough 1-min history to comfortably cover the last several
        # calendar days (weekends included) so the trading-day trim below
        # always has full data to work with, independent of `limit`.
        raw_limit = min(max(limit * bar_minutes, 60 * 24 * 7), 50_000)
        raw = store.get_bars(symbol, RAW_TIMEFRAME, limit=raw_limit)
        df = resample_bars(raw, timeframe)
        if not df.empty:
            now_utc = pd.Timestamp.now(tz="UTC")
            # Judge staleness against the underlying 1-min data, not the
            # resampled bar's bin-start label. A resampled bin is *always*
            # labeled at its start, so for e.g. a 30Min/1Hour chart the last
            # bin naturally looks "old" by design even when perfectly live —
            # scaling the tolerance with the interval (the previous approach)
            # meant coarser intervals waited up to 2 hours before even trying
            # to catch up. A small fixed tolerance on the raw feed is the
            # actual freshness signal we care about.
            raw_last_ts = raw.index[-1] if not raw.empty else df.index[-1]
            stale_after = pd.Timedelta(minutes=5)
            if (
                now_utc - raw_last_ts > stale_after
                and is_within_market_data_window(now_utc)
                and settings.has_credentials()
            ):
                logger.info(
                    "%s: local data is stale (last bar %s, %.0f min ago) — "
                    "fetching catch-up from Alpaca REST",
                    symbol, raw_last_ts, (now_utc - raw_last_ts).total_seconds() / 60,
                )
                try:
                    fresh = fetch_bars(symbol, timeframe="1Min", lookback_days=2)
                    if not fresh.empty:
                        store.upsert_bars_df(symbol, RAW_TIMEFRAME, fresh)
                        raw = store.get_bars(symbol, RAW_TIMEFRAME, limit=raw_limit)
                        df = resample_bars(raw, timeframe)
                        new_last_ts = raw.index[-1] if not raw.empty else None
                        if new_last_ts is not None and new_last_ts > raw_last_ts:
                            logger.info(
                                "%s: catch-up advanced last bar from %s to %s",
                                symbol, raw_last_ts, new_last_ts,
                            )
                        else:
                            logger.warning(
                                "%s: catch-up fetch returned %d bars but none newer than "
                                "%s — Alpaca (feed=%s) simply has no more recent trades for "
                                "this symbol yet. Common for lower-volume names on the free "
                                "IEX feed (only ~2.5%% of market volume); SIP would cover this.",
                                symbol, len(fresh), raw_last_ts, settings.data_feed,
                            )
                    else:
                        logger.warning(
                            "%s: catch-up fetch returned no bars at all (feed=%s)",
                            symbol, settings.data_feed,
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("Stale-data catch-up fetch failed for %s", symbol)
            # The trading-day window is now the actual bound on how much data
            # to show — not the bar-count `limit` (2 days of 1-min bars is
            # ~780 rows, more than the old 300-bar default would have kept).
            return _limit_to_recent_trading_days(df)

    if not settings.has_credentials():
        return pd.DataFrame()

    lookback_days = 5 if timeframe != "1Day" else max(60, limit * 2)
    return fetch_bars(symbol, timeframe=timeframe, lookback_days=lookback_days, limit=limit)


def _load_crypto_symbol_df(symbol: str, timeframe: str, limit: int):
    """Same job as _load_symbol_df, for a crypto pair (e.g. 'BTC/USDT')
    instead of a stock ticker. Simpler than the stock version in one way:
    crypto trades 24/7, so there's no market-data-window to check before
    doing a staleness catch-up fetch — if the local data looks stale and
    credentials are configured, always try to top it up."""
    if timeframe != "1Day":
        bar_minutes = BAR_MINUTES.get(timeframe, 1)
        raw_limit = min(max(limit * bar_minutes, 60 * 24 * 7), 50_000)
        raw = store.get_bars(symbol, RAW_TIMEFRAME, limit=raw_limit)
        df = resample_bars(raw, timeframe)
        if not df.empty:
            now_utc = pd.Timestamp.now(tz="UTC")
            raw_last_ts = raw.index[-1] if not raw.empty else df.index[-1]
            stale_after = pd.Timedelta(minutes=5)
            if now_utc - raw_last_ts > stale_after and settings.has_credentials():
                logger.info(
                    "%s: local crypto data is stale (last bar %s, %.0f min ago) — "
                    "fetching catch-up from Alpaca REST",
                    symbol, raw_last_ts, (now_utc - raw_last_ts).total_seconds() / 60,
                )
                try:
                    fresh = fetch_crypto_bars(symbol, timeframe="1Min", lookback_days=2)
                    if not fresh.empty:
                        store.upsert_bars_df(symbol, RAW_TIMEFRAME, fresh)
                        raw = store.get_bars(symbol, RAW_TIMEFRAME, limit=raw_limit)
                        df = resample_bars(raw, timeframe)
                except Exception:  # noqa: BLE001
                    logger.exception("Stale-data crypto catch-up fetch failed for %s", symbol)
            # Reused as-is: it just keeps the most recent N distinct ET
            # calendar dates present in the data, which for continuously-
            # trading crypto simply means "the last ~N*24h", no different
            # in spirit from trimming a stock chart to recent trading days.
            return _limit_to_recent_trading_days(df)

    if not settings.has_credentials():
        return pd.DataFrame()

    lookback_days = 5 if timeframe != "1Day" else max(60, limit * 2)
    return fetch_crypto_bars(symbol, timeframe=timeframe, lookback_days=lookback_days, limit=limit)


# Fixed indicator set/timeframe for the Watchlists table's Trend column —
# deliberately not user-configurable per row (unlike the Focus Stock
# Analysis page's checkboxes), so a watchlist's per-symbol cost stays
# bounded regardless of how many symbols it has. 5Min keeps MACD's 26+9
# lookback and Bollinger/RSI's ~14-20 period comfortably covered within
# WATCHLIST_TREND_BAR_LIMIT bars.
WATCHLIST_TREND_TIMEFRAME = "5Min"
WATCHLIST_TREND_INDICATORS = ["ema", "rsi", "macd"]
WATCHLIST_TREND_BAR_LIMIT = 100


def _symbol_trend(symbol: str) -> dict | None:
    """Composite bullish/bearish/neutral read for one symbol (the always-on
    SMA plus WATCHLIST_TREND_INDICATORS), reusing the same
    compute_signals/compute_composite_signal machinery as the Focus Stock
    Analysis chart. Returns None — never raises — if bars aren't available
    yet or anything about the read fails, so one bad symbol can't break
    the whole watchlist listing."""
    try:
        df = _load_symbol_df(symbol, WATCHLIST_TREND_TIMEFRAME, WATCHLIST_TREND_BAR_LIMIT)
        if df.empty:
            return None
        signals = compute_signals(df, WATCHLIST_TREND_INDICATORS)
        return compute_composite_signal(signals)
    except Exception:
        logger.warning("Trend computation failed for watchlist symbol %s", symbol, exc_info=True)
        return None


def _compute_watchlist_trends(symbols: list[str]) -> dict[str, dict | None]:
    """Sequential per-symbol trend computation, meant to be run inside a
    single asyncio.to_thread() call from the async endpoint below — bundles
    every symbol's (synchronous, local-SQLite-bound) work into one
    background-thread hop instead of one hop per symbol."""
    return {symbol: _symbol_trend(symbol) for symbol in symbols}


@app.get("/api/debug/{symbol}")
async def debug_symbol(symbol: str) -> JSONResponse:
    """Diagnostic: shows exactly how stale local data is for a symbol and
    whether Alpaca's REST API actually has anything newer, so a chart that
    stops earlier than expected can be traced to either (a) the live stream
    being behind, or (b) the feed genuinely having no more recent trades for
    that symbol — those look identical in the chart but need different fixes."""
    symbol = symbol.strip().upper()
    now_utc = pd.Timestamp.now(tz="UTC")
    raw = store.get_bars(symbol, RAW_TIMEFRAME, limit=50_000)
    market_open = is_within_market_data_window(now_utc)
    result = {
        "symbol": symbol,
        "now_utc": now_utc.isoformat(),
        "now_et": now_utc.tz_convert("America/New_York").isoformat(),
        "market_open": market_open,
        "local_last_bar_utc": raw.index[-1].isoformat() if not raw.empty else None,
        "local_last_bar_et": raw.index[-1].tz_convert("America/New_York").isoformat() if not raw.empty else None,
        "local_bar_count": len(raw),
        "subscribed_to_live_stream": stream_manager is not None and symbol in stream_manager.symbols,
        "data_feed": settings.data_feed,
    }
    if not market_open:
        result["rest_api_skipped"] = (
            "The market has closed — outside the market data window "
            f"({settings.market_data_start_et.strftime('%H:%M')}-"
            f"{settings.market_data_end_et.strftime('%H:%M')} ET), so no REST "
            "call was made to Alpaca."
        )
    elif settings.has_credentials():
        try:
            fresh = fetch_bars(symbol, timeframe="1Min", lookback_days=2)
            result["rest_api_last_bar_utc"] = fresh.index[-1].isoformat() if not fresh.empty else None
            result["rest_api_last_bar_et"] = (
                fresh.index[-1].tz_convert("America/New_York").isoformat() if not fresh.empty else None
            )
            result["rest_api_bar_count"] = len(fresh)
        except Exception as exc:  # noqa: BLE001
            result["rest_api_error"] = str(exc)
    return JSONResponse(result)


@app.get("/api/bars")
async def get_bars(symbol: str, timeframe: str = "1Min", limit: int = 500):
    symbol = symbol.strip().upper()
    if not symbol:
        return JSONResponse({"error": "symbol is required"}, status_code=400)
    if stream_manager is not None:
        stream_manager.subscribe_symbol(symbol)

    try:
        df = _load_symbol_df(symbol, timeframe, limit)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    if df.empty:
        return JSONResponse({"bars": []})
    out = df.reset_index().rename(columns={"ts": "timestamp"})
    out["timestamp"] = out["timestamp"].astype(str)
    return JSONResponse({"bars": out.to_dict(orient="records")})


@app.get("/api/chart")
async def get_chart(
    symbol: str, timeframe: str = "1Min", indicators: str = "", limit: int = 300
):
    """Returns a Plotly figure as JSON (fig.to_dict() via to_json) for the frontend
    to render with Plotly.js. `indicators` is a comma-separated list, e.g.
    'sma,ema,boll,rsi,sar,kdj'."""
    symbol = symbol.strip().upper()
    if not symbol:
        return JSONResponse({"error": "symbol is required"}, status_code=400)
    if stream_manager is not None:
        stream_manager.subscribe_symbol(symbol)

    try:
        df = _load_symbol_df(symbol, timeframe, limit)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    if df.empty:
        return JSONResponse({"error": "no data available for this symbol yet — check the ticker is correct"}, status_code=404)

    indicator_list = [i.strip() for i in indicators.split(",") if i.strip()]
    company_name = get_company_name(symbol)

    # Bullish/bearish read per indicator (plus the always-on SMA overlay),
    # each per that indicator's standard textbook rule, evaluated against
    # the latest bar of this df/timeframe. Computed *before* the figure so
    # build_candlestick_figure can bake each reading directly into that
    # indicator's own subplot title/line — previously this rendered in a
    # separate section below the whole chart, which could scroll out of
    # view once enough indicator boxes were stacked (2+ selected). Kept in
    # its own try/except: a bug here should degrade to no trend suffixes,
    # never take down the whole chart response.
    try:
        signals = compute_signals(df, indicator_list)
    except Exception:
        logger.warning(
            "compute_signals failed for %s (indicators=%s) — chart will render without trend suffixes",
            symbol, indicator_list, exc_info=True,
        )
        signals = {}

    # Majority-vote rollup across whatever signals came back above — "do
    # most of the currently-showing indicators agree" at a glance. None if
    # `signals` is empty (compute_signals failed, or nothing has enough
    # bars yet).
    composite = compute_composite_signal(signals)

    try:
        fig = build_candlestick_figure(
            df, symbol, timeframe, indicator_list,
            company_name=company_name, signals=signals, composite=composite,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # company_name is echoed back (not just baked into the figure's title)
    # so the frontend can rebuild the title text itself on each live price
    # tick without re-fetching/re-rendering the whole chart.
    return JSONResponse(
        {
            "figure": fig.to_json(),
            "company_name": company_name,
            "signals": signals,
            "composite": composite,
        }
    )


@app.get("/api/crypto-chart")
async def get_crypto_chart(
    symbol: str, timeframe: str = "30Min", indicators: str = "", limit: int = 300
):
    """Crypto counterpart to /api/chart — same Plotly-figure-as-JSON shape,
    same indicator math and composite signal (both are asset-agnostic, they
    just operate on an OHLCV DataFrame), but sourced from Alpaca's crypto
    market data instead of stocks, with no market-data-window gating (crypto
    trades 24/7) and a Binance quote link instead of CNBC's. `symbol` is a
    BASE/QUOTE pair like 'BTC/USDT'."""
    symbol = symbol.strip().upper()
    if not symbol:
        return JSONResponse({"error": "symbol is required"}, status_code=400)
    if "/" not in symbol:
        return JSONResponse({"error": "symbol must be a pair like BTC/USDT"}, status_code=400)
    if crypto_stream_manager is not None:
        crypto_stream_manager.subscribe_symbol(symbol)

    try:
        df = _load_crypto_symbol_df(symbol, timeframe, limit)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    if df.empty:
        return JSONResponse({"error": "no data available for this pair yet — check the symbol is correct (e.g. BTC/USDT)"}, status_code=404)

    indicator_list = [i.strip() for i in indicators.split(",") if i.strip()]
    display_name = crypto_display_name(symbol)

    try:
        signals = compute_signals(df, indicator_list)
    except Exception:
        logger.warning(
            "compute_signals failed for %s (indicators=%s) — chart will render without trend suffixes",
            symbol, indicator_list, exc_info=True,
        )
        signals = {}

    composite = compute_composite_signal(signals)

    try:
        fig = build_candlestick_figure(
            df, symbol, timeframe, indicator_list,
            company_name=display_name, signals=signals, composite=composite,
            quote_url=binance_quote_url(symbol),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse(
        {
            "figure": fig.to_json(),
            "company_name": display_name,
            "signals": signals,
            "composite": composite,
        }
    )


def _mover_to_dict(row) -> dict:
    return {
        "symbol": row.symbol,
        "company": row.company,
        "price": row.price,
        "change_pct": row.change_pct,
        "market_cap": row.market_cap,
        "cnbc_url": row.cnbc_url,
    }


def _halt_to_dict(row, price: float | None = None, direction: str | None = None) -> dict:
    return {
        "symbol": row.symbol,
        "company": row.company,
        "price": price,
        "halt_date": row.halt_date,
        "halt_time": row.halt_time,
        "market": row.market,
        "reason_code": row.reason_code,
        "reason_label": reason_label(row.reason_code),
        "pause_threshold_price": row.pause_threshold_price,
        # "up"/"down" if this was a price-band (volatility) halt and a prior
        # close was available to compare against, else None — see
        # is_volatility_halt() in screeners/halts.py for which reason codes
        # this applies to (news/regulatory/ETF/market-wide halts aren't
        # caused by this stock's own price move, so direction doesn't apply).
        "direction": direction,
        "resumption_date": row.resumption_date,
        "resumption_time": row.resumption_time,
        "currently_halted": row.currently_halted,
        "cnbc_url": row.cnbc_url,
    }


# The 3 screener endpoints below are independent of Alpaca (TradingView scrape
# + Nasdaq's official RSS feed) and are intentionally NOT gated by
# is_within_market_data_window() — that gate exists specifically to limit
# Alpaca API usage, a different data source with its own availability.
@app.get("/api/screener/52w-high")
async def screener_52w_high() -> JSONResponse:
    try:
        rows = await asyncio.to_thread(fetch_52_week_high)
    except TVScreenerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except Exception as exc:  # noqa: BLE001
        logger.exception("52-week high screener failed")
        return JSONResponse({"error": f"Failed to fetch 52-week high stocks: {exc}"}, status_code=502)
    return JSONResponse({"rows": [_mover_to_dict(r) for r in rows]})


@app.get("/api/screener/52w-low")
async def screener_52w_low() -> JSONResponse:
    try:
        rows = await asyncio.to_thread(fetch_52_week_low)
    except TVScreenerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except Exception as exc:  # noqa: BLE001
        logger.exception("52-week low screener failed")
        return JSONResponse({"error": f"Failed to fetch 52-week low stocks: {exc}"}, status_code=502)
    return JSONResponse({"rows": [_mover_to_dict(r) for r in rows]})


@app.get("/api/screener/halts")
async def screener_halts() -> JSONResponse:
    try:
        rows = await asyncio.to_thread(fetch_current_halts)
    except HaltsScreenerError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Trade halts screener failed")
        return JSONResponse({"error": f"Failed to fetch trade halts: {exc}"}, status_code=502)

    # Current price, unlike the halt data itself, comes from Alpaca (not
    # CNBC — CNBC.com isn't reachable from this app's network) — so this
    # part *is* gated by the market data window, same as every other Alpaca
    # call in the app. Best-effort: if the lookup fails, prices are just
    # omitted rather than failing the whole screener.
    prices: dict[str, float] = {}
    prev_closes: dict[str, float] = {}
    if rows and is_within_market_data_window() and settings.has_credentials():
        symbols = [r.symbol for r in rows]
        try:
            prices = await asyncio.to_thread(fetch_latest_prices, symbols)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to fetch latest prices for halted symbols", exc_info=True)

        # Only worth fetching for symbols where direction is even meaningful
        # (volatility/price-band halts).
        vol_symbols = [r.symbol for r in rows if is_volatility_halt(r.reason_code)]
        if vol_symbols:
            try:
                prev_closes = await asyncio.to_thread(fetch_previous_closes, vol_symbols)
                logger.info(
                    "Halts direction: got a previous close for %d/%d volatility-halt symbols",
                    len(prev_closes), len(vol_symbols),
                )
            except Exception:  # noqa: BLE001
                logger.warning("Failed to fetch previous closes for halted symbols", exc_info=True)
    elif rows:
        logger.info(
            "Halts direction skipped: market_open=%s, has_credentials=%s",
            is_within_market_data_window(), settings.has_credentials(),
        )

    return JSONResponse(
        {
            "rows": [
                _halt_to_dict(r, prices.get(r.symbol), compute_halt_direction(r, prices, prev_closes))
                for r in rows
            ]
        }
    )


@app.get("/api/kdj-alerts")
async def api_kdj_alerts(symbol: str | None = None, limit: int = 100) -> JSONResponse:
    """History of past KDJ cross alerts (see alerts/kdj_monitor.py and
    BarStore.record_kdj_alert/get_kdj_alerts). Independent of Alpaca and
    the market data window — this just reads the local SQLite store.
    `price_1h`/`price_1d`/`outcome_1h`/`outcome_1d` are null until
    _kdj_alert_backfill_loop has had enough elapsed time to fill them in."""
    limit = max(1, min(limit, 500))
    try:
        rows = await asyncio.to_thread(store.get_kdj_alerts, symbol, limit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to load KDJ alert history")
        return JSONResponse({"error": f"Failed to load KDJ alert history: {exc}"}, status_code=500)
    return JSONResponse({"rows": rows})


def _watchlist_body_fields(body: dict) -> tuple[str, str, list[str]] | JSONResponse:
    """Shared validation for the create/update watchlist request bodies.
    Returns (name, note, symbols) on success, or a JSONResponse error to
    return immediately on failure."""
    if not isinstance(body, dict):
        return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "A non-empty 'name' is required"}, status_code=400)
    note = body.get("note") or ""
    if not isinstance(note, str):
        return JSONResponse({"error": "'note' must be a string"}, status_code=400)
    symbols = body.get("symbols") or []
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        return JSONResponse({"error": "'symbols' must be a list of strings"}, status_code=400)
    return name, note, symbols


@app.get("/api/watchlists")
async def api_list_watchlists() -> JSONResponse:
    return JSONResponse({"watchlists": [asdict(w) for w in load_watchlists()]})


@app.post("/api/watchlists")
async def api_create_watchlist(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Request body must be JSON"}, status_code=400)
    parsed = _watchlist_body_fields(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    name, note, symbols = parsed
    wl = create_watchlist(name, note, symbols)
    return JSONResponse({"watchlist": asdict(wl)})


@app.put("/api/watchlists/{watchlist_id}")
async def api_update_watchlist(watchlist_id: str, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Request body must be JSON"}, status_code=400)
    parsed = _watchlist_body_fields(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    name, note, symbols = parsed
    wl = update_watchlist(watchlist_id, name, note, symbols)
    if wl is None:
        return JSONResponse({"error": "Watchlist not found"}, status_code=404)
    return JSONResponse({"watchlist": asdict(wl)})


@app.delete("/api/watchlists/{watchlist_id}")
async def api_delete_watchlist(watchlist_id: str) -> JSONResponse:
    if not delete_watchlist(watchlist_id):
        return JSONResponse({"error": "Watchlist not found"}, status_code=404)
    return JSONResponse({"deleted": True})


@app.get("/api/watchlists/{watchlist_id}/quotes")
async def api_watchlist_quotes(watchlist_id: str) -> JSONResponse:
    """Per-symbol company name + current price + change for one watchlist.
    Company name is static reference data (always looked up, any time of
    day — see data/company_names.py). Price/change come from Alpaca and are
    gated by the market data window like every other Alpaca call in this
    app; outside it, price/change are simply omitted (null)."""
    wl = get_watchlist(watchlist_id)
    if wl is None:
        return JSONResponse({"error": "Watchlist not found"}, status_code=404)

    symbols = wl.symbols
    prices: dict[str, float] = {}
    prev_closes: dict[str, float] = {}
    if symbols and is_within_market_data_window() and settings.has_credentials():
        try:
            prices = await asyncio.to_thread(fetch_latest_prices, symbols)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to fetch latest prices for watchlist %s", watchlist_id, exc_info=True)
        try:
            prev_closes = await asyncio.to_thread(fetch_previous_closes, symbols)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to fetch previous closes for watchlist %s", watchlist_id, exc_info=True)

    # Composite Trend read per symbol (see _symbol_trend above) — best
    # effort, like price/change above: never fails the whole listing, just
    # comes back null for symbols it couldn't compute yet (e.g. brand new
    # to the store, insufficient bars). Also starts streaming any symbol
    # not already subscribed, same as viewing it on the Focus Stock
    # Analysis page would, so the trend/price stay live going forward.
    trends: dict[str, dict | None] = {}
    if symbols:
        if stream_manager is not None:
            for symbol in symbols:
                stream_manager.subscribe_symbol(symbol)
        try:
            trends = await asyncio.to_thread(_compute_watchlist_trends, symbols)
        except Exception:  # noqa: BLE001
            logger.warning("Trend computation failed for watchlist %s", watchlist_id, exc_info=True)

    rows = []
    for symbol in symbols:
        price = prices.get(symbol)
        prev_close = prev_closes.get(symbol)
        change = price - prev_close if price is not None and prev_close is not None else None
        change_pct = (change / prev_close * 100) if change is not None and prev_close else None
        rows.append(
            {
                "symbol": symbol,
                "company": get_company_name(symbol),
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "cnbc_url": cnbc_quote_url(symbol),
                "trend": trends.get(symbol),
            }
        )
    return JSONResponse({"watchlist": asdict(wl), "rows": rows})


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            # Client doesn't need to send anything; keep the connection open.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)
