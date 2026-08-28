"""Tests for charts/candlestick.py's quote_url override — the mechanism
that lets crypto pairs link to Binance in the chart title instead of the
default CNBC stock-quote link."""
from __future__ import annotations

from charts.candlestick import build_candlestick_figure

from conftest import make_ohlcv_df


def test_default_quote_url_uses_cnbc_for_stocks():
    df = make_ohlcv_df(n=60)
    fig = build_candlestick_figure(df, "AAPL", "15Min", [], company_name="Apple Inc")
    title = fig.layout.annotations[0].text
    assert "cnbc.com/quotes/AAPL" in title
    assert "Apple Inc" in title


def test_quote_url_override_used_when_given():
    df = make_ohlcv_df(n=60)
    fig = build_candlestick_figure(
        df, "BTC/USDT", "15Min", [], company_name="Bitcoin",
        quote_url="https://www.binance.com/en/trade/BTC_USDT",
    )
    title = fig.layout.annotations[0].text
    assert "binance.com/en/trade/BTC_USDT" in title
    assert "cnbc.com" not in title
    assert "Bitcoin" in title


def test_falls_back_to_linking_the_symbol_when_no_company_name():
    df = make_ohlcv_df(n=60)
    fig = build_candlestick_figure(
        df, "SOL/USDT", "15Min", [], company_name=None,
        quote_url="https://www.binance.com/en/trade/SOL_USDT",
    )
    title = fig.layout.annotations[0].text
    assert 'href="https://www.binance.com/en/trade/SOL_USDT"' in title
    assert "SOL/USDT" in title
