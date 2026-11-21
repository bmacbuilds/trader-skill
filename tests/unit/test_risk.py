"""The adapter: the upstream sizing and check as TraderOS records (S15, S16, S17-F1, I05; ADR-009).

Every expected number is worked by hand in the comments. The reel: TSLA, Monday 2026-10-19 at 10:15 New York, entry
364.18, stop 358.32, target 370.04, the upstream example policy ($5,000, 10% buffer, $50 max loss).
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from tests.unit.quotes import quote_file
from traderos import api, cli, risk
from traderos.contracts import (
    DataError,
    Direction,
    ErrorCode,
    ExposureStatus,
    NoTradeRecord,
    RiskPlan,
    TraderOSError,
    ValidationError,
)
from traderos.risk import inputs
from traderos.risk.policy import EXAMPLE

NOW = datetime(2026, 10, 19, 14, 15, tzinfo=UTC)          # 10:15 New York


def plan(**kw) -> RiskPlan:
    base = dict(plan_id="RP-1", instrument_id="INS-TSLA", symbol="TSLA", direction=Direction.LONG,
                entry_price=D("364.18"), stop_price=D("358.32"), target_price=D("370.04"), plan_date=date(2026, 10, 19),
                as_of=NOW)
    base.update(kw)
    return RiskPlan(**base)


def policy(**over):
    p = dict(EXAMPLE)
    p.update(over)
    return p


def account(**over):
    a = {"settled_cash": "5000", "reserved_cash": "0", "positions": [], "orders": []}
    a.update(over)
    return inputs.account(a)


def quote(sym="TSLA", price="364.18", at=NOW):
    """A fresh quote at the plan's entry: a market entry with no usable price blocks (review item 4)."""
    return inputs.prices({"prices": {sym: price}, "detail": {sym: {"time": at.isoformat()}}})


QUOTE = quote()


def test_the_four_measures_by_hand():
    a = risk.assess(plan(), policy(), account=account(), prices=QUOTE, now=NOW)
    # The upstream limits: $50 / 5.86 = 8; 3,750 / 364.18 = 10; 4,000 / 364.18 = 10; 4,500 / 364.18 = 12 -> 8 (never up)
    assert a.quantity.value == 8
    assert a.planned_loss.value == D("46.88")                   # 5.86 x 8
    assert a.stressed_loss.value == D("190.21")                 # stop gapped 5%: 358.32 x 0.95 = 340.404; 23.776 x 8
    assert a.theoretical_worst_case.value == D("2913.44")       # 364.18 x 8 to zero
    assert a.exposure_status is ExposureStatus.KNOWN and a.ticket_eligible and a.refusal_reasons == ()
    assert a.valid_until == datetime(2026, 10, 19, 14, 30, tzinfo=UTC) and a.policy_hash and a.account_hash
    assert any(lim.code == "SIZE_LIMIT" and "(binds)" in lim.detail for lim in a.limitations)


@pytest.mark.parametrize("change", [dict(stop_price=D("370")), dict(stop_price=D("364.18")),
                                    dict(target_price=D("360")), dict(entry_price=D("0"), stop_price=None,
                                                                      target_price=None),
                                    dict(direction=Direction.SHORT), dict(target_price=None),
                                    dict(plan_date=None), dict(plan_date=date(2026, 10, 16))])
def test_impossible_plans_are_refused_not_repaired(change):
    with pytest.raises(TraderOSError):
        risk.assess(plan(**change), policy(), account=account(), prices=QUOTE, now=NOW)


def test_the_typed_codes_of_a_refused_plan():
    with pytest.raises(ValidationError) as e:
        risk.assess(plan(direction=Direction.SHORT, stop_price=D("370"), target_price=D("350")), policy(), now=NOW)
    assert e.value.code is ErrorCode.OUTSIDE_CAPABILITY             # Upstream sizes long day trades only
    with pytest.raises(ValidationError) as e:
        risk.assess(plan(plan_date=date(2026, 10, 16)), policy(), now=NOW)
    assert e.value.code is ErrorCode.DATA_STALE
    with pytest.raises(DataError) as e:
        risk.assess(plan(), {"budget": "5000"}, now=NOW)
    assert e.value.code is ErrorCode.MISSING_DATA


def test_sizing_is_capped_by_settled_cash_less_reservations_and_says_so():
    # settled 1,500 - reserved 300 = 1,200; minus the 500 buffer = 700 / 364.18 -> 1 share, not 8
    a = risk.assess(plan(), policy(), account=account(settled_cash="1500", reserved_cash="300"), prices=QUOTE,
                    now=NOW)
    assert a.quantity.value == 1
    assert any(lim.code == "SIZE_LIMIT" and lim.detail.startswith("cash") and "(binds)" in lim.detail
               for lim in a.limitations)


def test_no_stop_is_no_protection():
    a = risk.assess(plan(stop_price=None), policy(), account=account(), prices=QUOTE, now=NOW)
    assert a.planned_loss.value is None and a.stressed_loss.value is None and a.quantity.value is None
    assert ErrorCode.NO_PROTECTION.value in a.refusal_reasons and not a.ticket_eligible


def test_unknown_exposure_is_unknown_never_zero():
    a = risk.assess(plan(), policy(), prices=QUOTE, now=NOW)
    assert a.exposure_status is ExposureStatus.UNKNOWN and ErrorCode.UNKNOWN_EXPOSURE.value in a.refusal_reasons
    assert a.account_hash is None and a.quantity.value == 8          # still sized by the policy's limits (O2)
    with pytest.raises(TraderOSError) as e:
        risk.ticket(plan(), a, now=NOW)
    assert e.value.code is ErrorCode.UNKNOWN_EXPOSURE
    # unknown even when the refusal came from the sizing and the check never ran
    far = risk.assess(plan(stop_price=D("300"), target_price=D("500")), policy(), now=NOW)
    assert ErrorCode.UNKNOWN_EXPOSURE.value in far.refusal_reasons and far.quantity.value == 0


def test_a_sizing_refusal_is_remembered_with_its_words():
    a = risk.assess(plan(stop_price=D("300")), policy(), account=account(), prices=QUOTE, now=NOW)
    assert a.refusal_reasons == (ErrorCode.NOT_AUTHORIZED.value,) and a.quantity.value == 0
    nt = risk.no_trade(plan(), a)
    assert isinstance(nt, NoTradeRecord) and "more than your $50 max loss per trade" in nt.rationale


def test_whole_contracts_with_a_multiplier():
    # per contract (5.20 - 4.00) x 100 = 120; $250 / 120 = 2.08 -> 2 contracts; planned 240, worst 5.20 x 100 x 2
    p = plan(symbol="ABC", entry_price=D("5.20"), stop_price=D("4.00"), target_price=D("8"), multiplier=D("100"))
    a = risk.assess(p, policy(max_loss_per_trade_usd="250", max_daily_loss_usd="300"), account=account(),
                    prices=quote("ABC", "5.20"), now=NOW)
    assert a.quantity.value == 2 and a.planned_loss.value == D("240.00")
    assert a.theoretical_worst_case.value == D("1040.00") and a.ticket_eligible


def test_a_fresh_price_above_the_plan_sizes_smaller_and_is_said():
    prices = inputs.prices({"prices": {"TSLA": "365.00"}, "detail": {"TSLA": {"time": "2026-10-19T14:14:00Z"}}})
    ev = risk.evaluate(plan(), policy(), account=account(), prices=prices, now=NOW)
    # 365.00 - 358.32 = 6.68 a share: $50 / 6.68 = 7 shares; reward 5.04 / 6.68 = 0.75 < 1: refused
    assert ev.assessment.quantity.value == 7 and "above the plan's entry" in ev.assessment.input_freshness
    assert ErrorCode.NOT_AUTHORIZED.value in ev.assessment.refusal_reasons
    lim = risk.evaluate(dataclasses.replace(plan(), limit_price=D("364.00")), policy(), account=account(),
                        prices=prices, now=NOW)
    assert lim.assessment.ticket_eligible and "a limit order" in lim.assessment.input_freshness


def test_ticket_is_paper_only_for_the_exact_plan_and_refusals_are_remembered():
    p = plan()
    a = risk.assess(p, policy(), account=account(), prices=QUOTE, now=NOW)
    t = risk.ticket(p, a, now=NOW)
    assert t.quantity == 8 and t.order_type == "market" and t.target_price == D("370.04")
    assert "never places a live order" in t.disclaimer
    with pytest.raises(ValidationError) as e:
        risk.ticket(dataclasses.replace(p, entry_price=D("364")), a, now=NOW)
    assert e.value.code is ErrorCode.INTEGRITY_MISMATCH
    assert risk.no_trade(p, a) is None
    refused = risk.assess(p, policy(), prices=QUOTE, now=NOW)
    nt = risk.no_trade(p, refused)
    assert isinstance(nt, NoTradeRecord) and ErrorCode.UNKNOWN_EXPOSURE.value in nt.rejection_codes
    assert "no account statement" in nt.rationale


def test_the_public_api():
    a = api.assess_hypothetical_plan(plan(), policy(), account={"settled_cash": "5000", "reserved_cash": "0",
                                                                "positions": [], "orders": []},
                                     prices={"prices": {"TSLA": "364.18"}, "detail": {"TSLA": {"time": AT}}},
                                     now=NOW)
    assert a.ticket_eligible and a.quantity.value == 8
    with pytest.raises(ValidationError):
        api.assess_hypothetical_plan(plan(), policy(), account={"cash": "5000"}, now=NOW)


# ---- the files a person writes -------------------------------------------------------------------------------------
def _write(tmp_path, data, name="plan.json"):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(path)


GOOD = {"symbol": "tsla", "direction": "long", "date": "2026-10-19", "entry": "364.18", "stop": "358.32",
        "target": "370.04"}
AT = "2026-10-19T14:15:00Z"


def _assess(tmp_path, home, plan_data=GOOD, policy_data=None, account_data=None, *extra):
    plan_path = _write(tmp_path, plan_data)
    args = ["--json", "--home", home, "risk", "assess", "--plan", plan_path,
            "--policy", _write(tmp_path, policy_data or EXAMPLE, "policy.json"), "--now", AT, *extra]
    if isinstance(plan_data, dict) and "--limit" not in extra:     # a market entry needs a fresh quote (item 4)
        args += ["--prices", quote_file(tmp_path, plan_path, AT, "quote.json")]
    if account_data is not False:
        args += ["--account", _write(tmp_path, account_data or {"settled_cash": "5000", "reserved_cash": "0",
                                                                "positions": [], "orders": []}, "account.json")]
    return cli.main(args)


def test_cli_plan_file_is_strict(tmp_path, capsys):
    home = str(tmp_path / "home")
    assert _assess(tmp_path, home) == 0
    a = json.loads(capsys.readouterr().out)["result"]
    assert a["ticket_eligible"] is True and a["quantity"]["value"] == "8"
    assert _assess(tmp_path, home) == 0                                         # again: a new, equal assessment
    a = json.loads(capsys.readouterr().out)["result"]
    assert cli.main(["--home", home, "ticket", a["assessment_id"], "--now", AT]) == 0
    assert "It never places a live order." in capsys.readouterr().out
    for bad in ('{"symbol": "TSLA", "direction": "long", "entry": 364.18}',
                '{"symbol": "TSLA", "direction": "long", "entry": "1", "colour": "red"}',
                '{"symbol": "TSLA", "symbol": "QQQ", "direction": "long", "entry": "1"}',
                '{"symbol": "TSLA", "direction": "long", "entry": "1", "equity": "25000"}',
                '{"symbol": "TSLA", "direction": "long", "entry": "1", "quantity": "1000"}',
                '{"symbol": "TSLA", "direction": "long", "entry": NaN}'):
        assert cli.main(["--home", home, "risk", "assess", "--plan", _write(tmp_path, bad, "bad.json"),
                         "--policy", _write(tmp_path, EXAMPLE, "policy.json"), "--now", AT]) == 3, bad
    err = capsys.readouterr().err
    assert "moved out of the plan" in err and "quantity -> the size now comes from your policy" in err


def test_cli_policy_and_account_files_are_strict(tmp_path, capsys):
    home = str(tmp_path / "home")
    floats = json.dumps(EXAMPLE).replace('"1.0"', "1.0")
    assert _assess(tmp_path, home, GOOD, floats) == 3
    missing = dict(EXAMPLE)
    missing.pop("max_trades_per_day")
    capsys.readouterr()
    assert _assess(tmp_path, home, GOOD, missing) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == "MISSING_DATA" and "max_trades_per_day" in failure["message"]
    assert _assess(tmp_path, home, GOOD, dict(EXAMPLE, broker="ibkr")) == 3                   # ADR-012
    assert "Interactive Brokers" in json.loads(capsys.readouterr().out)["failure"]["message"]
    assert _assess(tmp_path, home, GOOD, dict(EXAMPLE, paper=False)) == 3
    assert "live trading is refused" in json.loads(capsys.readouterr().out)["failure"]["message"]
    assert _assess(tmp_path, home, GOOD, dict(EXAMPLE, sec_user_agent="x")) == 3
    assert "not a risk policy key" in json.loads(capsys.readouterr().out)["failure"]["message"]
    assert _assess(tmp_path, home, GOOD, None, {"cash": "5000"}) == 3
    capsys.readouterr()
    assert _assess(tmp_path, home, GOOD, None, {"settled_cash": "5000", "reserved_cash": "0", "positions": []}) == 3
    assert "missing orders" in json.loads(capsys.readouterr().out)["failure"]["message"]
    assert _assess(tmp_path, home, GOOD, None, {"settled_cash": "NaN", "reserved_cash": "0", "positions": [],
                                                "orders": []}) == 3
    assert _assess(tmp_path, home, GOOD, None, {"settled_cash": "5000", "reserved_cash": "0", "positions": [],
                                                "orders": [{"symbol": "AAPL", "side": "buy", "status": "approved",
                                                            "date": "2026-10-19"}]}) == 3
    capsys.readouterr()


def test_cli_refusal_leaves_a_no_trade_record_and_a_journal_line(tmp_path, capsys):
    from traderos.store import Store
    home = str(tmp_path / "home")
    assert _assess(tmp_path, home, GOOD, None, False) == 0
    a = json.loads(capsys.readouterr().out)["result"]
    assert a["ticket_eligible"] is False and "UNKNOWN_EXPOSURE" in a["refusal_reasons"]
    assert cli.main(["--home", home, "ticket", a["assessment_id"], "--now", AT]) == 3
    (nt,) = Store(home).all(NoTradeRecord)
    assert "UNKNOWN_EXPOSURE" in nt.rejection_codes
    assert cli.main(["--home", home, "journal", "add", "skip TSLA until exposure is known", "--kind", "decision",
                     "--ref", nt.record_id]) == 0
    capsys.readouterr()
    assert cli.main(["--home", home, "journal", "list"]) == 0
    out = capsys.readouterr().out
    assert "skip TSLA" in out and "refused (UNKNOWN_EXPOSURE)" in out


def test_cli_text_is_upstream_report_and_the_four_measures(tmp_path, capsys):
    home = str(tmp_path / "home")
    plan_path = _write(tmp_path, GOOD)
    assert cli.main(["--home", home, "risk", "assess", "--plan", plan_path, "--policy",
                     _write(tmp_path, EXAMPLE, "policy.json"), "--account",
                     _write(tmp_path, {"settled_cash": "5000", "reserved_cash": "0", "positions": [], "orders": []},
                            "account.json"), "--prices", quote_file(tmp_path, plan_path, AT, "quote.json"),
                     "--now", AT]) == 0
    out = capsys.readouterr().out
    for line in ("Size: 8 shares of TSLA = $2,913.44 at 364.18", "max loss      $50 max loss / 5.86 risk a share",
                 "PASS  #1 buy TSLA 8 shares, entry", "max loss $46.88 (limit $50), reward:risk 1.00 (min 1)",
                 "today before this: 0 of 3 trades, $0 of $150 daily loss", "stressed loss:         190.21 USD",
                 "valid until:           10:30 New York", "traderos ticket RA-"):
        assert line in out, line
    assert "place_stock_order" not in out and "trader-broker" not in out


def test_cli_limit_order(tmp_path, capsys):
    home = str(tmp_path / "home")
    assert _assess(tmp_path, home, GOOD, None, None, "--limit", "364.00") == 0
    a = json.loads(capsys.readouterr().out)["result"]
    assert a["planned_loss"]["value"] == "45.44"                              # 8 x (364.00 - 358.32)
    assert cli.main(["--json", "--home", home, "ticket", a["assessment_id"], "--now", AT]) == 0
    t = json.loads(capsys.readouterr().out)["result"]
    assert (t["order_type"], t["limit_price"], t["target_price"]) == ("limit", "364", "370.04")   # canonical form


# ---- upstream PR #49 in the traderos CLI ---------------------------------------------------------------------------
def _traderos(home, *argv, env=None):
    e = dict(os.environ, PYTHONIOENCODING="cp1252", TRADEROS_HOME=home,
             PYTHONPATH=os.pathsep.join([os.path.join(os.path.dirname(cli.__file__), ".."), *sys.path]))
    e.update(env or {})
    return subprocess.run([sys.executable, "-m", "traderos.cli", *argv], capture_output=True, env=e, timeout=60)


def test_a_character_outside_the_console_code_page_still_prints(tmp_path):
    home = str(tmp_path / "home")
    assert _traderos(home, "journal", "add", "Tesla \U0001F680 to the moon").returncode == 0
    done = _traderos(home, "journal", "list")
    assert done.returncode == 0 and b"\xf0\x9f\x9a\x80" in done.stdout and b"Traceback" not in done.stderr


def test_an_error_outside_the_code_page_is_one_line(tmp_path):
    done = _traderos(str(tmp_path / "home"), "risk", "assess", "--plan", str(tmp_path / "\U0001F680.json"),
                     "--policy", str(tmp_path / "p.json"))
    assert done.returncode == 3 and b"\xf0\x9f\x9a\x80" in done.stderr and b"Traceback" not in done.stderr


def test_main_in_process_under_redirect_stdout(tmp_path):
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main(["--home", str(tmp_path / "home"), "journal", "add", "a \U0001F680 note"]) == 0
    assert "Journal entry" in buf.getvalue()
