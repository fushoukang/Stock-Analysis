# Stock Trading Analysis

Real-time stock data streaming and technical analysis on top of Alpaca's
Trading/Market Data API, with a web GUI for candlestick charts and indicators.

This app is **read-only market data analysis** — it streams and stores bars
and computes indicators, but never places, modifies, or cancels orders.

## Features

- Real-time bar + trade streaming via Alpaca's WebSocket (`alpaca.data.live.StockDataStream`)
- Historical backfill via Alpaca's REST market data API
- Local SQLite storage of OHLCV bars
- Indicators: MA, SMA, EMA, Bollinger Bands (BOLL), RSI, Parabolic SAR, KDJ
- Candlestick charts (Plotly) with indicator overlays and oscillator subplots,
  for multiple time intervals (1Min, 5Min, 15Min, 1Hour, 1Day)
- Web GUI (FastAPI + Plotly.js) with live updates over a WebSocket. The chart
  title's price updates continuously from individual trade ticks (not just
  once per minute bar close) — a lightweight in-place title update, not a
  full chart re-render
- KDJ cross monitor: watches the symbols in `monitor_list.txt`, recomputes a
  rolling 15-minute KDJ every 2 minutes (`KDJ_CHECK_INTERVAL_SEC`) from the
  live 1-min stream, and emails an alert if K and D crossed within the last
  5 minutes (`KDJ_FRESHNESS_WINDOW_MIN`, both configurable in `.env`) — which
  is the same moment K, D, and J are all equal, since J = 3K − 2D
- Market data window: Alpaca (WebSocket stream, REST catch-up/backfill, and
  company-name lookups) is only ever contacted between `MARKET_DATA_START_ET`
  and `MARKET_DATA_END_ET` (default 6:30 AM - 6:00 PM ET, Mon-Fri — early +
  regular + post market hours). Outside that window the app makes zero
  Alpaca API calls and the GUI shows a "The market has closed" banner
- Category dropdown (top-right of the header): "Focus Stock Analysis" is the
  main chart view above; the other three are market screeners, independent of
  Alpaca and not gated by the market data window:
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
    company name, current price, and change (price/change via Alpaca,
    gated by the market data window; company name always available). "Edit
    The Watchlist" and "Delete The Watchlist" buttons below the table edit
    or remove the selected list. Distinct from `monitor_list.txt` (the
    Symbol dropdown's "Edit List" — the list this app actively streams and
    runs KDJ alerts on): watchlists are just user-organized reference lists,
    not tied to streaming or alerting

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

3. Adjust `WATCHLIST` in `.env` to the symbols you want to track.

   `MARKET_DATA_START_ET`/`MARKET_DATA_END_ET` control the window during
   which the app talks to Alpaca at all (default 06:30-18:00 ET). Adjust
   only if you specifically want a narrower/wider window than early+regular+
   post market hours.

4. (Optional) To enable KDJ cross email alerts: put the symbols to watch in
   `monitor_list.txt` (whitespace/comma-separated), and fill in `SMTP_HOST`,
   `SMTP_USERNAME`, `SMTP_PASSWORD` in `.env`. For Gmail, use an App Password
   (https://myaccount.google.com/apppasswords) — not your normal password.
   `ALERT_EMAIL_TO` controls where the alert goes. Without SMTP credentials
   set, the monitor still runs and logs detected crosses, it just can't
   email them.

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
  historical.py         Alpaca historical bar fetch
  stream.py             Alpaca live WebSocket stream -> store + broadcast queue
indicators/
  moving_average.py     SMA, EMA, MA
  bollinger.py           Bollinger Bands
  rsi.py                 RSI
  sar.py                 Parabolic SAR
  kdj.py                 KDJ
  compute.py             aggregator used by the chart builder / API
charts/
  candlestick.py         Plotly candlestick figure builder
alerts/
  email_alert.py          SMTP email sending
  kdj_monitor.py           resample -> KDJ -> cross detection -> alert, on a loop
web/
  app.py                 FastAPI app: REST + WebSocket + static GUI
  static/index.html      browser GUI
main.py                 entry point (uvicorn)
monitor_list.txt        symbols the KDJ monitor watches
```

## Notes / next steps

- The live stream persists raw 1-minute bars (Alpaca's native streaming
  aggregation). Larger intervals (5Min/1Hour/1Day) are currently served via
  historical REST calls rather than resampled locally — resampling the
  stored 1Min bars would let those update live too.
- No authentication/authorization on the web GUI — it's meant for local use.
  Add auth before exposing it beyond localhost.
- IEX (free) data feed is the default; switch `ALPACA_DATA_FEED=sip` in
  `.env` if you have a SIP subscription for full-market data.
- The market data window (`MARKET_DATA_START_ET`/`MARKET_DATA_END_ET`)
  doesn't know about market holidays — on a holiday the app will still try
  to connect during the configured window; Alpaca just won't have new data.
  Same accepted trade-off used elsewhere in this app's market-hours logic.
