"""
Real-time crypto market data streaming via Alpaca's WebSocket API
(alpaca-py's CryptoDataStream).

Mirrors data/stream.py's LiveStreamManager for stocks — same BarUpdate-ish
shape, same store, same drain_updates()/drain_price_updates() polling
interface, so web/app.py's broadcast loop and the frontend's WebSocket
handling don't need to care whether a symbol is a stock ticker or a crypto
pair. The one structural difference: crypto trades 24/7, so unlike the
stock stream there's no market-data-window to idle around — this manager
just tries to stay connected all the time, reconnecting with backoff on any
drop.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from alpaca.data.live.crypto import CryptoDataStream

from config import settings
from data.store import BarStore

logger = logging.getLogger("data.crypto_stream")

RAW_TIMEFRAME = "1Min"


@dataclass
class CryptoBarUpdate:
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


class CryptoLiveStreamManager:
    """Owns the Alpaca crypto WebSocket connection and fans out bar + price
    updates. Crypto pair symbols (e.g. 'BTC/USDT') always contain a '/',
    which stock tickers never do — so bar/price updates from this manager
    can safely share the same local SQLite `bars` table and the same
    WebSocket broadcast message stream as stocks with zero risk of a
    symbol collision."""

    def __init__(self, store: BarStore, symbols: list[str] | None = None):
        if not settings.has_credentials():
            raise RuntimeError(
                "Alpaca API credentials are not set. Copy .env.example to .env "
                "and fill in ALPACA_API_KEY / ALPACA_SECRET_KEY."
            )
        self.store = store
        self.symbols = set(s.strip().upper() for s in (symbols or settings.crypto_watchlist))
        self.updates: "queue.Queue[CryptoBarUpdate]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stream: CryptoDataStream | None = None
        self._sub_lock = threading.Lock()
        self._stop_event = threading.Event()

        # Latest trade price per symbol, plus which symbols changed since
        # the last drain — same "latest value wins" snapshot pattern as the
        # stock stream manager.
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
            CryptoBarUpdate(
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
        # No market-data-window check here (unlike the stock stream) —
        # crypto trades 24/7, so this loop's only job is to stay connected
        # and reconnect with backoff on any drop.
        backoff = 5
        max_backoff = 60
        while not self._stop_event.is_set():
            try:
                self._stream = CryptoDataStream(settings.api_key, settings.secret_key)
                with self._sub_lock:
                    symbols_snapshot = list(self.symbols)
                if symbols_snapshot:
                    self._stream.subscribe_bars(self._on_bar, *symbols_snapshot)
                    self._stream.subscribe_trades(self._on_trade, *symbols_snapshot)
                logger.info("Alpaca crypto live stream connecting (symbols=%s)...", symbols_snapshot)
                backoff = 5  # reset once we get far enough to attempt a connection
                self._stream.run()  # blocks until disconnect/stop/fatal error
            except Exception:
                logger.exception("Alpaca crypto live stream error")
            finally:
                self._stream = None

            if self._stop_event.is_set():
                break
            logger.warning("Alpaca crypto live stream is down — reconnecting in %ss", backoff)
            self._stop_event.wait(backoff)
            backoff = min(backoff * 2, max_backoff)

        logger.info("Alpaca crypto live stream thread exiting.")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="alpaca-crypto-stream")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass

    def is_connected(self) -> bool:
        return self._stream is not None

    def subscribe_symbol(self, symbol: str) -> None:
        """Add a crypto pair to the live stream at runtime (e.g. the user
        typed a pair that isn't in CRYPTO_WATCHLIST). Safe to call from any
        thread; a no-op if already subscribed or the stream isn't up yet
        (it'll just serve historical data via the REST endpoint)."""
        symbol = symbol.strip().upper()
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
                logger.exception("Failed to subscribe crypto live stream to %s", symbol)

    def drain_price_updates(self) -> dict[str, tuple[float, str]]:
        """Non-blocking: returns {symbol: (price, iso_ts)} for every symbol
        whose price changed since the last call, then clears the dirty set."""
        with self._price_lock:
            out = {sym: self._latest_price[sym] for sym in self._dirty_price_symbols if sym in self._latest_price}
            self._dirty_price_symbols.clear()
        return out

    def drain_updates(self, max_items: int = 100) -> list[CryptoBarUpdate]:
        """Non-blocking: pull up to max_items pending updates for broadcast."""
        items: list[CryptoBarUpdate] = []
        for _ in range(max_items):
            try:
                items.append(self.updates.get_nowait())
            except queue.Empty:
                break
        return items
