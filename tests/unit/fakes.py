"""Test fakes for part B: a small exchange calendar and dataset builders on top of MemoryBarReader.

WeekdayCalendar is NOT the real XNYS calendar (that is part A's core): sessions are Monday to Friday, 14:30 to
21:00 UTC, except HOLIDAYS, and EARLY_CLOSES close at 18:00 UTC. Outside COVERAGE it raises CALENDAR_UNKNOWN, as the
contract requires.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D

from traderos.contracts import DataError, ErrorCode, ExchangeCalendar, with_hash
from traderos.contracts.common import DatasetUsability, Severity
from traderos.contracts.examples import all_examples, instrument
from traderos.contracts.market import Adjustment, AvailabilityPolicy, Bar, Coverage, Interval, SourceKind
from traderos.testing import MemoryBarReader

HOLIDAYS = {date(2026, 11, 26)}                  # a Thursday
EARLY_CLOSES = {date(2026, 11, 27)}              # the Friday after it
OPEN, CLOSE, EARLY = time(14, 30, tzinfo=UTC), time(21, 0, tzinfo=UTC), time(18, 0, tzinfo=UTC)


class WeekdayCalendar:
    calendar_id = "TEST"
    version = "1"
    coverage = (date(2025, 1, 1), date(2027, 12, 31))

    def _check(self, day: date) -> None:
        if not self.coverage[0] <= day <= self.coverage[1]:
            raise DataError(f"{day} is outside the calendar", code=ErrorCode.CALENDAR_UNKNOWN)

    def is_session(self, day: date) -> bool:
        self._check(day)
        return day.weekday() < 5 and day not in HOLIDAYS

    def sessions(self, start: date, end: date) -> tuple[date, ...]:
        out, d = [], start
        while d <= end:
            if self.is_session(d):
                out.append(d)
            d += timedelta(days=1)
        return tuple(out)

    def open_close(self, day: date) -> tuple[datetime, datetime]:
        if not self.is_session(day):
            raise DataError(f"{day} is not a session", code=ErrorCode.CALENDAR_UNKNOWN)
        close = EARLY if day in EARLY_CLOSES else CLOSE
        return datetime.combine(day, OPEN), datetime.combine(day, close)

    def is_early_close(self, day: date) -> bool:
        return self.is_session(day) and day in EARLY_CLOSES

    def next_session(self, day: date) -> date:
        d = day + timedelta(days=1)
        while not self.is_session(d):
            d += timedelta(days=1)
        return d

    def previous_session(self, day: date) -> date:
        d = day - timedelta(days=1)
        while not self.is_session(d):
            d -= timedelta(days=1)
        return d

    def session_of(self, instant: datetime) -> date | None:
        day = instant.astimezone(UTC).date()
        if not self.is_session(day):
            return None
        o, c = self.open_close(day)
        return day if o <= instant < c else None


CAL = WeekdayCalendar()
assert isinstance(CAL, ExchangeCalendar)


def ins(symbol: str):
    return dataclasses.replace(instrument(), instrument_id=f"INS-{symbol}", symbol=symbol)


def daily(symbol: str, closes, *, last_day: date, gaps=None, complete_last: bool = True):
    """Daily bars ending on `last_day`, one per session, from oldest close to newest. Open = previous close unless
    `gaps` gives an open for that index; high/low wrap open and close."""
    days = []
    d = last_day
    while len(days) < len(closes):
        if CAL.is_session(d):
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    out, prev = [], None
    for i, (day, c) in enumerate(zip(days, closes, strict=True)):
        c = D(str(c))
        o = D(str(gaps[i])) if gaps and i in gaps else (prev if prev is not None else c)
        o_, cl = CAL.open_close(day)
        out.append(Bar(instrument_id=f"INS-{symbol}", interval=Interval.D1, start=o_, end=cl, open=o,
                       high=max(o, c) + D("0.5"), low=min(o, c) - D("0.5"), close=c, volume=D(1000 + i),
                       available_at=cl, provider="test", adjustment=Adjustment.RAW,
                       complete=complete_last or i < len(closes) - 1))
        prev = c
    return out


def intraday(symbol: str, day: date, closes, *, interval=Interval.M5, start: datetime | None = None):
    step = timedelta(minutes=5)
    t = start or CAL.open_close(day)[0]
    out, prev = [], None
    for c in closes:
        c = D(str(c))
        o = prev if prev is not None else c
        out.append(Bar(instrument_id=f"INS-{symbol}", interval=interval, start=t, end=t + step, open=o,
                       high=max(o, c) + D("0.05"), low=min(o, c) - D("0.05"), close=c, volume=D(500),
                       available_at=t + step, provider="test", adjustment=Adjustment.RAW))
        t += step
        prev = c
    return out


def dataset(series: dict[str, list[Bar]], *, interval=Interval.D1, synthetic=False, quality_issues=(),
            availability=AvailabilityPolicy.PROVIDER_STATED, feed=None, dataset_id="DS-T"):
    base = all_examples()["dataset/1"]
    instruments = tuple(ins(s) for s in series)
    manifest = with_hash(dataclasses.replace(
        base, dataset_id=dataset_id, interval=interval, synthetic=synthetic, instruments=instruments,
        source_kind=SourceKind.TUTORIAL if synthetic else SourceKind.CSV,
        usability=(DatasetUsability.UNAVAILABLE if any(q.severity is Severity.BLOCKING for q in quality_issues)
                   else DatasetUsability.LIMITED),
        availability_policy=availability, feed=feed, quality_issues=tuple(quality_issues), limitations=(),
        coverage=tuple(Coverage(instrument_id=i.instrument_id, symbol=i.symbol,
                                first_bar_end=series[i.symbol][0].end if series[i.symbol] else None,
                                last_bar_end=series[i.symbol][-1].end if series[i.symbol] else None,
                                bars=len(series[i.symbol])) for i in instruments),
        canonical_hash=None))
    reader = MemoryBarReader()
    reader.add(manifest, [b for bars in series.values() for b in bars])
    return reader, manifest
