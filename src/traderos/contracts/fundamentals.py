"""SEC-sourced records: earnings (S04), 13F holdings (S06) and the fundamental screen (S07). Contracts v1.2, additive
(ADR-007); extended additively in v1.3 for the port of the upstream SEC tools (ADR-011): every new field has a default,
and two records are new, `holdings-diff/1` and `copy-targets/1` (hypothetical weights, never an order).

Every number names the filing it came from and the day that filing became public (`filed`), so nothing is used before
it was public (spec 06, point in time). For an earnings value that is the latest filing that reported it, because a
restatement wins: the first day the period's value could have been known is `first_filed` (FIN-001). A 13F record's
`filed` is the day its holdings, amendments applied, were all public. A value TraderOS computed rather than read,
such as a fourth quarter derived from the annual total minus three quarters, says so (`derived`).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from traderos.contracts.common import Completeness, Limitation, MetricValue, SafeFor
from traderos.contracts.errors import ValidationError
from traderos.contracts.records import Part, Record, contract


@contract
class FilingFact(Part):
    concept: str                                 # e.g. "us-gaap:Revenues"
    value: Decimal
    unit: str                                    # e.g. "USD", "USD/shares"
    period_start: date | None = None
    period_end: date
    fiscal_year: int
    fiscal_period: str                           # FY, Q1, Q2, Q3, Q4
    form: str                                    # 10-K, 10-Q, 10-K/A ...
    filed: date
    accession: str
    derived: bool = False
    amendment: bool = False
    first_filed: date | None = None              # v1.3: the day the period's value was FIRST public (FIN-001)


@contract
class FilingLink(Part):
    """v1.3: a filing a report points at (the newest 10-Q/10-K, the newest earnings 8-K and its press release)."""
    form: str
    filed: date
    accession: str
    url: str
    period: date | None = None
    exhibit_url: str | None = None


@contract
class QuarterLine(Part):
    fiscal_year: int
    fiscal_period: str
    period_end: date
    revenue: MetricValue
    net_income: MetricValue
    eps_diluted: MetricValue
    filed: date | None = None                    # the latest filing among its facts: when the line was complete
    derived: bool = False
    sources: tuple[str, ...] = ()                # accession numbers
    # v1.3 (ADR-011), the upstream columns. Margins and growth are "percent": 16.4 means 16.4%.
    period_start: date | None = None
    gross_profit: MetricValue | None = None
    operating_income: MetricValue | None = None
    gross_margin: MetricValue | None = None
    operating_margin: MetricValue | None = None
    net_margin: MetricValue | None = None
    revenue_yoy: MetricValue | None = None
    net_income_yoy: MetricValue | None = None
    eps_yoy: MetricValue | None = None
    first_filed: date | None = None              # the day every number in the line had first been public
    form: str | None = None
    derived_series: tuple[str, ...] = ()         # which series in the line are derived (PR #24, review change 1)
    amended_series: tuple[str, ...] = ()         # which series come from a 10-K/A or 10-Q/A


@contract
class EarningsReport(Record):
    SCHEMA = "earnings/1"
    report_id: str
    cik: str
    entity: str
    ticker: str | None = None
    as_of: date                                  # only facts filed on or before this day were read
    quarters: tuple[QuarterLine, ...]
    facts: tuple[FilingFact, ...]
    source_url: str
    retrieved_at: datetime
    completeness: Completeness
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    canonical_hash: str | None = None
    # v1.3 (ADR-011)
    ttm: dict[str, MetricValue] | None = None    # revenue, net_income, eps_diluted over ttm_quarter_ends
    ttm_quarter_ends: tuple[date, ...] = ()      # the SAME four quarter ends for every series
    ttm_derived: tuple[str, ...] = ()            # series whose trailing total includes a derived quarter
    concepts: dict[str, tuple[str, ...]] | None = None   # the us-gaap concepts each series was read from
    changes: tuple[str, ...] = ()                # "what changed" in plain sentences
    latest_report: FilingLink | None = None
    earnings_release: FilingLink | None = None


@contract
class Holding(Part):
    issuer: str
    title_of_class: str
    cusip: str
    value_usd: Decimal
    shares: Decimal
    share_type: str                              # SH or PRN
    put_call: str | None = None
    # v1.3 (ADR-011)
    name: str | None = None
    ticker: str | None = None
    ticker_source: str | None = None             # override, name, name-prefix, none
    ticker_check: bool = False                   # a guess: confirm it before using it
    issuer_cik: str | None = None
    pct_of_13f: Decimal | None = None            # percent of the whole information table, 2 decimals
    cusips: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()                # share classes combined into this holding
    classes: tuple[str, ...] = ()
    rows: int | None = None                      # information-table rows summed into it


@contract
class HoldingsReport(Record):
    """S06: one 13F filing as reported. Reported holdings at a quarter end, made public later; never a trade time."""
    SCHEMA = "holdings/1"
    report_id: str
    filer_cik: str
    filer_name: str
    accession: str
    form: str
    period_of_report: date
    filed: date
    holdings: tuple[Holding, ...]
    total_value_usd: Decimal
    source_url: str
    retrieved_at: datetime
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    canonical_hash: str | None = None
    # v1.3 (ADR-011)
    combined: bool = False                       # share classes of one issuer summed
    cover_total_usd: Decimal | None = None       # the cover page's tableValueTotal, in dollars
    amendments_applied: tuple[str, ...] = ()     # 13F-HR/A accessions applied (restatement or new holdings)
    notes: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.filed < self.period_of_report:
            raise ValidationError("HoldingsReport: a 13F is filed after its period ends")


CHANGE_STATUSES = ("NEW", "SOLD OUT", "ADDED", "TRIMMED", "UNCHANGED")


@contract
class HoldingChange(Part):
    status: str                                  # NEW, SOLD OUT, ADDED, TRIMMED, UNCHANGED
    issuer: str
    title_of_class: str
    cusip: str
    ticker: str | None = None
    put_call: str | None = None
    shares_before: Decimal
    shares_after: Decimal
    change: Decimal
    change_pct: Decimal | None = None
    value_before: Decimal
    value_after: Decimal

    def validate(self) -> None:
        if self.status not in CHANGE_STATUSES:
            raise ValidationError(f"HoldingChange: status {self.status!r} is not one of {CHANGE_STATUSES}")


@contract
class HoldingsDiff(Record):
    """v1.3, S06: share counts at two quarter ends of one filer. Not trades, not their dates, not their prices."""
    SCHEMA = "holdings-diff/1"
    diff_id: str
    filer_cik: str
    filer_name: str
    accession: str
    period_of_report: date
    filed: date
    prior_accession: str
    prior_period_of_report: date
    prior_filed: date
    changes: tuple[HoldingChange, ...]
    source_url: str
    prior_source_url: str
    retrieved_at: datetime
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    canonical_hash: str | None = None

    def validate(self) -> None:
        if self.prior_period_of_report >= self.period_of_report:
            raise ValidationError("HoldingsDiff: the prior quarter ends before the current one")


@contract
class TargetWeight(Part):
    ticker: str
    name: str
    weight: Decimal                              # percent of the chosen positions' value, exact; they sum to 100
    pct_of_13f: Decimal
    value_usd: Decimal
    shares: Decimal
    cusips: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    check_ticker: bool = False

    def validate(self) -> None:
        if self.weight <= 0:
            raise ValidationError("TargetWeight: a weight is positive")


@contract
class SkippedPosition(Part):
    issuer: str
    ticker: str | None = None
    why: str


@contract
class CopyTargets(Record):
    """v1.3, S06: a filer's stock positions scaled to 100, as HYPOTHETICAL weights for research and planning.

    It sizes nothing, names no account and places no order; there is no field for any of that."""
    SCHEMA = "copy-targets/1"
    targets_id: str
    filer_cik: str
    filer_name: str
    accession: str
    period_of_report: date
    filed: date
    method: str
    weights: tuple[TargetWeight, ...]
    skipped: tuple[SkippedPosition, ...] = ()
    caveats: tuple[str, ...] = ()
    source_url: str
    retrieved_at: datetime
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    canonical_hash: str | None = None

    def validate(self) -> None:
        if not self.weights:
            raise ValidationError("CopyTargets: nothing left to copy")
        if not self.safe_for or set(self.safe_for) - {SafeFor.RESEARCH, SafeFor.HYPOTHETICAL_PLANNING}:
            raise ValidationError("CopyTargets: hypothetical weights are for research and hypothetical planning only")


@contract
class ScreenRow(Part):
    cik: str
    ticker: str | None = None
    entity: str
    metrics: dict[str, MetricValue]
    passed: bool | None                          # None: a criterion could not be evaluated (missing data)
    reasons: tuple[str, ...] = ()                # why it did not pass (upstream's "fails")
    # v1.3 (ADR-011)
    labels: dict[str, str] | None = None         # text columns a rule can test (sector, ...)
    passes: tuple[str, ...] = ()                 # the rules it met, in words
    skipped: tuple[str, ...] = ()                # rules on a missing metric, with "missing": "skip"
    score: Decimal | None = None                 # rank score among the companies that pass, 0 (worst) to 1
    rank_parts: dict[str, Decimal] | None = None
    rank_gaps: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()                  # STALE_PERIOD, PERIOD_MISMATCH, DERIVED ...
    period_end: date | None = None               # the newest quarter its figures cover
    filed: date | None = None                    # the day those figures were all public


@contract
class FundamentalScreen(Record):
    SCHEMA = "fundamental-screen/1"
    screen_id: str
    as_of: date
    criteria: dict[str, Any]
    rows: tuple[ScreenRow, ...]
    completeness: Completeness
    limitations: tuple[Limitation, ...] = ()
    safe_for: tuple[SafeFor, ...]
    created_at: datetime
    canonical_hash: str | None = None
    # v1.3 (ADR-011)
    name: str | None = None
    prices_as_of: date | None = None
    top: int | None = None
