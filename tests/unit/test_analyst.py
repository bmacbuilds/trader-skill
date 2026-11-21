"""S04: the upstream analyst tests (tank/day-trader-skill/tests/test_analyst.py with upstream PR #24's test), ported.

Each test keeps its inputs and its expected numbers. What changed in a test, and why:
  * numbers are Decimal (the port reads exact decimals): 2.02 is Decimal("2.02");
  * its command line (facts.py AAPL --offline DIR) is `traderos earnings AAPL --as-of 2026-11-01`, served from the
    same files by tests/unit/sec_offline.py; refusals exit 3, as the TraderOS CLI contract says;
  * after its tests: the review's cases for PR #24 and a regression test for every flaw ADR-011 names.
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

from tests.unit.sec_offline import ANALYST, T0, UA, Offline, client, companyfacts
from traderos import cli
from traderos.adapters import sec
from traderos.contracts import (
    AuthorizationError,
    Completeness,
    DataError,
    ErrorCode,
    from_json,
    to_json,
    verify_hash,
)
from traderos.contracts.fundamentals import EarningsReport
from traderos.fundamentals import facts

DIR = ANALYST
AS_OF = "2026-11-01"


def run(args, transport=None):
    """`traderos earnings ...` in-process, every SEC URL served from the upstream fixture files."""
    out, err = io.StringIO(), io.StringIO()
    old = sec._urllib_transport
    sec._urllib_transport = transport or Offline(DIR)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--home", os.environ["TRADEROS_HOME"], "earnings", *args])
    finally:
        sec._urllib_transport = old
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _identified(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    monkeypatch.setattr(sec, "MIN_INTERVAL_S", 0)


class Apple(unittest.TestCase):
    def setUp(self):
        self.r = facts.analyse(companyfacts("0000320193"), quarters=8)

    def test_latest_quarter(self):
        q = self.r["rows"][0]
        self.assertEqual(q["end"], "2026-06-27")
        self.assertEqual(q["fiscal"], "Q3 2026")
        self.assertEqual(q["revenue"], 109417000000)
        self.assertEqual(q["net_income"], 29789000000)
        self.assertEqual(q["eps_diluted"], D("2.02"))
        self.assertEqual(q["revenue_yoy"], D("16.4"))    # vs 94,036 a year earlier
        self.assertEqual(q["eps_yoy"], D("28.7"))        # vs 1.57
        self.assertEqual(q["gross_margin"], D("50.1"))
        self.assertEqual(q["operating_margin"], D("32.6"))
        self.assertEqual(q["net_margin"], D("27.2"))
        self.assertFalse(q["derived"])

    def test_fiscal_labels_come_from_the_first_filing(self):
        labels = [r["fiscal"] for r in self.r["rows"][:6]]
        self.assertEqual(labels, ["Q3 2026", "Q2 2026", "Q1 2026", "Q4 2025", "Q3 2025", "Q2 2025"])

    def test_q4_is_derived_from_the_10k(self):
        q4 = next(r for r in self.r["rows"] if r["end"] == "2025-09-27")
        self.assertTrue(q4["derived"])
        # FY2025 416,161 minus Q1 124,300, Q2 95,359, Q3 94,036
        self.assertEqual(q4["revenue"], 102466000000)
        self.assertEqual(q4["revenue_yoy"], D("7.9"))

    def test_trailing_twelve_months(self):
        self.assertEqual(self.r["ttm"]["revenue"], 466823000000)
        self.assertEqual(self.r["ttm"]["net_income"], 128930000000)
        self.assertEqual(self.r["ttm"]["eps_diluted"], D("8.71"))

    def test_what_changed_in_words(self):
        lines = facts.changes(self.r)
        self.assertIn("revenue $109.42B (+16.4% vs a year earlier)", lines[0])
        self.assertIn("diluted EPS $2.02 (+28.7%)", lines[0])
        self.assertTrue(any(line.startswith("Gross margin 50.1% (+0.8 points") for line in lines))

    def test_restated_values_win_but_labels_stay(self):
        facts_ = [
            {"start": "2025-01-01", "end": "2025-03-31", "val": 100, "fy": 2025, "fp": "Q1",
             "form": "10-Q", "filed": "2025-05-01", "accn": "a"},
            {"start": "2025-01-01", "end": "2025-03-31", "val": 90, "fy": 2026, "fp": "Q1",
             "form": "10-Q", "filed": "2026-05-01", "accn": "b"},
            {"start": "2025-01-01", "end": "2025-03-31", "val": 1, "fy": 2025, "fp": "Q1",
             "form": "8-K", "filed": "2025-04-20", "accn": "c"},
        ]
        q = facts.quarterly(facts_)
        self.assertEqual(q["2025-03-31"]["value"], 90)
        self.assertEqual((q["2025-03-31"]["fy"], q["2025-03-31"]["fp"]), (2025, "Q1"))


def _q(end, val, fp, form, accn, start):
    return {"start": start, "end": end, "val": val, "fy": 2026, "fp": fp, "form": form, "filed": end, "accn": accn}


class DerivedFlag(unittest.TestCase):
    def test_derived_includes_a_q4_that_is_only_derived_in_eps(self):
        # revenue and net income are reported directly for all four quarters (not derived);
        # only EPS skips Q4, so only the eps_diluted series needs the FY-minus-Q1..Q3 derivation
        quarters = [("2026-01-01", "2026-03-31", "Q1", 100), ("2026-04-01", "2026-06-30", "Q2", 110),
                    ("2026-07-01", "2026-09-30", "Q3", 120), ("2026-10-01", "2026-12-31", "Q4", 130)]
        revenue = [_q(end, val, fp, "10-Q", fp, start) for start, end, fp, val in quarters]
        net_income = [_q(end, D(val) / 10, fp, "10-Q", fp + "n", start) for start, end, fp, val in quarters]
        eps = [_q(end, val, fp, "10-Q", fp + "e", start)
               for start, end, fp, val in (("2026-01-01", "2026-03-31", "Q1", D("1.00")),
                                           ("2026-04-01", "2026-06-30", "Q2", D("1.10")),
                                           ("2026-07-01", "2026-09-30", "Q3", D("1.20")))]
        eps.append(_q("2026-12-31", D("4.50"), "FY", "10-K", "fy", "2026-01-01"))
        companyfacts = {"entityName": "Test Co", "cik": 1, "facts": {"us-gaap": {
            "Revenues": {"units": {"USD": revenue}},
            "NetIncomeLoss": {"units": {"USD": net_income}},
            "EarningsPerShareDiluted": {"units": {"USD/shares": eps}},
        }}}
        result = facts.analyse(companyfacts, quarters=4)
        q4 = next(r for r in result["rows"] if r["end"] == "2026-12-31")
        self.assertFalse(facts.quarterly(revenue)["2026-12-31"]["derived"])
        self.assertTrue(q4["derived"])
        self.assertEqual(q4["eps_diluted"], D("1.2"))       # 4.50 - (1.00 + 1.10 + 1.20)


class Filings(unittest.TestCase):
    def test_latest_10q_and_earnings_press_release(self):
        with open(os.path.join(DIR, "submissions-CIK0000320193.json")) as fh:
            sub = json.load(fh)
        with tempfile.TemporaryDirectory() as tmp:
            f = facts.latest_filings(sub, "0000320193", client(pathlib.Path(tmp), DIR))
        self.assertEqual(f["report"]["form"], "10-Q")
        self.assertEqual(f["report"]["period"], "2026-06-27")
        self.assertTrue(f["report"]["url"].endswith("/000032019326000020/aapl-20260627.htm"))
        e = f["earnings_8k"]
        self.assertEqual(e["filed"], "2026-07-30")
        self.assertEqual(e["exhibit"], "https://www.sec.gov/Archives/edgar/data/320193/"
                                       "000032019326000018/a8-kex991q3202606272026.htm")

    def test_exhibit_names_seen_on_edgar(self):
        pick = facts.exhibit_name
        self.assertEqual(pick(["axp-20260724.htm", "q226exhibit991.htm", "q226exhibit992.htm",
                               "R1.htm"], "axp-20260724.htm"), "q226exhibit991.htm")
        self.assertEqual(pick(["goog-20260722.htm", "googexhibit991q22026.htm"],
                              "goog-20260722.htm"), "googexhibit991q22026.htm")
        self.assertEqual(pick(["a2026q2earningsreleaseex-9.htm", "ko-20260728.htm"],
                              "ko-20260728.htm"), "a2026q2earningsreleaseex-9.htm")
        self.assertEqual(pick(["bac-06302026ex993.htm", "bac-06302026ex991.htm"]),
                         "bac-06302026ex991.htm")
        self.assertIsNone(pick(["only-8k.htm"], "only-8k.htm"))


class LaggingData(unittest.TestCase):
    def test_says_when_sec_facts_trail_a_filed_10q(self):
        code, out, err = run(["KO", "--as-of", AS_OF])
        self.assertEqual(code, 0, err)
        self.assertIn("COCA COLA CO (KO)", out)
        self.assertIn("NOTE: SEC's XBRL facts stop at 2026-04-03, but a 10-Q for the period "
                      "ending 2026-07-03 was filed 2026-07-29", out)

    def test_one_offs_are_flagged(self):
        result = {"rows": [
            {"end": "2026-06-30", "derived": False, "revenue": D("100"), "net_income": D("90"),
             "operating_income": D("30"), "eps_diluted": D("1"), "revenue_yoy": D("10"),
             "net_income_yoy": D("200"), "eps_yoy": D("200"), "gross_margin": None,
             "operating_margin": D("30"), "net_margin": D("90")}]}
        self.assertTrue(any("above operating income" in line for line in facts.changes(result)))


class Cli(unittest.TestCase):
    def test_metrics_file_for_the_screener(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trader", "metrics.json")
            code, out, err = run(["AAPL", "KO", "--as-of", AS_OF, "--metrics", path])
            self.assertEqual(code, 0, err)
            with open(path) as fh:
                data = json.load(fh)
        rows = {r["ticker"]: r for r in data["companies"]}
        self.assertEqual(sorted(rows), ["AAPL", "KO"])
        self.assertEqual(rows["AAPL"]["eps_ttm"], "8.71")              # exact decimal strings
        self.assertEqual(rows["AAPL"]["revenue_growth_yoy"], "16.4")
        self.assertEqual(rows["KO"]["period_end"], "2026-04-03")
        self.assertIn("wrote", out)
        self.assertNotIn(chr(0x2014), out)

    def test_unknown_ticker_and_no_user_agent(self):
        code, _, err = run(["ZZZZ", "--as-of", AS_OF])
        self.assertEqual(code, 3)
        self.assertIn("no ZZZZ", err)
        old = os.environ.pop("SEC_USER_AGENT", None)
        try:
            code, _, err = run(["AAPL", "--as-of", AS_OF])
            self.assertEqual(code, 3)
            self.assertIn("identify the person", err)
            os.environ["SEC_USER_AGENT"] = "Your Name you@example.com"
            with self.assertRaises(AuthorizationError):
                sec.user_agent(None)
        finally:
            os.environ.pop("SEC_USER_AGENT", None)
            if old is not None:
                os.environ["SEC_USER_AGENT"] = old

    def test_json_output(self):
        out, err = io.StringIO(), io.StringIO()
        sec._urllib_transport, old = Offline(DIR), sec._urllib_transport
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--json", "--home", os.environ["TRADEROS_HOME"], "earnings", "AAPL",
                                 "--quarters", "4", "--as-of", AS_OF])
        finally:
            sec._urllib_transport = old
        self.assertEqual(code, 0, err.getvalue())
        data = json.loads(out.getvalue())["result"]
        self.assertEqual(len(data["quarters"]), 4)
        self.assertEqual(data["concepts"]["revenue"], ["RevenueFromContractWithCustomerExcludingAssessedTax"])


# ---- the review's cases for PR #24 (upstream_pr_review.md, PR #24 section 10) ---------------------------------------
def _company(**series):
    names = {"revenue": ("Revenues", "USD"), "net_income": ("NetIncomeLoss", "USD"),
             "eps": ("EarningsPerShareDiluted", "USD/shares"), "gross_profit": ("GrossProfit", "USD"),
             "operating_income": ("OperatingIncomeLoss", "USD"), "cost": ("CostOfRevenue", "USD")}
    return {"entityName": "Test Co", "cik": 1, "facts": {"us-gaap": {
        names[k][0]: {"units": {names[k][1]: v}} for k, v in series.items()}}}


QS = (("2026-01-01", "2026-03-31", "Q1"), ("2026-04-01", "2026-06-30", "Q2"), ("2026-07-01", "2026-09-30", "Q3"),
      ("2026-10-01", "2026-12-31", "Q4"))


def _four(values, tag, q4=True):
    rows = [_q(end, D(v), fp, "10-Q", tag + fp, start) for (start, end, fp), v in zip(QS, values, strict=False)]
    return rows if q4 else rows[:3]


def _fy(value, tag):
    return _q("2026-12-31", D(value), "FY", "10-K", tag + "fy", "2026-01-01")


def test_pr24_case2_every_series_reported_for_all_four_quarters_is_not_derived():
    r = facts.analyse(_company(revenue=_four([100, 110, 120, 130], "r"), net_income=_four([10, 11, 12, 13], "n"),
                               eps=_four(["1", "1.1", "1.2", "1.3"], "e")), quarters=4)
    q4 = r["rows"][0]
    assert q4["end"] == "2026-12-31" and q4["derived"] is False and q4["derived_series"] == []


def test_pr24_case3_a_derived_gross_profit_flags_the_row():
    gp = [*_four([50, 55, 60], "g", q4=False), _fy(230, "g")]
    r = facts.analyse(_company(revenue=_four([100, 110, 120, 130], "r"), net_income=_four([10, 11, 12, 13], "n"),
                               gross_profit=gp), quarters=4)
    q4 = r["rows"][0]
    assert q4["gross_profit"] == 65 and q4["derived"] is True and q4["derived_series"] == ["gross_profit"]
    assert q4["gross_margin"] == D("50.0")


def test_pr24_case3b_derived_cost_of_revenue_and_operating_income_flag_the_row_too():
    cost = [*_four([50, 55, 60], "c", q4=False), _fy(230, "c")]
    oi = [*_four([20, 22, 24], "o", q4=False), _fy(90, "o")]
    r = facts.analyse(_company(revenue=_four([100, 110, 120, 130], "r"), net_income=_four([10, 11, 12, 13], "n"),
                               cost=cost, operating_income=oi), quarters=4)
    q4 = r["rows"][0]
    assert q4["gross_profit"] == 130 - 65 and q4["operating_income"] == 24
    assert q4["derived"] is True and q4["derived_series"] == ["cost_of_revenue", "operating_income"]


def test_pr24_case4_no_q4_eps_and_no_fy_eps_is_none_and_not_derived():
    r = facts.analyse(_company(revenue=_four([100, 110, 120, 130], "r"), net_income=_four([10, 11, 12, 13], "n"),
                               eps=_four(["1", "1.1", "1.2"], "e", q4=False)), quarters=4)
    q4 = r["rows"][0]
    assert q4["eps_diluted"] is None and q4["derived"] is False


def test_pr24_derived_reaches_the_trailing_total_and_the_metrics_row():
    eps = [*_four(["1.00", "1.10", "1.20"], "e", q4=False), _fy("4.50", "e")]
    r = facts.analyse(_company(revenue=_four([100, 110, 120, 130], "r"), net_income=_four([10, 11, 12, 13], "n"),
                               eps=eps), quarters=4)
    assert r["ttm"]["eps_diluted"] == D("4.50") and r["ttm_derived"] == ["eps_diluted"]
    row = facts.metrics_row("TEST", r)
    assert "eps_ttm" in row["derived"] and "latest quarter: eps_diluted" in row["derived"]


# ---- regression tests for the flaws ADR-011 names in facts.py ------------------------------------------------------
def test_flaw1_point_in_time_a_later_restatement_is_not_known_earlier():
    rev = [_q("2026-03-31", 100, "Q1", "10-Q", "a", "2026-01-01"),
           dict(_q("2026-03-31", 90, "Q1", "10-Q/A", "z", "2026-01-01"), filed="2026-09-01")]
    assert facts.quarterly(rev, as_of=date(2026, 6, 1))["2026-03-31"]["value"] == 100     # Upstream: 90 on any day
    later = facts.quarterly(rev, as_of=date(2026, 9, 1))["2026-03-31"]
    assert later["value"] == 90 and later["amendment"] is True and later["first_filed"] == "2026-03-31"
    assert facts.quarterly(rev, as_of=date(2026, 3, 30)) == {}                              # nothing public yet
    with pytest.raises(DataError) as e:
        facts.analyse(_company(revenue=rev), as_of=date(2026, 1, 1))
    assert e.value.code is ErrorCode.MISSING_DATA


def test_flaw2_decimal_a_derived_value_is_exact():
    eps = [*_four(["1.00", "1.10", "1.20"], "e", q4=False), _fy("4.50", "e")]
    assert facts.quarterly(eps)["2026-12-31"]["value"] == D("1.20")    # Upstream: 1.2000000000000002


def test_flaw3_another_currency_is_refused_by_name():
    cny = {"entityName": "X", "cik": 1, "facts": {"us-gaap": {
        "Revenues": {"units": {"CNY": [_q("2026-03-31", 7000000000, "Q1", "10-Q", "a", "2026-01-01")]}},
        "NetIncomeLoss": {"units": {"CNY": [_q("2026-03-31", 700000000, "Q1", "10-Q", "a", "2026-01-01")]}}}}}
    with pytest.raises(DataError) as e:                                 # Upstream printed it as US$ millions
        facts.analyse(cny)
    assert e.value.code is ErrorCode.OUTSIDE_CAPABILITY and "CNY" in e.value.message
    mixed = _company(revenue=_four([100, 110, 120, 130], "r"), net_income=_four([10, 11, 12, 13], "n"))
    mixed["facts"]["us-gaap"]["EarningsPerShareDiluted"] = {"units": {"CNY/shares": _four(["1", "1", "1", "1"], "e")}}
    r = facts.analyse(mixed, quarters=4)
    assert r["rows"][0]["eps_diluted"] is None and r["refused_units"] == {"eps_diluted": ["CNY/shares"]}
    report = facts.to_report(r, ticker="X", as_of=date(2027, 1, 1), source_url="u", retrieved_at=T0, quarters=4)
    assert any(lim.code == "CURRENCY" and "CNY/shares" in lim.detail for lim in report.limitations)


def test_flaw4_and_5_first_filed_and_amendment_travel_with_each_value():
    cf = companyfacts("0000320193")
    q = facts.quarterly(cf["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"])
    q3 = q["2025-06-28"]
    assert (q3["filed"], q3["first_filed"], q3["amendment"]) == ("2026-07-31", "2025-08-01", False)
    q4 = q["2025-09-27"]                                                # derived: public once all four were
    assert q4["derived"] and q4["first_filed"] == "2025-10-31"


def test_flaw7_no_margin_over_a_revenue_that_is_not_positive():
    assert facts.margin(D(-50), D(-100)) is None                       # Upstream: +50.0
    assert facts.margin(D(5), D(0)) is None and facts.margin(D(-5), D(100)) == D("-5.0")


def test_flaw8_trailing_totals_use_the_same_four_quarter_ends():
    ends = (("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-03-31"),
            ("2026-04-01", "2026-06-30"), ("2026-07-01", "2026-09-30"))
    rev = [_q(e, 10, "Q", "10-Q", f"r{i}", s) for i, (s, e) in enumerate(ends)]
    eps = [_q(e, D("0.1"), "Q", "10-Q", f"e{i}", s) for i, (s, e) in enumerate(ends[:4])]   # newest EPS missing
    r = facts.analyse(_company(revenue=rev, net_income=[dict(x, val=1) for x in rev], eps=eps))
    assert r["rows"][0]["end"] == "2026-09-30" and r["ttm_ends"][-1] == "2026-09-30"
    assert r["ttm"]["revenue"] == 40 and "eps_diluted" not in r["ttm"]   # Upstream: 0.4 over a different year
    report = facts.to_report(r, ticker="T", as_of=date(2027, 1, 1), source_url="u", retrieved_at=T0, quarters=6)
    assert report.ttm["eps_diluted"].value is None and "same four" in report.ttm["eps_diluted"].unavailable_reason


def test_flaw9_labels_a_q4_from_a_10k_and_a_comparative_from_a_later_filing():
    r3 = [_q("2026-12-31", 130, "FY", "10-K", "k", "2026-10-01")]
    v = facts.quarterly(r3)["2026-12-31"]
    assert (v["fp"], v["fy"]) == ("Q4", 2026)                         # Upstream: FY 2026
    r4 = [dict(_q("2025-06-30", 80, "Q2", "10-Q", "x", "2025-04-01"), filed="2026-07-30"),
          dict(_q("2026-06-30", 110, "Q2", "10-Q", "x", "2026-04-01"), filed="2026-07-30")]
    qq = facts.quarterly(r4)
    assert (qq["2025-06-30"]["fp"], qq["2025-06-30"]["fy"]) == ("Q2", 2025)   # Upstream: Q2 2026, twice
    assert (qq["2026-06-30"]["fp"], qq["2026-06-30"]["fy"]) == ("Q2", 2026)
    annual = [{"start": "2024-01-01", "end": "2024-12-31", "val": 1, "fy": 2026, "fp": "FY", "form": "10-K",
               "filed": "2027-02-01", "accn": "k"},
              {"start": "2026-01-01", "end": "2026-12-31", "val": 1, "fy": 2026, "fp": "FY", "form": "10-K",
               "filed": "2027-02-01", "accn": "k"}]
    assert facts.periods(annual)[("2024-01-01", "2024-12-31")]["fy"] == 2024   # two years back, not 2026


def test_flaw10_a_row_is_dated_by_the_latest_filing_of_its_numbers():
    rev = _four([100, 110, 120, 130], "r")
    ni = [*_four([10, 11, 12], "n", q4=False), dict(_q("2026-12-31", 13, "Q4", "10-Q/A", "nA", "2026-10-01"),
                                                     filed="2027-03-01")]
    r = facts.analyse(_company(revenue=rev, net_income=ni), quarters=1)
    assert r["rows"][0]["filed"] == "2027-03-01" and r["rows"][0]["amended_series"] == ["net_income"]


def test_flaw1_filing_links_are_point_in_time_too(tmp_path):
    sub = json.loads((DIR / "submissions-CIK0000320193.json").read_text())
    f = facts.latest_filings(sub, "0000320193", client(tmp_path, DIR), as_of=date(2026, 7, 30))
    assert f["report"] is None and f["earnings_8k"]["filed"] == "2026-07-30"    # the 10-Q came a day later
    assert facts.latest_filings(sub, "0000320193", None, as_of=date(2026, 7, 29)) == {"report": None,
                                                                                  "earnings_8k": None}


def test_the_record_round_trips_and_says_what_it_is(tmp_path):
    r = facts.fetch(client(tmp_path, DIR), "aapl", as_of=date(2026, 11, 1), quarters=8)
    verify_hash(r)
    assert from_json(EarningsReport, to_json(r)) == r
    assert r.ticker == "AAPL" and r.cik == "0000320193" and r.entity == "Apple Inc."
    assert r.completeness is Completeness.COMPLETE and r.ttm["eps_diluted"].value == D("8.71")
    assert [str(e) for e in r.ttm_quarter_ends] == ["2025-09-27", "2025-12-27", "2026-03-28", "2026-06-27"]
    assert r.ttm_derived == ("revenue", "net_income", "eps_diluted")
    q4 = next(q for q in r.quarters if str(q.period_end) == "2025-09-27")
    assert q4.derived and q4.first_filed == date(2025, 10, 31) and q4.gross_margin.unit == "percent"
    assert {lim.code for lim in r.limitations} >= {"AS_FILED", "AVAILABLE_ON_FILED_DATE", "DERIVED_Q4"}
    assert r.latest_report.form == "10-Q" and r.earnings_release.exhibit_url.endswith("a8-kex991q3202606272026.htm")
    assert all(f.filed <= r.as_of for f in r.facts) and any(f.derived for f in r.facts)
    text = facts.describe(r)
    assert "Q4 2025" in text and "derived" in text and "filed by 2026-11-01" in text
    early = facts.fetch(client(tmp_path / "2", DIR), "AAPL", as_of=date(2026, 6, 1))
    assert early.quarters[-1].period_end == date(2026, 3, 28)           # the June 10-Q is not public yet


def test_a_restated_quarter_moves_the_derived_q4_and_its_filed_day():
    q = [*_four([100, 110, 120], "r", q4=False), dict(_fy(460, "r"), filed="2027-02-01"),
         dict(_q("2026-06-30", 115, "Q2", "10-Q/A", "rA", "2026-04-01"), filed="2027-04-01")]
    before = facts.quarterly(q, as_of=date(2027, 3, 1))["2026-12-31"]
    after = facts.quarterly(q, as_of=date(2027, 5, 1))["2026-12-31"]
    assert (before["value"], before["filed"], before["amendment"]) == (130, "2027-02-01", False)
    assert (after["value"], after["filed"], after["amendment"]) == (125, "2027-04-01", True)
    assert after["first_filed"] == "2027-02-01" and after["sources"] == ("rA", "rQ1", "rQ3", "rfy")


# ---- edge pins on the upstream kept rules (each kills a mutant that otherwise survives)
# ---------------------------------
def test_a_six_month_year_to_date_value_is_never_a_quarter():
    q = [_q("2026-06-30", 110, "Q2", "10-Q", "a", "2026-04-01"), _q("2026-06-30", 210, "Q2", "10-Q", "a", "2026-01-01")]
    assert facts.quarterly(q)["2026-06-30"]["value"] == 110


def test_q4_is_derived_only_from_three_filed_quarters():
    assert "2026-12-31" not in facts.quarterly([*_four([100, 110], "r", q4=False), _fy(460, "r")])


def test_no_trailing_total_over_a_missing_quarter():
    ends = (("2025-01-01", "2025-03-31"), ("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31"),
            ("2026-01-01", "2026-03-31"))                       # 2025 Q2 is missing: the four span 365 days
    rev = [_q(e, 10, "Q", "10-Q", f"r{i}", s) for i, (s, e) in enumerate(ends)]
    r = facts.analyse(_company(revenue=rev, net_income=[dict(x, val=1) for x in rev]))
    assert r["ttm"] == {} and r["ttm_ends"] == []
