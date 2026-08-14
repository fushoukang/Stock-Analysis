"""CNBC.com quote link helper — shared by every screener so each listed
symbol can link out to its CNBC quote page."""
from __future__ import annotations


def cnbc_quote_url(symbol: str) -> str:
    """e.g. 'AAPL' -> 'https://www.cnbc.com/quotes/AAPL'."""
    return f"https://www.cnbc.com/quotes/{symbol.strip().upper()}"
