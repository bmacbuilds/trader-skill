"""Typed failures (spec 05.3): a refusal is a stable code, never an accidental success.

Every public function either returns its typed record or raises a `TraderOSError` subclass carrying one `ErrorCode`.
The CLI and the Claude skill turn an exception into a `Failure` record (`to_failure`) so the same code reaches every
surface. Unknown data never defaults to zero or to permission.

Codes are stable strings: renaming or removing one is a contract change (ADR + migration). Adding one is allowed in a
minor contract revision; consumers must treat an unknown code as a refusal.
"""
from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    # spec 05.3, verbatim
    MISSING_DATA = "MISSING_DATA"
    DATA_STALE = "DATA_STALE"
    ENTITLEMENT_DENIED = "ENTITLEMENT_DENIED"
    DATA_RIGHTS_UNKNOWN = "DATA_RIGHTS_UNKNOWN"
    CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"
    INCOMPLETE_BAR = "INCOMPLETE_BAR"
    PIT_UNVERIFIED = "PIT_UNVERIFIED"
    INVALID_STRATEGY = "INVALID_STRATEGY"
    LOOKAHEAD_RISK = "LOOKAHEAD_RISK"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    HOLDOUT_COMPROMISED = "HOLDOUT_COMPROMISED"
    NONREPRODUCIBLE = "NONREPRODUCIBLE"
    OUTSIDE_CAPABILITY = "OUTSIDE_CAPABILITY"
    UNVERIFIED_DEPENDENCY = "UNVERIFIED_DEPENDENCY"
    INTEGRITY_MISMATCH = "INTEGRITY_MISMATCH"
    RESOURCE_BUDGET_EXCEEDED = "RESOURCE_BUDGET_EXCEEDED"
    INVALID_APPROVAL = "INVALID_APPROVAL"
    BROKER_INDETERMINATE = "BROKER_INDETERMINATE"
    UNKNOWN_EXPOSURE = "UNKNOWN_EXPOSURE"
    NO_PROTECTION = "NO_PROTECTION"
    HALTED = "HALTED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    MACHINE_CONTRACT_UNVERIFIED = "MACHINE_CONTRACT_UNVERIFIED"
    # added in contracts v1, each named elsewhere in the spec
    CAPABILITY_DISABLED = "CAPABILITY_DISABLED"      # 01.2: a feature not release_approved in capabilities.json
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"        # 11.3: an unknown or future schema_version fails safely
    INVALID_INPUT = "INVALID_INPUT"                  # 04.2: untrusted input that fails parsing, bounds or types
    PIT_LIMITED = "PIT_LIMITED"                      # 06.3: availability assigned by policy, not by the provider
    AVAILABILITY_UNVERIFIED = "AVAILABILITY_UNVERIFIED"  # 05.1: provider availability cannot be reconstructed
    SYNTHETIC_DATA = "SYNTHETIC_DATA"                # 06.1: tutorial data; never real-market evidence
    UNOFFICIAL_DATA = "UNOFFICIAL_DATA"              # v1.4, ADR-014: a free, unofficial source (`data fetch`)


class TraderOSError(Exception):
    """Base of every typed refusal. `code` is the contract; `message` is for people."""

    default_code: ErrorCode = ErrorCode.INVALID_INPUT

    def __init__(self, message: str, *, code: ErrorCode | None = None, limitations: tuple[str, ...] = (),
                 details: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = ErrorCode(code or self.default_code)
        self.message = message
        self.limitations = tuple(limitations)
        self.details = dict(details or {})

    def to_failure(self):  # -> traderos.contracts.common.Failure (imported late: it imports this module)
        from traderos.contracts.common import Failure
        return Failure(code=self.code, message=self.message, limitations=self.limitations,
                       details=self.details or None)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code.value}: {self.message})"


class ValidationError(TraderOSError):
    default_code = ErrorCode.INVALID_INPUT


class DataError(TraderOSError):
    default_code = ErrorCode.MISSING_DATA


class DomainError(TraderOSError):
    default_code = ErrorCode.OUTSIDE_CAPABILITY


class AuthorizationError(TraderOSError):
    default_code = ErrorCode.NOT_AUTHORIZED


class ResearchError(TraderOSError):
    default_code = ErrorCode.NONREPRODUCIBLE


class IncompleteEvidence(TraderOSError):
    default_code = ErrorCode.INSUFFICIENT_EVIDENCE


class IntegrityError(TraderOSError):
    default_code = ErrorCode.INTEGRITY_MISMATCH


class CapabilityDisabled(TraderOSError):
    default_code = ErrorCode.CAPABILITY_DISABLED
