"""The restored order half (ADR-012): the upstream paper orders, paper accounts only, live refused in code.

The owner's ruling (OWNER post by OSDev1 on the wall, 2026-11-20 14:50 UTC; Brian's answer "Y"): "It sends only to
the paper endpoint, and live is refused in code with a test that proves it." These tests are that proof, with no
network: the client's transport is a FakeAlpaca that records every call (tests/unit/alpaca_fake.py), and conftest
makes the real transport fail any test that reaches it.

Ported from the upstream tests/test_broker.py (tank/day-trader-skill at 3d0f4ef, with PRs #2 and #21) for the restored
half: TicketShape.test_tool_input_is_the_alpaca_call and test_the_bracket_call_leaves_out_why_and_plan; Flow's
test_the_full_two_yes_flow, test_flatten_by_the_close, test_flatten_with_the_broker_qty,
test_result_needs_an_approval_and_decline_is_logged, test_ibkr_tickets_send_the_final_yes_to_ibkr (now: refused) and
test_fill_sell_relieves_the_oldest_lot. Not ported, with the reason: DesktopConfig (claude_desktop_config.example.json
configured the MCP server that no longer sends; TraderOS sends itself) and test_a_malformed_state_file_is_one_line_not_
a_traceback (broker.json is the journal now; a refusal is typed and one line by the CLI contract, tested in
tests/contract/test_cli_v1.py). The document tests stay in test_risk_tickets.py.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from tests.unit.alpaca_fake import KEY_ID, SECRET, FakeAlpaca, install
from tests.unit.quotes import quote_file
from traderos import cli
from traderos.adapters import alpaca_paper
from traderos.contracts import AuthorizationError, ErrorCode, TraderOSError
from traderos.risk import policy, tickets
from traderos.risk.policy import EXAMPLE, PAPER_EXAMPLE

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "risk"
AT = "2026-10-19T14:15:00Z"                        # 10:15 New York
NOW = datetime(2026, 10, 19, 14, 16, tzinfo=UTC)   # place's real clock in these tests: a minute later
AAPL = {"symbol": "AAPL", "side": "buy", "notional": "1250", "type": "market", "time_in_force": "day"}
TSLA_CALL = {"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
# what alpaca-mcp-server 2.4.1's place_stock_order POSTed for TSLA_CALL (overrides.py, place_stock_order)
TSLA_REST = {"symbol": "TSLA", "side": "buy", "type": "market", "time_in_force": "day", "qty": "8",
             "order_class": "bracket", "take_profit": {"limit_price": "370.04"}, "stop_loss": {"stop_price": "358.32"}}
FLATTEN_CALL = {"symbol": "TSLA", "side": "sell", "qty": "8", "type": "market", "time_in_force": "day"}
REAL_TRANSPORT = alpaca_paper._urllib_transport          # captured at import, before conftest's no-network guard


def tsla(**over):
    o = json.loads((FIX / "orders-tsla.json").read_text(encoding="utf-8"))["orders"][0]
    o.update(over)
    return o


# ---- the desk: a person at the CLI ---------------------------------------------------------------------------------
@pytest.fixture
def desk(tmp_path, capsys, monkeypatch):
    home = str(tmp_path / "home")
    seen = []                                       # every byte printed, for the secrets test

    def write(name, data):
        path = tmp_path / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    files = {"plan": write("plan.json", {"symbol": "TSLA", "direction": "long", "date": "2026-10-19",
                                         "entry": "364.18", "stop": "358.32", "target": "370.04"}),
             "policy": write("policy.json", PAPER_EXAMPLE),
             "account": write("account.json", {"settled_cash": "5000", "reserved_cash": "0", "positions": [],
                                               "orders": []})}

    def run(*argv, now=AT, ok=None):
        args = ["--json", "--home", home, *argv]
        if now:
            args += ["--now", now]
        code = cli.main(args)
        cap = capsys.readouterr()
        seen.append(cap.out + cap.err)
        out = json.loads(cap.out)
        if ok is not None:
            assert code == (0 if ok else 3), out
        return code, out

    def text(*argv, now=AT):
        args = ["--home", home, *argv] + (["--now", now] if now else [])
        code = cli.main(args)
        cap = capsys.readouterr()
        seen.append(cap.out + cap.err)
        return code, cap.out + cap.err

    def assess(now=AT, **over):
        f = dict(files, **over)
        quote = quote_file(tmp_path, f["plan"], now, "quote.json")         # a fresh quote (ADR-009, review item 4)
        _, out = run("risk", "assess", "--plan", f["plan"], "--policy", f["policy"], "--account", f["account"],
                     "--prices", quote, now=now, ok=True)
        return out["result"]

    def ticket(now=AT, **over):
        a = assess(now=now, **over)
        assert a["ticket_eligible"], a["refusal_reasons"]
        run("ticket", a["assessment_id"], now=now, ok=True)
        return "PT-" + a["assessment_id"][3:]

    def approved(now=AT, **over):
        pt = ticket(now=now, **over)
        run("ticket", "approve", pt, "--reply", "yes", now=now, ok=True)
        return pt

    def place(pt, policy_file=None, ok=None):
        return run("ticket", "place", pt, "--policy", policy_file or files["policy"], now=None, ok=ok)

    run.write, run.files, run.assess, run.ticket, run.approved, run.place, run.text = (write, files, assess, ticket,
                                                                                      approved, place, text)
    run.home, run.seen, run.tmp = pathlib.Path(home), seen, tmp_path
    return run


@pytest.fixture
def alpaca(monkeypatch):
    return install(monkeypatch, now=NOW)


# ==== 1. live is refused: the host ================================================================================
def test_the_only_host_is_the_paper_host():
    assert alpaca_paper.PAPER_HOST == "paper-api.alpaca.markets"
    assert alpaca_paper.BASE_URL == "https://paper-api.alpaca.markets"
    assert alpaca_paper.LIVE_HOSTS == ("api.alpaca.markets",)


@pytest.mark.parametrize("url", [
    "https://api.alpaca.markets/v2/orders",                      # live, by name
    "https://API.ALPACA.MARKETS/v2/orders",
    "https://paper-api.alpaca.markets@api.alpaca.markets/v2/orders",
    "https://paper-api.alpaca.markets.example.com/v2/orders",
    "http://paper-api.alpaca.markets/v2/orders",                  # not HTTPS
    "https://paper-api.alpaca.markets:8443/v2/orders",
    "https://user:pw@paper-api.alpaca.markets/v2/orders",
    "https://broker-api.alpaca.markets/v2/orders",
    "https://data.alpaca.markets/v2/orders",
    "https://paper-api.alpaca.markets/v1/orders",
    "https://paper-api.alpaca.markets/",
])
def test_any_other_address_is_refused_before_any_call(url):
    fake = FakeAlpaca()
    client = alpaca_paper.PaperClient(KEY_ID, SECRET, transport=fake)
    with pytest.raises(alpaca_paper.LiveRefused) as e:
        alpaca_paper.check_url(url)
    assert e.value.code is ErrorCode.NOT_AUTHORIZED and "live trading is refused in this build" in e.value.message
    if alpaca_paper.urllib.parse.urlsplit(url).hostname.lower() == "api.alpaca.markets":
        assert "is Alpaca's live endpoint" in e.value.message                 # refused by name
    assert fake.calls == [] and client.transport is fake


def test_the_live_host_is_refused_by_the_transport_itself(monkeypatch):
    # even a caller that bypasses the client: the real transport checks the URL before opening anything
    opened = []
    monkeypatch.setattr(alpaca_paper._OPENER, "open", lambda *a, **k: opened.append(a))
    with pytest.raises(alpaca_paper.LiveRefused):
        REAL_TRANSPORT("POST", "https://api.alpaca.markets/v2/orders", {}, b"{}")
    assert opened == []


def test_a_client_cannot_be_pointed_anywhere_else():
    with pytest.raises(TypeError):
        alpaca_paper.PaperClient(KEY_ID, SECRET, base_url="https://api.alpaca.markets")
    with pytest.raises(TypeError):
        alpaca_paper.client(base_url="https://api.alpaca.markets")
    fake = FakeAlpaca()
    alpaca_paper.PaperClient(KEY_ID, SECRET, transport=fake).account()
    assert [c[1] for c in fake.calls] == ["https://paper-api.alpaca.markets/v2/account"]


@pytest.mark.parametrize("name", alpaca_paper.ENDPOINT_ENV)
@pytest.mark.parametrize("value", ["https://api.alpaca.markets", "api.alpaca.markets", "https://example.com/v2"])
def test_an_endpoint_in_the_environment_is_refused(monkeypatch, name, value):
    fake = install(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(alpaca_paper.LiveRefused) as e:
        alpaca_paper.client(None).account()
    assert name in e.value.message and fake.calls == []


def test_the_paper_endpoint_in_the_environment_is_ignored(monkeypatch):
    fake = install(monkeypatch)
    monkeypatch.setenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    alpaca_paper.client(None).account()
    assert {urlhost(c[1]) for c in fake.calls} == {"paper-api.alpaca.markets"}


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "live", ""])
def test_a_live_switch_in_the_environment_is_refused(monkeypatch, value):
    fake = install(monkeypatch)
    monkeypatch.setenv("ALPACA_PAPER_TRADE", value)
    with pytest.raises(alpaca_paper.LiveRefused):
        alpaca_paper.client(None)
    assert fake.calls == []


def test_a_live_key_id_is_refused(monkeypatch):
    fake = install(monkeypatch)
    monkeypatch.setenv("APCA_API_KEY_ID", "AKLIVEKEY0123456789")
    with pytest.raises(alpaca_paper.LiveRefused) as e:
        alpaca_paper.client(None)
    assert "LIVE key id" in e.value.message and "AKLIVEKEY" not in e.value.message and fake.calls == []


def test_an_account_that_is_not_paper_is_refused_and_nothing_is_sent():
    fake = FakeAlpaca(account=dict(alpaca_fake_account(), account_number="912345678"))
    client = alpaca_paper.PaperClient(KEY_ID, SECRET, transport=fake)
    with pytest.raises(alpaca_paper.LiveRefused) as e:
        client.account()
    assert "not like a paper account" in e.value.message
    for acct in ({}, {"account_number": None}, [], "PA"):
        with pytest.raises(TraderOSError):
            alpaca_paper.require_paper_account(acct)
    assert alpaca_paper.require_paper_account({"account_number": "PA3ABCDEF"})["account_number"] == "PA3ABCDEF"


def test_no_redirect_is_followed():
    import urllib.request
    handler = alpaca_paper._NoRedirect()
    order = urllib.request.Request(alpaca_paper.BASE_URL + "/v2/orders", data=b"{}", method="POST")
    with pytest.raises(alpaca_paper.Indeterminate):        # it reached Alpaca: outcome unknown, never "not placed"
        handler.redirect_request(order, None, 307, "Temporary Redirect", {}, "https://api.alpaca.markets/v2/orders")
    assert any(isinstance(h, alpaca_paper._NoRedirect) for h in alpaca_paper._OPENER.handlers)
    assert not any(type(h).__name__ == "HTTPRedirectHandler" for h in alpaca_paper._OPENER.handlers)


# ==== 2. live is refused: the policy ===============================================================================
@pytest.mark.parametrize("paper", [False, "true", "True", 1, None, "yes", [True]])
def test_a_policy_whose_paper_is_not_true_is_refused(paper):
    with pytest.raises(AuthorizationError) as e:
        policy.policy(dict(EXAMPLE, paper=paper))
    assert e.value.code is ErrorCode.NOT_AUTHORIZED and "live trading is refused in this build" in e.value.message


def test_place_needs_paper_stated_true():
    checked = policy.policy(EXAMPLE)                        # paper not stated: fine to assess, not to place
    assert "paper" not in checked
    for c in (checked, dict(checked, paper=False), dict(checked, paper="true"), dict(checked, paper=1)):
        with pytest.raises(AuthorizationError) as e:
            policy.require_paper(c)
        assert e.value.code is ErrorCode.NOT_AUTHORIZED and "live trading is refused" in e.value.message
    assert policy.require_paper(policy.policy(PAPER_EXAMPLE))["paper"] is True


def test_the_ibkr_branch_is_refused():
    # The upstream test_ibkr_tickets_send_the_final_yes_to_ibkr: its IBKR branch drafted through IBKR's connector; here
    # it is refused, because TraderOS cannot prove an IBKR connector routes to a paper account
    for cfg in (dict(PAPER_EXAMPLE, broker="ibkr"), dict(PAPER_EXAMPLE, broker="IBKR")):
        with pytest.raises(AuthorizationError) as e:
            policy.policy(cfg)
        assert e.value.code is ErrorCode.NOT_AUTHORIZED and "Interactive Brokers" in e.value.message
    with pytest.raises(AuthorizationError) as e:
        policy.require_paper(dict(policy.policy(PAPER_EXAMPLE), broker="ibkr"))
    assert "Interactive Brokers" in e.value.message
    with pytest.raises(AuthorizationError):
        policy.require_paper(dict(policy.policy(PAPER_EXAMPLE), broker="schwab"))
    with pytest.raises(TraderOSError) as e2:
        policy.policy(dict(PAPER_EXAMPLE, broker="schwab"))
    assert e2.value.code is ErrorCode.INVALID_INPUT


def test_the_live_line_can_never_render():
    assert tickets.ACCOUNT_LINE == "PAPER account, Alpaca"
    source = pathlib.Path(tickets.__file__).read_text(encoding="utf-8")
    assert "REAL MONEY" not in source.split('"""', 2)[2] and "LIVE ACCOUNT" not in source.split('"""', 2)[2]


@pytest.mark.parametrize("paper", ['"paper": false', '"paper": "true"', None])
def test_place_refuses_a_policy_that_is_not_paper(desk, alpaca, paper):
    pt = desk.approved()
    if paper is None:
        bad = desk.write("bad.json", EXAMPLE)               # paper not stated
    else:
        bad = str(desk.tmp / "bad.json")
        (desk.tmp / "bad.json").write_text(json.dumps(EXAMPLE)[:-1] + ", " + paper + "}", encoding="utf-8")
    _, out = desk.place(pt, bad, ok=False)
    assert out["failure"]["code"] == "NOT_AUTHORIZED" and "live trading is refused" in out["failure"]["message"]
    assert alpaca.calls == []


def test_place_refuses_the_ibkr_policy(desk, alpaca):
    pt = desk.approved()
    _, out = desk.place(pt, desk.write("ibkr.json", dict(PAPER_EXAMPLE, broker="ibkr")), ok=False)
    assert out["failure"]["code"] == "NOT_AUTHORIZED" and "Interactive Brokers" in out["failure"]["message"]
    assert alpaca.calls == []


def test_place_refuses_a_policy_the_check_did_not_use(desk, alpaca):
    pt = desk.approved()
    other = desk.write("other.json", dict(PAPER_EXAMPLE, max_loss_per_trade_usd="500"))
    _, out = desk.place(pt, other, ok=False)
    assert out["failure"]["code"] == "INTEGRITY_MISMATCH" and alpaca.calls == []
    desk("ticket", "result", pt, "--status", "denied", ok=True)          # it was not sent: say so
    # an assessment made under a policy without "paper" cannot be placed with one that adds it
    pt2 = desk.approved(now="2026-10-19T14:15:30Z", policy=desk.write("nopaper.json", EXAMPLE))
    _, out = desk.place(pt2, ok=False)
    assert out["failure"]["code"] == "INTEGRITY_MISMATCH" and alpaca.calls == []



def test_place_refuses_a_contract_multiplier(desk, alpaca):
    # review of #10, item 2: an Alpaca paper order is a number of shares; a plan sized in contracts of 100 would send
    # a hundredth of what was approved, so it is refused before any call
    plan = desk.write("contracts.json", {"symbol": "ABC", "direction": "long", "date": "2026-10-19", "entry": "1.00",
                                         "stop": "0.99", "target": "1.05", "multiplier": "100"})
    pt = desk.approved(plan=plan)
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "OUTSIDE_CAPABILITY" and "multiplier of 100" in out["failure"]["message"]
    assert alpaca.calls == []

# ==== 3. the human gate: an explicit yes, for this exact ticket, under an unexpired check =========================
def test_no_yes_no_order(desk, alpaca):
    pt = desk.ticket()
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "NOT_AUTHORIZED" and "no yes is recorded" in out["failure"]["message"]
    desk("ticket", "approve", pt, "--reply", "sure", ok=False)              # not a yes: nothing recorded
    _, out = desk.place(pt, ok=False)
    assert "no yes is recorded" in out["failure"]["message"] and alpaca.calls == []


def test_a_declined_ticket_is_never_sent(desk, alpaca):
    pt = desk.ticket()
    desk("ticket", "decline", pt, ok=True)
    _, out = desk.place(pt, ok=False)
    assert "DONE: declined" in out["failure"]["message"] and alpaca.calls == []


def test_the_yes_is_bound_to_the_exact_order(desk, alpaca, monkeypatch):
    pt = desk.approved()
    from traderos.risk import check
    real = check.fingerprint
    monkeypatch.setattr(tickets, "fingerprint", lambda o: real(dict(o, qty="9")))     # not the order approved
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "INTEGRITY_MISMATCH" and "not for this exact order" in out["failure"]["message"]
    assert alpaca.calls == []


def test_an_expired_check_is_never_sent(desk, monkeypatch):
    pt = desk.approved()                                                       # valid until 10:30 New York
    fake = install(monkeypatch, now=datetime(2026, 10, 19, 14, 31, tzinfo=UTC))
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "DATA_STALE" and "expired at 10:30" in out["failure"]["message"]
    assert "--status denied" in out["failure"]["message"] and fake.calls == []


def test_place_takes_no_now_and_a_check_from_the_future_is_refused(desk, monkeypatch):
    # placing uses the real clock: --now cannot move an expiry, and a check made at a pinned future time is refused
    pt = desk.approved()
    fake = install(monkeypatch, now=datetime(2026, 10, 19, 14, 40, tzinfo=UTC))
    _, out = desk("ticket", "place", pt, "--policy", desk.files["policy"], now="2026-10-19T14:20:00Z", ok=False)
    assert out["failure"]["code"] == "INVALID_INPUT" and "takes no --now" in out["failure"]["message"]
    fake = install(monkeypatch, now=datetime(2026, 10, 19, 13, 0, tzinfo=UTC))       # real time before the check
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "DATA_STALE" and "after the real time" in out["failure"]["message"]
    assert fake.calls == []


def test_a_superseded_ticket_is_never_sent(desk, alpaca):
    pt = desk.approved()
    far = desk.write("far.json", {"symbol": "TSLA", "direction": "long", "date": "2026-10-19", "entry": "364.18",
                                  "stop": "300", "target": "500"})
    desk.assess(now="2026-10-19T14:15:30Z", plan=far)
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "DATA_STALE" and "supersedes" in out["failure"]["message"]
    assert alpaca.calls == []


def test_no_keys_no_order_and_how_to_set_them(desk, monkeypatch):
    pt = desk.approved()
    fake = install(monkeypatch, now=NOW, keys=False)
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "NOT_AUTHORIZED"
    assert "traderos config set alpaca_paper_key_id" in out["failure"]["message"] and fake.calls == []


# ==== 4. what is sent: the upstream tool_input, as the MCP server posted it
# ================================================
def test_tool_input_is_the_alpaca_call():
    # The upstream TicketShape.test_tool_input_is_the_alpaca_call, unchanged
    o = dict(AAPL, why="from a list")
    assert tickets.tool_input(o) == AAPL
    assert tickets.tool_input({"symbol": "KO", "side": "buy", "qty": 3, "type": "limit", "limit_price": 89.5,
                               "time_in_force": "day"}) == {"symbol": "KO", "side": "buy", "qty": "3",
                                                            "type": "limit", "limit_price": "89.5",
                                                            "time_in_force": "day"}


def test_the_bracket_call_leaves_out_why_and_plan():
    # The upstream TicketShape.test_the_bracket_call_leaves_out_why_and_plan, unchanged
    assert tickets.tool_input(tsla()) == TSLA_CALL
    assert tickets.fingerprint(tsla()) == tickets.fingerprint(dict(TSLA_CALL, plan=tsla()["plan"]))
    assert tickets.fingerprint(tsla()) != tickets.fingerprint(TSLA_CALL)


@pytest.mark.parametrize("order", [AAPL, dict(AAPL, why="x"), FLATTEN_CALL,
                                   {"symbol": "KO", "side": "sell", "qty": "1", "type": "limit", "limit_price": "95",
                                    "time_in_force": "gtc"}])
def test_the_body_of_an_order_without_legs_is_tool_input_byte_for_byte(order):
    fake = FakeAlpaca()
    alpaca_paper.PaperClient(KEY_ID, SECRET, transport=fake).submit(tickets.body(order))
    (method, url, headers, sent), = fake.calls
    assert (method, url) == ("POST", "https://paper-api.alpaca.markets/v2/orders")
    assert sent == json.dumps(tickets.tool_input(order), separators=(",", ":")).encode()
    assert headers["Content-Type"] == "application/json"


def test_the_bracket_body_is_tool_input_with_the_legs_as_the_server_posted_them():
    fake = FakeAlpaca()
    alpaca_paper.PaperClient(KEY_ID, SECRET, transport=fake).submit(tickets.body(tsla()))
    sent = fake.calls[0][3]
    body = json.loads(sent)
    assert body == TSLA_REST                                          # what alpaca-mcp-server POSTed for TSLA_CALL
    assert json.dumps(alpaca_paper.tool_args(body), separators=(",", ":")).encode() == \
        json.dumps(TSLA_CALL, separators=(",", ":")).encode()       # and back: the upstream tool_input, byte for byte
    assert sent == json.dumps({"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
                               "order_class": "bracket", "take_profit": {"limit_price": "370.04"},
                               "stop_loss": {"stop_price": "358.32"}}, separators=(",", ":")).encode()
    leg = alpaca_paper.order_body(dict(TSLA_CALL, stop_loss_limit_price="358.00"))
    assert leg["stop_loss"] == {"stop_price": "358.32", "limit_price": "358.00"}
    assert alpaca_paper.order_body({"symbol": "X", "side": "buy", "qty": "1", "type": "market",
                                    "time_in_force": "day", "stop_loss_stop_price": "9"})["order_class"] == "bracket"
    with pytest.raises(TraderOSError):
        alpaca_paper.order_body({"symbol": "X", "stop_loss_limit_price": "9"})


@pytest.mark.parametrize("limit", [None, "364.00"])
def test_what_is_sent_is_what_the_check_passed(limit):
    # the ticket's order (what place sends) carries exactly the fields of the order the upstream check passed
    from tests.unit.test_risk import NOW as CHECKED_AT
    from tests.unit.test_risk import QUOTE, account, plan
    from traderos import risk
    p = plan(limit_price=D(limit)) if limit else plan()
    ev = risk.evaluate(p, PAPER_EXAMPLE, account=account(), prices=QUOTE, now=CHECKED_AT)
    assert ev.assessment.ticket_eligible, ev.assessment.refusal_reasons
    t = risk.ticket(p, ev.assessment, now=CHECKED_AT)
    sent = tickets.tool_input(risk.ticket_order(t, ev.assessment, p))
    assert sent == tickets.tool_input(ev.order)
    assert sent["order_class"] == "bracket" and sent["stop_loss_stop_price"] and sent["take_profit_limit_price"]


# ==== 5. the full flow: the upstream two yeses
# ==========================================================================
def test_the_full_two_yes_flow(desk, alpaca):
    # The upstream Flow.test_the_full_two_yes_flow, with TraderOS as the sender
    pt = desk.ticket()
    code, text = desk.text("ticket", "show", pt)
    assert code == 0
    assert text.startswith(f"PAPER TICKET {pt}, PAPER account, Alpaca\n")
    for part in ("BUY 8 TSLA, market, good for today", "stop-loss 358.32", "take-profit 370.04",
                 "max loss about $46.88 if the stop fills at 358.32 (it can fill worse in a fast market)"):
        assert "  " + part + "\n" in text
    assert "READY: risk check PASS" in text
    assert "TraderOS will send exactly this to Alpaca's paper endpoint (paper-api.alpaca.markets):" in text
    assert '"order_class": "bracket"' in text and '"stop_price": "358.32"' in text
    assert '"plan"' not in text and '"why"' not in text and "LIVE" not in text
    assert f"Then `traderos ticket place {pt}` sends it" in text
    _, out = desk("ticket", "show", pt, ok=True)
    shown = out["result"]
    assert shown["tool_input"] == TSLA_CALL and shown["body"] == TSLA_REST and shown["paper"] is True
    assert shown["endpoint"] == "https://paper-api.alpaca.markets" and shown["max_loss"] == "46.88"

    _, out = desk("ticket", "approve", pt, "--reply", "sure", ok=False)
    assert out["failure"]["code"] == "INVALID_APPROVAL"
    code, text = desk.text("ticket", "approve", pt, "--reply", "yes")
    assert code == 0 and f"Recorded your yes for {pt}" in text and "Nothing is sent before that" in text
    assert json.loads(text[text.index("{"):text.rindex("}") + 1]) == TSLA_REST
    _, out = desk("ticket", "approve", pt, "--reply", "yes", ok=False)
    assert "APPROVED" in out["failure"]["message"]
    assert alpaca.calls == []                                               # a yes sends nothing

    _, out = desk.place(pt, ok=True)
    assert [(m, u) for m, u, _, _ in alpaca.calls] == [
        ("GET", "https://paper-api.alpaca.markets/v2/account"), ("POST", "https://paper-api.alpaca.markets/v2/orders")]
    assert json.loads(alpaca.calls[1][3]) == TSLA_REST
    oid = out["result"]["broker_order_id"]
    assert out["result"]["broker_status"] == "accepted" and out["result"]["endpoint"] == alpaca_paper.BASE_URL
    _, out = desk.place(pt, ok=False)                                       # never twice
    assert "DONE: placed" in out["failure"]["message"] and len(alpaca.sent()) == 1
    _, text = desk.text("ticket", "show", pt)
    assert "DONE: placed" in text

    alpaca.fill(oid, 8, "364.20")
    _, out = desk("ticket", "fill", pt, "--account", desk.files["account"], now="2026-10-19T14:16:00Z", ok=True)
    acct = json.loads(pathlib.Path(desk.files["account"]).read_text(encoding="utf-8"))
    assert acct["settled_cash"] == "2086.40" and acct["positions"][0]["shares"] == "8"
    assert acct["positions"][0]["lots"] == [{"id": "TSLA-1", "date": "2026-10-19", "shares": "8", "cost": "2913.60"}]
    _, out = desk("ticket", "fill", pt, "--account", desk.files["account"], now="2026-10-19T14:17:00Z", ok=True)
    assert out["result"]["recorded"] == 0                                   # each fill once

    code, text = desk.text("ticket", "log")
    assert "placed" in text and "1 placed today." in text and "filled" in text
    # the risk check now counts it: a second TSLA buy today is a duplicate, and $46.88 is used
    again = desk.assess(now="2026-10-19T14:20:00Z")
    assert again["ticket_eligible"] is False
    assert any("already placed today" in lim["detail"] for lim in again["limitations"])
    from traderos.commands import decisions
    from traderos.risk import check
    from traderos.store import Store
    res = check.check([tsla()], PAPER_EXAMPLE, state={"orders": decisions._log(Store(desk.home))},
                      now=datetime(2026, 10, 19, 14, 30, tzinfo=UTC))
    assert (res["today"]["entries_placed"], res["today"]["loss_at_risk"]) == (1, D("46.88"))
    assert [r["status"] for r in decisions._log(Store(desk.home))] == ["approved", "placed", "filled"]


def test_an_approved_ticket_counts_as_placed_until_its_result(desk, alpaca):
    pt = desk.approved()
    aapl = desk.write("aapl.json", {"symbol": "AAPL", "direction": "long", "date": "2026-10-19", "entry": "50.00",
                                    "stop": "49.00", "target": "52.00"})
    b = desk.assess(now="2026-10-19T14:16:00Z", plan=aapl)
    assert "UNKNOWN_EXPOSURE" in b["refusal_reasons"]
    desk("ticket", "result", pt, "--status", "denied", now="2026-10-19T14:17:00Z", ok=True)
    c = desk.assess(now="2026-10-19T14:18:00Z", plan=aapl)
    assert c["ticket_eligible"] is True, c["refusal_reasons"]
    _, out = desk.place(pt, ok=False)
    assert "DONE: denied" in out["failure"]["message"] and alpaca.calls == []


def test_result_needs_an_approval_and_decline_is_logged(desk):
    # The upstream Flow.test_result_needs_an_approval_and_decline_is_logged
    pt = desk.ticket()
    _, out = desk("ticket", "result", pt, "--status", "placed", ok=False)
    assert "no yes recorded" in out["failure"]["message"]
    desk("ticket", "decline", pt, "--reply", "no thanks", ok=True)
    _, text = desk.text("ticket", "show", pt)
    assert "DONE: declined" in text


def test_result_by_hand_with_a_fill(desk):
    # The upstream `result --status placed --broker-id --filled-qty --filled-price`: what happened, when the person
    # knows
    pt = desk.approved()
    desk("ticket", "result", pt, "--status", "placed", "--broker-id", "abc123", "--filled-qty", "8",
         "--filled-price", "364.20", "--account", desk.files["account"], now="2026-10-19T14:16:00Z", ok=True)
    acct = json.loads(pathlib.Path(desk.files["account"]).read_text(encoding="utf-8"))
    assert (acct["settled_cash"], acct["positions"][0]["shares"]) == ("2086.40", "8")
    _, text = desk.text("ticket", "log")
    assert "Alpaca order abc123" in text and "1 placed today." in text


def test_a_refused_order_is_failed_and_an_unknown_one_is_never_resent(desk, monkeypatch):
    fake = install(monkeypatch, FakeAlpaca(submit=(403, {"code": 40310000, "message": "insufficient buying power"})),
                   now=NOW)
    pt = desk.approved()
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "NOT_AUTHORIZED" and "insufficient buying power" in out["failure"]["message"]
    _, text = desk.text("ticket", "show", pt)
    assert "DONE: failed" in text
    _, out = desk.place(pt, ok=False)
    assert "DONE: failed" in out["failure"]["message"] and len(fake.sent()) == 1
    # a 5xx (or a timeout) may have landed: indeterminate, never resent, until the person records what happened
    fake2 = install(monkeypatch, FakeAlpaca(submit=(503, {"message": "unavailable"})), now=NOW)
    desk("ticket", "result", pt, "--status", "failed", ok=False)            # failed is final: no new result
    pt2 = desk.approved(now="2026-10-19T14:15:20Z")
    _, out = desk.place(pt2, ok=False)
    assert out["failure"]["code"] == "BROKER_INDETERMINATE"
    _, out = desk.place(pt2, ok=False)
    assert out["failure"]["code"] == "BROKER_INDETERMINATE" and "never sent again" in out["failure"]["message"]
    assert len(fake2.sent()) == 1
    _, text = desk.text("ticket", "show", pt2)
    assert "OUTCOME UNKNOWN" in text
    # while unknown, it counts as placed: a new entry is refused
    aapl = desk.write("aapl.json", {"symbol": "AAPL", "direction": "long", "date": "2026-10-19", "entry": "50.00",
                                    "stop": "49.00", "target": "52.00"})
    assert "UNKNOWN_EXPOSURE" in desk.assess(now="2026-10-19T14:16:00Z", plan=aapl)["refusal_reasons"]
    desk("ticket", "result", pt2, "--status", "placed", "--broker-id", "b7d0e2c1-6a4f-4d3b-9e8a-000000000009",
         now="2026-10-19T14:17:00Z", ok=True)
    _, text = desk.text("ticket", "show", pt2)
    assert "DONE: placed" in text


def test_the_send_is_recorded_before_it_happens(desk, monkeypatch):
    # a crash between the send and its answer leaves the ticket "outcome unknown", never "approved": no resend
    pt = desk.approved()
    fake = install(monkeypatch, now=NOW)

    def crash(method, url, headers, body):
        if method == "POST":
            fake.calls.append((method, url, headers, body))
            raise RuntimeError("the machine went away mid-send")
        return fake(method, url, headers, body)
    monkeypatch.setattr(alpaca_paper, "_urllib_transport", crash)
    code, text = desk.text("ticket", "place", pt, "--policy", desk.files["policy"], now=None)
    assert code == 4 and "This is a bug" in text                             # a crash, reported as a bug
    _, text = desk.text("ticket", "show", pt)
    assert "OUTCOME UNKNOWN" in text
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "BROKER_INDETERMINATE" and len(fake.sent()) == 1


def test_only_one_sender(desk, monkeypatch):
    from traderos.commands import decisions, paper
    pt = desk.approved()
    fake = install(monkeypatch, now=NOW)
    real = decisions.remember_payload

    def racing(store, kind, refs, summary, data, **kw):                       # another place claims it first
        if kind == "journal.ticket.indeterminate" and not getattr(racing, "done", False):
            racing.done = True
            real(store, kind, refs, "the other sender", dict(data, note="other"), **kw)
        return real(store, kind, refs, summary, data, **kw)
    monkeypatch.setattr(decisions, "remember_payload", racing)
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "BROKER_INDETERMINATE" and "another" in out["failure"]["message"]
    assert fake.sent() == []
    monkeypatch.setattr(decisions, "remember_payload", real)
    from traderos.store import Store
    store = Store(desk.home)
    t, a, _, _ = decisions._load_ticket(store, pt)
    with pytest.raises(TraderOSError) as e:                                   # a claimed ticket is not claimed again
        paper._claim(store, t, a, {}, "x", NOW)
    assert "changed while it was being placed" in e.value.message


def test_a_send_that_never_left_is_failed(desk, monkeypatch):
    pt = desk.approved()
    fake = install(monkeypatch, now=NOW)

    def no_dns(method, url, headers, body):
        if method == "POST":
            raise alpaca_paper.DataError("could not reach Alpaca's paper endpoint (gaierror); nothing was sent")
        return fake(method, url, headers, body)
    monkeypatch.setattr(alpaca_paper, "_urllib_transport", no_dns)
    _, out = desk.place(pt, ok=False)
    assert out["failure"]["code"] == "MISSING_DATA" and "nothing was sent" in out["failure"]["message"]
    _, text = desk.text("ticket", "show", pt)
    assert "DONE: failed" in text


def test_a_timeout_on_the_send_is_indeterminate(monkeypatch):
    def timeout(method, url, headers, body):
        raise alpaca_paper.Indeterminate("no answer from Alpaca's paper endpoint (TimeoutError)")
    client = alpaca_paper.PaperClient(KEY_ID, SECRET, transport=timeout)
    with pytest.raises(alpaca_paper.Indeterminate) as e:
        client.submit(TSLA_REST)
    assert e.value.code is ErrorCode.BROKER_INDETERMINATE
    with pytest.raises(TraderOSError) as e:                               # a read that times out changed nothing
        client.account()
    assert e.value.code is ErrorCode.MISSING_DATA and "nothing was changed" in e.value.message


# ==== 6. flatten by the close =====================================================================================
def test_flatten_by_the_close(desk, monkeypatch):
    # The upstream Flow.test_flatten_by_the_close: the entry, its fill, then the flatten at 15:55
    fake = install(monkeypatch, now=NOW)
    pt = desk.approved()
    _, out = desk.place(pt, ok=True)
    fake.fill(out["result"]["broker_order_id"], 8, "364.20")
    desk("ticket", "fill", pt, "--account", desk.files["account"], now="2026-10-19T14:16:00Z", ok=True)
    late = "2026-10-19T19:55:00Z"                                            # 15:55 New York
    code, text = desk.text("ticket", "flatten", "TSLA", "--policy", desk.files["policy"], "--account",
                           desk.files["account"], now=late)
    assert code == 0, text
    assert "Flatten TSLA: SELL 8 shares of TSLA, market order, good for today (flat by 15:55 New York)." in text
    assert "passed it as an exit" in text and "cancels your open TSLA sell orders" in text
    fl = text.split("Ticket ", 1)[1].split(":", 1)[0]
    _, out = desk("ticket", "show", fl, now=late, ok=True)
    assert out["result"]["tool_input"] == FLATTEN_CALL and out["result"]["body"] == FLATTEN_CALL
    assert out["result"]["kind"] == "exit" and out["result"]["ready"] is True
    desk("ticket", "approve", fl, "--reply", "yes sell 8 TSLA", now=late, ok=True)
    install(monkeypatch, fake, now=datetime(2026, 10, 19, 19, 56, tzinfo=UTC))
    before = len(fake.calls)
    _, out = desk.place(fl, ok=True)
    calls = [(m, u.split("?")[0].rsplit("/", 1)[-1] if m != "POST" else "orders") for m, u, _, _ in
             fake.calls[before:]]
    assert calls[0] == ("GET", "account") and calls[1] == ("GET", "orders")
    assert [m for m, _ in calls].count("DELETE") == 2                        # the bracket's two legs
    assert calls[-1] == ("POST", "orders") and json.loads(fake.calls[-1][3]) == FLATTEN_CALL
    assert len(out["result"]["canceled"]) == 2
    _, text = desk.text("ticket", "log", now="2026-10-19T19:56:00Z")
    assert text.count(" canceled ") == 2 and "2 placed today." in text
    flat = fake.orders[out["result"]["broker_order_id"]]
    fake.fill(flat["id"], 8, "366.10")
    desk("ticket", "fill", fl, "--account", desk.files["account"], now="2026-10-19T19:56:00Z", ok=True)
    acct = json.loads(pathlib.Path(desk.files["account"]).read_text(encoding="utf-8"))
    assert (acct["settled_cash"], acct["unsettled_cash"], acct["positions"]) == ("2086.40", "2928.80", [])
    from traderos.commands import decisions
    from traderos.risk import check
    from traderos.store import Store
    assert check.day_trades({"orders": decisions._log(Store(desk.home))}, "2026-10-19") == (1, ["2026-10-19 TSLA"])
    _, out = desk("ticket", "flatten", "TSLA", "--policy", desk.files["policy"], "--account", desk.files["account"],
                  now=late, ok=False)
    assert "nothing to flatten" in out["failure"]["message"]


def test_flatten_with_the_broker_qty(desk):
    # The upstream Flow.test_flatten_with_the_broker_qty: --qty when the statement does not show it; a sell of shares
    # you do not hold is a short entry, which the check refuses (no ticket); --qty 0 is refused
    held = desk.write("held.json", {"settled_cash": "2086.40", "reserved_cash": "0",
                                    "positions": [{"symbol": "TSLA", "shares": "8"}], "orders": []})
    code, text = desk.text("ticket", "flatten", "tsla", "--qty", "8", "--policy", desk.files["policy"], "--account",
                           held)
    assert code == 0 and "Check it against your paper account" not in text
    _, out = desk("ticket", "flatten", "TSLA", "--qty", "0", "--policy", desk.files["policy"], "--account", held,
                  ok=False)
    assert "--qty wants the number of shares you hold" in out["failure"]["message"]
    _, out = desk("ticket", "flatten", "TSLA", "--qty", "8", "--policy", desk.files["policy"], "--account",
                  desk.files["account"], ok=False)
    assert out["failure"]["code"] == "NOT_AUTHORIZED" and "short" in out["failure"]["message"]


def test_a_flatten_whose_leg_filled_meanwhile_is_not_sent(desk, monkeypatch):
    fake = install(monkeypatch, now=NOW)
    pt = desk.approved()
    _, out = desk.place(pt, ok=True)
    fake.fill(out["result"]["broker_order_id"], 8, "364.20")
    desk("ticket", "fill", pt, "--account", desk.files["account"], ok=True)
    late = "2026-10-19T19:55:00Z"
    _, out = desk("ticket", "flatten", "TSLA", "--policy", desk.files["policy"], "--account", desk.files["account"],
                  now=late, ok=True)
    fl = out["result"]["ticket_id"]
    desk("ticket", "approve", fl, "--reply", "yes", now=late, ok=True)
    fake.cancel_to = "filled"                                                 # the stop filled first
    install(monkeypatch, fake, now=datetime(2026, 10, 19, 19, 56, tzinfo=UTC))
    _, out = desk.place(fl, ok=False)
    assert out["failure"]["code"] == "UNKNOWN_EXPOSURE" and "may already be closed" in out["failure"]["message"]
    assert fake.sent()[-1][3] != json.dumps(FLATTEN_CALL, separators=(",", ":")).encode()


# ==== 7. fills: the upstream apply_fill on the account statement
# ========================================================
def test_fill_sell_relieves_the_oldest_lot(desk):
    # The upstream Flow.test_fill_sell_relieves_the_oldest_lot: cash 0 + 225 (here: unsettled, T+1), KO-1 gone, KO-2
    # half
    acct = desk.write("ko.json", {"settled_cash": "0", "reserved_cash": "0", "orders": [], "positions": [
        {"symbol": "KO", "shares": "3", "lots": [{"id": "KO-1", "date": "2025-01-02", "shares": "2", "cost": "120"},
                                                 {"id": "KO-2", "date": "2026-01-02", "shares": "1", "cost": "70"}]}]})
    desk("ticket", "fill", "--symbol", "KO", "--side", "sell", "--qty", "2.5", "--price", "90", "--date",
         "2026-10-19", "--account", acct, ok=True)
    pf = json.loads(pathlib.Path(acct).read_text(encoding="utf-8"))
    assert (pf["settled_cash"], pf["unsettled_cash"]) == ("0", "225.00")
    lots = pf["positions"][0]["lots"]
    assert [lot["id"] for lot in lots] == ["KO-2"] and (lots[0]["shares"], lots[0]["cost"]) == ("0.5", "35.00")
    _, text = desk.text("ticket", "log")
    assert "filled" in text and "SELL 2.5 KO at 90.00" in text
    _, out = desk("ticket", "fill", "--symbol", "KO", "--side", "sell", "--qty", "5", "--price", "90", "--account",
                  acct, ok=False)
    assert "cannot record selling 5 KO" in out["failure"]["message"]


def test_apply_fill_by_hand():
    acct = {"settled_cash": "5000", "reserved_cash": "0", "positions": [], "orders": []}
    pf = tickets.apply_fill(acct, "tsla", "buy", "8", "364.20", "2026-10-19")
    assert pf["settled_cash"] == "2086.40" and pf["positions"] == [
        {"symbol": "TSLA", "shares": "8", "lots": [{"id": "TSLA-1", "date": "2026-10-19", "shares": "8",
                                                    "cost": "2913.60"}]}]
    assert acct["positions"] == []                                        # the input is not changed
    pf = tickets.apply_fill(pf, "TSLA", "sell", "8", "366.10", "2026-10-19")
    assert (pf["settled_cash"], pf["unsettled_cash"], pf["positions"]) == ("2086.40", "2928.80", [])
    for bad in (("0", "1"), ("1", "0"), ("NaN", "1"), ("1", "-2")):
        with pytest.raises(TraderOSError):
            tickets.apply_fill(acct, "KO", "buy", *bad, "2026-10-19")
    assert tickets.log_fill("ko", "sell", D("2.5"), "90") == {"status": "filled", "symbol": "KO", "side": "sell",
                                                              "qty": "2.5", "price": "90.00",
                                                              "plain": "SELL 2.5 KO at 90.00"}


# ==== 8. the keys are never printed, journalled, stored or exported ===============================================
def test_secrets_never_appear_anywhere(desk, monkeypatch):
    fake = install(monkeypatch, now=NOW, keys=False)
    desk("config", "set", "alpaca_paper_key_id", KEY_ID, now=None, ok=True)
    desk("config", "set", "alpaca_paper_secret_key", SECRET, now=None, ok=True)
    settings = desk.home / "config" / "settings.json"
    assert settings.stat().st_mode & 0o777 == 0o600
    _, out = desk("config", "show", now=None, ok=True)
    assert out["result"] == {"alpaca_paper_key_id": "set (...6789)", "alpaca_paper_secret_key": "set (hidden)"}
    desk.text("config", "show", now=None)
    desk.text("config", "set", "alpaca_paper_key_id", KEY_ID, now=None)          # the words after saving, too
    desk.text("config", "set", "alpaca_paper_secret_key", SECRET, now=None)
    desk("doctor", now=None, ok=True)
    desk.text("doctor", now=None)
    desk("ticket", "account", now=None, ok=True)
    desk.text("ticket", "account", now=None)
    pt = desk.approved()
    desk.text("ticket", "show", pt)
    _, out = desk.place(pt, ok=True)
    fake.fill(out["result"]["broker_order_id"], 8, "364.20")
    desk("ticket", "fill", pt, "--account", desk.files["account"], ok=True)
    desk.text("ticket", "log", "--all")
    desk.text("journal", "list")
    desk.text("memory", "search", "paper")
    fake.submit = (403, {"message": f"forbidden for key {KEY_ID} / {SECRET}"})       # even if Alpaca echoes them
    aapl = desk.write("aapl.json", {"symbol": "AAPL", "direction": "long", "date": "2026-10-19", "entry": "50.00",
                                    "stop": "49.00", "target": "52.00"})
    pt2 = desk.approved(now="2026-10-19T14:15:30Z", plan=aapl)
    _, out = desk.place(pt2, ok=False)
    assert "****" in out["failure"]["message"]
    # the keys went to Alpaca in its two headers, and nowhere else
    assert {c[2]["APCA-API-KEY-ID"] for c in fake.calls} == {KEY_ID}
    assert {c[2]["APCA-API-SECRET-KEY"] for c in fake.calls} == {SECRET}
    assert not any(KEY_ID.encode() in (c[3] or b"") or SECRET.encode() in (c[3] or b"") for c in fake.calls)
    printed = "\n".join(desk.seen)
    files = [p for p in desk.tmp.rglob("*") if p.is_file() and p != settings]
    stored = b"".join(p.read_bytes() for p in files)
    with sqlite3.connect(desk.home / "registry.sqlite") as db:
        dump = "\n".join(db.iterdump())
    for secret in (KEY_ID, SECRET, KEY_ID[2:14], SECRET[6:20]):
        assert secret not in printed, secret
        assert secret.encode() not in stored, secret
        assert secret not in dump, secret
    assert files and any(p.name == "registry.sqlite" for p in files)


def test_config_set_never_echoes_the_value_it_saved(tmp_path, capsys):
    """OSDev1 (wall, 19:22): `config set` said "It is never printed: <value>" and printed it. A secret is shown only
    masked; any other setting (an SEC identification is someone's email address) is not echoed at all."""
    from traderos import settings as st
    samples = {"sec_user_agent": "Research Desk desk@example.org"}
    for key in st.KEYS:
        value = samples.get(key, "PKTESTVALUE0123456789")
        assert cli.main(["--home", str(tmp_path / "h"), "config", "set", key, value]) == 0
        out = capsys.readouterr().out
        assert out.startswith(f"Saved {key} in ") and value not in out and "never printed" not in out
        assert ("Shown masked: " in out) is (key in st.SECRETS)


def alpaca_fake_account():
    from tests.unit.alpaca_fake import ACCOUNT
    return dict(ACCOUNT)


def urlhost(url):
    import urllib.parse
    return urllib.parse.urlsplit(url).hostname




# ==== the review of #10, second round (OSDev6, comment 7050923565; ADR-009) =========================================
CONTRACTS = {"symbol": "ABC", "direction": "long", "date": "2026-10-19", "entry": "1.00", "stop": "0.99",
             "target": "1.05", "multiplier": "100"}


def test_a_holding_in_contracts_is_never_flattened(desk, alpaca):
    # MEDIUM: flatten read only the plan's multiplier (a flatten has none), so 10 contracts of 100 became a paper sell
    # of 10 shares, and its fill would book $500 of a $50,000 holding. Refused, with the reason and the way out.
    held = desk.write("xyz.json", {"settled_cash": "20000", "reserved_cash": "0", "orders": [],
                                   "positions": [{"symbol": "XYZ", "shares": "10", "multiplier": "100"}]})
    for extra in ((), ("--qty", "10")):
        _, out = desk("ticket", "flatten", "XYZ", *extra, "--policy", desk.files["policy"], "--account", held,
                      ok=False)
        assert out["failure"]["code"] == "OUTSIDE_CAPABILITY"
        assert "contract multiplier of 100" in out["failure"]["message"] and "ticket fill" in out["failure"]["message"]
    _, out = desk("ticket", "log", "--all", ok=True)
    assert out["result"] == [] and alpaca.calls == []                       # no ticket, nothing sent
    # a holding stated at 1 flattens as before
    one = desk.write("one.json", {"settled_cash": "0", "reserved_cash": "0", "orders": [],
                                  "positions": [{"symbol": "XYZ", "shares": "10", "multiplier": "1"}]})
    desk("ticket", "flatten", "XYZ", "--policy", desk.files["policy"], "--account", one, ok=True)


def test_a_result_needs_the_account_only_for_a_fill(desk):
    # #16's review: `ticket result --status placed --filled-qty --filled-price` without --account crashed (exit 4)
    pt = desk.approved()
    _, out = desk("ticket", "result", pt, "--status", "placed", "--filled-qty", "8", "--filled-price", "364.20",
                  now="2026-10-19T14:16:00Z", ok=False)
    assert out["failure"]["code"] == "INVALID_INPUT" and "--account FILE" in out["failure"]["message"]
    _, text = desk.text("ticket", "show", pt)
    assert "APPROVED by you" in text                                       # nothing was recorded
    _, out = desk("ticket", "result", pt, "--status", "placed", "--broker-id", "abc123", now="2026-10-19T14:16:00Z",
                  ok=True)                                                  # no fill: no account needed
    assert out["result"]["status"] == "placed"
    _, text = desk.text("ticket", "show", pt)
    assert "DONE: placed" in text


def test_a_contract_ticket_never_says_it_will_be_sent(desk, alpaca):
    # LOW: `place` refuses a ticket sized in contracts (item 2), so its ticket and its yes must not say it is sent
    _, text = desk.text("ticket", "show", desk.ticket())                   # an ordinary ticket shows what is sent
    assert "TraderOS will send exactly this to Alpaca's paper endpoint" in text and "will not send" not in text
    pt = desk.ticket(now="2026-10-19T14:15:30Z", plan=desk.write("contracts.json", CONTRACTS))
    _, text = desk.text("ticket", "show", pt, now="2026-10-19T14:15:30Z")
    assert "TraderOS will not send this: it is sized in contracts of 100" in text
    assert "will send exactly" not in text and "sends it" not in text
    code, text = desk.text("ticket", "approve", pt, "--reply", "yes", now="2026-10-19T14:15:30Z")
    assert code == 0 and "TraderOS will not send it: it is sized in contracts of 100" in text
    assert "will send exactly" not in text


def test_a_fill_keeps_a_contract_tickets_multiplier(desk):
    # an untested fix of the first round: a contract ticket's fill (recorded by hand: place refuses it) costs
    # qty x price x multiplier, and the holding keeps its multiplier, so the check values it as the plan
    pt = desk.approved(plan=desk.write("contracts.json", CONTRACTS))
    desk("ticket", "result", pt, "--status", "placed", "--filled-qty", "2", "--filled-price", "1.00",
         "--account", desk.files["account"], now="2026-10-19T14:16:00Z", ok=True)
    acct = json.loads(pathlib.Path(desk.files["account"]).read_text(encoding="utf-8"))
    (pos,) = acct["positions"]
    assert (acct["settled_cash"], pos["symbol"], pos["shares"], pos["multiplier"]) == ("4800.00", "ABC", "2", "100")


def test_an_unknown_setting_is_refused_on_read(desk, monkeypatch):
    # LOW, from before the diff: a hand-added key (a base_url) was kept and silently ignored. Refused, named, and the
    # value never printed; doctor still reports, and place refuses before any request.
    fake = install(monkeypatch, now=NOW, keys=False)
    path = desk.home / "config" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stray = "https://paper-api.alpaca.markets/stray"
    path.write_text(json.dumps({"alpaca_paper_key_id": KEY_ID, "alpaca_paper_secret_key": SECRET,
                                "base_url": stray}), encoding="utf-8")
    _, out = desk("config", "show", now=None, ok=False)
    assert out["failure"]["code"] == "INTEGRITY_MISMATCH"
    assert "unknown setting(s) 'base_url'" in out["failure"]["message"]
    _, out = desk("doctor", now=None, ok=False)               # a failing doctor exits 3 and names the check (#22)
    assert out["failure"]["code"] == "INTEGRITY_MISMATCH"
    paper = out["failure"]["details"]["alpaca paper"]
    assert paper.startswith("FAIL") and "'base_url'" in paper
    pt = desk.approved()
    _, out = desk.place(pt, ok=False)
    assert "'base_url'" in out["failure"]["message"] and fake.calls == []
    printed = "\n".join(desk.seen)
    assert stray not in printed and KEY_ID not in printed and SECRET not in printed


def test_doctor_fails_on_a_live_setting_with_the_adapters_own_code(tmp_path, capsys, monkeypatch):
    """After #22, a failing doctor exits 3; a paper-check FAIL carries the paper adapter's own refusal code, and no keys
    at all is not a failure (they are needed only for `ticket place`)."""
    from traderos.adapters import alpaca_paper
    from traderos.contracts import TraderOSError
    home = str(tmp_path / "h")
    assert cli.main(["--home", home, "init"]) == 0
    capsys.readouterr()
    assert cli.main(["--json", "--home", home, "doctor"]) == 0
    assert "paper keys not set" in json.dumps(json.loads(capsys.readouterr().out)["result"])
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "false")
    with pytest.raises(TraderOSError) as refused:
        alpaca_paper.check_environment()
    assert cli.main(["--json", "--home", home, "doctor"]) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == refused.value.code.value and failure["details"]["alpaca paper"].startswith("FAIL")
