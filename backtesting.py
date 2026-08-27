"""
Lightweight backtesting engine: simulates a simple long/flat strategy driven
by indicators/signals.py's composite majority-vote signal, evaluated
bar-by-bar over historical OHLCV data.

This answers one narrow question: "if I'd gone long every time the
composite signal turned bullish, and flat every time it turned bearish,
using only these indicators, how would that have done over this window?"
It is NOT a proper walk-forward backtest — there's no slippage, fees,
position sizing, shorting, or portfolio-level risk management — and it is
NOT investment advice. It's a sanity-check tool for eyeballing whether the
composite signal has historically leaned in a useful direction for a given
symbol/timeframe, nothing more.

At each bar, the composite signal is recomputed from only a bounded
trailing window of bars (BACKTEST_LOOKBACK_BARS), the same size class as
the Watchlist Trend column's WATCHLIST_TREND_BAR_LIMIT — this keeps each
step's cost roughly constant instead of recomputing over the entire history
every time (an expanding window would be O(n^2) and needlessly slow for no
accuracy benefit, since every indicator here is already a trailing/causal
calculation that only looks backward from the current bar anyway).
"""
from __future__ import annotations

from indicators.signals import (
    BULLISH,
    BEARISH,
    compute_signals,
    compute_composite_signal,
)

# Indicators the strategy watches, mirroring the Watchlist Trend column's
# fixed set (see web/app.py's WATCHLIST_TREND_INDICATORS) — SMA is always
# included on top of these by compute_signals() itself.
DEFAULT_BACKTEST_INDICATORS = ["ema", "rsi", "macd"]

# Bars fed to compute_signals() at each simulated step. Comfortably covers
# MACD's 26+9 lookback and RSI/EMA's shorter windows with room to spare.
BACKTEST_LOOKBACK_BARS = 120

# Minimum bars of history required before the first signal is trusted (must
# be at least enough for the slowest default indicator, MACD, to produce a
# non-NaN reading).
BACKTEST_MIN_BARS = 40

# Hard cap on how many bars a single backtest simulates, to keep a manual
# "Run Backtest" click responsive. Trims to the most recent bars if the
# loaded history is longer.
BACKTEST_MAX_BARS = 500


class BacktestError(Exception):
    pass


def run_backtest(
    df,
    indicators: list[str] | None = None,
    initial_capital: float = 10_000.0,
) -> dict:
    """
    df: OHLCV DataFrame indexed by timestamp, ascending order (same shape
        as what build_candlestick_figure/compute_signals expect).
    indicators: which indicators (besides the always-on SMA) the composite
        signal should vote across. Defaults to DEFAULT_BACKTEST_INDICATORS.

    Returns a dict with final_equity, total_return_pct, num_trades,
    win_rate_pct, a trade log, and an equity curve — see the return
    statement below for the exact shape.
    """
    indicators = indicators or DEFAULT_BACKTEST_INDICATORS
    if df is None or df.empty:
        raise BacktestError("No bar data available for this symbol/timeframe.")
    if len(df) < BACKTEST_MIN_BARS:
        raise BacktestError(
            f"Not enough bars for a backtest — need at least {BACKTEST_MIN_BARS}, "
            f"got {len(df)}. Try a longer time range or a smaller interval."
        )

    if len(df) > BACKTEST_MAX_BARS:
        df = df.iloc[-BACKTEST_MAX_BARS:]

    position: dict | None = None  # {"entry_ts", "entry_price"} while long
    cash = float(initial_capital)
    shares = 0.0
    trades: list[dict] = []
    equity_curve: list[dict] = []

    for i in range(BACKTEST_MIN_BARS, len(df)):
        window_start = max(0, i - BACKTEST_LOOKBACK_BARS + 1)
        window = df.iloc[window_start : i + 1]
        ts = df.index[i]
        price = float(df["close"].iloc[i])

        label = None
        try:
            signals = compute_signals(window, indicators)
            composite = compute_composite_signal(signals)
            if composite is not None:
                label = composite["label"]
        except Exception:
            # A signal failure mid-backtest just means "no decision this
            # bar" — same fault-isolation philosophy as compute_signals()
            # itself; it must never abort the whole simulation.
            label = None

        if position is None and label == BULLISH:
            shares = cash / price
            cash = 0.0
            position = {"entry_ts": ts, "entry_price": price}
        elif position is not None and label == BEARISH:
            cash = shares * price
            trades.append(_close_trade(position, ts, price, forced=False))
            shares = 0.0
            position = None

        equity_curve.append({"ts": _iso(ts), "equity": round(cash + shares * price, 2)})

    # Force-close any still-open position at the last bar so total_return_pct
    # reflects a fully realized outcome, not a hanging mark-to-market
    # position. Flagged "forced" in the trade log so it's clear this exit
    # wasn't a real bearish signal.
    if position is not None:
        last_ts = df.index[-1]
        last_price = float(df["close"].iloc[-1])
        cash = shares * last_price
        trades.append(_close_trade(position, last_ts, last_price, forced=True))
        shares = 0.0
        position = None

    final_equity = round(cash, 2)
    total_return_pct = round((final_equity - initial_capital) / initial_capital * 100, 2)
    wins = [t for t in trades if t["return_pct"] > 0]
    win_rate_pct = round(len(wins) / len(trades) * 100, 2) if trades else None

    return {
        "initial_capital": round(initial_capital, 2),
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "num_trades": len(trades),
        "win_rate_pct": win_rate_pct,
        "indicators": indicators,
        "bars_used": len(df),
        "start_time": _iso(df.index[BACKTEST_MIN_BARS]),
        "end_time": _iso(df.index[-1]),
        "trades": trades,
        "equity_curve": equity_curve,
    }


def _close_trade(position: dict, exit_ts, exit_price: float, forced: bool) -> dict:
    entry_price = position["entry_price"]
    return_pct = round((exit_price - entry_price) / entry_price * 100, 2)
    return {
        "entry_time": _iso(position["entry_ts"]),
        "exit_time": _iso(exit_ts),
        "entry_price": round(entry_price, 2),
        "exit_price": round(exit_price, 2),
        "return_pct": return_pct,
        "forced": forced,
    }


def _iso(ts) -> str:
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)
