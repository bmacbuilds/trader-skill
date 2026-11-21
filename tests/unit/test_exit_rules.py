"""The upstream stop and target rules, ported (src/traderos/intelligence/exit_rules.py; trader-exit/exit.py).

The numbers are the upstream project's own, from tank/day-trader-skill/tests/test_exit.py: the reel's or-mid 1R plan
(stop 358.32, target 370.04), the default 2R (375.90), or-low, price, pct and atr stops, half-up targets, and its
refusals. On a $0.01 tick the port gives its cents exactly. Then the same rules through plan_exit, on stored bars.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from tests.unit.fakes import CAL, daily, dataset, intraday
from traderos.contracts import DataError, ValidationError
from traderos.contracts.market import Interval
from traderos.intelligence.exit import plan_exit
from traderos.intelligence.exit_rules import atr_of, compute, flat_by, parse_stop, parse_target

CENT = D("0.01")


def c(entry, stop, target="2R", oh="362.18", ol="354.47", atr=None, n=None):
    sr, sp = parse_stop(stop)
    tr, tp = parse_target(target)
    return compute(D(entry), CENT, sr, sp, tr, tp, or_high=None if oh is None else D(oh),
                   or_low=None if ol is None else D(ol), atr=atr, atr_bars=n)


def test_the_reel_or_mid_1r_and_the_default_2r():
    p = c("364.18", "or-mid", "1R")
    assert (p.stop, p.risk, p.target, p.reward_risk) == (D("358.32"), D("5.86"), D("370.04"), D("1.00"))
    assert "rounded down" in p.stop_detail                    # floor(358.325)
    assert c("364.18", "or-mid").target == D("375.90")


def test_other_stops():
    p = c("364.18", "or-low")
    assert (p.stop, p.risk, p.target) == (D("354.47"), D("9.71"), D("383.6"))
    assert c("364.18", "price:358.329").stop == D("358.32")  # rounds away from the entry
    p = c("364.18", "pct:1")
    assert (p.stop, p.risk, p.target) == (D("360.53"), D("3.65"), D("371.48"))
    p = c("364.18", "atr:1.5", atr=D("3.2181"), n=9)
    assert p.stop == D("359.35") and p.stop_label == "1.5 x ATR(9) under the entry"    # 364.18 - 4.82715


def test_labels_never_print_an_exponent():
    p = c("100", "pct:10", "10R", None, None)                 # Decimal("10").normalize() prints as 1E+1
    assert p.stop_label == "10% under the entry" and p.target_label.startswith("10R (10 times")


def test_targets_round_half_up_and_price_targets():
    p = c("100", "price:99.01", "1.5R", None, None)
    assert p.target == D("101.49")                            # 100 + 1.485
    p = c("364.18", "or-mid", "price:372")
    assert p.reward_risk == D("1.33") and p.target_label == "your price (1.33R)"
    assert c("364.18", "or-mid", "price:372.005").target == D("372.01")                  # a price target, half up
    assert c("100", "price:97", "price:104.01", None, None).reward_risk == D("1.34")      # 1.3367, half up


def test_refusals_say_why():
    for stop, target, why in (("price:364.18", "2R", "at or above the entry"),
                              ("price:365", "2R", "at or above the entry"),
                              ("or-mid", "price:364", "at or below the entry"),
                              ("price:364.01", "2R", "too tight to survive the spread"),
                              ("pct:0.04", "2R", "too tight to survive the spread")):
        with pytest.raises(ValidationError, match=why):
            c("364.18", stop, target)
    assert c("364.18", "price:363.99").risk == D("0.19")      # 0.05% of 364.18 is 0.182: 0.19 is allowed
    assert c("200", "price:199.90").risk == D("0.10")         # exactly 0.05% of 200: allowed (under it is not)
    with pytest.raises(ValidationError, match="at or below the entry"):
        c("364.18", "or-mid", "price:364.18")                  # a target AT the entry
    with pytest.raises(ValidationError):
        c("364.18", "price:364.00")                            # 0.18 is not
    with pytest.raises(ValidationError):
        c("358.00", "or-mid")                                  # the midpoint is above this entry
    for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
        with pytest.raises(ValidationError):
            parse_stop(bad)
    for bad in ("0R", "R", "lots", "price:x"):
        with pytest.raises(ValidationError):
            parse_target(bad)
    with pytest.raises(ValidationError):
        c("364.18", "or-mid", oh=None, ol=None)
    with pytest.raises(ValidationError):
        c("364.18", "or-mid", oh="354.47", ol="362.18")
    with pytest.raises(ValidationError):
        c("364.18", "or-mid", oh="362.18", ol="362.18")       # a range with no height


def test_the_tick_is_the_instruments():
    sr, sp = parse_stop("pct:1")
    tr, tp = parse_target("1.5R")
    p = compute(D("100"), D("0.25"), sr, sp, tr, tp)          # a quarter-point tick: 99 stays, 101.5 stays
    assert (p.stop, p.target) == (D(99), D("101.5"))
    p = compute(D("100"), D("0.25"), *parse_stop("pct:1.1"), tr, tp)
    assert (p.stop, p.target) == (D("98.75"), D(102))        # 98.9 down to 98.75; 100 + 1.875 half up to 102


def test_atr_is_the_sessions_true_range():
    bars = intraday("SPY", date(2026, 11, 23), [100, 101, 100, 104])
    # first bar: own range 0.10; then max(h-l, |h-prev close|, |l-prev close|): 1.10, 1.10, 4.10
    assert atr_of(bars) == ((D("0.10") + D("1.10") + D("1.10") + D("4.10")) / 4, 4)
    assert atr_of(intraday("SPY", date(2026, 11, 23), [100] * 20))[1] == 14
    gap = intraday("SPY", date(2026, 11, 23), [100, 100])
    gap[1] = dataclasses.replace(gap[1], open=D(103), high=D("103.1"), low=D(103), close=D(103))
    assert atr_of(gap) == ((D("0.10") + D("3.1")) / 2, 2)    # the gap from the 100 close counts: 103.1 - 100


MON = date(2026, 11, 23)
FRI = date(2026, 11, 27)                                       # an early close at 18:00 UTC (1:00 p.m. New York)


def test_flat_by_moves_up_with_an_early_close_and_refuses_late_times():
    o = CAL.open_close(MON)[0]
    assert flat_by(CAL, MON, None) == (o + timedelta(hours=6, minutes=25), "")                 # 15:55
    o = CAL.open_close(FRI)[0]
    at, note = flat_by(CAL, FRI, None)
    assert at == o + timedelta(hours=3, minutes=25) and "early close" in note                  # 12:55
    with pytest.raises(ValidationError):
        flat_by(CAL, FRI, "14:00")                             # given, and at or after the 1:00 p.m. close
    for day, late in ((MON, "16:30"), (MON, "16:00"), (FRI, "13:00")):                        # at the close too
        with pytest.raises(ValidationError):
            flat_by(CAL, day, late)
    with pytest.raises(ValidationError, match="no time left"):
        flat_by(CAL, MON, None, entry_at=CAL.open_close(MON)[0] + timedelta(hours=6, minutes=25))


# ---- through plan_exit, on stored bars ----------------------------------------------------------------------------

OPEN = CAL.open_close(MON)[0]
RANGE = [100, 100, 100]                                        # highs 100.05, lows 99.95: or-mid 100.00


def plan(closes, minutes, *, day=MON, interval=Interval.M5, series=None, as_of=None, **kw):
    r, _ = dataset({"SPY": series or intraday("SPY", day, closes)}, interval=interval)
    t = as_of or CAL.open_close(day)[0] + timedelta(minutes=minutes)
    return plan_exit(r, CAL, "DS-T", "SPY", as_of=t, now=t, max_staleness_bars=20, **kw)


def test_plan_exit_reads_the_range_from_the_session_and_adds_a_time_exit():
    p = plan([*RANGE, 101], 20, direction="long", entry_price="101", target_rule="2R")
    by = {s.name: s for s in p.scenarios}
    assert by["stop"].price.value == D(100) and by["target"].price.value == D(103)
    assert any("opening-range midpoint" in a for a in by["stop"].assumptions)
    assert by["time exit"].trigger == f"out by {(OPEN + timedelta(hours=6, minutes=25)).isoformat()}, win or lose"
    early = plan([*RANGE, 101], 20, day=FRI, direction="long", entry_price="101", stop_rule="or-low")
    assert "early close" in {s.name: s for s in early.scenarios}["time exit"].assumptions[0]


def test_plan_exit_atr_and_the_risk_warning():
    p = plan([*RANGE, 101], 20, direction="long", entry_price="101", stop_rule="atr:2", target_rule="price:101.1")
    by = {s.name: s for s in p.scenarios}
    assert by["stop"].price.value == D("100.3")               # ATR of 4 bars: (0.1 + 0.1 + 0.1 + 1.1) / 4 = 0.35
    assert any("under the min_reward_risk 1" in r for r in p.risks)                           # 0.1 / 0.7 = 0.14R
    with pytest.raises(DataError, match="at least 3"):
        plan([100, 100], 10, direction="long", entry_price="101", stop_rule="atr:2")


def test_plan_exit_refuses_what_upstream_planner_refuses():
    with pytest.raises(ValidationError, match="longs only"):
        plan([*RANGE, 101], 20, direction="short", entry_price="101", stop_rule="pct:1")
    with pytest.raises(ValidationError, match="not both"):
        plan([*RANGE, 101], 20, direction="long", entry_price="101", stop_rule="pct:1", stop_price="99")
    with pytest.raises(ValidationError, match="no opening-range stop"):
        plan([*RANGE, 101], 20, direction="long", entry_price="101", target_rule="2R", setup="level")
    with pytest.raises(DataError, match="range is set at"):
        plan(RANGE[:2], 10, direction="long", entry_price="101", stop_rule="or-mid")
    holed = [b for i, b in enumerate(intraday("SPY", MON, [*RANGE, 101])) if i != 1]
    with pytest.raises(DataError, match="missing 1 of 3"):
        plan(None, 20, series=holed, direction="long", entry_price="101", stop_rule="or-mid")
    days = daily("SPY", [100] * 30, last_day=MON)
    with pytest.raises(ValidationError, match="1-minute or 5-minute"):
        plan(None, 400, series=days, interval=Interval.D1, direction="long", entry_price="101", stop_rule="or-mid")
    p = plan(None, 400, series=days, interval=Interval.D1, direction="long", entry_price="101", stop_rule="pct:1")
    assert {s.name: s for s in p.scenarios}["stop"].price.value == D("99.99")          # pct needs no bars
    given = plan([*RANGE, 101], 20, direction="long", entry_price="101", stop_rule="or-mid", or_high="100.5",
                 or_low="99.5")
    assert {s.name: s for s in given.scenarios}["stop"].price.value == D(100)          # The upstream flags still work
    mixed = plan([*RANGE, 101], 20, direction="long", entry_price="101", stop_price="99.5", target_rule="1R")
    assert [s.price.value for s in mixed.scenarios[:1]] == [D("99.5")]                 # the given stop is kept
    assert {s.name: s for s in mixed.scenarios}["target"].price.value == D("102.5")


def test_atr_is_the_last_14_true_ranges():
    bars = intraday("SPY", date(2026, 11, 23), [100, 110, 100, 110, 100, 110] + [100] * 15)   # 6 wide, then 15 quiet
    assert atr_of(bars) == (D("0.10"), 14)                     # only the last 14: the wide ones have rolled out


def test_atr_never_reads_past_the_entry():
    """Review of #13, item 4 (QT01): an entry at 10:00 gets the same ATR stop planned at 10:00 or at 10:20."""
    closes = [100, 101, 100, 101, 100, 101, 102, 90, 110, 90]  # the last three bars swing after the entry
    entry_at = OPEN + timedelta(minutes=35)                    # 10:05, after seven bars
    then = plan(closes, 35, direction="long", entry_price="102", stop_rule="atr:2")
    later = plan(closes, 50, direction="long", entry_price="102", stop_rule="atr:2", entry_at=entry_at)
    stop = lambda p: {s.name: s for s in p.scenarios}["stop"]                 # noqa: E731
    assert stop(then).price.value == stop(later).price.value
    assert any("bars to the entry" in a for a in stop(later).assumptions)
    assert any("bars to 2026-11-23T15:05:00+00:00" in a for a in stop(then).assumptions)


def test_the_plan_day_is_the_new_york_day_after_a_closed_evening():
    """Review of #13, item 5: Sunday 19:30 plans Monday; Thanksgiving 21:00 plans Friday's 12:55 early close."""
    bars = intraday("SPY", date(2026, 11, 20), [100] * 78)
    for ny_evening, flat in ((datetime(2026, 11, 23, 0, 30, tzinfo=UTC), "2026-11-23T20:55:00+00:00"),
                             (datetime(2026, 11, 27, 2, 0, tzinfo=UTC), "2026-11-27T17:55:00+00:00")):
        p = plan(None, 0, series=bars, direction="long", entry_price="101", stop_rule="pct:1", as_of=ny_evening)
        assert {s.name: s for s in p.scenarios}["time exit"].trigger == f"out by {flat}, win or lose"


def test_given_range_and_inputs_are_checked():
    with pytest.raises(ValidationError, match="tick"):
        plan([*RANGE, 101], 20, direction="long", entry_price="101", stop_rule="or-low", or_high="100.5",
             or_low="99.505")                                  # off the 0.01 tick: refused, as an entry is
    for bad in ("abc", "NaN", "-1"):
        with pytest.raises(ValidationError):
            plan([*RANGE, 101], 20, direction="long", entry_price="101", stop_rule="pct:1", min_reward_risk=bad)


def test_reward_risk_exactly_at_the_minimum_does_not_warn():
    p = plan([*RANGE, 101], 20, direction="long", entry_price="101", stop_rule="or-mid", target_rule="1R")
    assert not any("min_reward_risk" in r for r in p.risks)    # 1.00R against 1.0: not under it


def test_atr_is_rounded_to_6_places_as_upstream_did():
    sr, sp = parse_stop("atr:10")
    tr, tp = parse_target("2R")
    assert compute(D(100), CENT, sr, sp, tr, tp, atr=D("0.0100005"), atr_bars=14).stop == D("99.90")   # not 99.89


def test_a_range_high_off_the_tick_is_refused_too():
    with pytest.raises(ValidationError, match="tick"):
        plan([*RANGE, 101], 20, direction="long", entry_price="101", stop_rule="or-low", or_high="100.505",
             or_low="99.5")


def test_after_hours_bars_never_hide_the_range_from_the_exit_planner():
    session = intraday("SPY", MON, [*RANGE, 101] + [100] * 74)
    close = CAL.open_close(MON)[1]
    late = [dataclasses.replace(b, start=b.start + (close - OPEN), end=b.end + (close - OPEN),
                                available_at=b.available_at + (close - OPEN)) for b in intraday("SPY", MON, [100] * 36)]
    p = plan(None, 0, series=[*session, *late], direction="long", entry_price="101", stop_rule="or-mid",
             as_of=close + timedelta(hours=3))
    assert {s.name: s for s in p.scenarios}["stop"].price.value == D(100)


def test_a_range_read_from_bars_is_rounded_to_the_tick_as_upstream_did():
    """Review of #13, item 12: highs up to 354.955 and lows down to 353.995 give the upstream or-mid 354.48, not
    354.47."""
    bars = intraday("SPY", MON, [354.5, 354.5, 354.5, 356])
    bars[0] = dataclasses.replace(bars[0], high=D("354.955"), low=D("353.995"))
    p = plan(None, 20, series=bars, direction="long", entry_price="356", stop_rule="or-mid")
    assert {s.name: s for s in p.scenarios}["stop"].price.value == D("354.48")   # (354.96 + 354.00) / 2


def test_entry_at_must_be_on_the_plans_day_and_not_after_it():
    for when in (OPEN + timedelta(minutes=25), OPEN - timedelta(days=3)):        # after as_of; an earlier day
        with pytest.raises(ValidationError, match="entry_at"):
            plan([*RANGE, 101], 20, direction="long", entry_price="101", stop_rule="pct:1", entry_at=when)
