"""traderos.strategies.conditions (spec 07.2, 12.2 A). What would have to break for this to go red:

* untrusted content that is accepted when it should be refused: an unknown key, a float, a too-deep or too-large
  tree, bars_held outside a strategy exit, a comparison across units, two numbers compared;
* an unknown side counted as False instead of None, or Kleene logic got wrong in all/any;
* a trace that does not say which values were compared and why one was unavailable;
* a series evaluation that reads a later bar.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal as D

import pytest

from tests.unit.test_features import BARS, mk
from traderos.contracts import ErrorCode, ValidationError
from traderos.contracts.examples import RULES
from traderos.strategies.conditions import (
    MAX_DEPTH,
    MAX_LIST,
    MAX_NODES,
    Context,
    evaluate,
    evaluate_series,
    history_needed,
    parse,
    text,
)

SCAN = Context.SCAN


def ok(raw, ctx=SCAN):
    return parse(raw, context=ctx)


def refused(raw, ctx=SCAN):
    with pytest.raises(ValidationError) as e:
        parse(raw, context=ctx)
    assert e.value.code is ErrorCode.INVALID_STRATEGY
    return str(e.value)


def test_the_example_strategy_parses_in_its_own_contexts():
    entry = ok(RULES["entry"], Context.STRATEGY_ENTRY)
    exit_ = ok(RULES["exit"], Context.STRATEGY_EXIT)
    assert text(entry) == "all(close > sma(200), rsi(2) < 10)"
    assert text(exit_) == "any(close > sma(5), bars_held >= 10)"
    assert history_needed(entry) == 200 and history_needed(ok({"above": [{"rsi": 14}, 50]})) == 141


@pytest.mark.parametrize("raw,why", [
    ({"above": ["close", 10.5]}, "float"),
    ({"above": ["close"]}, "exactly two"),
    ({"above": ["close", "vwap"]}, "neither a bar field"),
    ({"above": [10, "20"]}, "two numbers"),
    ({"above": ["close", {"rsi": 2}]}, "units differ"),
    ({"above": ["volume", {"sma": 20}]}, "units differ"),
    ({"above": ["close", True]}, "true/false"),
    ({"above": ["close", "NaN"]}, "finite"),
    ({"above": ["close", {"sma": 20, "ema": 5}]}, "not a bar field"),
    ({"crosses": ["close", 10]}, "unknown condition"),
    ({"all": []}, "non-empty"),
    ({"all": [{"above": ["close", 1]}], "any": []}, "exactly one key"),
    ([{"above": ["close", 1]}], "exactly one key"),
    ({"bars_held": 10}, "only allowed in a strategy's exit"),
    ({"above": ["close", {"sma": 0}]}, "period"),
    ({"above": ["close", {"exec": "rm -rf /"}]}, "unknown feature"),
])
def test_untrusted_content_is_refused(raw, why):
    assert why in refused(raw)


def test_bars_held_is_bounded_even_in_an_exit():
    for bad in (0, -1, True, 1.5, "10"):
        assert "bars_held" in refused({"bars_held": bad}, Context.STRATEGY_EXIT)
    for ctx in (Context.SCAN, Context.ALERT, Context.STRATEGY_ENTRY):
        refused({"bars_held": 3}, ctx)


def test_size_is_bounded():
    deep = {"above": ["close", 1]}
    for _ in range(MAX_DEPTH):
        deep = {"all": [deep]}
    assert "deeper" in refused(deep)
    assert "lists more than" in refused({"any": [{"above": ["close", i]} for i in range(MAX_LIST + 1)]})
    wide = {"all": [{"all": [{"above": ["close", i]} for i in range(MAX_LIST)]} for _ in range(3)]}
    assert MAX_NODES < 3 * MAX_LIST
    assert f"more than {MAX_NODES}" in refused(wide)


def test_evaluate_says_why_with_traces():
    # closes 10 11 12 11 13; sma(3) at the last bar = 12; rsi(2) = 83.3333333333
    e = evaluate(ok({"all": [{"above": ["close", {"sma": 3}]}, {"below": [{"rsi": 2}, "90"]}]}), BARS)
    assert e.passed is True
    a, b = e.traces
    assert (a.condition, a.passed, a.left.value, a.right.value, a.feature_refs) == \
        ("close > sma(3)", True, D("13"), D("12"), ("sma/1",))
    assert (b.condition, b.passed, b.left.value, b.right.value) == ("rsi(2) < 90", True, D("83.3333333333"), D("90"))
    assert a.left.unit == "price" and b.left.unit == "index_0_100" and b.right.unit == "number"


def test_unknown_is_none_and_says_why():
    e = evaluate(ok({"above": ["close", {"sma": 10}]}), BARS)            # five bars; sma(10) needs ten
    (t,) = e.traces
    assert e.passed is None and t.passed is None and t.right.value is None
    assert "needs 10 complete bars, has 5" in t.reason
    live = [*BARS[:4], dataclasses.replace(BARS[4], complete=False)]
    e = evaluate(ok({"above": ["close", 1]}), live)
    assert e.passed is None and "not complete" in e.traces[0].reason
    e = evaluate(ok({"above": ["close", 1]}), [])
    assert e.passed is None and "no bars" in e.traces[0].reason


def test_kleene_logic():
    t, f = {"above": ["close", 1]}, {"below": ["close", 1]}             # true and false at every bar
    u = {"above": ["close", {"sma": 10}]}                               # unknown: not enough bars
    def p(raw):
        return evaluate(ok(raw), BARS).passed
    assert p({"all": [t, t]}) is True and p({"all": [t, f]}) is False and p({"all": [t, u]}) is None
    assert p({"all": [f, u]}) is False                                  # one False decides all
    assert p({"any": [f, f]}) is False and p({"any": [f, t]}) is True and p({"any": [f, u]}) is None
    assert p({"any": [t, u]}) is True                                   # one True decides any


def test_bars_held_in_an_exit():
    c = ok(RULES["exit"], Context.STRATEGY_EXIT)                        # close > sma(5) or bars_held >= 10
    assert evaluate(c, BARS, bars_held=10).passed is True
    assert evaluate(c, BARS, bars_held=3).passed is True                # close 13 > sma(5) = 11.4
    assert evaluate(c, BARS[:4], bars_held=3).passed is None            # sma(5) unknown, 3 < 10: unknown, not False
    assert evaluate(c, BARS[:4], bars_held=10).passed is True
    held = evaluate(c, BARS, bars_held=None)
    assert any("no position" in tr.reason for tr in held.traces)


def test_series_is_causal_and_matches_point_evaluation():
    c = ok({"above": ["close", {"sma": 2}]})
    series = evaluate_series(c, BARS)
    assert series == tuple(evaluate(c, BARS[:i + 1]).passed for i in range(len(BARS)))
    changed = [*BARS[:3], mk((1, 1, 1, 1, 1), i=3), mk((1, 1, 1, 1, 1), i=4)]
    assert evaluate_series(c, changed)[:3] == series[:3]
    with pytest.raises(ValidationError):
        evaluate_series(c, BARS, bars_held=[1, 2])
