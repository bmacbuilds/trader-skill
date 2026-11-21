"""S01 Chart Scanner: explainable screening on completed bars (spec 12.1 S01, 12.2 A).

For each symbol in the universe it says eligible, not eligible, partial or unavailable, and shows why: every
comparison's two values, their units, the features' formula versions, and the reason a value was unavailable.
Nothing qualifies on data that is missing, stale, incomplete or flagged as blocking; nothing is invented to fill
an empty result. A scan with nothing eligible says `no_eligible_setup=True`.
"""
from __future__ import annotations

from datetime import datetime

from traderos.contracts import (
    BarReader,
    Completeness,
    ErrorCode,
    ExchangeCalendar,
    ScanCriteria,
    ScanResult,
    ValidationError,
    record_hash,
    with_hash,
)
from traderos.contracts.intel import Eligibility, ScanCandidate
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
from traderos.strategies import conditions as cnd

MAX_UNIVERSE = 500                               # a resource bound (spec 04.3): a bigger scan is split by the caller


def scan(reader: BarReader, calendar: ExchangeCalendar, dataset_id: str, criteria: ScanCriteria, *,
         as_of: datetime | None = None, now: datetime | None = None) -> ScanResult:
    manifest = usable(reader.manifest(dataset_id))
    if criteria.interval is not manifest.interval:
        raise ValidationError(f"the criteria are for {criteria.interval.value} bars; {dataset_id} holds "
                              f"{manifest.interval.value} bars", code=ErrorCode.INVALID_INPUT)
    if not criteria.universe:
        raise ValidationError("the universe is empty", code=ErrorCode.INVALID_INPUT)
    if len(criteria.universe) > MAX_UNIVERSE:
        raise ValidationError(f"more than {MAX_UNIVERSE} symbols in one scan",
                              code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
    staleness_bound(criteria.max_staleness_bars)
    cond = cnd.parse(criteria.conditions, context=cnd.Context.SCAN)
    at = resolve_as_of(reader, dataset_id, as_of)
    expected = expected_last_end(calendar, manifest.interval, at)
    need = cnd.history_needed(cond)

    candidates = []
    for name in criteria.universe:
        ins = find_instrument(manifest, name)
        if ins is None:
            candidates.append(ScanCandidate(instrument_id=name, symbol=name, eligibility=Eligibility.UNAVAILABLE,
                                            traces=(), reasons=(ErrorCode.MISSING_DATA.value,)))
            continue
        bars = complete_bars(reader.bars(dataset_id, ins.instrument_id, as_of=at, limit=need + 5))[-need:]
        last = bars[-1] if bars else None
        reasons: list[str] = []
        if last is None:
            candidates.append(ScanCandidate(instrument_id=ins.instrument_id, symbol=ins.symbol,
                                            eligibility=Eligibility.UNAVAILABLE, traces=(),
                                            reasons=(ErrorCode.MISSING_DATA.value,)))
            continue
        ev = cnd.evaluate(cond, bars, price_unit=ins.currency)
        behind = bars_behind(calendar, manifest.interval, last.end, expected)
        if behind > criteria.max_staleness_bars - 1:
            reasons.append(ErrorCode.DATA_STALE.value)
        reasons.extend(c for c in last.quality_codes if c not in reasons)
        if any(t.passed is None for t in ev.traces):
            reasons.append(ErrorCode.MISSING_DATA.value if len(bars) < need else ErrorCode.INSUFFICIENT_EVIDENCE.value)
        if ErrorCode.DATA_STALE.value in reasons:
            eligibility = Eligibility.UNAVAILABLE      # computed and shown, but never a candidate
        elif ev.passed is True:
            eligibility = Eligibility.ELIGIBLE
        elif ev.passed is False:
            eligibility = Eligibility.NOT_ELIGIBLE
        elif any(t.passed is not None for t in ev.traces):
            eligibility = Eligibility.PARTIAL
        else:
            eligibility = Eligibility.UNAVAILABLE
        candidates.append(ScanCandidate(instrument_id=ins.instrument_id, symbol=ins.symbol, eligibility=eligibility,
                                        traces=ev.traces, reasons=tuple(dict.fromkeys(reasons)),
                                        last_bar_end=last.end, last_bar_available_at=last.available_at))

    judged = [c for c in candidates if c.eligibility in (Eligibility.ELIGIBLE, Eligibility.NOT_ELIGIBLE)]
    completeness = (Completeness.COMPLETE if len(judged) == len(candidates)
                    else Completeness.UNAVAILABLE if not judged and not any(
                        c.eligibility is Eligibility.PARTIAL for c in candidates)
                    else Completeness.PARTIAL)
    limitations, safe_for = labels(manifest)
    criteria_hash = record_hash(criteria)
    return with_hash(ScanResult(
        scan_id=stable_id("SC", dataset_hash(manifest), criteria_hash, at.isoformat()),
        dataset_id=dataset_id, dataset_hash=dataset_hash(manifest), criteria_hash=criteria_hash, as_of=at,
        provider=manifest.provider, feed=manifest.feed, candidates=tuple(candidates),
        no_eligible_setup=not any(c.eligibility is Eligibility.ELIGIBLE for c in candidates),
        completeness=completeness, limitations=limitations, safe_for=safe_for, created_at=now_utc(now)))
