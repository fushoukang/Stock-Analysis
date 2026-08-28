"""Tests for data/crypto_info.py's pure display helpers."""
from __future__ import annotations

from data.crypto_info import crypto_base_asset, crypto_display_name, binance_quote_url


def test_crypto_base_asset_strips_quote_currency():
    assert crypto_base_asset("BTC/USDT") == "BTC"
    assert crypto_base_asset("eth/usd") == "ETH"


def test_crypto_display_name_known_asset():
    assert crypto_display_name("BTC/USDT") == "Bitcoin"
    assert crypto_display_name("eth/usd") == "Ethereum"


def test_crypto_display_name_unknown_asset_returns_none():
    assert crypto_display_name("ZZZFAKE/USDT") is None


def test_binance_quote_url_format():
    assert binance_quote_url("BTC/USDT") == "https://www.binance.com/en/trade/BTC_USDT"
    assert binance_quote_url("eth/usd") == "https://www.binance.com/en/trade/ETH_USD"
