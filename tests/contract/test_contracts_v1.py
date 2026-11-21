"""Contracts v1 are frozen: these tests fail on any accidental change to a record, its serialization or its hash.

A deliberate change needs an ADR, then `python tools/contracts/regen_golden.py` (docs/contracts-v1.md section 8).
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from traderos import contracts as c
from traderos.contracts import canonical
from traderos.contracts.examples import all_examples, bar, variants

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "contracts"
EXAMPLES = all_examples()


# ---- every record: round trip, golden bytes, golden hash ---------------------------------------------------------
def test_every_record_type_has_an_example_and_a_golden_file():
    assert set(EXAMPLES) == set(c.RECORD_TYPES)
    golden = {p.name for p in GOLDEN.glob("*.json")}
    assert golden == {s.replace("/", "_v") + ".json" for s in c.RECORD_TYPES}


@pytest.mark.parametrize("schema", sorted(c.RECORD_TYPES))
def test_round_trip_and_golden(schema):
    record = EXAMPLES[schema]
    text = c.to_json(record)
    assert c.from_json(c.RECORD_TYPES[schema], text) == record
    golden = (GOLDEN / (schema.replace("/", "_v") + ".json")).read_text(encoding="utf-8").rstrip("\n")
    assert text == golden, f"{schema}: serialization drifted from the frozen golden file"
    assert c.from_json(c.RECORD_TYPES[schema], golden) == record


@pytest.mark.parametrize("name", sorted(variants()))
def test_variants_round_trip_and_golden(name):
    """Second shapes of a record after an additive change (ADR-010), frozen like the main set."""
    record = variants()[name]
    text = c.to_json(record)
    assert c.from_json(type(record), text) == record
    golden = (GOLDEN / "variants" / f"{name}.json").read_text(encoding="utf-8").rstrip("\n")
    assert text == golden, f"{name}: serialization drifted from the frozen golden file"
    hashes = dict(reversed(line.split("  ")) for line in (GOLDEN / "variants" / "HASHES.txt").read_text().splitlines())
    assert c.record_hash(record) == hashes[f"{name}.json"]
    assert {p.stem for p in (GOLDEN / "variants").glob("*.json")} == set(variants())


def test_golden_hashes_are_frozen():
    for line in (GOLDEN / "HASHES.txt").read_text().splitlines():
        digest, name = line.split("  ")
        schema = name.removesuffix(".json").replace("_v", "/")
        assert c.record_hash(EXAMPLES[schema]) == digest, f"{schema}: canonical hash drifted"


def test_with_hash_and_verify():
    passport = EXAMPLES["passport/1"]
    c.verify_hash(passport)
    tampered = dataclasses.replace(passport, hypothesis="something else")
    with pytest.raises(c.IntegrityError) as e:
        c.verify_hash(tampered)
    assert e.value.code is c.ErrorCode.INTEGRITY_MISMATCH


def test_the_hash_field_is_excluded_from_its_own_hash():
    passport = EXAMPLES["passport/1"]
    assert c.record_hash(passport) == c.record_hash(dataclasses.replace(passport, canonical_hash=None))


# ---- canonical form (docs/contracts-v1.md section 3) -------------------------------------------------------------
@pytest.mark.parametrize("value,text", [
    (D("1.50"), "1.5"), (D("100"), "100"), (D("1E+2"), "100"), (D("-0"), "0"), (D("0.000"), "0"),
    (D("0.00012300"), "0.000123"), (D("-12.3400"), "-12.34"), (D("123456789012345678901234567890.123"),
                                                                "123456789012345678901234567890.123"),
])
def test_decimal_spelling(value, text):
    assert canonical.decimal_str(value) == text


@given(st.decimals(allow_nan=False, allow_infinity=False, places=8, min_value=-10**12, max_value=10**12))
def test_decimal_round_trip_is_exact(value):
    assert canonical.parse_decimal(canonical.decimal_str(value)) == value


def test_timestamps_are_utc_z_and_naive_is_refused():
    ny = timezone(timedelta(hours=-5))
    assert canonical.iso_utc(datetime(2026, 11, 20, 9, 30, tzinfo=ny)) == "2026-11-20T14:30:00Z"
    assert canonical.iso_utc(datetime(2026, 11, 20, 9, 30, 0, 5, tzinfo=UTC)) == "2026-11-20T09:30:00.000005Z"
    assert canonical.parse_utc("2026-11-20T09:30:00-05:00") == datetime(2026, 11, 20, 14, 30, tzinfo=UTC)
    with pytest.raises(c.ValidationError):
        canonical.iso_utc(datetime(2026, 11, 20, 9, 30))
    with pytest.raises(c.ValidationError):
        canonical.parse_utc("2026-11-20T09:30:00")


def test_floats_nan_and_huge_ints_are_refused():
    for bad in (1.5, float("nan"), {"x": 0.1}, [1.0], 2**53):
        with pytest.raises(c.ValidationError):
            canonical.canonical_bytes(bad)
    for bad in (D("NaN"), D("Infinity")):
        with pytest.raises(c.ValidationError):
            canonical.decimal_str(bad)


def test_keys_sorted_compact_utf8():
    expected = '{"a":"é","b":1,"c":[true,null]}'.encode()
    assert canonical.canonical_bytes({"b": 1, "a": "é", "c": [True, None]}) == expected


def test_decimal_from_float_is_bounded_and_refuses_non_finite():
    assert canonical.decimal_from_float(0.1 + 0.2) == D("0.300000000000")
    assert canonical.decimal_from_float(float("inf")) is None
    assert canonical.decimal_from_float(float("nan")) is None


# ---- strict parsing of untrusted input ---------------------------------------------------------------------------
def _bar_json(**changes):
    data = json.loads(c.to_json(bar()))
    data.update(changes)
    return json.dumps(data)


@pytest.mark.parametrize("text,needle", [
    (_bar_json(extra="x"), "unknown key"),
    (_bar_json(schema_version="bar/2"), "not supported"),
    ('{"schema_version": "bar/1", "open": 1.5}', "float literal"),
    ('{"a": 1, "a": 2}', "duplicate key"),
    ('{"x": NaN}', "refused"),
    ("not json", "not valid JSON"),
    (_bar_json(end="2026-11-19T21:00:00"), "naive"),
    (_bar_json(volume=12), None),                 # an int is an exact decimal: accepted
    (_bar_json(complete="yes"), "true or false"),
    (_bar_json(interval="2d"), "is not one of"),
])
def test_strict_parsing(text, needle):
    if needle is None:
        c.from_json(c.Bar, text)
        return
    with pytest.raises(c.ValidationError) as e:
        c.from_json(c.Bar, text)
    assert needle in str(e.value)


def test_missing_required_key():
    data = json.loads(c.to_json(bar()))
    del data["close"]
    with pytest.raises(c.ValidationError, match="missing required key 'close'"):
        c.from_dict(c.Bar, data)


def test_unknown_schema_version_is_its_own_code():
    with pytest.raises(c.ValidationError) as e:
        c.from_json(c.Bar, _bar_json(schema_version="bar/9"))
    assert e.value.code is c.ErrorCode.SCHEMA_UNSUPPORTED


def test_construction_refuses_float_and_naive():
    with pytest.raises(c.ValidationError):
        dataclasses.replace(bar(), close=603.25)
    with pytest.raises(c.ValidationError):
        dataclasses.replace(bar(), end=datetime(2026, 11, 19, 21, 0))


# ---- record invariants -------------------------------------------------------------------------------------------
def test_bar_invariants():
    b = bar()
    with pytest.raises(c.ValidationError, match="impossible OHLC"):
        dataclasses.replace(b, high=D("599"))
    with pytest.raises(c.ValidationError) as e:
        dataclasses.replace(b, available_at=b.end - timedelta(seconds=1))
    assert e.value.code is c.ErrorCode.LOOKAHEAD_RISK
    with pytest.raises(c.ValidationError):
        dataclasses.replace(b, volume=D("-1"))
    with pytest.raises(c.ValidationError):
        dataclasses.replace(b, start=b.end)


def test_scan_says_no_eligible_setup_exactly_when_nothing_qualifies():
    scan = EXAMPLES["scan/1"]
    with pytest.raises(c.ValidationError):
        dataclasses.replace(scan, no_eligible_setup=True)
    none = dataclasses.replace(scan.candidates[0], eligibility=c.Eligibility.NOT_ELIGIBLE)
    dataclasses.replace(scan, candidates=(none,), no_eligible_setup=True)


def test_unknown_exposure_is_never_ticket_eligible():
    ra = EXAMPLES["risk-assessment/1"]
    with pytest.raises(c.ValidationError):
        dataclasses.replace(ra, ticket_eligible=True)


def test_paper_ticket_has_no_broker_field():
    names = {f.name for f in dataclasses.fields(c.PaperTicket)}
    assert not names & {"account", "account_id", "broker", "broker_order_id", "credential", "token", "submit"}


@pytest.mark.parametrize("path", ["/etc/passwd", "../x.json", "a/../b.json", "a//b.json", "C:\\x", "a\\b.json"])
def test_bundle_paths_are_relative_and_safe(path):
    with pytest.raises(c.ValidationError):
        c.BundleEntry(path=path, record_type="passport/1", record_id="x", sha256="0" * 64)


def test_experiment_splits_are_chronological_with_the_holdout_last():
    spec = EXAMPLES["experiment-spec/1"]
    dev, val, hold = spec.splits
    with pytest.raises(c.ValidationError, match="overlap"):
        dataclasses.replace(spec, splits=(dev, dataclasses.replace(val, start=date(2021, 6, 1)), hold))
    with pytest.raises(c.ValidationError, match="holdout"):
        dataclasses.replace(spec, splits=(dataclasses.replace(hold, start=date(2010, 1, 4), end=date(2014, 12, 31)),
                                          dev, val))


def test_a_metric_is_a_value_or_a_reason_never_both_never_neither():
    with pytest.raises(c.ValidationError):
        c.MetricValue(value=None, unit="ratio")
    with pytest.raises(c.ValidationError):
        c.MetricValue(value=D("0"), unit="ratio", unavailable_reason="x")


def test_failed_trial_names_its_error():
    with pytest.raises(c.ValidationError):
        dataclasses.replace(EXAMPLES["trial/1"], status=c.TrialStatus.FAILED)


def test_tutorial_is_the_only_synthetic_source():
    with pytest.raises(c.ValidationError):
        dataclasses.replace(EXAMPLES["dataset/1"], source_kind=c.SourceKind.CSV)


def test_a_blocking_issue_cannot_leave_a_dataset_usable():
    m = EXAMPLES["dataset/1"]
    blocking = c.QualityIssue(code="IMPOSSIBLE_OHLC", severity=c.Severity.BLOCKING, detail="x")
    with pytest.raises(c.ValidationError):
        dataclasses.replace(m, quality_issues=(blocking,))
    dataclasses.replace(m, quality_issues=(blocking,), usability=c.DatasetUsability.REJECTED)


# ---- errors ------------------------------------------------------------------------------------------------------
def test_error_codes_are_stable_strings():
    for code in c.ErrorCode:
        assert code.value == code.name
    spec_05_3 = """MISSING_DATA DATA_STALE ENTITLEMENT_DENIED DATA_RIGHTS_UNKNOWN CALENDAR_UNKNOWN INCOMPLETE_BAR
        PIT_UNVERIFIED INVALID_STRATEGY LOOKAHEAD_RISK INSUFFICIENT_EVIDENCE HOLDOUT_COMPROMISED NONREPRODUCIBLE
        OUTSIDE_CAPABILITY UNVERIFIED_DEPENDENCY INTEGRITY_MISMATCH RESOURCE_BUDGET_EXCEEDED INVALID_APPROVAL
        BROKER_INDETERMINATE UNKNOWN_EXPOSURE NO_PROTECTION HALTED NOT_AUTHORIZED MACHINE_CONTRACT_UNVERIFIED""".split()
    assert set(spec_05_3) <= {e.value for e in c.ErrorCode}


def test_an_error_travels_as_a_failure_record():
    err = c.DataError("no bars", code=c.ErrorCode.MISSING_DATA, limitations=("tutorial only",))
    failure = err.to_failure()
    assert c.from_json(c.Failure, c.to_json(failure)) == failure
    assert failure.code is c.ErrorCode.MISSING_DATA


# ---- the public API refuses, typed, until its part lands ---------------------------------------------------------
BUILT = {                                         # functions whose part has landed; each has its own tests
    "load_dataset", "check_dataset", "parse_strategy", "approve_strategy_version", "register_experiment",
    "run_experiment", "falsify_experiment", "build_passport", "search_memory", "assess_hypothetical_plan",
    "export_portable_bundle", "import_portable_bundle",
    "scan_market", "scan_setups", "ring_alerts", "market_context", "assess_entry", "plan_exit",   # part B
    "evaluate_alert",
}


def test_unbuilt_api_functions_refuse_with_capability_disabled():
    import inspect

    from traderos import api
    for name, fn in inspect.getmembers(api, inspect.isfunction):
        if name.startswith("_") or fn.__module__ != api.__name__ or name in BUILT:
            continue
        params = inspect.signature(fn).parameters
        args = [None] * sum(1 for p in params.values() if p.default is p.empty and p.kind is p.POSITIONAL_OR_KEYWORD)
        kwargs = {p.name: None for p in params.values() if p.default is p.empty and p.kind is p.KEYWORD_ONLY}
        try:
            fn(*args, **kwargs)
        except c.CapabilityDisabled as e:
            assert e.code is c.ErrorCode.CAPABILITY_DISABLED and e.details["owner"]
        else:
            pytest.fail(f"api.{name} answered without being built; add it to BUILT with its own tests")


# ---- contracts v1.1 (ADR-009): fields added to a record keep every v1.0 record's bytes and hash -------------------
@pytest.mark.parametrize("schema", ["risk-plan/1", "risk-assessment/1", "paper-ticket/1"])
def test_a_v1_0_record_reads_back_with_its_bytes_and_its_hash(schema):
    old = (GOLDEN / "v1.0" / (schema.replace("/", "_v") + ".json")).read_text(encoding="utf-8").rstrip("\n")
    record = c.from_json(c.RECORD_TYPES[schema], old)
    assert c.to_json(record) == old
    if getattr(record, "canonical_hash", None):
        c.verify_hash(record)
    for name in c.RECORD_TYPES[schema].OMIT_IF_NONE:
        assert getattr(record, name) is None and f'"{name}"' not in old


def test_only_an_added_field_may_be_omitted_when_none():
    for cls in c.RECORD_TYPES.values():
        names = {f.name for f in dataclasses.fields(cls)}
        assert names >= cls.OMIT_IF_NONE, cls
        for f in dataclasses.fields(cls):
            if f.name in cls.OMIT_IF_NONE:
                assert f.default is None, f"{cls.__name__}.{f.name} must default to None"


@pytest.mark.parametrize("text", ["[" * 100_000 + "]" * 100_000, '{"n": ' + "9" * 5000 + "}"])
def test_hostile_json_is_a_validation_error_not_a_crash(text):
    with pytest.raises(c.ValidationError):
        c.from_json(c.Bar, text)
