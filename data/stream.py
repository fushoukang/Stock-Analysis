"""
Real-time market data streaming via Alpaca's WebSocket API (alpaca-py's
StockDataStream).

alpaca-py's stream client owns and blocks on its own asyncio event loop, so
it is run in a dedicated background thread. Two kinds of updates flow out of
it:
  - Bars (1-minute aggregates): persisted to the local SQLite store and
    queued for broadcast, driving chart redraws.
  - Trades (individual prints): not persisted (they're not OHLCV bars), just
    tracked as a "latest price per symbol" snapshot with dirty-tracking, so
    the GUI can update a symbol's displayed price continuously between bar
    closes/full chart redraws without the cost of re-fetching or re-drawing
    the whole chart for every tick.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from alpaca.data.live import StockDataStream

from config import settings, is_within_market_data_window
from data.store import BarStore

logger = logging.getLogger("data.stream")

RAW_TIMEFRAME = "1Min"

# How often (seconds) the connection thread re-checks the market data window
# while idle (outside the window, waiting for it to open), and how often the
# watcher thread checks whether an active connection needs to be torn down
# because the window just closed.
WINDOW_POLL_SEC = 30


@dataclass
class BarUpdate:
    symbol: str
    timeframe: str
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return asdict(self)


class LiveStreamManager:
    """Owns the Alpaca WebSocket connection and fans out bar + price updates."""

    def __init__(self, store: BarStore, symbols: list[str] | None = None):
        if not settings.has_credentials():
            raise RuntimeError(
                "Alpaca API credentials are not set. Copy .env.example to .env "
                "and fill in ALPACA_API_KEY / ALPACA_SECRET_KEY."
            )
        self.store = store
        self.symbols = set(s.upper() for s in (symbols or settings.watchlist))
        self.updates: "queue.Queue[BarUpdate]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._watcher_thread: threading.Thread | None = None
        self._stream: StockDataStream | None = None
        self._sub_lock = threading.Lock()
        self._stop_event = threading.Event()

        # Latest trade price per symbol, plus which symbols changed since the
        # last drain — a "latest value wins" snapshot rather than a queue,
        # since only the current price matters for a live title display (no
        # need to replay every individual trade for a symbol that ticked 50
        # times in the last second).
        self._latest_price: dict[str, tuple[float, str]] = {}
        self._dirty_price_symbols: set[str] = set()
        self._price_lock = threading.Lock()

    async def _on_trade(self, trade) -> None:
        ts = trade.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        with self._price_lock:
            self._latest_price[trade.symbol] = (trade.price, ts.isoformat())
            self._dirty_price_symbols.add(trade.symbol)

    async def _on_bar(self, bar) -> None:
        ts = bar.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self.store.upsert_bar(
            symbol=bar.symbol,
            timeframe=RAW_TIMEFRAME,
            ts=ts,
            open_=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        self.updates.put(
            BarUpdate(
                symbol=bar.symbol,
                timeframe=RAW_TIMEFRAME,
                ts=ts.isoformat(),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
        )

    def _run(self) -> None:
        # alpaca-py's client already retries plain network hiccups internally,
        # but if it ever exits (a fatal error, an unexpected exception, the
        # library just giving up, or the watcher thread below deliberately
        # disconnecting it because the market data window closed), fall back
        # to reconnecting ourselves with backoff rather than leaving the app
        # permanently without live data — but only re-connect while we're
        # actually inside the market data window; otherwise idle and re-check
        # periodically. This is the only place `StockDataStream` gets
        # constructed, so outside the window this thread makes no Alpaca
        # WebSocket calls at all.
        backoff = 5
        max_backoff = 60
        while not self._stop_event.is_set():
            if not is_within_market_data_window():
                logger.info(
                    "Outside the market data window (%s-%s ET) — live stream idle.",
                    settings.market_data_start_et.strftime("%H:%M"),
                    settings.market_data_end_et.strftime("%H:%M"),
                )
                self._stop_event.wait(WINDOW_POLL_SEC)
                continue

            try:
                self._stream = StockDataStream(
                    settings.api_key, settings.secret_key, feed=settings.data_feed_enum()
                )
                with self._sub_lock:
                    symbols_snapshot = list(self.symbols)
                if symbols_snapshot:
                    self._stream.subscribe_bars(self._on_bar, *symbols_snapshot)
                    self._stream.subscribe_trades(self._on_trade, *symbols_snapshot)
                logger.info("Alpaca live stream connecting (symbols=%s)...", symbols_snapshot)
                backoff = 5  # reset once we get far enough to attempt a connection
                self._stream.run()  # blocks until disconnect/stop/fatal error
            except Exception:
                logger.exception("Alpaca live stream error")
            finally:
                self._stream = None

            if self._stop_event.is_set():
                break
            if is_within_market_data_window():
                logger.warning("Alpaca live stream is down — reconnecting in %ss", backoff)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, max_backoff)
            else:
                # Disconnected because the window just closed (see
                # _watch_window below) — don't burn a backoff cycle on it,
                # the top-of-loop check will idle until the window reopens.
                logger.info("Market data window closed — live stream disconnected.")
                backoff = 5

        logger.info("Alpaca live stream thread exiting.")

    def _watch_window(self) -> None:
        """Runs alongside _run() in its own thread. StockDataStream.run() in
        _run() blocks for the whole session and won't return on its own just
        because the window closed — this thread is what actually tears an
        active connection down the moment that happens, so the app never
        keeps talking to Alpaca past the end of the window."""
        while not self._stop_event.is_set():
            self._stop_event.wait(WINDOW_POLL_SEC)
            if self._stop_event.is_set():
                break
            if self._stream is not None and not is_within_market_data_window():
                logger.info("Market data window closed — disconnecting Alpaca live stream.")
                try:
                    self._stream.stop()
                except Exception:
                    logger.exception("Error stopping live stream at window close")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="alpaca-stream")
        self._thread.start()
        self._watcher_thread = threading.Thread(
            target=self._watch_window, daemon=True, name="alpaca-stream-window-watcher"
        )
        self._watcher_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass

    def is_connected(self) -> bool:
        """True if there's currently a live Alpaca WebSocket connection
        (i.e. we're inside the market data window and successfully
        connected) — used by /api/status to report stream state."""
        return self._stream is not None

    def subscribe_symbol(self, symbol: str) -> None:
        """Add a symbol to the live stream at runtime (e.g. a user typed a
        ticker that isn't in the configured watchlist). Safe to call from any
        thread/event loop; a no-op if already subscribed or the stream isn't
        up yet (it'll just serve historical data via the REST endpoints)."""
        symbol = symbol.upper().strip()
        if not symbol:
            return
        with self._sub_lock:
            if symbol in self.symbols:
                return
            self.symbols.add(symbol)
        if self._stream is not None:
            try:
                self._stream.subscribe_bars(self._on_bar, symbol)
                self._stream.subscribe_trades(self._on_trade, symbol)
            except Exception:
                logger.exception("Failed to subscribe live stream to %s", symbol)

    def drain_price_updates(self) -> dict[str, tuple[float, str]]:
        """Non-blocking: returns {symbol: (price, iso_ts)} for every symbol
        whose price changed since the last call, then clears the dirty set."""
        with self._price_lock:
            out = {sym: self._latest_price[sym] for sym in self._dirty_price_symbols if sym in self._latest_price}
            self._dirty_price_symbols.clear()
        return out

    def drain_updates(self, max_items: int = 100) -> list[BarUpdate]:
        """Non-blocking: pull up to max_items pending updates for broadcast."""
        items: list[BarUpdate] = []
        for _ in range(max_items):
            try:
                items.append(self.updates.get_nowait())
            except queue.Empty:
                break
        return items
