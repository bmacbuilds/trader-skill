"""Instruments, market observations and datasets (spec 05.2, 06).

FOUR TIMES (spec 05.1): `observed_at` is when the market event happened (for a bar, its `end`), `received_at` when
we ingested it, `available_at` the first moment it was usable, `recorded_at` when we persisted it. A bar's
`available_at` can be later than its `end` and never earlier. When a provider's availability cannot be
reconstructed the dataset says PIT_LIMITED or AVAILABILITY_UNVERIFIED rather than inventing it.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from traderos.contracts.common import DatasetUsability, Limitation, Severity
from traderos.contracts.errors import ErrorCode, ValidationError
from traderos.contracts.records import Part, Record, contract


class AssetClass(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    OPTION = "option"


class OptionRight(StrEnum):
    CALL = "call"
    PUT = "put"


class Interval(StrEnum):
    """Bar intervals contracts v1 knows. A dataset has exactly one."""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"


class Adjustment(StrEnum):
    RAW = "raw"                                  # executable prices as traded
    SPLIT = "split"                              # split-adjusted
    SPLIT_DIVIDEND = "split_dividend"            # split- and dividend-adjusted: research only, not executable
    UNKNOWN = "unknown"


class SourceKind(StrEnum):
    TUTORIAL = "tutorial"                        # bundled, labelled synthetic (spec 06.1 Explore)
    CSV = "csv"                                  # the user's own file (spec 06.3)
    ALPACA = "alpaca"                            # later, the user's own entitlement
    SEC_EDGAR = "sec_edgar"
    YAHOO_CHART = "yahoo_chart"                  # Yahoo Finance chart endpoint: free, unofficial (v1.4, ADR-014)
    STOOQ = "stooq"                              # Stooq's daily CSV, the fallback: free, unofficial (v1.4, ADR-014)


class AvailabilityPolicy(StrEnum):
    PROVIDER_STATED = "provider_stated"          # available_at came with the data
    INTERVAL_END = "interval_end"                # assigned as the bar's end: research only, PIT_LIMITED (06.3)


@contract
class Instrument(Record):
    """A tradable thing. `instrument_id` is the stable identity; a ticker alone is not (it can be reused)."""
    SCHEMA = "instrument/1"
    instrument_id: str
    symbol: str
    asset_class: AssetClass
    venue: str                                   # MIC or "UNKNOWN" for a CSV that does not say
    currency: str                                # ISO 4217, e.g. "USD"
    tick_size: Decimal
    multiplier: Decimal = Decimal(1)
    # options only (spec 05.2)
    underlying_id: str | None = None
    occ_symbol: str | None = None
    expiry: date | None = None
    right: OptionRight | None = None
    strike: Decimal | None = None
    settlement: str | None = None
    exercise_style: str | None = None

    def validate(self) -> None:
        if not self.instrument_id or not self.symbol:
            raise ValidationError("Instrument: instrument_id and symbol are required")
        if self.tick_size <= 0 or self.multiplier <= 0:
            raise ValidationError(f"Instrument {self.symbol}: tick_size and multiplier must be positive")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValidationError(f"Instrument {self.symbol}: currency must be an ISO 4217 code")
        option_fields = (self.underlying_id, self.expiry, self.right, self.strike)
        if self.asset_class is AssetClass.OPTION and any(v is None for v in option_fields):
            raise ValidationError(f"Instrument {self.symbol}: an option needs underlying, expiry, right and strike")


@contract
class Bar(Record):
    """One completed (or explicitly incomplete) OHLCV bar. Immutable; `bar_id` is content-derived by the store."""
    SCHEMA = "bar/1"
    instrument_id: str
    interval: Interval
    start: datetime
    end: datetime                                # observed_at
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    available_at: datetime
    received_at: datetime | None = None
    provider: str
    feed: str | None = None
    adjustment: Adjustment = Adjustment.UNKNOWN
    complete: bool = True
    quality_codes: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.end > self.start:
            raise ValidationError(f"Bar {self.instrument_id}@{self.end}: end must be after start")
        if self.available_at < self.end:
            raise ValidationError(f"Bar {self.instrument_id}@{self.end}: available_at before the bar ended",
                                  code=ErrorCode.LOOKAHEAD_RISK)
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValidationError(f"Bar {self.instrument_id}@{self.end}: prices must be positive")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close):
            raise ValidationError(f"Bar {self.instrument_id}@{self.end}: impossible OHLC")
        if self.volume < 0:
            raise ValidationError(f"Bar {self.instrument_id}@{self.end}: negative volume")


@contract
class Quote(Record):
    """A top-of-book quote. Only where the provider's rights allow quote-aware research (spec 09.2)."""
    SCHEMA = "quote/1"
    instrument_id: str
    observed_at: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    available_at: datetime
    provider: str
    feed: str | None = None

    def validate(self) -> None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValidationError(f"Quote {self.instrument_id}@{self.observed_at}: invalid bid/ask")
        if self.available_at < self.observed_at:
            raise ValidationError("Quote: available_at before observed_at", code=ErrorCode.LOOKAHEAD_RISK)


@contract
class QualityIssue(Part):
    """One finding of the quality gate (spec 06.4). Anomalies are investigated, not silently dropped."""
    code: str                                    # e.g. DUPLICATE_TIMESTAMP, IMPOSSIBLE_OHLC, MISSING_SESSION
    severity: Severity
    instrument_id: str | None = None
    at: datetime | None = None
    detail: str
    rows: tuple[int, ...] = ()                   # source row numbers, when the source has rows


@contract
class RightsLabel(Part):
    """What the user may do with the data. Unknown rights are DATA_RIGHTS_UNKNOWN, not permission."""
    label: str                                   # e.g. "synthetic-tutorial", "user-provided", "alpaca-iex"
    stated_by: str                               # "traderos" for the tutorial, "user" for their own CSV
    export_raw_allowed: bool = False
    redistribution_allowed: bool = False
    notes: str = ""


@contract
class Coverage(Part):
    instrument_id: str
    symbol: str
    first_bar_end: datetime | None
    last_bar_end: datetime | None
    bars: int
    missing_sessions: int = 0


@contract
class DatasetManifest(Record):
    """An immutable dataset snapshot (spec 05.2 Q01). Its content hashes name the exact bytes it was built from."""
    SCHEMA = "dataset/1"
    dataset_id: str
    name: str
    source_kind: SourceKind
    provider: str
    feed: str | None = None
    synthetic: bool                              # True for tutorial data: SafeFor.TUTORIAL_ONLY, never evidence
    rights: RightsLabel
    interval: Interval
    adjustment: Adjustment
    availability_policy: AvailabilityPolicy
    calendar_id: str
    calendar_version: str
    instruments: tuple[Instrument, ...]
    universe_as_of: date | None = None
    coverage: tuple[Coverage, ...]
    usability: DatasetUsability
    quality_issues: tuple[QualityIssue, ...] = ()
    limitations: tuple[Limitation, ...] = ()
    raw_sha256: str                              # the source file's exact bytes
    normalized_sha256: str                       # canonical hash of the normalized bar list
    row_count: int
    recorded_at: datetime
    canonical_hash: str | None = None

    def validate(self) -> None:
        if self.synthetic and self.source_kind is not SourceKind.TUTORIAL:
            raise ValidationError("DatasetManifest: only tutorial datasets are synthetic")
        if any(i.severity is Severity.BLOCKING for i in self.quality_issues) and \
                self.usability not in (DatasetUsability.REJECTED, DatasetUsability.UNAVAILABLE):
            raise ValidationError("DatasetManifest: a blocking quality issue leaves the dataset rejected or "
                                  "unavailable")


# ---- the DataAdapter protocol's messages (spec 06.2) -------------------------------------------------------------
@contract
class AccessContext(Part):
    """Who is asking, under which entitlement. Free has one local user; the Machine binds a tenant."""
    user_id: str = "local"
    tenant_id: str | None = None
    entitlement: str | None = None


@contract
class FeedCapability(Part):
    asset_class: AssetClass
    feed: str
    intervals: tuple[Interval, ...]
    history_start: date | None = None
    delayed_minutes: int = 0
    indicative: bool = False
    adjustments: tuple[Adjustment, ...] = ()
    survivorship_free: bool = False
    live: bool = False
    cache_allowed: bool = False
    redistribution_allowed: bool = False


@contract
class ProviderCapabilities(Record):
    SCHEMA = "provider-capabilities/1"
    provider: str
    feeds: tuple[FeedCapability, ...]
    quotes: bool = False
    corporate_actions: bool = False
    option_chains: bool = False
    rate_limit_per_minute: int | None = None
    limitations: tuple[Limitation, ...] = ()


@contract
class BarRequest(Part):
    symbols: tuple[str, ...]
    interval: Interval
    start: datetime
    end: datetime
    feed: str | None = None
    adjustment: Adjustment = Adjustment.RAW


@contract
class DataBatch(Record):
    """What an adapter returns: observations plus the provider facts needed to judge them."""
    SCHEMA = "data-batch/1"
    provider: str
    feed: str | None = None
    instruments: tuple[Instrument, ...]
    bars: tuple[Bar, ...] = ()
    quotes: tuple[Quote, ...] = ()
    received_at: datetime
    limitations: tuple[Limitation, ...] = ()
