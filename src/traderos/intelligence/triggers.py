"""The upstream entry triggers, ported from the tank (tank/day-trader-skill/trader-entry/entry.py `evaluate` at
3d0f4ef).

The rules are upstream's, line for line:
* **orb**: the range is the high and low of the first `or_minutes` (default 15). The trigger is the first COMPLETED
  bar after the range that CLOSES above the range high. A wick above the high is not a trigger.
* **level**: the first completed bar that closes above a given price (for example a 20-day high from the scanner).
* **min_volume_x**: the trigger bar also needs at least X times the average volume of the opening-range bars.
* **The entry window**: no entries before `no_entries_before` (09:45) or after `no_entries_after` (15:30), New York
  time. On a 1:00 p.m. early close the cutoff moves up with the close (15:30 becomes 12:30); a cutoff already before
  the early close stays (the upstream `early_cutoff`).
* **Statuses**: triggered, waiting (before the open, the range still forming, no close above yet), expired (no trigger
  by the cutoff) and no-data (the market closed today, no bars, or an opening range missing a bar: upstream PR #23,
  "a missing bar must not look done just because enough time has passed"). A trigger's time is the trigger bar's
  close and its price is that close.
* Long side only, 1-minute or 5-minute bars, and it never places an order.

Changed, each with its reason:
* **Stored bars through the as-of BarReader, not Yahoo's chart endpoint.** Flaw: spec 06.1 holds Yahoo; the bars
  had no point-in-time guarantee. The upstream lag handling (`SETTLE`, `data_time`) was for Yahoo's late posting; the
  store already withholds a bar until its `available_at`.
* **XNYS sessions, not a 2026-2027 holiday table.** Flaw: hand-kept, copied into four files.
* **New York wall-clock times are placed from the session's 09:30 open**, and the New York date from the same open
  (local midnight is 9h30m before it), so no timezone table is needed and DST cannot be guessed wrong (a better
  method for the same times).
* **Decimal prices, not floats** (contracts v1).
* **A thin bar's volume multiple is shown rounded down.** Flaw: `%.2f` printed 1.998x as "2.00x ... under your
  2.00x", a passing number in a refusal.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from traderos.clock import eastern_to_utc
from traderos.contracts import DataError, ErrorCode, ExchangeCalendar, ValidationError
from traderos.contracts.market import Bar, Interval
from traderos.intelligence._common import last_close_at_or_before

DEFAULTS = {"no_entries_before": "09:45", "no_entries_after": "15:30"}   # The upstream DEFAULTS
STEP = {Interval.M1: 60, Interval.M5: 300}
LOCAL_OPEN = (9, 30)                             # XNYS opens at 9:30 New York time
REGULAR_SESSION = timedelta(hours=6, minutes=30)  # 9:30 to 16:00
_BEFORE_OPEN = timedelta(hours=9, minutes=30)     # New York midnight to the open
_AFTER_OPEN = timedelta(hours=14, minutes=30)     # the open to the next New York midnight
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Trigger:
    status: str                                  # "triggered" | "waiting" | "expired" | "no-data"
    phase: str                                   # "closed" | "pre-open" | "forming" | "open" | "after-cutoff" | ...
    reason: str
    level: Decimal | None = None
    trigger_bar: Bar | None = None
    volume_x: Decimal | None = None
    last_close: Decimal | None = None
    cutoff: datetime | None = None
    window_start: datetime | None = None


def ny_date(at: datetime) -> date:
    """The New York date at `at`, from the clock's Eastern-time rules (exact across DST)."""
    d = at.astimezone(UTC).date()
    return d if at >= eastern_to_utc(d, time(0)) else d - timedelta(days=1)


def ny_time(at: datetime) -> time:
    """The New York wall-clock time at `at`."""
    day = ny_date(at)
    return (datetime.combine(day, time(0)) + (at - eastern_to_utc(day, time(0)))).time()


def number(value, what: str, *, minimum: Decimal | None = None, allow_zero: bool = False) -> Decimal | None:
    """A finite Decimal from an int, a Decimal or a decimal string (never a float, NaN or infinity), or None."""
    if value is None:
        return None
    if isinstance(value, (bool, float)):
        raise ValidationError(f"{what}: write a decimal as a string, not {type(value).__name__}",
                              code=ErrorCode.INVALID_INPUT)
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation:
        raise ValidationError(f"{what} is not a number: {value!r}", code=ErrorCode.INVALID_INPUT) from None
    if not d.is_finite() or d < 0 or (d == 0 and not allow_zero) or (minimum is not None and d < minimum):
        raise ValidationError(f"{what} must be a finite number above 0" + (" (or 0)" if allow_zero else "")
                              + f", not {value!r}", code=ErrorCode.INVALID_INPUT)
    return d


def _hhmm(text: str, what: str) -> tuple[int, int]:
    m = _HHMM.match(text or "")
    if not m:
        raise ValidationError(f"{what} wants HH:MM, not {text!r}", code=ErrorCode.INVALID_INPUT)
    return int(m.group(1)), int(m.group(2))


def _down(x: Decimal) -> Decimal:
    """A volume multiple shown to 2 places, rounded DOWN: 1.998x prints as 1.99x, never as a passing 2.00x."""
    return x.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def local_time(open_utc: datetime, hhmm: tuple[int, int]) -> datetime:
    """A New York wall-clock time on the session that opened at `open_utc` (09:30 New York)."""
    return open_utc + timedelta(hours=hhmm[0] - LOCAL_OPEN[0], minutes=hhmm[1] - LOCAL_OPEN[1])


def early_cutoff(at: datetime, open_utc: datetime, close_utc: datetime) -> datetime:
    """The upstream rule: a cutoff on an early-close day moves up with the close; one already before the close stays."""
    if at < close_utc:
        return at
    moved = at - (open_utc + REGULAR_SESSION - close_utc)
    return moved if moved > open_utc else close_utc


def trading_day(calendar: ExchangeCalendar, at: datetime):
    """The New York date at `at` and whether the NYSE trades on it: (session or None, the date's first instant).

    A session's New York day runs from 9h30m before its open to 14h30m after it, so this is exact without a
    timezone table: 8 p.m. New York on a Monday is still Monday, as in the upstream `to_eastern(now).date()`."""
    day = calendar.session_of(at)
    if day is not None:
        return day
    prev = last_close_at_or_before(calendar, at)[0]
    if at < calendar.open_close(prev)[0] + _AFTER_OPEN:
        return prev
    nxt = at.date() if calendar.is_session(at.date()) and calendar.open_close(at.date())[0] > at \
        else calendar.next_session(at.date())
    if at >= calendar.open_close(nxt)[0] - _BEFORE_OPEN:
        return nxt
    return None


def session_slots(bars: Sequence[Bar], open_: datetime, close: datetime, step: int, at: datetime) -> dict[int, Bar]:
    """The session's complete, on-grid bars that had closed by `at`, by slot (0 = the bar starting at the open)."""
    by_slot: dict[int, Bar] = {}
    for b in bars:
        offset = (b.start - open_).total_seconds()
        if not b.complete or offset < 0 or offset % step or b.start >= close or b.end > at:
            continue
        by_slot[int(offset) // step] = b
    return by_slot


def opening_range(by_slot: dict[int, Bar], n_or: int) -> tuple[Decimal, Decimal]:
    """(high, low) of the first n_or bars; every one of them must be there (upstream PR #23)."""
    or_bars = [by_slot[k] for k in range(n_or) if k in by_slot]
    if len(or_bars) != n_or:
        raise DataError(f"the opening range is missing {n_or - len(or_bars)} of {n_or} bar(s): the range cannot be "
                        "trusted", code=ErrorCode.MISSING_DATA)
    return max(b.high for b in or_bars), min(b.low for b in or_bars)


def evaluate(calendar: ExchangeCalendar, bars: Sequence[Bar], interval: Interval, at: datetime, *,
             setup: str = "orb", or_minutes: int = 15, level: Decimal | None = None,
             min_volume_x: Decimal | None = None, no_entries_before: str = DEFAULTS["no_entries_before"],
             no_entries_after: str = DEFAULTS["no_entries_after"]) -> Trigger:
    """The upstream `evaluate`: the setup's status at `at` from the session's complete bars (oldest first)."""
    if interval not in STEP:
        raise ValidationError("entry triggers read 1-minute or 5-minute bars", code=ErrorCode.INVALID_INPUT)
    step = STEP[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise ValidationError(f"or_minutes must be a whole number of {interval.value} bars",
                              code=ErrorCode.INVALID_INPUT)
    if setup not in ("orb", "level"):
        raise ValidationError("setup is orb or level", code=ErrorCode.INVALID_INPUT)
    if setup == "level" and level is None:
        raise ValidationError("setup level needs a level price (for example a 20-day high)",
                              code=ErrorCode.INVALID_INPUT)
    level = number(level, "level")                          # finite and above 0
    min_volume_x = number(min_volume_x, "min_volume_x", allow_zero=True)   # Upstream: 0 (or none) means off
    before_hm, after_hm = _hhmm(no_entries_before, "no_entries_before"), _hhmm(no_entries_after, "no_entries_after")
    n_or = or_minutes * 60 // step
    day = trading_day(calendar, at)
    if day is None:
        return Trigger("no-data", "closed", "the NYSE is closed today")
    open_, close = calendar.open_close(day)
    early = calendar.is_early_close(day)
    before = local_time(open_, before_hm)
    cutoff = early_cutoff(local_time(open_, after_hm), open_, close)
    or_end = open_ + timedelta(minutes=or_minutes)
    by_slot = session_slots(bars, open_, close, step, at)
    session = [by_slot[k] for k in sorted(by_slot)]
    slot_of = {id(b): k for k, b in by_slot.items()}
    if at < open_:
        return Trigger("waiting", "pre-open", "the NYSE opens at 9:30" + (
            f" and the {or_minutes}-minute range is set at {or_end.isoformat()}" if setup == "orb" else ""),
            cutoff=cutoff, window_start=before)
    or_bars = [b for b in session if slot_of[id(b)] < n_or]
    avg_vol = sum((b.volume for b in or_bars), Decimal(0)) / len(or_bars) if or_bars else None
    if setup == "orb":
        if at < or_end:
            return Trigger("waiting", "forming", f"{len(or_bars)} of {n_or} range bars closed; the range is set at "
                                                 f"{or_end.isoformat()}", cutoff=cutoff, window_start=before)
        if not or_bars:
            return Trigger("no-data", "open", "no bars for the opening range", cutoff=cutoff, window_start=before)
        if len(or_bars) != n_or:
            return Trigger("no-data", "open", f"the opening range is missing {n_or - len(or_bars)} of {n_or} bar(s): "
                                              f"the range cannot be trusted", cutoff=cutoff, window_start=before)
        lvl = max(b.high for b in or_bars)
    else:
        lvl = level
    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in session:
        if slot_of[id(b)] < first_slot or b.end < before or b.end > cutoff:
            continue
        if b.close > lvl:
            vx = b.volume / avg_vol if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = session[-1] if session else None
    what = "the range high" if setup == "orb" else "the level"
    if trig is not None:
        vx = trig.volume / avg_vol if avg_vol else None
        return Trigger("triggered", "open" if at < close else "after-close",
                       f"the bar ending {trig.end.isoformat()} closed above {what} {lvl}", level=lvl, trigger_bar=trig,
                       volume_x=None if vx is None else vx.quantize(Decimal("0.01")), last_close=last.close,
                       cutoff=cutoff, window_start=before)
    if last is None:
        return Trigger("no-data", "open", f"no bars for {day.isoformat()} yet", level=lvl, cutoff=cutoff,
                       window_start=before)
    if at >= cutoff:
        return Trigger("expired", "after-close" if at >= close else "after-cutoff",
                       f"no close above {what} {lvl} by {cutoff.isoformat()}, the last time for a new entry"
                       + (f" (early close today at {close.isoformat()})" if early else ""), level=lvl,
                       last_close=last.close, cutoff=cutoff, window_start=before)
    if last.close > lvl and last in thin:
        why = (f"the bar ending {last.end.isoformat()} closed above at {last.close} but on "
               f"{_down(last.volume / avg_vol if avg_vol else Decimal(0))}x the range's volume, under {min_volume_x}x")
    elif last.close > lvl and last.end < before:
        why = f"the bar ending {last.end.isoformat()} closed above at {last.close}, but no entries before {before}"
    else:
        side = "under" if lvl >= last.close else "over"
        why = f"last close {last.close}, {abs(lvl - last.close)} {side} {what} {lvl}"
    return Trigger("waiting", "open", why, level=lvl, last_close=last.close, cutoff=cutoff, window_start=before)
