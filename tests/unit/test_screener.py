"""S07: the upstream screener tests (tank/trader-skill/tests/test_screener.py), ported, on its fixtures.

Each test keeps its inputs and its expected numbers. What changed in a test, and why:
  * numbers are Decimal;
  * its command line (screen.py FILE --rules R --prices P) is `traderos fundamentals screen --metrics FILE
    --criteria R --prices P`; the result is a fundamental-screen/1 record (no --out file), and a refusal exits 3;
  * BAC's missing operating margin no longer decides its row (ADR-011 change 2): it is `passed: null`, not a fail
    and, with "missing": "skip", not a pass;
  * its undated companies.csv is screened as of today, as its command did; dated in the past it is refused
    (ADR-011 change 4, OSDev6's review of #11, H1);
  * after its tests: a regression test for every flaw ADR-011 names in screen.py, and the facts-records path.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal as D

import pytest

from tests.unit.sec_offline import ANALYST, SCREENER, T0, UA, Offline, client
from traderos import cli
from traderos.adapters import sec
from traderos.contracts import Completeness, ErrorCode, ValidationError, from_json, to_json, verify_hash
from traderos.contracts.fundamentals import FundamentalScreen
from traderos.fundamentals import facts
from traderos.fundamentals import screen as scr

DIR = SCREENER
METRICS = os.path.join(DIR, "metrics.json")
PRICES = os.path.join(DIR, "prices.json")
RULES = os.path.join(DIR, "rules.example.json")


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["--home", os.environ["TRADEROS_HOME"], "fundamentals", "screen", *args])
    return code, out.getvalue(), err.getvalue()


def companies(prices=True):
    rows = scr.load_metrics(METRICS)
    if prices:
        scr.add_prices(rows, PRICES)
    return rows


class Prices(unittest.TestCase):
    def test_pe_and_earnings_yield(self):
        by = {c["ticker"]: c for c in companies()}
        self.assertEqual(by["AAPL"]["pe"], D("39.2"))       # 341.27 / 8.71
        self.assertEqual(by["GOOGL"]["pe"], D("17.8"))      # 353.78 / 19.91
        self.assertEqual(by["BAC"]["earnings_yield"], D("8.04"))
        self.assertEqual(by["KO"]["price"], D("90.11"))

    def test_no_pe_for_losses(self):
        rows = [{"ticker": "LOSS", "eps_ttm": D("-1.0")}]
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "prices.json")
            with open(p, "w") as fh:
                json.dump({"prices": {"LOSS": 10}}, fh)
            scr.add_prices(rows, p)
        self.assertNotIn("pe", rows[0])
        self.assertEqual(rows[0]["earnings_yield"], D("-10.0"))


class Screen(unittest.TestCase):
    def setUp(self):
        self.spec = scr.load_rules(RULES)
        self.results = scr.screen(companies(), self.spec)

    def test_the_example_screen(self):
        passed = [(r["ticker"], r["score"]) for r in self.results if r["passed"]]
        self.assertEqual(passed, [("GOOGL", D("0.75")), ("KO", D("0.25"))])
        fails = {r["ticker"]: r["fails"] for r in self.results if not r["passed"]}
        self.assertEqual(fails["AAPL"], ["pe 39.2, needs <= 35"])
        self.assertEqual(fails["BAC"], ["operating_margin is missing"])
        self.assertEqual(fails["AXP"], ["revenue_growth_yoy 8.6, needs >= 10",
                                        "operating_margin is missing"])
        verdict = {r["ticker"]: r["passed"] for r in self.results}
        self.assertEqual(verdict, {"GOOGL": True, "KO": True, "BAC": None, "AAPL": False, "AXP": False})

    def test_missing_can_be_skipped(self):
        spec = dict(self.spec, missing="skip")
        res = {r["ticker"]: r for r in scr.screen(companies(), spec)}
        self.assertIsNone(res["BAC"]["passed"])             # Upstream: True, on a rule never checked
        self.assertEqual(res["BAC"]["skipped"], ["operating_margin is missing"])
        self.assertNotIn("score", res["BAC"])                # only a pass is ranked

    def test_every_op(self):
        c = {"ticker": "KO", "sector": "Consumer Staples", "pe": 28.3}
        cases = [({"metric": "pe", "op": "between", "value": [10, 30]}, True),
                 ({"metric": "pe", "op": "between", "value": [10, 20]}, False),
                 ({"metric": "pe", "op": "<", "value": 28.3}, False),
                 ({"metric": "pe", "op": "!=", "value": 1}, True),
                 ({"metric": "ticker", "op": "in", "value": ["ko", "pep"]}, True),
                 ({"metric": "ticker", "op": "not in", "value": ["KO"]}, False),
                 ({"metric": "sector", "op": "==", "value": "consumer staples"}, True),
                 ({"metric": "sector", "op": ">", "value": 3}, False),
                 ({"metric": "beta", "op": "<", "value": 1}, None),
                 # edge pins: each comparison at equality
                 ({"metric": "pe", "op": ">=", "value": 28.3}, True),
                 ({"metric": "pe", "op": "<=", "value": 28.3}, True),
                 ({"metric": "pe", "op": ">", "value": 28.3}, False),
                 ({"metric": "pe", "op": "==", "value": "28.30"}, True),
                 ({"metric": "pe", "op": "between", "value": [28.3, 28.3]}, True)]
        for rule, want in cases:
            self.assertEqual(scr.check(c, rule)[0], want, rule)

    def test_bad_rules_are_refused(self):
        for bad in ({"rules": [{"metric": "pe", "op": "~", "value": 1}]},
                    {"rules": [{"metric": "pe", "op": "between", "value": 3}]},
                    {"rules": [{"metric": "pe", "op": "<"}]},
                    {"rules": [], "rank": [{"metric": "pe", "order": "up"}]},
                    {"rules": [], "missing": "maybe"}):
            with self.assertRaises(scr.ScreenError):
                scr.check_rules(bad)


class Cli(unittest.TestCase):
    def test_text_report_and_shortlist(self):
        code, text, err = run(["--metrics", METRICS, "--criteria", RULES, "--prices", PRICES])
        self.assertEqual(code, 0, err)
        self.assertIn("2 of 5 companies pass", text)
        self.assertIn("Prices as of 2026-10-13", text)
        self.assertIn("not a recommendation", text)
        self.assertNotIn(chr(0x2014), text)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--json", "--home", os.environ["TRADEROS_HOME"], "fundamentals", "screen",
                             "--metrics", METRICS, "--criteria", RULES, "--prices", PRICES])
        sl = from_json(FundamentalScreen, json.dumps(json.loads(out.getvalue())["result"]))
        self.assertEqual([r.ticker for r in sl.rows if r.passed], ["GOOGL", "KO"])
        self.assertEqual(len([r for r in sl.rows if r.passed is False]), 2)      # Upstream: 3 (BAC is unknown)

    def test_csv_with_units_and_nothing_passing(self):
        csv_path = os.path.join(DIR, "companies.csv")
        rows = scr.load_metrics(csv_path)
        self.assertEqual(rows[0]["eps_ttm"], D("8.71"))
        self.assertEqual(rows[0]["revenue_growth_yoy"], D("16.4"))
        self.assertIsNone(rows[1]["operating_margin"])
        self.assertEqual(rows.units["net_margin"], "percent")
        code, text, _ = run(["--metrics", csv_path, "--criteria", RULES])     # its command: no day given
        self.assertEqual(code, 0)   # no prices, so no pe: nothing passes (a result, not a refusal)
        self.assertIn("0 of 3 companies pass", text)
        self.assertIn("pe is missing", text)
        # its CSV states no dates, so a screen dated in the past cannot tell whether its figures were public then
        code, _, err = run(["--metrics", csv_path, "--criteria", RULES, "--as-of", "2026-10-13"])
        self.assertEqual(code, 3)
        self.assertIn("cannot tell when AAPL's figures were filed", err)

    def test_bad_input_exit_2(self):
        code, _, err = run(["--metrics", "/nonexistent.json", "--criteria", RULES])
        self.assertEqual(code, 3)                            # a typed refusal in the TraderOS CLI contract
        self.assertIn("no such file", err)

    def test_top_limits_the_shortlist(self):
        code, text, _ = run(["--metrics", METRICS, "--criteria", RULES, "--prices", PRICES, "--top", "1"])
        self.assertEqual(code, 0)
        self.assertIn(" 1. PASS GOOGL", text)
        self.assertNotIn(" 2. PASS", text)
        self.assertIn("... 1 more pass", text)


# ---- regression tests for the flaws ADR-011 names in screen.py ------------------------------------------------------
@pytest.mark.parametrize("value", ["nan", "inf", "-Infinity", True, float("nan")])
def test_flaw1_nan_infinity_and_booleans_are_refused(value):
    with pytest.raises(scr.ScreenError):                     # Upstream: "inf" passed pe >= 10, true passed > 0
        scr.to_number(value)


def test_flaw1_in_a_file_and_in_a_rule(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("ticker,pe\nAAA,inf\n")
    with pytest.raises(scr.ScreenError):
        scr.load_metrics(p)
    p = tmp_path / "m.json"
    p.write_text('{"companies": [{"ticker": "AAA", "pe": NaN}]}')
    with pytest.raises(scr.ScreenError):
        scr.load_metrics(p)
    with pytest.raises(scr.ScreenError):
        scr.check_rules({"rules": [{"metric": "flag", "op": ">", "value": True}]})
    with pytest.raises(scr.ScreenError):
        scr.check({"ticker": "X", "flag": True}, {"metric": "flag", "op": ">", "value": 0})


def test_flaw2_a_missing_metric_never_passes_or_fails_a_row():
    rows = [{"ticker": "BAC", "operating_margin": None, "net_margin": D("28.8"), "filed": "2026-07-31"}]
    for missing in ("fail", "skip"):
        spec = {"rules": [{"metric": "operating_margin", "op": ">=", "value": 25}], "missing": missing}
        assert scr.screen(rows, spec)[0]["passed"] is None
    spec = {"rules": [{"metric": "operating_margin", "op": ">=", "value": 25},
                      {"metric": "net_margin", "op": ">=", "value": 30}]}
    assert scr.screen(rows, spec)[0]["passed"] is False         # a known fail beats an unknown
    record, _ = scr.run(scr.Companies(rows), {"rules": spec["rules"][:1]}, as_of=date(2026, 10, 1))
    assert record.completeness is Completeness.PARTIAL and record.rows[0].passed is None
    assert record.rows[0].metrics["operating_margin"].unavailable_reason == "missing in the input"


def test_flaw3_one_unit_per_column(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("ticker,net_margin\nAAA,25%\nBBB,0.25\n")      # Upstream: 25.0 and 0.25 compared with one rule
    with pytest.raises(scr.ScreenError) as e:
        scr.load_metrics(p)
    assert "net_margin" in e.value.message
    p.write_text("ticker,eps_ttm\nAAA,8.71%\n")
    with pytest.raises(scr.ScreenError):                         # EPS is not a percent
        scr.load_metrics(p)
    p.write_text("ticker,roe\nAAA,25%\nBBB,12%\n")
    assert scr.load_metrics(p).units["roe"] == "percent"


def test_flaw3_a_rule_in_another_unit_is_refused():
    units = dict(scr.UNITS)
    for rule in ({"metric": "net_margin", "op": ">=", "value": "0.25", "unit": "fraction"},
                 {"metric": "eps_ttm", "op": ">", "value": "5%"}):
        with pytest.raises(scr.ScreenError):
            scr.check_units({"rules": [rule]}, units)
    scr.check_units({"rules": [{"metric": "net_margin", "op": ">=", "value": "25%"}]}, units)
    assert scr.check({"ticker": "A", "net_margin": D("27.2")},
                     {"metric": "net_margin", "op": ">=", "value": "25%"})[0] is True


def test_flaw4_point_in_time_dates_and_flags(tmp_path):
    rows = companies(prices=False)
    with pytest.raises(ValidationError) as e:                    # metrics computed after the screen's day
        scr.run(rows, scr.load_rules(RULES), as_of=date(2026, 10, 1))
    assert e.value.code is ErrorCode.LOOKAHEAD_RISK
    with pytest.raises(ValidationError) as e:
        scr.add_prices(companies(prices=False), PRICES, as_of=date(2026, 10, 12))
    assert e.value.code is ErrorCode.LOOKAHEAD_RISK
    record, _ = scr.run(companies(), scr.load_rules(RULES), prices_as_of=date(2026, 10, 13), now=T0)
    ko = next(r for r in record.rows if r.ticker == "KO")
    assert record.as_of == date(2026, 10, 13) and ko.period_end == date(2026, 4, 3)
    assert [f.split(":")[0] for f in ko.flags] == ["STALE_PERIOD", "PERIOD_MISMATCH"]
    assert not next(r for r in record.rows if r.ticker == "AAPL").flags
    p = tmp_path / "late.json"
    p.write_text(json.dumps({"companies": [{"ticker": "X", "net_margin": "10", "filed": "2026-12-01",
                                            "period_end": "2026-09-30"}]}))
    with pytest.raises(ValidationError) as e:                    # a row filed after as_of
        scr.run(scr.load_metrics(p), {"rules": []}, as_of=date(2026, 11, 1))
    assert e.value.code is ErrorCode.LOOKAHEAD_RISK


def test_flaw5_decimal_scores_are_exact():
    rows = [{"ticker": t, "g": D(g), "m": D(m)}
            for t, g, m in (("A", "0.1", "3"), ("B", "0.2", "1"), ("C", "0.3", "2"))]
    res = scr.screen(rows, {"rules": [], "rank": [{"metric": "g", "weight": "0.1"}, {"metric": "m", "weight": "0.2"}]})
    assert [(r["ticker"], r["score"]) for r in res] == [("A", D("0.667")), ("C", D("0.667")), ("B", D("0.167"))]
    assert all(isinstance(v, D) for r in res for v in r["rank_parts"].values())


def test_flaw6_screen_from_facts_records_and_the_record(tmp_path, monkeypatch):
    monkeypatch.setattr(sec, "MIN_INTERVAL_S", 0)
    c = client(tmp_path, ANALYST)
    reports = [facts.fetch(c, t, as_of=date(2026, 11, 1), filings=False) for t in ("AAPL", "KO")]
    rows = scr.from_reports(reports, [("BAC", "0000070858", "BANK OF AMERICA CORP", "no us-gaap XBRL facts")])
    spec = {"rules": [{"metric": "net_margin", "op": ">=", "value": 20},
                      {"metric": "revenue_growth_yoy", "op": ">=", "value": 10}],
            "rank": [{"metric": "revenue_growth_yoy"}]}
    record, text = scr.run(rows, spec, as_of=date(2026, 11, 1), now=T0)
    verify_hash(record)
    assert from_json(FundamentalScreen, to_json(record)) == record
    by = {r.ticker: r for r in record.rows}
    assert by["AAPL"].passed is True and by["AAPL"].metrics["eps_ttm"].value == D("8.71")
    assert by["AAPL"].metrics["net_margin"].unit == "percent" and by["AAPL"].filed == date(2026, 7, 31)
    assert any(f.startswith("DERIVED: eps_ttm") for f in by["AAPL"].flags)
    assert by["BAC"].passed is None and "no data" in by["BAC"].reasons[0] and "NO_DATA" in by["BAC"].flags
    assert "STALE_PERIOD" in by["KO"].flags[0]
    assert record.completeness is Completeness.PARTIAL and "not a recommendation" in text


def test_the_command_end_to_end_from_filings(monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    monkeypatch.setattr(sec, "MIN_INTERVAL_S", 0)
    fake = Offline(ANALYST)
    monkeypatch.setattr(sec, "_urllib_transport", fake)
    rules = tmp_path / "r.json"
    rules.write_text(json.dumps({"rules": [{"metric": "net_margin", "op": ">=", "value": 20}]}))
    code, text, err = run(["--tickers", "AAPL,KO", "--criteria", str(rules), "--as-of", "2026-11-01"])
    assert code == 0, err
    assert "2 of 2 companies pass" in text and all(h["User-Agent"] == UA for _, h in fake.calls)
    code, _, err = run(["--tickers", "AAPL,ZZZZ", "--criteria", str(rules)])
    assert code == 3 and "ZZZZ" in err                            # an unknown ticker is named
    rules.write_text(json.dumps({"min": {"net_margin": "0.05"}}))
    calls = len(fake.calls)
    code, _, err = run(["--tickers", "AAPL", "--criteria", str(rules)])
    assert code == 3 and "retired" in err and len(fake.calls) == calls     # refused before any request


@pytest.mark.parametrize("text", ['{"rules": [{"metric": "pe", "op": "<", "value": NaN}]}',
                                  '{"rules": [], "rules": []}', "[" * 100_000, "{oops",
                                  '{"rules": [{"metric": "pe", "op": "<", "value": "x"}]}',
                                  '{"rules": [{"metric": "net_margin", "op": ">", "value": 1, "unit": "USD"}]}'])
def test_rules_files_are_strict_and_never_crash(monkeypatch, tmp_path, text):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    fake = Offline(ANALYST)
    monkeypatch.setattr(sec, "_urllib_transport", fake)
    rules = tmp_path / "r.json"
    rules.write_text(text)
    code, _, _ = run(["--tickers", "AAPL", "--criteria", str(rules)])
    assert code == 3 and fake.calls == []                         # refused before any request


def test_input_files_are_capped(tmp_path):
    big = tmp_path / "big.csv"
    big.write_text("ticker,x\n" + "A,1\n" * 300_000)
    with pytest.raises(ValidationError) as e:
        scr.load_metrics(big)
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED
    assert pathlib.Path(RULES).stat().st_size < scr.MAX_FILE
