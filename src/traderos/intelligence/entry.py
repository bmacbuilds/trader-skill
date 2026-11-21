"""S13 Entry Timer: is a setup session-eligible at this instant? Research only; never an order (spec 12.1 S13).

It answers from the exchange calendar and the data, not from a clock on the wall:

* **Closed market:** not eligible, with when the next session opens.
* **Opening range:** for intraday data, the first OPENING_RANGE_MINUTES of a session are not eligible: the range is
  not formed yet (upstream PR #23, DATA-001, incomplete opening range).
* **Close:** in the last CLOSE_BUFFER_MINUTES, and on an early-close day the constraint names the early close
  (upstream PR #22, CAL-001).
* **Stale data:** the newest complete bar is behind the newest that should exist: unavailable, DATA_STALE.
* **Daily data** cannot time an entry inside a session: it says so as a constraint and a limitation.

With `setup="orb"` or `"level"` it answers the upstream question instead: has the setup triggered?
(traderos.intelligence. triggers, ported from the tank's Entry Timer). Triggered is ELIGIBLE at the trigger bar's close;
waiting and expired are NOT_ELIGIBLE with the upstream reason; no bars, or an opening range missing a bar, is
UNAVAILABLE. The upstream entry window (09:45 to 15:30 New York, moved up on an early close) then replaces the generic
opening-range and close buffers.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from traderos.contracts import (
    BarReader,
    Completeness,
    DataError,
    EntryAssessment,
    ErrorCode,
    ExchangeCalendar,
    Limitation,
    MetricValue,
    ValidationError,
    with_hash,
)
from traderos.contracts.intel import EntryStatus
from traderos.contracts.market import Interval
from traderos.intelligence import triggers
from traderos.intelligence._common import (
    bars_behind,
    complete_bars,
    dataset_hash,
    expected_last_end,
    find_instrument,
    labels,
    now_utc,
    resolve_as_of,
    stable_id,
    staleness_bound,
    usable,
)

OPENING_RANGE_MINUTES = 30
CLOSE_BUFFER_MINUTES = 15
RESEARCH_ONLY = Limitation(code="RESEARCH_ONLY", detail="Entry Timer assesses timing for research; it never places, "
                                                        "sends or recommends an order.")


def _hhmm(t: datetime) -> str:
    return t.strftime("%H:%M UTC")


def _next_open(calendar: ExchangeCalendar, at: datetime) -> datetime:
    """The next session's open after `at`: today's, if today is a session that has not opened yet."""
    day = at.date()
    if calendar.is_session(day) and calendar.open_close(day)[0] > at:
        return calendar.open_close(day)[0]
    return calendar.open_close(calendar.next_session(day))[0]


def assess_entry(reader: BarReader, calendar: ExchangeCalendar, dataset_id: str, symbol: str, *,
                 as_of: datetime | None = None, setup_ref: str | None = None,
                 max_staleness_bars: int = 1, now: datetime | None = None, setup: str | None = None,
                 level: Decimal | None = None, or_minutes: int = 15, min_volume_x: Decimal | None = None,
                 no_entries_before: str = triggers.DEFAULTS["no_entries_before"],
                 no_entries_after: str = triggers.DEFAULTS["no_entries_after"]) -> EntryAssessment:
    staleness_bound(max_staleness_bars)
    manifest = usable(reader.manifest(dataset_id))
    ins = find_instrument(manifest, symbol)
    if ins is None:
        raise DataError(f"{symbol} is not in {dataset_id}", code=ErrorCode.MISSING_DATA)
    at = resolve_as_of(reader, dataset_id, as_of)
    limitations, safe_for = labels(manifest)
    limitations = (*limitations, RESEARCH_ONLY)
    if setup is not None:
        return _assess_setup(reader, calendar, manifest, ins, at, limitations, safe_for, setup_ref=setup_ref,
                             max_staleness_bars=max_staleness_bars, now=now,
                             spec={"setup": setup, "level": level, "or_minutes": or_minutes,
                                   "min_volume_x": min_volume_x, "no_entries_before": no_entries_before,
                                   "no_entries_after": no_entries_after})

    constraints: list[str] = []
    invalid: list[str] = []
    day = calendar.session_of(at)
    session_open = day is not None
    if not session_open:
        invalid.append(f"the market is closed; the next session opens {_next_open(calendar, at).isoformat()}")
    else:
        open_, close = calendar.open_close(day)
        if calendar.is_early_close(day):
            constraints.append(f"early close today at {_hhmm(close)}")
        if manifest.interval is Interval.D1:
            constraints.append("daily bars: timing inside the session is not assessed")
        elif at < open_ + timedelta(minutes=OPENING_RANGE_MINUTES):
            invalid.append(f"opening range incomplete until {_hhmm(open_ + timedelta(minutes=OPENING_RANGE_MINUTES))}")
        if at >= close - timedelta(minutes=CLOSE_BUFFER_MINUTES):
            invalid.append(f"less than {CLOSE_BUFFER_MINUTES} minutes to the close at {_hhmm(close)}")

    bars = complete_bars(reader.bars(dataset_id, ins.instrument_id, as_of=at, limit=3))
    last = bars[-1] if bars else None
    if last is None:
        status, ref = EntryStatus.UNAVAILABLE, MetricValue(value=None, unit=ins.currency,
                                                            unavailable_reason="no complete bars available")
        completeness = Completeness.UNAVAILABLE
    else:
        ref = MetricValue(value=last.close, unit=ins.currency)
        behind = bars_behind(calendar, manifest.interval, last.end, expected_last_end(calendar, manifest.interval, at))
        if behind > max_staleness_bars - 1:
            status, completeness = EntryStatus.UNAVAILABLE, Completeness.PARTIAL
            invalid.append(f"{ErrorCode.DATA_STALE.value}: the newest complete bar ended {last.end.isoformat()}, "
                           f"{behind} bar(s) behind")
        else:
            status = EntryStatus.NOT_ELIGIBLE if invalid else EntryStatus.ELIGIBLE
            # Daily bars inside a running session: the timing itself was not assessed, so the answer is partial.
            completeness = (Completeness.PARTIAL if manifest.interval is Interval.D1 and session_open
                            else Completeness.COMPLETE)
    if manifest.interval is Interval.D1:
        limitations = (*limitations, Limitation(code="DAILY_BARS", detail="Daily bars cannot time an entry within "
                                                                          "the session."))
    return with_hash(EntryAssessment(
        assessment_id=stable_id("EA", dataset_hash(manifest), ins.instrument_id, at.isoformat(), setup_ref),
        dataset_id=dataset_id, dataset_hash=dataset_hash(manifest),
        instrument_id=ins.instrument_id, symbol=ins.symbol, as_of=at, setup_ref=setup_ref, status=status,
        session_open=session_open, reference_price=ref, constraints=tuple(constraints),
        invalidation_reasons=tuple(invalid), completeness=completeness, limitations=limitations, safe_for=safe_for,
        created_at=now_utc(now)))


def _assess_setup(reader, calendar, manifest, ins, at, limitations, safe_for, *, setup_ref, max_staleness_bars, now,
                  spec) -> EntryAssessment:
    """The upstream trigger, in entry/1's terms."""
    if manifest.interval not in triggers.STEP:
        raise ValidationError(f"setup {spec['setup']} reads 1-minute or 5-minute bars; {manifest.dataset_id} holds "
                              f"{manifest.interval.value} bars", code=ErrorCode.INVALID_INPUT)
    # The trading day's bars from its open: after-hours bars kept by the CSV gate can never push the opening range
    # out of a "newest N" window (review of #13, item 7). The newest bar is read separately, for freshness.
    trading = triggers.trading_day(calendar, at)
    session = (complete_bars(reader.bars(manifest.dataset_id, ins.instrument_id, as_of=at,
                                         start=calendar.open_close(trading)[0])) if trading is not None else ())
    t = triggers.evaluate(calendar, session, manifest.interval, at, **spec)
    bars = complete_bars(reader.bars(manifest.dataset_id, ins.instrument_id, as_of=at, limit=2))
    constraints = [f"{spec['setup']}: {t.reason}"] if t.status == "triggered" else []
    if t.window_start is not None and t.cutoff is not None:
        constraints.append(f"entries from {_hhmm(t.window_start)} to {_hhmm(t.cutoff)}")
    day = calendar.session_of(at)
    if day is not None and calendar.is_early_close(day):
        constraints.append(f"early close today at {_hhmm(calendar.open_close(day)[1])}")
    invalid = [] if t.status == "triggered" else [t.reason]
    last = bars[-1] if bars else None
    behind = (bars_behind(calendar, manifest.interval, last.end, expected_last_end(calendar, manifest.interval, at))
              if last is not None else 0)
    if t.phase == "closed":
        status, completeness = EntryStatus.NOT_ELIGIBLE, Completeness.COMPLETE
        invalid = [f"the market is closed; the next session opens {_next_open(calendar, at).isoformat()}"]
    elif last is not None and behind > max_staleness_bars - 1:
        status, completeness = EntryStatus.UNAVAILABLE, Completeness.PARTIAL
        invalid.append(f"{ErrorCode.DATA_STALE.value}: the newest complete bar ended {last.end.isoformat()}, "
                       f"{behind} bar(s) behind")
    elif t.status == "no-data":
        status, completeness = EntryStatus.UNAVAILABLE, Completeness.UNAVAILABLE
    else:
        status = EntryStatus.ELIGIBLE if t.status == "triggered" else EntryStatus.NOT_ELIGIBLE
        completeness = Completeness.COMPLETE
    price = t.trigger_bar.close if t.trigger_bar is not None else (last.close if last is not None else None)
    ref = (MetricValue(value=price, unit=ins.currency) if price is not None
           else MetricValue(value=None, unit=ins.currency, unavailable_reason="no complete bars available"))
    key = "|".join(f"{k}={spec[k]}" for k in sorted(spec))
    return with_hash(EntryAssessment(
        assessment_id=stable_id("EA", dataset_hash(manifest), ins.instrument_id, at.isoformat(), setup_ref, key),
        dataset_id=manifest.dataset_id, dataset_hash=dataset_hash(manifest),
        instrument_id=ins.instrument_id, symbol=ins.symbol, as_of=at, setup_ref=setup_ref, status=status,
        session_open=day is not None, reference_price=ref, constraints=tuple(constraints),
        invalidation_reasons=tuple(invalid), completeness=completeness, limitations=limitations, safe_for=safe_for,
        created_at=now_utc(now)))
