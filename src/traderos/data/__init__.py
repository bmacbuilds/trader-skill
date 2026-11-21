"""Bar data in: the CSV import contract (spec 06.3) and the quality gate (spec 06.4).

CSV CONTRACT (docs/data-csv-v1.md):
  * required header `symbol,timestamp_utc,open,high,low,close,volume`;
    optional `available_at_utc,interval,source,feed,adjustment,currency,complete,rights_label`; anything else is
    refused, so a misspelled column never silently disappears;
  * `timestamp_utc` is the bar's START, RFC 3339 with `Z` or an offset; a naive timestamp is refused. For a daily
    bar it names the New York session: the session's open, its close and New York midnight all name the same
    session, and the stored bar runs from that session's open to its close (13:00 on a half day). Midnight UTC is
    refused for daily bars: it is the evening before in New York, so it cannot be read without guessing;
  * without `available_at_utc`, a bar is taken as available when it ended (policy `interval_end`): research only,
    and the dataset carries PIT_LIMITED (spec 06.3). It never certifies intrabar or vendor publication timing;
  * text cells that start like a spreadsheet formula (= + - @) are refused, as are oversized files, rows and cells.

QUALITY GATE: every issue gets a code and a severity. A BLOCKING issue (an impossible bar, a duplicate, a bar on a
day the exchange was closed, an unreadable value) refuses the whole import: it is never repaired silently and
cannot be accepted casually (spec 06.4). Warnings (missing sessions, zero volume, a large move to investigate) are
recorded in the manifest and travel with every result built on it.

The raw file is kept byte for byte in the store (content-addressed), and the manifest names its SHA-256.
"""
from __future__ import annotations

import contextlib
import csv
import dataclasses
import io
import pathlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from traderos.clock import XNYS, eastern_to_utc
from traderos.contracts import (
    ActorRole,
    Adjustment,
    AssetClass,
    AvailabilityPolicy,
    Bar,
    Coverage,
    DataError,
    DatasetManifest,
    DatasetUsability,
    ErrorCode,
    Instrument,
    IntegrityError,
    Interval,
    Limitation,
    QualityIssue,
    ResearchMemoryEvent,
    RightsLabel,
    Severity,
    SourceKind,
    TraderOSError,
    ValidationError,
    canonical_bytes,
    parse_decimal,
    parse_utc,
    record_hash,
    sha256_bytes,
    with_hash,
)

MAX_BYTES = 256 * 1024 * 1024
MAX_ROWS = 5_000_000
MAX_CELL = 64
MAX_LISTED = 20                                  # row numbers listed per issue code; the count is always complete
REQUIRED = ("symbol", "timestamp_utc", "open", "high", "low", "close", "volume")
OPTIONAL = ("available_at_utc", "interval", "source", "feed", "adjustment", "currency", "complete", "rights_label")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# spec 06.3: the fixed ETF research universe; anything else from a CSV is taken as an equity (and says so)
KNOWN_ETFS = {"SPY", "QQQ", "IWM", "XLF", "XLK", "GLD", "SLV", "DIA", "TLT", "XLE", "XLV", "XLY", "XLP", "XLI",
              "XLU", "XLB", "XLRE", "XLC", "VTI", "VOO", "EFA", "EEM", "HYG", "LQD", "IEF", "SHY"}
INTERVAL_DELTA = {Interval.M1: timedelta(minutes=1), Interval.M5: timedelta(minutes=5),
                  Interval.M15: timedelta(minutes=15), Interval.H1: timedelta(hours=1)}
LARGE_MOVE = Decimal("0.25")                     # a 25% open gap or close move is investigated, never dropped


@dataclass(frozen=True)
class ImportOptions:
    name: str
    interval: Interval = Interval.D1
    currency: str = "USD"
    venue: str = "UNKNOWN"
    provider: str = "csv"
    rights_label: str = "user-provided"
    rights_stated_by: str = "user"
    source_kind: SourceKind = SourceKind.CSV
    synthetic: bool = False
    export_raw_allowed: bool = False
    redistribution_allowed: bool = False
    limitations: tuple[Limitation, ...] = ()      # the source's own limits, added to the manifest's (ADR-014)


@dataclass
class _Issues:
    found: dict[tuple[str, Severity], dict] = dataclasses.field(default_factory=dict)

    def add(self, code: str, severity: Severity, detail: str, *, row: int | None = None,
            instrument_id: str | None = None, at: datetime | None = None) -> None:
        key = (code, severity)
        entry = self.found.setdefault(key, {"count": 0, "rows": [], "detail": detail, "instrument_id": instrument_id,
                                            "at": at})
        entry["count"] += 1
        if row is not None and len(entry["rows"]) < MAX_LISTED:
            entry["rows"].append(row)

    def records(self) -> tuple[QualityIssue, ...]:
        out = []
        for (code, severity), e in sorted(self.found.items(), key=lambda kv: (kv[0][1] != Severity.BLOCKING, kv[0][0])):
            detail = e["detail"] if e["count"] == 1 else f"{e['detail']} ({e['count']} times)"
            out.append(QualityIssue(code=code, severity=severity, instrument_id=e["instrument_id"], at=e["at"],
                                    detail=detail, rows=tuple(e["rows"])))
        return tuple(out)

    def blocking(self) -> list[QualityIssue]:
        return [i for i in self.records() if i.severity is Severity.BLOCKING]


def instrument_id(symbol: str, venue: str, currency: str) -> str:
    return f"INS-{symbol}-{venue}-{currency}"


def read_file(path: str | pathlib.Path) -> bytes:
    p = pathlib.Path(path)
    if not p.is_file():
        raise DataError(f"{p} is not a file", code=ErrorCode.MISSING_DATA)
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise DataError(f"{p.name} is {size} bytes; the limit is {MAX_BYTES}", code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
    return p.read_bytes()


def _cell(row: dict, key: str, line: int) -> str:
    value = row.get(key)
    if value is None:
        return ""
    value = value.strip()
    if len(value) > MAX_CELL:
        raise ValidationError(f"row {line}: {key} is longer than {MAX_CELL} characters")
    return value


def _text(value: str, key: str, line: int) -> str:
    if value.startswith(FORMULA_START):
        raise ValidationError(f"row {line}: {key} {value!r} starts like a spreadsheet formula; refused")
    return value


def _daily_session(cal: XNYS, ts: datetime) -> date:
    """The New York session a daily bar's timestamp names (see the module docstring)."""
    if ts.hour == 0 and ts.minute == 0 and ts.second == 0 and ts.microsecond == 0:
        raise ValidationError(f"{ts.isoformat()} is midnight UTC, the evening before in New York; stamp a daily bar "
                              "with the session's close (e.g. 21:00Z), open, or New York midnight with its offset")
    for offset in (timedelta(hours=-4), timedelta(hours=-5)):
        local = ts + offset
        day = local.date()
        if eastern_to_utc(day, local.time()) == ts:
            return day
    raise ValidationError(f"cannot place {ts.isoformat()} in New York time")


def parse_rows(raw: bytes, opts: ImportOptions, cal: XNYS, issues: _Issues) -> tuple[list[Bar], dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise DataError(f"the file is not UTF-8 text (byte {e.start})", code=ErrorCode.INVALID_INPUT) from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [h for h in REQUIRED if h not in header]
    unknown = [h for h in header if h not in REQUIRED + OPTIONAL]
    dupes = [h for h, n in Counter(header).items() if n > 1]
    if missing or unknown or dupes:
        parts = []
        if missing:
            parts.append(f"missing column(s) {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown column(s) {', '.join(unknown)}")
        if dupes:
            parts.append(f"repeated column(s) {', '.join(dupes)}")
        raise DataError("; ".join(parts) + f". Required: {','.join(REQUIRED)}. Optional: {','.join(OPTIONAL)}.",
                        code=ErrorCode.INVALID_INPUT)
    reader.fieldnames = header
    facts: dict[str, set[str]] = defaultdict(set)
    bars: list[Bar] = []
    seen: dict[tuple[str, datetime], int] = {}
    for n, row in enumerate(reader):
        line = n + 2
        if n >= MAX_ROWS:
            raise DataError(f"more than {MAX_ROWS} rows", code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
        if None in row:                                         # more cells than columns
            issues.add("EXTRA_CELLS", Severity.BLOCKING, "a row has more cells than the header", row=line)
            continue
        try:
            symbol = _text(_cell(row, "symbol", line), "symbol", line).upper()
            if not SYMBOL.match(symbol):
                raise ValidationError(f"row {line}: {symbol!r} is not a ticker symbol")
            for key in ("source", "feed", "currency", "rights_label", "interval", "adjustment"):
                value = _text(_cell(row, key, line), key, line)
                if value:
                    facts[key].add(value if key != "currency" else value.upper())
            interval = Interval(_cell(row, "interval", line) or opts.interval.value)
            if interval is not opts.interval:
                raise ValidationError(f"row {line}: interval {interval.value} differs from the dataset's "
                                      f"{opts.interval.value}")
            ts = parse_utc(_cell(row, "timestamp_utc", line), field=f"row {line} timestamp_utc")
            prices = {k: parse_decimal(_cell(row, k, line), field=f"row {line} {k}")
                      for k in ("open", "high", "low", "close", "volume")}
            complete_text = _cell(row, "complete", line).lower()
            if complete_text not in ("", "true", "false", "1", "0"):
                raise ValidationError(f"row {line}: complete must be true or false")
            complete = complete_text not in ("false", "0")
            adjustment = Adjustment(_cell(row, "adjustment", line) or "unknown")
        except (ValidationError, ValueError) as e:
            issues.add("UNREADABLE_VALUE", Severity.BLOCKING, str(e), row=line)
            continue
        currency = (_cell(row, "currency", line) or opts.currency).upper()
        ins = instrument_id(symbol, opts.venue, currency)
        try:
            if interval is Interval.D1:
                try:
                    session = _daily_session(cal, ts)
                except ValidationError as e:
                    issues.add("AMBIGUOUS_TIMESTAMP", Severity.BLOCKING, e.message, row=line, instrument_id=ins)
                    continue
                if not cal.is_session(session):
                    issues.add("NOT_A_SESSION", Severity.BLOCKING, f"{session} is not an XNYS session "
                               f"({cal.holiday_name(session) or 'weekend'})", row=line, instrument_id=ins)
                    continue
                start, end = cal.open_close(session)
            else:
                session = cal.session_of(ts)
                start = ts
                end = ts + INTERVAL_DELTA[interval]
                if session is None:
                    issues.add("OUTSIDE_SESSION", Severity.WARNING, "a bar starts outside regular hours",
                               row=line, instrument_id=ins, at=ts)
                else:
                    close = cal.open_close(session)[1]
                    if end > close:
                        end = close
                        issues.add("SHORT_BAR", Severity.INFO, "a bar is cut short by the session close", row=line,
                                   instrument_id=ins, at=ts)
        except TraderOSError as e:
            issues.add("CALENDAR_UNKNOWN", Severity.BLOCKING, e.message, row=line, instrument_id=ins)
            continue
        avail_text = _cell(row, "available_at_utc", line)
        facts["_availability"].add("given" if avail_text else "missing")
        try:
            available = parse_utc(avail_text, field=f"row {line} available_at_utc") if avail_text else end
        except ValidationError as e:
            issues.add("UNREADABLE_VALUE", Severity.BLOCKING, e.message, row=line)
            continue
        if (ins, end) in seen:
            issues.add("DUPLICATE_BAR", Severity.BLOCKING, f"two bars for {symbol} ending {end.isoformat()} (rows "
                       f"{seen[(ins, end)]} and {line})", row=line, instrument_id=ins, at=end)
            continue
        seen[(ins, end)] = line
        try:
            bars.append(Bar(instrument_id=ins, interval=interval, start=start, end=end, open=prices["open"],
                            high=prices["high"], low=prices["low"], close=prices["close"], volume=prices["volume"],
                            available_at=available, provider=opts.provider, feed=None, adjustment=adjustment,
                            complete=complete))
        except ValidationError as e:
            code = "LOOKAHEAD_AVAILABILITY" if e.code is ErrorCode.LOOKAHEAD_RISK else "IMPOSSIBLE_BAR"
            issues.add(code, Severity.BLOCKING, e.message.split(": ", 1)[-1], row=line, instrument_id=ins, at=end)
            continue
        facts["_symbols"].add(f"{symbol}|{currency}")
        if not complete:
            issues.add("INCOMPLETE_BAR", Severity.INFO, "a bar is marked incomplete; it is never used as an input",
                       row=line, instrument_id=ins, at=end)
        if prices["volume"] == 0:
            issues.add("ZERO_VOLUME", Severity.WARNING, "a bar has zero volume", row=line, instrument_id=ins, at=end)
    if not bars and not issues.blocking():
        raise DataError("the file has no bars", code=ErrorCode.MISSING_DATA)
    for key in ("source", "feed", "rights_label", "interval", "adjustment"):
        if len(facts[key]) > 1:
            issues.add("MIXED_" + key.upper(), Severity.BLOCKING, f"the file mixes {key} values "
                       f"{sorted(facts[key])}; import one at a time")
    return bars, facts


def _gate_series(bars: list[Bar], cal: XNYS, issues: _Issues) -> list[Coverage]:
    by: dict[str, list[Bar]] = defaultdict(list)
    for b in bars:
        by[b.instrument_id].append(b)
    coverage = []
    for ins, series in sorted(by.items()):
        series.sort(key=lambda b: b.end)
        missing: list[date] = []
        if series[0].interval is Interval.D1:
            have = {b.start.astimezone(UTC).date() for b in series}
            first = series[0].start.astimezone(UTC).date()
            last = series[-1].start.astimezone(UTC).date()
            missing = [d for d in cal.sessions(first, last) if d not in have]
            for d in missing[:MAX_LISTED]:
                issues.add("MISSING_SESSION", Severity.WARNING, "a session between the first and last bar has no "
                           "bar", instrument_id=ins, at=cal.open_close(d)[1])
            for _ in missing[MAX_LISTED:]:
                issues.add("MISSING_SESSION", Severity.WARNING, "a session between the first and last bar has no "
                           "bar", instrument_id=ins)
        for prev, cur in pairwise(series):
            if prev.complete and cur.complete and abs(cur.open / prev.close - 1) > LARGE_MOVE:
                issues.add("LARGE_MOVE_INVESTIGATE", Severity.WARNING, "an open more than 25% from the previous close; "
                           "a split, a data error or a real event: investigate before trusting results",
                           instrument_id=ins, at=cur.end)
        symbol = ins.split("-")[1]
        coverage.append(Coverage(instrument_id=ins, symbol=symbol, first_bar_end=series[0].end,
                                 last_bar_end=series[-1].end, bars=len(series), missing_sessions=len(missing)))
    return coverage


def build(raw: bytes, opts: ImportOptions, *, now: datetime | None = None,
          cal: XNYS | None = None) -> tuple[DatasetManifest, list[Bar]]:
    """Validate, gate and normalize one file. Raises DataError(INVALID_INPUT) listing the blocking issues."""
    cal = cal or XNYS()
    now = now or datetime.now(UTC)
    issues = _Issues()
    bars, facts = parse_rows(raw, opts, cal, issues)
    blocking = issues.blocking()
    if blocking:
        lines = [f"{i.code}: {i.detail}" + (f" (rows {', '.join(map(str, i.rows))})" if i.rows else "")
                 for i in blocking[:10]]
        raise DataError("the file was not imported; nothing is repaired silently:\n  " + "\n  ".join(lines),
                        code=ErrorCode.INVALID_INPUT, details={"blocking_issues": str(len(blocking))})
    coverage = _gate_series(bars, cal, issues)
    bars.sort(key=lambda b: (b.instrument_id, b.end))
    symbols = sorted(facts["_symbols"])
    instruments = tuple(Instrument(instrument_id=instrument_id(s, opts.venue, c), symbol=s,
                                   asset_class=AssetClass.ETF if (s in KNOWN_ETFS or opts.synthetic)
                                   else AssetClass.EQUITY, venue=opts.venue, currency=c, tick_size=Decimal("0.01"))
                        for s, c in (x.split("|") for x in symbols))
    provided_availability = facts["_availability"] == {"given"}
    adjustments = {b.adjustment for b in bars}
    limitations = []
    if opts.synthetic:
        limitations.append(Limitation(code=ErrorCode.SYNTHETIC_DATA.value, severity=Severity.WARNING,
                                      detail="Synthetic tutorial data. Results show how TraderOS works; they are not "
                                             "evidence about any real market."))
    limitations.extend(opts.limitations)
    policy = AvailabilityPolicy.PROVIDER_STATED if provided_availability else AvailabilityPolicy.INTERVAL_END
    if policy is AvailabilityPolicy.INTERVAL_END:
        limitations.append(Limitation(code=ErrorCode.PIT_LIMITED.value, detail="No provider availability times: "
                                      "each bar is taken as usable when it ended. Research only."))
    if not opts.synthetic:
        limitations.append(Limitation(code="IDENTITY_FROM_TICKER", detail="Instruments are identified by ticker; a "
                                      "reused or renamed ticker would not be detected."))
        limitations.append(Limitation(code="SELECTION_BIAS", detail="The universe is the symbols in this file, chosen "
                                      "today; delisted names are absent."))
        limitations.append(Limitation(code="TICK_SIZE_ASSUMED", detail="Tick size assumed 0.01."))
    if adjustments == {Adjustment.UNKNOWN}:
        limitations.append(Limitation(code="ADJUSTMENT_UNKNOWN", detail="The file does not say whether prices are "
                                      "adjusted for splits and dividends."))
    normalized = sha256_bytes(canonical_bytes(list(bars)))
    raw_sha = sha256_bytes(raw)
    identity = sha256_bytes(canonical_bytes({"raw": raw_sha, "options": {
        "name": opts.name, "interval": opts.interval, "currency": opts.currency, "venue": opts.venue,
        "provider": opts.provider, "rights": opts.rights_label, "kind": opts.source_kind}}))
    manifest = DatasetManifest(
        dataset_id=f"DS-{identity[:20]}", name=opts.name, source_kind=opts.source_kind, provider=opts.provider,
        feed=(next(iter(facts["feed"])) if facts["feed"] else None), synthetic=opts.synthetic,
        rights=RightsLabel(label=next(iter(facts["rights_label"])) if facts["rights_label"] else opts.rights_label,
                           stated_by=opts.rights_stated_by, export_raw_allowed=opts.export_raw_allowed,
                           redistribution_allowed=opts.redistribution_allowed),
        interval=opts.interval,
        adjustment=next(iter(adjustments)) if len(adjustments) == 1 else Adjustment.UNKNOWN,
        availability_policy=policy, calendar_id=cal.calendar_id, calendar_version=cal.version,
        instruments=instruments, universe_as_of=None, coverage=tuple(coverage), usability=DatasetUsability.LIMITED,
        quality_issues=issues.records(), limitations=tuple(limitations), raw_sha256=raw_sha,
        normalized_sha256=normalized, row_count=len(bars), recorded_at=now)
    return with_hash(manifest), bars


def import_into(store, raw: bytes, opts: ImportOptions, *, now: datetime | None = None) -> DatasetManifest:
    """Gate the file and store it. Importing the same file with the same options again returns the stored dataset.

    A dataset that is already here under this id must hold exactly the bars this file builds (a bundle could have
    planted others under a genuine id): otherwise nothing is imported and the dataset is named. One file has one set
    of rights in a profile: the same file under other rights, here or named by a restored record, is refused and
    named, never stored twice under contradictory rights."""
    from traderos.store import same_dataset
    manifest, bars = build(raw, opts, now=now)
    for other in [*store.datasets(), *store.kept_manifests()]:
        if other.raw_sha256 == manifest.raw_sha256 and other.rights != manifest.rights:
            raise DataError(f"this file is already in your profile as {other.dataset_id} ({other.name!r}) under rights "
                            f"{_rights(other)}; it was not imported again under {_rights(manifest)}. One file has one "
                            "set of rights: import it with the rights it already has.",
                            code=ErrorCode.DATA_RIGHTS_UNKNOWN, details={"dataset_id": other.dataset_id})
    for existing in store.datasets():
        if existing.dataset_id == manifest.dataset_id:
            if same_dataset(existing, manifest):
                return existing
            raise IntegrityError(f"dataset {existing.dataset_id} is already here, but it holds {existing.row_count} "
                                 f"bars (normalized sha256 {existing.normalized_sha256[:12]}...) where this file "
                                 f"builds {manifest.row_count} (normalized sha256 {manifest.normalized_sha256[:12]}"
                                 "...): it was not built from this file. Nothing was imported.",
                                 details={"dataset_id": existing.dataset_id})
    with store.transaction() as db:              # the file, the dataset and its memory event: all, or none
        store.put_artifact(raw, "text/csv", db=db)
        store.put_dataset(manifest, bars, db=db)
        event = ResearchMemoryEvent(
            event_id=f"ME-{manifest.dataset_id}",
            actor_role=ActorRole.HUMAN if not opts.synthetic else ActorRole.SYSTEM, kind="dataset.imported",
            subject_refs=(manifest.dataset_id,),
            outcome_codes=tuple(lim.code for lim in manifest.limitations),
            summary=f"Imported {manifest.name}: {manifest.row_count} bars, "
                    f"{', '.join(i.symbol for i in manifest.instruments)}; {len(manifest.quality_issues)} quality "
                    "issue(s)", payload_hash=record_hash(manifest), previous_event_hash=store.last_memory_hash(db=db),
            at=manifest.recorded_at)
        with contextlib.suppress(IntegrityError):    # a restored profile already remembers this import: it stands
            store.remember(with_hash(event), db=db)
    return manifest


def _rights(m: DatasetManifest) -> str:
    return (f"{m.rights.label!r} (stated by {m.rights.stated_by}; raw export "
            f"{'allowed' if m.rights.export_raw_allowed else 'not allowed'})")


def describe(m: DatasetManifest) -> str:
    """A dataset in words: what it is, what it covers, and every limit on it (spec 13.2 data check)."""
    lines = [f"{m.dataset_id}  {m.name}",
             f"  source: {m.source_kind.value} ({m.provider})" + ("  SYNTHETIC TUTORIAL DATA" if m.synthetic else ""),
             f"  rights: {m.rights.label}, stated by {m.rights.stated_by}; raw export "
             f"{'allowed' if m.rights.export_raw_allowed else 'not allowed'}",
             f"  bars: {m.row_count} x {m.interval.value}, {m.adjustment.value} prices, calendar {m.calendar_id} "
             f"{m.calendar_version}, availability {m.availability_policy.value}",
             f"  usability: {m.usability.value}"]
    for c in m.coverage:
        span = f"{c.first_bar_end:%Y-%m-%d} to {c.last_bar_end:%Y-%m-%d}" if c.first_bar_end and c.last_bar_end else ""
        lines.append(f"    {c.symbol:<8} {c.bars:>6} bars  {span}"
                     + (f"  ({c.missing_sessions} missing sessions)" if c.missing_sessions else ""))
    if m.limitations:
        lines.append("  limitations:")
        lines += [f"    {lim.code}: {lim.detail}" for lim in m.limitations]
    if m.quality_issues:
        lines.append("  quality issues:")
        lines += [f"    [{i.severity.value}] {i.code}: {i.detail}" for i in m.quality_issues]
    lines.append(f"  raw file sha256 {m.raw_sha256}")
    return "\n".join(lines)


__all__ = ["OPTIONAL", "REQUIRED", "ImportOptions", "build", "describe", "import_into", "instrument_id", "read_file"]
