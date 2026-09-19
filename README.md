# Stock Analysis

Real-time stock and crypto data streaming and technical analysis on top of
Alpaca's Trading/Market Data API, with a web GUI for candlestick charts and
indicators.

## Features

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
- Web GUI (FastAPI + Plotly.js) with live updates over a WebSocket. The chart
  title's price updates continuously from individual trade ticks (not just
  once per minute bar close) — a lightweight in-place title update, not a
  full chart re-render
- **Focus Crypto Analysis** page: the same candlestick charts, indicators,
  and trend signals as the stock page, for crypto pairs (e.g.
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
  - **Stock**: watches the union of every account's own Symbol list (see the
    per-user Symbol list note under **Watchlists** below) rather than one
    shared list, and emails each detected cross only to the account(s)
    actually watching that symbol — not one fixed address. Emails toggle
    (for everyone, still a single on/off switch) with
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
    the email is silenced. Both use the same SMTP credentials; the crypto
    monitor still emails the single fixed `ALERT_EMAIL_TO` address, while
    the stock monitor emails whichever account(s) are actually watching the
    symbol that crossed (see above)
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
    symbols each). **Per account**: each signed-in user has their own
    private set, persisted under `data/user_watchlists/` (one file per
    user, `data/watchlists.py`, directory configurable via
    `USER_WATCHLISTS_DIR`). The very first time an account creates/edits/
    deletes a watchlist (or simply loads the page), their private copy is
    seeded from `watchlists.json` (the shared default template, still
    configurable via `WATCHLISTS_PATH`) — after that, their list is fully
    independent: their own edits never affect anyone else's, and later
    edits to the template don't retroactively change accounts already
    seeded. Shown as a bar of chips with a "+" to create a new one;
    selecting a chip shows that list's symbols with company name, current
    price, change, and a Trend column (price/change/trend via Alpaca,
    gated by the market data window; company name always available).
    Trend is the same composite majority-vote read described above (SMA +
    EMA + RSI + MACD on 5-minute bars), shown as a colored "Bullish/
    Bearish/Neutral (agree/total)" pill — a quick per-symbol scan across
    the whole list without opening each chart. "Edit The Watchlist" and
    "Delete The Watchlist" buttons below the table edit or remove the
    selected list. Distinct from the Focus Stock Analysis page's Symbol
    list (the Symbol dropdown's "Edit List" — the list this app actively
    streams and runs KDJ alerts on, also per-account now, persisted under
    `data/user_monitor_lists/`/`USER_MONITOR_LISTS_DIR` and seeded the same
    way from `monitor_list.txt`): watchlists are just user-organized
    reference lists, not tied to streaming or alerting. Deleting an
    account (admin panel) also deletes that account's private watchlist
    and Symbol list files

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

5. Set up login accounts. The web GUI is gated behind named (email +
   password) accounts — every page, every `/api/*` route, and the live
   WebSocket all require one, since `HOST=auto` (below) means the GUI is
   reachable by anything on your LAN by default. There are two ways to get
   an account:

   - **Create your own directly** (no email round trip):
     ```
     python manage_users.py add <your-email> [--group <label>]
     ```
     You'll be prompted for a password (typed twice, not echoed); the
     account is immediately usable. `--group` is an optional bookkeeping
     label (see below) — omit it if you don't need one.
     `python manage_users.py list/passwd/remove` manage accounts anytime.

   - **Let people self-register** at `/signup` with their email + a
     password + a shared registration code you give out. Set
     `REGISTRATION_CODE` in `.env` to any secret string to turn this on.
     Signing up sends a "click this link to verify your email" message
     using the SMTP credentials configured for KDJ alerts above (step 4) — so
     `SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD` must also be set, or
     `/signup` stays disabled and says so. A new account can't sign in
     until that link is clicked; an expired/lost link can be re-sent from
     the "incorrect email or password" screen's "Resend verification
     email" button. `VERIFICATION_TOKEN_MAX_AGE_HOURS` (default 24)
     controls how long that link stays valid.

     **Different codes for different groups:** instead of (or alongside)
     the single `REGISTRATION_CODE`, set any number of
     `REGISTRATION_CODE_<GROUP>` vars in `.env` — e.g.
     `REGISTRATION_CODE_FAMILY=...` and `REGISTRATION_CODE_FRIENDS=...`.
     Whoever signs up with a given code gets tagged with that group
     (upper-cased) purely as a label for your own bookkeeping — everyone
     ends up with identical access to the app either way. See who signed
     up under which group with `python manage_users.py list`.

     **Public guest signup:** the `GUEST` group is treated specially —
     set `REGISTRATION_CODE_GUEST=0000` (or any value) and the `/signup`
     page shows it directly to visitors ("Enter 0000 as the registration
     code below if you don't have one") instead of expecting them to
     already know a code. Every
     other group's code stays private (never shown in the UI); this is
     meant specifically for a low-friction default you're fine handing to
     anyone, not a real secret.

   **Forgot password:** anyone with an account can click "Forgot
   password?" on the sign-in page, enter their email, and get a "set a
   new password" link — no admin involvement needed. Requires SMTP to be
   configured (same credentials as signup/KDJ alerts); if it isn't, the
   page says so and points at `python manage_users.py passwd <email>`
   instead. The link is single-purpose and expires quickly —
   `RESET_TOKEN_MAX_AGE_HOURS` (default 1) controls how long. Using a
   reset link also verifies the account, same as clicking a signup
   verification link, since it proves the same thing (control of the
   mailbox).

   Until at least one account exists (or signup is turned on), the app
   stays locked — there's no unauthenticated fallback. Also set
   `SESSION_SECRET_KEY` (a fresh one is generated in your `.env` for you
   already — see `web/auth.py`) so logins and any pending verification
   links survive a restart instead of resetting each time;
   `SESSION_MAX_AGE_HOURS` (default 168 = 7 days) controls how long a
   login lasts before signing in again.

   **Managing accounts from the browser:** set `ADMIN_EMAILS` (comma-
   separated) in `.env` to let those specific account(s) reach
   `/admin/users` from the GUI — a "Manage accounts" link appears in the
   header once signed in as one of them. It lists every account (email,
   verified status, group, signup date) with a Delete button per row
   (behind a confirmation page). Everyone else who's logged in gets a 403
   if they try to visit it directly. You can't delete your own account
   from this page (to avoid an accidental lockout) — use
   `python manage_users.py remove <email>` for that. This is purely an
   in-browser alternative to `manage_users.py list/remove`, not a
   replacement — the CLI still works the same as before.

   **Managing your own account:** any signed-in user (not just admins) has
   a "My account" link in the header (`/account`) showing their email,
   registration group, and signup date, plus a self-service "Delete my
   account" control. It requires re-entering the current password to
   confirm — an ordinary session doing this to itself, unlike the admin
   flow above, so a left-open browser can't delete the account with one
   misclick. Deleting removes the account, its watchlists, and its saved
   Symbol list, ends the session immediately, and can't be undone.

## Run

```
python main.py
```

By default `HOST=auto` in `.env`, so the server detects this machine's LAN
IPv4 address at startup and binds to it — the GUI is reachable from other
devices on your network without hardcoding an address. Because uvicorn
binds to that specific LAN IP rather than to all interfaces, **`http://localhost:8000`
will not work with the default settings** — `localhost`/`127.0.0.1` is a
different interface from the LAN IP the server actually bound to, so the
connection is refused.

Open the address uvicorn prints to your terminal at startup instead
(`Uvicorn running on http://<LAN-IP>:8000`) — e.g. `http://192.168.1.23:8000`.
That same address also works from other devices on your network.

On startup the app backfills recent history for each watchlist symbol,
connects the live stream, and begins pushing updates to any open browser
tabs. The first thing you'll see is a login page — sign in with an account
from Setup step 5 (or follow its "Sign up" link if you turned on
self-registration); the header shows who's signed in, with a "Logout" link,
once you're in.

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
  users.py                login accounts (salted PBKDF2 hashes) -> users.json
  watchlists.py            Watchlists page CRUD -> watchlists.json (default
                            template) + data/user_watchlists/ (per account)
  user_paths.py            shared email -> filename-safe slug helper, used
                            by the two per-account data stores above/below
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
web/
  app.py                 FastAPI app: REST + WebSocket + static GUI
  auth.py                 login/session-cookie auth (see Setup step 5)
  static/index.html      browser GUI
main.py                 entry point (uvicorn)
manage_users.py         CLI to add/remove/list login accounts (see Setup step 5)
monitor_list.txt        default-template symbols for the Focus Stock
                         Analysis page's Symbol list / KDJ monitor
data/user_watchlists/   each account's own private Watchlists (gitignored)
data/user_monitor_lists/ each account's own private Symbol list (gitignored)
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
(bar upsert/prune, KDJ alert record/backfill), and the crypto
display-name/Binance-link helpers plus the chart title's quote_url override.
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
