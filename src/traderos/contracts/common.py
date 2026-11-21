"""Status vocabularies (spec 00.3) and the small parts every record shares.

Never collapse distinct concepts: a capability's engineering status, what a user may see, a dataset's usability, an
experiment's outcome, a source file's disposition and a test's execution are six separate axes, each its own enum.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from traderos.contracts.errors import ErrorCode
from traderos.contracts.records import Part, Record, contract


# ---- spec 00.3, one enum per axis -------------------------------------------------------------------------------
class CapabilityImplementation(StrEnum):
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    TEST_PASSING = "test_passing"
    INDEPENDENTLY_VERIFIED = "independently_verified"
    RELEASE_APPROVED = "release_approved"        # the only state advertised as enabled
    DEFERRED = "deferred"
    REJECTED = "rejected"


class UserCapability(StrEnum):
    VERIFIED = "verified"
    EXPERIMENTAL = "experimental"
    DEFERRED = "deferred"
    UNAVAILABLE = "unavailable"


class DatasetUsability(StrEnum):
    VERIFIED = "verified"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


class ExperimentOutcome(StrEnum):
    """An evidence assessment; never paper or live permission, advice or a promise of return."""
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CANDIDATE_FOR_PAPER = "candidate_for_paper"


class SourceDisposition(StrEnum):
    UNREVIEWED = "unreviewed"
    RETAIN = "retain"
    IMPROVE = "improve"
    REBUILD = "rebuild"
    SUPERSEDE = "supersede"
    DISCARD = "discard"
    HOLD = "hold"


class TestExecution(StrEnum):
    __test__ = False                             # not a pytest class
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED_WITH_REASON = "skipped_with_reason"
    NOT_RUN = "not_run"                          # never counts as passed


# ---- partial results (spec 05.3: completeness, limitations, safe_for) --------------------------------------------
class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class SafeFor(StrEnum):
    """What a result may be used for. There is no value for trading: Free is research and hypothetical planning."""
    DISPLAY = "display"
    RESEARCH = "research"
    HYPOTHETICAL_PLANNING = "hypothetical_planning"
    TUTORIAL_ONLY = "tutorial_only"              # computed on synthetic data; never market evidence


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"                        # cannot be overridden by a casual accept (spec 06.4)


@contract
class Limitation(Part):
    """One stated limit on a result: a stable code and words for a person."""
    code: str
    detail: str
    severity: Severity = Severity.WARNING


@contract
class MetricValue(Part):
    """A number that may be unavailable. Unavailable carries a reason; it is never stored as zero (spec 09.4)."""
    value: Decimal | None
    unit: str
    unavailable_reason: str | None = None

    def validate(self) -> None:
        from traderos.contracts.errors import ValidationError
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValidationError("MetricValue: exactly one of value and unavailable_reason")


@contract
class Failure(Record):
    """A typed refusal on the wire (TraderOSError.to_failure). `code` is an ErrorCode value."""
    SCHEMA = "failure/1"
    code: ErrorCode
    message: str
    limitations: tuple[str, ...] = ()
    details: dict[str, str] | None = None


@contract
class ToolReceipt(Record):
    """Proof a tool really ran: what was asked, what came back, by which core version (spec 13.3, I06)."""
    SCHEMA = "receipt/1"
    receipt_id: str
    tool: str
    args_hash: str
    result_hash: str
    core_version: str
    at: datetime
