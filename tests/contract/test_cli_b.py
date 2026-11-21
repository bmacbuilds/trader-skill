"""Part B through the frozen CLI (docs/cli-contract-v1.md): market, scan, entry, exit, alerts check, on the tutorial.

What would have to break for this to go red:
* a part B command answers while its capability is not usable in this build (spec 01.2);
* a refusal exits anything but 3 with its typed code, or a result is not the typed record;
* tutorial results lose their TUTORIAL_ONLY label, or Market Pulse cannot name a regime on the tutorial;
* a second alert check hides the window it missed, or an event is not kept in the store;
* --dataset is guessed when the store holds more than one.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from traderos import cli
from traderos.capabilities import Capabilities, registry
from traderos.clock import XNYS
from traderos.contracts import CapabilityId, UserCapability, with_hash
from traderos.contracts.intel import AlertEvent
from traderos.contracts.market import Interval
from traderos.intelligence._common import expected_last_end
from traderos.store import Store

CRITERIA = {"schema_version": "scan-criteria/1", "criteria_id": "CR-1", "name": "above the 200-day average",
            "interval": "1d", "universe": ["TUTMKT", "TUTGLD", "NOPE"],
            "conditions": {"above": ["close", {"sma": 200}]}}
RULE = {"schema_version": "alert-rule/1", "rule_id": "AR-1", "instrument_id": "TUTMKT", "symbol": "TUTMKT",
        "interval": "1d", "conditions": {"above": ["close", "100"]}, "sampling_seconds": 3600,
        "max_staleness_seconds": 100000, "missed_policy": "report_missed", "created_at": "2025-12-01T00:00:00Z"}


@pytest.fixture
def home(tmp_path, capsys):
    h = str(tmp_path / "home")
    assert cli.main(["--home", h, "init"]) == 0
    capsys.readouterr()
    return h


def run(capsys, *argv):
    code = cli.main(["--json", *argv])
    return code, json.loads(capsys.readouterr().out)


def test_market_on_the_tutorial(home, capsys):
    code, out = run(capsys, "--home", home, "market")
    assert code == 0 and out["ok"]
    mc = out["result"]
    assert mc["schema_version"] == "market-context/1" and mc["safe_for"] == ["tutorial_only"]
    assert mc["regime"]["label"] in ("uptrend", "downtrend", "range")             # TUTMKT is the benchmark
    assert mc["breadth"]["value"] is None and len(mc["moves"]) == 7


def test_scan_with_a_criteria_file(home, tmp_path, capsys):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(CRITERIA))
    code, out = run(capsys, "--home", home, "scan", "--criteria", str(f), "--as-of", "2025-12-31T22:00:00Z")
    assert code == 0
    by = {c["symbol"]: c for c in out["result"]["candidates"]}
    assert by["NOPE"]["eligibility"] == "unavailable" and by["NOPE"]["reasons"] == ["MISSING_DATA"]
    assert by["TUTMKT"]["traces"][0]["left"]["unit"] == "USD"
    f.write_text(json.dumps({**CRITERIA, "conditions": {"above": ["close", 1.5]}}))
    code, out = run(capsys, "--home", home, "scan", "--criteria", str(f))
    assert code == 3 and out["failure"]["code"] == "INVALID_INPUT" and "float" in out["failure"]["message"]


def test_entry_and_exit(home, capsys):
    code, out = run(capsys, "--home", home, "entry", "TUTMKT", "--as-of", "2025-12-27T15:00:00Z")   # a Saturday
    assert code == 0 and out["result"]["status"] == "not_eligible" and out["result"]["session_open"] is False
    for extra in (["--setup", "orb"], ["--level", "100"], ["--setup", "level", "--level", "1.5e2"]):
        code, out = run(capsys, "--home", home, "entry", "TUTMKT", *extra)       # daily bars; flags need --setup
        assert code == 3 and out["failure"]["code"] == "INVALID_INPUT", extra
    code, out = run(capsys, "--home", home, "exit", "TUTMKT", "--direction", "long", "--entry", "100", "--stop", "95")
    assert code == 0 and out["result"]["direction"] == "long"
    assert {s["name"] for s in out["result"]["scenarios"]} >= {"stop", "exit at the last close", "one ATR against"}
    code, out = run(capsys, "--home", home, "exit", "TUTMKT", "--direction", "long", "--entry", "100",
                    "--stop", "pct:2", "--target", "2R")                                # The upstream rules on any bars
    by = {s["name"]: s for s in out["result"]["scenarios"]}
    assert code == 0 and by["stop"]["price"]["value"] == "98" and by["target"]["price"]["value"] == "104"
    assert "time exit" in by
    for extra in (["--stop", "or-mid"], ["--stop", "1e2"], ["--stop", "95", "--flat-by", "15:00"]):
        code, out = run(capsys, "--home", home, "exit", "TUTMKT", "--direction", "long", "--entry", "100", *extra)
        assert code == 3 and out["failure"]["code"] == "INVALID_INPUT", extra    # daily bars; not a rule; no rule
    code, out = run(capsys, "--home", home, "exit", "TUTMKT", "--direction", "short", "--entry", "100",
                    "--stop", "95")
    assert code == 3 and out["failure"]["code"] == "INVALID_INPUT"


def test_alerts_report_the_missed_window_across_runs(home, tmp_path, capsys):
    (tut,) = Store(home).datasets()
    mkt = next(i for i in tut.instruments if i.symbol == "TUTMKT")
    rule = {**RULE, "instrument_id": mkt.instrument_id}                         # an id, never a ticker
    f = tmp_path / "rules.json"
    f.write_text(json.dumps([{**RULE, "instrument_id": "TUTMKT"}]))
    code, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f))
    assert code == 3 and out["failure"]["code"] == "MISSING_DATA"
    f.write_text(json.dumps([rule]))
    code, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f), "--at", "2025-12-31T16:00:00Z")
    assert code == 0 and out["result"][0]["triggered"] is not None and out["result"][0]["missed"] is False
    code, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f), "--at", "2025-12-31T20:00:00Z")
    (ev,) = out["result"]
    assert ev["missed"] is True and ev["previous_evaluated_at"] == "2025-12-31T16:00:00Z"
    assert len(Store(home).all(AlertEvent)) == 2
    code, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f), "--at", "2025-12-31T20:30:00Z")
    assert out["result"][0]["previous_evaluated_at"] == "2025-12-31T20:00:00Z"      # the newest, not the first
    store = Store(home)                                      # the same rule on another dataset never counts
    last = store.latest(AlertEvent, where={"rule_id": rule["rule_id"]}, order_by="evaluated_at")
    store.put(with_hash(dataclasses.replace(last, event_id="AE-OTHER-DATASET", dataset_id="DS-ELSEWHERE",
                                            evaluated_at=datetime(2025, 12, 31, 20, 45, tzinfo=UTC),
                                            canonical_hash=None)))
    with pytest.MonkeyPatch.context() as mp:                 # one query for the last evaluation, never a full scan
        mp.setattr(Store, "all", lambda *a, **k: pytest.fail("alerts check read every stored event"))
        code, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f), "--at", "2025-12-31T21:00:00Z")
    assert code == 0 and out["result"][0]["previous_evaluated_at"] == "2025-12-31T20:30:00Z"
    code, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f), "--at", "2025-12-31T16:30:00Z")
    assert out["result"][0]["previous_evaluated_at"] == "2025-12-31T16:00:00Z"      # only evaluations before --at
    f.write_text(json.dumps([rule, rule]))
    assert run(capsys, "--home", home, "alerts", "check", "--rules", str(f))[0] == 3     # two rules, one id


def test_alerts_rerun_after_an_edit_and_a_refused_rule_stores_nothing(home, tmp_path, capsys):
    (tut,) = Store(home).datasets()
    mkt = next(i for i in tut.instruments if i.symbol == "TUTMKT")
    rule = {**RULE, "instrument_id": mkt.instrument_id}
    f = tmp_path / "rules.json"
    at = ("--at", "2025-12-31T16:00:00Z")
    for edited in (rule, {**rule, "conditions": {"above": ["close", "101"]}}, {**rule, "active": False}):
        f.write_text(json.dumps([edited]))                  # the same instant, re-checked after each edit
        code, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f), *at)
        assert code == 0, out
    stored = len(Store(home).all(AlertEvent))
    second = {**rule, "rule_id": "AR-2", "interval": "5m"}  # refused: the dataset holds daily bars
    f.write_text(json.dumps([{**rule, "rule_id": "AR-3"}, second]))
    code, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f), *at)
    assert code == 3 and len(Store(home).all(AlertEvent)) == stored          # AR-3's event was not kept either
    # A store write that fails halfway keeps none of the run's events (one transaction).
    f.write_text(json.dumps([{**rule, "rule_id": "AR-4"}, {**rule, "rule_id": "AR-5"}]))
    real, calls = Store.put, []

    def fail_second(self, record, **kw):
        calls.append(record)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(self, record, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Store, "put", fail_second)
        assert cli.main(["--home", home, "alerts", "check", "--rules", str(f), *at]) == 4
    capsys.readouterr()
    assert len(Store(home).all(AlertEvent)) == stored


def test_a_duplicate_key_is_refused_even_when_both_values_agree(home, tmp_path, capsys):
    (tut,) = Store(home).datasets()
    mkt = next(i for i in tut.instruments if i.symbol == "TUTMKT")
    f = tmp_path / "rules.json"
    f.write_text(json.dumps([{**RULE, "instrument_id": mkt.instrument_id}])[:-2] + ', "rule_id": "AR-1"}]')
    code, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f), "--at", "2025-12-31T16:00:00Z")
    assert code == 3 and "duplicate key" in out["failure"]["message"]


@pytest.mark.parametrize(("text", "code"), [
    ('{"a": 1, "a": 2}', "INVALID_INPUT"),                  # a duplicate key
    ("[" * 100_000 + "]" * 100_000, "INVALID_INPUT"),       # nested past Python's recursion limit
    ("[" + "9" * 5000 + "]", "INVALID_INPUT"),              # an integer past Python's 4300-digit limit
    (" " * (1024 * 1024 + 1), "RESOURCE_BUDGET_EXCEEDED"),  # over the 1 MB file cap
])
def test_a_hostile_rules_file_exits_3_not_4(home, tmp_path, capsys, text, code):
    f = tmp_path / "rules.json"
    f.write_text(text)
    rc, out = run(capsys, "--home", home, "alerts", "check", "--rules", str(f))
    assert rc == 3 and out["failure"]["code"] == code


def test_a_second_dataset_is_never_guessed(home, tmp_path, capsys):
    csv = tmp_path / "mine.csv"
    csv.write_text("symbol,timestamp_utc,open,high,low,close,volume\n"
                   "ABC,2025-12-29T21:00:00Z,10,11,9,10.5,1000\nABC,2025-12-30T21:00:00Z,10.5,11,10,10.8,1200\n")
    assert cli.main(["--home", home, "data", "import", str(csv)]) == 0
    capsys.readouterr()
    code, out = run(capsys, "--home", home, "market")
    assert code == 3 and "several datasets" in out["failure"]["message"]


def test_every_part_b_command_is_gated_by_the_registry(home, tmp_path, capsys, monkeypatch):
    off = dataclasses.replace(registry(), entries=tuple(
        dataclasses.replace(e, user_status=UserCapability.UNAVAILABLE, limitations=())
        if e.capability_id in (CapabilityId.S01, CapabilityId.S03, CapabilityId.S13, CapabilityId.S14,
                               CapabilityId.S18) else e for e in registry().entries), canonical_hash=None)
    monkeypatch.setattr("traderos.commands.intel.Capabilities", lambda: Capabilities(off))
    f = tmp_path / "x.json"
    f.write_text("{}")
    for argv in (["market"], ["scan", "--criteria", str(f)], ["entry", "TUTMKT"],
                 ["exit", "TUTMKT", "--direction", "long", "--entry", "1"], ["alerts", "check", "--rules", str(f)]):
        code, out = run(capsys, "--home", home, *argv)
        assert code == 3 and out["failure"]["code"] == "CAPABILITY_DISABLED", argv


def test_part_b_is_experimental_never_verified():
    caps = Capabilities()
    for cid in (CapabilityId.S01, CapabilityId.S03, CapabilityId.S13, CapabilityId.S14, CapabilityId.S18):
        e = caps.entry(cid)
        assert e.user_status is UserCapability.EXPERIMENTAL and "independent review" in e.next_gate
        assert any(lim.code == "EXPERIMENTAL" for lim in e.limitations)


def test_the_last_covered_day_does_not_step_outside_the_calendar():
    # 2027-12-31 is XNYS's last covered day: the newest close is that day's, found without asking for 2028-01-01
    got = expected_last_end(XNYS(), Interval.D1, datetime(2027, 12, 31, 22, tzinfo=UTC))
    assert got == XNYS().open_close(datetime(2027, 12, 31).date())[1]


def test_upstream_alerts_ring_once_and_come_from_a_plan(home, tmp_path, capsys):
    (tut,) = Store(home).datasets()
    mkt = next(i for i in tut.instruments if i.symbol == "TUTMKT").instrument_id
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"id": "bell", "type": "opening_bell"},
                                 {"id": "up", "instrument_id": mkt, "symbol": "TUTMKT", "type": "above",
                                  "price": "0.01"}]))
    ring = ("--home", home, "alerts", "ring", "--rules", str(rules), "--at", "2025-12-31T15:00:00Z")
    code, out = run(capsys, *ring)
    assert code == 0 and [e["schema_version"] for e in out["result"]] == ["alert-event/1"] * 2
    assert [e["reasons"][0][:6] for e in out["result"]] == ["RANG: "] * 2
    assert "tutorial_only" in out["result"][1]["safe_for"]                      # the tutorial's label carries
    code, out = run(capsys, *ring)
    assert code == 0 and not any(r.startswith("RANG") for e in out["result"] for r in e["reasons"])
    code, out = run(capsys, "--home", home, "alerts", "reset", "up")
    assert code == 0 and out["result"] == {"reset": ["up"], "not_rung": []}
    code, out = run(capsys, *ring)
    assert [e["rule_id"] for e in out["result"] if e["reasons"][0].startswith("RANG")] == ["up"]
    for bad in ('[{"id": "x", "instrument_id": "I", "type": "above", "price": 1.5}]',     # a float price
                '[{"id": "x", "symbol": "TUTMKT", "type": "above", "price": "1"}]'):    # a ticker alone
        rules.write_text(bad)
        code, out = run(capsys, *ring)
        assert code == 3 and out["failure"]["code"] == "INVALID_INPUT"
    code, out = run(capsys, "--home", home, "exit", "TUTMKT", "--direction", "long", "--entry", "100", "--stop",
                    "pct:2", "--target", "2R", "--as-of", "2025-12-31T15:00:00Z")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(out))                                               # the whole --json envelope
    code, out = run(capsys, "--home", home, "alerts", "ring", "--from-plan", str(plan), "--at",
                    "2025-12-31T21:00:00Z", "--state", str(tmp_path / "s.json"))
    assert code == 0 and any(e["rule_id"].endswith("-flat-by") and e["reasons"][0].startswith("RANG")
                             for e in out["result"])


def test_an_alert_that_cannot_be_checked_says_unknown(home, tmp_path, capsys):
    (tut,) = Store(home).datasets()
    mkt = next(i for i in tut.instruments if i.symbol == "TUTMKT").instrument_id
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"id": "s", "instrument_id": mkt, "type": "stop", "price": "1"}]))
    later = "2026-03-02T15:00:00Z"                                                 # months after the tutorial ends
    assert cli.main(["--home", home, "alerts", "ring", "--rules", str(rules), "--at", later]) == 0
    text = capsys.readouterr().out
    assert "UNKNOWN s: could not be checked (DATA_STALE" in text and "  Nothing rang." not in text
    state = tmp_path / "bad.json"
    state.write_text('{"rung": {"x": 5}}')
    code, out = run(capsys, "--home", home, "alerts", "ring", "--rules", str(rules), "--state", str(state))
    assert code == 3 and out["failure"]["code"] == "INVALID_INPUT"


def test_scan_until_and_exit_entry_at_reach_the_api(home, capsys):
    code, out = run(capsys, "--home", home, "scan", "--setups", "orb", "--until", "9pm")
    assert code == 3 and out["failure"]["code"] == "INVALID_INPUT"                # daily data, and a bad time
    code, out = run(capsys, "--home", home, "exit", "TUTMKT", "--direction", "long", "--entry", "100",
                    "--stop", "95", "--entry-at", "2025-12-31T15:00:00Z")
    assert code == 3 and "go with a stop or target rule" in out["failure"]["message"]


def test_the_orb_expiry_defaults_to_1530_in_the_api_and_the_cli():
    import argparse
    import inspect

    from traderos import api
    from traderos.commands import intel
    assert inspect.signature(api.scan_setups).parameters["until"].default == "15:30"
    parser = argparse.ArgumentParser()
    intel.register(parser.add_subparsers(dest="command"))
    assert parser.parse_args(["scan", "--setups", "orb"]).until == "15:30"


def test_the_exit_text_never_prints_a_pnl_or_a_return(home, capsys):
    """The upstream Exit Planner rule: "It never prints a P&L or a return." The record keeps contract v1's pnl_per_unit;
    the text says what it is: each scenario's difference from the entry, per unit."""
    for extra in (["--stop", "95"], ["--stop", "pct:2", "--target", "2R"]):
        capsys.readouterr()
        code = cli.main(["--home", home, "exit", "TUTMKT", "--direction", "long", "--entry", "100", *extra])
        text = capsys.readouterr().out
        assert code == 0 and "vs the entry, per unit" in text, extra
        assert "P&L" not in text and "pnl" not in text.lower() and " return" not in text.lower(), text
