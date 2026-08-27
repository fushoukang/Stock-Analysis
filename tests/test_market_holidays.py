"""Verifies market_holidays.py's rule-based NYSE/Nasdaq calendar against
known real dates (including a Saturday-observed-Friday shift), rather than
just re-deriving the same rules the module already implements."""
from __future__ import annotations

from datetime import date

from market_holidays import is_market_holiday, nyse_holidays


def test_2026_known_holidays():
    # Independently looked-up real NYSE closure dates for 2026.
    assert is_market_holiday(date(2026, 1, 1))    # New Year's Day (Thursday)
    assert is_market_holiday(date(2026, 1, 19))   # MLK Day (3rd Monday of Jan)
    assert is_market_holiday(date(2026, 2, 16))   # Presidents Day (3rd Monday of Feb)
    assert is_market_holiday(date(2026, 4, 3))    # Good Friday
    assert is_market_holiday(date(2026, 5, 25))   # Memorial Day (last Monday of May)
    assert is_market_holiday(date(2026, 6, 19))   # Juneteenth (Friday)
    assert is_market_holiday(date(2026, 9, 7))    # Labor Day (1st Monday of Sep)
    assert is_market_holiday(date(2026, 11, 26))  # Thanksgiving (4th Thursday of Nov)
    assert is_market_holiday(date(2026, 12, 25))  # Christmas Day (Friday)


def test_2026_independence_day_observed_shift_to_friday():
    # July 4, 2026 falls on a Saturday -> observed the preceding Friday.
    assert date(2026, 7, 4).weekday() == 5  # sanity check it's really a Saturday
    assert is_market_holiday(date(2026, 7, 3))  # observed Friday is the holiday
    assert not is_market_holiday(date(2026, 7, 4))  # the actual Saturday is not


def test_2025_and_2024_spot_checks():
    assert is_market_holiday(date(2025, 1, 1))
    assert is_market_holiday(date(2025, 12, 25))
    assert is_market_holiday(date(2024, 11, 28))  # Thanksgiving 2024
    assert is_market_holiday(date(2024, 5, 27))   # Memorial Day 2024


def test_regular_weekday_is_not_a_holiday():
    assert not is_market_holiday(date(2026, 8, 27))  # an ordinary Thursday
    assert not is_market_holiday(date(2026, 3, 10))  # an ordinary Tuesday


def test_nyse_holidays_returns_exactly_ten_dates_per_year():
    assert len(nyse_holidays(2026)) == 10
    assert len(nyse_holidays(2030)) == 10  # rule-based, so future years work too
