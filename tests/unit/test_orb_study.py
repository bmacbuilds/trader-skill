"""The upstream backtest tests, ported with the ORB study (ADR-010).

Source: tests/test_backtest.py from upstream PR #53 at 16133276 (MIT). Its three tests are kept, on its TSLA fixture
as a stored dataset instead of a cache folder; the review's gaps are added (symbol checks, bounds, what a run
records, the research family).
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from tests.unit import tm_fixture as F
from traderos import cli
from traderos.contracts import ErrorCode
from traderos.store import Store
from traderos.timemachine import orb_study as bt
from traderos.timemachine import plan as tm

READER, DS = F.reader()
NOW = tm.parse_now("2026-10-19 10:50")


def run_doc(**kw):
    kw.setdefault("draws", 200)
    return bt.run(READER, DS, NOW, **kw)


def test_the_trades_are_the_time_machines_history():
    doc = run_doc(until="16:00")
    assert (doc["stocks"], doc["until"]) == (1, "16:00")
    old = next(v for v in doc["variants"] if v["label"].endswith("(old)"))
    assert old["trades"] == 33                       # the reel's 33, every one with a blind control
    assert round(old["avg_R"], 3) == -0.062
    new = doc["variants"][0]
    assert (new["label"], new["trades"]) == ("1R, next open, 2 bps", 33)
    assert round(new["avg_R"], 3) == -0.113
    for v in doc["variants"]:
        assert v["R_ci"][0] <= v["avg_R"] <= v["R_ci"][1]
        assert v["edge_R"] == pytest.approx(v["avg_R"] - v["control_R"], abs=1e-9)
        assert v["first_half"]["trades"] + v["second_half"]["trades"] == v["trades"]
    # each trade is the Time Machine's own history: the same function, the same numbers
    sessions = bt.load_sessions(READER, DS, NOW, tm.XNYS())[1]["TSLA"]
    h = tm.history_replay(sessions, {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0}, 77)
    assert new["avg_R"] == pytest.approx(h["avg_R"], abs=1e-12)


def test_the_text_report_and_the_universe():
    text = "\n".join(bt.describe(run_doc(draws=100)))
    for line in ("on 1 stocks of TSLA 5m test fixture (rights unverified)", "resampling whole days",
                 "breakout adds", "not a verdict", "Research family RF-orb-15m-or_mid-long: these 4 variants were "
                 "tried together"):
        assert line in text
    assert "S&P 500" not in text                     # flaw: the words were hard-coded whatever the universe
    m = READER.manifest(DS)
    assert bt.universe(m) == ([("TSLA", "INS-TSLA-UNKNOWN-USD")], [])
    assert bt.universe(m, ["AAPL", '"brk.b"', "AAPL", "tsla"]) == ([("TSLA", "INS-TSLA-UNKNOWN-USD")],
                                                                   ["AAPL", "BRK.B"])


def test_no_usable_symbol_is_one_plain_refusal():
    with pytest.raises(tm.TMError) as e:
        run_doc(symbols=["AAPL"])
    assert e.value.code is ErrorCode.INSUFFICIENT_EVIDENCE and "20 or more full sessions" in e.value.message
    with pytest.raises(tm.TMError):
        bt.run(READER, DS, tm.parse_now("2026-08-01 10:00"))


def test_a_symbol_is_checked_and_counted_once():
    # flaw: "../ESCAPED" reached a cache path, and two spellings of one file counted its trades twice
    with pytest.raises(tm.TMError) as e:
        run_doc(symbols=["TSLA", "../TSLA"])
    assert e.value.code is ErrorCode.INVALID_INPUT
    doc = run_doc(symbols=["TSLA", "tsla", " TSLA "])
    assert doc["stocks"] == 1 and doc["variants"][0]["trades"] == 33


def test_until_and_draws_are_checked():
    for bad in ("15:57", "9:45", "16:05", "noon"):
        with pytest.raises(tm.TMError):
            run_doc(until=bad)
    for bad in (0, -1, bt.MAX_DRAWS + 1, 2.5, True):
        with pytest.raises(tm.TMError):
            run_doc(draws=bad)


def test_a_run_records_what_produced_it():
    doc = run_doc()
    assert doc["dataset_id"] == DS and doc["dataset_hash"] == READER.manifest(DS).canonical_hash
    assert (doc["as_of"], doc["seed"], doc["draws"], doc["variants_tried"]) == ("2026-10-19T14:50:00Z", 19, 200, 4)
    assert doc["code_version"].startswith("traderos ") and "orb_study/1 sha256" in doc["code_version"]
    assert "SELECTION_BIAS" in doc["limitations"]
    assert run_doc() == doc                           # same data, same seed: the same numbers


def test_the_variants_are_one_research_family(tmp_path):
    store = Store(tmp_path / "home").init()
    manifest, bars = F.dataset()
    store.put_dataset(manifest, bars)
    body, _doc = bt.study(store, DS, now_utc=NOW, draws=50)
    assert (body["research_family_id"], body["variants_tried"], body["earlier_runs"]) == (bt.FAMILY, 4, 0)
    assert all(isinstance(v["avg_R"], str) for v in body["variants"] if v["trades"])   # decimals, never floats
    again, _ = bt.study(store, DS, now_utc=tm.parse_now("2026-10-19 11:00"), draws=50)
    assert again["earlier_runs"] == 0                 # the same run again is a replay, not a new member
    other, odoc = bt.study(store, DS, now_utc=NOW, draws=50, until="15:00")
    assert other["earlier_runs"] == 1
    assert "1 earlier run(s) of this family" in "\n".join(bt.describe(odoc))
    events = store.search_memory(bt.FAMILY, kinds=("orb_study.completed",))
    assert len(events) == 2 and all("one of 4 tried" in e.summary for e in events)
    assert all(DS in e.subject_refs for e in events)


def test_the_cli(tmp_path):
    (tmp_path / "tsla.csv").write_bytes(F.yahoo_csv())
    home = str(tmp_path / "home")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert cli.main(["--home", home, "init"]) == 0
        assert cli.main(["--json", "--home", home, "data", "import", str(tmp_path / "tsla.csv"), "--interval", "5m",
                         "--name", "tsla5"]) == 0
        ds = json.loads(out.getvalue().splitlines()[-1])["result"]["dataset_id"]
        assert cli.main(["--home", home, "timemachine", "orb-study", "--dataset", ds, "--as-of",
                         "2026-10-19 10:50", "--draws", "50"]) == 0
        assert cli.main(["--home", home, "timemachine", "orb-study", "--dataset", ds, "--symbols", "../X"]) == 3
        assert cli.main(["--home", home, "timemachine", "orb-study", "--dataset", ds, "--replays", "5"]) == 3
    assert "on 1 stocks of tsla5" in out.getvalue()
