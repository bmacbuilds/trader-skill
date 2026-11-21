"""CSV import contract and quality gate (spec 06.3, 06.4), and the tutorial dataset (spec 06.1)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traderos import api, data
from traderos.contracts import AvailabilityPolicy, DataError, ErrorCode, Interval, Severity, SourceKind, record_hash
from traderos.data import ImportOptions, build, import_into, tutorial
from traderos.store import Store

NOW = datetime(2026, 11, 20, 22, 0, tzinfo=UTC)
OPTS = ImportOptions(name="test")
HEAD = "symbol,timestamp_utc,open,high,low,close,volume"


def csv(*rows, head=HEAD):
    return ("\n".join((head, *rows)) + "\n").encode()


def codes(manifest):
    return {i.code for i in manifest.quality_issues}


def refused(raw, opts=OPTS):
    with pytest.raises(DataError) as e:
        build(raw, opts, now=NOW)
    return e.value


# ---- the tutorial ------------------------------------------------------------------------------------------------
TUTORIAL_RAW_SHA256 = "7cbafb03f8ceca947ce63a958c0a2a977b5ed3124e5a7018fc3ded1d27c06ab6"


def test_tutorial_is_byte_identical_everywhere():
    assert data.sha256_bytes(tutorial.csv_bytes()) == TUTORIAL_RAW_SHA256


def test_tutorial_is_labelled_synthetic_and_passes_the_same_gate():
    m, bars = build(tutorial.csv_bytes(), tutorial.OPTIONS, now=NOW)
    assert m.synthetic and m.source_kind is SourceKind.TUTORIAL
    assert {lim.code for lim in m.limitations} >= {"SYNTHETIC_DATA", "PIT_LIMITED"}
    assert len(m.instruments) == 7 and all(i.symbol.startswith("TUT") for i in m.instruments)
    assert m.row_count == len(bars) == 7 * 2514
    assert not [i for i in m.quality_issues if i.severity is Severity.BLOCKING]


# ---- daily timestamps name a session -----------------------------------------------------------------------------
@pytest.mark.parametrize("stamp", ["2026-11-20T21:00:00Z", "2026-11-20T14:30:00Z", "2026-11-20T00:00:00-05:00",
                                   "2026-11-20T16:00:00-05:00"])
def test_daily_stamps_that_name_the_same_session(stamp):
    m, bars = build(csv(f"SPY,{stamp},600,604,598,603,100"), OPTS, now=NOW)
    (b,) = bars
    assert b.start == datetime(2026, 11, 20, 14, 30, tzinfo=UTC) and b.end == datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
    assert b.available_at == b.end and m.availability_policy is AvailabilityPolicy.INTERVAL_END


def test_half_day_bar_ends_at_13_00_new_york():
    _, (b,) = build(csv("SPY,2026-11-27T18:00:00Z,600,604,598,603,100"), OPTS, now=NOW)
    assert b.end == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


# ---- refusals: blocking issues refuse the whole file -------------------------------------------------------------
@pytest.mark.parametrize("row,code", [
    ("SPY,2026-11-20T00:00:00Z,600,604,598,603,100", "AMBIGUOUS_TIMESTAMP"),
    ("SPY,2026-11-20,600,604,598,603,100", "UNREADABLE_VALUE"),              # date only: naive
    ("SPY,2026-11-20T21:00:00,600,604,598,603,100", "UNREADABLE_VALUE"),     # no zone
    ("SPY,2026-11-26T21:00:00Z,600,604,598,603,100", "NOT_A_SESSION"),       # Thanksgiving
    ("SPY,2026-11-21T21:00:00Z,600,604,598,603,100", "NOT_A_SESSION"),       # Saturday
    ("=cmd,2026-11-20T21:00:00Z,600,604,598,603,100", "UNREADABLE_VALUE"),   # formula
    ("SPY,2026-11-20T21:00:00Z,600,590,598,603,100", "IMPOSSIBLE_BAR"),      # high below open
    ("SPY,2026-11-20T21:00:00Z,600,604,598,603,-5", "IMPOSSIBLE_BAR"),
    ("SPY,2026-11-20T21:00:00Z,abc,604,598,603,100", "UNREADABLE_VALUE"),
    ("SPY,2026-11-20T21:00:00Z,NaN,604,598,603,100", "UNREADABLE_VALUE"),
    ("SPY,2026-11-20T21:00:00Z,600,604,598,603,100,extra", "EXTRA_CELLS"),
    ("SPY,2007-11-20T21:00:00Z,600,604,598,603,100", "CALENDAR_UNKNOWN"),
])
def test_blocking_rows_refuse_the_file(row, code):
    err = refused(csv("SPY,2026-11-19T21:00:00Z,600,604,598,603,100", row))
    assert err.code is ErrorCode.INVALID_INPUT and code in err.message


@pytest.mark.parametrize("cell", ['=HYPERLINK("http://x")', "+cmd", "@SUM(A1)", "-2+3"])
def test_formula_text_in_free_text_columns_is_refused(cell):
    raw = csv(f"SPY,2026-11-20T21:00:00Z,600,604,598,603,100,{cell}", head=HEAD + ",rights_label")
    assert "spreadsheet formula" in refused(raw).message


def test_duplicate_bar_is_blocking():
    row = "SPY,2026-11-20T21:00:00Z,600,604,598,603,100"
    assert "DUPLICATE_BAR" in refused(csv(row, row.replace("21:00", "14:30"))).message


def test_available_before_the_bar_ended_is_look_ahead():
    raw = csv("SPY,2026-11-20T21:00:00Z,600,604,598,603,100,2026-11-20T20:00:00Z", head=HEAD + ",available_at_utc")
    assert "LOOKAHEAD_AVAILABILITY" in refused(raw).message


@pytest.mark.parametrize("head,needle", [
    ("symbol,timestamp_utc,open,high,low,close", "missing column(s) volume"),
    (HEAD + ",colour", "unknown column(s) colour"),
    (HEAD + ",close", "repeated column(s) close"),
])
def test_header_is_checked(head, needle):
    assert needle in refused(csv(head=head)).message


def test_mixed_interval_and_non_utf8_and_size():
    assert "differs from the dataset" in refused(csv("SPY,2026-11-20T21:00:00Z,600,604,598,603,100,5m",
                                                     head=HEAD + ",interval")).message
    with pytest.raises(DataError):
        build(b"symbol\xff,timestamp_utc\n", OPTS, now=NOW)


def test_oversized_file_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "big.csv"
    path.write_bytes(b"x" * 2048)
    monkeypatch.setattr(data, "MAX_BYTES", 1024)
    with pytest.raises(DataError) as e:
        data.read_file(path)
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED


# ---- warnings travel with the dataset ----------------------------------------------------------------------------
def test_warnings_are_recorded_not_repaired():
    m, bars = build(csv("SPY,2026-11-17T21:00:00Z,100,104,98,103,100",
                        "SPY,2026-11-19T21:00:00Z,103,105,101,102,0",          # 11-18 missing; zero volume
                        "SPY,2026-11-20T21:00:00Z,150,156,149,155,120"), OPTS, now=NOW)   # a 47% gap
    assert {"MISSING_SESSION", "ZERO_VOLUME", "LARGE_MOVE_INVESTIGATE"} <= codes(m)
    assert len(bars) == 3 and m.coverage[0].missing_sessions == 1


def test_provider_availability_is_kept_and_not_pit_limited():
    raw = csv("SPY,2026-11-20T21:00:00Z,600,604,598,603,100,2026-11-20T21:15:00Z", head=HEAD + ",available_at_utc")
    m, (b,) = build(raw, OPTS, now=NOW)
    assert b.available_at == datetime(2026, 11, 20, 21, 15, tzinfo=UTC)
    assert m.availability_policy is AvailabilityPolicy.PROVIDER_STATED
    assert "PIT_LIMITED" not in {lim.code for lim in m.limitations}


def test_intraday_bars_start_at_the_stamp_and_are_cut_at_a_half_day_close():
    opts = ImportOptions(name="5m", interval=Interval.M5)
    m, bars = build(csv("SPY,2026-11-27T17:55:00Z,600,604,598,603,100",       # 12:55 New York, half day
                        "SPY,2026-11-27T13:00:00Z,600,604,598,603,100"), opts, now=NOW)  # 08:00: pre-market
    _, second = sorted(bars, key=lambda b: b.start)
    assert second.end == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert "OUTSIDE_SESSION" in codes(m)


# ---- the store ---------------------------------------------------------------------------------------------------
def test_import_is_idempotent_and_readable_as_of(tmp_path):
    store = Store(tmp_path / "home").init()
    raw = csv("SPY,2026-11-19T21:00:00Z,600,604,598,603,100", "SPY,2026-11-20T21:00:00Z,603,606,600,605,120")
    first = import_into(store, raw, OPTS, now=NOW)
    again = import_into(store, raw, OPTS, now=datetime(2026, 11, 21, tzinfo=UTC))
    assert again == first and len(store.datasets()) == 1
    reader = store.bar_reader()
    ins = first.instruments[0].instrument_id
    assert len(reader.bars(first.dataset_id, ins, as_of=datetime(2026, 11, 20, 20, 59, tzinfo=UTC))) == 1
    assert len(reader.bars(first.dataset_id, ins, as_of=datetime(2026, 11, 20, 21, 0, tzinfo=UTC))) == 2
    assert store.read_artifact(first.raw_sha256) == raw
    (event,) = store.search_memory("imported")
    assert event.payload_hash == record_hash(first)


def test_api_load_and_check(tmp_path):
    store = Store(tmp_path / "home")
    m = api.load_dataset("tutorial", store=store)
    assert api.check_dataset(m.dataset_id, store=store) == m
    assert api.load_dataset("tutorial", store=store) == m
