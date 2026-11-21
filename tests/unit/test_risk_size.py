"""The upstream trader-budget tests (tank/day-trader-skill/tests/test_budget.py at 3d0f4ef, with PR #2's tests), ported
to traderos.risk.size, plus a regression test for each flaw ADR-009 names. Fixtures: tests/fixtures/risk (MIT, see
NOTICE there). The upstream numbers are kept; they are Decimals now.

Not ported, with the reason: the CLI tests of size.py's own --write/--json/--budget flags (the traderos CLI replaces
that CLI: tests/unit/test_risk.py drives it) and test_missing_plan_and_weights_only_flags (no such flags exist).
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime
from decimal import Decimal as D

import pytest

from traderos.contracts import DataError, ErrorCode, ValidationError
from traderos.risk import policy, size

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "risk"
TODAY = "2026-10-19"


def cfg(**over):
    c = dict(policy.EXAMPLE)
    c.update(over)
    return c


def plan(**over):
    p = json.loads((FIX / "plan-tsla.json").read_text(encoding="utf-8"))
    p.update(over)
    return p


def prices_doc(price, quote_time="2026-10-19T14:52:13Z"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {"TSLA": price},
            "detail": {"TSLA": {"prev_close": 354.72, "time": quote_time, "source": "yahoo"}}}


def ny(text):
    from traderos.clock import eastern_to_utc
    d, t = text.split(" ")
    return eastern_to_utc(datetime.fromisoformat(d).date(), datetime.strptime(t, "%H:%M").time())


def numbers(obj):
    """The upstream fixtures hold floats; compare them as the decimals they were written as."""
    if isinstance(obj, dict):
        return {k: numbers(v) for k, v in obj.items()}
    if isinstance(obj, float):
        return policy.number(obj)
    return obj


# ---- the reel: $5,000, 10% buffer, $50 max loss, TSLA risk 5.86 -> 8 shares, $2,913.44, max loss $46.88 ------------
def test_the_policy_example_is_upstream_config_without_the_broker_keys():
    upstream = json.loads((FIX / "config.example.json").read_text(encoding="utf-8"))
    for k in ("broker", "paper", "sec_user_agent"):
        upstream.pop(k)
    assert policy.policy(numbers(upstream)) == policy.policy(policy.EXAMPLE)
    assert set(upstream) == set(policy.EXAMPLE)
    # ADR-012: its broker keys are accepted again, paper only (its example says alpaca and paper: true)
    full = numbers(json.loads((FIX / "config.example.json").read_text(encoding="utf-8")))
    full.pop("sec_user_agent")
    assert policy.policy(full) == policy.policy(policy.PAPER_EXAMPLE)


def test_tsla_is_8_shares_bound_by_the_max_loss():
    s = size.size_trade(plan(), cfg(), today=TODAY)
    assert s["qty"] == 8 and s["bound_by"] == "max loss"
    assert (s["risk_per_share"], s["cost"], s["max_loss"], s["reward_risk"]) == (D("5.86"), D("2913.44"),
                                                                                 D("46.88"), D("1.00"))
    assert s["cash_buffer"] == 500
    assert [(lim["name"], lim["shares"]) for lim in s["limits"]] == [("max loss", 8), ("position cap", 10),
                                                                    ("max order", 10), ("cash", 12)]
    assert [lim["name"] for lim in s["limits"] if lim["binds"]] == ["max loss"]
    assert s["refusal"] is None


def test_the_bracket_order():
    o = size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY))
    why = o.pop("why")
    assert o == {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
                 "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32",
                 "plan": {"entry": D("364.18"), "stop": D("358.32"), "target": D("370.04"),
                          "risk_per_share": D("5.86"), "max_loss": D("46.88"), "setup": "orb", "entry_time": "10:15 AM",
                          "flat_by": "15:55"}}
    assert "bound by your $50 max loss" in why and "$46.88" in why


def test_the_fixture_orders_file_is_what_it_writes():
    fixture = json.loads((FIX / "orders-tsla.json").read_text(encoding="utf-8"))["orders"][0]
    assert size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY)) == numbers(fixture)


def test_a_limit_order():
    o = size.bracket_order(size.size_trade(plan(), cfg(), limit=364.18, today=TODAY))
    assert (o["type"], o["limit_price"], o["qty"]) == ("limit", "364.18", "8")
    s = size.size_trade(plan(), cfg(), limit=D("364.00"), today=TODAY)
    assert (s["risk_per_share"], s["qty"], s["max_loss"]) == (D("5.68"), 8, D("45.44"))
    assert size.bracket_order(s)["limit_price"] == "364.00"


def test_the_report_says_how_and_never_places():
    s = size.size_trade(plan(), cfg(), today=TODAY)
    text = size.plan_report(s, size.bracket_order(s))
    assert "Size: 8 shares of TSLA = $2,913.44 at 364.18" in text and "8 x 5.86 = $46.88" in text
    assert "<- binds" in text and "not financial advice" in text
    assert "place_stock_order" not in text and "trader-broker" not in text
    assert "nothing has been or can be placed" in text


# ---- the plan rules ------------------------------------------------------------------------------------------------
def test_zero_shares_is_a_plain_refusal():
    s = size.size_trade(plan(stop=300.0), cfg(), today=TODAY)
    assert s["qty"] == 0 and "more than your $50 max loss per trade" in s["refusal"]
    assert size.bracket_order(s) is None
    assert "No order:" in size.plan_report(s, None)


@pytest.mark.parametrize("key", ["max_loss_per_trade_usd", "max_position_pct", "max_order_usd", "budget"])
@pytest.mark.parametrize("bad", [0, -5, "fifty", True, "NaN", "Infinity", "-Infinity", float("nan"), float("inf")])
def test_a_limit_that_is_not_a_positive_finite_number_is_refused(key, bad):
    with pytest.raises(ValidationError) as e:
        size.size_trade(plan(), cfg(**{key: bad}), today=TODAY)
    assert e.value.message == f"{key} must be a positive finite number, got {bad!r}"


def test_a_null_limit_is_refused_and_a_missing_one_is_missing_data():
    for key in ("max_loss_per_trade_usd", "max_position_pct", "max_order_usd"):
        with pytest.raises(ValidationError) as e:
            size.size_trade(plan(), cfg(**{key: None}), today=TODAY)
        assert e.value.message == f"{key} must be a positive finite number, got None"
    with pytest.raises(DataError) as e:
        size.size_trade(plan(), cfg(budget=None), today=TODAY)
    assert "no budget" in e.value.message and e.value.code is ErrorCode.MISSING_DATA
    with pytest.raises(ValidationError) as e:
        size.size_trade(plan(), cfg(), budget=0, today=TODAY)
    assert "budget must be a positive finite number, got 0" in e.value.message


def test_no_default_sizes_a_trade():
    # The upstream DEFAULTS sized {"budget": 5000} with a $50 max loss nobody set (spec 15.6: never our defaults)
    with pytest.raises(DataError) as e:
        size.size_trade(plan(), {"budget": 5000}, today=TODAY)
    assert e.value.code is ErrorCode.MISSING_DATA and "max_loss_per_trade_usd" in e.value.message
    partial = cfg()
    partial.pop("max_daily_loss_usd")
    with pytest.raises(DataError) as e:
        size.size_trade(plan(), partial, today=TODAY)
    assert e.value.code is ErrorCode.MISSING_DATA and "max_daily_loss_usd" in e.value.message


def test_a_plan_without_a_date_is_refused():
    undated = plan()
    undated.pop("date")
    for p in (undated, plan(date=None), plan(date="")):
        with pytest.raises(ValidationError) as e:
            size.size_trade(p, cfg(), today=TODAY)
        assert e.value.message.startswith("the plan has no date")
    with pytest.raises(ValidationError):
        size.size_trade(undated, cfg())


def test_a_malformed_policy_is_one_typed_line():
    with pytest.raises(ValidationError) as e:
        size.size_trade(plan(), cfg(cash_buffer_pct="ten"), today=TODAY)
    assert e.value.message == "cash_buffer_pct must be a finite number of at least 0, got 'ten'"


def test_each_limit_can_bind():
    assert size.size_trade(plan(), cfg(max_order_usd=1000), today=TODAY)["bound_by"] == "max order"
    s = size.size_trade(plan(), cfg(max_position_pct=20), today=TODAY)
    assert (s["qty"], s["bound_by"]) == (2, "position cap")
    s = size.size_trade(plan(), cfg(), cash=1200, today=TODAY)
    assert (s["qty"], s["bound_by"], s["cash_available"]) == (1, "cash", 700)
    s = size.size_trade(plan(), cfg(max_loss_per_trade_usd=100), today=TODAY)
    assert (s["qty"], s["bound_by"]) == (10, "position cap")


def test_a_fresh_higher_price_means_a_bigger_risk():
    # the quote exactly (review item 1): upstream rounded 366.475 to 366.48 and measured 8.16 a share. 50 / 8.155 = 6.13
    # -> 6 shares, 6 x 8.155 = 48.93 at the stop; reward 3.565 / 8.155 = 0.437
    s = size.size_trade(plan(), cfg(min_reward_risk=0), price=366.475, today=TODAY)
    assert (s["entry"], s["entry_from"], s["risk_per_share"]) == (D("366.475"), "price", D("8.155"))
    assert (s["qty"], s["max_loss"], s["reward_risk"]) == (6, D("48.93"), D("0.44"))
    assert s["refusal"] is None
    s = size.size_trade(plan(), cfg(), price=366.475, today=TODAY)
    assert "reward:risk 0.44 (target 370.04, entry 366.475, stop 358.32) is under your minimum 1" in s["refusal"]
    assert size.bracket_order(s) is None
    lower = size.size_trade(plan(), cfg(), price=363.00, today=TODAY)
    assert (lower["entry"], lower["qty"]) == (D("364.18"), 8)


def test_a_price_is_used_only_when_fresh():
    p, notes = size.price_notes(prices_doc("366.475"), "TSLA", D("364.18"), ny("2026-10-19 10:55"))
    assert p == D("366.475") and "above the plan's entry" in notes[0]
    assert size.size_trade(plan(), cfg(), price=p, today=TODAY)["qty"] == 6
    p, notes = size.price_notes(prices_doc("366.475"), "TSLA", D("364.18"), ny("2026-10-19 11:30"))
    assert p is None and "not used" in notes[0]
    undated = {"prices": {"TSLA": "366.475"}}
    p, notes = size.price_notes(undated, "TSLA", D("364.18"), ny("2026-10-19 10:55"))
    assert p is None and "undated" in notes[0]
    assert size.price_notes(prices_doc("366.475"), "TSLA", D("364.18"), ny("2026-10-19 10:55"), limit=D("364")) == \
        (None, [])


def test_bad_plans():
    for p, word, code in ((plan(stop=365.0), "not below the entry", ErrorCode.INVALID_INPUT),
                          (plan(target=360.0), "already happened", ErrorCode.INVALID_INPUT),
                          (plan(target=None), "positive target", ErrorCode.INVALID_INPUT),
                          (plan(side="sell"), "long (buy) day trades only", ErrorCode.OUTSIDE_CAPABILITY),
                          (plan(symbol=""), "no symbol", ErrorCode.INVALID_INPUT)):
        with pytest.raises(ValidationError) as e:
            size.size_trade(p, cfg(), today=TODAY)
        assert word in e.value.message and e.value.code is code
    with pytest.raises(ValidationError) as e:
        size.size_trade(plan(), cfg(), price=371.00, today=TODAY)
    assert "already happened" in e.value.message
    with pytest.raises(ValidationError) as e:
        size.size_trade(plan(), cfg(blocked=["tsla"]), today=TODAY)
    assert "blocked list" in e.value.message and e.value.code is ErrorCode.NOT_AUTHORIZED
    with pytest.raises(ValidationError) as e:
        size.size_trade(plan(), cfg(), today="2026-10-20")
    assert "make a new plan" in e.value.message and e.value.code is ErrorCode.DATA_STALE


def test_under_the_minimum_order():
    cheap = plan(symbol="ABC", entry=4.0, stop=3.0, target=5.0)
    s = size.size_trade(cheap, cfg(max_loss_per_trade_usd=1), today=TODAY)
    assert s["qty"] == 1 and "under your $5 minimum order" in s["refusal"]
    assert size.bracket_order(s) is None


# ---- named changes (ADR-009) ----------------------------------------------------------------------------------------
def test_whole_contracts_with_a_multiplier():
    # one contract: risk (5.20 - 4.00) x 100 = 120, cost 5.20 x 100 = 520. Limits: $250 / 120 = 2; $3,750 / 520 = 7;
    # $4,000 / 520 = 7; $4,500 / 520 = 8 -> 2 contracts (never rounded up), max loss 240, cost 1,040
    s = size.size_trade(plan(symbol="ABC", entry="5.20", stop="4.00", target="8", multiplier="100"),
                        cfg(max_loss_per_trade_usd=250), today=TODAY)
    assert [lim["shares"] for lim in s["limits"]] == [2, 7, 7, 8]
    assert (s["qty"], s["max_loss"], s["cost"]) == (2, D("240.00"), D("1040.00"))
    o = size.bracket_order(s)
    assert o["plan"]["multiplier"] == 100 and "x 100" in s["limits"][0]["how"]
    with pytest.raises(ValidationError):
        size.size_trade(plan(multiplier=0), cfg(), today=TODAY)


def test_the_cash_row_is_settled_cash_less_reservations():
    # the adapter passes settled 1,500 less 300 reserved: 1,200 - 500 buffer = 700 / 364.18 -> 1 share
    s = size.size_trade(plan(), cfg(), cash=D("1500") - D("300"), today=TODAY)
    assert (s["qty"], s["bound_by"], s["cash_available"]) == (1, "cash", D("700.00"))
    assert "settled cash less reservations" in s["limits"][3]["how"]


def test_shares_for_counts_in_exact_cents():
    assert size.shares_for(D("50"), D("5.86")) == 8
    assert size.shares_for(D("0.30"), D("0.10")) == 3          # 0.3 / 0.1 in floats is 2.9999999999999996
    assert size.shares_for(D("0"), D("1")) == 0 and size.shares_for(D("1"), D("0")) == 0


def test_the_edges_of_the_plan_and_the_policy():
    for edge in (dict(target=364.18), dict(stop=364.18)):                       # at the entry is not beyond it
        with pytest.raises(ValidationError):
            size.size_trade(plan(**edge), cfg(), today=TODAY)
    for key in ("cash_buffer_pct", "min_order_usd"):
        with pytest.raises(ValidationError, match=f"{key} must be a finite number of at least 0"):
            size.size_trade(plan(), cfg(**{key: "-1"}), today=TODAY)
    for key in ("max_trades_per_day", "max_orders_per_day"):
        with pytest.raises(ValidationError, match="whole number"):
            size.size_trade(plan(), cfg(**{key: "2.5"}), today=TODAY)
    with pytest.raises(ValidationError, match="between 0 and 100"):
        size.size_trade(plan(), cfg(cash_buffer_pct="100"), today=TODAY)
