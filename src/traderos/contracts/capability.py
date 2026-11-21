"""The 21-capability roadmap and the release registry's entry contract (spec 01.2, 12.1).

Every ID exists whether or not its module ships. `capabilities.json` is compiled per release; the CLI, the skill
and the Machine read the same registry, and a capability that is not `release_approved` answers
CAPABILITY_DISABLED with the gate it is waiting for.
"""
from __future__ import annotations

from enum import StrEnum

from traderos.contracts.common import CapabilityImplementation, Limitation, TestExecution, UserCapability
from traderos.contracts.records import Part, Record, contract


class CapabilityId(StrEnum):
    TRADER = "TRADER"                            # the /trader orchestrated workflow (I06)
    S01 = "S01"   # Chart Scanner
    S02 = "S02"   # News Monitor
    S03 = "S03"   # Market Pulse
    S04 = "S04"   # Earnings Analyst
    S05 = "S05"   # Congressional Tracker (HOLD)
    S06 = "S06"   # Institutional Tracker (13F)
    S07 = "S07"   # Fundamental Screener
    S08 = "S08"   # Quant Analyst
    S09 = "S09"   # Strategy Builder
    S10 = "S10"   # Backtester
    S11 = "S11"   # Time Machine
    S12 = "S12"   # Options Analyst (data gate)
    S13 = "S13"   # Entry Timer
    S14 = "S14"   # Exit Planner
    S15 = "S15"   # Budget Manager
    S16 = "S16"   # Risk Manager
    S17 = "S17"   # Order Executor: the paper ticket, sent to an Alpaca PAPER account only (ADR-012)
    S18 = "S18"   # Price Alerts
    S19 = "S19"   # Trade Journal
    S20 = "S20"   # Tax Optimizer (HOLD)
    S21 = "S21"   # Portfolio Rebalancer


NAMES: dict[CapabilityId, str] = {
    CapabilityId.TRADER: "/trader workflow",
    CapabilityId.S01: "Chart Scanner", CapabilityId.S02: "News Monitor", CapabilityId.S03: "Market Pulse",
    CapabilityId.S04: "Earnings Analyst", CapabilityId.S05: "Congressional Tracker",
    CapabilityId.S06: "Institutional Tracker", CapabilityId.S07: "Fundamental Screener",
    CapabilityId.S08: "Quant Analyst", CapabilityId.S09: "Strategy Builder", CapabilityId.S10: "Backtester",
    CapabilityId.S11: "Time Machine", CapabilityId.S12: "Options Analyst", CapabilityId.S13: "Entry Timer",
    CapabilityId.S14: "Exit Planner", CapabilityId.S15: "Budget Manager", CapabilityId.S16: "Risk Manager",
    CapabilityId.S17: "Order Executor", CapabilityId.S18: "Price Alerts", CapabilityId.S19: "Trade Journal",
    CapabilityId.S20: "Tax Optimizer", CapabilityId.S21: "Portfolio Rebalancer",
}


class ExecutionMode(StrEnum):
    TUTORIAL = "tutorial"
    RESEARCH = "research"
    HYPOTHETICAL = "hypothetical"
    PAPER = "paper"                              # contracts v1.2 (ADR-012): orders to an Alpaca PAPER account only


@contract
class TestEvidence(Part):
    __test__ = False
    test_id: str
    result: TestExecution
    artifact: str | None = None


@contract
class CapabilityEntry(Part):
    capability_id: CapabilityId
    name: str
    implementation: CapabilityImplementation
    user_status: UserCapability
    implementation_version: str | None = None
    modes: tuple[ExecutionMode, ...] = ()
    prerequisites: tuple[str, ...] = ()          # data/provider prerequisites, e.g. "csv-or-tutorial-bars"
    evidence: tuple[TestEvidence, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    next_gate: str = ""                          # what it waits for, shown with CAPABILITY_DISABLED


@contract
class CapabilityRegistry(Record):
    """The compiled per-release `capabilities.json` (spec 01.2). Read-only at runtime."""
    SCHEMA = "capabilities/1"
    release: str
    entries: tuple[CapabilityEntry, ...]
    canonical_hash: str | None = None
