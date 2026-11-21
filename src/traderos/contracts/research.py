"""Features, strategies, experiments, trials, challenges and the Evidence Passport (spec 05.2, 07, 08, 09, 10).

A draft is editable; an approved StrategyVersion is immutable and a new rule is a new revision. An ExperimentSpec is
frozen (its spec_hash stored) before the first calculation, and every attempted trial is recorded, failures
included. Python decides verdicts; AI text is labelled interpretation and never sets a state.
"""
from __future__ import annotations

from dataclasses import field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any

from traderos.contracts.common import ExperimentOutcome, Limitation, MetricValue
from traderos.contracts.errors import ValidationError
from traderos.contracts.market import Interval
from traderos.contracts.records import Part, Record, contract
from traderos.contracts.risk import Direction


@contract
class FeatureDefinition(Record):
    """A versioned pure feature (spec 07.1). `params` values are ints or decimal strings."""
    SCHEMA = "feature/1"
    feature_id: str                              # e.g. "sma"
    formula_version: str                         # e.g. "sma/1"; a formula change is a new version
    params: dict[str, int | str]
    inputs: tuple[str, ...]                      # bar fields read, e.g. ("close",)
    units: str
    lookback: int                                # bars of history the formula reads
    min_history: int                             # bars before the first usable value
    missing_policy: str                          # e.g. "unavailable": a missing input makes the value unavailable
    availability_rule: str = "bar_available_at"  # a value is usable when its last input bar is available


class AuthorType(StrEnum):
    HUMAN = "human"
    AI = "ai"                                    # a Researcher draft: untrusted until a person approves it
    IMPORT = "import"
    PACK = "pack"


@contract
class OwnerAction(Part):
    """The person's explicit act that approved something. Never text an AI controls (spec 04.2 Approval)."""
    actor: str
    method: str                                  # e.g. "cli_confirm", "skill_confirm"
    at: datetime
    statement: str                               # what they were shown and confirmed


@contract
class StrategyDraft(Record):
    """Natural language or a declarative rulebook turned into a typed, validated, still-editable draft."""
    SCHEMA = "strategy-draft/1"
    draft_id: str
    name: str
    hypothesis: str
    rules: dict[str, Any]                        # the strategy/1 document (spec 07.2), validated by parse_strategy
    author_type: AuthorType
    source_pack: str | None = None
    parent_version_id: str | None = None
    semantic_hash: str                           # canonical hash of `rules` alone (no timestamps)
    created_at: datetime


@contract
class StrategyVersion(Record):
    """An approved, immutable strategy revision."""
    SCHEMA = "strategy-version/1"
    version_id: str
    strategy_id: str                             # the family every revision of this strategy shares
    revision: int
    draft_id: str
    name: str
    hypothesis: str
    rules: dict[str, Any]
    semantic_hash: str
    parent_version_id: str | None = None
    approved_by: OwnerAction
    canonical_hash: str | None = None

    def validate(self) -> None:
        if self.revision < 1:
            raise ValidationError("StrategyVersion: revision starts at 1")


class SplitName(StrEnum):
    DEVELOPMENT = "development"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


@contract
class SplitWindow(Part):
    name: SplitName
    start: date
    end: date

    def validate(self) -> None:
        if self.end < self.start:
            raise ValidationError(f"split {self.name}: end before start")


@contract
class AccountAssumptions(Part):
    """Simulated account terms (spec 09.2). Research assumptions, not broker facts."""
    starting_cash: Decimal
    currency: str = "USD"
    account_type: str = "cash"                   # "cash" | "margin"
    settlement_days: int = 1                     # U.S. T+1 where applicable
    fractional_shares: bool = False


@contract
class ExperimentSpec(Record):
    """Frozen before the first calculation (spec 08.3). `spec_hash` is its canonical hash."""
    SCHEMA = "experiment-spec/1"
    experiment_id: str
    research_family_id: str
    hypothesis: str
    strategy_version_id: str
    strategy_semantic_hash: str
    dataset_id: str
    dataset_hash: str
    universe: tuple[str, ...]                    # instrument_ids
    universe_as_of: date | None = None
    interval: Interval
    calendar_id: str
    calendar_version: str
    splits: tuple[SplitWindow, ...]
    embargo_bars: int = 0
    account: AccountAssumptions
    fill_model_id: str                           # e.g. "next_open_conservative/1"
    cost_model_id: str                           # e.g. "bps_spread_fee/1"
    cost_params: dict[str, str]
    search_space: dict[str, Any]                 # declared parameter grid; empty = the approved rules only
    trial_budget: int
    selection_rule: str
    test_family: str
    seed: int
    challenge_policy_id: str                     # e.g. "fs/1"
    verdict_policy_id: str
    created_by: str
    created_at: datetime
    canonical_hash: str | None = None            # the frozen spec hash

    def validate(self) -> None:
        if self.trial_budget < 1:
            raise ValidationError("ExperimentSpec: trial_budget must be at least 1")
        names = [s.name for s in self.splits]
        if len(names) != len(set(names)):
            raise ValidationError("ExperimentSpec: each split appears once")
        ordered = sorted(self.splits, key=lambda s: s.start)
        for a, b in pairwise(ordered):
            if b.start <= a.end:
                raise ValidationError("ExperimentSpec: splits overlap; they must be chronological")
        if SplitName.HOLDOUT in names and ordered[-1].name is not SplitName.HOLDOUT:
            raise ValidationError("ExperimentSpec: the holdout must be the last window")


class TrialStatus(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"                            # evidence of a failed run, never a zero return
    CANCELED = "canceled"


@contract
class Trial(Record):
    """Every attempted trial, kept whatever its result (spec 05.2)."""
    SCHEMA = "trial/1"
    trial_id: str
    experiment_id: str
    ordinal: int
    parameters: dict[str, Any]
    split: SplitName
    status: TrialStatus
    error_code: str | None = None
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    started_at: datetime | None = None
    ended_at: datetime | None = None
    selected: bool = False
    selection_trace: str = ""
    verification_replay: bool = False            # an exact rerun: never a new statistical claim (spec 08.4)

    def validate(self) -> None:
        if self.status is TrialStatus.FAILED and not self.error_code:
            raise ValidationError("Trial: a failed trial records its error_code")


class ChallengeOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    NOT_PERFORMED_DUE_TO_DATA = "not_performed_due_to_data"
    ERROR = "error"


@contract
class ChallengeResult(Record):
    """One falsification test's result (spec 10 I03). A required test that cannot run never defaults to pass."""
    SCHEMA = "challenge/1"
    result_id: str
    challenge_id: str                            # FS01..FS10
    registry_version: str
    experiment_id: str
    hypothesis: str
    scope: str
    outcome: ChallengeOutcome
    finding: str
    adverse_scenario: str = ""
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    input_ids: tuple[str, ...] = ()
    reference_ids: tuple[str, ...] = ()
    created_at: datetime
    canonical_hash: str | None = None


class PassportState(StrEnum):
    DRAFT = "draft"
    REGISTERED = "registered"
    TESTED = "tested"
    CHALLENGED = "challenged"
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CANDIDATE_FOR_PAPER = "candidate_for_paper"


@contract
class UncertaintyReport(Part):
    """I08: four separate dimensions, never one green badge."""
    data_quality: str                            # e.g. "verified", "limited: PIT_LIMITED", "synthetic tutorial"
    test_execution: str                          # e.g. "8 of 10 challenges ran; FS05 not performed: data"
    strategy_merit: ExperimentOutcome
    operational_freshness: str = "not applicable"


@contract
class EvidencePassport(Record):
    """The portable dossier of one strategy's research (spec 10 I01). Revisions append; nothing is rewritten."""
    SCHEMA = "passport/1"
    passport_id: str
    revision: int
    previous_revision_hash: str | None = None
    state: PassportState
    strategy_version_id: str
    strategy_semantic_hash: str
    hypothesis: str
    rules: dict[str, Any]
    experiment_ids: tuple[str, ...]
    experiment_spec_hashes: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    dataset_hashes: tuple[str, ...]
    data_rights: tuple[str, ...]
    assumptions: dict[str, str]
    trial_ids: tuple[str, ...]
    trials_attempted: int
    metrics: dict[str, MetricValue]
    baselines: dict[str, dict[str, MetricValue]]
    challenge_result_ids: tuple[str, ...]
    uncertainty: UncertaintyReport
    limitations: tuple[Limitation, ...] = ()
    reproduce_command: str
    generated_at: datetime
    canonical_hash: str | None = None

    def validate(self) -> None:
        if self.revision < 1:
            raise ValidationError("EvidencePassport: revision starts at 1")
        if self.revision > 1 and not self.previous_revision_hash:
            raise ValidationError("EvidencePassport: a later revision names the one before it")


class ScenarioKind(StrEnum):
    OBSERVED = "observed"                        # what the strategy did on the recorded history
    RESAMPLED = "resampled"                      # generated: dependent (block) resampling of observed returns
    STRESS = "stress"                            # generated: a deliberate, stated shock re-run through the simulator


@contract
class Scenario(Part):
    """One Time Machine scenario. Generated scenarios say how they were generated; none is a forecast."""
    name: str
    kind: ScenarioKind
    method: str
    assumptions: tuple[str, ...] = ()
    paths: int = 1
    metrics: dict[str, MetricValue] = field(default_factory=dict)


@contract
class PlanReplaySubject(Part):
    """What a plan replay is about (ADR-010): one planned intraday trade, replayed against a stored 5-minute dataset.
    The prices are the plan's. Nothing here is an order, and nothing here can become one."""
    symbol: str
    instrument_id: str
    direction: Direction
    session: date                                # the plan's New York session
    entry: Decimal                               # the entry bar's close: the signal, not the fill
    stop: Decimal
    target: Decimal
    entry_bar_end: datetime                      # when the entry bar closed
    replay_until: datetime                       # each replay ends here (the plan's flat_by, or the close)
    setup: str
    stop_rule: str
    opening_range_minutes: int
    plan_hash: str                               # SHA-256 of the normalized plan
    as_of: datetime                              # the data was read as of this instant (BarReader as_of)
    fill_model_id: str                           # e.g. "next_open_bracket/1"
    cost_bps_per_side: Decimal
    code_version: str                            # package version and the replay module's source hash

    def validate(self) -> None:
        long = self.direction is Direction.LONG
        if not ((self.stop < self.entry < self.target) if long else (self.target < self.entry < self.stop)):
            raise ValidationError("PlanReplaySubject: a long needs stop < entry < target, a short the mirror")
        if self.replay_until <= self.entry_bar_end:
            raise ValidationError("PlanReplaySubject: the replay must end after the entry bar")


@contract
class TimeMachineReport(Record):
    """S11 Time Machine (spec 08.6 Q09, 12.1): observed replay, dependent resampling and deliberate stress, kept
    apart. Added in contracts v1.1 (additive; ADR-006). Its subject is a strategy version, or (additive, ADR-010) a
    planned intraday trade in `plan_replay`; exactly one of the two."""
    SCHEMA = "timemachine/1"
    report_id: str
    strategy_version_id: str | None = None
    strategy_semantic_hash: str | None = None
    plan_replay: PlanReplaySubject | None = None
    dataset_id: str
    dataset_hash: str
    window_start: date
    window_end: date
    horizon_sessions: int
    seed: int
    scenarios: tuple[Scenario, ...]
    limitations: tuple[Limitation, ...] = ()
    created_at: datetime
    canonical_hash: str | None = None

    def validate(self) -> None:
        kinds = {s.kind for s in self.scenarios}
        if ScenarioKind.OBSERVED not in kinds:
            raise ValidationError("TimeMachineReport: the observed history is always reported first-hand")
        strategy = (self.strategy_version_id, self.strategy_semantic_hash)
        if self.plan_replay is None and not all(strategy):
            raise ValidationError("TimeMachineReport: name the strategy version and its hash, or a plan_replay")
        if self.plan_replay is not None and any(s is not None for s in strategy):
            raise ValidationError("TimeMachineReport: one subject only, a strategy version or a plan_replay")
