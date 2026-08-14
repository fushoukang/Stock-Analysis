"""Parses TradingView's market cap display format, e.g. '3.36 B USD',
'746.04 M USD', '58.74 B USD' -> a plain USD float. Returns None for
missing/unparseable values ('—', empty, unexpected units) — callers should
treat None as "unknown" and exclude it from any market-cap threshold filter,
since we can't confirm it meets the bar either way.
"""
from __future__ import annotations

import re

_UNIT_MULTIPLIERS = {
    "K": 1_000,
    "M": 1_000_000,
    "B": 1_000_000_000,
    "T": 1_000_000_000_000,
}

# e.g. "3.36 B USD", "746.04 M USD", "235.4M USD", "58.74 B"
_PATTERN = re.compile(
    r"^\s*([\d,]+(?:\.\d+)?)\s*([KMBT])?\s*(?:USD)?\s*$", re.IGNORECASE
)


def parse_market_cap(text: str | None) -> float | None:
    if not text:
        return None
    text = text.strip()
    if not text or text in ("—", "-", "–", "N/A", "n/a"):
        return None
    m = _PATTERN.match(text)
    if not m:
        return None
    number_str, unit = m.group(1), m.group(2)
    try:
        number = float(number_str.replace(",", ""))
    except ValueError:
        return None
    if unit:
        number *= _UNIT_MULTIPLIERS[unit.upper()]
    return number


def format_market_cap(value: float | None) -> str:
    """Inverse-ish of parse_market_cap, for display: 3_360_000_000 -> '3.36B'."""
    if value is None:
        return "—"
    for suffix, mult in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= mult:
            return f"{value / mult:.2f}{suffix}"
    return f"{value:.0f}"
