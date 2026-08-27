"""Tests for screeners/halts.py: reason-code classification and the
extracted, independently-testable compute_halt_direction() function."""
from __future__ import annotations

from screeners.halts import (
    HaltRow,
    compute_halt_direction,
    is_volatility_halt,
    reason_label,
)


def _row(symbol="AAPL", reason_code="T5", pause_threshold_price=None) -> HaltRow:
    return HaltRow(
        symbol=symbol,
        company="Apple Inc",
        halt_date="08/27/2026",
        halt_time="10:00:00",
        market="NASDAQ",
        reason_code=reason_code,
        pause_threshold_price=pause_threshold_price,
        resumption_date="",
        resumption_time="",
        currently_halted=True,
        cnbc_url="https://cnbc.com/quotes/AAPL",
    )


def test_is_volatility_halt_true_for_known_codes():
    assert is_volatility_halt("T5")
    assert is_volatility_halt("LUDP")
    assert is_volatility_halt("LUDS")
    assert is_volatility_halt("M")


def test_is_volatility_halt_false_for_news_and_regulatory_codes():
    assert not is_volatility_halt("T1")
    assert not is_volatility_halt("H10")
    assert not is_volatility_halt("MWC1")


def test_reason_label_known_code():
    assert reason_label("T5") == "Volatility Pause (10%+ move in 5 min)"


def test_reason_label_unknown_code_falls_back_to_code_itself():
    assert reason_label("ZZZ99") == "ZZZ99"


def test_reason_label_blank_code_is_unknown():
    assert reason_label("") == "Unknown"


def test_direction_none_for_non_volatility_halt():
    row = _row(reason_code="T1", pause_threshold_price=150.0)
    assert compute_halt_direction(row, {}, {"AAPL": 140.0}) is None


def test_direction_up_using_pause_threshold_price():
    row = _row(pause_threshold_price=155.0)
    result = compute_halt_direction(row, {}, {"AAPL": 150.0})
    assert result == "up"


def test_direction_down_using_pause_threshold_price():
    row = _row(pause_threshold_price=140.0)
    result = compute_halt_direction(row, {}, {"AAPL": 150.0})
    assert result == "down"


def test_direction_falls_back_to_current_price_when_threshold_missing():
    row = _row(pause_threshold_price=None)
    result = compute_halt_direction(row, {"AAPL": 160.0}, {"AAPL": 150.0})
    assert result == "up"


def test_direction_none_when_no_reference_price_available():
    row = _row(pause_threshold_price=None)
    assert compute_halt_direction(row, {}, {"AAPL": 150.0}) is None


def test_direction_none_when_no_prev_close_available():
    row = _row(pause_threshold_price=155.0)
    assert compute_halt_direction(row, {}, {}) is None


def test_direction_none_when_reference_equals_prev_close():
    row = _row(pause_threshold_price=150.0)
    assert compute_halt_direction(row, {}, {"AAPL": 150.0}) is None


def test_direction_prefers_threshold_price_over_current_price():
    row = _row(pause_threshold_price=160.0)
    # current price says "down" but pause threshold (the actual halt-moment
    # price) says "up" — threshold should win.
    result = compute_halt_direction(row, {"AAPL": 100.0}, {"AAPL": 150.0})
    assert result == "up"
