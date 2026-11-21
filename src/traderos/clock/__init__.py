"""Exchange calendars (spec 04.3, 06.4, 09.1). `XNYS` is the New York Stock Exchange's regular sessions.

The calendar is rules plus a dated list of exceptions, not a library guess (ADR-004). It covers 2008-01-01 through
2027-12-31. Outside that window every method raises DataError(CALENDAR_UNKNOWN): it never guesses. A change to a rule
or an exception bumps VERSION, and every experiment records the version it used (spec 08.5).

Times are computed without a timezone database: U.S. Eastern time is UTC-5, or UTC-4 from the second Sunday in March
at 02:00 local until the first Sunday in November at 02:00 local (the rule in force since 2007). That keeps the
core free of runtime dependencies on every operating system (ADR-001).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from traderos.contracts import DataError, ErrorCode

VERSION = "xnys/2026.11.1"
COVERAGE = (date(2008, 1, 1), date(2027, 12, 31))
OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Closures that no rule produces. Each is a dated, public NYSE closure.
SPECIAL_CLOSURES = {
    date(2012, 10, 29): "Hurricane Sandy",
    date(2012, 10, 30): "Hurricane Sandy",
    date(2018, 12, 5): "National Day of Mourning, President George H. W. Bush",
    date(2025, 1, 9): "National Day of Mourning, President Jimmy Carter",
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0) of a month; n=-1 for the last."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (the anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date, *, saturday_to_friday: bool = True) -> date | None:
    if day.weekday() == 5:
        return day - timedelta(days=1) if saturday_to_friday else None
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def holidays(year: int) -> dict[date, str]:
    """Full-day closures for one year: the rules, plus SPECIAL_CLOSURES."""
    out: dict[date, str] = {}

    def add(day: date | None, name: str) -> None:
        if day is not None and day.year == year:
            out[day] = name

    # New Year's Day on a Saturday is not observed on the Friday before (NYSE rule).
    add(_observed(date(year, 1, 1), saturday_to_friday=False), "New Year's Day")
    add(_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day")
    add(_nth_weekday(year, 2, 0, 3), "Washington's Birthday")
    add(_easter(year) - timedelta(days=2), "Good Friday")
    add(_nth_weekday(year, 5, 0, -1), "Memorial Day")
    if year >= 2022:
        add(_observed(date(year, 6, 19)), "Juneteenth")
    add(_observed(date(year, 7, 4)), "Independence Day")
    add(_nth_weekday(year, 9, 0, 1), "Labor Day")
    add(_nth_weekday(year, 11, 3, 4), "Thanksgiving Day")
    add(_observed(date(year, 12, 25)), "Christmas Day")
    out.update({d: n for d, n in SPECIAL_CLOSURES.items() if d.year == year})
    return out


def early_closes(year: int) -> dict[date, str]:
    """13:00 closes: the day after Thanksgiving; July 3 and December 24 when they fall Monday-Thursday."""
    out = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1): "Day after Thanksgiving"}
    for day, name in ((date(year, 7, 3), "Independence Day eve"), (date(year, 12, 24), "Christmas Eve")):
        if day.weekday() <= 3:
            out[day] = name
    return out


def _eastern_offset(day: date, local: time) -> timedelta:
    """UTC offset of U.S. Eastern time at a local wall-clock time (rules in force since 2007)."""
    start = datetime.combine(_nth_weekday(day.year, 3, 6, 2), time(2))
    end = datetime.combine(_nth_weekday(day.year, 11, 6, 1), time(2))
    return timedelta(hours=-4) if start <= datetime.combine(day, local) < end else timedelta(hours=-5)


def eastern_to_utc(day: date, local: time) -> datetime:
    naive = datetime.combine(day, local)
    return (naive - _eastern_offset(day, local)).replace(tzinfo=UTC)


class XNYS:
    """The NYSE regular-session calendar. Implements traderos.contracts.ExchangeCalendar."""

    calendar_id = "XNYS"
    version = VERSION
    coverage = COVERAGE

    def __init__(self) -> None:
        self._closed: dict[date, str] = {}
        self._early: dict[date, str] = {}
        for year in range(COVERAGE[0].year, COVERAGE[1].year + 1):
            self._closed.update(holidays(year))
            self._early.update({d: n for d, n in early_closes(year).items() if d not in self._closed})

    def _check(self, day: date) -> None:
        if isinstance(day, datetime) or not isinstance(day, date):
            raise DataError(f"a calendar day is a date, got {type(day).__name__}", code=ErrorCode.INVALID_INPUT)
        if not (COVERAGE[0] <= day <= COVERAGE[1]):
            raise DataError(f"{day} is outside the XNYS calendar's coverage {COVERAGE[0]} to {COVERAGE[1]}",
                            code=ErrorCode.CALENDAR_UNKNOWN)

    def is_session(self, day: date) -> bool:
        self._check(day)
        return day.weekday() < 5 and day not in self._closed

    def holiday_name(self, day: date) -> str | None:
        self._check(day)
        return self._closed.get(day)

    def is_early_close(self, day: date) -> bool:
        return self.is_session(day) and day in self._early

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        self._check(start)
        self._check(end)
        out, day = [], start
        while day <= end:
            if self.is_session(day):
                out.append(day)
            day += timedelta(days=1)
        return tuple(out)

    def open_close(self, day: date) -> tuple[datetime, datetime]:
        if not self.is_session(day):
            raise DataError(f"{day} is not an XNYS session ({self._closed.get(day, 'weekend')})",
                            code=ErrorCode.CALENDAR_UNKNOWN)
        close = EARLY_CLOSE if day in self._early else CLOSE
        return eastern_to_utc(day, OPEN), eastern_to_utc(day, close)

    def next_session(self, day: date) -> date:
        self._check(day)
        nxt = day + timedelta(days=1)
        while not self.is_session(nxt):
            nxt += timedelta(days=1)
        return nxt

    def previous_session(self, day: date) -> date:
        self._check(day)
        prev = day - timedelta(days=1)
        while not self.is_session(prev):
            prev -= timedelta(days=1)
        return prev

    def session_of(self, instant: datetime) -> date | None:
        if instant.tzinfo is None:
            raise DataError("instant must be timezone-aware", code=ErrorCode.INVALID_INPUT)
        utc = instant.astimezone(UTC)
        for day in (utc.date() - timedelta(days=1), utc.date()):
            if COVERAGE[0] <= day <= COVERAGE[1] and self.is_session(day):
                opened, closed = self.open_close(day)
                if opened <= utc < closed:
                    return day
        return None

    def session_containing_close(self, instant: datetime) -> date | None:
        """The session whose close is exactly `instant` (a daily bar's end), if any."""
        utc = instant.astimezone(UTC)
        day = (utc + _eastern_offset(utc.date(), time(12))).date()
        if COVERAGE[0] <= day <= COVERAGE[1] and self.is_session(day) and self.open_close(day)[1] == utc:
            return day
        return None


CALENDARS = {"XNYS": XNYS}


def calendar(calendar_id: str = "XNYS") -> XNYS:
    try:
        return CALENDARS[calendar_id]()
    except KeyError:
        raise DataError(f"no calendar {calendar_id!r}", code=ErrorCode.CALENDAR_UNKNOWN) from None
