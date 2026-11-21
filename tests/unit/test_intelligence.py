"""Part B: S01 scan, S03 market pulse, S13 entry timer, S14 exit planner, S18 alerts (spec 12.1, 12.2 A).

What would have to break for this to go red:
* a result reads a bar not yet available at as_of (a scan as of a day equals a scan of the data cut at that day);
* stale, missing, blocked or too-short data qualifies a symbol, or an empty scan invents a candidate;
* a weekend or a holiday makes data look stale, or a missed session does not;
* breadth is reported from too few instruments; a regime is named without the history for it;
* an entry is called eligible while the market is closed, in the opening range, near the close or on stale data;
* an impossible exit plan (stop on the wrong side) is planned instead of refused, or a missing stop is not flagged;
* an alert answers False on stale data or outside a session, or hides a missed window;
* tutorial data is not labelled TUTORIAL_ONLY, or a record does not survive its own hash and JSON round trip.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from tests.unit.fakes import CAL, daily, dataset, intraday
from traderos.contracts import (
    Completeness,
    DataError,
    ErrorCode,
    SafeFor,
    ScanCriteria,
    Severity,
    ValidationError,
    from_json,
    to_json,
    verify_hash,
)
from traderos.contracts.intel import AlertEvent, AlertRule, Eligibility, EntryStatus, ScanResult
from traderos.contracts.market import AvailabilityPolicy, Interval, QualityIssue
from traderos.contracts.risk import Direction
from traderos.intelligence import assess_entry, evaluate_alert, market_context, plan_exit, scan
from traderos.intelligence import pulse as pulse_mod

FRI = date(2026, 11, 20)
SUN_NOON = datetime(2026, 11, 22, 12, tzinfo=UTC)
NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
UP = list(range(100, 160))                        # 60 rising closes
DOWN = list(range(160, 100, -1))


def crit(conditions, universe=("SPY", "QQQ"), **kw):
    return ScanCriteria(criteria_id="CR-1", name="test", interval=kw.pop("interval", Interval.D1),
                        universe=tuple(universe), conditions=conditions, **kw)


ABOVE_SMA20 = {"above": ["close", {"sma": 20}]}


# ---- S01 scan ----------------------------------------------------------------------------------------------------

def test_scan_says_who_qualified_and_why():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI), "QQQ": daily("QQQ", DOWN, last_day=FRI)})
    res = scan(r, CAL, "DS-T", crit(ABOVE_SMA20), as_of=SUN_NOON, now=NOW)
    spy, qqq = res.candidates
    assert spy.eligibility is Eligibility.ELIGIBLE and qqq.eligibility is Eligibility.NOT_ELIGIBLE
    assert spy.traces[0].condition == "close > sma(20)" and spy.traces[0].left.value == D(159)
    assert spy.traces[0].left.unit == spy.traces[0].right.unit == "USD"            # prices in the currency
    assert spy.traces[0].right.value == sum(D(x) for x in UP[-20:]) / 20
    assert spy.last_bar_end == CAL.open_close(FRI)[1]
    assert res.no_eligible_setup is False and res.completeness is Completeness.COMPLETE
    verify_hash(res)
    assert from_json(ScanResult, to_json(res)) == res
    assert res.scan_id == scan(r, CAL, "DS-T", crit(ABOVE_SMA20), as_of=SUN_NOON, now=NOW).scan_id


def test_scan_never_reads_ahead_of_as_of():
    full = daily("SPY", UP, last_day=FRI)
    r, _ = dataset({"SPY": full})
    as_of = full[40].available_at
    cut, _ = dataset({"SPY": full[:41]})
    a = scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",)), as_of=as_of, now=NOW)
    b = scan(cut, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",)), as_of=as_of, now=NOW)
    assert a.candidates == b.candidates and a.candidates[0].last_bar_end == full[40].end


def test_nothing_eligible_is_said_not_invented():
    r, _ = dataset({"SPY": daily("SPY", DOWN, last_day=FRI)})
    res = scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",)), as_of=SUN_NOON, now=NOW)
    assert res.no_eligible_setup is True and res.candidates[0].eligibility is Eligibility.NOT_ELIGIBLE


def test_stale_missing_and_short_data_never_qualify():
    r, _ = dataset({
        "SPY": daily("SPY", UP, last_day=FRI - timedelta(days=4)),                  # Monday's close, read Sunday
        "QQQ": daily("QQQ", UP[:10], last_day=FRI)})                                # too short for sma(20)
    res = scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY", "QQQ", "DIA")), as_of=SUN_NOON, now=NOW)
    by = {c.symbol: c for c in res.candidates}
    assert by["SPY"].eligibility is Eligibility.UNAVAILABLE and "DATA_STALE" in by["SPY"].reasons
    assert by["SPY"].traces, "a stale symbol still shows its computed traces"
    assert by["QQQ"].eligibility is Eligibility.UNAVAILABLE and "MISSING_DATA" in by["QQQ"].reasons
    assert "needs 20 complete bars, has 10" in by["QQQ"].traces[0].reason
    assert by["DIA"].eligibility is Eligibility.UNAVAILABLE and by["DIA"].reasons == ("MISSING_DATA",)
    assert res.no_eligible_setup is True and res.completeness is Completeness.UNAVAILABLE


def test_a_dataset_with_a_blocking_issue_is_refused_by_all_five():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)},
                   quality_issues=(QualityIssue(code="IMPOSSIBLE_OHLC", severity=Severity.BLOCKING,
                                                instrument_id="INS-SPY", detail="row 12"),))
    calls = [lambda: scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",)), as_of=SUN_NOON),
             lambda: market_context(r, CAL, "DS-T", as_of=SUN_NOON),
             lambda: assess_entry(r, CAL, "DS-T", "SPY", as_of=SUN_NOON),
             lambda: plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price="100", as_of=SUN_NOON),
             lambda: evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}), at=SUN_NOON)]
    for call in calls:
        with pytest.raises(DataError) as e:
            call()
        assert "IMPOSSIBLE_OHLC" in e.value.message


def test_a_holiday_and_an_early_close_are_not_staleness():
    # Thursday 11-26 is a holiday and Friday 11-27 closes at 18:00: Wednesday's bar read Thursday night is current
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=date(2026, 11, 25))})
    res = scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",)), as_of=datetime(2026, 11, 26, 23, tzinfo=UTC), now=NOW)
    assert res.candidates[0].eligibility is Eligibility.ELIGIBLE
    res = scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",)), as_of=datetime(2026, 11, 27, 18, 30, tzinfo=UTC),
               now=NOW)                                                              # Friday closed early: missed
    assert "DATA_STALE" in res.candidates[0].reasons
    # Friday's bar read Monday night is one SESSION behind (not three days): allowed when two bars of lag are
    r2, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    mon_night = datetime(2026, 11, 23, 22, tzinfo=UTC)
    res = scan(r2, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",), max_staleness_bars=2), as_of=mon_night, now=NOW)
    assert res.candidates[0].eligibility is Eligibility.ELIGIBLE
    res = scan(r2, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",)), as_of=mon_night, now=NOW)
    assert "DATA_STALE" in res.candidates[0].reasons


def test_a_running_bar_is_not_scanned():
    bars = daily("SPY", [*UP[:-1], 1], last_day=FRI, complete_last=False)           # the live bar would fail
    r, _ = dataset({"SPY": bars})
    res = scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",)), as_of=SUN_NOON, now=NOW)
    assert res.candidates[0].last_bar_end == bars[-2].end
    assert "DATA_STALE" in res.candidates[0].reasons                                # Friday's bar is not done


def test_labels_and_refusals():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)}, synthetic=True,
                   availability=AvailabilityPolicy.INTERVAL_END, feed="iex")
    res = scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",)), as_of=SUN_NOON, now=NOW)
    assert res.safe_for == (SafeFor.TUTORIAL_ONLY,)
    assert {"SYNTHETIC_DATA", "PIT_LIMITED", "OUTSIDE_CAPABILITY"} <= {x.code for x in res.limitations}
    with pytest.raises(ValidationError):
        scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",), interval=Interval.H1), as_of=SUN_NOON)
    with pytest.raises(ValidationError) as e:
        scan(r, CAL, "DS-T", crit({"bars_held": 3}, ("SPY",)), as_of=SUN_NOON)
    assert e.value.code is ErrorCode.INVALID_STRATEGY


# ---- S03 market pulse ------------------------------------------------------------------------------------------

def test_pulse_moves_regime_and_honest_breadth():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI), "QQQ": daily("QQQ", DOWN, last_day=FRI)})
    mc = market_context(r, CAL, "DS-T", as_of=SUN_NOON, now=NOW)
    spy, qqq = mc.moves
    assert spy.change_pct.value == (D(159) / D(158) - 1).quantize(D("1e-10")) and spy.change_pct.unit == "fraction"
    assert qqq.change_pct.value < 0 and not spy.stale
    assert mc.breadth.value is None and "breadth needs at least 20" in mc.breadth.unavailable_reason
    assert mc.regime.label == "uptrend" and mc.regime.inputs["sma_50"].value is not None
    verify_hash(mc)


def test_breadth_with_enough_coverage_and_regimes():
    series = {f"S{i:02}": daily(f"S{i:02}", UP if i % 4 else DOWN, last_day=FRI) for i in range(20)}
    series["SPY"] = daily("SPY", DOWN, last_day=FRI)
    r, _ = dataset(series)
    mc = market_context(r, CAL, "DS-T", as_of=SUN_NOON, now=NOW)
    assert mc.breadth.value == D("0.7143")                                          # 15 of 21 rose
    assert mc.regime.label == "downtrend"
    short, _ = dataset({"SPY": daily("SPY", UP[:30], last_day=FRI)})
    assert market_context(short, CAL, "DS-T", as_of=SUN_NOON, now=NOW).regime.label == "unknown"
    none_, _ = dataset({"XLF": daily("XLF", UP, last_day=FRI)})
    assert market_context(none_, CAL, "DS-T", as_of=SUN_NOON, now=NOW).regime.label == "unknown"
    ranged = [*UP[:59], 100]                                # sma(50) still rising, close fallen below it
    r3, _ = dataset({"SPY": daily("SPY", ranged, last_day=FRI)})
    assert market_context(r3, CAL, "DS-T", as_of=SUN_NOON, now=NOW).regime.label == "range"
    assert pulse_mod.BREADTH_MIN == 20


def test_pulse_flags_stale():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI - timedelta(days=2))})
    mc = market_context(r, CAL, "DS-T", as_of=SUN_NOON, now=NOW)
    assert mc.moves[0].stale and mc.completeness is Completeness.UNAVAILABLE


# ---- S13 entry timer -------------------------------------------------------------------------------------------

MON = date(2026, 11, 23)


def five_min(day=MON, n=24):
    return intraday("SPY", day, [100 + i / 10 for i in range(n)])


def test_entry_closed_market_says_when_it_opens():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    e = assess_entry(r, CAL, "DS-T", "SPY", as_of=SUN_NOON)
    assert e.status is EntryStatus.NOT_ELIGIBLE and e.session_open is False
    assert e.invalidation_reasons == ("the market is closed; the next session opens 2026-11-23T14:30:00+00:00",)
    assert e.reference_price.value == D(159)
    assert e.dataset_id == "DS-T" and e.dataset_hash and e.created_at.tzinfo is not None
    verify_hash(e)


def test_entry_opening_range_close_buffer_and_fresh():
    r, _ = dataset({"SPY": five_min(n=80)}, interval=Interval.M5)
    o = CAL.open_close(MON)[0]
    early = assess_entry(r, CAL, "DS-T", "SPY", as_of=o + timedelta(minutes=20))
    assert early.status is EntryStatus.NOT_ELIGIBLE and "opening range incomplete until 15:00 UTC" in \
        early.invalidation_reasons
    ok = assess_entry(r, CAL, "DS-T", "SPY", as_of=o + timedelta(minutes=62))
    assert ok.status is EntryStatus.ELIGIBLE and ok.completeness is Completeness.COMPLETE
    assert ok.reference_price.value == D("101.1")                                   # bar 11 ended 15:30
    late = assess_entry(r, CAL, "DS-T", "SPY", as_of=CAL.open_close(MON)[1] - timedelta(minutes=10))
    assert late.status is not EntryStatus.ELIGIBLE                                  # stale data and the close


def test_entry_stale_and_early_close():
    r, _ = dataset({"SPY": five_min(n=12)}, interval=Interval.M5)                   # data stops at 15:30
    o = CAL.open_close(MON)[0]
    stale = assess_entry(r, CAL, "DS-T", "SPY", as_of=o + timedelta(hours=2))
    assert stale.status is EntryStatus.UNAVAILABLE and stale.invalidation_reasons[-1].startswith("DATA_STALE")
    fri = date(2026, 11, 27)
    r2, _ = dataset({"SPY": intraday("SPY", fri, [100] * 36)}, interval=Interval.M5)
    e = assess_entry(r2, CAL, "DS-T", "SPY", as_of=datetime(2026, 11, 27, 17, 50, tzinfo=UTC))
    assert "early close today at 18:00 UTC" in e.constraints
    assert any("to the close at 18:00 UTC" in x for x in e.invalidation_reasons)


def test_entry_on_daily_bars_says_it_cannot_time_inside_a_session():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    e = assess_entry(r, CAL, "DS-T", "SPY", as_of=datetime(2026, 11, 23, 16, tzinfo=UTC))
    assert "daily bars: timing inside the session is not assessed" in e.constraints
    assert "DAILY_BARS" in {x.code for x in e.limitations}


# ---- S14 exit planner ------------------------------------------------------------------------------------------

def test_exit_geometry_is_refused_not_repaired():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    bad = [dict(direction="long", entry_price="100", stop_price="101"),
           dict(direction="long", entry_price="100", target_price="99"),
           dict(direction="long", entry_price="100", stop_price="100"),
           dict(direction="short", entry_price="100", stop_price="99"),
           dict(direction="short", entry_price="100", target_price="101"),
           dict(direction="buy", entry_price="100"),
           dict(direction="long", entry_price="-1"),
           dict(direction="long", entry_price="1e9999")]
    for kw in bad:
        with pytest.raises(ValidationError):
            plan_exit(r, CAL, "DS-T", "SPY", as_of=SUN_NOON, **kw)


def test_long_exit_scenarios():
    closes = UP[:]
    gaps = {50: "145.5"}                                                             # a 3% gap down at bar 50
    r, _ = dataset({"SPY": daily("SPY", closes, last_day=FRI, gaps=gaps)})
    p = plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price="150", stop_price="145",
                  target_price="160", as_of=SUN_NOON)
    by = {s.name: s for s in p.scenarios}
    assert by["stop"].pnl_per_unit.value == D(-5)
    worst = D("145.5") / D(149) - 1
    gapped = by["gap through stop"]
    assert gapped.price.value == (D(145) * (1 + worst.quantize(D("1e-10")))).quantize(D("0.01")).normalize()
    assert gapped.pnl_per_unit.value < D(-5)
    assert by["target"].pnl_per_unit.value == D(10) and "2.00R against the stop" in by["target"].assumptions[0]
    assert by["exit at the last close"].pnl_per_unit.value == D(9)
    assert by["one ATR against"].price.value is not None
    assert p.completeness is Completeness.COMPLETE
    assert any("not a guaranteed price" in x for x in p.risks)
    assert p.direction is Direction.LONG and p.dataset_id == "DS-T" and by["stop"].pnl_per_unit.unit == "USD"
    verify_hash(p)
    assert plan_exit(r, CAL, "DS-T", "SPY", direction=Direction.LONG, entry_price=D(150), stop_price=D(145),
                     as_of=SUN_NOON).direction is Direction.LONG


def test_short_exit_and_no_stop():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    p = plan_exit(r, CAL, "DS-T", "SPY", direction="short", entry_price="160", target_price="150", as_of=SUN_NOON)
    by = {s.name: s for s in p.scenarios}
    assert by["target"].pnl_per_unit.value == D(10) and by["exit at the last close"].pnl_per_unit.value == D(1)
    assert "stop" not in by and any(x.startswith("NO_PROTECTION") for x in p.risks)
    short, _ = dataset({"SPY": daily("SPY", UP[:5], last_day=FRI)})
    p = plan_exit(short, CAL, "DS-T", "SPY", direction="long", entry_price="100", stop_price="99", as_of=SUN_NOON)
    assert p.completeness is Completeness.PARTIAL
    assert "needs 15 complete bars" in {s.name: s for s in p.scenarios}["one ATR against"].price.unavailable_reason


# ---- S18 alerts ------------------------------------------------------------------------------------------------

def rule(conditions, **kw):
    base = dict(rule_id="AR-1", instrument_id="INS-SPY", symbol="SPY", interval=Interval.M5, conditions=conditions,
                sampling_seconds=60,
                max_staleness_seconds=600, missed_policy="report_missed", created_at=NOW)
    base.update(kw)
    return AlertRule(**base)


def test_alert_triggers_during_a_session_on_fresh_data():
    r, _ = dataset({"SPY": five_min(n=24)}, interval=Interval.M5)
    at = CAL.open_close(MON)[0] + timedelta(minutes=121)                            # bar 23 ended 16:30
    ev = evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "102"]}), at=at)
    assert ev.triggered is True and ev.staleness_seconds == 60 and ev.missed is False
    assert ev.completeness is Completeness.COMPLETE and ev.dataset_id == "DS-T" and ev.provider == "tutorial"
    verify_hash(ev)
    assert ev.traces[0].left.value == D("102.3")
    assert evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "103"]}), at=at).triggered is False
    assert from_json(AlertEvent, to_json(ev)) == ev


def test_alert_never_answers_false_when_it_cannot_see():
    r, _ = dataset({"SPY": five_min(n=24)}, interval=Interval.M5)
    o = CAL.open_close(MON)[0]
    stale = evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}), at=o + timedelta(hours=3))
    assert stale.triggered is None and stale.reasons[-1].startswith("DATA_STALE")
    assert stale.completeness is not Completeness.COMPLETE
    with pytest.raises(ValidationError):
        evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}, interval=Interval.D1), at=o + timedelta(hours=1))
    closed = evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}), at=SUN_NOON)
    assert closed.triggered is None and "outside a running session" in closed.reasons[-1]
    off = evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}, active=False), at=o + timedelta(hours=1))
    assert off.triggered is None and "not active" in off.reasons[-1]
    unknown = evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", {"sma": 50}]}), at=o + timedelta(hours=2))
    assert unknown.triggered is None


def test_alert_reports_a_missed_window_and_never_reads_ahead():
    r, _ = dataset({"SPY": five_min(n=24)}, interval=Interval.M5)
    at = CAL.open_close(MON)[0] + timedelta(minutes=61)                             # bar 11 ended 15:30
    ev = evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "101"]}), at=at, previous_at=at - timedelta(
        minutes=10))
    assert ev.missed is True and ev.reasons[0].startswith("MISSED_WINDOW: 600 s of session time")
    assert ev.previous_evaluated_at == at - timedelta(minutes=10)
    assert ev.data_observed_at == at - timedelta(minutes=1) and ev.traces[0].left.value == D("101.1")
    assert evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}), at=at,
                          previous_at=at - timedelta(seconds=90)).missed is False
    with pytest.raises(ValidationError):
        evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}, missed_policy="ignore"), at=at)
    with pytest.raises(ValidationError):
        evaluate_alert(r, CAL, "DS-T", rule({"bars_held": 2}), at=at)
    with pytest.raises(ValidationError):
        evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}), at=at.replace(tzinfo=None))


def test_no_part_b_module_can_reach_an_order_or_the_network():
    import ast
    import pathlib

    import traderos.intelligence as pkg
    forbidden = {"socket", "urllib", "http", "requests", "subprocess", "traderos.trading"}
    for path in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for n in names:
                assert not any(n == f or n.startswith(f + ".") for f in forbidden), (path.name, n)
    _ = dataclasses


# ---- OSDev4's review of #5: each item, and each mutation that survived it ----------------------------------------

TUE = date(2026, 11, 24)


def test_review_1_exit_plan_checks_freshness():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=date(2026, 9, 1))})
    p = plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price="150", stop_price="145", as_of=SUN_NOON)
    last = {s.name: s for s in p.scenarios}["exit at the last close"]
    assert p.completeness is Completeness.PARTIAL and last.price.value is None
    assert last.price.unavailable_reason.startswith("DATA_STALE") and "DATA_STALE" in {x.code for x in p.limitations}


def test_review_2_gap_through_stop_uses_session_gaps_not_bar_gaps():
    # Monday's five-minute bars at 100, Tuesday opens at 95: a 5% overnight gap. Bar-to-bar gaps are all 0.
    bars = [*intraday("SPY", MON, [100] * 78), *intraday("SPY", TUE, [95] * 12)]
    r, _ = dataset({"SPY": bars}, interval=Interval.M5)
    at = CAL.open_close(TUE)[0] + timedelta(minutes=61)
    p = plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price="96", stop_price="94", as_of=at)
    gap = {s.name: s for s in p.scenarios}["gap through stop"]
    assert gap.price.value == D("89.3"), gap                                       # 94 x 0.95, not 93.99


def test_review_3_regime_checks_the_benchmark_and_keeps_history_past_a_running_bar():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=date(2026, 10, 1))})
    mc = market_context(r, CAL, "DS-T", as_of=SUN_NOON, now=NOW)
    assert mc.regime.label == "unknown" and mc.regime.inputs["close"].unavailable_reason.startswith("DATA_STALE")
    live = daily("SPY", [*UP, 1], last_day=FRI, complete_last=False)              # 60 complete + 1 running
    r2, _ = dataset({"SPY": live})
    mc = market_context(r2, CAL, "DS-T", as_of=SUN_NOON, max_staleness_bars=2, now=NOW)
    assert mc.regime.label == "uptrend"


def test_review_4_the_staleness_allowance_is_bounded():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    for bad in (0, 21, 10**9, True):
        with pytest.raises(ValidationError):
            market_context(r, CAL, "DS-T", as_of=SUN_NOON, max_staleness_bars=bad)
        with pytest.raises(ValidationError):
            assess_entry(r, CAL, "DS-T", "SPY", as_of=SUN_NOON, max_staleness_bars=bad)
        with pytest.raises(ValidationError):
            plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price="100", as_of=SUN_NOON,
                      max_staleness_bars=bad)
        with pytest.raises(ValidationError):
            scan(r, CAL, "DS-T", crit(ABOVE_SMA20, ("SPY",), max_staleness_bars=bad), as_of=SUN_NOON)


def test_review_5_an_alert_reads_its_instrument_id_never_a_ticker():
    r, _ = dataset({"SPY": five_min(n=24)}, interval=Interval.M5)
    at = CAL.open_close(MON)[0] + timedelta(minutes=121)
    with pytest.raises(DataError) as e:
        evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}, instrument_id="INS-ELSEWHERE"), at=at)
    assert e.value.code is ErrorCode.MISSING_DATA
    with pytest.raises(ValidationError):
        evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}, symbol="QQQ"), at=at)


def test_review_6_missed_windows_count_session_time_only():
    fri = date(2026, 11, 20)
    r, _ = dataset({"SPY": [*intraday("SPY", fri, [100] * 78), *five_min(n=24)]}, interval=Interval.M5)
    friday_close = CAL.open_close(fri)[1]
    monday = CAL.open_close(MON)[0] + timedelta(minutes=4)
    ev = evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}, sampling_seconds=300), at=monday,
                        previous_at=friday_close - timedelta(minutes=1))
    assert ev.missed is False                                                      # 60 s + 240 s of session
    ev = evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}, sampling_seconds=300),
                        at=monday + timedelta(minutes=10), previous_at=friday_close - timedelta(minutes=1))
    assert ev.missed is True
    with pytest.raises(ValidationError):
        evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "1"]}), at=monday, previous_at=monday + timedelta(1))


def test_review_7_a_nan_or_infinite_price_is_a_typed_refusal():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    for bad in (D("NaN"), D("Infinity"), D("-Infinity"), "NaN"):
        with pytest.raises(ValidationError):
            plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price=bad, as_of=SUN_NOON)


def test_review_8_low_items():
    # daily bars inside a running session: eligible at most, never a complete answer
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    e = assess_entry(r, CAL, "DS-T", "SPY", as_of=datetime(2026, 11, 23, 16, tzinfo=UTC), max_staleness_bars=2)
    assert e.completeness is Completeness.PARTIAL
    # a daily bar stamped at midnight UTC belongs to the session before it, not to the next day
    from traderos.intelligence._common import bars_behind
    wed_close = CAL.open_close(date(2026, 11, 25))[1]
    assert bars_behind(CAL, Interval.D1, datetime(2026, 11, 25, 0, 0, tzinfo=UTC), wed_close) == 1
    # off-tick prices are refused; computed prices round against the position
    with pytest.raises(ValidationError):
        plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price="150.005", as_of=SUN_NOON)


def test_survivor_breadth_needs_twenty_and_flat_is_not_rising():
    def market(n_up, n_flat, n_down):
        series = {}
        for i in range(n_up + n_flat + n_down):
            tail = [159] if i < n_up else [158] if i < n_up + n_flat else [157]
            series[f"S{i:02}"] = daily(f"S{i:02}", [*UP[:-1], *tail], last_day=FRI)
        return market_context(dataset(series)[0], CAL, "DS-T", as_of=SUN_NOON, now=NOW).breadth
    assert market(19, 0, 0).value is None                                          # 19 is not enough
    assert market(10, 5, 5).value == D("0.5")                                      # exactly 20: flat is not rising


def test_survivor_one_atr_against_both_sides():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    long_ = {s.name: s for s in plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price="150",
                                          as_of=SUN_NOON).scenarios}["one ATR against"]
    short = {s.name: s for s in plan_exit(r, CAL, "DS-T", "SPY", direction="short", entry_price="150",
                                          as_of=SUN_NOON).scenarios}["one ATR against"]
    assert long_.price.value < 150 < short.price.value
    assert long_.pnl_per_unit.value < 0 and short.pnl_per_unit.value < 0


def test_survivor_worst_gap_both_sides_and_the_lookback():
    # opens: -1% at bar 40 and -3% at bar 50 (down gaps); +2% at bar 45 and +4% at bar 55 (up gaps);
    # a -20% gap at bar 2 is older than the 60-session lookback once 80 sessions exist
    closes = list(range(100, 180))
    opens = {2: closes[1] * 0.8, 40: closes[39] * 0.99, 50: closes[49] * 0.97, 45: closes[44] * 1.02,
             55: closes[54] * 1.04}
    r, _ = dataset({"SPY": daily("SPY", closes, last_day=FRI, gaps={k: f"{v:.2f}" for k, v in opens.items()})})
    long_ = plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price="180", stop_price="170", as_of=SUN_NOON)
    gap = {s.name: s for s in long_.scenarios}["gap through stop"]
    worst_down = D(f"{opens[50]:.2f}") / D(closes[49]) - 1
    assert gap.price.value == (D(170) * (1 + worst_down)).quantize(D("0.01"), rounding="ROUND_FLOOR")
    assert "60 sessions" in gap.assumptions[0]
    short = plan_exit(r, CAL, "DS-T", "SPY", direction="short", entry_price="170", stop_price="180",
                      as_of=SUN_NOON)
    up = {s.name: s for s in short.scenarios}["gap through stop"]
    worst_up = D(f"{opens[55]:.2f}") / D(closes[54]) - 1
    assert up.price.value == (D(180) * (1 + worst_up)).quantize(D("0.01"), rounding="ROUND_CEILING")


def test_survivor_regime_close_equal_to_its_sma_is_not_above():
    closes = [*UP[:59], 134]                     # sma(50) of the last 50 = (110..158 + 134) / 50 = 134 = close
    r, _ = dataset({"SPY": daily("SPY", closes, last_day=FRI)})
    mc = market_context(r, CAL, "DS-T", as_of=SUN_NOON, now=NOW)
    assert mc.regime.inputs["sma_50"].value == D(134) and mc.regime.label == "range"


def test_survivor_staleness_is_exact_by_one_session():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    mon_night = datetime(2026, 11, 23, 22, tzinfo=UTC)                               # one session behind
    assert market_context(r, CAL, "DS-T", as_of=mon_night, now=NOW).moves[0].stale is True
    assert market_context(r, CAL, "DS-T", as_of=mon_night, max_staleness_bars=2, now=NOW).moves[0].stale is False
    assert assess_entry(r, CAL, "DS-T", "SPY", as_of=mon_night).status is EntryStatus.UNAVAILABLE
    assert assess_entry(r, CAL, "DS-T", "SPY", as_of=mon_night, max_staleness_bars=2).status is not \
        EntryStatus.UNAVAILABLE


def test_session_gaps_keeps_only_the_newest_sessions_and_skips_a_missing_one():
    from traderos.intelligence._common import session_gaps
    bars = daily("SPY", [100, 101, 102, 103, 104], last_day=FRI, gaps={1: "99", 2: "103", 3: "100", 4: "105"})
    got = session_gaps(CAL, bars, 2)
    assert got == [D("100") / D("102") - 1, D("105") / D("103") - 1]                # the newest two only
    assert len(session_gaps(CAL, bars, 60)) == 4
    assert len(session_gaps(CAL, [bars[0], bars[2]], 60)) == 0                      # a session missing between


# ---- boundaries the re-measured mutants found unpinned (python -B; see #13's ADR-008 note) ----------------------

def test_boundary_a_shared_ticker_is_not_identity():
    from traderos.intelligence._common import find_instrument
    _, m = dataset({"SPY": daily("SPY", UP, last_day=FRI), "SPX": daily("SPX", UP, last_day=FRI)})
    twin = dataclasses.replace(m, instruments=(m.instruments[0], dataclasses.replace(m.instruments[1], symbol="SPY")))
    assert find_instrument(twin, "SPY") is None                    # two instruments call themselves SPY: neither
    assert find_instrument(twin, "INS-SPX").symbol == "SPY"         # an instrument_id still resolves


def test_boundary_staleness_clock_edges():
    from traderos.intelligence._common import expected_last_end, is_stale, last_close_at_or_before
    fri_close = CAL.open_close(FRI)[1]
    assert last_close_at_or_before(CAL, fri_close) == (FRI, fri_close)          # at the close, that close
    o = CAL.open_close(MON)[0]
    assert expected_last_end(CAL, Interval.M5, o + timedelta(minutes=5)) == o + timedelta(minutes=5)
    mon_night = datetime(2026, 11, 23, 22, tzinfo=UTC)
    assert is_stale(CAL, Interval.D1, fri_close, mon_night, 1) == (True, 1)    # one session behind, allowed 1
    assert is_stale(CAL, Interval.D1, fri_close, mon_night, 2) == (False, 1)


def test_boundary_entry_opening_range_and_close_buffer_edges():
    r, _ = dataset({"SPY": five_min(n=78)}, interval=Interval.M5)
    o, c = CAL.open_close(MON)
    at_30 = assess_entry(r, CAL, "DS-T", "SPY", as_of=o + timedelta(minutes=30))
    assert not any("opening range" in x for x in at_30.invalidation_reasons)    # complete at exactly 30 minutes
    at_29 = assess_entry(r, CAL, "DS-T", "SPY", as_of=o + timedelta(minutes=29), max_staleness_bars=2)
    assert any("opening range" in x for x in at_29.invalidation_reasons)
    buffer = assess_entry(r, CAL, "DS-T", "SPY", as_of=c - timedelta(minutes=15))
    assert any("minutes to the close" in x for x in buffer.invalidation_reasons)    # exactly 15 left: inside it
    before = assess_entry(r, CAL, "DS-T", "SPY", as_of=c - timedelta(minutes=16), max_staleness_bars=2)
    assert not any("minutes to the close" in x for x in before.invalidation_reasons)
    twelve = assess_entry(r, CAL, "DS-T", "SPY", as_of=c - timedelta(minutes=12), max_staleness_bars=2)
    assert any("less than 15 minutes" in x for x in twelve.invalidation_reasons)


def test_boundary_exit_prices_and_flat_sessions():
    r, _ = dataset({"SPY": daily("SPY", UP, last_day=FRI)})
    for bad in ({"entry_price": "0"}, {"entry_price": "100", "target_price": "100"}):
        with pytest.raises(ValidationError):
            plan_exit(r, CAL, "DS-T", "SPY", direction="long", as_of=SUN_NOON, **{"entry_price": "100", **bad})
    p = plan_exit(r, CAL, "DS-T", "SPY", direction="long", entry_price="150", stop_price="140", as_of=SUN_NOON)
    gap = {s.name: s for s in p.scenarios}["gap through stop"]
    assert gap.price.value is None and "no adverse session gap" in gap.price.unavailable_reason   # 0 is not adverse


def test_boundary_alert_missed_window_and_staleness_edges():
    r, _ = dataset({"SPY": five_min(n=24)}, interval=Interval.M5)
    at = CAL.open_close(MON)[0] + timedelta(minutes=121)                            # bar 23 ended 60 s before
    two = evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "102"]}), at=at,
                         previous_at=at - timedelta(seconds=120))
    assert two.missed is False                                                      # exactly 2 periods: not missed
    assert evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "102"]}), at=at,
                          previous_at=at - timedelta(seconds=121)).missed is True
    assert evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "102"]}, max_staleness_seconds=60),
                          at=at).triggered is True                                  # exactly the allowance: fresh
    assert evaluate_alert(r, CAL, "DS-T", rule({"above": ["close", "102"]}, max_staleness_seconds=59),
                          at=at).triggered is None


def test_boundary_a_flat_average_is_not_rising():
    closes = [100 + i for i in range(10)] * 6                       # every 50-close window averages 104.5
    r, _ = dataset({"SPY": daily("SPY", closes, last_day=FRI)})
    mc = market_context(r, CAL, "DS-T", as_of=SUN_NOON, now=NOW)
    assert mc.regime.inputs["sma_slope"].value == 0 and mc.regime.label == "range"     # above, but not rising
