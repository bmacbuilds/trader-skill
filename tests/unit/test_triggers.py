"""The upstream entry triggers, ported (src/traderos/intelligence/triggers.py;
tank/day-trader-skill/trader-entry/entry.py).

Each test pins one of the upstream rules at its edge: a close (not a wick) strictly above the range high or the level,
the entry window inclusive at both ends (09:45 and 15:30 New York), the cutoff moving up with an early close, a thin bar
skipped for the next, an opening range missing a bar refused, and the New York day running midnight to midnight.
The fake calendar opens at 14:30 UTC (9:30 New York in November) and closes at 21:00, 18:00 on 2026-11-27.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from tests.unit.fakes import CAL, dataset, intraday
from traderos.contracts import ValidationError
from traderos.contracts.intel import EntryStatus
from traderos.contracts.market import Interval
from traderos.intelligence.entry import assess_entry
from traderos.intelligence.triggers import early_cutoff, evaluate, trading_day

MON = date(2026, 11, 23)
OPEN = CAL.open_close(MON)[0]                    # 14:30 UTC
FLAT = [100] * 3                                 # a 15-minute range on 5-minute bars: highs 100.05


def at(minutes):
    return OPEN + timedelta(minutes=minutes)


def ev(closes, minutes, *, day=MON, **kw):
    return evaluate(CAL, intraday("SPY", day, closes), Interval.M5, at(minutes) if day == MON else minutes, **kw)


def test_orb_is_the_first_close_strictly_above_the_range_high():
    t = ev([*FLAT, D("100.05"), D("100.06"), 101], 31)          # 100.05 equals the high; 100.06 is above it
    assert t.status == "triggered" and t.level == D("100.05")
    assert t.trigger_bar.close == D("100.06") and t.trigger_bar.end == at(25)
    wick = ev([*FLAT, D("100.04")], 21)                         # its high is 100.09: a wick over, a close under
    assert wick.status == "waiting" and wick.phase == "open"
    forming = evaluate(CAL, intraday("SPY", MON, [*FLAT, 101]), Interval.M5, at(19))   # the 101 bar ends at 20
    assert forming.status == "waiting" and forming.trigger_bar is None


def test_the_range_is_forming_until_its_last_bar_closes_and_a_missing_bar_is_refused():
    assert ev(FLAT[:2], 14).phase == "forming"
    assert ev(FLAT, 15).phase == "open"
    holed = [b for i, b in enumerate(intraday("SPY", MON, [*FLAT, 101])) if i != 1]
    t = evaluate(CAL, holed, Interval.M5, at(21))
    assert t.status == "no-data" and "missing 1 of 3" in t.reason       # upstream PR #23


def test_the_window_opens_at_0945_inclusive():
    # A 5-minute range: the bar ending 9:40 closes above, but entries start at 9:45. The bar ending 9:45 counts.
    early = ev([100, 101], 11, or_minutes=5)
    assert early.status == "waiting" and "no entries before" in early.reason
    assert ev([100, 101, 101], 16, or_minutes=5).trigger_bar.end == at(15)


def test_the_window_closes_at_1530_inclusive_then_expires():
    flat = [100] * 72                                           # bars to 15:30 New York (slot 71 ends at 6h)
    last_in = ev([*flat[:-1], 101], 6 * 60)
    assert last_in.status == "triggered" and last_in.trigger_bar.end == at(6 * 60)
    late = ev([*flat, 101], 6 * 60 + 5)                         # closes above at 15:35: too late
    assert late.status == "expired" and late.phase == "after-cutoff"
    assert ev(flat, 6 * 60).status == "expired"                 # at the cutoff with no trigger
    assert ev(flat, 6 * 60 - 1).status == "waiting"


def test_the_cutoff_moves_up_with_an_early_close():
    fri = date(2026, 11, 27)                                    # closes 18:00 UTC (1:00 p.m. New York)
    o, c = CAL.open_close(fri)
    assert early_cutoff(o + timedelta(hours=6), o, c) == o + timedelta(hours=3)          # 15:30 -> 12:30
    assert early_cutoff(o + timedelta(hours=2, minutes=30), o, c) == o + timedelta(hours=2, minutes=30)  # 12:00 stays
    assert early_cutoff(c, o, c) == c - timedelta(hours=3)          # exactly at the close moves up too (upstream: at <)
    short = o + timedelta(hours=1)                              # a close one hour after the open
    assert early_cutoff(o + timedelta(hours=2), o, short) == short                       # moved before the open
    t = evaluate(CAL, intraday("SPY", fri, [100] * 40), Interval.M5, o + timedelta(hours=3))
    assert t.status == "expired" and t.cutoff == o + timedelta(hours=3) and "early close" in t.reason


def test_a_thin_close_is_skipped_for_the_next_with_enough_volume():
    bars = intraday("SPY", MON, [*FLAT, 101, 102])
    bars[3] = dataclasses.replace(bars[3], volume=D(999))       # 999 / 500 = 1.998x
    bars[4] = dataclasses.replace(bars[4], volume=D(1000))      # exactly 2x: enough
    t = evaluate(CAL, bars, Interval.M5, at(26), min_volume_x=D(2))
    assert t.trigger_bar is bars[4] and t.volume_x == D("2.00")
    thin = evaluate(CAL, bars[:4], Interval.M5, at(21), min_volume_x=D(2))
    assert thin.status == "waiting" and "on 1.99x the range's volume" in thin.reason      # never a passing 2.00x


def test_level_reads_from_the_first_bar_and_needs_a_level():
    t = ev([D("99.9"), D("100.1")], 11, setup="level", level=D(100), no_entries_before="09:30")
    assert t.status == "triggered" and t.trigger_bar.end == at(10)
    assert ev([100, 100], 11, setup="level", level=D(100), no_entries_before="09:30").status == "waiting"
    with pytest.raises(ValidationError):
        ev(FLAT, 20, setup="level")


def test_the_new_york_day_runs_midnight_to_midnight():
    assert trading_day(CAL, OPEN - timedelta(hours=9, minutes=30)) == MON                       # 00:00 New York
    assert trading_day(CAL, OPEN - timedelta(hours=9, minutes=31)) is None                      # Sunday 23:59
    assert trading_day(CAL, OPEN + timedelta(hours=14, minutes=29)) == MON                      # 23:59 Monday
    assert trading_day(CAL, OPEN + timedelta(hours=14, minutes=30)) == date(2026, 11, 24)       # Tuesday, pre-open
    assert trading_day(CAL, datetime(2026, 11, 26, 16, tzinfo=UTC)) is None                     # Thanksgiving
    evening = evaluate(CAL, intraday("SPY", MON, [*FLAT, 101]), Interval.M5, OPEN + timedelta(hours=12))
    assert evening.status == "triggered" and evening.phase == "after-close"
    assert ev(FLAT, -1).phase == "pre-open"
    assert evaluate(CAL, [], Interval.M5, datetime(2026, 11, 22, 15, tzinfo=UTC)).phase == "closed"


def test_inputs_are_refused_not_guessed():
    for kw in ({"or_minutes": 7}, {"or_minutes": 0}, {"setup": "vwap"}, {"min_volume_x": D(-1)},
               {"min_volume_x": "NaN"}, {"no_entries_before": "9.45"}, {"no_entries_after": "24:00"},
               {"setup": "level", "level": "NaN"}, {"setup": "level", "level": -5}, {"setup": "level", "level": 0},
               {"setup": "level", "level": 1.5}, {"setup": "level", "level": "abc"}):
        with pytest.raises(ValidationError):
            ev(FLAT, 20, **kw)
    with pytest.raises(ValidationError):
        evaluate(CAL, [], Interval.D1, at(20))


def test_a_volume_multiple_of_0_is_off_as_in_upstream_code():
    bars = intraday("SPY", MON, [*FLAT, 101])
    assert evaluate(CAL, bars, Interval.M5, at(21), min_volume_x=D(0)) == evaluate(CAL, bars, Interval.M5, at(21))


# ---- in entry/1's terms ------------------------------------------------------------------------------------------

def entry(closes, minutes, **kw):
    r, _ = dataset({"SPY": intraday("SPY", MON, closes)}, interval=Interval.M5)
    return assess_entry(r, CAL, "DS-T", "SPY", as_of=at(minutes), now=at(minutes), **kw)


def test_assess_entry_maps_upstream_statuses():
    hit = entry([*FLAT, 101, 102], 26, setup="orb")
    assert hit.status is EntryStatus.ELIGIBLE and hit.reference_price.value == D(101)
    assert any(c.startswith("orb: the bar ending") for c in hit.constraints)
    wait = entry([*FLAT, 100], 21, setup="orb")
    assert wait.status is EntryStatus.NOT_ELIGIBLE and wait.invalidation_reasons[0].startswith("last close 100")
    holed = [b for i, b in enumerate(intraday("SPY", MON, [*FLAT, 101])) if i != 0]
    r, _ = dataset({"SPY": holed}, interval=Interval.M5)
    gap = assess_entry(r, CAL, "DS-T", "SPY", as_of=at(21), now=at(21), setup="orb")
    assert gap.status is EntryStatus.UNAVAILABLE
    assert entry([*FLAT, 101], 21, setup="orb").assessment_id != entry([*FLAT, 101], 21, setup="level",
                                                                      level=D(100)).assessment_id


def test_assess_entry_refuses_a_setup_on_daily_bars_and_says_closed_on_a_weekend():
    from tests.unit.fakes import daily
    r, _ = dataset({"SPY": daily("SPY", [100] * 5, last_day=MON)})
    with pytest.raises(ValidationError):
        assess_entry(r, CAL, "DS-T", "SPY", as_of=OPEN + timedelta(hours=7), setup="orb")
    r, _ = dataset({"SPY": intraday("SPY", date(2026, 11, 20), [100] * 78)}, interval=Interval.M5)
    sun = datetime(2026, 11, 22, 15, tzinfo=UTC)
    shut = assess_entry(r, CAL, "DS-T", "SPY", as_of=sun, now=sun, setup="orb")
    assert shut.status is EntryStatus.NOT_ELIGIBLE and "market is closed" in shut.invalidation_reasons[0]


def test_after_hours_bars_never_push_the_opening_range_out():
    """Review of #13, item 7: the CSV gate keeps after-hours bars; at 19:00 the 9:50 trigger still stands."""
    session = intraday("SPY", MON, [*FLAT, 101] + [100] * 74)
    close = CAL.open_close(MON)[1]
    late = [dataclasses.replace(b, start=b.start + (close - OPEN), end=b.end + (close - OPEN),
                                available_at=b.available_at + (close - OPEN)) for b in intraday("SPY", MON, [100] * 36)]
    r, _ = dataset({"SPY": [*session, *late]}, interval=Interval.M5)
    seven = close + timedelta(hours=3)                                          # 19:00 New York
    e = assess_entry(r, CAL, "DS-T", "SPY", as_of=seven, now=seven, setup="orb", max_staleness_bars=20)
    assert e.status is EntryStatus.ELIGIBLE and e.reference_price.value == D(101)


def test_a_volume_multiple_of_0_is_off_even_on_a_silent_range():
    bars = [dataclasses.replace(b, volume=D(0)) for b in intraday("SPY", MON, [*FLAT, 101])]
    assert evaluate(CAL, bars, Interval.M5, at(21), min_volume_x=D(0)).status == "triggered"
