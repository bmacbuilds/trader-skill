"""What every part B result shares: the instant it decides at, how fresh its data is, and what it may be used for.

* **As of.** Every read goes through `BarReader.bars(as_of=...)`, so nothing not yet available is ever read (QT01).
  `as_of=None` means the newest instant any bar in the dataset became available.
* **Freshness from the exchange calendar, never from wall-clock gaps.** A Friday close read on Sunday is not stale;
  a Thursday close read after Friday's close is. Outside the calendar's coverage it raises CALENDAR_UNKNOWN and
  never guesses (contracts v1 section 7).
* **Labels.** Tutorial data is TUTORIAL_ONLY with SYNTHETIC_DATA; availability assigned by policy is PIT_LIMITED;
  an IEX feed says it is one exchange, not the whole market. Nothing is ever safe for trading: SafeFor has no such
  value.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from traderos.contracts import (
    Bar,
    BarReader,
    DataError,
    DatasetManifest,
    DatasetUsability,
    ErrorCode,
    ExchangeCalendar,
    Instrument,
    Limitation,
    SafeFor,
    Severity,
    ValidationError,
    record_hash,
)
from traderos.contracts.canonical import sha256_bytes
from traderos.contracts.market import AvailabilityPolicy, Interval

STEP = {Interval.M1: timedelta(minutes=1), Interval.M5: timedelta(minutes=5), Interval.M15: timedelta(minutes=15),
        Interval.H1: timedelta(hours=1)}
_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)


def aware(value: datetime, name: str = "as_of") -> datetime:
    if value.tzinfo is None:
        raise ValidationError(f"{name} must be timezone-aware", code=ErrorCode.INVALID_INPUT)
    return value.astimezone(UTC)


def resolve_as_of(reader: BarReader, dataset_id: str, as_of: datetime | None) -> datetime:
    """The instant to decide at: as given, or the newest `available_at` of any bar in the dataset."""
    if as_of is not None:
        return aware(as_of)
    newest = [b.available_at for ins in reader.instruments(dataset_id)
              for b in reader.bars(dataset_id, ins.instrument_id, as_of=_FAR_FUTURE, limit=1)]
    if not newest:
        raise DataError(f"{dataset_id} has no bars", code=ErrorCode.MISSING_DATA)
    return max(newest)


def usable(manifest: DatasetManifest) -> DatasetManifest:
    """A dataset its quality gate rejected, or found unavailable, is never read as if it were usable (spec 06.4: a
    blocking issue cannot be overridden by a casual accept)."""
    if manifest.usability in (DatasetUsability.REJECTED, DatasetUsability.UNAVAILABLE):
        blocking = [q.code for q in manifest.quality_issues if q.severity is Severity.BLOCKING]
        raise DataError(f"{manifest.dataset_id} is {manifest.usability.value}"
                        + (f": blocking quality issue(s) {', '.join(blocking)}" if blocking else ""),
                        code=ErrorCode.MISSING_DATA, details={"usability": manifest.usability.value})
    return manifest


def find_instrument(manifest: DatasetManifest, name: str) -> Instrument | None:
    """By instrument_id, else by symbol when exactly one instrument has it (a ticker alone is not identity)."""
    for ins in manifest.instruments:
        if ins.instrument_id == name:
            return ins
    hits = [i for i in manifest.instruments if i.symbol.upper() == name.upper()]
    return hits[0] if len(hits) == 1 else None


def complete_bars(bars: Iterable[Bar]) -> tuple[Bar, ...]:
    """Only finished bars: a session still running is never scanned or alerted on as if it were done."""
    return tuple(b for b in bars if b.complete)


# ---- the calendar ----------------------------------------------------------------------------------------------

def last_close_at_or_before(calendar: ExchangeCalendar, instant: datetime):
    """(session, close) of the newest session that had closed by `instant`. A session closes on its own UTC date
    (XNYS closes at 20:00 or 21:00 UTC), so the search starts at the instant's UTC date: it never steps past the
    calendar's last covered day."""
    day = instant.astimezone(UTC).date()
    for _ in range(15):
        if calendar.is_session(day):
            close = calendar.open_close(day)[1]
            if close <= instant:
                return day, close
        day = calendar.previous_session(day)
    raise DataError(f"no session closed in the two weeks before {instant.isoformat()}",
                    code=ErrorCode.CALENDAR_UNKNOWN)


def expected_last_end(calendar: ExchangeCalendar, interval: Interval, instant: datetime) -> datetime:
    """When the newest bar that SHOULD exist by `instant` ended. Daily: the newest close. Intraday: the newest whole
    interval since today's open, or yesterday's close in the first interval or outside a session."""
    if interval is Interval.D1:
        return last_close_at_or_before(calendar, instant)[1]
    step = STEP[interval]
    day = calendar.session_of(instant)
    if day is not None:
        open_, close = calendar.open_close(day)
        until = min(instant, close)
        if until - open_ >= step:
            return open_ + ((until - open_) // step) * step
    return last_close_at_or_before(calendar, instant - timedelta(microseconds=1) if day else instant)[1]


def _session_by_end(calendar: ExchangeCalendar, end: datetime):
    """The session a bar ending at `end` belongs to: the one open just before `end`, else the newest session that had
    closed by `end` (never the calendar date of `end`, which undercounts across a weekend or a holiday)."""
    return calendar.session_of(end - timedelta(microseconds=1)) or last_close_at_or_before(calendar, end)[0]


MAX_STALENESS_BARS = 20                          # a staleness allowance is a small whole number of bars


def staleness_bound(n: int, name: str = "max_staleness_bars") -> int:
    """1..MAX_STALENESS_BARS. 0 would call everything stale; a huge number would call a month-old close fresh."""
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_STALENESS_BARS:
        raise ValidationError(f"{name} must be a whole number from 1 to {MAX_STALENESS_BARS}, not {n!r}",
                              code=ErrorCode.INVALID_INPUT)
    return n


def is_stale(calendar: ExchangeCalendar, interval: Interval, last_end: datetime, at: datetime,
             max_staleness_bars: int) -> tuple[bool, int]:
    """(stale, bars behind): the newest complete bar against the newest that should exist by `at`."""
    behind = bars_behind(calendar, interval, last_end, expected_last_end(calendar, interval, at))
    return behind > max_staleness_bars - 1, behind


def session_seconds(calendar: ExchangeCalendar, start: datetime, end: datetime) -> float:
    """Seconds of regular sessions between two instants: nights, weekends and holidays count for nothing."""
    if end <= start:
        return 0.0
    total, day = 0.0, start.astimezone(UTC).date()
    last = end.astimezone(UTC).date()
    while day <= last:
        if calendar.is_session(day):
            o, c = calendar.open_close(day)
            lo, hi = max(o, start), min(c, end)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


def session_gaps(calendar: ExchangeCalendar, bars: Sequence[Bar], sessions: int) -> list:
    """Each session's first open against the previous session's last close, for the newest `sessions` session pairs
    in `bars` (complete bars, oldest first). Bar-to-bar gaps inside a session are not gaps: on 5-minute data they
    would hide the overnight risk a stop really runs."""
    by_day: dict = {}
    for b in bars:
        day = calendar.session_of(b.start)
        if day is not None:
            first, last = by_day.get(day, (b, b))
            by_day[day] = (first if first.start <= b.start else b, last if last.end >= b.end else b)
    days = sorted(by_day)
    out = []
    for prev, day in pairwise(days):
        if calendar.previous_session(day) != prev:
            continue                             # a missing session in between: that gap is unknown, not measured
        close = by_day[prev][1].close
        if close > 0:
            out.append(by_day[day][0].open / close - Decimal(1))
    return out[-sessions:]


def bars_behind(calendar: ExchangeCalendar, interval: Interval, last_end: datetime, expected: datetime) -> int:
    """How many bars the data is behind the newest one that should exist (0 = current)."""
    if last_end >= expected:
        return 0
    if interval is Interval.D1:
        last_day = _session_by_end(calendar, last_end)
        exp_day = _session_by_end(calendar, expected)
        return len(calendar.sessions(last_day + timedelta(days=1), exp_day))
    step = STEP[interval]
    last_day = calendar.session_of(last_end - timedelta(microseconds=1))
    exp_day = calendar.session_of(expected - timedelta(microseconds=1))
    if last_day is not None and last_day == exp_day:
        return int((expected - last_end) // step)
    # Across sessions: the rest of the last bar's session, every whole session between, and today's bars so far.
    behind = 0
    if last_day is not None:
        behind += int((calendar.open_close(last_day)[1] - last_end) // step)
        start = last_day + timedelta(days=1)
    else:
        start = last_end.date()
    if exp_day is not None:
        for d in calendar.sessions(start, exp_day - timedelta(days=1)):
            o, c = calendar.open_close(d)
            behind += int((c - o) // step)
        behind += int((expected - calendar.open_close(exp_day)[0]) // step)
    return max(behind, 1)


# ---- labels ----------------------------------------------------------------------------------------------------

def labels(manifest: DatasetManifest) -> tuple[tuple[Limitation, ...], tuple[SafeFor, ...]]:
    """The limitations every result on this dataset carries, and what it is safe for."""
    lims = list(manifest.limitations)
    codes = {x.code for x in lims}

    def add(code: ErrorCode, detail: str, severity: Severity = Severity.WARNING) -> None:
        if code.value not in codes:
            lims.append(Limitation(code=code.value, detail=detail, severity=severity))
            codes.add(code.value)

    if manifest.synthetic:
        add(ErrorCode.SYNTHETIC_DATA, "Synthetic tutorial data; not market evidence.")
    if manifest.availability_policy is AvailabilityPolicy.INTERVAL_END:
        add(ErrorCode.PIT_LIMITED, "When each bar became available was assigned as its end, not stated by the "
                                   "provider: research only.")
    if (manifest.feed or "").lower() == "iex":
        add(ErrorCode.OUTSIDE_CAPABILITY, "IEX prices and volume come from one exchange, not the whole market "
                                          "(SIP); volume and breadth are partial.")
    safe = (SafeFor.TUTORIAL_ONLY,) if manifest.synthetic else (SafeFor.RESEARCH, SafeFor.DISPLAY)
    return tuple(lims), safe


def dataset_hash(manifest: DatasetManifest) -> str:
    """The manifest's canonical hash, computed when the store handed over a manifest without one."""
    return manifest.canonical_hash or record_hash(manifest)


def stable_id(prefix: str, *parts: object) -> str:
    """A deterministic opaque id: the same inputs give the same id, so a rerun is recognisably the same result."""
    return f"{prefix}-{sha256_bytes('|'.join(str(p) for p in parts).encode())[:16]}"


def now_utc(now: datetime | None) -> datetime:
    return aware(now, "now") if now is not None else datetime.now(UTC)


def newest(bars: Sequence[Bar]) -> Bar | None:
    return bars[-1] if bars else None
