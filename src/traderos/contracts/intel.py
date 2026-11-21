"""Market Intelligence records: scan, market context, entry, exit and alerts (S01, S03, S13, S14, S18).

These are the records part B returns. Every result says which data it read, when that data became available,
and why each symbol did or did not qualify (spec 12.2 A). Insufficient data is said, never filled with artificial
candidates: a scan with nothing eligible returns `no_eligible_setup=True`, not an invented match.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from traderos.contracts.common import Completeness, Limitation, MetricValue, SafeFor
from traderos.contracts.errors import ValidationError
from traderos.contracts.market import Interval
from traderos.contracts.records import Part, Record, contract
from traderos.contracts.risk import Direction


class Eligibility(StrEnum):
    ELIGIBLE = "eligible"
    PARTIAL = "partial"                          # some conditions could not be evaluated (data), see reasons
    NOT_ELIGIBLE = "not_eligible"                # evaluated, and it does not qualify
    UNAVAILABLE = "unavailable"                  # could not be evaluated at all


@contract
class ScanCriteria(Record):
    """What to look for. `conditions` uses the strategy/1 condition grammar (spec 07.2: all/any, above/below...)."""
    SCHEMA = "scan-criteria/1"
    criteria_id: str
    name: str
    interval: Interval
    universe: tuple[str, ...]                    # symbols or instrument_ids, as the dataset names them
    conditions: dict[str, Any]
    max_staleness_bars: int = 1                  # how old the newest completed bar may be, in bars


@contract
class ConditionTrace(Part):
    """Why one condition passed or failed for one symbol: the inspectable part of 'why did this qualify'."""
    condition: str                               # canonical text of the condition, e.g. "close > sma(200)"
    passed: bool | None                          # None: could not be evaluated
    left: MetricValue
    right: MetricValue
    feature_refs: tuple[str, ...] = ()           # FeatureDefinition formula versions used
    reason: str = ""


@contract
class ScanCandidate(Part):
    instrument_id: str
    symbol: str
    eligibility: Eligibility
    traces: tuple[ConditionTrace, ...]
    reasons: tuple[str, ...] = ()                # ErrorCode values or quality codes behind partial/unavailable
    last_bar_end: datetime | None = None
    last_bar_available_at: datetime | None = None


@contract
class ScanResult(Record):
    SCHEMA = "scan/1"
    scan_id: str
    dataset_id: str
    dataset_hash: str
    criteria_hash: str
    as_of: datetime                              # only bars available at or before this were read
    provider: str
    feed: str | None = None
    candidates: tuple[ScanCandidate, ...]
    no_eligible_setup: bool
    completeness: Completeness
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    created_at: datetime
    canonical_hash: str | None = None

    def validate(self) -> None:
        eligible = any(c.eligibility is Eligibility.ELIGIBLE for c in self.candidates)
        if self.no_eligible_setup == eligible:
            raise ValidationError("ScanResult: no_eligible_setup must be true exactly when nothing is eligible")


@contract
class InstrumentMove(Part):
    instrument_id: str
    symbol: str
    last_close: MetricValue
    change_pct: MetricValue                      # over `lookback_bars`
    lookback_bars: int
    last_bar_end: datetime | None = None
    stale: bool = False


@contract
class RegimeReading(Part):
    """Only features observable at `as_of` (spec 08.6 Q03). `unknown` is a valid answer."""
    label: str                                   # e.g. "uptrend", "downtrend", "range", "unknown"
    inputs: dict[str, MetricValue]
    method: str                                  # versioned, e.g. "sma_slope/1"


@contract
class MarketContext(Record):
    """Market Pulse (S03): timestamped, feed-aware context; breadth only when coverage truly allows it."""
    SCHEMA = "market-context/1"
    context_id: str
    dataset_id: str
    dataset_hash: str
    as_of: datetime
    provider: str
    feed: str | None = None
    moves: tuple[InstrumentMove, ...]
    breadth: MetricValue                         # unavailable with a reason when coverage is insufficient
    regime: RegimeReading
    completeness: Completeness
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    created_at: datetime
    canonical_hash: str | None = None


class EntryStatus(StrEnum):
    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"
    UNAVAILABLE = "unavailable"


@contract
class EntryAssessment(Record):
    """Entry Timer (S13): is a setup session-eligible now? Research only in F1; never an order."""
    SCHEMA = "entry/1"
    assessment_id: str
    dataset_id: str
    dataset_hash: str
    instrument_id: str
    symbol: str
    as_of: datetime
    setup_ref: str | None = None                 # the ScanResult or strategy that proposed it
    status: EntryStatus
    session_open: bool | None = None
    reference_price: MetricValue
    constraints: tuple[str, ...] = ()            # e.g. "opening range incomplete", "early close 13:00 ET"
    invalidation_reasons: tuple[str, ...] = ()
    completeness: Completeness
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    created_at: datetime
    canonical_hash: str | None = None


@contract
class ExitScenario(Part):
    name: str                                    # e.g. "stop", "target", "time exit", "gap through stop"
    trigger: str
    price: MetricValue
    pnl_per_unit: MetricValue
    assumptions: tuple[str, ...] = ()


@contract
class ExitPlan(Record):
    """Exit Planner (S14): hypothetical exit scenarios and their risks. No order is ever submitted."""
    SCHEMA = "exit-plan/1"
    plan_id: str
    dataset_id: str
    dataset_hash: str
    instrument_id: str
    symbol: str
    as_of: datetime
    direction: Direction
    entry_price: Decimal
    scenarios: tuple[ExitScenario, ...]
    risks: tuple[str, ...] = ()
    completeness: Completeness
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    created_at: datetime
    canonical_hash: str | None = None


@contract
class AlertRule(Record):
    """Price Alerts (S18): what to watch, how often it is sampled, and what happens to a missed alert."""
    SCHEMA = "alert-rule/1"
    rule_id: str
    instrument_id: str
    symbol: str
    interval: Interval                           # the bars the conditions are evaluated over
    conditions: dict[str, Any]                   # strategy/1 condition grammar
    sampling_seconds: int
    max_staleness_seconds: int
    missed_policy: str                           # e.g. "report_missed": a missed window is reported, never hidden
    active: bool = True
    created_at: datetime


@contract
class AlertEvent(Record):
    SCHEMA = "alert-event/1"
    event_id: str
    rule_id: str
    dataset_id: str
    dataset_hash: str
    provider: str
    evaluated_at: datetime
    previous_evaluated_at: datetime | None = None
    data_observed_at: datetime | None = None
    triggered: bool | None                       # None: could not be evaluated (stale or missing data)
    missed: bool = False                         # a window since previous_evaluated_at went unevaluated
    staleness_seconds: int | None = None
    reasons: tuple[str, ...] = ()
    traces: tuple[ConditionTrace, ...] = ()
    completeness: Completeness
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    canonical_hash: str | None = None

    def validate(self) -> None:
        if self.triggered is None and self.completeness is Completeness.COMPLETE:
            raise ValidationError("AlertEvent: an unevaluated rule is not a complete result")
