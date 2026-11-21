"""Hypothetical risk and the paper ticket (S15, S16, S17).

The records are calculations: none of them holds a broker account, a credential or an order id. When account
exposure cannot be verified it is UNKNOWN and the plan is refused executable eligibility (spec 12.2 C). A PaperTicket
is a document a person can read and copy; since the owner's ruling of 2026-11-20 (ADR-012) it can also be sent, after
the person's explicit yes, to their Alpaca PAPER account only (traderos.adapters.alpaca_paper), and what happened is
recorded in the journal, never in the ticket. Live trading is refused in code.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from traderos.contracts.common import Completeness, Limitation, MetricValue, SafeFor
from traderos.contracts.errors import ValidationError
from traderos.contracts.records import Record, contract


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class ExposureStatus(StrEnum):
    KNOWN = "known"                              # stated by the user as a hypothesis, never read from a broker in F1
    UNKNOWN = "unknown"


@contract
class RiskPlan(Record):
    """A user's hypothetical plan. Account values are what the user states, not a broker's.

    Since contracts v1.1 (ADR-009) the limits come from the person's policy file and the account from their account
    statement, each bound to the assessment by hash (policy_hash, account_hash). The v1.0 account fields below stay
    readable and are left empty by `traderos risk assess`; a reader must not take an empty one as zero."""
    SCHEMA = "risk-plan/1"
    OMIT_IF_NONE = frozenset({"plan_date", "limit_price", "tick_size"})   # added in v1.1 and v1.3: old hashes hold
    plan_id: str
    instrument_id: str
    symbol: str
    direction: Direction
    entry_price: Decimal
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    quantity: Decimal | None = None              # v1.0 only: since ADR-009 the size always comes from the policy
    multiplier: Decimal = Decimal(1)
    account_equity: Decimal | None = None        # user-stated, hypothetical
    settled_cash: Decimal | None = None
    reserved_cash: Decimal = Decimal(0)
    risk_per_trade: Decimal | None = None        # fraction of equity, e.g. "0.01"
    existing_exposure: Decimal | None = None     # None = unknown; never assumed zero
    whole_units: bool = True
    currency: str = "USD"
    prices_observed_at: datetime | None = None
    as_of: datetime
    # contracts v1.1 (ADR-009), additive: the upstream plan date and its --limit
    plan_date: date | None = None                # the New York trading day the plan is for (the upstream plan "date")
    limit_price: Decimal | None = None           # a limit order at this price; None = a market order
    # contracts v1.3 (ADR-009, review of #10), additive: the instrument's tick. An order price off it is refused,
    # never rounded; unstated, the U.S. equity rule applies (whole cents at $1.00 and above, 4 decimals below)
    tick_size: Decimal | None = None

    def validate(self) -> None:
        for name in ("entry_price", "multiplier"):
            if getattr(self, name) <= 0:
                raise ValidationError(f"RiskPlan: {name} must be positive")
        if self.risk_per_trade is not None and not (0 < self.risk_per_trade <= 1):
            raise ValidationError("RiskPlan: risk_per_trade is a fraction in (0, 1]")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValidationError("RiskPlan: limit_price must be positive")
        if self.tick_size is not None and self.tick_size <= 0:
            raise ValidationError("RiskPlan: tick_size must be positive")


@contract
class HypotheticalRiskAssessment(Record):
    """The four measures (spec 13.2 risk assess), each possibly unavailable, plus every refusal reason."""
    SCHEMA = "risk-assessment/1"
    OMIT_IF_NONE = frozenset({"valid_until", "policy_hash", "account_hash"})     # added in v1.1
    assessment_id: str
    plan_id: str
    plan_hash: str
    quantity: MetricValue
    planned_loss: MetricValue                    # entry to stop, times quantity and multiplier
    stressed_loss: MetricValue                   # a gap through the stop under the stated stress
    theoretical_worst_case: MetricValue          # unavailable ("undefined") for an unbounded short
    exposure_status: ExposureStatus
    input_freshness: str
    refusal_reasons: tuple[str, ...] = ()        # ErrorCode values, e.g. UNKNOWN_EXPOSURE, DATA_STALE
    ticket_eligible: bool                        # may a paper ticket DOCUMENT be drafted; never an order
    completeness: Completeness
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    created_at: datetime
    # contracts v1.1 (ADR-009), additive
    valid_until: datetime | None = None          # the verdict expires (upstream #21); None counts as expired
    policy_hash: str | None = None               # sha256 of the canonical policy the check used
    account_hash: str | None = None              # sha256 of the stated account and log; None = none stated (UNKNOWN)
    canonical_hash: str | None = None

    def validate(self) -> None:
        if self.ticket_eligible and (self.refusal_reasons or self.exposure_status is ExposureStatus.UNKNOWN):
            raise ValidationError("HypotheticalRiskAssessment: a refused or unknown-exposure plan is not eligible")
        if SafeFor.RESEARCH not in self.safe_for and SafeFor.HYPOTHETICAL_PLANNING not in self.safe_for and \
                SafeFor.TUTORIAL_ONLY not in self.safe_for and SafeFor.DISPLAY not in self.safe_for:
            raise ValidationError("HypotheticalRiskAssessment: safe_for is required")


@contract
class PaperTicket(Record):
    """S17: an order ticket. It has no broker, account or credential field: sending it to the paper account is the
    adapter's job and its outcome is a journal event (ADR-012)."""
    SCHEMA = "paper-ticket/1"
    OMIT_IF_NONE = frozenset({"target_price", "kind"})               # added in v1.1 and v1.2
    ticket_id: str
    assessment_id: str
    plan_hash: str
    symbol: str
    direction: Direction
    quantity: Decimal
    order_type: str                              # e.g. "limit"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None          # contracts v1.1 (ADR-009), additive: the take-profit leg
    time_in_force: str = "day"
    notes: tuple[str, ...] = ()
    disclaimer: str = "Hypothetical document. TraderOS did not and cannot place this order."
    created_at: datetime
    # contracts v1.2 (ADR-012), additive: "exit" for the upstream flatten ticket, which SELLS the long `direction`
    # names; None is an entry (every v1.1 ticket), which buys a long.
    kind: str | None = None
    canonical_hash: str | None = None

    def validate(self) -> None:
        if self.kind not in (None, "exit"):
            raise ValidationError("PaperTicket: kind is \"exit\" or absent (an entry)")
        if self.kind == "exit" and (self.direction is not Direction.LONG or self.stop_price is not None
                                    or self.target_price is not None):
            raise ValidationError("PaperTicket: an exit closes a long: no stop or target legs")
        if self.quantity <= 0:
            raise ValidationError("PaperTicket: quantity must be positive")
