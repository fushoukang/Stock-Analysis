# Stock Trading Analysis

Real-time stock and crypto data streaming and technical analysis on top of
Alpaca's Trading/Market Data API, with a web GUI for candlestick charts and
indicators.

This app is **read-only market data analysis** — it streams and stores bars
and computes indicators, but never places, modifies, or cancels orders.

## Features

- Version number: `pyproject.toml`'s `version` field is the single source
  of truth — shown next to the title in the GUI header (`vX.Y.Z`, via
  `/api/status`'s `app_version`, read by `config.py`'s
  `_read_pyproject_version()`). Bump the app's version by editing that one
  line; nothing else needs to change
- Real-time bar + trade streaming via Alpaca's WebSocket (`alpaca.data.live.StockDataStream`)
- Historical backfill via Alpaca's REST market data API
- Local SQLite storage of OHLCV bars, with a background job (every 6 hours)
  pruning bars older than `BAR_RETENTION_DAYS` (default 30) so the database
  doesn't grow unbounded
- Indicators: MA, SMA, EMA, Bollinger Bands (BOLL), MACD (12/26/9 EMA
  convergence-divergence, with its own histogram, plotted just ahead of
  RSI in the indicator checkboxes and chart), MTM (Momentum, MTM(12,6) —
  MTM = close minus the close 12 bars ago, MAMTM = a 6-period moving
  average of that momentum line; plotted just after MACD), RSI, Parabolic
  SAR, SuperTrend (ATR-based trend-following overlay, `SUPERTREND(10, 3.0)`
  — a single line that flips sides of price on each trend change, in the
  same family as Parabolic SAR but driven by ATR volatility instead of an
  acceleration factor; plotted just after SAR, colored green while below
  price/uptrend and red while above/downtrend), KDJ, VWAP
  (Volume-Weighted Average Price — cumulative, resets each session,
  computed directly from the bars at whichever timeframe is selected)
- Trend read (`indicators/signals.py`): a bullish/bearish/neutral score for
  the always-on SMA overlay plus every currently selected indicator, each
  scored against that indicator's standard textbook rule (e.g. price vs.
  EMA, RSI vs. 50/70/30, K vs. D, price vs. VWAP, MACD vs. Signal,
  MTM vs. MAMTM, SuperTrend's own trend flag) using
  only the latest bar. Shown as a colored "— Bullish/Bearish/Neutral"
  suffix baked directly into that indicator's own subplot title (SMA's
  reading rides on the price chart's title, since that's where the SMA
  line is drawn) — kept inside the chart itself rather than a separate
  section below it, so it can't end up scrolled out of view as more
  indicator boxes stack up. A composite majority-vote rollup across
  whichever indicators are currently selected (e.g. "Composite: Bullish
  (5/8)") also rides on the price title, right after SMA's own reading —
  an unweighted vote, not a confidence score. Each is a single-snapshot
  read of a standard rule, not a backtest, and not investment advice
- Candlestick charts (Plotly) with indicator overlays and oscillator subplots,
  for multiple time intervals (1Min, 5Min, 15Min, 1Hour, 1Day)
- Backtesting (`backtesting.py`, "Run Backtest" button on the Focus Stock
  Analysis page, `/api/backtest`): simulates a simple long/flat strategy —
  go long when the composite signal (SMA + EMA/RSI/MACD by default) turns
  bullish, go flat when it turns bearish — over the symbol's recent history
  at whatever interval is currently selected. Reports total return, final
  equity, win rate, and a full trade log in a dismissible panel below the
  chart. No fees, slippage, position sizing, or shorting — a lightweight
  sanity check on how the composite signal has historically leaned for a
  symbol, not a proper walk-forward backtest, and not investment advice
- Web GUI (FastAPI + Plotly.js) with live updates over a WebSocket. The chart
  title's price updates continuously from individual trade ticks (not just
  once per minute bar close) — a lightweight in-place title update, not a
  full chart re-render
- **Focus Crypto Analysis** page: the same candlestick charts, indicators,
  trend signals, and backtesting as the stock page, for crypto pairs (e.g.
  BTC/USDT, ETH/USDT) via Alpaca's separate crypto exchange/data path
  (`data/crypto_historical.py`, `data/crypto_stream.py`,
  `alpaca.data.live.crypto.CryptoDataStream`) — same account credentials,
  no separate signup. Default pairs come from `CRYPTO_WATCHLIST` in `.env`;
  any other pair can be typed directly (BASE/QUOTE format). Unlike stocks,
  crypto trades 24/7, so there's no market-hours gating — the backfill and
  live stream just run continuously. One deliberate scope difference from
  the stock page: the chart reloads in full on each new bar (roughly once
  per interval) rather than also smoothing the title price between
  individual trade ticks. The chart title links out to the pair's Binance
  trade page (`data/crypto_info.py`) instead of a CNBC quote page, and shows
  a small static friendly name (e.g. "Bitcoin") for common base assets
- KDJ cross monitor: two independent instances of the same `KDJMonitor`
  class (`alerts/kdj_monitor.py`) — one for stocks, one for crypto — each
  recompute a rolling 15-minute KDJ every 2 minutes (`KDJ_CHECK_INTERVAL_SEC`,
  shared) from the live 1-min stream and email an alert if K and D crossed
  within the last 5 minutes (`KDJ_FRESHNESS_WINDOW_MIN`, shared) — which is
  the same moment K, D, and J are all equal, since J = 3K − 2D. On-screen
  alerts show up as chips next to the indicator checkboxes on whichever page
  matches the symbol (crypto pairs always contain "/", stock tickers never
  do, so the shared WebSocket alert message routes to the right page
  automatically)
  - **Stock**: watches the symbols in `monitor_list.txt`. Emails toggle with
    `KDJ_EMAIL_ALERTS_ENABLED` in `.env` (default `true`)
  - **Crypto**: watches `CRYPTO_KDJ_MONITOR_SYMBOLS` in `.env` (default
    `BTC/USDT`), runs 24/7 like the rest of the crypto data path (no
    market-hours gating). Emails toggle independently from the stock
    monitor's, via an "Email Alerts" switch right on the Focus Crypto
    Analysis page (next to the KDJ Alerts chip strip) — flips live, no
    restart, and persists to `crypto_kdj_alert_state.json`
    (`POST /api/crypto-kdj-email-alerts`, `alerts/kdj_monitor.py`'s
    `load_crypto_kdj_email_alerts_enabled`/`save_crypto_kdj_email_alerts_enabled`).
    `CRYPTO_KDJ_EMAIL_ALERTS_ENABLED` in `.env` (default `true`) only sets
    the starting value the first time that file doesn't exist yet
  - Either way, the monitor keeps running and detecting crosses even with its
    email switch off, and the on-screen WebSocket alert keeps firing — only
    the email is silenced. Both instances email the same `ALERT_EMAIL_TO`
    address via the same SMTP credentials
- KDJ alert history: every cross the monitor detects is persisted to the
  local SQLite store (`kdj_alerts` table, `BarStore.record_kdj_alert`), not
  just emailed/pushed live. A background loop (`_kdj_alert_backfill_loop` in
  `web/app.py`, every 15 minutes) later fills in the price 1 hour and 1 day
  after each alert, and classifies the move as up/down/flat relative to the
  price at alert time — a simple outcome read, not a backtest. Browsable via
  the "KDJ Alert History" category in the header dropdown (`/api/kdj-alerts`,
  optional `?symbol=`/`?limit=` query params) — "1h Later"/"1d Later" show
  "pending…" until enough time has passed for that bar to exist
- Market data window: Alpaca (WebSocket stream, REST catch-up/backfill, and
  company-name lookups) is only ever contacted between `MARKET_DATA_START_ET`
  and `MARKET_DATA_END_ET` (default 6:30 AM - 6:00 PM ET, Mon-Fri — early +
  regular + post market hours). Outside that window the app makes zero
  Alpaca API calls and the GUI shows a "The market has closed" banner
- Category dropdown (top-right of the header): "Focus Stock Analysis" and
  "Focus Crypto Analysis" are the two chart views described above; the rest
  are market screeners/tools, independent of Alpaca's stock data path and
  not gated by the market data window:
  - **52 Week High / 52 Week Low Stocks** — scraped from TradingView's market
    movers pages (`screeners/tradingview.py`), filtered to market cap > $1B
    (high) / > $100M (low), each symbol linking to its CNBC.com quote page.
    Note: TradingView's default view returns a capped, alphabetically-sorted
    subset (~100 rows), not the full universe of that day's movers
  - **Current Market Halt Stocks** — today's LULD (Limit Up-Limit Down)
    volatility halts only (up to 10, most recent first) from Nasdaq's
    official Trade Halt RSS feed (`screeners/halts.py`); news, regulatory,
    ETF, and market-wide-circuit-breaker halts are filtered out. Cached to
    respect Nasdaq's 1-query-per-minute guidance. Shows each symbol's
    current price and up/down direction (comparing the halt's pause
    threshold price to the prior close) via Alpaca
  - **Watchlists** — user-defined named lists of symbols (name + note +
    symbols each), persisted to `watchlists.json` (`data/watchlists.py`,
    configurable via `WATCHLISTS_PATH`). Shown as a bar of chips with a "+"
    to create a new one; selecting a chip shows that list's symbols with
    company name, current price, change, and a Trend column (price/change/
    trend via Alpaca, gated by the market data window; company name always
    available). Trend is the same composite majority-vote read described
    above (SMA + EMA + RSI + MACD on 5-minute bars), shown as a colored
    "Bullish/Bearish/Neutral (agree/total)" pill — a quick per-symbol scan
    across the whole list without opening each chart. "Edit The Watchlist"
    and "Delete The Watchlist" buttons below the table edit or remove the
    selected list. Distinct from `monitor_list.txt` (the Symbol dropdown's
    "Edit List" — the list this app actively streams and runs KDJ alerts
    on): watchlists are just user-organized reference lists, not tied to
    streaming or alerting

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your Alpaca API keys:
   ```
   cp .env.example .env
   ```
   Get keys at https://app.alpaca.markets/. `ALPACA_PAPER=false` targets a
   live account's keys; the app itself never trades regardless of this
   setting — it only affects which key pair is expected.

3. Adjust `WATCHLIST` in `.env` to the symbols you want to track, and
   `CRYPTO_WATCHLIST` to the crypto pairs (BASE/QUOTE format, e.g.
   `BTC/USDT,ETH/USDT`) for the Focus Crypto Analysis page — same Alpaca
   account credentials, no separate signup needed.

   `MARKET_DATA_START_ET`/`MARKET_DATA_END_ET` control the window during
   which the app talks to Alpaca at all (default 06:30-18:00 ET). Adjust
   only if you specifically want a narrower/wider window than early+regular+
   post market hours.

4. (Optional) To enable KDJ cross email alerts: put the stock symbols to
   watch in `monitor_list.txt` (whitespace/comma-separated) and/or the
   crypto pairs to watch in `CRYPTO_KDJ_MONITOR_SYMBOLS` in `.env` (default
   `BTC/USDT`), then fill in `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`.
   For Gmail, use an App Password (https://myaccount.google.com/apppasswords)
   — not your normal password. `ALERT_EMAIL_TO` controls where both monitors'
   alerts go. Without SMTP credentials set, both monitors still run and log
   detected crosses, they just can't email them. Set
   `KDJ_EMAIL_ALERTS_ENABLED=false` / `CRYPTO_KDJ_EMAIL_ALERTS_ENABLED=false`
   to turn either monitor's emails off independently, without touching SMTP
   credentials or stopping either monitor.

## Run

```
python main.py
```

By default `HOST=auto` in `.env`, so the server detects this machine's LAN
IPv4 address at startup and binds to it — the GUI is reachable from other
devices on your network without hardcoding an address. The actual address is
printed to your terminal by uvicorn when it starts (`Uvicorn running on
http://...`); set `HOST` to a specific value (e.g. `127.0.0.1` for
local-only access) to override auto-detection.

Then open http://localhost:8000 in a browser. On startup the app backfills
recent history for each watchlist symbol, connects the live stream, and
begins pushing updates to any open browser tabs.

## Project layout

```
config.py              settings loaded from .env
data/
  store.py             SQLite OHLCV storage
  historical.py         Alpaca historical bar fetch (stocks)
  stream.py             Alpaca live WebSocket stream (stocks) -> store + broadcast queue
  crypto_historical.py  Alpaca historical bar fetch (crypto)
  crypto_stream.py       Alpaca live WebSocket stream (crypto) -> store + broadcast queue
  crypto_info.py          crypto display-name lookup + Binance quote link
indicators/
  moving_average.py     SMA, EMA, MA
  bollinger.py           Bollinger Bands
  macd.py                 MACD
  mtm.py                   MTM (Momentum)
  rsi.py                 RSI
  sar.py                 Parabolic SAR
  supertrend.py           SuperTrend (ATR-based trend-following overlay)
  kdj.py                 KDJ
  vwap.py                 VWAP
  compute.py             aggregator used by the chart builder / API
  signals.py              per-indicator bullish/bearish trend read
charts/
  candlestick.py         Plotly candlestick figure builder
alerts/
  email_alert.py          SMTP email sending
  kdj_monitor.py           resample -> KDJ -> cross detection -> alert, on a loop
                            (run as two instances: stock + crypto, see config.py)
backtesting.py          long/flat strategy simulation driven by the composite signal
web/
  app.py                 FastAPI app: REST + WebSocket + static GUI
  static/index.html      browser GUI
main.py                 entry point (uvicorn)
monitor_list.txt        symbols the KDJ monitor watches
```

## Tests

```
pip install -r requirements.txt
pytest
```

Covers the pure-logic modules: indicator math (`indicators/`), the
composite signal's per-rule correctness and fault-isolation guarantee
(`indicators/signals.py` — one indicator's exception must never wipe out
signals already computed for the others), KDJ cross detection and
monitor-list read/write, the halts screener's direction logic, watchlists
CRUD round-trips, the rule-based market holiday calendar, `BarStore`
(bar upsert/prune, KDJ alert record/backfill), the backtesting engine, and
the crypto display-name/Binance-link helpers plus the chart title's
quote_url override.
Doesn't cover the FastAPI endpoints themselves end-to-end or the frontend
JS — those are verified manually (`TestClient` + `node --check` during
development) rather than as part of this pytest suite.

## Notes / next steps

- The live stream persists raw 1-minute bars (Alpaca's native streaming
  aggregation). Larger intervals (5Min/1Hour/1Day) are currently served via
  historical REST calls rather than resampled locally — resampling the
  stored 1Min bars would let those update live too.
- No authentication/authorization on the web GUI — it's meant for local use.
  Add auth before exposing it beyond localhost.
- IEX (free) data feed is the default; switch `ALPACA_DATA_FEED=sip` in
  `.env` if you have a SIP subscription for full-market data.
- The crypto KDJ monitor's watch list (`CRYPTO_KDJ_MONITOR_SYMBOLS`) is a
  plain comma-separated `.env` value, unlike the stock monitor's editable
  `monitor_list.txt` (which also has an "Edit List" popup in the GUI). A
  natural follow-up would be a `crypto_monitor_list.txt` file plus an
  equivalent GUI editor, for parity with the stock side.
- The market data window (`MARKET_DATA_START_ET`/`MARKET_DATA_END_ET`) now
  also checks a rule-based NYSE/Nasdaq holiday calendar (`market_holidays.py`
  — New Year's, MLK Day, Presidents Day, Good Friday, Memorial Day,
  Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas, with the
  standard weekend-observed shift), so the app makes zero Alpaca calls on
  market holidays, not just weekends. It doesn't model early-close
  half-days (e.g. the day after Thanksgiving) — those are still treated as
  a normal full trading day.
