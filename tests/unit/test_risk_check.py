"""The upstream trader-risk tests (tank/day-trader-skill/tests/test_risk.py at 3d0f4ef, with the tests of PRs #2, #21
and #48) and PR #55's tests/test_daily_risk_ledger.py, ported to traderos.risk.check; plus the review's listed cases and
a regression test for each flaw ADR-009 names (P4-P16). Fixtures: tests/fixtures/risk (MIT, NOTICE).

Where the port changes the upstream answer on purpose, the test says so and names the change. The upstream numbers are
kept; they are Decimals now. Every check gets an account statement and a log (FRESH, LOG): without them exposure is
UNKNOWN (P7), which the upstream fixtures never had to state.

Not ported, with the reason: test_cli and the Cli class (check.py's own CLI is replaced by `traderos risk assess`,
tested in tests/unit/test_risk.py); test_live_account_warns (live is refused in this build, ADR-012, so there is no
LIVE account to warn about: the policy refuses a "paper" that is not true, test_the_broker_keys_are_paper_only);
test_early_cutoff_agrees_with_trader_entry
compares with trader-entry/entry.py, which is not on this branch: the same cases are pinned by hand instead.
"""
from __future__ import annotations

import datetime
import json
import pathlib
from decimal import Decimal as D

import pytest

from traderos.clock import eastern_to_utc
from traderos.contracts import ErrorCode, TraderOSError, ValidationError
from traderos.risk import check, code_of, policy, tickets

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "risk"
NOW = "2026-10-19 10:15"
TODAY = "2026-10-19"
V1_TODAY = "2026-10-13"
PRICES = {"AAPL": "341.27", "AXP": "309.84", "GOOGL": "353.78", "KO": "90.11", "BAC": "53.96"}
QUOTE_TIME = datetime.datetime(2026, 10, 19, 14, 52, 13, tzinfo=datetime.UTC)  # TSLA 366.475, 10:52:13 NY
FRESH = {"settled_cash": "5000", "reserved_cash": "0", "positions": []}           # a fresh $5,000 account
LOG = {"orders": []}                                                              # nothing placed lately
# v1's rules with the day-trade checks switched off (0 / null), stated: the long-term checks still hold.
# max_loss_per_trade_usd must be positive since PR #2, so it is stated as a limit these orders never reach.
V1 = {"budget": "5000", "cash_buffer_pct": "10", "max_position_pct": "25", "max_order_usd": "2000",
      "min_order_usd": "5",
      "max_orders_per_day": 10, "no_entries_before": None, "no_entries_after": None, "max_trades_per_day": 0,
      "max_daily_loss_usd": "0", "max_loss_per_trade_usd": "100000", "min_reward_risk": "0", "blocked": [],
      "pdt_guard": False}


def at(text):
    """The upstream --now: a New York wall clock, or an instant with Z."""
    if text.endswith("Z"):
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    d, t = text.split(" ")
    return eastern_to_utc(datetime.date.fromisoformat(d), datetime.time.fromisoformat(t))


def cfg(**over):
    c = dict(policy.EXAMPLE)
    c.update(over)
    return c


def v1(**over):
    c = dict(V1)
    c.update(over)
    return c


def tsla(**over):
    o = json.loads((FIX / "orders-tsla.json").read_text(encoding="utf-8"))["orders"][0]
    o.update(over)
    return o


def bracket(sym, qty, entry, stop, target):
    return {"symbol": sym, "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": f"{target:.2f}",
            "stop_loss_stop_price": f"{stop:.2f}", "plan": {"entry": entry, "stop": stop, "target": target}}


def v1_orders():
    """The upstream v1 reel buys. They carry no stop, which is NO_PROTECTION since P10, so each gets a protective plan
    stop 10% under its price: the long-term checks these tests are about then answer exactly as the upstream one did."""
    orders = json.loads((FIX / "orders-reel.json").read_text(encoding="utf-8"))["orders"]
    return [with_stop(o) for o in orders]


def with_stop(o):
    p = D(PRICES.get(o["symbol"], "100"))
    return dict(o, plan={"stop": str((p * D("0.9")).quantize(D("0.01")))})


def buy(sym, usd):
    return with_stop({"symbol": sym, "side": "buy", "notional": str(usd), "type": "market", "time_in_force": "day"})


def placed(sym, side="buy", day=TODAY, max_loss=46.88, kind=None, status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T10:20:00-04:00"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


AUTO = object()


def run(orders, c=None, *, portfolio=FRESH, state=LOG, now=NOW, prices=AUTO, times=None, today=None):
    """The upstream check. Unless a test names its prices, each order with a plan entry gets a fresh quote AT that entry
    (review item 4: a market entry with no usable price blocks), so the entry is measured exactly as upstream's was."""
    instant = at(now) if isinstance(now, str) else now
    if prices is AUTO:
        prices = {o["symbol"]: str(o["plan"]["entry"]) for o in orders
                  if isinstance(o.get("plan"), dict) and o["plan"].get("entry") is not None}
        times = {s: instant for s in prices}
    return check.check(orders, c or cfg(), portfolio=portfolio, prices=prices, state=state, today=today,
                       now=instant, price_times=times)


def fresh_at(prices, now=NOW):
    return {s: at(now) for s in prices}


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


# ---- the reel ------------------------------------------------------------------------------------------------------
def test_the_tsla_bracket_passes_every_check():
    res = run([tsla()])
    r = res["results"][0]
    assert r["verdict"] == "PASS" and r["reasons"] == [] and r["warnings"] == []
    assert (r["kind"], r["entry_ref"], r["stop"], r["target"]) == ("entry", D("364.18"), D("358.32"), D("370.04"))
    assert (r["risk_per_share"], r["max_loss"], r["reward_risk"]) == (D("5.86"), D("46.88"), D("1.00"))
    assert r["value_usd"] == D("2913.44") and res["cash_after"] == D("2086.56") and res["date"] == TODAY
    assert "paper" not in res                       # F1 has no broker account


def test_nine_shares_is_blocked_by_the_max_loss_only():
    res = run([tsla(qty="9")])
    assert verdicts(res) == ["BLOCK"]
    assert res["results"][0]["reasons"] == [
        "loss: 9 shares x 5.86 risk a share = $52.74 at the stop, over your $50 max loss per trade"]


def test_valid_until_is_15_minutes_or_the_entry_cutoff_whichever_first():
    res = run([tsla()])
    assert res["checked_at"] == "2026-10-19T14:15:00Z"
    assert res["results"][0]["valid_until"] == "2026-10-19T14:30:00Z"            # 10:30 ET: 15 minutes
    res = run([tsla()], now="2026-10-19 15:20")
    assert res["results"][0]["valid_until"] == "2026-10-19T19:30:00Z"            # the 15:30 cutoff, not 15:35
    sell = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"}
    pf = {"settled_cash": "0", "reserved_cash": "0",
          "positions": [{"ticker": "TSLA", "shares": 8, "lots": [{"date": TODAY, "shares": 8, "cost": 1}]}]}
    res = run([sell], portfolio=pf, now="2026-10-19 15:50")                       # exits: 15 minutes only
    assert res["results"][0]["valid_until"] == "2026-10-19T20:05:00Z"


def test_the_reviews_expiry_cases():
    # Mon 2026-11-23, 10:15 ET, no cutoff: 15 minutes. 3:20 PM with no_entries_after 15:30: 3:30, not 3:35.
    assert run([tsla()], cfg(no_entries_after=None), now="2026-11-23 10:15")["results"][0]["valid_until"] == \
        "2026-11-23T15:30:00Z"
    assert run([tsla()], now="2026-11-23 15:20")["results"][0]["valid_until"] == "2026-11-23T20:30:00Z"


def test_one_fingerprint_shared_with_the_ticket():
    o = tsla()
    assert tickets.fingerprint is check.fingerprint
    assert check.fingerprint(dict(o, why="anything")) == check.fingerprint(o)
    assert check.fingerprint(dict(o, qty="9")) != check.fingerprint(o)
    assert check.fingerprint(dict(o, stop_loss_stop_price="358.00")) != check.fingerprint(o)


def test_fingerprint_covers_the_plans_levels():
    o = tsla()
    same = dict(o["plan"], setup="flag", max_loss=1, entry="364.18", stop="358.320", target="370.04")
    assert check.fingerprint(dict(o, plan=same)) == check.fingerprint(o)
    for k in ("entry", "stop", "target"):
        assert check.fingerprint(dict(o, plan=dict(o["plan"], **{k: 300.0}))) != check.fingerprint(o), k
    assert check.fingerprint(dict(o, plan={})) != check.fingerprint(o)
    no_plan = dict(o)
    no_plan.pop("plan")
    assert check.fingerprint(dict(o, plan={})) == check.fingerprint(no_plan)


# ---- the day-trade checks -----------------------------------------------------------------------------------------
def test_a_stop_is_required_whatever_the_policy_says():
    o = tsla()
    for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price", "plan"):
        o.pop(k)
    o["plan"] = {"entry": 364.18}
    res = run([o])
    assert "stop: no stop" in reasons(res)
    assert code_of(res["results"][0]["reasons"][0]) is ErrorCode.NO_PROTECTION
    # P10: upstream's "require_stop": false let it PASS with a warning. Now the policy cannot say false.
    with pytest.raises(ValidationError, match="require_stop may only be true"):
        run([o], cfg(require_stop=False, min_reward_risk=0))


def test_a_plan_stop_counts_but_warns():
    o = tsla()
    for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price"):
        o.pop(k)
    res = run([o])
    assert verdicts(res) == ["PASS"] and "in the plan only" in warnings(res)
    assert res["results"][0]["max_loss"] == D("46.88")


def test_stop_and_target_on_the_right_side():
    assert "must be below the entry 364.18" in reasons(run([tsla(stop_loss_stop_price="365.00")]))
    assert "target: the target 364.00 must be above the entry 364.18" in \
        reasons(run([tsla(take_profit_limit_price="364.00")]))


def test_reward_risk():
    assert "reward:risk: 0.65" in reasons(run([tsla(take_profit_limit_price="368.00")]))
    assert verdicts(run([tsla(take_profit_limit_price="368.00")], cfg(min_reward_risk="0.5"))) == ["PASS"]


def test_a_fresh_higher_price_is_the_entry():
    res = run([tsla()], now="2026-10-19 10:55", prices={"TSLA": "366.475"}, times={"TSLA": QUOTE_TIME})
    r = res["results"][0]
    # the quote exactly, never rounded (review item 1): upstream rounded 366.475 to 366.48 and measured 8.16 a share
    assert (r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]) == (D("366.475"), "price", D("8.155"))
    assert "loss: 8 shares x 8.155 risk a share = $65.24" in reasons(res)


def test_a_stale_or_future_price_blocks_a_market_entry():
    # Upstream (and PR #48's third case): PASS with a warning. Review change 4 to #2 (P5): a market entry needs a fresh
    # price, because the plan's entry cannot show whether the price already ran through the stop.
    res = run([tsla()], now="2026-10-19 11:30", prices={"TSLA": "366.475"}, times={"TSLA": QUOTE_TIME})
    assert verdicts(res) == ["BLOCK"] and res["results"][0]["entry_ref"] == D("364.18")
    assert "older than 15 minutes" in reasons(res)
    assert code_of(res["results"][0]["reasons"][0]) is ErrorCode.DATA_STALE
    res = run([tsla()], now="2026-10-19 10:15", prices={"TSLA": "366.475"}, times={"TSLA": QUOTE_TIME})
    assert verdicts(res) == ["BLOCK"] and "stamped" in reasons(res) and "future" in reasons(res)
    assert "older than 15 minutes" not in reasons(res)
    # a limit order is measured from its own price: a stale quote does not block it
    res = run([tsla(type="limit", limit_price="364.00")], now="2026-10-19 11:30", prices={"TSLA": "366.475"},
              times={"TSLA": QUOTE_TIME})
    assert verdicts(res) == ["PASS"]


def test_a_stale_price_blocks_a_market_entry_with_no_plan():
    order = {"symbol": "KO", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day"}
    res = run([order], now="2026-10-19 11:30", prices={"KO": "90.11"}, times={"KO": QUOTE_TIME})
    assert verdicts(res) == ["BLOCK"]
    assert "no fresh price to size a market entry" in reasons(res) and "older than 15 minutes" in reasons(res)


def test_an_untimed_price_is_not_fresh():
    # P16: the upstream price_times=None counted every price as fresh. A price with no time is not used, and a market
    # entry with a plan is blocked for it.
    res = run([tsla()], prices={"TSLA": "358.00"})
    assert verdicts(res) == ["BLOCK"] and "has no timestamp" in reasons(res)
    assert "already at or below the stop" not in reasons(res)


def test_a_lower_price_does_not_shrink_the_risk():
    res = run([tsla()], prices={"TSLA": "360.00"}, times=fresh_at(["TSLA"]))
    assert res["results"][0]["entry_ref"] == D("364.18") and verdicts(res) == ["PASS"]


def test_a_fresh_price_through_the_stop_blocks():
    # AMZN, 2026-10-29: plan entry 248.79, stop 244.85, last 244.67. The plan's entry hid it.
    res = run([tsla()], prices={"TSLA": "358.00"}, times=fresh_at(["TSLA"]))
    assert verdicts(res) == ["BLOCK"]
    assert "stop: the last price 358.00 is already at or below the stop 358.32" in reasons(res)


def test_a_fresh_price_through_the_target_blocks():
    res = run([tsla()], prices={"TSLA": "370.50"}, times=fresh_at(["TSLA"]))
    assert verdicts(res) == ["BLOCK"]
    assert "target: the last price 370.50 is already at or above the target 370.04" in reasons(res)


def test_a_short_through_its_stop_blocks():
    # the review's #48 case: short entry 100.00, stop 102.00, target 96.00, fresh 102.10
    short = {"symbol": "XYZ", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "96.00", "stop_loss_stop_price": "102.00",
             "plan": {"entry": "100.00"}}
    res = run([short], cfg(allow_short=True), prices={"XYZ": "102.10"}, times=fresh_at(["XYZ"]))
    assert "at or above the stop 102.00" in reasons(res)


def test_a_limit_order_uses_its_limit_price():
    res = run([tsla(type="limit", limit_price="364.00")], prices={"TSLA": "366.475"}, times=fresh_at(["TSLA"]))
    r = res["results"][0]
    assert (r["entry_ref"], r["max_loss"], r["verdict"]) == (D("364.00"), D("45.44"), "PASS")


def test_daily_loss_and_trades_per_day():
    res = run([tsla()], state={"orders": [placed("AAPL"), placed("AMD")]})
    assert verdicts(res) == ["PASS"]
    assert "today before this: 2 of 3 trades, $93.76 of $150 daily loss" in res["results"][0]["info"][-1]
    res = run([tsla()], state={"orders": [placed("AAPL"), placed("AMD"), placed("NVDA")]})
    why = reasons(res)
    assert "trades: 3 entries placed today" in why
    assert "daily loss: $140.64 already at risk today plus $46.88 for this one = $187.52, over your $150" in why
    yesterday = {"orders": [placed(s, day="2026-10-16") for s in ("AAPL", "AMD", "NVDA")]}
    assert verdicts(run([tsla()], state=yesterday)) == ["PASS"]


def test_an_approval_with_no_result_blocks_new_entries():
    # PR #2 counted it as placed and warned; the review's change 4 (P4): it BLOCKs every new entry until resolved.
    appr = dict(placed("TSLA", kind="entry", status="approved"), fingerprint=check.fingerprint(tsla()),
                at=TODAY + "T10:10:00-04:00", plain="BUY 8 TSLA, market, good for today")
    want = "an approval for BUY 8 TSLA, market, good for today at 2026-10-19T10:10:00-04:00 has no result recorded"
    res = run([tsla()], state={"orders": [appr]})
    assert verdicts(res) == ["BLOCK"]
    assert "duplicate: a buy TSLA was already placed today" in reasons(res)
    assert "exposure: existing exposure unknown" in reasons(res)
    assert res["warnings"] == [want]
    assert (res["today"]["orders_placed"], res["today"]["entries_placed"], res["today"]["unresolved_approvals"],
            res["today"]["loss_at_risk"]) == (1, 1, 1, D("46.88"))
    # the review's case: a new AAPL entry is BLOCKed too, UNKNOWN_EXPOSURE
    res = run([bracket("AAPL", 1, 50.0, 49.0, 52.0)], state={"orders": [appr]})
    assert verdicts(res) == ["BLOCK"] and "exposure: existing exposure unknown" in reasons(res)
    assert ErrorCode.UNKNOWN_EXPOSURE in {code_of(r) for r in res["results"][0]["reasons"]}
    # three of them use up the trades and the daily loss
    three = [dict(appr, symbol=s, fingerprint="fp-" + s, plain="BUY " + s) for s in ("AAPL", "AMD", "NVDA")]
    res = run([tsla()], state={"orders": three})
    assert "trades: 3 entries placed today" in reasons(res)
    assert "daily loss: $140.64 already at risk today" in reasons(res)
    assert sum(1 for w in res["warnings"] if "no result recorded" in w) == 3
    ten = [dict(appr, symbol=f"T{i}", fingerprint=f"fp-{i}", kind="exit", max_loss=None) for i in range(10)]
    assert "daily limit: 10 placed today" in reasons(run([tsla()], state={"orders": ten}))
    # an outcome recorded after the approval resolves it
    for st in ("placed", "denied", "failed"):
        done = dict(appr, status=st, at=TODAY + "T10:11:00-04:00")
        res = run([tsla()], state={"orders": [appr, done]})
        assert res["warnings"] == [], st
        assert verdicts(res) == ["BLOCK" if st == "placed" else "PASS"], st
    # an outcome for another order, or an approval from another day, does not
    other = dict(appr, status="placed", fingerprint="fp-other", at=TODAY + "T10:11:00-04:00")
    assert run([tsla()], state={"orders": [appr, other]})["warnings"] == [want]
    old = dict(appr, date="2026-10-16", at="2026-10-16T10:10:00-04:00")
    res = run([tsla()], state={"orders": [old]})
    assert (verdicts(res), res["warnings"]) == (["PASS"], [])


def test_exits_and_declines_do_not_count_as_trades():
    st = {"orders": [placed("AAPL"), placed("AAPL", side="sell", kind="exit", max_loss=None),
                     placed("AMD", status="declined"), placed("NVDA", status="failed")]}
    res = run([tsla()], state=st)
    assert verdicts(res) == ["PASS"] and res["today"]["entries_placed"] == 1


def test_the_batch_counts_toward_the_daily_limits():
    res = run([bracket(s, 1, 50.0, 10.0, 90.0) for s in ("AAA", "BBB", "CCC", "DDD")])
    assert verdicts(res) == ["PASS", "PASS", "PASS", "BLOCK"]
    why = reasons(res, 3)
    assert "trades: 0 entries placed today plus 3 passed earlier" in why
    assert "daily loss: $120 already at risk today plus $40 for this one = $160" in why


@pytest.mark.parametrize("now,want", [
    ("2026-10-19 09:44", "no new entries before 09:45 New York (it is 09:44)"),
    ("2026-10-19 15:31", "no new entries after 15:30 New York (it is 15:31)"),
    ("2026-10-19 08:00", "the market is closed (Mon 2026-10-19 08:00"),
    ("2026-10-19 16:05", "the market is closed"),
    ("2026-10-17 10:15", "the market is closed today (weekend"),
    ("2026-11-26 10:15", "the market is closed today (NYSE holiday: Thanksgiving Day"),
    ("2026-11-27 12:31", "no new entries after 12:30 New York"),
    ("2026-11-27 13:05", "the market is closed"),
    ("2026-11-02T14:40:00Z", "no new entries before 09:45 New York (it is 09:40)"),
])
def test_entry_window_new_york_time(now, want):
    assert "window: " + want in reasons(run([tsla()], now=now))


@pytest.mark.parametrize("now", ["2026-10-19 09:45", "2026-10-19 15:30", "2026-11-27 12:30", "2026-11-02T15:15:00Z",
                                 "2026-10-30T14:40:00Z", "2026-03-09T13:50:00Z"])
def test_inside_the_window_passes(now):
    assert verdicts(run([tsla()], now=now)) == ["PASS"]


def test_the_window_when_it_is_switched_off_and_the_early_close():
    assert "early close" in " ".join(run([tsla()], now="2026-11-27 12:30")["warnings"])
    off = cfg(no_entries_before=None, no_entries_after=None)
    assert "window: the market is closed today (weekend" in reasons(run([tsla()], off, now="2026-10-17 10:15"))
    assert "window: the market is closed today (weekend" in reasons(run([tsla()], off, now="2026-11-21 10:00"))
    assert verdicts(run([tsla()], off, now="2026-10-19 10:15")) == ["PASS"]
    with pytest.raises(ValidationError):
        run([tsla()], cfg(no_entries_before="9.45am"))


def test_a_day_outside_the_calendar_is_calendar_unknown():
    # P13: upstream warned that its holiday list covered 2026-2027 only and let the entry through
    res = run([tsla()], now="2030-01-15 10:15")
    assert verdicts(res) == ["BLOCK"]
    (reason,) = [r for r in res["results"][0]["reasons"] if r.startswith("window:")]
    assert "the session is unknown" in reason and code_of(reason) is ErrorCode.CALENDAR_UNKNOWN


def test_early_cutoff_by_hand():
    day = datetime.date(2026, 11, 27)                   # a 1:00 p.m. early close
    for hhmm, close, want in (("15:30", check.EARLY, "12:30"), ("12:00", check.EARLY, "12:00"),
                              ("12:30", check.EARLY, "12:30"), ("09:45", check.EARLY, "09:45"),
                              ("15:30", check.CLOSE, "15:30")):
        got = check.early_cutoff(day, check.hhmm(hhmm, "no_entries_after"), close)
        assert got.strftime("%H:%M") == want, hhmm


def test_the_window_uses_new_york_time_not_the_machine():
    assert check.to_eastern(at("2026-10-19 10:15")) == datetime.datetime(2026, 10, 19, 10, 15)
    assert at("2026-10-19 10:15") == datetime.datetime(2026, 10, 19, 14, 15, tzinfo=datetime.UTC)
    assert at("2026-12-01 10:15") == datetime.datetime(2026, 12, 1, 15, 15, tzinfo=datetime.UTC)
    with pytest.raises(ValidationError):
        check.check([tsla()], cfg(), FRESH, state=LOG, now=datetime.datetime(2026, 10, 19, 14, 15))   # naive


def test_pdt_guard():
    def trade(day):
        return [placed("X" + day[-2:], day=day), placed("X" + day[-2:], side="sell", day=day, kind="exit",
                                                        status="filled", max_loss=None)]
    st = {"orders": trade("2026-10-13") + trade("2026-10-14") + trade("2026-10-15")}
    assert verdicts(run([tsla()])) == ["PASS"]
    assert "pdt: 3 day trades in the last 5 business days" in reasons(run([tsla()], cfg(pdt_guard=True), state=st))
    assert verdicts(run([tsla()], state=st)) == ["PASS"]
    old = {"orders": trade("2026-10-12") + trade("2026-10-14") + trade("2026-10-15")}
    assert verdicts(run([tsla()], cfg(pdt_guard=True), state=old)) == ["PASS"]
    pending = {"orders": trade("2026-10-14") + trade("2026-10-15") + [placed("AMD")]}
    assert "pdt: 3 day trades" in reasons(run([tsla()], cfg(pdt_guard=True), state=pending))
    buy_only = {"orders": [placed("AMD", day="2026-10-15"), placed("NVDA", day="2026-10-16")]}
    assert check.day_trades(buy_only, TODAY)[0] == 0


def test_a_short_entry_when_allowed():
    short = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "358.32", "stop_loss_stop_price": "370.04",
             "plan": {"entry": 364.18}}
    assert "short: selling more TSLA than you hold" in reasons(run([short]))
    r = run([short], cfg(allow_short=True))["results"][0]
    assert (r["verdict"], r["kind"], r["max_loss"], r["reward_risk"]) == ("PASS", "entry", D("46.88"), D("1.00"))
    assert "must be above the entry 364.18 for a sell" in \
        reasons(run([dict(short, stop_loss_stop_price="360.00")], cfg(allow_short=True)))


def test_bracket_format():
    assert "whole-share qty" in reasons(run([dict(tsla(qty=None), notional="2900")]))
    assert "whole shares only" in reasons(run([tsla(qty="8.5")]))
    assert "time_in_force day or gtc" in reasons(run([tsla(time_in_force="ioc")]))
    assert verdicts(run([tsla(time_in_force="gtc")])) == ["PASS"]
    o = tsla()
    o.pop("take_profit_limit_price")
    assert "needs both take_profit_limit_price and stop_loss_stop_price" in reasons(run([o]))
    assert "needs a stop_loss_stop_price" in reasons(run([dict(tsla(), stop_loss_stop_price=None,
                                                               stop_loss_limit_price="357.00")]))
    assert "as a string" in warnings(run([tsla(stop_loss_stop_price=358.32)]))


@pytest.mark.parametrize("field,bad", [("qty", "NaN"), ("qty", "Infinity"), ("qty", float("nan")),
                                       ("stop_loss_stop_price", "NaN"), ("take_profit_limit_price", "inf"),
                                       ("qty", True)])
def test_a_non_finite_order_number_is_a_format_block(field, bad):
    # P8: qty "NaN" passed every check and the ticket read "BUY NaN shares"
    res = run([tsla(**{field: bad})])
    assert verdicts(res) == ["BLOCK"] and f"format: {field} must be a positive number" in reasons(res)


def test_a_non_finite_account_is_refused():
    # P9: a NaN cash turned every cash and position check off
    for bad in ({"settled_cash": "NaN"}, {"settled_cash": float("nan")}, {"reserved_cash": "-1"},
                {"settled_cash": None}):
        with pytest.raises(ValidationError):
            run([tsla()], portfolio=dict(FRESH, **bad))
    with pytest.raises(ValidationError):
        run([tsla()], portfolio=dict(FRESH, positions=[{"symbol": "KO", "shares": "NaN"}]))


def test_no_account_or_no_log_is_unknown_exposure():
    # P7: upstream assumed a fresh account funded with the budget, and nothing placed today
    for kw in ({"portfolio": None}, {"state": None}):
        res = run([tsla()], **kw)
        assert verdicts(res) == ["BLOCK"]
        assert ErrorCode.UNKNOWN_EXPOSURE in {code_of(r) for r in res["results"][0]["reasons"]}
    assert run([tsla()], portfolio=None)["cash_after"] is None


def test_extended_hours():
    lim = {"symbol": "KO", "side": "buy", "qty": "1", "type": "limit", "limit_price": "90",
           "time_in_force": "gtc", "extended_hours": True}
    times = fresh_at(PRICES, V1_TODAY + " 10:15")
    res = run([with_stop(lim)], v1(), prices=PRICES, times=times, today=V1_TODAY, now=V1_TODAY + " 10:15")
    assert verdicts(res) == ["PASS"]
    res = run([with_stop(dict(lim, type="market", limit_price=None))], v1(), prices=PRICES, times=times,
              today=V1_TODAY, now=V1_TODAY + " 10:15")
    assert "extended hours needs a limit order" in reasons(res)


# ---- exits ---------------------------------------------------------------------------------------------------------
PF = {"settled_cash": "2086.56", "reserved_cash": "0",
      "positions": [{"ticker": "TSLA", "shares": 8, "lots": [{"date": TODAY, "shares": 8, "cost": "2913.44"}]}]}
SELL = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"}


def test_getting_out_is_never_blocked_by_the_day_limits():
    full = {"orders": [placed("TSLA"), placed("AMD"), placed("NVDA")] + [placed(f"T{i}", max_loss=1)
                                                                          for i in range(6)]
            + [placed("TSLA", side="sell", kind="exit", max_loss=None)]}
    c = cfg(blocked=["TSLA"], max_order_usd=100, max_daily_loss_usd=50)
    r = run([SELL], c, portfolio=PF, state=full, now="2026-10-19 15:58")["results"][0]
    assert (r["verdict"], r["kind"]) == ("PASS", "exit"), r["reasons"]
    why = " | ".join(r["warnings"])
    for word in ("blocked:", "duplicate:", "daily limit:"):
        assert "exit, not blocked: " + word in why
    assert "price" not in why
    assert verdicts(run([SELL], portfolio=PF, now="2026-10-17 12:00")) == ["PASS"]


def test_a_partial_exit_and_the_rest():
    half = dict(SELL, qty="4")
    res = run([half, half], portfolio=PF, prices={"TSLA": "366.0"}, times=fresh_at(["TSLA"]))
    assert verdicts(res) == ["PASS", "PASS"] and [r["kind"] for r in res["results"]] == ["exit", "exit"]
    assert "exit, not blocked: duplicate" in warnings(res, 1)
    res = run([half, half, half], portfolio=PF, prices={"TSLA": "366.0"}, times=fresh_at(["TSLA"]))
    assert verdicts(res) == ["PASS", "PASS", "BLOCK"] and "short:" in reasons(res, 2)


def test_format_asset_and_short_still_apply_to_a_sell():
    assert "needs a limit_price" in reasons(run([dict(SELL, type="limit")], portfolio=PF))
    assert "short: selling more TSLA than you hold (8 shares)" in reasons(run([dict(SELL, qty="9")], portfolio=PF))
    assert "add it to your account statement first" in reasons(run([SELL]))


def test_buy_then_sell_in_one_batch():
    res = run([tsla(), SELL])
    assert verdicts(res) == ["PASS", "PASS"] and res["results"][1]["kind"] == "exit"


def test_unknown_risk_never_blocks_a_closing_sell():
    portfolio = {"settled_cash": "2000", "reserved_cash": "0", "positions": [{"ticker": "TSLA", "shares": 8}]}
    res = run([SELL, tsla()], portfolio=portfolio, state={"orders": [placed("AAPL", max_loss=None)]},
              prices={"TSLA": "364.18"}, times=fresh_at(["TSLA"]))
    assert verdicts(res) == ["PASS", "BLOCK"] and res["results"][0]["kind"] == "exit"


# ---- v1's long-term checks (day-trade checks off) ------------------------------------------------------------------
def v1_one(order, **over):
    return run([order], v1(**over), prices=PRICES, times=fresh_at(PRICES, V1_TODAY + " 10:15"), today=V1_TODAY,
               now=V1_TODAY + " 10:15")["results"][0]


def v1_run(orders, **kw):
    kw.setdefault("prices", PRICES)
    kw.setdefault("times", fresh_at(PRICES, V1_TODAY + " 10:15"))
    return run(orders, kw.pop("c", v1()), today=V1_TODAY, now=V1_TODAY + " 10:15", **kw)


def test_upstream_stopless_v1_buys_are_no_protection():
    orders = json.loads((FIX / "orders-reel.json").read_text(encoding="utf-8"))["orders"]
    res = v1_run(orders)
    assert verdicts(res) == ["BLOCK"] * 5
    assert all(code_of(r) is ErrorCode.NO_PROTECTION for x in res["results"] for r in x["reasons"])


def test_the_five_buys_pass_on_a_fresh_5000_account():
    res = v1_run(v1_orders())
    assert verdicts(res) == ["PASS"] * 5 and res["cash_after"] == D("500.00")
    assert res["results"][0]["value_usd"] == D("1250") and res["results"][0]["amount"] == "$1,250"


def test_a_sixth_buy_is_blocked_three_ways():
    res = v1_run([*v1_orders(), buy("AAPL", 100)])
    assert verdicts(res) == ["PASS"] * 5 + ["BLOCK"]
    why = reasons(res, 5)
    assert "cash:" in why and "position:" in why and "duplicate:" in why


def test_the_example_policy_blocks_buys_without_a_stop():
    orders = json.loads((FIX / "orders-reel.json").read_text(encoding="utf-8"))["orders"]
    res = run(orders)
    assert verdicts(res)[0] == "BLOCK" and "stop: no stop" in reasons(res)


def test_max_and_min_order():
    assert "over your $2,000 max order" in " ".join(v1_one(buy("KO", 2500), max_position_pct="100")["reasons"])
    assert "under your $5 minimum" in " ".join(v1_one(buy("KO", 2))["reasons"])


def test_blocked_list():
    r = v1_one(buy("KO", 100), blocked=["KO"])
    assert r["verdict"] == "BLOCK" and "blocked list" in r["reasons"][0]


def test_options_off_unless_allowed():
    opt = with_stop({"symbol": "BE270617C00100000", "side": "buy", "qty": "1", "type": "limit",
                     "limit_price": "12.50", "time_in_force": "day"})
    opt["plan"]["stop"] = "10.00"
    r = v1_one(opt)
    assert r["verdict"] == "BLOCK" and "option" in " ".join(r["reasons"])
    assert code_of(r["reasons"][0]) is ErrorCode.OUTSIDE_CAPABILITY
    r = v1_one(opt, allow_options=True)
    assert r["verdict"] == "PASS" and r["warnings"]


def test_crypto_and_junk_symbols():
    crypto = with_stop({"symbol": "BTC/USD", "side": "buy", "notional": "50", "type": "market",
                        "time_in_force": "day"})
    assert "stocks and ETFs only" in " ".join(v1_one(crypto)["reasons"])


def test_format_rules():
    assert "exactly one of qty or notional" in " ".join(v1_one(dict(buy("KO", 100), qty="1"))["reasons"])
    lim = dict(buy("KO", 100), type="limit", limit_price="90")
    assert "market order with time_in_force day" in " ".join(v1_one(lim)["reasons"])
    nolimit = with_stop({"symbol": "KO", "side": "buy", "qty": "1", "type": "limit", "time_in_force": "day"})
    assert "needs a limit_price" in " ".join(v1_one(nolimit)["reasons"])
    assert "side must be buy or sell" in " ".join(v1_one({"symbol": "KO", "side": "hold", "notional": "100"})
                                                   ["reasons"])


def test_numbers_should_be_strings():
    r = v1_one(with_stop({"symbol": "KO", "side": "buy", "notional": 100, "type": "market", "time_in_force": "day"}))
    assert r["verdict"] == "PASS" and "as a string" in r["warnings"][0]


def test_share_orders_need_a_price():
    q = with_stop({"symbol": "ZZZZ", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day"})
    assert "no price for ZZZZ" in " ".join(v1_one(q)["reasons"])
    r = v1_one(dict(q, symbol="KO", plan={"stop": "81.10"}))
    assert r["verdict"] == "PASS" and r["value_usd"] == D("180.22")


def test_margin():
    r = run([buy("KO", 1500)], v1(cash_buffer_pct="0", max_position_pct="100"), prices=PRICES,
            times=fresh_at(PRICES, V1_TODAY + " 10:15"), portfolio={"settled_cash": "1000", "reserved_cash": "0",
                                                                     "positions": []},
            today=V1_TODAY, now=V1_TODAY + " 10:15")["results"][0]
    assert "margin" in " ".join(r["reasons"])


KO_PF = {"settled_cash": "500", "reserved_cash": "0",
         "positions": [{"ticker": "KO", "shares": "7.8571", "lots": [{"date": "2026-10-13", "shares": "7.8571",
                                                                      "cost": "708"}]}]}


def test_short_sales_are_blocked():
    sell = {"symbol": "KO", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day"}
    assert "short:" in " ".join(v1_run([sell], portfolio=KO_PF)["results"][0]["reasons"])
    assert v1_run([dict(sell, qty="3")], portfolio=KO_PF)["results"][0]["verdict"] == "PASS"


def test_a_sale_does_not_fund_a_later_buy_the_same_day():
    # The upstream test_a_sell_frees_cash_for_a_later_buy expected PASS, PASS: its line 776 added a sale's proceeds to
    # cash at once. They settle later (S15 / RC17): only settled cash less reservations funds a buy.
    orders = [{"symbol": "KO", "side": "sell", "qty": "5", "type": "market", "time_in_force": "day"},
              buy("BAC", 450)]
    res = v1_run(orders, c=v1(max_position_pct="100"), portfolio=KO_PF)
    assert verdicts(res) == ["PASS", "BLOCK"] and "cash:" in reasons(res, 1)
    assert verdicts(v1_run([buy("BAC", 450)], c=v1(max_position_pct="100"), portfolio=KO_PF)) == ["BLOCK"]
    reserved = dict(KO_PF, settled_cash="1000", reserved_cash="600")       # 400 free < 450 + buffer
    assert "cash:" in reasons(v1_run([buy("BAC", 300)], c=v1(max_position_pct="100"), portfolio=reserved))


def test_daily_limit_and_already_placed_today():
    st = {"orders": [{"symbol": f"T{i}", "side": "buy", "status": "placed", "at": V1_TODAY + "T10:00:00"}
                     for i in range(10)]}
    assert "daily limit" in reasons(v1_run([buy("KO", 100)], state=st))
    res = run([buy("KO", 100)], v1(), state=st, prices=PRICES, times=fresh_at(PRICES, "2026-10-14 10:15"),
              today="2026-10-14", now="2026-10-14 10:15")
    assert verdicts(res) == ["PASS"]
    ko = {"orders": [{"symbol": "KO", "side": "buy", "status": "placed", "at": V1_TODAY + "T10:00:00"}]}
    assert "already placed today" in reasons(v1_run([buy("KO", 100)], state=ko))
    declined = {"orders": [{"symbol": "KO", "side": "buy", "status": "declined", "at": V1_TODAY + "T10:00:00"}]}
    assert verdicts(v1_run([buy("KO", 100)], state=declined)) == ["PASS"]


def test_the_broker_keys_are_paper_only():
    # ADR-012: upstream's "broker" and "paper" are back, paper only: "paper" must be true, "broker" alpaca; live and the
    # IBKR branch are refused. The SEC key is still not a risk policy key.
    assert verdicts(run([tsla()], cfg(broker="alpaca", paper=True))) == ["PASS"]
    for k, v in (("paper", False), ("paper", "true"), ("broker", "ibkr")):
        with pytest.raises(TraderOSError) as e:
            run([tsla()], cfg(**{k: v}))
        assert e.value.code is ErrorCode.NOT_AUTHORIZED
    with pytest.raises(ValidationError, match="not a risk policy key"):
        run([tsla()], cfg(sec_user_agent="x"))


# ---- PR #55: unknown risk in today's log is not zero ----------------------------------------------------------------
def test_missing_loss_blocks_new_entries_until_the_log_is_reconciled():
    record = placed("AAPL", max_loss=None)
    state = {"orders": [record, placed("AMD", max_loss=20)]}
    res = run([tsla()], state=state)
    assert verdicts(res) == ["BLOCK"]
    assert "daily loss: total risk from today's entries is unknown (buy AAPL)" in reasons(res)
    assert res["today"]["loss_at_risk"] is None and res["today"]["known_loss_at_risk"] == 20
    assert res["today"]["entries_with_unknown_loss"] == 1
    assert "unknown ($20 known) of $150 daily loss" in res["results"][0]["info"][-1]
    assert "reconcile the trade log" in reasons(res)
    assert code_of(res["results"][0]["reasons"][0]) is ErrorCode.UNKNOWN_EXPOSURE
    record["max_loss"] = 50
    reconciled = run([tsla()], state=state)
    assert verdicts(reconciled) == ["PASS"] and reconciled["today"]["loss_at_risk"] == 70


@pytest.mark.parametrize("bad", [None, "bad", "NaN", float("nan"), "Infinity", float("inf"), float("-inf"), -100,
                                 "-100", True, False, 10 ** 400])
def test_invalid_recorded_losses_cannot_bypass_the_daily_limit(bad):
    res = run([tsla()], state={"orders": [dict(placed("AAPL"), max_loss=bad)]})
    assert verdicts(res) == ["BLOCK"] and res["today"]["loss_at_risk"] is None
    assert res["today"]["entries_with_unknown_loss"] == 1


def test_zero_and_numeric_string_losses_remain_usable():
    res = run([tsla()], state={"orders": [placed("AAPL", max_loss=0), placed("AMD", max_loss="50")]})
    assert verdicts(res) == ["PASS"] and res["today"]["loss_at_risk"] == 50
    assert res["today"]["entries_with_unknown_loss"] == 0


def test_the_reviews_mixed_log_case():
    # "50" and 0 count; -10 makes the total unknown: BLOCK
    res = run([tsla()], state={"orders": [placed("A", max_loss="50"), placed("B", max_loss=0),
                                          placed("C", max_loss=-10)]})
    assert verdicts(res) == ["BLOCK"] and res["today"]["known_loss_at_risk"] == 50


def test_unknown_loss_blocks_every_new_entry_in_a_batch():
    res = run([bracket(s, 1, 50, 49, 52) for s in ("AAA", "BBB")], state={"orders": [placed("AAPL", max_loss=None)]})
    assert verdicts(res) == ["BLOCK", "BLOCK"]


def test_old_failed_declined_and_exit_records_do_not_create_unknown_entry_risk():
    state = {"orders": [placed("AAPL", max_loss=None, day="2026-10-16"), placed("AMD", max_loss=None, status="failed"),
                        placed("NVDA", max_loss=None, status="declined"),
                        placed("KO", max_loss=None, side="sell", kind="exit")]}
    res = run([tsla()], state=state)
    assert verdicts(res) == ["PASS"] and res["today"]["loss_at_risk"] == 0
    assert res["today"]["entries_with_unknown_loss"] == 0


def test_a_disabled_daily_limit_still_blocks_unknown_risk():
    # PR #55 kept a 0 limit as "disabled" and passed; the review's change 1 (spec 05.3): unknown risk blocks
    # whatever the limit
    for off in ("0", None):
        res = run([tsla()], cfg(max_daily_loss_usd=off), state={"orders": [placed("AAPL", max_loss=None)]})
        assert verdicts(res) == ["BLOCK"] and res["today"]["loss_at_risk"] is None
        assert "daily loss: total risk from today's entries is unknown (buy AAPL)" in reasons(res)
        assert any("total daily risk is unknown" in w for w in res["warnings"])


def test_every_reason_has_a_typed_code():
    seen = set()
    for res in (run([tsla(qty="9")]), run([tsla()], now="2026-10-19 09:40"), run([tsla()], portfolio=None),
                run([tsla(take_profit_limit_price="368.00")]), run([dict(tsla(), symbol="BTC/USD")])):
        for r in res["results"][0]["reasons"]:
            seen.add((r.split(":")[0], code_of(r)))
    assert ("loss", ErrorCode.NOT_AUTHORIZED) in seen and ("window", ErrorCode.NOT_AUTHORIZED) in seen
    assert ("exposure", ErrorCode.UNKNOWN_EXPOSURE) in seen and ("asset", ErrorCode.OUTSIDE_CAPABILITY) in seen
    assert ("reward", ErrorCode.NOT_AUTHORIZED) in seen
    assert code_of("something new: x") is ErrorCode.NOT_AUTHORIZED        # never permission


def test_the_f1_wording_names_no_broker_step():
    appr = dict(placed("TSLA", kind="entry", status="approved"), fingerprint="fp", plain="BUY 8 TSLA")
    res = run([tsla()], state={"orders": [appr]})
    text = check.report(res) + json.dumps(res, default=str)
    for word in ("get_orders", "LIVE", "ticket.py", "trader-broker", "place_stock_order"):
        assert word not in text


def test_the_multiplier_scales_value_and_loss():
    # 2 contracts x 5.20 x 100 = $1,040; at the stop 2 x 1.20 x 100 = $240, over a $200 max loss
    o = bracket("ABC", 2, 5.20, 4.00, 8.00)
    o["plan"]["multiplier"] = "100"
    r = run([o], cfg(max_loss_per_trade_usd="200", max_daily_loss_usd="1000"))["results"][0]
    assert (r["value_usd"], r["max_loss"], r["multiplier"]) == (D("1040.00"), D("240.00"), D("100"))
    assert "loss: 2 shares x 1.20 risk a share x 100 = $240 at the stop, over your $200 max loss" in \
        " ".join(r["reasons"])
    assert "format: multiplier must be a positive number" in reasons(run([dict(o, plan=dict(o["plan"],
                                                                                          multiplier="0"))]))
