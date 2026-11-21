"""The core interfaces parts build against (spec 04.4, 06.2, 07.1). FROZEN in contracts v1.

Parallel builders code against these Protocols and the records in `traderos.contracts`; the core supplies the real
implementations (`traderos.clock`, `traderos.data`, `traderos.features`). Every read is AS-OF: a caller states the
instant it is deciding at, and only observations available at or before that instant come back. That rule is how
QT01 (no look-ahead) holds across every part without each part re-implementing it.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from traderos.contracts.capability import CapabilityEntry, CapabilityId
from traderos.contracts.market import (
    AccessContext,
    Bar,
    BarRequest,
    DataBatch,
    DatasetManifest,
    Instrument,
    Interval,
    ProviderCapabilities,
)
from traderos.contracts.research import FeatureDefinition


@runtime_checkable
class ExchangeCalendar(Protocol):
    """Sessions of one exchange, from a real calendar (spec 04.3). Outside `coverage` every method raises
    DataError(CALENDAR_UNKNOWN); it never guesses."""
    calendar_id: str                             # e.g. "XNYS"
    version: str                                 # bumps whenever a holiday or early close changes
    coverage: tuple[date, date]

    def is_session(self, day: date) -> bool: ...
    def sessions(self, start: date, end: date) -> tuple[date, ...]: ...
    def open_close(self, day: date) -> tuple[datetime, datetime]: ...   # aware UTC; raises on a non-session
    def is_early_close(self, day: date) -> bool: ...
    def next_session(self, day: date) -> date: ...                      # the first session strictly after day
    def previous_session(self, day: date) -> date: ...                  # the last session strictly before day
    def session_of(self, instant: datetime) -> date | None: ...         # the session open at instant, if any


@runtime_checkable
class BarReader(Protocol):
    """Read-only, as-of-safe access to a stored dataset. Never returns a bar whose available_at > as_of."""

    def manifest(self, dataset_id: str) -> DatasetManifest: ...
    def instruments(self, dataset_id: str) -> tuple[Instrument, ...]: ...
    def bars(self, dataset_id: str, instrument_id: str, *, as_of: datetime, start: datetime | None = None,
             limit: int | None = None) -> tuple[Bar, ...]: ...
    # oldest first; `limit` keeps the newest `limit` bars


@runtime_checkable
class Feature(Protocol):
    """A versioned pure feature (spec 07.1). Output is aligned with `bars`; None means not yet usable (warm-up or
    a missing input), never zero. Must not read beyond the bars it is given."""
    definition: FeatureDefinition

    def compute(self, bars: Sequence[Bar]) -> tuple[Decimal | None, ...]: ...


@runtime_checkable
class DataAdapter(Protocol):
    """A capability-checked provider client (spec 06.2). An unsupported call raises OUTSIDE_CAPABILITY; it never
    falls back to lower-grade data on its own."""
    provider: str

    def capabilities(self, identity: AccessContext) -> ProviderCapabilities: ...
    def fetch_bars(self, req: BarRequest, identity: AccessContext) -> DataBatch: ...


@runtime_checkable
class Capabilities(Protocol):
    """The release registry (spec 01.2). `require` raises CapabilityDisabled for anything not release_approved
    in this build, unless the caller asks for an explicitly experimental, research-only mode the entry allows."""

    def entry(self, capability_id: CapabilityId) -> CapabilityEntry: ...
    def require(self, capability_id: CapabilityId) -> CapabilityEntry: ...


__all__ = ["BarReader", "Capabilities", "DataAdapter", "ExchangeCalendar", "Feature", "Interval"]
