"""S18 Price Alerts: one evaluation of one rule at one instant (spec 12.1 S18).

Free has no always-on service, so this never claims one: a scheduler the user runs calls it every
`sampling_seconds`, and each call says honestly what it saw.

* **Only during a running session.** Outside one, the rule is not evaluated and the event says so.
* **Fresh data or no answer.** If the newest complete bar ended more than `max_staleness_seconds` before the
  evaluation, `triggered` is None with DATA_STALE, never False.
* **Missed windows are reported, never hidden** (`missed_policy: report_missed`): when the previous evaluation was
  more than two sampling periods ago, the event is marked missed and says how long the gap was.
"""
from __future__ import annotations

from datetime import datetime

from traderos.contracts import (
    BarReader,
    Completeness,
    DataError,
    ErrorCode,
    ExchangeCalendar,
    ValidationError,
    record_hash,
    with_hash,
)
from traderos.contracts.intel import AlertEvent, AlertRule
from traderos.intelligence._common import (
    aware,
    complete_bars,
    dataset_hash,
    labels,
    session_seconds,
    stable_id,
    usable,
)
from traderos.strategies import conditions as cnd

MISSED_POLICIES = ("report_missed",)


def evaluate_alert(reader: BarReader, calendar: ExchangeCalendar, dataset_id: str, rule: AlertRule, *,
                   at: datetime, previous_at: datetime | None = None) -> AlertEvent:
    at = aware(at, "at")
    if rule.sampling_seconds < 1 or rule.max_staleness_seconds < 1:
        raise ValidationError("sampling_seconds and max_staleness_seconds must be at least 1",
                              code=ErrorCode.INVALID_INPUT)
    if rule.missed_policy not in MISSED_POLICIES:
        raise ValidationError(f"missed_policy {rule.missed_policy!r} is not supported; use report_missed",
                              code=ErrorCode.INVALID_INPUT)
    cond = cnd.parse(rule.conditions, context=cnd.Context.ALERT)
    manifest = usable(reader.manifest(dataset_id))
    if rule.interval is not manifest.interval:
        raise ValidationError(f"the rule is for {rule.interval.value} bars; {dataset_id} holds "
                              f"{manifest.interval.value} bars", code=ErrorCode.INVALID_INPUT)
    limitations, safe_for = labels(manifest)
    # A ticker is not identity: the rule names an instrument_id, and only that instrument is read.
    ins = next((i for i in manifest.instruments if i.instrument_id == rule.instrument_id), None)
    if ins is None:
        raise DataError(f"{rule.instrument_id} ({rule.symbol}) is not in {dataset_id}", code=ErrorCode.MISSING_DATA)
    if ins.symbol != rule.symbol:
        raise ValidationError(f"the rule names {rule.instrument_id} as {rule.symbol}; {dataset_id} calls it "
                              f"{ins.symbol}", code=ErrorCode.INVALID_INPUT)

    reasons: list[str] = []
    missed = False
    if previous_at is not None:
        previous_at = aware(previous_at, "previous_at")
        if previous_at > at:
            raise ValidationError("previous_at is after at", code=ErrorCode.INVALID_INPUT)
        # Only session time can be missed: a weekend or a night between two checks is not a missed window.
        gap = session_seconds(calendar, previous_at, at)
        if gap > 2 * rule.sampling_seconds:
            missed = True
            reasons.append(f"MISSED_WINDOW: {int(gap)} s of session time since the previous evaluation; the rule "
                           f"samples every {rule.sampling_seconds} s")

    def event(triggered, *, observed=None, staleness=None, traces=()) -> AlertEvent:
        if triggered is not None:
            completeness = Completeness.COMPLETE
        elif any(t.passed is not None for t in traces):
            completeness = Completeness.PARTIAL
        else:
            completeness = Completeness.UNAVAILABLE
        return with_hash(AlertEvent(
            # The rule's content is part of the id: a changed threshold, or a rule switched off, is a different
            # evaluation of the same instant, never an INTEGRITY_MISMATCH against the earlier one.
            event_id=stable_id("AE", rule.rule_id, record_hash(rule), dataset_hash(manifest), at.isoformat(),
                               previous_at.isoformat() if previous_at else ""),
            rule_id=rule.rule_id, dataset_id=dataset_id,
            dataset_hash=dataset_hash(manifest), provider=manifest.provider, evaluated_at=at,
            previous_evaluated_at=previous_at, data_observed_at=observed, triggered=triggered, missed=missed,
            staleness_seconds=staleness, reasons=tuple(reasons), traces=tuple(traces), completeness=completeness,
            limitations=limitations, safe_for=safe_for))

    if not rule.active:
        reasons.append("the rule is not active")
        return event(None)
    if calendar.session_of(at) is None:
        reasons.append("outside a running session: not evaluated")
        return event(None)
    bars = complete_bars(reader.bars(dataset_id, ins.instrument_id, as_of=at, limit=cnd.history_needed(cond) + 2))
    if not bars:
        reasons.append(f"{ErrorCode.MISSING_DATA.value}: no complete bars available")
        return event(None)
    last = bars[-1]
    staleness = int((at - last.end).total_seconds())
    ev = cnd.evaluate(cond, bars, price_unit=ins.currency)
    if staleness > rule.max_staleness_seconds:
        reasons.append(f"{ErrorCode.DATA_STALE.value}: the newest complete bar ended {staleness} s ago; the rule "
                       f"allows {rule.max_staleness_seconds} s")
        return event(None, observed=last.end, staleness=staleness, traces=ev.traces)
    if ev.passed is None:
        reasons.append(f"{ErrorCode.INSUFFICIENT_EVIDENCE.value}: a condition could not be evaluated")
    return event(ev.passed, observed=last.end, staleness=staleness, traces=ev.traces)
