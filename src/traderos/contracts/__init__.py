"""TraderOS shared contracts, version 1 (docs/contracts-v1.md). FROZEN: changes need an ADR and a migration fixture.

Import records and interfaces from here:

    from traderos.contracts import Bar, ScanResult, ErrorCode, DataError, BarReader, to_json, from_json
"""

from traderos.contracts.canonical import (
    canonical_bytes,
    decimal_from_float,
    decimal_str,
    iso_utc,
    parse_date,
    parse_decimal,
    parse_utc,
    sha256_bytes,
)
from traderos.contracts.capability import NAMES as CAPABILITY_NAMES
from traderos.contracts.capability import CapabilityEntry, CapabilityId, CapabilityRegistry, ExecutionMode, TestEvidence
from traderos.contracts.common import (
    CapabilityImplementation,
    Completeness,
    DatasetUsability,
    ExperimentOutcome,
    Failure,
    Limitation,
    MetricValue,
    SafeFor,
    Severity,
    SourceDisposition,
    TestExecution,
    ToolReceipt,
    UserCapability,
)
from traderos.contracts.errors import (
    AuthorizationError,
    CapabilityDisabled,
    DataError,
    DomainError,
    ErrorCode,
    IncompleteEvidence,
    IntegrityError,
    ResearchError,
    TraderOSError,
    ValidationError,
)
from traderos.contracts.evidence import (
    ActorRole,
    BundleEntry,
    ExportProfile,
    ImportReport,
    LedgerEntry,
    LedgerKind,
    NoTradeRecord,
    Omission,
    PortableBundleManifest,
    ResearchMemoryEvent,
    StepStatus,
    WorkflowResult,
    WorkflowStep,
)
from traderos.contracts.fundamentals import (
    CopyTargets,
    EarningsReport,
    FilingFact,
    FilingLink,
    FundamentalScreen,
    Holding,
    HoldingChange,
    HoldingsDiff,
    HoldingsReport,
    QuarterLine,
    ScreenRow,
    SkippedPosition,
    TargetWeight,
)
from traderos.contracts.intel import (
    AlertEvent,
    AlertRule,
    ConditionTrace,
    Eligibility,
    EntryAssessment,
    EntryStatus,
    ExitPlan,
    ExitScenario,
    InstrumentMove,
    MarketContext,
    RegimeReading,
    ScanCandidate,
    ScanCriteria,
    ScanResult,
)
from traderos.contracts.interfaces import BarReader, Capabilities, DataAdapter, ExchangeCalendar, Feature
from traderos.contracts.market import (
    AccessContext,
    Adjustment,
    AssetClass,
    AvailabilityPolicy,
    Bar,
    BarRequest,
    Coverage,
    DataBatch,
    DatasetManifest,
    FeedCapability,
    Instrument,
    Interval,
    OptionRight,
    ProviderCapabilities,
    QualityIssue,
    Quote,
    RightsLabel,
    SourceKind,
)
from traderos.contracts.records import (
    Part,
    Record,
    contract,
    from_dict,
    from_json,
    record_hash,
    to_dict,
    to_json,
    verify_hash,
    with_hash,
)
from traderos.contracts.research import (
    AccountAssumptions,
    AuthorType,
    ChallengeOutcome,
    ChallengeResult,
    EvidencePassport,
    ExperimentSpec,
    FeatureDefinition,
    OwnerAction,
    PassportState,
    PlanReplaySubject,
    Scenario,
    ScenarioKind,
    SplitName,
    SplitWindow,
    StrategyDraft,
    StrategyVersion,
    TimeMachineReport,
    Trial,
    TrialStatus,
    UncertaintyReport,
)
from traderos.contracts.risk import Direction, ExposureStatus, HypotheticalRiskAssessment, PaperTicket, RiskPlan

CONTRACTS_VERSION = "1"

# Every record class, by its SCHEMA: the bundle importer and the golden fixtures use this to find a parser.
RECORD_TYPES: dict[str, type[Record]] = {
    cls.SCHEMA: cls
    for cls in (
        AlertEvent,
        AlertRule,
        Bar,
        CapabilityRegistry,
        ChallengeResult,
        DataBatch,
        DatasetManifest,
        EntryAssessment,
        EvidencePassport,
        ExitPlan,
        ExperimentSpec,
        Failure,
        FeatureDefinition,
        HypotheticalRiskAssessment,
        ImportReport,
        Instrument,
        LedgerEntry,
        MarketContext,
        NoTradeRecord,
        PaperTicket,
        PortableBundleManifest,
        ProviderCapabilities,
        Quote,
        ResearchMemoryEvent,
        RiskPlan,
        ScanCriteria,
        ScanResult,
        StrategyDraft,
        StrategyVersion,
        ToolReceipt,
        Trial,
        WorkflowResult,
        TimeMachineReport,
        EarningsReport,
        HoldingsReport,
        FundamentalScreen,
        HoldingsDiff,
        CopyTargets,
    )
}

__all__ = [name for name in dir() if not name.startswith("_")]
