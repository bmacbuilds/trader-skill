"""XNYS calendar against public facts (spec 04.3, 09.5 'missing bar and half day').

Independent references: the NYSE's published annual trading-day counts and holiday and early-close schedules. The
counts below are the published totals, not values produced by this code.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from traderos.clock import XNYS, _easter
from traderos.contracts import DataError, ErrorCode, ExchangeCalendar

CAL = XNYS()
PUBLISHED_TRADING_DAYS = {2008: 253, 2009: 252, 2010: 252, 2011: 252, 2012: 250, 2013: 252, 2014: 252, 2015: 252,
                          2016: 252, 2017: 251, 2018: 251, 2019: 252, 2020: 253, 2021: 252, 2022: 251, 2023: 250,
                          2024: 252, 2025: 250}


def test_is_an_exchange_calendar():
    assert isinstance(CAL, ExchangeCalendar)


@pytest.mark.parametrize("year,count", sorted(PUBLISHED_TRADING_DAYS.items()))
def test_trading_days_per_year_match_the_published_count(year, count):
    assert len(CAL.sessions(date(year, 1, 1), date(year, 12, 31))) == count


@pytest.mark.parametrize("day", [date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
                                 date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
                                 date(2026, 11, 26), date(2026, 12, 25), date(2025, 1, 9), date(2018, 12, 5),
                                 date(2012, 10, 29), date(2012, 10, 30), date(2022, 6, 20), date(2021, 12, 24)])
def test_closed(day):
    assert not CAL.is_session(day)


@pytest.mark.parametrize("day", [date(2021, 12, 31),          # New Year's on a Saturday: no Friday holiday
                                 date(2021, 6, 18),           # Juneteenth is a market holiday from 2022 only
                                 date(2026, 11, 20), date(2026, 7, 2)])
def test_open(day):
    assert CAL.is_session(day)


def test_early_closes_at_13_00_new_york():
    for day in (date(2026, 11, 27), date(2026, 12, 24), date(2025, 7, 3), date(2025, 12, 24), date(2024, 7, 3)):
        assert CAL.is_early_close(day), day
        assert CAL.open_close(day)[1].astimezone(UTC).hour in (17, 18)   # 13:00 EDT or EST
    assert CAL.open_close(date(2026, 11, 27))[1] == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert not CAL.is_early_close(date(2026, 7, 2))                     # July 4 on a Saturday: no half day


def test_daylight_saving_shifts_the_utc_open():
    assert CAL.open_close(date(2026, 3, 6))[0] == datetime(2026, 3, 6, 14, 30, tzinfo=UTC)    # EST
    assert CAL.open_close(date(2026, 3, 9))[0] == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)    # EDT from Mar 8
    assert CAL.open_close(date(2026, 10, 30))[0] == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert CAL.open_close(date(2026, 11, 2))[0] == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)  # EST from Nov 1


def test_easter_reference_dates():
    assert [_easter(y) for y in (2008, 2019, 2024, 2025, 2026, 2027)] == [
        date(2008, 3, 23), date(2019, 4, 21), date(2024, 3, 31), date(2025, 4, 20), date(2026, 4, 5),
        date(2027, 3, 28)]


def test_next_previous_and_session_of():
    assert CAL.next_session(date(2026, 11, 25)) == date(2026, 11, 27)
    assert CAL.previous_session(date(2026, 11, 30)) == date(2026, 11, 27)
    assert CAL.session_of(datetime(2026, 11, 20, 15, 0, tzinfo=UTC)) == date(2026, 11, 20)
    assert CAL.session_of(datetime(2026, 11, 20, 21, 0, tzinfo=UTC)) is None             # after the close
    assert CAL.session_containing_close(datetime(2026, 11, 20, 21, 0, tzinfo=UTC)) == date(2026, 11, 20)
    assert CAL.session_containing_close(datetime(2026, 11, 27, 18, 0, tzinfo=UTC)) == date(2026, 11, 27)


@pytest.mark.parametrize("day", [date(2007, 12, 31), date(2028, 1, 3)])
def test_outside_coverage_is_calendar_unknown_never_a_guess(day):
    with pytest.raises(DataError) as e:
        CAL.is_session(day)
    assert e.value.code is ErrorCode.CALENDAR_UNKNOWN


def test_open_close_of_a_holiday_is_refused():
    with pytest.raises(DataError):
        CAL.open_close(date(2026, 12, 25))
