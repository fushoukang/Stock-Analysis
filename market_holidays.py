"""Standard NYSE/Nasdaq market holiday calendar, computed by rule rather
than a hardcoded list of dates — so it stays correct for any year (past or
future) without needing yearly maintenance, and without adding a third-party
dependency (e.g. `pandas_market_calendars`) just for this.

Deliberately zero-dependency and importable standalone (no `config` import),
so `config.py` can import from here without any circular-import risk.

Covers the 9 holidays NYSE/Nasdaq actually observes (full-day closures only
— early-close half-days, e.g. the day after Thanksgiving, are NOT modeled
here, since `is_within_market_data_window()` only needs a yes/no "is the
market open at all today" answer, not intraday early-close times):

  New Year's Day, Martin Luther King Jr. Day, Washington's Birthday
  (Presidents Day), Good Friday, Memorial Day, Juneteenth National
  Independence Day (observed by NYSE since 2022), Independence Day,
  Labor Day, Thanksgiving Day, Christmas Day.

Fixed-date holidays (New Year's, Juneteenth, Independence Day, Christmas)
follow the standard federal "observed" shift: Saturday -> observed the
preceding Friday, Sunday -> observed the following Monday. Weekday-rule
holidays (MLK, Presidents, Memorial, Labor, Thanksgiving) and Good Friday
always land on a weekday already, so no shift applies to them.
"""
from __future__ import annotations

from datetime import date, timedelta


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) for the date of
    Easter Sunday in the Gregorian calendar. Good Friday (Easter - 2 days)
    is a NYSE holiday even though it isn't a US federal holiday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth occurrence (1-indexed) of `weekday` (Monday=0..Sunday=6) in
    the given month/year. E.g. _nth_weekday(2026, 1, 0, 3) == 3rd Monday of
    January 2026 (MLK Day)."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    d += timedelta(days=offset + 7 * (n - 1))
    return d


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last occurrence of `weekday` in the given month/year (e.g. the
    last Monday of May = Memorial Day)."""
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _observed(d: date) -> date:
    """Federal 'observed' shift for a fixed-date holiday: Saturday moves to
    the preceding Friday, Sunday moves to the following Monday."""
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set[date]:
    """All NYSE/Nasdaq full-market-closure holidays for a given calendar
    year, as a set of `date` objects (already shifted for weekend-observed
    fixed-date holidays)."""
    return {
        _observed(date(year, 1, 1)),                    # New Year's Day
        _nth_weekday(year, 1, 0, 3),                     # MLK Day (3rd Mon of Jan)
        _nth_weekday(year, 2, 0, 3),                     # Presidents Day (3rd Mon of Feb)
        _easter_sunday(year) - timedelta(days=2),        # Good Friday
        _last_weekday(year, 5, 0),                       # Memorial Day (last Mon of May)
        _observed(date(year, 6, 19)),                    # Juneteenth
        _observed(date(year, 7, 4)),                     # Independence Day
        _nth_weekday(year, 9, 0, 1),                      # Labor Day (1st Mon of Sep)
        _nth_weekday(year, 11, 3, 4),                     # Thanksgiving (4th Thu of Nov)
        _observed(date(year, 12, 25)),                    # Christmas Day
    }


def is_market_holiday(d: date) -> bool:
    """True if `d` (a plain calendar date, interpreted as US Eastern —
    callers should pass an ET date, not a UTC one) is a full NYSE/Nasdaq
    market-closure holiday."""
    return d in nyse_holidays(d.year)
