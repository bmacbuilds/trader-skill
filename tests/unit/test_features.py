"""traderos.features (spec 07.1). Reference values are worked by hand in the comments, never taken from an upstream
tool. What would have to break for this to go red:

* a value that reads a later bar (QT01), proven by changing later bars at random (hypothesis);
* a warm-up, a missing input or an undefined value that comes back as 0 instead of None;
* an incomplete bar used as an input, or one that resets a recursive feature;
* an unknown feature, parameter or field, or a period out of bounds, that is accepted.
"""
from __future__ import annotations

import dataclasses
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from traderos.contracts import ErrorCode, ValidationError, from_json, to_json
from traderos.contracts.interfaces import Feature
from traderos.contracts.market import Adjustment, Bar, Interval
from traderos.contracts.research import FeatureDefinition
from traderos.features import MAX_PERIOD, NAMES, feature

T0 = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)


def mk(ohlcv, *, complete=True, i=0) -> Bar:
    o, h, lo, c, v = (D(str(x)) for x in ohlcv)
    end = T0 + timedelta(days=i)
    return Bar(instrument_id="INS-T", interval=Interval.D1, start=end - timedelta(hours=6, minutes=30), end=end,
               open=o, high=h, low=lo, close=c, volume=v, available_at=end, provider="test",
               adjustment=Adjustment.RAW, complete=complete)


# Five bars used throughout: (open, high, low, close, volume)
ROWS = [(10, 11, 9, 10, 100), (10, 12, 10, 11, 200), (11, 13, 11, 12, 300), (12, 12, 10, 11, 150),
        (12, 14, 12, 13, 500)]
BARS = [mk(r, i=i) for i, r in enumerate(ROWS)]


def vals(name, params=None, bars=BARS):
    return feature(name, params).compute(bars)


def dz(*xs):
    return tuple(None if x is None else D(x) for x in xs)


def test_sma_by_hand():
    # closes 10 11 12 11 13: (10+11+12)/3 = 11; (11+12+11)/3 = 11.3333333333; (12+11+13)/3 = 12
    assert vals("sma", 3) == dz(None, None, "11", "11.3333333333", "12")


def test_ema_by_hand():
    # k = 2/(3+1) = 0.5. Seed = SMA of the first three = 11; then 11*.5+11*.5 = 11; 13*.5+11*.5 = 12
    assert vals("ema", 3) == dz(None, None, "11", "11", "12")


def test_rsi_by_hand():
    # changes +1 +1 -1 +2. Wilder(2): seed gain (1+1)/2=1, loss 0 -> 100. Then gain (1+0)/2=.5, loss (0+1)/2=.5
    # -> RS 1 -> 50. Then gain (.5+2)/2=1.25, loss (.5+0)/2=.25 -> RS 5 -> 100-100/6 = 83.3333333333
    assert vals("rsi", 2) == dz(None, None, "100", "50", "83.3333333333")


def test_atr_by_hand():
    # true ranges from bar 1: max(2,2,0)=2, max(2,2,0)=2, max(2,0,2)=2, max(2,3,1)=3.
    # Wilder(2): seed (2+2)/2 = 2; (2+2)/2 = 2; (2+3)/2 = 2.5
    assert vals("atr", 2) == dz(None, None, "2", "2", "2.5")


def test_return_gap_range_extremes_by_hand():
    # 11/10-1 = .1; 12/11-1 = .0909090909; 11/12-1 = -.0833333333; 13/11-1 = .1818181818
    assert vals("ret") == dz(None, "0.1", "0.0909090909", "-0.0833333333", "0.1818181818")
    assert vals("ret", 2) == dz(None, None, "0.2", "0", "0.0833333333")      # 12/10-1, 11/11-1, 13/12-1
    assert vals("gap") == dz(None, "0", "0", "0", "0.0909090909")            # open 12 over prior close 11
    assert vals("range") == dz("2", "2", "2", "2", "2")
    assert vals("highest", 2) == dz(None, "12", "13", "13", "14")            # of highs 11 12 13 12 14
    assert vals("lowest", 2) == dz(None, "9", "10", "10", "10")              # of lows 9 10 11 10 12
    assert vals("highest", {"period": 2, "field": "close"}) == dz(None, "11", "12", "12", "13")


def test_relative_volume_by_hand():
    # 300/mean(100,200)=2; 150/mean(200,300)=0.6; 500/mean(300,150)=2.2222222222
    assert vals("relvol", 2) == dz(None, None, "2", "0.6", "2.2222222222")


def test_realized_volatility_against_float_math():
    # an independent float computation, to 1e-9
    closes = [10, 11, 12, 11, 13]
    got = vals("rvol", 2)
    assert got[:2] == (None, None)
    for i in range(2, 5):
        logs = [math.log(closes[j] / closes[j - 1]) for j in (i - 1, i)]
        m = sum(logs) / 2
        want = math.sqrt(sum((x - m) ** 2 for x in logs) / 1)
        assert abs(float(got[i]) - want) < 1e-9, (i, got[i], want)


def test_undefined_is_none_never_zero():
    flat = [mk((5, 5, 5, 5, 0), i=i) for i in range(6)]
    assert vals("rsi", 2, flat)[2:] == dz("50", "50", "50", "50")   # no gains, no losses: neutral by convention
    assert vals("relvol", 2, flat) == (None,) * 6                     # zero mean volume: undefined, not 0
    # (a zero or negative price can't reach a feature: the bar/1 contract refuses it; the guards stay anyway)


def test_an_incomplete_bar_is_not_an_input():
    rows = [mk(r, i=i) for i, r in enumerate(ROWS[:3])]
    live = mk((99, 99, 1, 99, 9999), complete=False, i=3)
    bars = [*rows, live, mk(ROWS[4], i=4)]
    for name, params in (("sma", 3), ("ret", 1), ("gap", None), ("range", None), ("relvol", 2), ("highest", 2)):
        assert vals(name, params, bars)[3] is None, name
    # a window that includes it is unavailable too
    assert vals("sma", 3, bars)[4] is None
    # recursive features carry their state past it: EMA 13*.5 + 11*.5 = 12, as if the live bar were not there
    assert vals("ema", 3, bars) == dz(None, None, "11", None, "12")
    # RSI's next change is 13 - 12 (the last usable close), not 13 - 99
    assert vals("rsi", 2, bars)[4] == vals("rsi", 2, [*rows, mk(ROWS[4], i=4)])[3]


def test_every_feature_is_a_feature_with_a_valid_definition():
    for name in NAMES:
        f = feature(name) if name in ("gap", "range") else feature(name, 3)
        assert isinstance(f, Feature)
        d = f.definition
        assert d.formula_version == f"{name}/1" and d.min_history >= 1 and d.lookback >= d.min_history
        assert from_json(FeatureDefinition, to_json(d)) == d
    assert feature("sma", {"period": 5, "field": "volume"}).definition.units == "shares"
    assert feature("rsi", 14).definition.lookback == 141 and feature("rsi", 14).definition.min_history == 15
    assert feature("sma", 200).label == "sma(200)" and feature("ema", {"period": 9, "field": "high"}).label \
        == "ema(9, high)" and feature("gap").label == "gap()"


@pytest.mark.parametrize("name,params", [
    ("smaa", 3), ("sma", 0), ("sma", MAX_PERIOD + 1), ("sma", True), ("sma", 2.5), ("sma", "3"),
    ("sma", {"period": 3, "colour": "red"}), ("sma", {"period": 3, "field": "vwap"}), ("gap", 5), ("rvol", 1),
    ("rsi", {}),
])
def test_refused(name, params):
    with pytest.raises(ValidationError) as e:
        feature(name, params)
    assert e.value.code is ErrorCode.INVALID_STRATEGY


# ---- QT01: a later bar never changes an earlier value -----------------------------------------------------------
_price = st.integers(min_value=1, max_value=50_000).map(lambda n: D(n) / 100)


@st.composite
def series(draw):
    n = draw(st.integers(min_value=2, max_value=40))
    out = []
    for i in range(n):
        o, c = draw(_price), draw(_price)
        h = max(o, c) + draw(_price) / 10
        lo = max(min(o, c) - draw(_price) / 10, D("0.01"))
        out.append(mk((o, h, lo, c, draw(st.integers(0, 10**7))), complete=draw(st.booleans()) or i < n - 1, i=i))
    return out


ALL = [("sma", 3), ("ema", 4), ("rsi", 3), ("atr", 3), ("ret", 2), ("rvol", 3), ("relvol", 3), ("gap", None),
       ("range", None), ("highest", 3), ("lowest", 3)]


@settings(max_examples=60, deadline=None)
@given(bars=series(), cut=st.integers(min_value=0, max_value=39), seed=st.integers(0, 10**6))
def test_qt01_later_bars_never_change_earlier_values(bars, cut, seed):
    cut = min(cut, len(bars) - 1)
    changed = list(bars)
    for j in range(cut + 1, len(changed)):
        b = changed[j]
        bump = D(seed % 97 + 1)
        changed[j] = dataclasses.replace(b, open=b.open * bump, high=b.high * bump * 2, low=b.low,
                                         close=b.close * bump, volume=b.volume + seed)
    for name, params in ALL:
        f = feature(name, params)
        assert f.compute(bars)[:cut + 1] == f.compute(changed)[:cut + 1], name
