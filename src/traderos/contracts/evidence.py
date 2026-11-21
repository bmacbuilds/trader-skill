"""Memory, no-trade decisions, workflows, the portfolio ledger and portable bundles (spec 05.2, 10 I04-I07, 11).

All append-only: a change is a new event, never an edit. Counterfactual analysis of a no-trade decision is labelled
as such and never mixed with realized results.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from traderos.contracts.common import Completeness, Limitation
from traderos.contracts.errors import ValidationError
from traderos.contracts.records import Part, Record, contract


class ActorRole(StrEnum):
    HUMAN = "human"
    RESEARCHER_AI = "researcher_ai"
    CHALLENGER_AI = "challenger_ai"
    SYSTEM = "system"


@contract
class ResearchMemoryEvent(Record):
    """One append-only fact in the user's private research memory (I04). Never an LLM's recollection."""
    SCHEMA = "memory-event/1"
    event_id: str
    actor_role: ActorRole
    kind: str                                    # e.g. "strategy.approved", "experiment.completed", "no_trade"
    subject_refs: tuple[str, ...]                # record ids this event is about
    outcome_codes: tuple[str, ...] = ()
    summary: str                                 # searchable words; facts only
    payload_hash: str                            # canonical hash of the record the event points at
    previous_event_hash: str | None = None
    at: datetime
    canonical_hash: str | None = None


@contract
class NoTradeRecord(Record):
    """I05: a candidate deliberately not taken, and why. Never counted as executed P&L."""
    SCHEMA = "no-trade/1"
    record_id: str
    candidate_ref: str
    strategy_version_id: str | None = None
    decided_at: datetime
    decision_authority: str                      # who decided: a person, or the policy by name and version
    evidence_refs: tuple[str, ...]
    rejection_codes: tuple[str, ...]             # e.g. UNKNOWN_EXPOSURE, INSUFFICIENT_EVIDENCE, "too_expensive"
    rationale: str
    counterfactual_ref: str | None = None
    canonical_hash: str | None = None

    def validate(self) -> None:
        if not self.rejection_codes:
            raise ValidationError("NoTradeRecord: at least one rejection code")


class StepStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    SKIPPED = "skipped"                          # optional step, skipped with a recorded reason
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@contract
class WorkflowStep(Part):
    name: str                                    # e.g. "market_pulse", "scan", "draft", "review", "experiment"
    status: StepStatus
    receipt_ids: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    input_hashes: tuple[str, ...] = ()           # upstream outputs this step checked before running
    reason: str = ""

    def validate(self) -> None:
        if self.status is not StepStatus.COMPLETED and not self.reason:
            raise ValidationError(f"WorkflowStep {self.name}: a {self.status} step says why")


@contract
class WorkflowResult(Record):
    """I06: one workflow_id across the whole flow; partial is explicit, nothing is invented."""
    SCHEMA = "workflow/1"
    workflow_id: str
    intent: str
    steps: tuple[WorkflowStep, ...]
    evidence_refs: tuple[str, ...] = ()
    final_ref: str | None = None
    completeness: Completeness
    limitations: tuple[Limitation, ...] = ()
    started_at: datetime
    ended_at: datetime | None = None
    canonical_hash: str | None = None


class LedgerKind(StrEnum):
    DEPOSIT = "deposit"
    FILL = "fill"
    FEE = "fee"
    DIVIDEND = "dividend"
    RESERVE = "reserve"
    RELEASE = "release"
    SETTLE = "settle"
    MARK = "mark"


@contract
class LedgerEntry(Record):
    """One immutable line of the simulated portfolio ledger (spec 09.3). Money is Decimal; signs are explicit:
    cash_delta > 0 adds cash, quantity_delta > 0 adds units."""
    SCHEMA = "ledger/1"
    entry_id: str
    run_id: str
    sequence: int
    at: datetime
    kind: LedgerKind
    instrument_id: str | None = None
    quantity_delta: Decimal = Decimal(0)
    price: Decimal | None = None
    cash_delta: Decimal = Decimal(0)
    settled_cash_delta: Decimal = Decimal(0)
    reserved_delta: Decimal = Decimal(0)
    currency: str = "USD"
    mark_source: str | None = None
    causal_ref: str                              # the event/order that caused it
    note: str = ""


class ExportProfile(StrEnum):
    FULL_PRIVATE = "full_private"                # a private backup: everything the rights allow
    SHARE_REDACTED = "share_redacted"            # a shareable Passport copy: deterministic redaction


@contract
class BundleEntry(Part):
    path: str                                    # relative, allowlisted; no "..", no absolute paths
    record_type: str                             # the record's SCHEMA, e.g. "passport/1"
    record_id: str
    sha256: str                                  # the file's exact bytes

    def validate(self) -> None:
        parts = self.path.split("/")
        if self.path.startswith("/") or "\\" in self.path or ".." in parts or "" in parts or ":" in self.path:
            raise ValidationError(f"BundleEntry: unsafe path {self.path!r}")


@contract
class Omission(Part):
    ref: str
    reason: str                                  # e.g. "provider raw data not exportable"


@contract
class PortableBundleManifest(Record):
    """I07: the `.traderos` bundle's manifest. Import previews every hash, version and type first."""
    SCHEMA = "bundle/1"
    bundle_id: str
    package_release: str
    export_profile: ExportProfile
    entries: tuple[BundleEntry, ...]
    omissions: tuple[Omission, ...] = ()
    rights_notes: tuple[str, ...] = ()
    created_at: datetime | None = None           # ADR-013: only when the exporter is given one; never the clock
    signature: str | None = None
    canonical_hash: str | None = None


@contract
class ImportReport(Record):
    """I07: what importing a bundle would do (preview) or did (apply). Nothing existing is ever overwritten."""
    SCHEMA = "import-report/1"
    bundle_id: str
    mode: str                                    # "preview" | "apply"
    additions: tuple[str, ...] = ()              # record ids that would be / were added
    duplicates: tuple[str, ...] = ()             # already present with the same hash: skipped
    conflicts: tuple[str, ...] = ()              # same id, different hash: refused, never overwritten
    excluded: tuple[Omission, ...] = ()
    missing_sources: tuple[str, ...] = ()        # e.g. raw data the export omitted; rerun needs reconnection
    hash_failures: tuple[str, ...] = ()
    migrations: tuple[str, ...] = ()
    ok: bool
    created_at: datetime

    def validate(self) -> None:
        if self.mode not in ("preview", "apply"):
            raise ValidationError("ImportReport: mode is preview or apply")
        if self.ok and (self.conflicts or self.hash_failures):
            raise ValidationError("ImportReport: conflicts or hash failures are never ok")
