"""The upstream scanner setups, ported (src/traderos/intelligence/setups.py;
tank/day-trader-skill/trader-scanner/scan.py).

Each test pins one of the upstream rules at its edge, by hand, so a port that drifts from its definitions goes red:
strict versus inclusive comparisons, its windows (the 20 closes BEFORE the last, the 10 sessions before the last 5),
its floors ($5 and 1,000,000 shares, inclusive), 30 bars of history, and the opening-range rules (every slot present,
a close above the range HIGH, a wick is not a trigger).
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from tests.unit.fakes import CAL, daily, dataset, intraday
from traderos.contracts import DomainError, ValidationError
from traderos.contracts.intel import Eligibility
from traderos.contracts.market import Interval
from traderos.intelligence.setups import daily_setups, scan_setups

FRI = date(2026, 11, 20)
SUN = datetime(2026, 11, 22, 12, tzinfo=UTC)
NOW = SUN
BIG = 2_000_000                                  # volume above the upstream 1,000,000 floor


def liquid(bars, volume=BIG):
    return [dataclasses.replace(b, volume=D(volume)) for b in bars]


def tags(closes):
    return daily_setups(liquid(daily("SPY", closes, last_day=FRI)), "USD")[0]


def test_breakout_20d_is_strictly_above_the_20_closes_before_the_last():
    assert "breakout_20d" in tags([100] * 29 + [101])
    assert "breakout_20d" not in tags([100] * 29 + [100])                   # equal is not a breakout
    assert "breakout_20d" not in tags([100] * 9 + [102] + [100] * 19 + [101])   # 102 is 20 closes back: counts
    assert "breakout_20d" in tags([100] * 8 + [102] + [100] * 20 + [101])      # 102 is 21 back: outside


def test_bull_flag_run_over_10_sessions_before_the_last_5_and_a_5_percent_range():
    base = [100] * 15                                                         # c[-16] .. is index 14
    climb = [100 + i * D("1.1") for i in range(1, 11)]                        # c[-6] = 111
    flag = [D(111), D(112), D(111), D(112), D(111)]
    closes = [*base, *climb, *flag]
    assert len(closes) == 30 and closes[-16] == 100 and closes[-6] == 111
    assert "bull_flag" in tags(closes)                                        # +11%, range 1/111
    exact = [*base, *[100 + i for i in range(1, 11)], *[D(110)] * 5]          # c[-6] = 110: exactly +10%
    assert "bull_flag" not in tags(exact)                                     # Upstream: run > 0.10, strictly
    wide = [*base, *climb, D(111), D(117), D(111), D(111), D(111)]            # range 6/111 > 5%
    assert "bull_flag" not in tags(wide)
    # The run is c[-6] against c[-16]: a 5th-from-last close of 109 would read +9%, but c[-6] = 111 reads +11%.
    assert "bull_flag" in tags([*base, *climb, D(109), D(110), D(109), D(110), D("109.5")])
    # The range is the last 5 closes only: c[-6] = 117 is outside it (with it, 6/111 > 5%).
    steep = [*base, *[100 + i * D("1.7") for i in range(1, 11)], *flag]
    assert steep[-6] == 117 and "bull_flag" in tags(steep)


def test_reclaim_ma20_previous_close_under_its_average_then_close_over():
    closes = [100] * 28 + [90, 110]              # ma20_prev = (19*100 + 90)/20 = 99.5; ma20 = 100
    got, traces = daily_setups(liquid(daily("SPY", closes, last_day=FRI)), "USD")
    assert "reclaim_ma20" in got
    t = {x.condition: x for x in traces}
    assert t["reclaim_ma20: previous close < its 20-day average"].right.value == D("99.5")
    assert t["reclaim_ma20: close > the 20-day average"].right.value == D(100)
    assert "reclaim_ma20" not in tags([100] * 28 + [101, 110])               # never closed under
    assert "reclaim_ma20" not in tags([100] * 28 + [100, 110])               # AT its average is not under it


def test_oversold_and_oversold_bounce():
    falling = [200 - i for i in range(30)]
    assert "oversold" in tags(falling) and "oversold_bounce" not in tags(falling)
    bounce = [*falling[:-1], falling[-2] + 1]                                 # rsi was < 30, today closed up
    assert "oversold_bounce" in tags(bounce)
    assert not {"oversold", "oversold_bounce"} & set(tags([100 + i for i in range(30)]))
    # The upstream line is 30: an RSI of 37.66 is not oversold, and a bounce from 38.86 is not an oversold bounce.
    zigzag = [100]
    for i in range(29):
        zigzag.append(zigzag[-1] + (-3 if i % 2 == 0 else 2))
    assert "oversold" not in tags(zigzag)
    assert "oversold_bounce" not in tags([*zigzag[1:], zigzag[-1] + 2])


def scan(series, **kw):
    r, _ = dataset(series, interval=kw.pop("interval", Interval.D1))
    return scan_setups(r, CAL, "DS-T", kw.pop("setups", "daily"), as_of=kw.pop("as_of", SUN), now=NOW, **kw)


def test_floors_are_inclusive_and_history_is_30_bars():
    up = [100] * 29 + [101]
    res = scan({"OK": liquid(daily("OK", [*up[:-1], 5.5], last_day=FRI), 1_000_000),
                "CHEAP": liquid(daily("CHEAP", [*[4] * 29, D("4.99")], last_day=FRI)),
                "THIN": liquid(daily("THIN", up, last_day=FRI), 999_999),
                "EDGE": liquid(daily("EDGE", [*[4] * 29, 5], last_day=FRI), 1_000_000),
                "SHORT": liquid(daily("SHORT", up[-29:], last_day=FRI))})          # Upstream: at least 30
    by = {c.symbol: c for c in res.candidates}
    for thin in ("CHEAP", "THIN"):              # evaluated and under the floor: not eligible, not "unavailable"
        assert by[thin].eligibility is Eligibility.NOT_ELIGIBLE and by[thin].reasons == ("ILLIQUID",)
    assert by["EDGE"].eligibility is Eligibility.ELIGIBLE and "setup:breakout_20d" in by["EDGE"].reasons
    assert by["SHORT"].eligibility is Eligibility.UNAVAILABLE and by["SHORT"].reasons == ("MISSING_DATA",)
    assert by["SHORT"].traces[0].left.value == 29 and by["SHORT"].traces[0].right.value == 30
    assert any(lim.code == "NOT_A_BUY_LIST" for lim in res.limitations)


def test_daily_hits_rank_by_dollars_traded_and_the_interval_must_match():
    up = [100] * 29 + [101]
    res = scan({"SMALL": liquid(daily("SMALL", up, last_day=FRI), 1_500_000),
                "LARGE": liquid(daily("LARGE", up, last_day=FRI), 9_000_000)})
    assert [c.symbol for c in res.candidates] == ["LARGE", "SMALL"]
    with pytest.raises(ValidationError):
        scan({"X": liquid(daily("X", up, last_day=FRI))}, setups="orb")


# ---- the opening-range breakout ---------------------------------------------------------------------------------

MON = date(2026, 11, 23)
OPEN = CAL.open_close(MON)[0]


def orb(closes, **kw):
    bars = intraday("SPY", MON, closes)
    return scan({"SPY": bars}, interval=Interval.M5, setups="orb", **kw)


def test_orb_triggers_on_the_first_close_above_the_range_high_not_a_wick():
    # range: closes 100, 100.2, 100.1 -> highs up to 100.25. Bar 3 closes 100.22 (high 100.27: a wick above).
    # Bar 4 closes 100.30 > 100.25: the trigger, at its close (open + 25 minutes).
    res = orb([100, D("100.2"), D("100.1"), D("100.22"), D("100.3"), D("100.4")],
              as_of=OPEN + timedelta(minutes=31))
    (c,) = res.candidates
    assert c.eligibility is Eligibility.ELIGIBLE and c.traces[0].right.value == D("100.25")
    assert c.traces[0].left.value == D("100.3") and (OPEN + timedelta(minutes=25)).isoformat() in c.traces[0].condition
    wick_only = orb([100, D("100.2"), D("100.1"), D("100.22"), D("100.2")], as_of=OPEN + timedelta(minutes=26))
    assert wick_only.candidates[0].eligibility is Eligibility.NOT_ELIGIBLE


def test_orb_needs_every_slot_and_refuses_the_wrong_time():
    bars = intraday("SPY", MON, [100, D("100.2"), D("100.1"), D("100.3"), D("100.4")])
    holed = [b for i, b in enumerate(bars) if i != 1]
    res = scan({"SPY": holed}, interval=Interval.M5, setups="orb", as_of=OPEN + timedelta(minutes=26))
    assert res.candidates[0].reasons == ("MISSING_DATA",)
    with pytest.raises(DomainError):
        orb([100, 100, 100, 100], as_of=OPEN + timedelta(minutes=19))       # the first bar after the range is open
    with pytest.raises(DomainError):
        orb([100] * 6, as_of=SUN)                                            # no session
    with pytest.raises(ValidationError):
        orb([100] * 6, as_of=OPEN + timedelta(minutes=40), or_minutes=7)


def test_orb_hits_rank_by_trigger_time():
    early = intraday("ZED", MON, [100, 100, 100, 101, 101, 101])              # triggers at bar 3
    late = intraday("ABE", MON, [100, 100, 100, 100, 100, 101])               # triggers at bar 5
    res = scan({"ABE": late, "ZED": early}, interval=Interval.M5, setups="orb",
               as_of=OPEN + timedelta(minutes=31))
    assert [c.symbol for c in res.candidates] == ["ZED", "ABE"]               # by trigger time, not by name


def test_orb_a_close_equal_to_the_range_high_is_not_above_it():
    # range highs: 100.25 (close 100.2 + 0.05). Bar 3 closes at exactly 100.25: not above.
    res = orb([100, D("100.2"), D("100.1"), D("100.25")], as_of=OPEN + timedelta(minutes=21))
    assert res.candidates[0].eligibility is Eligibility.NOT_ELIGIBLE


def test_bull_flag_range_is_strictly_under_5_percent():
    closes = [90] * 15 + [90 + i for i in range(1, 11)] + [100, 105, 100, 100, 100]    # run +11.1%, range 5/100
    assert closes[-16] == 90 and closes[-6] == 100
    assert "bull_flag" not in tags(closes)
    assert "bull_flag" in tags([*closes[:-4], D("104.99"), 100, 100, 100])


class _Rsi:
    """rsi/1 stand-in giving fixed last two values, to pin the upstream 30 line exactly."""
    def __init__(self, prev, last):
        self.definition = type("Def", (), {"lookback": 141})()
        self.values = (D(prev), D(last))

    def compute(self, bars):
        return [None] * (len(bars) - 2) + list(self.values)


def test_rsi_exactly_30_is_not_oversold_and_a_flat_close_is_not_a_bounce(monkeypatch):
    from traderos.intelligence import setups
    monkeypatch.setattr(setups, "feature", lambda name, n: _Rsi(29, 30))
    got = tags([100] * 30)
    assert "oversold" not in got and "oversold_bounce" not in got           # 30 is not < 30; 100 is not > 100
    assert "oversold_bounce" in tags([100] * 29 + [101])


def test_rsi_reads_the_window_scan_criteria_reads():
    from traderos.features import feature
    closes = [100 + (i % 7) - (i % 3) * 2 + i / 10 for i in range(200)]
    bars = liquid(daily("SPY", closes, last_day=FRI))
    traces = {t.condition: t for t in daily_setups(bars, "USD")[1]}
    rsi = feature("rsi", 14)
    windowed = rsi.compute(bars[-rsi.definition.lookback:])[-1]
    assert traces["oversold: rsi(14) < 30"].left.value == windowed != rsi.compute(bars)[-1]


def test_a_stale_daily_name_is_unavailable():
    old = liquid(daily("OLD", [100] * 29 + [101], last_day=date(2026, 11, 13)))
    (c,) = scan({"OLD": old}).candidates
    assert c.eligibility is Eligibility.UNAVAILABLE and "DATA_STALE" in c.reasons


def test_orb_floor_is_upstream_5_dollars():
    cheap = intraday("PENNY", MON, [D("3.2"), D("3.2"), D("3.2"), D("3.4")])
    (c,) = scan({"PENNY": cheap}, interval=Interval.M5, setups="orb", as_of=OPEN + timedelta(minutes=21)).candidates
    assert c.eligibility is Eligibility.NOT_ELIGIBLE and c.reasons == ("ILLIQUID",)       # not scanned
    (c,) = scan({"PENNY": cheap}, interval=Interval.M5, setups="orb", as_of=OPEN + timedelta(minutes=21),
                min_price=D(0)).candidates
    assert c.eligibility is Eligibility.ELIGIBLE


def test_orb_expires_after_1530_and_replays_to_the_close():
    day = [100] * 3 + [101] + [100] * 74                                       # the whole session, 78 bars
    assert orb(day[:72], as_of=OPEN + timedelta(hours=6)).candidates            # 15:30 exactly: still allowed
    with pytest.raises(DomainError, match="expired"):
        orb(day[:73], as_of=OPEN + timedelta(hours=6, minutes=5))
    assert orb(day[:73], as_of=OPEN + timedelta(hours=6, minutes=5), until=None).candidates
    close = CAL.open_close(MON)[1]
    (c,) = orb(day, as_of=close, until=None).candidates                         # The upstream --at 16:00
    assert c.eligibility is Eligibility.ELIGIBLE


def test_orb_cutoff_on_a_bar_end_includes_that_bar_and_the_first_close_is_allowed():
    (c,) = orb([100, 100, 100, 101], as_of=OPEN + timedelta(minutes=20)).candidates   # first bar after the range
    assert c.eligibility is Eligibility.ELIGIBLE                                # ends exactly at as_of: it counts


def test_orb_needs_the_last_slot_too():
    bars = intraday("SPY", MON, [100] * 6)[:-1]                                 # the 9:55 bar is missing
    (c,) = scan({"SPY": bars}, interval=Interval.M5, setups="orb", as_of=OPEN + timedelta(minutes=31)).candidates
    assert c.reasons == ("MISSING_DATA",)


def test_a_thin_name_with_short_history_is_illiquid_first():
    (c,) = scan({"T": liquid(daily("T", [100] * 29, last_day=FRI), 10)}).candidates   # Upstream: illiquid, then short
    assert c.eligibility is Eligibility.NOT_ELIGIBLE and c.reasons == ("ILLIQUID",)
