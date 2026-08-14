"""Candlestick chart builder (Plotly): a price/volume section plus one
separate subplot ("box") per selected indicator.

The x-axis uses a categorical (not continuous datetime) axis. Our data only
ever contains actual trading-session bars — Alpaca doesn't return bars for
closed-market hours — so a category axis naturally has no gaps to hide and
each bar gets a fixed, evenly-spaced slot. (An earlier version used a
continuous date axis with `rangebreaks` to hide closed-market hours, but
Plotly's automatic bar-width calculation doesn't handle datetime axes with
rangebreaks well as more data accumulates — bar widths drift and can balloon
over a session. Category axis avoids that class of bug entirely.)
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from indicators.compute import OVERLAY_INDICATORS, compute_indicators
from indicators.moving_average import sma_nm

# The always-on SMA overlay uses the Chinese-TA SMA(N, M) formula (see
# indicators/moving_average.sma_nm), not a plain arithmetic average.
SMA_N = 12
SMA_M = 2

INDICATOR_LABELS = {
    "sma": "SMA",
    "ema": "EMA",
    "boll": "Bollinger Bands",
    "rsi": "RSI",
    "sar": "Parabolic SAR",
    "kdj": "KDJ",
}


def build_candlestick_figure(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    indicators: list[str] | None = None,
    params: dict | None = None,
    company_name: str | None = None,
) -> go.Figure:
    """
    df: OHLCV DataFrame indexed by timestamp.
    indicators: list of indicator names, each rendered in its own subplot
        below the price/volume chart, e.g. ["boll","rsi","kdj"].
        Price-scale indicators (ema/boll/sar) also get a thin close-price
        line in their box for reference. SMA is always drawn overlaid
        directly on the candlestick chart itself (not a separate box),
        regardless of what's in `indicators`.
    company_name: optional display name for the symbol (e.g. "Apple Inc."),
        shown in the price chart's title alongside the latest price. Omitted
        from the title entirely if not provided (e.g. lookup unavailable).
    """
    # SMA is always shown merged into the price chart, so it never takes its
    # own box — drop it here if present so it doesn't double up below.
    indicators = [i.lower() for i in dict.fromkeys(indicators or []) if i.lower() != "sma"]

    # Display in US market local time.
    df = df.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("America/New_York")

    # Categorical x labels — one evenly-spaced slot per actual bar, so there's
    # no visual gap for nights/weekends and no time-based bar-width drift.
    label_fmt = "%Y-%m-%d" if timeframe == "1Day" else "%Y-%m-%d %H:%M"
    x_labels = df.index.strftime(label_fmt)

    results = compute_indicators(df, indicators, params) if indicators else {}
    sma_params = {"n": SMA_N, "m": SMA_M, **(params or {}).get("sma", {})}
    sma_series = sma_nm(df["close"], **sma_params)

    # Row 1: candlestick. Row 2: volume. Row 3+: one box per selected indicator.
    n_indicator_rows = len(indicators)
    n_rows = 2 + n_indicator_rows

    price_h = 0.5
    volume_h = 0.15
    remaining = max(1.0 - price_h - volume_h, 0.1)
    # All indicator boxes got a general size bump; BOLL keeps extra room on
    # top of that (OHLC bars + 3 band lines need more space than a
    # single/triple-line indicator box like EMA/RSI/KDJ).
    INDICATOR_HEIGHT_WEIGHT = 1.4
    BOLL_HEIGHT_WEIGHT = 2.2
    indicator_weights = [
        BOLL_HEIGHT_WEIGHT if ind == "boll" else INDICATOR_HEIGHT_WEIGHT for ind in indicators
    ]
    total_weight = sum(indicator_weights) or 1
    row_heights = [price_h, volume_h] + [
        remaining * w / total_weight for w in indicator_weights
    ]

    last_price = df["close"].iloc[-1] if len(df) else None
    price_suffix = f" — ${last_price:,.2f}" if last_price is not None else ""
    name_suffix = f" — {company_name}" if company_name else ""
    # Plotly subplot titles are annotations and support basic HTML-like tags,
    # so <b> makes every box title (price, volume, each indicator) bold.
    titles = [f"<b>{symbol}{name_suffix}{price_suffix}</b>", "<b>Volume</b>"] + [
        f"<b>{INDICATOR_LABELS.get(i, i.upper())}</b>" for i in indicators
    ]

    # Keep spacing comfortable but within Plotly's limit (must be < 1/(n_rows-1)).
    vertical_spacing = min(0.08, 0.9 / max(n_rows - 1, 1))

    fig = make_subplots(
        rows=n_rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=vertical_spacing,
        row_heights=row_heights,
        subplot_titles=titles,
    )

    fig.add_trace(
        go.Candlestick(
            x=x_labels,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name=symbol,
        ),
        row=1,
        col=1,
    )

    # SMA is always merged directly onto the candlestick chart.
    fig.add_trace(
        go.Scatter(
            x=x_labels, y=sma_series, name=f"SMA({sma_params['n']},{sma_params['m']})",
            line=dict(width=2, color="#f59e0b"),
        ),
        row=1,
        col=1,
    )

    colors = ["green" if c >= o else "red" for o, c in zip(df["open"], df["close"])]
    fig.add_trace(
        go.Bar(x=x_labels, y=df["volume"], name="Volume", marker_color=colors),
        row=2,
        col=1,
    )

    row = 3
    for ind in indicators:
        if ind in OVERLAY_INDICATORS and ind != "boll":
            # Price-scale indicator: show a thin close-price line for context.
            fig.add_trace(
                go.Scatter(
                    x=x_labels, y=df["close"], name="Close", line=dict(width=1, color="#9ca3af"),
                    showlegend=False,
                ),
                row=row,
                col=1,
            )

        if ind == "ema":
            fig.add_trace(
                go.Scatter(x=x_labels, y=results["ema"], name="EMA", line=dict(width=3)),
                row=row,
                col=1,
            )
        elif ind == "boll":
            # Full OHLC bars for price context, instead of just a close line —
            # lets the bands be compared against the actual candle range.
            fig.add_trace(
                go.Ohlc(
                    x=x_labels,
                    open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                    name="OHLC", showlegend=False,
                    increasing_line_color="green", decreasing_line_color="red",
                ),
                row=row,
                col=1,
            )
            fig.update_xaxes(rangeslider_visible=False, row=row, col=1)
            boll = results["boll"]
            boll_widths = {"upper": 2.5, "mid": 4, "lower": 2.5}
            for col_name, label in [("upper", "BOLL Upper"), ("mid", "BOLL Mid"), ("lower", "BOLL Lower")]:
                fig.add_trace(
                    go.Scatter(
                        x=x_labels, y=boll[col_name], name=label,
                        line=dict(width=boll_widths[col_name]),
                    ),
                    row=row,
                    col=1,
                )
        elif ind == "sar":
            sar_series = results["sar"]
            # SAR dot below price = uptrend (green), above price = downtrend (red).
            is_up = sar_series < df["close"]
            fig.add_trace(
                go.Scatter(
                    x=x_labels, y=sar_series.where(is_up), name="SAR (up)", mode="markers",
                    marker=dict(size=7, symbol="circle", color="green"),
                ),
                row=row,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=x_labels, y=sar_series.where(~is_up), name="SAR (down)", mode="markers",
                    marker=dict(size=7, symbol="circle", color="red"),
                ),
                row=row,
                col=1,
            )
        elif ind == "rsi":
            fig.add_trace(
                go.Scatter(x=x_labels, y=results["rsi"], name="RSI", line=dict(width=3)),
                row=row,
                col=1,
            )
            fig.add_hline(y=70, line=dict(dash="dash", width=1), row=row, col=1)
            fig.add_hline(y=30, line=dict(dash="dash", width=1), row=row, col=1)
        elif ind == "kdj":
            kdj_df = results["kdj"]
            for col_name in ["k", "d", "j"]:
                fig.add_trace(
                    go.Scatter(
                        x=x_labels, y=kdj_df[col_name], name=col_name.upper(),
                        line=dict(width=3),
                    ),
                    row=row,
                    col=1,
                )
        row += 1

    # Extra absolute pixels for the taller BOLL box, on top of its larger
    # share of the relative row-height split above. The base per-indicator-row
    # allocation (220px, up from 150px) applies to every indicator, KDJ
    # included, so all indicator boxes render bigger.
    boll_extra_px = 130 if "boll" in indicators else 0
    fig.update_layout(
        xaxis_rangeslider_visible=False,
        height=350 + 220 * (1 + n_indicator_rows) + boll_extra_px,
        margin=dict(l=20, r=100, t=40, b=20),
        legend=dict(orientation="h"),
        template="plotly_white",
    )

    # Categorical axis: fixed number of evenly-spaced ticks so labels don't
    # clutter as more bars accumulate.
    fig.update_xaxes(type="category", nticks=12)

    # Only the bottom-most subplot needs x-axis tick labels — showing them on
    # every row crowds the gap between each box and the one below it.
    for r in range(1, n_rows):
        fig.update_xaxes(showticklabels=False, row=r, col=1)

    return fig
