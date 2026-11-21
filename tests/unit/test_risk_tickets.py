"""The upstream trader-broker tests (tank/day-trader-skill/tests/test_broker.py at 3d0f4ef, with the tests of PRs #2 and
#21), ported to traderos.risk.tickets and the `traderos ticket` commands: the ticket, its yes and its status.

Since ADR-012 the order half is back; its tests (the upstream TicketShape tool_input tests, Flow's two-yes flow with the
send, result, fill, flatten and the IBKR branch, now refused) are in test_paper_orders.py, with the tests that prove
live is refused. Not ported, with the reason: DesktopConfig (claude_desktop_config.example.json configured the MCP
server that no longer sends: TraderOS sends itself, ADR-012). Flow's document tests are ported here against
`traderos ticket`: show, approve (the F1 port's "sign-off" is its alias), decline, log, expiry, an edited plan,
yesterday's check and a blocked plan.
"""
from __future__ import annotations

import dataclasses
import json
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from tests.unit.quotes import quote_file
from traderos import cli, risk
from traderos.contracts import ErrorCode, HypotheticalRiskAssessment, TraderOSError, ValidationError
from traderos.risk import tickets
from traderos.risk.policy import EXAMPLE

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "risk"
AAPL = {"symbol": "AAPL", "side": "buy", "notional": "1250", "type": "market", "time_in_force": "day"}
BATCH = ["AAPL", "AXP", "GOOGL", "KO", "BAC"]
TSLA_CALL = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
TSLA_PLAIN = ("BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 / max loss about "
              "$46.88 if the stop fills at 358.32 (it can fill worse in a fast market)")
AT = "2026-10-19T14:15:00Z"                        # 10:15 New York


def tsla(**over):
    o = json.loads((FIX / "orders-tsla.json").read_text(encoding="utf-8"))["orders"][0]
    o.update(over)
    return o


# ---- an explicit yes, for one ticket -------------------------------------------------------------------------------
def yes(reply, order=AAPL, batch=BATCH):
    return tickets.parse_yes(reply, order, batch)[0]


@pytest.mark.parametrize("reply", ["yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes AAPL",
                                   "yes $1,250 of AAPL", "yes 1250"])
def test_plain_yes_counts(reply):
    assert yes(reply)


@pytest.mark.parametrize("reply", ["", "sure", "ok", "okay", "go", "do it", "yeah", "yep", "absolutely", "send it",
                                   "yes to all", "yes, all of them", "yes to the rest", "yes both", "yes AXP",
                                   "yes 1000", "yes but make it 1000", "yes?", "no", "not yes", "yes, wait",
                                   "yes if it is under 340"])
def test_anything_else_does_not(reply):
    assert not yes(reply)


def test_only_another_ticket_in_the_batch_is_refused_by_name():
    assert not yes("yes AXP")
    assert yes("yes MSFT")
    assert yes("yes AXP", batch=["AAPL"])
    assert "names AXP, but this ticket is for AAPL" in tickets.parse_yes("yes AXP", AAPL, BATCH)[1]


def test_amount_must_match_this_ticket():
    sell = {"symbol": "KO", "side": "sell", "qty": "3", "type": "market", "time_in_force": "day"}
    assert yes("yes sell 3 KO", sell) and not yes("yes sell 4", sell)


def test_a_bracket_yes():
    o = tsla()
    for r in ("yes", "yes 8 TSLA", "yes 358.32", "yes max loss 46.88", "yes target 370.04"):
        assert yes(r, o, ["TSLA"]), r
    for r in ("yes 9", "yes 10 TSLA", "yes but stop at 357", "yes, stop 358.32", "yes AAPL", "sure"):
        assert not yes(r, o, ["TSLA", "AAPL"]), r


# ---- the ticket in plain words -------------------------------------------------------------------------------------
def test_plain_english():
    assert tickets.plain(AAPL) == "BUY $1,250 of AAPL, market order, good for today"
    assert tickets.plain({"symbol": "KO", "side": "sell", "qty": "1", "type": "limit", "limit_price": "95",
                          "time_in_force": "gtc"}) == "SELL 1 share of KO, limit order at $95, good until canceled"
    assert tickets.plain(tsla()) == TSLA_PLAIN
    lim = tsla(type="limit", limit_price="364.00")
    assert tickets.plain(lim).split(" / ")[0] == "BUY 8 TSLA, limit 364.00, good for today"
    assert tickets.max_loss(lim) == (D("45.44"), D("358.32"))
    assert tickets.max_loss(dict(TSLA_CALL)) == (None, D("358.32"))


def test_the_order_half_is_back_and_paper_only():
    # ADR-012 restores what ADR-009 removed: tool_input, apply_fill, log_fill and the upstream statuses
    for name in ("tool_input", "apply_fill", "log_fill", "ticket_json", "kind_of"):
        assert hasattr(tickets, name), name
    assert tickets.STATUSES == ("approved", "declined", "placed", "denied", "failed", "filled")
    import ast
    tree = ast.parse(pathlib.Path(tickets.__file__).read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    words = " ".join(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and id(n) not in docs)                # every string the code can print (docstrings name history)
    for word in ("place_stock_order", "Allow once", "IBKR", "Interactive Brokers", "LIVE ACCOUNT", "REAL MONEY"):
        assert word not in words, word


# ---- the document flow, through `traderos` -------------------------------------------------------------------------
@pytest.fixture
def desk(tmp_path, capsys):
    home = str(tmp_path / "home")

    def write(name, data):
        path = tmp_path / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    files = {"plan": write("plan.json", {"symbol": "TSLA", "direction": "long", "date": "2026-10-19",
                                         "entry": "364.18", "stop": "358.32", "target": "370.04"}),
             "policy": write("policy.json", EXAMPLE),
             "account": write("account.json", {"settled_cash": "5000", "reserved_cash": "0", "positions": [],
                                               "orders": []})}

    def run(*argv, now=AT, ok=None):
        args = ["--json", "--home", home, *argv]
        if now:
            args += ["--now", now]
        code = cli.main(args)
        out = json.loads(capsys.readouterr().out)
        if ok is not None:
            assert code == (0 if ok else 3), out
        return code, out

    def assess(now=AT, quote=True, **files_over):
        f = dict(files, **files_over)
        extra = ["--prices", quote_file(tmp_path, f["plan"], now, "quote.json")] if quote else []
        _, out = run("risk", "assess", "--plan", f["plan"], "--policy", f["policy"], "--account", f["account"],
                     *extra, now=now, ok=True)
        return out["result"]

    def text(*argv, now=AT):
        args = ["--home", home, *argv] + (["--now", now] if now else [])
        code = cli.main(args)
        cap = capsys.readouterr()
        return code, cap.out + cap.err

    run.assess, run.text, run.write, run.files = assess, text, write, files
    return run


def test_the_full_approve_flow(desk):
    a = desk.assess()
    assert a["ticket_eligible"] is True and a["valid_until"] == "2026-10-19T14:30:00Z"
    code, text = desk.text("ticket", a["assessment_id"])
    assert code == 0
    for part in TSLA_PLAIN.split(" / "):
        assert "  " + part + "\n" in text
    assert "READY: risk check PASS, waiting for your yes" in text and "PAPER account, Alpaca" in text
    assert "place_stock_order" not in text and "Allow once" not in text and "LIVE" not in text
    assert "It never places a live order." in text
    pt = "PT-" + a["assessment_id"][3:]
    _, out = desk("ticket", "approve", pt, "--reply", "sure", ok=False)
    assert out["failure"]["code"] == "INVALID_APPROVAL" and "not recorded" in out["failure"]["message"]
    _, text = desk.text("ticket", "log")
    assert "No decisions recorded today." in text
    desk("ticket", "approve", pt, "--reply", "yes", ok=True)
    _, out = desk("ticket", "sign-off", pt, "--reply", "yes", ok=False)        # the F1 name is an alias
    assert "APPROVED" in out["failure"]["message"]
    _, text = desk.text("ticket", "show", pt)
    assert f"APPROVED by you at 2026-10-19T10:15:00-04:00, waiting for `traderos ticket place {pt}`" in text
    _, text = desk.text("ticket", "log")
    assert "approved" in text and "0 placed today." in text


def test_an_approval_counts_as_placed_until_the_account_says_what_happened(desk):
    a = desk.assess()
    pt = "PT-" + a["assessment_id"][3:]
    desk.text("ticket", a["assessment_id"])
    desk("ticket", "approve", pt, "--reply", "yes", ok=True)
    # P4: a new entry is blocked while the approved ticket has no outcome (here: in the account statement)
    aapl = desk.write("aapl.json", {"symbol": "AAPL", "direction": "long", "date": "2026-10-19", "entry": "50.00",
                                    "stop": "49.00", "target": "52.00"})
    b = desk.assess(now="2026-10-19T14:20:00Z", plan=aapl)
    assert "UNKNOWN_EXPOSURE" in b["refusal_reasons"] and b["exposure_status"] == "unknown"
    # the person states it was placed: it resolves, and inherits the ticket's max loss (the upstream `result` did)
    # (the 8 TSLA state their cost: a holding with no price and no stated cost is UNKNOWN_EXPOSURE, ADR-009)
    placed = desk.write("placed.json", {"settled_cash": "2086.56", "reserved_cash": "0",
                                        "positions": [{"symbol": "TSLA", "shares": "8", "lots": [
                                            {"date": "2026-10-19", "shares": "8", "cost": "2913.44"}]}],
                                        "orders": [{"symbol": "TSLA", "side": "buy", "status": "placed",
                                                    "date": "2026-10-19", "ticket": pt}]})
    c = desk.assess(now="2026-10-19T14:21:00Z", plan=aapl, account=placed)
    assert c["ticket_eligible"] is True, c["refusal_reasons"]
    again = desk.assess(now="2026-10-19T14:22:00Z", account=placed)
    assert "NOT_AUTHORIZED" in again["refusal_reasons"]
    assert any("already placed today" in lim["detail"] for lim in again["limitations"])
    # or it says it was not placed: then nothing counts
    denied = desk.write("denied.json", {"settled_cash": "5000", "reserved_cash": "0", "positions": [],
                                        "orders": [{"symbol": "TSLA", "side": "buy", "status": "denied",
                                                    "date": "2026-10-19", "ticket": pt}]})
    assert desk.assess(now="2026-10-19T14:23:00Z", account=denied)["ticket_eligible"] is True


def test_the_risk_check_expires(desk):
    a = desk.assess()
    pt = "PT-" + a["assessment_id"][3:]
    desk.text("ticket", a["assessment_id"])
    _, text = desk.text("ticket", "show", pt, now="2026-10-19T14:29:00Z")
    assert "READY: risk check PASS" in text
    _, text = desk.text("ticket", "show", pt, now="2026-10-19T14:31:00Z")
    assert "NOT READY: the risk check expired at 10:30 New York" in text
    _, out = desk("ticket", "approve", pt, "--reply", "yes", now="2026-10-19T14:31:00Z", ok=False)
    assert "expired" in out["failure"]["message"]
    b = desk.assess(now="2026-10-19T14:40:00Z")
    _, out = desk("ticket", b["assessment_id"], now="2026-10-19T14:56:00Z", ok=False)
    assert out["failure"]["code"] == "DATA_STALE" and "expired at 10:55" in out["failure"]["message"]


def test_yesterdays_check_does_not_count(desk):
    a = desk.assess()
    _, out = desk("ticket", a["assessment_id"], now="2026-10-20T14:16:00Z", ok=False)
    assert "expired" in out["failure"]["message"]


def test_a_newer_assessment_supersedes_an_older_one(desk):
    # The upstream single orders.json and PR #2's drop_stale "on every failure": only the latest attempt can be a ticket
    first = desk.assess()
    far = desk.write("far.json", {"symbol": "TSLA", "direction": "long", "date": "2026-10-19", "entry": "364.18",
                                  "stop": "300", "target": "500"})
    refused = desk.assess(now="2026-10-19T14:16:00Z", plan=far)
    assert refused["ticket_eligible"] is False
    _, out = desk("ticket", first["assessment_id"], now="2026-10-19T14:17:00Z", ok=False)
    assert "supersedes" in out["failure"]["message"] and out["failure"]["code"] == "DATA_STALE"
    # a failed attempt (a plan for another day, a NaN limit) supersedes too
    second = desk.assess(now="2026-10-19T14:18:00Z")
    nan = desk.write("nan.json", dict(EXAMPLE, max_loss_per_trade_usd="NaN"))
    _, out = desk("risk", "assess", "--plan", desk.files["plan"], "--policy", nan, "--account",
                     desk.files["account"], now="2026-10-19T14:19:00Z", ok=False)
    assert out["failure"]["message"] == "max_loss_per_trade_usd must be a positive finite number, got 'NaN'"
    _, out = desk("ticket", second["assessment_id"], now="2026-10-19T14:20:00Z", ok=False)
    assert "supersedes" in out["failure"]["message"]
    third = desk.assess(now="2026-10-19T14:21:00Z")
    _, out = desk("risk", "assess", "--plan", desk.files["plan"], "--policy", desk.files["policy"],
                     "--account", desk.files["account"], now="2026-10-20T14:21:00Z", ok=False)
    assert "the plan is for 2026-10-19 and today is 2026-10-20" in out["failure"]["message"]
    desk("ticket", third["assessment_id"], now="2026-10-19T14:22:00Z", ok=False)
    # a ticket drafted before a newer attempt can no longer be approved
    fourth = desk.assess(now="2026-10-19T14:23:00Z")
    desk("ticket", fourth["assessment_id"], now="2026-10-19T14:23:00Z", ok=True)
    desk.assess(now="2026-10-19T14:24:00Z", plan=far)
    _, out = desk("ticket", "approve", "PT-" + fourth["assessment_id"][3:], "--reply", "yes",
                     now="2026-10-19T14:25:00Z", ok=False)
    assert "supersedes" in out["failure"]["message"]


def test_blocked_plans_are_never_ready(desk):
    a = desk.assess(now="2026-10-19T13:40:00Z")                     # 9:40 New York
    assert a["ticket_eligible"] is False
    assert any("no new entries before 09:45" in lim["detail"] for lim in a["limitations"])
    _, out = desk("ticket", a["assessment_id"], now="2026-10-19T13:41:00Z", ok=False)
    assert out["failure"]["code"] == "NOT_AUTHORIZED"


def test_decline_is_logged(desk):
    a = desk.assess()
    pt = "PT-" + a["assessment_id"][3:]
    desk.text("ticket", a["assessment_id"])
    desk("ticket", "decline", pt, "--reply", "no thanks", ok=True)
    _, text = desk.text("ticket", "show", pt)
    assert "DONE: declined" in text
    _, text = desk.text("ticket", "log")
    assert "declined" in text and "0 placed today." in text
    _, out = desk("ticket", "approve", pt, "--reply", "yes", ok=False)
    assert "DONE: declined" in out["failure"]["message"]


def test_ticket_usage_errors(desk):
    _, out = desk("ticket", "show", now=None, ok=False)
    assert "say which ticket" in out["failure"]["message"]
    _, out = desk("ticket", "RA-x", "PT-y", now=None, ok=False)
    assert "unknown ticket action" in out["failure"]["message"]
    _, out = desk("ticket", "show", "PT-missing", now=None, ok=False)
    assert out["failure"]["code"] == "MISSING_DATA"


# ---- the ticket's own rules ----------------------------------------------------------------------------------------
def _eligible():
    from tests.unit.test_risk import NOW, QUOTE, account, plan, policy
    p = plan()
    return p, risk.assess(p, policy(), account=account(), prices=QUOTE, now=NOW)


def test_a_verdict_with_no_expiry_counts_as_expired():
    # review change 1 to #21 (P11): PR #21 skipped the check when valid_until was missing
    p, a = _eligible()
    old = dataclasses.replace(a, valid_until=None, canonical_hash=None)
    with pytest.raises(TraderOSError) as e:
        risk.ticket(p, old, now=a.created_at)
    assert e.value.code is ErrorCode.DATA_STALE and "counts as expired" in e.value.message
    ready, label = tickets.status_of(old, None, a.created_at)
    assert not ready and "counts as expired" in label


def test_an_edited_plan_is_not_this_ticket():
    p, a = _eligible()
    for change in ({"stop_price": D("300")}, {"entry_price": D("365")}, {"target_price": D("380")}):
        with pytest.raises(ValidationError) as e:
            risk.ticket(dataclasses.replace(p, **change), a, now=a.created_at)
        assert e.value.code is ErrorCode.INTEGRITY_MISMATCH


def test_the_ticket_carries_its_target_and_says_paper_only():
    p, a = _eligible()
    t = risk.ticket(p, a, now=a.created_at + timedelta(minutes=1))
    assert (t.quantity, t.order_type, t.limit_price, t.stop_price, t.target_price) == \
        (D("8"), "market", None, D("358.32"), D("370.04"))
    assert "never places a live order" in t.disclaimer and "PAPER account" in t.disclaimer
    assert tickets.plain(risk.ticket_order(t, a, p)) == TSLA_PLAIN
    assert isinstance(a, HypotheticalRiskAssessment) and a.valid_until == datetime(2026, 10, 19, 14, 30, tzinfo=UTC)


def test_a_yes_naming_another_open_ticket_is_refused(desk):
    # The upstream batch rule: the other tickets drafted today that nobody has decided on are the batch
    a = desk.assess()
    desk("ticket", a["assessment_id"], ok=True)
    aapl = desk.write("aapl.json", {"symbol": "AAPL", "direction": "long", "date": "2026-10-19", "entry": "50.00",
                                    "stop": "49.00", "target": "52.00"})
    b = desk.assess(now="2026-10-19T14:16:00Z", plan=aapl)
    desk("ticket", b["assessment_id"], now="2026-10-19T14:16:00Z", ok=True)
    pt = "PT-" + b["assessment_id"][3:]
    _, out = desk("ticket", "approve", pt, "--reply", "yes TSLA", now="2026-10-19T14:17:00Z", ok=False)
    assert "names TSLA, but this ticket is for AAPL" in out["failure"]["message"]
    desk("ticket", "approve", pt, "--reply", "yes AAPL", now="2026-10-19T14:17:00Z", ok=True)
