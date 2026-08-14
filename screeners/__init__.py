"""
Market screeners: 52-week high/low stocks (scraped from TradingView's market
movers pages) and current trading halts (from Nasdaq's official RSS feed).

These are independent of Alpaca — they're not gated by the market data
window (config.is_within_market_data_window) since they're a different data
source with their own availability, not subject to the same API-call
restriction the user asked for around Alpaca specifically.

Scraping HTML is inherently fragile: if TradingView changes its page
layout, screeners/tradingview.py's parser can break. It's written
defensively (looks for the data table by its header text rather than
hard-coded CSS classes) and raises a clear error rather than silently
returning wrong data if it can't find what it expects.
"""
