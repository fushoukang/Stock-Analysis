"""Small display helpers for crypto pairs in the GUI: a static base-asset
display-name lookup (no extra Alpaca API call needed — crypto asset names
are effectively static reference data) and an external quote-link builder
(Binance's public spot trading page), mirroring screeners/cnbc.py's role
for stocks and data/company_names.py's role for stock display names.
"""
from __future__ import annotations

# Common crypto base asset -> friendly display name. Deliberately a small,
# static map rather than an API call — an unlisted symbol just falls back
# to showing its own raw base ticker (see crypto_display_name below), the
# same "None is fine, just omit it" convention data/company_names.py uses
# for stocks whose name lookup fails.
_DISPLAY_NAMES: dict[str, str] = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "DOGE": "Dogecoin",
    "LTC": "Litecoin",
    "BCH": "Bitcoin Cash",
    "AVAX": "Avalanche",
    "LINK": "Chainlink",
    "UNI": "Uniswap",
    "AAVE": "Aave",
    "SHIB": "Shiba Inu",
    "DOT": "Polkadot",
    "XRP": "XRP",
    "XTZ": "Tezos",
    "SUSHI": "SushiSwap",
    "YFI": "Yearn.Finance",
    "MKR": "Maker",
    "CRV": "Curve DAO Token",
    "GRT": "The Graph",
    "BAT": "Basic Attention Token",
    "USDT": "Tether",
    "USDC": "USD Coin",
    "PEPE": "Pepe",
}


def crypto_base_asset(symbol: str) -> str:
    """'BTC/USDT' -> 'BTC'."""
    return symbol.strip().upper().split("/")[0]


def crypto_display_name(symbol: str) -> str | None:
    """A friendly name for the pair's base asset, e.g. 'BTC/USDT' ->
    'Bitcoin', or None if it's not in the static map above — callers
    should treat None as "just show the raw symbol"."""
    return _DISPLAY_NAMES.get(crypto_base_asset(symbol))


def binance_quote_url(symbol: str) -> str:
    """'BTC/USDT' -> 'https://www.binance.com/en/trade/BTC_USDT' — a
    public, informational price page (not a trading action), used as the
    chart title's outbound link for crypto pairs the same way
    screeners/cnbc.py's cnbc_quote_url() is used for stocks."""
    pair = symbol.strip().upper().replace("/", "_")
    return f"https://www.binance.com/en/trade/{pair}"
