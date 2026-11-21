"""OSDev6's review of the S15/S16/S17 port on #10 (comment 7045037626): one test (or more) per item, with his own
numbers where he gave them. Each fix is named in ADR-009 ("Review of #10"). Every expected number is worked by hand.
"""
from __future__ import annotations

import dataclasses
import datetime
import decimal
import json
from decimal import Decimal as D

import pytest

from tests.unit.quotes import quote_file
from tests.unit.test_risk import NOW, QUOTE, account, plan, policy, quote
from tests.unit.test_risk_check import at, bracket, cfg, reasons, run, tsla, verdicts, warnings
from tests.unit.test_risk_size import TODAY
from tests.unit.test_risk_size import plan as jplan
from traderos import api, cli, risk
from traderos.contracts import DataError, ErrorCode, TraderOSError, ValidationError
from traderos.risk import check, code_of, inputs, size, tickets

BIG = dict(budget="10000000", max_order_usd="10000000", max_position_pct="100", max_daily_loss_usd="1000000")


# ---- 1. never round an order price -------------------------------------------------------------------------------
def test_sub_dollar_prices_are_measured_exactly():
    # OSDev6: entry 0.5049, stop 0.4851 at $50 max loss. Upstream rounded them to 0.50 / 0.49 and sized 5000 shares,
    # a real loss of $99 at the plan's stop. Exact: 0.0198 a share; $50 / 0.0198 = 2525.25 -> 2525; 2525 x 0.0198
    # = $49.995, at most $50.
    s = size.size_trade(jplan(symbol="ABC", entry="0.5049", stop="0.4851", target="0.60"), cfg(), today=TODAY)
    assert (s["qty"], s["risk_per_share"], s["entry"], s["stop"]) == (2525, D("0.0198"), D("0.5049"), D("0.4851"))
    assert s["qty"] * (D("0.5049") - D("0.4851")) <= 50
    o = size.bracket_order(s)
    assert (o["stop_loss_stop_price"], o["take_profit_limit_price"]) == ("0.4851", "0.60")      # never moved
    # the ticket's own max loss (an order whose plan carries none: a limit order) on the exact risk: 2525 x 0.0198 =
    # 49.995 -> $50.00. The upstream cents made it 0.02 a share, $50.50
    lo = size.bracket_order(size.size_trade(jplan(symbol="ABC", entry="0.5049", stop="0.4851", target="0.60"), cfg(),
                                            limit="0.5049", today=TODAY))
    assert lo["type"] == "limit" and tickets.max_loss(lo) == (D("50.00"), D("0.4851"))
    r = run([o], prices={"ABC": "0.5049"}, times={"ABC": at("2026-10-19 10:15")})["results"][0]
    assert r["verdict"] == "PASS" and r["risk_per_share"] == D("0.0198") and r["max_loss"] == D("50.00")
    assert "stop 0.4851" in " ".join(r["info"])


def test_the_ticket_carries_the_exact_prices():
    p = plan(symbol="ABC", entry_price=D("0.5049"), stop_price=D("0.4851"), target_price=D("0.60"))
    a = risk.assess(p, policy(), account=account(), prices=quote("ABC", "0.5049"), now=NOW)
    assert a.ticket_eligible, a.refusal_reasons
    t = risk.ticket(p, a, now=NOW)
    assert (t.quantity, t.stop_price, t.target_price) == (D("2525"), D("0.4851"), D("0.60"))
    order = risk.ticket_order(t, a, p)
    assert order["stop_loss_stop_price"] == "0.4851" and tickets.tool_input(order)["stop_loss_stop_price"] == "0.4851"
    assert "stop-loss 0.4851" in tickets.plain(order)


def test_an_order_price_off_the_tick_is_refused():
    # OSDev6's second case: multiplier 125000, max loss 5000, entry 1.0849, stop 1.0651. At $1.00 and above a U.S.
    # stock trades in whole cents, so with no tick_size it is refused, typed, never rounded to 1.08 / 1.07.
    big = cfg(max_loss_per_trade_usd="5000", **BIG)
    p = jplan(symbol="ABC", entry="1.0849", stop="1.0651", target="1.20", multiplier="125000")
    with pytest.raises(DataError) as e:
        size.size_trade(p, big, today=TODAY)
    assert e.value.code is ErrorCode.INVALID_INPUT and "the entry 1.0849 is off the tick" in e.value.message
    # stated, its tick decides: 0.0198 x 125000 = 2475 a contract; 5000 / 2475 = 2.02 -> 2; real loss 4950
    s = size.size_trade(dict(p, tick_size="0.0001"), big, today=TODAY)
    assert s["qty"] == 2 and s["qty"] * D("0.0198") * 125000 <= 5000
    for name, over in (("stop", dict(stop="358.325")), ("target", dict(target="370.041")),
                       ("entry", dict(entry="364.185"))):
        with pytest.raises(DataError, match=rf"the {name} .* is off the tick"):
            size.size_trade(jplan(**over), cfg(), today=TODAY)
    with pytest.raises(DataError, match=r"limit price 364\.001 is off the tick"):
        size.size_trade(jplan(), cfg(), limit="364.001", today=TODAY)
    with pytest.raises(DataError, match=r"its tick_size 0\.05"):
        size.size_trade(jplan(tick_size="0.05"), cfg(), today=TODAY)                # 364.18 is not on a nickel
    assert size.size_trade(jplan(entry="364.20", stop="358.30", target="370.05", tick_size="0.05"), cfg(),
                           today=TODAY)["qty"] == 8
    with pytest.raises(ValidationError):
        size.size_trade(jplan(tick_size="0"), cfg(), today=TODAY)


def test_off_the_tick_through_the_cli_is_exit_3(tmp_path, capsys):
    from tests.unit.test_risk import GOOD, _assess
    home = str(tmp_path / "home")
    off = dict(GOOD, entry="1.0849", stop="1.0651", target="1.20")
    assert _assess(tmp_path, home, off) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == "INVALID_INPUT" and "the entry 1.0849 is off the tick" in failure["message"]
    assert _assess(tmp_path, home, dict(off, tick_size="0.0001")) == 0
    assert json.loads(capsys.readouterr().out)["result"]["ticket_eligible"]


# ---- 2. holdings carry their multiplier ---------------------------------------------------------------------------
XYZ = {"symbol": "XYZ", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day",
       "order_class": "bracket", "take_profit_limit_price": "53.00", "stop_loss_stop_price": "49.00",
       "plan": {"entry": "50.00", "stop": "49.00", "target": "53.00", "multiplier": "100"}}
XYZ_POLICY = cfg(max_loss_per_trade_usd="250", max_order_usd="20000", max_daily_loss_usd="1000")


def _held(multiplier=None):
    pos = {"symbol": "XYZ", "shares": "10"}
    if multiplier is not None:
        pos["multiplier"] = multiplier
    return {"settled_cash": "20000", "reserved_cash": "0", "positions": [pos]}


def test_a_holding_is_valued_with_its_multiplier():
    # OSDev6: 10 contracts held at 50 x 100 = $50,000, settled cash $20,000: total $70,000. Two more contracts are
    # $10,000: $60,000 of $70,000 = 85.7%, over 75%. Upstream valued the holding at $500 and passed it.
    res = run([XYZ], XYZ_POLICY, portfolio=_held("100"), prices={"XYZ": "50.00"}, times={"XYZ": at("2026-10-19 10:15")})
    assert verdicts(res) == ["BLOCK"]
    assert "position: XYZ would be $60,000, 85.7% of the $70,000 account, over your 75% cap" in reasons(res)


def test_a_holding_without_the_plans_multiplier_is_unknown_exposure():
    res = run([XYZ], XYZ_POLICY, portfolio=_held(), prices={"XYZ": "50.00"}, times={"XYZ": at("2026-10-19 10:15")})
    assert verdicts(res) == ["BLOCK"]
    (why,) = [r for r in res["results"][0]["reasons"] if r.startswith("exposure:")]
    assert "multiplier is 100 but your account statement states none for it" in why
    assert code_of(why) is ErrorCode.UNKNOWN_EXPOSURE
    other = run([XYZ], XYZ_POLICY, portfolio=_held("50"), prices={"XYZ": "50.00"},
                times={"XYZ": at("2026-10-19 10:15")})
    assert "states 50" in reasons(other)
    # a holding stated at 100 and a plan at 1 is a mismatch too
    plain = dict(XYZ, plan={k: v for k, v in XYZ["plan"].items() if k != "multiplier"})
    assert "exposure:" in reasons(run([plain], XYZ_POLICY, portfolio=_held("100"), prices={"XYZ": "50.00"},
                                      times={"XYZ": at("2026-10-19 10:15")}))


def test_the_adapter_sizes_against_the_multiplied_holding():
    # the same account through the adapter: the position cap's room is 75% of $70,000 - $50,000 = $2,500, less than
    # one $5,000 contract: refused at the sizing, before the check (LOW b)
    p = plan(symbol="XYZ", entry_price=D("50.00"), stop_price=D("49.00"), target_price=D("53.00"),
             multiplier=D("100"))
    acct = inputs.account({**_held("100"), "orders": []})
    a = risk.assess(p, dict(XYZ_POLICY), account=acct, prices=quote("XYZ", "50.00"), now=NOW)
    assert not a.ticket_eligible and a.quantity.value == 0
    assert any("position cap" in lim.detail and "(binds)" in lim.detail for lim in a.limitations)


def test_the_account_statement_states_a_holdings_multiplier():
    a = inputs.account({**_held("100"), "orders": []})
    assert a["positions"][0]["multiplier"] == D("100")
    for bad in ("0", "-1", "abc", "1E+20"):
        with pytest.raises(ValidationError):
            inputs.account({**_held(bad), "orders": []})


def test_a_fill_keeps_the_holdings_multiplier():
    statement = {"settled_cash": "20000", "reserved_cash": "0", "positions": [], "orders": []}
    new = tickets.apply_fill(statement, "XYZ", "buy", "2", "50.00", "2026-10-19", multiplier=D("100"))
    assert new["positions"][0]["multiplier"] == "100" and new["settled_cash"] == "10000.00"     # 2 x 50 x 100
    more = tickets.apply_fill(new, "XYZ", "buy", "1", "50.00", "2026-10-19")              # by hand: the holding's
    assert more["settled_cash"] == "5000.00" and more["positions"][0]["shares"] == "3"
    with pytest.raises(ValidationError, match="holds XYZ at multiplier 100"):
        tickets.apply_fill(new, "XYZ", "buy", "1", "50.00", "2026-10-19", multiplier=D("1"))
    assert inputs.account(more)["positions"][0]["multiplier"] == D("100")


# ---- 3. a fractional multiplier never sizes past the cap ----------------------------------------------------------
@pytest.mark.parametrize("multiplier,want", [("12.5", 400), ("2.5", 2000)])
def test_a_fractional_multiplier_floors_the_exact_quotient(multiplier, want):
    # OSDev6: entry 10.01, stop 10.00, $50 max loss. Per contract 0.01 x 12.5 = 0.125: 50 / 0.125 = 400 (the upstream
    # cents rounded 0.125 to 0.12: 416, $52.00). 0.01 x 2.5 = 0.025: 2000 (upstream: 0.02, 2500, $62.50).
    s = size.size_trade(jplan(symbol="ABC", entry="10.01", stop="10.00", target="10.05", multiplier=multiplier),
                        cfg(**BIG), today=TODAY)
    assert s["qty"] == want and s["bound_by"] == "max loss" and s["max_loss"] <= 50
    rich = {"settled_cash": "10000000", "reserved_cash": "0", "positions": []}
    r = run([size.bracket_order(s)], cfg(**BIG), portfolio=rich, prices={"ABC": "10.01"},
            times={"ABC": at("2026-10-19 10:15")})["results"][0]
    assert r["verdict"] == "PASS" and r["max_loss"] == D("50.00"), r["reasons"]


# ---- 4. a market entry with no usable price blocks ----------------------------------------------------------------
def test_a_market_entry_with_no_price_blocks():
    for prices in (None, {}, {"AAPL": "100"}):
        res = run([tsla()], prices=prices, times={})
        assert verdicts(res) == ["BLOCK"]
        (why,) = [r for r in res["results"][0]["reasons"] if r.startswith("price:")]
        assert "no price for TSLA: a market entry needs a fresh one" in why
        assert code_of(why) is ErrorCode.MISSING_DATA
    # a limit order is measured from its own price and needs no quote
    assert verdicts(run([tsla(type="limit", limit_price="364.18")], prices=None)) == ["PASS"]


def test_a_price_over_the_bound_is_refused_at_read():
    with pytest.raises(ValidationError, match="out of range"):
        inputs.prices({"prices": {"TSLA": "1E+101"}})


def test_no_quote_through_the_cli_is_missing_data(tmp_path, capsys):
    from tests.unit.test_risk import GOOD, _write
    home = str(tmp_path / "home")
    acct = _write(tmp_path, {"settled_cash": "5000", "reserved_cash": "0", "positions": [], "orders": []}, "a.json")
    base = ["--json", "--home", home, "risk", "assess", "--plan", _write(tmp_path, GOOD), "--policy",
            _write(tmp_path, policy(), "p.json"), "--account", acct, "--now", "2026-10-19T14:15:00Z"]
    assert cli.main(base) == 0
    a = json.loads(capsys.readouterr().out)["result"]
    assert not a["ticket_eligible"] and "MISSING_DATA" in a["refusal_reasons"]
    big = _write(tmp_path, {"prices": {"TSLA": "1E+101"}}, "big.json")
    assert cli.main([*base, "--prices", big]) == 3
    assert "out of range" in json.loads(capsys.readouterr().out)["failure"]["message"]


# ---- 5. no untyped failure; a failure supersedes ------------------------------------------------------------------
@pytest.fixture
def desk(tmp_path, capsys):
    from tests.unit.test_risk import GOOD, _write
    home = str(tmp_path / "home")
    files = {"plan": _write(tmp_path, GOOD), "policy": _write(tmp_path, policy(), "p.json"),
             "account": _write(tmp_path, {"settled_cash": "5000", "reserved_cash": "0", "positions": [],
                                          "orders": []}, "a.json")}

    def run_cli(*argv, now="2026-10-19T14:15:00Z"):
        code = cli.main(["--json", "--home", home, *argv, "--now", now])
        return code, json.loads(capsys.readouterr().out)

    def assess(*extra, now="2026-10-19T14:15:00Z", **over):
        f = dict(files, **over)
        q = quote_file(tmp_path, files["plan"], now, "q.json")
        return run_cli("risk", "assess", "--plan", f["plan"], "--policy", f["policy"], "--account", f["account"],
                       "--prices", f.get("prices", q), *extra, now=now)

    run_cli.assess, run_cli.write, run_cli.files = assess, lambda d, n: _write(tmp_path, d, n), files
    return run_cli


@pytest.mark.parametrize("bad", ["limit", "entry", "reserved_cash"])
def test_an_out_of_range_number_exits_3_and_supersedes(desk, bad):
    from tests.unit.test_risk import GOOD
    code, out = desk.assess()
    assert code == 0 and out["result"]["ticket_eligible"]
    older = out["result"]["assessment_id"]
    if bad == "limit":
        code, out = desk.assess("--limit", "1E+99", now="2026-10-19T14:16:00Z")
    elif bad == "entry":
        code, out = desk.assess(now="2026-10-19T14:16:00Z", plan=desk.write(dict(GOOD, entry="1E+99"), "big.json"))
    else:
        acct = desk.write({"settled_cash": "5000", "reserved_cash": "1E+98", "positions": [], "orders": []}, "b.json")
        code, out = desk.assess(now="2026-10-19T14:16:00Z", account=acct)
    assert code == 3 and out["failure"]["code"] == "INVALID_INPUT" and "out of range" in out["failure"]["message"]
    key = {"limit": "--limit: '1E+99'", "entry": "plan.entry: '1E+99'", "reserved_cash": "account.reserved_cash"}[bad]
    assert out["failure"]["message"].startswith(key)                 # refused at read, naming the key
    code, out = desk("ticket", older, now="2026-10-19T14:17:00Z")
    assert code == 3 and "supersedes" in out["failure"]["message"]


def test_an_arithmetic_error_is_typed_recorded_and_supersedes(desk, monkeypatch):
    code, out = desk.assess()
    older = out["result"]["assessment_id"]

    def boom(*a, **k):
        raise decimal.InvalidOperation([decimal.InvalidOperation])
    monkeypatch.setattr(risk, "evaluate", boom)
    code, out = desk.assess(now="2026-10-19T14:16:00Z")
    assert code == 3 and out["failure"]["code"] == "INVALID_INPUT" and "out of range" in out["failure"]["message"]
    code, out = desk("ticket", older, now="2026-10-19T14:17:00Z")
    assert code == 3 and "supersedes" in out["failure"]["message"]


# ---- 6. a yes dated before its check is refused ----------------------------------------------------------------
def test_a_backdated_yes_is_refused_and_nothing_is_recorded(desk):
    code, out = desk.assess()
    a = out["result"]
    desk("ticket", a["assessment_id"])
    pt = "PT-" + a["assessment_id"][3:]
    code, out = desk("ticket", "approve", pt, "--reply", "yes", now="2026-10-18T14:15:00Z")
    assert code == 3 and "before this ticket's risk check" in out["failure"]["message"]
    code, out = desk("ticket", "log", now="2026-10-19T14:16:00Z")
    assert code == 0 and out["result"] == []
    code, out = desk("ticket", "approve", pt, "--reply", "yes", now="2026-10-19T14:15:00Z")
    assert code == 0                                                     # at (or after) the check: recorded


# ---- 7. a sale in the batch leaves the total unchanged; its proceeds are not spendable ----------------------------
KO = {"symbol": "KO", "side": "buy", "qty": "48", "type": "market", "time_in_force": "day",
      "plan": {"stop": "89.00"}}
KO_CFG = cfg(max_loss_per_trade_usd="1000", max_order_usd="5000", max_daily_loss_usd="0", max_trades_per_day=0,
             min_reward_risk="0")
TSLA10 = {"settled_cash": "5000", "reserved_cash": "0", "positions": [{"symbol": "TSLA", "shares": "10"}]}
QUOTES = {"TSLA": "364.18", "KO": "90.11"}


def test_a_sale_does_not_shrink_the_buffer():
    # OSDev6: settled $5,000 and 10 TSLA at 364.18 = $3,641.80: total $8,641.80, a 10% buffer of $864.18. Buying
    # 48 KO at 90.11 ($4,325.28) leaves $674.72: BLOCK. Selling the 10 TSLA first used to drop $3,641.80 from the
    # total (the buffer fell to $500) and PASS the buy; now the proceeds are unsettled cash, still in the total.
    times = {s: at("2026-10-19 10:15") for s in QUOTES}
    want = "cash: leaves $674.72 cash, under your 10% buffer of $864.18"
    alone = run([KO], KO_CFG, portfolio=TSLA10, prices=QUOTES, times=times)
    assert verdicts(alone) == ["BLOCK"] and want in reasons(alone)
    sell = {"symbol": "TSLA", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day"}
    res = run([sell, KO], KO_CFG, portfolio=TSLA10, prices=QUOTES, times=times)
    assert verdicts(res) == ["PASS", "BLOCK"] and want in reasons(res, 1)
    # the account statement's own unsettled cash is counted the same way
    stated = {"settled_cash": "5000", "reserved_cash": "0", "unsettled_cash": "3641.80", "positions": []}
    assert want in reasons(run([KO], KO_CFG, portfolio=stated, prices=QUOTES, times=times))


def test_the_adapter_counts_the_statements_unsettled_cash():
    # $5,000 settled and $3,641.80 unsettled: a $8,641.80 account and a $864.18 buffer, so the cash row is 4,135.82 /
    # 364.18 = 11.36 -> 11 shares (they leave $994.02). Without the unsettled cash: a $500 buffer and 12
    c = policy(max_loss_per_trade_usd="1000", max_order_usd="5000", max_position_pct="100")
    a = risk.assess(plan(), c, account=account(unsettled_cash="3641.80"), prices=QUOTE, now=NOW)
    assert a.quantity.value == 11 and a.ticket_eligible, a.refusal_reasons
    assert risk.assess(plan(), c, account=account(), prices=QUOTE, now=NOW).quantity.value == 12


# ---- LOW ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("change", [{"quantity": D("10")}, {"settled_cash": D("1")}, {"existing_exposure": D("0")},
                                    {"risk_per_trade": D("0.01")}, {"whole_units": False}, {"account_equity": D("1")},
                                    {"reserved_cash": D("5")},
                                    {"prices_observed_at": datetime.datetime(2026, 10, 19, tzinfo=datetime.UTC)}])
def test_the_api_refuses_the_v1_0_fields_it_does_not_read(change):
    with pytest.raises(ValidationError, match=r"not read since contracts v1\.1"):
        api.assess_hypothetical_plan(dataclasses.replace(plan(), **change), policy(), now=NOW)


def test_a_plan_in_another_currency_is_refused():
    with pytest.raises(TraderOSError) as e:
        risk.assess(dataclasses.replace(plan(), currency="EUR"), policy(), account=account(), prices=QUOTE, now=NOW)
    assert e.value.code is ErrorCode.OUTSIDE_CAPABILITY and "U.S. dollars" in e.value.message


def test_a_sized_quantity_passes_its_own_check():
    # OSDev6: settled 3,413.44. Upstream sized 8 against the budget's cap ($3,750) and the check blocked them at 85.4%
    # of the account. The stricter basis: 75% of 3,413.44 = 2,560.08 / 364.18 = 7.03 -> 7, and 7 pass (74.7%).
    a = risk.assess(plan(), policy(), account=account(settled_cash="3413.44"), prices=QUOTE, now=NOW)
    assert a.quantity.value == 7 and a.ticket_eligible, a.refusal_reasons
    # the buffer the same way: $5,000 settled and $20,000 of AAPL make a $25,000 account, a $2,500 buffer; the upstream
    # budget buffer ($500) sized 12 and the check would block them. 2,500 / 364.18 = 6.86 -> 6, and they pass.
    rich = account(positions=[{"symbol": "AAPL", "shares": "100"}])
    prices = inputs.prices({"prices": {"TSLA": "364.18", "AAPL": "200"},
                            "detail": {"TSLA": {"time": NOW.isoformat()}, "AAPL": {"time": NOW.isoformat()}}})
    b = risk.assess(plan(), policy(max_loss_per_trade_usd="100"), account=rich, prices=prices, now=NOW)
    assert b.quantity.value == 6 and b.ticket_eligible, b.refusal_reasons
    # and the cap less what you already hold: 5 TSLA (1,820.90) and $5,000 make 6,820.90; 75% is 5,115.675, less
    # the 1,820.90 held = 3,294.775 / 364.18 = 9.05 -> 9, and 14 held then are 74.7%. Not less what you hold, the
    # budget's 3,750 would size 10: 80.1%, blocked
    held = account(positions=[{"symbol": "TSLA", "shares": "5"}])
    c = risk.assess(plan(), policy(max_loss_per_trade_usd="100"), account=held, prices=QUOTE, now=NOW)
    assert c.quantity.value == 9 and c.ticket_eligible, c.refusal_reasons


@pytest.mark.parametrize("bad", [5, ["TSLA"], ""])
def test_a_symbol_or_ticket_that_is_not_a_string_is_refused(bad, tmp_path, capsys):
    from tests.unit.test_risk import GOOD, _write
    assert cli.main(["--json", "--home", str(tmp_path / "h"), "risk", "assess", "--plan",
                     _write(tmp_path, dict(GOOD, symbol=bad)), "--policy", _write(tmp_path, policy(), "p.json"),
                     "--now", "2026-10-19T14:15:00Z"]) == 3
    capsys.readouterr()
    order = {"symbol": "AAPL", "side": "buy", "status": "placed", "date": "2026-10-19"}
    for rec in (dict(order, symbol=bad), dict(order, ticket=bad)):
        with pytest.raises(ValidationError, match="must be a non-empty string"):
            inputs.account({"settled_cash": "1", "reserved_cash": "0", "positions": [], "orders": [rec]})


# ---- his three surviving mutants, pinned ----------------------------------------------------------------------------
def test_the_half_cent_tolerance_on_the_max_loss():
    # one share risking $50.01 is over a $50 max loss; $50.00 is not (check.py: loss > cap + half a cent)
    over = bracket("ABC", 1, 100.00, 49.99, 300.00)
    at_cap = bracket("ABC", 1, 100.00, 50.00, 300.00)
    c = cfg(min_reward_risk="0")
    assert "loss: 1 shares x 50.01 risk a share = $50.01" in reasons(run([over], c))
    assert verdicts(run([at_cap], c)) == ["PASS"]


def test_the_fifteen_minute_fresh_window():
    # a quote 15:00 old is fresh, 15:01 is stale (and a stale quote blocks a market entry)
    quoted = at("2026-10-19 10:15")
    fresh = run([tsla()], prices={"TSLA": "364.18"}, times={"TSLA": quoted},
                now=quoted + datetime.timedelta(minutes=15))
    stale = run([tsla()], prices={"TSLA": "364.18"}, times={"TSLA": quoted},
                now=quoted + datetime.timedelta(minutes=15, seconds=1))
    assert verdicts(fresh) == ["PASS"] and verdicts(stale) == ["BLOCK"]
    assert "older than 15 minutes" in reasons(stale)


def test_the_half_cent_tolerance_on_the_cash_buffer():
    # $5,000 settled, a 10% buffer of $500: a $4,500.01 buy leaves $499.99, under it; $4,500.00 leaves $500.00
    c = cfg(max_order_usd="5000", max_position_pct="100", max_loss_per_trade_usd="100", min_reward_risk="0")

    def limit_buy(price):
        return {"symbol": "ABC", "side": "buy", "qty": "1", "type": "limit", "limit_price": price,
                "time_in_force": "day", "order_class": "bracket", "take_profit_limit_price": "4600.00",
                "stop_loss_stop_price": "4460.00", "plan": {"stop": "4460.00"}}
    assert "cash: leaves $499.99 cash, under your 10% buffer of $500" in reasons(run([limit_buy("4500.01")], c))
    assert verdicts(run([limit_buy("4500.00")], c)) == ["PASS"]


def test_the_check_and_the_sizer_agree_on_exact_division():
    assert size.shares_for(D("50"), D("0.0198")) == 2525
    assert size.shares_for(D("50"), D("0.125")) == 400 and size.shares_for(D("0.30"), D("0.10")) == 3
    assert check.price_text(D("0.4851")) == "0.4851" and check.price_text(D("364.1")) == "364.10"


# ==== the second round (OSDev6, comment 7050923565 at c5d95ae) ======================================================
XYZ_PLAN = {"symbol": "XYZ", "direction": "long", "date": "2026-10-19", "entry": "50.00", "stop": "49.00",
            "target": "53.00", "multiplier": "100"}


def _cli(tmp_path, capsys, plan_data, account_data, policy_data, *extra):
    from tests.unit.test_risk import _write
    argv = ["--json", "--home", str(tmp_path / "home"), "risk", "assess", "--plan", _write(tmp_path, plan_data),
            "--policy", _write(tmp_path, policy_data, "p.json"), "--account", _write(tmp_path, account_data, "a.json"),
            "--now", "2026-10-19T14:15:00Z", *extra]
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)["result"]


def test_a_held_symbol_with_no_price_reopens_nothing_through_a_limit_order(tmp_path, capsys):
    # BLOCKING: a holding with no price and no stated cost was valued at $0 (the upstream cost basis of no lots), and a
    # limit order needs no prices file. OSDev6's repro: 10 XYZ x 100 held, $20,000 settled, `--limit 50.00` buy 2: exit
    # 0, eligible, at a true 60,000 of 70,000 = 85.7%. Now UNKNOWN_EXPOSURE (the holding's value) and MISSING_DATA (the
    # plan's own symbol needs its price, even for a limit order).
    from tests.unit.test_risk import _write
    pol = policy(budget="100000", max_loss_per_trade_usd="250", max_order_usd="20000", max_daily_loss_usd="1000")
    held = {"settled_cash": "20000", "reserved_cash": "0", "orders": [],
            "positions": [{"symbol": "XYZ", "shares": "10", "multiplier": "100"}]}
    code, a = _cli(tmp_path, capsys, XYZ_PLAN, held, pol, "--limit", "50.00")
    assert code == 0 and not a["ticket_eligible"]
    assert {"UNKNOWN_EXPOSURE", "MISSING_DATA"} <= set(a["refusal_reasons"]) and a["exposure_status"] == "unknown"
    words = " | ".join(lim["detail"] for lim in a["limitations"])
    assert "you hold XYZ with no price and no stated cost" in words
    assert "no price for XYZ: you hold it, so its price is needed" in words
    # with its price the position is measured: 75% of $70,000 less the $50,000 held leaves no room for a contract
    prices = _write(tmp_path, {"prices": {"XYZ": "50.00"}, "detail": {"XYZ": {"time": "2026-10-19T14:15:00Z"}}},
                    "q.json")
    code, a = _cli(tmp_path, capsys, XYZ_PLAN, held, pol, "--limit", "50.00", "--prices", prices)
    assert code == 0 and not a["ticket_eligible"] and a["quantity"]["value"] == "0"


def test_the_same_hole_at_multiplier_1(tmp_path, capsys):
    # 10 TSLA held and a limit buy with no prices file sized 8: (3,641.80 + 2,913.44) / 8,641.80 = 75.9%
    from tests.unit.test_risk import GOOD, _write
    held = {"settled_cash": "5000", "reserved_cash": "0", "orders": [],
            "positions": [{"symbol": "TSLA", "shares": "10"}]}
    code, a = _cli(tmp_path, capsys, GOOD, held, policy(), "--limit", "364.18")
    assert code == 0 and not a["ticket_eligible"] and "UNKNOWN_EXPOSURE" in a["refusal_reasons"]
    # with the price: 75% of 8,641.80 less the 3,641.80 held = 2,839.55 / 364.18 = 7.8 -> 7, which pass (71.6%)
    prices = _write(tmp_path, {"prices": {"TSLA": "364.18"}, "detail": {"TSLA": {"time": "2026-10-19T14:15:00Z"}}},
                    "q.json")
    code, a = _cli(tmp_path, capsys, GOOD, held, policy(), "--limit", "364.18", "--prices", prices)
    assert code == 0 and a["ticket_eligible"] and a["quantity"]["value"] == "7", a["refusal_reasons"]
    # a holding that states its cost is valued at it, as upstream did; the plan's own symbol still needs its price
    costed = dict(held, positions=[{"symbol": "TSLA", "shares": "10", "lots": [{"shares": "10", "cost": "3641.80"}]}])
    code, a = _cli(tmp_path, capsys, GOOD, costed, policy(), "--limit", "364.18")
    assert a["refusal_reasons"] == ["MISSING_DATA"]


def test_an_unpriced_holding_blocks_entries_not_exits():
    # another symbol held with no price and no cost: every entry is UNKNOWN_EXPOSURE; an exit only warns
    times = {"TSLA": at("2026-10-19 10:15")}
    port = {"settled_cash": "5000", "reserved_cash": "0",
            "positions": [{"symbol": "TSLA", "shares": "10"}, {"symbol": "AAPL", "shares": "5"},
                          {"symbol": "KO", "shares": "0"}]}
    buy = run([tsla(type="limit", limit_price="364.18")], portfolio=port, prices={"TSLA": "364.18"}, times=times)
    assert verdicts(buy) == ["BLOCK"]
    (why,) = [r for r in buy["results"][0]["reasons"] if r.startswith("exposure:")]
    assert "you hold AAPL with no price" in why and code_of(why) is ErrorCode.UNKNOWN_EXPOSURE     # KO: 0 shares
    sell = {"symbol": "TSLA", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day"}
    out = run([sell], portfolio=port, prices={"TSLA": "364.18"}, times=times)
    assert verdicts(out) == ["PASS"] and "you hold AAPL with no price" in warnings(out)
    costed = dict(port, positions=[{"symbol": "TSLA", "shares": "10"},
                                   {"symbol": "AAPL", "shares": "5", "lots": [{"shares": "5", "cost": "1000"}]}])
    assert verdicts(run([tsla(type="limit", limit_price="364.18")], portfolio=costed, prices={"TSLA": "364.18"},
                        times=times)) == ["PASS"]


def test_a_yes_one_second_before_its_check_is_refused(desk):
    # item 6, pinned at the second: the check is at 14:15:00Z; 14:14:59Z is refused, 14:15:00Z is recorded
    code, out = desk.assess()
    pt = "PT-" + out["result"]["assessment_id"][3:]
    desk("ticket", out["result"]["assessment_id"])
    code, out = desk("ticket", "approve", pt, "--reply", "yes", now="2026-10-19T14:14:59Z")
    assert code == 3 and "before this ticket's risk check" in out["failure"]["message"]
    code, out = desk("ticket", "approve", pt, "--reply", "yes", now="2026-10-19T14:15:00Z")
    assert code == 0


@pytest.mark.parametrize("field", ["tick_size", "multiplier"])
def test_a_tiny_tick_or_multiplier_is_a_typed_refusal(field):
    # api.assess_hypothetical_plan raised decimal.InvalidOperation on 1E-99; the CLI already typed it
    p = dataclasses.replace(plan(), **{field: D("1E-99")})
    acct = {"settled_cash": "5000", "reserved_cash": "0", "positions": [], "orders": []}
    q = {"prices": {"TSLA": "364.18"}, "detail": {"TSLA": {"time": NOW.isoformat()}}}
    with pytest.raises(ValidationError) as e:
        api.assess_hypothetical_plan(p, policy(), account=acct, prices=q, now=NOW)
    assert e.value.code is ErrorCode.INVALID_INPUT and "out of range" in e.value.message


def test_sub_cent_money_is_shown_as_it_is():
    # an untested fix of the first round: upstream's "{:,.2f}" showed a 0.4851 limit as $0.49
    assert (tickets._money(D("0.4851")), tickets._money(D("2913.44")), tickets._money(D("50"))) == \
        ("$0.4851", "$2,913.44", "$50")
    order = {"symbol": "ABC", "side": "buy", "qty": "2525", "type": "limit", "limit_price": "0.5049",
             "time_in_force": "day"}
    assert "limit order at $0.5049" in tickets.plain(order)


def test_negative_unsettled_cash_is_refused():
    # an untested fix of the first round: unsettled cash is never below 0, in the reader and in the upstream check
    with pytest.raises(ValidationError, match="unsettled_cash must be at least 0"):
        inputs.account({"settled_cash": "1", "reserved_cash": "0", "unsettled_cash": "-1", "positions": [],
                        "orders": []})
    with pytest.raises(check.RiskError, match="unsettled_cash must be a finite number of at least 0"):
        run([tsla()], portfolio={"settled_cash": "5000", "reserved_cash": "0", "unsettled_cash": "-1",
                                 "positions": []})


def test_an_empty_note_is_upstream_again():
    # the first round refused an empty note, at and plain with the symbol; LOW c named only the symbol and ticket
    order = {"symbol": "AAPL", "side": "buy", "status": "placed", "date": "2026-10-19", "note": "", "plain": ""}
    (rec,) = inputs.account({"settled_cash": "1", "reserved_cash": "0", "positions": [], "orders": [order]})["orders"]
    assert (rec["note"], rec["plain"]) == ("", "")
