"""The upstream price alerts, ported (src/traderos/intelligence/alert_rules.py; trader-alerts/alerts.py).

The upstream cases from tank/day-trader-skill/tests/test_alerts.py, on stored prices: a target rings once, a stop
re-arms when the price crosses back, pct_move from the previous close once a day, the opening bell once per trading day,
a time alert once per trading day that moves with an early close, date-scoped plan rules. Plus what OSDev4's review of
#13 asked for: a new plan's alerts ring although an earlier plan's rang; a rule that cannot be checked is UNKNOWN, never
"nothing hit"; a price rule names its instrument_id; a stop crossed and recovered between checks rings; the state file
is checked; an edited plan is refused; a price-only plan still gets its flat-by alert.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from tests.unit.fakes import CAL, dataset, intraday
from traderos.contracts import DataError, ValidationError
from traderos.contracts.market import Interval
from traderos.intelligence.alert_rules import (
    Quote,
    check,
    evaluate,
    market_status,
    quotes,
    reset,
    rules_from_plan,
    validate,
    validate_state,
)
from traderos.intelligence.exit import plan_exit
from traderos.intelligence.triggers import ny_date

IID = "INS-AAPL"


def ny(text):
    """A New York wall-clock time in November/December 2026 (standard time, UTC-5)."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC) + timedelta(hours=5)


def at(text, rules, prices=None, state=None):
    now = ny(text)
    out, st = evaluate(rules, prices or {}, market_status(CAL, now), state or {}, now)
    return [o.rang for o in out if o.rang], st


def q(price, prev="100", low=None, high=None):
    """A quote whose newest bar (ending 10:00 New York) traded from `low` to `high`."""
    p = D(price)
    bar = (ny("2026-11-23 09:55"), ny("2026-11-23 10:00"), D(low) if low else p, D(high) if high else p)
    return {IID: Quote(p, D(prev), D(prev), ny("2026-11-23 10:00"), (bar,), ny("2026-11-16 09:30"))}


SEEN = {"checked": {"s": "2026-11-23T14:55:00Z", "t": "2026-11-23T14:55:00Z"}}   # last checked 9:55 New York


def rule(**kw):
    return {"id": "r", "instrument_id": IID, "symbol": "AAPL", **kw}


def test_a_target_rings_once():
    rules = [rule(id="aapl-340", type="above", price=340)]
    rings, st = at("2026-11-23 10:00", rules, q("341.27"))
    assert rings == ["AAPL is $341.27: at or above your alert level $340."]
    assert at("2026-11-23 10:05", rules, q("341.27"), st)[0] == []
    assert at("2026-11-23 10:00", rules, q("339.99"))[0] == []
    assert len(at("2026-11-23 10:00", rules, q("340"))[0]) == 1                   # at the level counts


def test_stop_and_rearm():
    rules = [rule(id="aapl-stop", type="stop", price="54", rearm=True)]
    rings, st = at("2026-11-23 10:00", rules, q("53.9"))
    assert "at or below your stop $54" in rings[0]
    rings, st = at("2026-11-23 10:05", rules, q("55"), st)
    assert rings == [] and "aapl-stop" not in st["rung"]
    assert len(at("2026-11-23 10:10", rules, q("53.5"), st)[0]) == 1
    once = [rule(id="s", type="stop", price="54")]
    _, st = at("2026-11-23 10:00", once, q("53.9"))
    _, st = at("2026-11-23 10:05", once, q("55"), st)
    assert at("2026-11-23 10:10", once, q("53.5"), st)[0] == []                   # no rearm: quiet until reset
    assert len(at("2026-11-23 10:00", once, q("54"))[0]) == 1                     # at the stop counts
    st, cleared, absent = reset(st, ["s", "nope"])
    assert (cleared, absent) == (["s"], ["nope"]) and len(at("2026-11-23 10:15", once, q("53.5"), st)[0]) == 1


def test_a_stop_crossed_and_recovered_between_checks_rings():
    rules = [rule(id="s", type="stop", price="54")]
    rings, _ = at("2026-11-23 10:30", rules, q("55", low="53.80"), SEEN)
    assert rings == ["AAPL traded $53.80 since 2026-11-23T14:55:00+00:00: at or below your stop $54; it is $55.00 "
                     "now."]
    assert at("2026-11-23 10:30", [rule(id="t", type="target", price="56")], q("55", high="56"), SEEN)[0] != []


def test_a_rules_first_check_counts_only_the_price_now():
    """Review of #13, N1: a bar from before a rule was watched is never a touch."""
    rules = [rule(id="s", type="stop", price="54")]
    rings, st = at("2026-11-23 10:30", rules, q("55", low="53.80"))                # first check: no window yet
    assert rings == [] and st["checked"]["s"] == "2026-11-23T15:30:00Z"
    planned = [rule(id="s", type="stop", price="54", watch_from="2026-11-23T15:05:00Z")]
    assert at("2026-11-23 10:30", planned, q("55", low="53.80"))[0] == []           # the bar ended before the plan
    early = [rule(id="s", type="stop", price="54", watch_from="2026-11-23T14:50:00Z")]
    assert len(at("2026-11-23 10:30", early, q("55", low="53.80"))[0]) == 1         # it ended after: a touch


def test_pct_move_from_the_previous_close_once_a_day():
    down = q("98.86")                                                             # -1.14% from 100
    assert at("2026-11-23 10:00", [rule(id="b3", type="pct_move", pct=3)], down)[0] == []
    small = [rule(id="b1", type="pct_move", pct=1, direction="down")]
    rings, st = at("2026-11-23 10:00", small, down)
    assert rings == ["AAPL moved -1.14% to $98.86 (from $100.00)."]
    assert at("2026-11-23 15:00", small, down, st)[0] == []
    assert len(at("2026-11-24 10:00", small, down, st)[0]) == 1                   # a new day
    assert len(at("2026-11-23 10:00", small, q("99"))[0]) == 1                     # exactly 1%: counts
    _, late = at("2026-11-23 20:00", small, down)                                   # 01:00 UTC on the 24th
    assert len(at("2026-11-24 10:00", small, down, late)[0]) == 1                   # a new New York day
    assert at("2026-11-23 10:00", [rule(id="u", type="pct_move", pct=1, direction="up")], down)[0] == []
    assert "(from $97.00)" in at("2026-11-23 10:00", [rule(id="r", type="pct_move", pct=1, ref="97")], down)[0][0]
    no_prev = {IID: dataclasses.replace(down[IID], prev_close=None, session_open=D("99.5"))}
    half = [rule(id="h", type="pct_move", pct="0.5")]
    assert "(from $99.50)" in at("2026-11-23 10:00", half, no_prev)[0][0]         # The upstream fallback: the open


def test_the_opening_bell_once_per_trading_day():
    rules = [{"id": "bell", "type": "opening_bell"}]
    assert at("2026-11-23 09:29", rules)[0] == []
    rings, st = at("2026-11-23 09:30", rules)
    assert rings == ["Opening bell: the NYSE is open (09:30 New York)."]
    assert at("2026-11-23 11:00", rules, state=st)[0] == []
    assert len(at("2026-11-24 09:31", rules, state=st)[0]) == 1
    assert "early close today at 13:00" in at("2026-11-27 09:31", rules)[0][0]
    assert at("2026-11-26 10:00", rules)[0] == []                                  # Thanksgiving


def test_a_time_alert_rings_once_and_moves_with_an_early_close():
    flat = [{"id": "flat", "type": "time", "at": "15:55", "label": "flat by close", "before_close": True}]
    assert at("2026-11-27 12:54", flat)[0] == []
    rings, _ = at("2026-11-27 12:55", flat)
    assert "set for 12:55), the market closes at 13:00" in rings[0]
    assert at("2026-11-27 12:55", [{"id": "t", "type": "time", "at": "15:55", "label": "check"}])[0] == []
    assert at("2026-11-28 15:56", flat)[0] == []                                   # Saturday: no session
    assert at("2026-11-26 15:56", flat)[0] == []                                   # Thanksgiving
    rings, st = at("2026-12-01 15:55", flat)
    assert len(rings) == 1 and st["rung"]["flat"] == "2026-12-01T20:55:00Z"
    assert at("2026-12-01 15:59", flat, state=st)[0] == []
    assert "the market has closed" in at("2026-12-01 16:10", flat)[0][0]


def test_rules_and_state_are_validated_not_guessed():
    for bad in ({"id": "x", "type": "moon"}, {"id": "x", "type": "above", "instrument_id": IID},
                {"id": "x", "type": "time", "at": "4pm"}, rule(type="above", price=1.5), rule(type="above", price="0"),
                rule(type="pct_move", pct=1, direction="sideways"), {"id": "x", "type": "opening_bell", "date": "Nov"},
                {"id": "x", "type": "above", "symbol": "AAPL", "price": 1},          # a ticker alone is not identity
                rule(type="above", price=1, rearm="yes"), rule(type="above", price=1, instrument_id="")):
        with pytest.raises(ValidationError):
            validate(bad)
    for bad in ({"rung": {"x": 5}}, {"rung": []}, {"rung": {}, "last_check": "yesterday"}, {"other": 1}, []):
        with pytest.raises(ValidationError):
            validate_state(bad)


def test_ny_date_is_exact_across_dst():
    assert ny_date(datetime(2026, 11, 23, 4, 59, tzinfo=UTC)) == date(2026, 11, 22)        # 23:59 EST
    assert ny_date(datetime(2026, 11, 23, 5, tzinfo=UTC)) == date(2026, 11, 23)
    assert ny_date(datetime(2026, 7, 6, 3, 59, tzinfo=UTC)) == date(2026, 7, 5)            # 23:59 EDT
    assert ny_date(datetime(2026, 7, 6, 4, tzinfo=UTC)) == date(2026, 7, 6)


# ---- on stored data ---------------------------------------------------------------------------------------------

MON, FRI_BEFORE = date(2026, 11, 23), date(2026, 11, 20)


def store(series=None):
    bars = series or [*intraday("AAPL", FRI_BEFORE, [100] * 77 + [D("101.5")]),
                      *intraday("AAPL", MON, [100, 102, 103])]
    r, _ = dataset({"AAPL": bars}, interval=Interval.M5)
    return r


def test_prices_come_from_the_newest_close_and_the_previous_session():
    r = store()
    now = CAL.open_close(MON)[0] + timedelta(minutes=16)
    got, missing = quotes(r, CAL, "DS-T", [IID], now)
    assert (got[IID].price, got[IID].prev_close, got[IID].session_open) == (D(103), D("101.5"), D(100))
    with pytest.raises(DataError, match="ticker is not identity"):
        quotes(r, CAL, "DS-T", [IID, "INS-MSFT"], now)                             # as `alerts check` refuses it
    got, missing = quotes(r, CAL, "DS-T", [IID], now + timedelta(minutes=30))      # 6 bars behind
    assert got == {} and missing[IID].startswith("DATA_STALE")                    # no level rings on old data


def test_check_gives_one_typed_event_per_rule_and_unknown_is_not_quiet():
    r = store()
    now = CAL.open_close(MON)[0] + timedelta(minutes=46)                           # 6 bars behind: stale
    rules = [rule(id="s", type="stop", price="50"), {"id": "bell", "type": "opening_bell"}]
    events, _ = check(rules, at=now, state={}, calendar=CAL, reader=r, dataset_id="DS-T")
    stop, bell = events
    assert stop.triggered is None and stop.completeness.value == "unavailable"
    assert stop.reasons[0].startswith("DATA_STALE")
    assert bell.triggered is True and bell.reasons[0].startswith("RANG: Opening bell")
    assert stop.dataset_id == "DS-T" and stop.schema_version == "alert-event/1"
    with pytest.raises(ValidationError, match="names dataset"):
        check([rule(type="stop", price="50", dataset_id="DS-OTHER")], at=now, state={}, calendar=CAL, reader=r,
              dataset_id="DS-T")


def test_a_bar_since_the_last_check_is_read_for_a_touch():
    bars = [*intraday("AAPL", FRI_BEFORE, [100] * 78), *intraday("AAPL", MON, [100, 100, 100])]
    bars[-2] = dataclasses.replace(bars[-2], low=D("97"))                          # touched 97 at 9:35-9:40
    r = store(bars)
    o = CAL.open_close(MON)[0]
    rules = [rule(id="s", type="stop", price="98")]
    _, st = check(rules, at=o + timedelta(minutes=5), state={}, calendar=CAL, reader=r, dataset_id="DS-T")
    (ev,), _ = check(rules, at=o + timedelta(minutes=15), state=st, calendar=CAL, reader=r, dataset_id="DS-T")
    assert ev.triggered is True and f"traded $97.00 since {(o + timedelta(minutes=5)).isoformat()}" in ev.reasons[0]


def _plan(day, minutes, **kw):
    bars = intraday("AAPL", day, [100, 100, 100, 101])
    r, _ = dataset({"AAPL": bars}, interval=Interval.M5)
    t = CAL.open_close(day)[0] + timedelta(minutes=minutes)
    kw = kw or {"stop_rule": "or-mid", "target_rule": "2R"}
    return plan_exit(r, CAL, "DS-T", "AAPL", direction="long", entry_price="101", as_of=t, now=t,
                     max_staleness_bars=20, **kw)


def test_a_plan_gives_a_stop_a_target_and_a_flat_by_for_its_day():
    plan = _plan(MON, 20)
    rules = rules_from_plan(plan, CAL)
    base = f"aapl-{plan.plan_id.lower()}"
    assert [(x["id"], x["type"]) for x in rules] == [(f"{base}-stop", "stop"), (f"{base}-target", "target"),
                                                      (f"{base}-flat-by", "time")]
    assert (rules[0]["price"], rules[1]["price"], rules[2]["at"]) == ("100", "103", "15:55")
    assert rules[0]["instrument_id"] == plan.instrument_id and rules[0]["dataset_id"] == "DS-T"
    assert rules[0]["note"] == "plan stop: opening-range midpoint; (100.05 + 99.95) / 2 = 100"
    assert all(x["date"] == "2026-11-23" for x in rules)
    rings, _ = at("2026-11-23 15:55", rules, q("100.5"))
    assert len(rings) == 1 and rings[0].startswith("Flat by close")
    assert at("2026-11-24 15:56", rules)[0] == []                                 # the plan's day only


def test_a_new_plans_stop_rings_although_yesterdays_did():
    """Review of #13, item 1: Monday's plan stop rang; Tuesday's plan, same symbol and stop, must ring too."""
    mon = rules_from_plan(_plan(MON, 20), CAL)
    _, st = at("2026-11-23 10:20", mon, q("97"))
    tue = rules_from_plan(_plan(date(2026, 11, 24), 20), CAL)
    rings, st = at("2026-11-24 10:20", tue, q("97"), st)
    assert len(rings) == 1 and "your stop $100" in rings[0]
    _, st = at("2026-11-24 10:30", [*mon, *tue], q("97"), st)
    assert not any(mon[0]["id"] == k for k in st["rung"])                         # a past day's mark is dropped


def test_a_plan_without_a_stop_or_edited_is_refused_and_a_price_plan_gets_15_55():
    with pytest.raises(ValidationError, match="no stop"):
        rules_from_plan(_plan(MON, 20, target_price="103"), CAL)
    plan = _plan(MON, 20)
    by = {s.name: s for s in plan.scenarios}
    edited = dataclasses.replace(plan, scenarios=tuple(
        dataclasses.replace(s, price=dataclasses.replace(s.price, value=D(10))) if s is by["stop"] else s
        for s in plan.scenarios))                                                 # hash left as it was
    with pytest.raises(ValidationError, match="canonical_hash"):
        rules_from_plan(edited, CAL)
    priced = rules_from_plan(_plan(MON, 20, stop_price="99", target_price="103"), CAL)
    assert priced[-1] == {"id": priced[-1]["id"], "symbol": "AAPL", "type": "time", "at": "15:55",
                          "label": "flat by close", "before_close": True, "date": "2026-11-23"}


def test_a_saturday_price_plan_is_watched_on_monday():
    sat = datetime(2026, 11, 21, 15, tzinfo=UTC)
    bars = intraday("AAPL", FRI_BEFORE, [100] * 78)
    r, _ = dataset({"AAPL": bars}, interval=Interval.M5)
    plan = plan_exit(r, CAL, "DS-T", "AAPL", direction="long", entry_price="101", stop_price="99",
                     target_price="103", as_of=sat, now=sat, max_staleness_bars=20)
    assert {x["date"] for x in rules_from_plan(plan, CAL)} == {"2026-11-23"}


def test_a_plans_flat_by_on_an_early_close_rings_at_1255_not_955():
    rules = rules_from_plan(_plan(date(2026, 11, 27), 20), CAL)
    flat = [x for x in rules if x["type"] == "time"]
    assert flat[0]["at"] == "12:55" and not flat[0].get("before_close")
    assert at("2026-11-27 09:56", flat)[0] == []                                   # The upstream double shift rang here
    assert len(at("2026-11-27 12:55", flat)[0]) == 1


def test_after_a_reset_an_old_bars_low_does_not_ring_again():
    bars = [*intraday("AAPL", FRI_BEFORE, [100] * 78), *intraday("AAPL", MON, [100, 99])]
    bars[-1] = dataclasses.replace(bars[-1], low=D("97"))                          # touched 97, closed 99
    r = store(bars)
    o = CAL.open_close(MON)[0]
    rules = [rule(id="s", type="stop", price="98")]
    _, st = check(rules, at=o + timedelta(minutes=5), state={}, calendar=CAL, reader=r, dataset_id="DS-T")
    (ev,), st = check(rules, at=o + timedelta(minutes=10), state=st, calendar=CAL, reader=r, dataset_id="DS-T")
    assert ev.reasons[0].startswith("RANG")
    st, _, _ = reset(st, ["s"])
    (ev,), _ = check(rules, at=o + timedelta(minutes=11), state=st, calendar=CAL, reader=r, dataset_id="DS-T")
    assert ev.triggered is False and not ev.reasons                                # no new bar: only 99 now counts


def test_a_plans_first_ring_never_counts_the_bars_before_the_plan():
    """Review of #13, N1, OSDev4's repro: the trigger bar's low (99.95) sits under the or-mid stop (100.00) but came
    before the plan existed; a ring at the plan's own instant with a fresh state must stay quiet."""
    plan = _plan(MON, 20)
    rules = rules_from_plan(plan, CAL)
    assert rules[0]["watch_from"] == plan.as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    bars = [*intraday("AAPL", FRI_BEFORE, [100] * 78), *intraday("AAPL", MON, [100, 100, 100, 101, 101])]
    r = store(bars)
    o = CAL.open_close(MON)[0]
    events, st = check(rules[:2], at=o + timedelta(minutes=20), state={}, calendar=CAL, reader=r, dataset_id="DS-T")
    assert [e.triggered for e in events] == [False, False] and not any(e.reasons for e in events)
    dip = [*bars[:-1], dataclasses.replace(bars[-1], low=D("99.5"))]              # 10:15 bar dips under the stop
    (stop, _), _ = check(rules[:2], at=o + timedelta(minutes=25), state=st, calendar=CAL, reader=store(dip),
                         dataset_id="DS-T")
    assert stop.triggered is True and stop.reasons[0].startswith("RANG")


def test_after_hours_bars_are_never_the_price():
    """Review of #13, N3: the upstream regular-market quote; an evening pct_move still has its previous close."""
    close = CAL.open_close(MON)[1]
    session = [*intraday("AAPL", FRI_BEFORE, [100] * 78), *intraday("AAPL", MON, [102] * 78)]
    late = [dataclasses.replace(b, start=b.start + (close - CAL.open_close(MON)[0]),
                                end=b.end + (close - CAL.open_close(MON)[0]),
                                available_at=b.available_at + (close - CAL.open_close(MON)[0]))
            for b in intraday("AAPL", MON, [90] * 24)]                              # 16:00-18:00, at 90
    got, _ = quotes(store([*session, *late]), CAL, "DS-T", [IID], close + timedelta(hours=3))
    assert (got[IID].price, got[IID].prev_close) == (D(102), D(100)) and got[IID].observed_at == close


def test_impossible_stamps_are_refused_and_the_quiet_line_fits_the_rule():
    for stamp in ("2026-02-30T00:00:00Z", "2025-13-31T15:00:00Z", "2026-11-23T25:00:00Z"):
        for state in ({"last_check": stamp}, {"rung": {"x": stamp}}, {"checked": {"x": stamp}}):
            with pytest.raises(ValidationError):
                validate_state(state)
            with pytest.raises(ValidationError):
                reset(state, ["x"])
    marks = {"rung": {"r": "2026-11-23T15:00:00Z", "bell": "2026-11-23T14:30:00Z"}}
    (o,), _ = evaluate([rule(type="stop", price="54")], q("53"), market_status(CAL, ny("2026-11-24 10:00")),
                       marks, ny("2026-11-24 10:00"))
    assert o.reasons == ("rang at 2026-11-23T15:00:00Z; quiet until `traderos alerts reset r`",)
    (o,), _ = evaluate([rule(type="stop", price="54", rearm=True)], q("53"),
                       market_status(CAL, ny("2026-11-23 11:00")), marks, ny("2026-11-23 11:00"))
    assert "until the price crosses back" in o.reasons[0]
    (o,), _ = evaluate([{"id": "bell", "type": "opening_bell"}], {}, market_status(CAL, ny("2026-11-23 11:00")),
                       marks, ny("2026-11-23 11:00"))
    assert o.reasons == ("rang at 2026-11-23T14:30:00Z; it rings again the next trading day",)


# ---- OSDev4's re-review of e4e2595: R1, R2 and the test gaps -------------------------------------------------------

def _mon_bars(*tail):
    """Friday's full session at 100, then Monday at 102 from 9:30 to 10:00, then `tail` as (low, close) bars."""
    bars = [*intraday("AAPL", FRI_BEFORE, [100] * 78), *intraday("AAPL", MON, [102] * 6 + [c for _, c in tail])]
    for i, (low, _) in enumerate(tail, start=len(bars) - len(tail)):
        bars[i] = dataclasses.replace(bars[i], low=D(str(low)))
    return bars


def test_a_bar_that_began_before_a_plan_never_rings_it():
    """R1, OSDev4's repro: the 10:00-10:05 bar dips to 99 and closes at 102. A plan made at 10:03 with a stop at 100
    must not ring on it at 10:06 (its low may have come before the plan); a dip in the next bar still rings."""
    o = CAL.open_close(MON)[0]
    r = store(_mon_bars((99, 102)))
    plan = plan_exit(r, CAL, "DS-T", "AAPL", direction="long", entry_price="102", stop_rule="price:100",
                     target_rule="2R", as_of=o + timedelta(minutes=33), now=o + timedelta(minutes=33),
                     max_staleness_bars=20)
    rules = rules_from_plan(plan, CAL)[:2]
    events, st = check(rules, at=o + timedelta(minutes=36), state={}, calendar=CAL, reader=r, dataset_id="DS-T")
    assert [e.triggered for e in events] == [False, False] and not any(e.reasons for e in events)
    r2 = store(_mon_bars((99, 102), ("99.5", 102)))
    (stop, _), _ = check(rules, at=o + timedelta(minutes=41), state=st, calendar=CAL, reader=r2, dataset_id="DS-T")
    assert stop.triggered is True and "traded $99.50" in stop.reasons[0]


def test_a_bar_that_began_before_a_rules_first_check_never_rings_it():
    """R1, the plain rule: first checked at 10:03, the straddling 10:00-10:05 bar's 99 stays quiet at 10:06."""
    o = CAL.open_close(MON)[0]
    rules = [rule(id="s", type="stop", price="100")]
    _, st = check(rules, at=o + timedelta(minutes=33), state={}, calendar=CAL, reader=store(_mon_bars()),
                  dataset_id="DS-T")
    assert st["watching"]["s"] == st["checked"]["s"]
    (ev,), st = check(rules, at=o + timedelta(minutes=36), state=st, calendar=CAL,
                      reader=store(_mon_bars((99, 102))), dataset_id="DS-T")
    assert ev.triggered is False and not ev.reasons
    (ev,), _ = check(rules, at=o + timedelta(minutes=41), state=st, calendar=CAL,
                     reader=store(_mon_bars((99, 102), ("99.5", 102))), dataset_id="DS-T")
    assert ev.triggered is True and "traded $99.50" in ev.reasons[0]


def test_a_daily_bar_that_opened_before_the_plan_never_rings_it():
    """R1 on daily bars (OSDev4's TUTMKT repro): a plan made at 10:00 New York does not ring at the close on that
    day's low, which may have come before it."""
    from tests.unit.fakes import daily
    bars = daily("AAPL", [101] * 10, last_day=MON)
    bars[-1] = dataclasses.replace(bars[-1], low=D("99.64"))
    r, _ = dataset({"AAPL": bars})
    rules = [rule(id="s", type="stop", price="100.17", watch_from="2026-11-23T15:00:00Z")]
    (ev,), _ = check(rules, at=CAL.open_close(MON)[1] + timedelta(minutes=30), state={}, calendar=CAL, reader=r,
                     dataset_id="DS-T")
    assert ev.triggered is False and not ev.reasons


def test_after_a_reset_a_plan_rule_reads_from_its_last_check_not_from_the_plan():
    """A plan rule that rang and was reset reads from its own last check: the dip it rang on never rings again."""
    o = CAL.open_close(MON)[0]
    rules = [rule(id="s", type="stop", price="100", watch_from="2026-11-23T15:03:00Z")]
    r = store(_mon_bars((102, 102), ("99.5", 102)))
    (ev,), st = check(rules, at=o + timedelta(minutes=41), state={}, calendar=CAL, reader=r, dataset_id="DS-T")
    assert ev.triggered is True
    st, cleared, _ = reset(st, ["s"])
    assert cleared == ["s"]
    (ev,), _ = check(rules, at=o + timedelta(minutes=46), state=st, calendar=CAL,
                     reader=store(_mon_bars((102, 102), ("99.5", 102), (102, 102))), dataset_id="DS-T")
    assert ev.triggered is False and not ev.reasons


def test_the_previous_close_is_the_previous_sessions_own_close_only():
    """R2, OSDev4's repro: Thursday at 100, Friday missing, Monday at 104. A 3% pct_move at 11:00 Monday does not
    ring from Thursday's close; it falls back to Monday's open, as the upstream one did. A Friday without its closing
    bar is not a close either."""
    thu = date(2026, 11, 19)
    mon = [*intraday("AAPL", MON, [104] * 18)]
    t = CAL.open_close(MON)[0] + timedelta(minutes=90)
    got, _ = quotes(store([*intraday("AAPL", thu, [100] * 78), *mon]), CAL, "DS-T", [IID], t)
    assert got[IID].prev_close is None and got[IID].session_open == D(104)
    rules = [rule(id="p", type="pct_move", pct="3")]
    (o,), _ = evaluate(rules, got, market_status(CAL, t), {}, t)
    assert o.triggered is False and o.rang is None
    partial = intraday("AAPL", FRI_BEFORE, [100] * 77)                            # no 15:55-16:00 bar
    got, _ = quotes(store([*partial, *mon]), CAL, "DS-T", [IID], t)
    assert got[IID].prev_close is None
    got, _ = quotes(store([*intraday("AAPL", FRI_BEFORE, [100] * 78), *mon]), CAL, "DS-T", [IID], t)
    assert got[IID].prev_close == D(100)
    (o,), _ = evaluate(rules, got, market_status(CAL, t), {}, t)
    assert o.triggered is True and "+4.00%" in o.rang


def test_a_rule_last_checked_before_the_lookback_says_missed_window():
    stale = {"checked": {"s": "2026-11-10T15:00:00Z"}}                           # before q()'s floor (11-16 09:30)
    (o,), _ = evaluate([rule(id="s", type="stop", price="98")], q("101", low="97"),
                       market_status(CAL, ny("2026-11-23 10:01")), stale, ny("2026-11-23 10:01"))
    assert o.missed is True and o.reasons[0].startswith("MISSED_WINDOW") and o.triggered is True
    (o,), _ = evaluate([rule(id="s", type="stop", price="98")], q("101", low="97"),
                       market_status(CAL, ny("2026-11-23 10:01")), SEEN, ny("2026-11-23 10:01"))
    assert o.missed is False and not o.reasons[0].startswith("MISSED_WINDOW")


def test_the_quiet_line_for_a_pct_move_and_a_dated_flat_by():
    rules = [rule(id="p", type="pct_move", pct="3")]
    _, st = evaluate(rules, q("105"), market_status(CAL, ny("2026-11-23 10:01")), {}, ny("2026-11-23 10:01"))
    (o,), _ = evaluate(rules, q("105"), market_status(CAL, ny("2026-11-23 10:02")), st, ny("2026-11-23 10:02"))
    assert o.reasons[0].endswith("; it rings again the next trading day")
    flat = [{"id": "f", "type": "time", "at": "15:55", "date": "2026-11-23", "label": "flat by"}]
    rings, st = at("2026-11-23 15:56", flat)
    assert rings
    (o,), _ = evaluate(flat, {}, market_status(CAL, ny("2026-11-23 15:57")), st, ny("2026-11-23 15:57"))
    assert o.reasons[0].endswith("; it was set for 2026-11-23 only")


def test_each_rule_reports_its_own_previous_check():
    """A rule added later has no previous evaluation of its own, whatever the state's shared last_check says."""
    rules = [{"id": "a", "type": "time", "at": "09:00"}, {"id": "b", "type": "time", "at": "09:00"}]
    state = {"checked": {"a": "2026-11-23T14:00:00Z"}, "last_check": "2026-11-23T14:00:00Z"}
    (a, b), st = check(rules, at=ny("2026-11-23 10:00"), state=state, calendar=CAL)
    assert a.previous_evaluated_at == datetime(2026, 11, 23, 14, tzinfo=UTC) and b.previous_evaluated_at is None
    assert set(st["checked"]) == {"a", "b"}


def test_a_check_without_a_price_never_moves_the_window():
    """A check that had no price (stale or missing data) is not a check of the bars: the next check still reads
    every bar since the last check that had one."""
    rules = [rule(id="s", type="stop", price="98")]
    _, st = evaluate(rules, q("101"), market_status(CAL, ny("2026-11-23 10:01")), {}, ny("2026-11-23 10:01"))
    (o,), st = evaluate(rules, {}, market_status(CAL, ny("2026-11-23 10:20")), st, ny("2026-11-23 10:20"),
                        missing={IID: "DATA_STALE: behind"})
    assert o.triggered is None and st["checked"]["s"] == "2026-11-23T15:01:00Z"
    dip = (ny("2026-11-23 10:05"), ny("2026-11-23 10:10"), D("97"), D("101"))      # closed before the stale check
    late = {IID: Quote(D("101"), D("100"), D("100"), ny("2026-11-23 10:30"), (dip,), ny("2026-11-16 09:30"))}
    (o,), _ = evaluate(rules, late, market_status(CAL, ny("2026-11-23 10:31")), st, ny("2026-11-23 10:31"))
    assert o.triggered is True and "traded $97.00" in o.rang


def test_a_bar_that_begins_exactly_when_watching_begins_counts():
    rules = [rule(id="s", type="stop", price="98", watch_from="2026-11-23T15:05:00Z")]
    bar = (ny("2026-11-23 10:05"), ny("2026-11-23 10:10"), D("97"), D("101"))
    prices = {IID: Quote(D("101"), D("100"), D("100"), ny("2026-11-23 10:10"), (bar,), ny("2026-11-16 09:30"))}
    (o,), _ = evaluate(rules, prices, market_status(CAL, ny("2026-11-23 10:11")), {}, ny("2026-11-23 10:11"))
    assert o.triggered is True
