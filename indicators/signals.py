"""Per-indicator bullish/bearish signal, derived from the most commonly
cited textbook interpretation of each indicator, evaluated against the
latest bar only. This is a simple, single-snapshot read of "what does this
indicator's standard rule say right now" — not a backtest, not a composite
score, and not investment advice.

Used to render the "Trend" summary at the bottom of the Focus Stock
Analysis page: one line per currently selected indicator, plus the
always-on SMA overlay.
"""
from __future__ import annotations

import logging

import pandas as pd

from indicators.moving_average import sma_nm, ema as _ema
from indicators.bollinger import bollinger_bands
from indicators.rsi import rsi as _rsi
from indicators.sar import parabolic_sar
from indicators.kdj import kdj as _kdj
from indicators.vwap import vwap as _vwap
from indicators.macd import macd as _macd
from indicators.mtm import mtm as _mtm

logger = logging.getLogger("indicators.signals")

BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL = "neutral"

# Mirrors the SMA(12,2) always overlaid on the candlestick chart itself
# (see charts/candlestick.py) — not a plain arithmetic average.
SMA_N = 12
SMA_M = 2

DEFAULT_PARAMS = {
    "ema": {"span": 20},
    "boll": {"window": 20, "num_std": 2.0},
    "rsi": {"period": 14},
    "sar": {"af_step": 0.02, "af_max": 0.2},
    "kdj": {"n": 9, "k_period": 3, "d_period": 3},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "mtm": {"n": 12, "m": 6},
}


def _signal(label: str, reason: str) -> dict:
    return {"label": label, "reason": reason}


def compute_signals(
    df: pd.DataFrame, indicators: list[str], params: dict | None = None
) -> dict[str, dict]:
    """
    df: OHLCV DataFrame indexed by timestamp (same one passed to
        build_candlestick_figure — the currently selected timeframe's bars).
    indicators: the same list the chart was built with (may include "sma",
        which is ignored here since it's scored unconditionally below).

    Returns {indicator_name: {"label": "bullish"|"bearish"|"neutral",
    "reason": str}}. An indicator is omitted if there isn't enough data yet
    for its rule to produce a value (e.g. too few bars for its lookback).
    """
    params = params or {}
    close = df["close"]
    last_close = close.iloc[-1]
    out: dict[str, dict] = {}

    # SMA is always shown on the chart, so its trend reading is always
    # included too, regardless of what's in `indicators`. Wrapped so a
    # failure here (unexpected data shape, etc.) can't take out every other
    # indicator's signal below it — worst case SMA's own reading is
    # dropped, the rest of the panel still renders.
    try:
        sma_p = {"n": SMA_N, "m": SMA_M, **params.get("sma", {})}
        sma_series = sma_nm(close, **sma_p)
        last_sma = sma_series.iloc[-1] if len(sma_series) else float("nan")
        if pd.notna(last_sma):
            if last_close > last_sma:
                out["sma"] = _signal(
                    BULLISH, f"Price (${last_close:.2f}) is above SMA(12,2) (${last_sma:.2f})"
                )
            elif last_close < last_sma:
                out["sma"] = _signal(
                    BEARISH, f"Price (${last_close:.2f}) is below SMA(12,2) (${last_sma:.2f})"
                )
            else:
                out["sma"] = _signal(NEUTRAL, "Price is exactly at SMA(12,2)")
    except Exception:
        logger.warning("compute_signals: SMA signal failed", exc_info=True)

    selected = [i.lower() for i in dict.fromkeys(indicators or []) if i.lower() != "sma"]

    # Each indicator is scored independently and defensively: an exception
    # scoring one indicator (unexpected data shape, an edge case in a
    # particular param combo, etc.) only drops that indicator's chip from
    # the Trend line — it must never wipe out signals already computed for
    # the others, and must never bubble up and 500 the whole /api/chart
    # response (this is exactly the failure mode that made the entire
    # Trend panel vanish once 3+ indicators were selected: one indicator's
    # exception was aborting the whole loop before `out` was returned).
    for name in selected:
        try:
            p = {**DEFAULT_PARAMS.get(name, {}), **params.get(name, {})}

            if name == "ema":
                series = _ema(close, **p)
                last = series.iloc[-1] if len(series) else float("nan")
                if pd.isna(last):
                    continue
                span = p["span"]
                if last_close > last:
                    out["ema"] = _signal(BULLISH, f"Price (${last_close:.2f}) is above EMA({span}) (${last:.2f})")
                elif last_close < last:
                    out["ema"] = _signal(BEARISH, f"Price (${last_close:.2f}) is below EMA({span}) (${last:.2f})")
                else:
                    out["ema"] = _signal(NEUTRAL, f"Price is exactly at EMA({span})")

            elif name == "boll":
                bands = bollinger_bands(close, **p)
                upper = bands["upper"].iloc[-1] if len(bands) else float("nan")
                mid = bands["mid"].iloc[-1] if len(bands) else float("nan")
                lower = bands["lower"].iloc[-1] if len(bands) else float("nan")
                if pd.isna(mid):
                    continue
                # Standard reading: a close outside the bands signals an
                # overbought/oversold extreme (reversal risk in the opposite
                # direction); inside the bands, position relative to the
                # middle band (the SMA) gives the trend bias.
                if pd.notna(upper) and last_close > upper:
                    out["boll"] = _signal(
                        BEARISH,
                        f"Price (${last_close:.2f}) is above the upper Bollinger Band (${upper:.2f}) — overbought, pullback risk",
                    )
                elif pd.notna(lower) and last_close < lower:
                    out["boll"] = _signal(
                        BULLISH,
                        f"Price (${last_close:.2f}) is below the lower Bollinger Band (${lower:.2f}) — oversold, bounce potential",
                    )
                elif last_close > mid:
                    out["boll"] = _signal(BULLISH, f"Price is above the middle band (${mid:.2f})")
                elif last_close < mid:
                    out["boll"] = _signal(BEARISH, f"Price is below the middle band (${mid:.2f})")
                else:
                    out["boll"] = _signal(NEUTRAL, "Price is exactly at the middle band")

            elif name == "macd":
                macd_df = _macd(close, **p)
                m = macd_df["macd"].iloc[-1] if len(macd_df) else float("nan")
                s = macd_df["signal"].iloc[-1] if len(macd_df) else float("nan")
                if pd.isna(m) or pd.isna(s):
                    continue
                zero_note = " (above the zero line)" if m > 0 else " (below the zero line)"
                if m > s:
                    out["macd"] = _signal(BULLISH, f"MACD ({m:.2f}) is above Signal ({s:.2f}){zero_note}")
                elif m < s:
                    out["macd"] = _signal(BEARISH, f"MACD ({m:.2f}) is below Signal ({s:.2f}){zero_note}")
                else:
                    out["macd"] = _signal(NEUTRAL, "MACD equals Signal")

            elif name == "mtm":
                mtm_df = _mtm(close, **p)
                mtm_val = mtm_df["mtm"].iloc[-1] if len(mtm_df) else float("nan")
                ma_mtm = mtm_df["maMtm"].iloc[-1] if len(mtm_df) else float("nan")
                if pd.isna(mtm_val) or pd.isna(ma_mtm):
                    continue
                zero_note = " (above the zero line)" if mtm_val > 0 else " (below the zero line)"
                if mtm_val > ma_mtm:
                    out["mtm"] = _signal(BULLISH, f"MTM ({mtm_val:.2f}) is above MAMTM ({ma_mtm:.2f}){zero_note}")
                elif mtm_val < ma_mtm:
                    out["mtm"] = _signal(BEARISH, f"MTM ({mtm_val:.2f}) is below MAMTM ({ma_mtm:.2f}){zero_note}")
                else:
                    out["mtm"] = _signal(NEUTRAL, "MTM equals MAMTM")

            elif name == "rsi":
                series = _rsi(close, **p)
                last = series.iloc[-1] if len(series) else float("nan")
                if pd.isna(last):
                    continue
                if last >= 70:
                    out["rsi"] = _signal(BEARISH, f"RSI is {last:.1f} — overbought (>=70), pullback risk")
                elif last <= 30:
                    out["rsi"] = _signal(BULLISH, f"RSI is {last:.1f} — oversold (<=30), bounce potential")
                elif last > 50:
                    out["rsi"] = _signal(BULLISH, f"RSI is {last:.1f} — above the 50 midline")
                elif last < 50:
                    out["rsi"] = _signal(BEARISH, f"RSI is {last:.1f} — below the 50 midline")
                else:
                    out["rsi"] = _signal(NEUTRAL, "RSI is exactly 50")

            elif name == "sar":
                series = parabolic_sar(df, **p)
                last = series.iloc[-1] if len(series) else float("nan")
                if pd.isna(last):
                    continue
                if last < last_close:
                    out["sar"] = _signal(BULLISH, f"SAR dot (${last:.2f}) is below price — uptrend")
                elif last > last_close:
                    out["sar"] = _signal(BEARISH, f"SAR dot (${last:.2f}) is above price — downtrend")
                else:
                    out["sar"] = _signal(NEUTRAL, "SAR is exactly at price")

            elif name == "kdj":
                kdj_df = _kdj(df, **p)
                k = kdj_df["k"].iloc[-1] if len(kdj_df) else float("nan")
                d = kdj_df["d"].iloc[-1] if len(kdj_df) else float("nan")
                if pd.isna(k) or pd.isna(d):
                    continue
                if k > d:
                    extra = " (overbought territory, >80)" if k > 80 else ""
                    out["kdj"] = _signal(BULLISH, f"K ({k:.1f}) is above D ({d:.1f}){extra}")
                elif k < d:
                    extra = " (oversold territory, <20)" if k < 20 else ""
                    out["kdj"] = _signal(BEARISH, f"K ({k:.1f}) is below D ({d:.1f}){extra}")
                else:
                    out["kdj"] = _signal(NEUTRAL, "K equals D")

            elif name == "vwap":
                series = _vwap(df)
                last = series.iloc[-1] if len(series) else float("nan")
                if pd.isna(last):
                    continue
                if last_close > last:
                    out["vwap"] = _signal(BULLISH, f"Price (${last_close:.2f}) is above VWAP (${last:.2f})")
                elif last_close < last:
                    out["vwap"] = _signal(BEARISH, f"Price (${last_close:.2f}) is below VWAP (${last:.2f})")
                else:
                    out["vwap"] = _signal(NEUTRAL, "Price is exactly at VWAP")
        except Exception:
            logger.warning("compute_signals: '%s' signal failed, skipping it", name, exc_info=True)
            continue

    return out


def compute_composite_signal(signals: dict[str, dict]) -> dict | None:
    """Rolls up whichever per-indicator signals are present (SMA plus
    whatever's currently selected — exactly `compute_signals()`'s output)
    into a single majority-vote read: "bullish" if more indicators say
    bullish than bearish, "bearish" if the reverse, "neutral" on a tie
    (neutral-labeled indicators don't count toward either side, but do
    count toward the total). Returns None if `signals` is empty — there's
    nothing to vote on yet (e.g. not enough bars for any indicator).

    This is deliberately a simple unweighted vote, not a confidence score
    or backtested weighting — SMA and RSI count the same as each other,
    for instance. It's meant as an at-a-glance "do most of what's
    currently showing agree" read, not a trading signal in its own right.
    """
    if not signals:
        return None
    bullish_count = sum(1 for s in signals.values() if s.get("label") == BULLISH)
    bearish_count = sum(1 for s in signals.values() if s.get("label") == BEARISH)
    neutral_count = sum(1 for s in signals.values() if s.get("label") == NEUTRAL)
    total = bullish_count + bearish_count + neutral_count

    if bullish_count > bearish_count:
        label = BULLISH
    elif bearish_count > bullish_count:
        label = BEARISH
    else:
        label = NEUTRAL

    return {
        "label": label,
        "reason": f"{bullish_count} bullish, {bearish_count} bearish, {neutral_count} neutral of {total} indicator(s)",
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "neutral_count": neutral_count,
        "total": total,
    }
