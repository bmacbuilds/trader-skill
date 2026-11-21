"""OSDev6's review of #11 (changes needed at 6375c1b): one test, or more, for every item, on the upstream fixtures.

H1 the screener's look-ahead, M3 the copy that one tiny position failed, M1 the 13F date after an amendment, M2 the
DOCTYPE refusal a UTF-16 document slipped past, LOW 1-8, and the named changes that have a behaviour to pin.
ADR-011 names each change; nothing here reaches the network (conftest).
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from tests.unit.sec_offline import ANALYST, COPYCAT, SCREENER, T0, UA, Offline, client
from traderos import cli
from traderos.adapters import sec
from traderos.contracts import ErrorCode, ValidationError, from_json, verify_hash
from traderos.contracts.fundamentals import CopyTargets, HoldingsReport
from traderos.fundamentals import edgar13f as e13
from traderos.fundamentals import facts
from traderos.fundamentals import screen as scr

BRK = "1067983"
URLLIB_TRANSPORT = sec._urllib_transport          # the real one, kept before conftest swaps in its network guard
METRICS = SCREENER / "metrics.json"
GROWTH = {"rules": [{"metric": "revenue_growth_yoy", "op": ">=", "value": 10}]}


@pytest.fixture(autouse=True)
def _identified(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    monkeypatch.setattr(sec, "MIN_INTERVAL_S", 0)


def main(*argv, transport=None, as_json=False):
    """`traderos ...` in-process; SEC URLs are served from fixtures by `transport`."""
    out, err = io.StringIO(), io.StringIO()
    old = sec._urllib_transport
    sec._urllib_transport = transport or Offline(COPYCAT)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main([*(["--json"] if as_json else []), "--home", os.environ["TRADEROS_HOME"], *argv])
    finally:
        sec._urllib_transport = old
    return code, out.getvalue(), err.getvalue()


def _file(tmp_path, body, name="m.json"):
    p = tmp_path / name
    p.write_text(json.dumps(body))
    return p


# ---- H1: nothing filed after the screen's as_of is used -------------------------------------------------------------
def test_h1_osdev6_repro_upstream_file_without_its_as_of(tmp_path):
    data = json.loads(METRICS.read_text())
    del data["as_of"]                                            # its file, its date removed
    p = _file(tmp_path, data)
    rules = _file(tmp_path, GROWTH, "rules.json")
    code, _, err = main("fundamentals", "screen", "--metrics", str(p), "--criteria", str(rules),
                        "--as-of", "2025-06-01")
    assert code == 3 and "LOOKAHEAD_RISK" in err                 # was: exit 0, AAPL, BAC, GOOGL passed
    with pytest.raises(ValidationError) as e:
        scr.run(scr.load_metrics(p), GROWTH, as_of=date(2025, 6, 1), now=T0)
    assert e.value.code is ErrorCode.LOOKAHEAD_RISK and "cover a quarter ending 2026-06-27" in e.value.message


@pytest.mark.parametrize("row, what", [
    ({"period_end": "2025-06-30", "filed": "2025-05-01"}, "cover a quarter ending 2025-06-30"),
    ({"period_end": "2025-03-31", "filed": "2025-05-01", "as_of": "2026-10-13"}, "are as of 2026-10-13"),
    ({"period_end": "2025-03-31", "filed": "2025-06-02"}, "were filed 2025-06-02"),
])
def test_h1_a_row_from_after_as_of_is_refused_by_each_of_its_dates(row, what):
    rows = scr.Companies([dict(row, ticker="X", revenue_growth_yoy=D(20))], dict(scr.UNITS))
    with pytest.raises(ValidationError) as e:
        scr.run(rows, GROWTH, as_of=date(2025, 6, 1), now=T0)
    assert e.value.code is ErrorCode.LOOKAHEAD_RISK and what in e.value.message


def test_h1_on_the_day_itself_a_row_is_used():
    rows = scr.Companies([{"ticker": "X", "revenue_growth_yoy": D(20), "period_end": "2025-03-31",
                           "filed": "2025-06-01", "as_of": "2025-06-01"}], dict(scr.UNITS))
    record, _ = scr.run(rows, GROWTH, as_of=date(2025, 6, 1), now=T0)
    assert record.rows[0].passed is True


def test_h1_undated_figures_are_never_guessed_into_the_past():
    def rows(**file):
        return scr.Companies([{"ticker": "X", "revenue_growth_yoy": D(20)},
                              {"ticker": "Y", "_why": "no XBRL facts", "revenue_growth_yoy": None}],
                             dict(scr.UNITS), **file)
    with pytest.raises(ValidationError) as e:                    # no filed, no as_of: cannot tell
        scr.run(rows(), GROWTH, as_of=date(2026, 10, 31), now=T0)
    assert e.value.code is ErrorCode.LOOKAHEAD_RISK and "cannot tell when X's figures" in e.value.message
    assert scr.run(rows(), GROWTH, as_of=T0.date(), now=T0)[0].rows[0].passed is True     # today: in hand now
    assert scr.run(rows(), GROWTH, now=T0)[0].as_of == T0.date()                          # no day given: today
    dated = rows(as_of=date(2026, 10, 1))                         # the file vouches for its rows
    assert scr.run(dated, GROWTH, as_of=date(2026, 10, 31), now=T0)[0].rows[0].passed is True


def test_h1_osdev6_repro_the_merge_keeps_the_files_as_of(tmp_path, monkeypatch):
    p = tmp_path / "metrics.json"
    shutil.copy(METRICS, p)                                      # its file, dated 2026-10-13
    code, _, err = main("earnings", "AAPL", "--as-of", "2025-06-01", "--metrics", str(p), transport=Offline(ANALYST))
    assert code == 0, err
    data = json.loads(p.read_text())
    assert data["as_of"] == "2026-10-13"                         # was: lowered to 2025-06-01
    rows = {r["ticker"]: r for r in data["companies"]}
    assert rows["AAPL"]["as_of"] == "2025-06-01" and rows["BAC"]["as_of"] == "2026-10-13"
    with pytest.raises(ValidationError) as e:                    # was: BAC and GOOGL passed on 2026-06-30 quarters
        scr.run(scr.load_metrics(p), GROWTH, as_of=date(2025, 6, 1), now=T0)
    assert e.value.code is ErrorCode.LOOKAHEAD_RISK
    companies = scr.load_metrics(p)
    companies.as_of = None                                       # even with the file's date gone, BAC's own refuses
    with pytest.raises(ValidationError) as e:
        scr.run(companies, GROWTH, as_of=date(2025, 6, 1), now=T0)
    assert "AXP's figures are as of 2026-10-13" in e.value.message           # the first of its rows
    record, _ = scr.run(scr.load_metrics(p), GROWTH, now=T0)
    assert record.as_of == date(2026, 10, 13)


def test_h1_a_merge_never_vouches_for_a_row_it_cannot_date(tmp_path, monkeypatch):
    p = _file(tmp_path, {"companies": [{"ticker": "BAC", "revenue_growth_yoy": "15.0", "period_end": "2026-06-30"}]})
    code, _, err = main("earnings", "AAPL", "--as-of", "2026-11-01", "--metrics", str(p), transport=Offline(ANALYST))
    assert code == 0, err
    data = json.loads(p.read_text())
    assert data["as_of"] is None and "as_of" not in next(r for r in data["companies"] if r["ticker"] == "BAC")
    with pytest.raises(ValidationError) as e:                    # BAC's day is unknown: a past screen refuses
        scr.run(scr.load_metrics(p), GROWTH, as_of=date(2026, 11, 1), now=datetime(2026, 11, 20, tzinfo=UTC))
    assert "cannot tell when BAC's figures" in e.value.message


def test_h1_a_later_command_never_lowers_the_files_as_of(tmp_path):
    p = _file(tmp_path, {"as_of": "2026-10-13", "companies": [{"ticker": "BAC", "revenue_growth_yoy": "15.0",
                                                               "as_of": "2026-09-01", "filed": "2026-08-01"}]})
    code, _, err = main("earnings", "AAPL", "--as-of", "2025-06-01", "--metrics", str(p), transport=Offline(ANALYST))
    assert code == 0, err
    data = json.loads(p.read_text())
    assert data["as_of"] == "2026-10-13"                         # the file's, not its newest row's 2026-09-01
    assert [r["as_of"] for r in data["companies"]] == ["2025-06-01", "2026-09-01"]      # each row its own


def test_h1_metrics_rows_from_records_carry_their_as_of(tmp_path):
    report = facts.fetch(client(tmp_path, ANALYST), "AAPL", as_of=date(2026, 11, 1), filings=False)
    assert scr.metrics_from_report(report)["as_of"] == date(2026, 11, 1)


def test_h1_undated_prices_are_never_guessed_into_the_past(tmp_path):
    p = _file(tmp_path, {"prices": {"AAPL": "341.27"}}, "prices.json")
    with pytest.raises(ValidationError) as e:
        scr.add_prices(scr.load_metrics(METRICS), p, as_of=date(2026, 10, 13), today=date(2026, 11, 20))
    assert e.value.code is ErrorCode.LOOKAHEAD_RISK
    rows = scr.load_metrics(METRICS)
    assert scr.add_prices(rows, p, as_of=date(2026, 11, 20), today=date(2026, 11, 20)) is None    # today: fine
    assert rows[0]["price"] == D("341.27")
    assert scr.add_prices(scr.load_metrics(METRICS), SCREENER / "prices.json", as_of=date(2026, 10, 13),
                          today=date(2026, 11, 20)) == date(2026, 10, 13)                      # its dated file


# ---- M3: one tiny position never fails the whole copy ---------------------------------------------------------------
def test_m3_osdev6_repro_berkshire_copy_without_top():
    code, text, err = main("holdings", BRK, "--copy", "--as-of", "2026-11-01", as_json=True)
    assert code == 0, err                                        # was: exit 3, "a weight is positive"
    t = from_json(CopyTargets, json.dumps(json.loads(text)["result"]))
    verify_hash(t)
    assert len(t.weights) == 25 and "DHI" not in [w.ticker for w in t.weights]
    assert abs(sum(w.weight for w in t.weights) - 100) < D("1e-20")      # unrounded: they sum to 100
    assert t.weights[0].weight == D("22.04")                     # its table figures sum to 100.00 here
    dhi = next(s for s in t.skipped if s.ticker == "DHI")
    assert dhi.why.startswith("true weight 0.00019% of the copy") and "rounds to 0.00%" in dhi.why
    code, text, err = main("holdings", BRK, "--copy", "--as-of", "2026-11-01")
    assert code == 0, err
    assert "left out: DHI (D R HORTON INC), true weight 0.00019% of the copy" in text
    assert "  AAPL    Apple                         22.04%  (22.04% of the 13F)" in text      # its line, as before


def test_m3_a_position_worth_nothing_is_left_out_by_name():
    rows = [{"issuer": n, "title": "COM", "cusip": c, "figi": "", "value": D(v), "shares": D(1), "share_type": "SH",
             "put_call": "", "manager": ""}
            for n, c, v in (("BIG CO", "000000001", "3000"), ("ZERO CO", "000000002", "0"))]
    positions, _ = e13.aggregate(rows)
    for p, t in zip(positions, ("BIG", "ZERO"), strict=True):
        p["ticker"] = t
    weights, skipped = e13.copy_weights(positions)
    assert [(w["ticker"], w["weight"]) for w in weights] == [("BIG", D(100))]
    assert skipped == [{"issuer": "ZERO CO", "ticker": "ZERO", "why": "true weight 0.00% of the copy: its share of "
                        "the 13F rounds to 0.00%, so it scales to 0, and a copy weight is above zero"}]


# ---- M1 and LOW 1: an amendment dates the holdings, and a bad one never blocks the quarter ---------------------------
def _amendments(*filed):
    """Berkshire's fixtures plus 13F-HR/As for 2026-06-30: (accession, filed, type, rows, total, cover prefix)."""
    sub = json.loads((COPYCAT / "CIK0001067983.json").read_text())
    rec, extra = sub["filings"]["recent"], {}
    for acc, day, kind, rows, total, prefix in filed:
        for key, value in (("form", "13F-HR/A"), ("accessionNumber", acc), ("filingDate", day),
                           ("reportDate", "2026-06-30"), ("primaryDocument", "primary_doc.xml")):
            rec[key].insert(0, value)
        base = f"https://www.sec.gov/Archives/edgar/data/1067983/{acc.replace('-', '')}/"
        table = "".join(f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>{c}"
                        f"</cusip><value>{v}</value><shrsOrPrnAmt><sshPrnamt>{s}</sshPrnamt><sshPrnamtType>SH"
                        "</sshPrnamtType></shrsOrPrnAmt></infoTable>" for n, c, v, s in rows)
        extra[base + "index.json"] = json.dumps({"directory": {"item": [{"name": "primary_doc.xml"},
                                                                        {"name": "infotable.xml"}]}}).encode()
        extra[base + "infotable.xml"] = f"<informationTable>{table}</informationTable>".encode()
        extra[base + "primary_doc.xml"] = (
            f"{prefix}<edgarSubmission><formData><coverPage><isAmendment>true</isAmendment><amendmentInfo>"
            f"<amendmentType>{kind}</amendmentType></amendmentInfo></coverPage><summaryPage><tableValueTotal>"
            f"{total}</tableValueTotal></summaryPage></formData></edgarSubmission>").encode()
    extra["https://data.sec.gov/submissions/CIK0001067983.json"] = json.dumps(sub).encode()
    return Offline(COPYCAT, extra)


NEW = ("0001193125-26-400001", "2026-09-30", "NEW HOLDINGS", [("EXTRA CO", "000000009", "1000", "10")], "1000", "")
RESTATED = ("0001193125-26-400002", "2026-10-30", "RESTATEMENT", [("AAA CORP", "000000008", "300", "300")], "300", "")


def test_m1_osdev6_repro_the_holdings_are_dated_by_the_latest_amendment_applied(tmp_path):
    fake = _amendments(NEW, RESTATED)
    code, text, err = main("holdings", BRK, "--as-of", "2026-11-15", transport=fake, as_json=True)
    assert code == 0, err
    r = from_json(HoldingsReport, json.dumps(json.loads(text)["result"]))
    assert [h.issuer for h in r.holdings] == ["AAA CORP"] and len(r.amendments_applied) == 2
    assert r.filed == date(2026, 10, 30)                         # was: 2026-08-14, the original's
    delay = next(lim for lim in r.limitations if lim.code == "REPORTED_WITH_DELAY")
    assert "made public 2026-10-30" in delay.detail
    meta, _, _ = e13.load_quarter(sec.SecClient(tmp_path, ua=UA, transport=fake), BRK, as_of=date(2026, 10, 15))
    assert (meta["filed"], meta["original_filed"]) == ("2026-09-30", "2026-08-14")       # only NEW HOLDINGS yet
    code, text, err = main("holdings", BRK, "--as-of", "2026-11-15", transport=fake)
    assert "filed 2026-08-14 (accession 0001193125-26-352200)" in text                   # the original, by name


def test_m1_an_amendment_not_applied_does_not_date_the_holdings(tmp_path):
    unknown = ("0001193125-26-400003", "2026-09-30", "", [("X", "000000009", "1", "1")], "1", "")
    meta, rows, _ = e13.load_quarter(sec.SecClient(tmp_path, ua=UA, transport=_amendments(unknown)), BRK,
                                     as_of=date(2026, 11, 15))
    assert meta["filed"] == "2026-08-14" and meta["applied"] == [] and len(rows) == 89


def test_m1_the_diff_and_the_copy_are_dated_by_the_amendment_too():
    fake = _amendments(NEW)                                      # its own home: the SEC cache holds no other fixture
    code, text, err = main("holdings", BRK, "--diff", "--as-of", "2026-11-15", transport=fake, as_json=True)
    assert code == 0, err
    assert json.loads(text)["result"]["filed"] == "2026-09-30"
    code, text, err = main("holdings", BRK, "--copy", "--as-of", "2026-11-15", transport=fake, as_json=True)
    assert code == 0, err
    t = from_json(CopyTargets, json.dumps(json.loads(text)["result"]))
    assert t.filed == date(2026, 9, 30) and "made public 2026-09-30" in t.limitations[0].detail
    code, text, err = main("holdings", BRK, "--copy", "--as-of", "2026-11-15", transport=fake)
    assert "filed 2026-08-14 (accession 0001193125-26-352200)" in text and "Holdings as of 2026-06-30, filed " \
        "2026-09-30." in text


@pytest.mark.parametrize("where", ["cover", "table"])
def test_low1_a_refused_amendment_is_not_applied_and_the_quarter_still_loads(tmp_path, where):
    doctype = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY n "INJECTED">]>'
    bad = ("0001193125-26-400004", "2026-09-30", "RESTATEMENT", [("AAA", "000000008", "1", "1")], "1",
           doctype if where == "cover" else "")
    fake = _amendments(bad)
    if where == "table":
        url = "https://www.sec.gov/Archives/edgar/data/1067983/000119312526400004/infotable.xml"
        fake.extra[url] = doctype.encode() + fake.extra[url]
    meta, rows, _ = e13.load_quarter(sec.SecClient(tmp_path, ua=UA, transport=fake), BRK, as_of=date(2026, 11, 15))
    assert len(rows) == 89 and meta["applied"] == [] and meta["filed"] == "2026-08-14"   # was: the quarter refused
    assert "was not applied" in meta["notes"][0] and "DOCTYPE" in meta["notes"][0]


def test_low1_the_original_filing_still_fails_closed(tmp_path):
    url = "https://www.sec.gov/Archives/edgar/data/1067983/000119312526352200/primary_doc.xml"
    cover = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY n "INJECTED">]><edgarSubmission/>'
    with pytest.raises(ValidationError):
        e13.load_quarter(client(tmp_path, COPYCAT, extra={url: cover}), BRK, as_of=date(2026, 11, 15))


# ---- M2: the DOCTYPE refusal holds in any encoding -------------------------------------------------------------------
DOC = ('<?xml version="1.0"?><!DOCTYPE t [<!ENTITY n "INJECTED NAME">]><informationTable><infoTable>'
       '<nameOfIssuer>&n;</nameOfIssuer><cusip>037833100</cusip><value>5</value><sshPrnamt>1</sshPrnamt></infoTable>'
       '</informationTable>')


@pytest.mark.parametrize("data", [
    DOC.encode("utf-16"),                                          # OSDev6's: with a byte-order mark
    DOC.replace('version="1.0"', 'version="1.0" encoding="UTF-16LE"').encode("utf-16-le"),
    DOC.replace('version="1.0"', 'version="1.0" encoding="UTF-16BE"').encode("utf-16-be"),
    DOC.replace('<!DOCTYPE t [<!ENTITY n "INJECTED NAME">]>', "<!DOCTYPE informationTable>").replace("&n;", "A")
    .encode("utf-16"),                                             # a DOCTYPE alone, no entity
    ('<?xml version="1.0"?><!DOCTYPE t [<!ENTITY n "X">]><edgarSubmission><formData><coverPage><amendmentType>&n;'
     '</amendmentType></coverPage></formData></edgarSubmission>').encode("utf-16"),
])
def test_m2_a_doctype_is_refused_in_any_encoding(data):
    for parse in (e13.parse_info_table, e13.parse_cover):
        with pytest.raises(ValidationError) as e:                  # was: issuer "INJECTED NAME"
            parse(data)
        assert e.value.code is ErrorCode.INVALID_INPUT


def test_m2_broken_xml_is_still_reported_as_broken_not_as_a_doctype():
    with pytest.raises(e13.EdgarError) as e:
        e13.parse_info_table("<informationTable><infoTable>".encode("utf-16"))
    assert e.value.code is ErrorCode.INTEGRITY_MISMATCH


def test_m2_an_encoding_the_parser_cannot_read_is_refused_too():
    with pytest.raises((ValidationError, e13.EdgarError)):
        e13.parse_info_table(DOC.encode("utf-32"))


def test_m2_utf16_without_a_doctype_still_parses():
    plain = DOC.replace('<!DOCTYPE t [<!ENTITY n "INJECTED NAME">]>', "").replace("&n;", "APPLE INC")
    assert e13.parse_info_table(plain.encode("utf-16"))[0]["issuer"] == "APPLE INC"


# ---- LOW 2: text where a number belongs never decides a row ---------------------------------------------------------
def test_low2_text_in_a_numeric_metric_is_unknown_not_a_verdict(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("ticker,net_margin,roe,sector\nAAA,#DIV/0!,n/a,Tech\nBBB,27.2,12,Tech\nCCC,25,#N/A,Banks\n")
    rows = scr.load_metrics(p)
    for rule, want in ((("net_margin", "!=", 0), {"AAA": None, "BBB": True, "CCC": True}),   # was: AAA passed
                       (("net_margin", ">", 10), {"AAA": None, "BBB": True, "CCC": True}),   # was: AAA failed
                       (("roe", "==", 12), {"AAA": None, "BBB": True, "CCC": None}),        # a column of numbers
                       (("sector", "==", "tech"), {"AAA": True, "BBB": True, "CCC": False})):  # text: its compare
        spec = {"rules": [dict(zip(("metric", "op", "value"), rule, strict=True))]}
        assert {r["ticker"]: r["passed"] for r in scr.screen(rows, spec)} == want, rule
    record, _ = scr.run(scr.load_metrics(p), {"rules": [{"metric": "net_margin", "op": "!=", "value": 0}]}, now=T0)
    aaa = next(r for r in record.rows if r.ticker == "AAA")
    assert aaa.passed is None and aaa.reasons == ("net_margin is '#DIV/0!', not a number",)


# ---- LOW 3: the holdings text shows every position, as the upstream one did
# ---------------------------------------------------
def test_low3_holdings_text_shows_every_position_unless_top_is_given():
    code, text, err = main("holdings", BRK, "--combine", "--as-of", "2026-11-01")
    assert code == 0, err
    assert "\n 26  DHI " in text and "more" not in text          # was: 20 rows and "... 6 more"
    code, text, _ = main("holdings", BRK, "--combine", "--top", "5", "--as-of", "2026-11-01")
    assert "... 21 more" in text


# ---- LOW 4: with no exhibit found, the 8-K line links the filing index, as the upstream one did
# --------------------------------
def test_low4_the_earnings_8k_falls_back_to_its_index(tmp_path):
    r = facts.fetch(client(tmp_path, ANALYST), "AAPL", as_of=date(2026, 11, 1))
    acc = r.earnings_release.accession
    bare = dataclasses.replace(r, earnings_release=dataclasses.replace(r.earnings_release, exhibit_url=None))
    index = f"https://www.sec.gov/Archives/edgar/data/320193/{acc.replace('-', '')}/{acc}-index.htm"
    assert index in facts.describe(bare) and r.earnings_release.url not in facts.describe(bare)
    assert r.earnings_release.exhibit_url in facts.describe(r)


# ---- LOW 5: a redirect never carries the person's identification off the SEC ----------------------------------------
@pytest.mark.parametrize("target", ["https://example.org/x", "http://www.sec.gov/x", "https://www.sec.gov:8443/x",
                                    "https://user@www.sec.gov/x", "https://sec.gov.example.org/x"])
def test_low5_a_redirect_off_the_sec_is_refused(target):
    req = urllib.request.Request("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": UA})
    with pytest.raises(ValidationError) as e:
        sec._SecRedirectsOnly().redirect_request(req, io.BytesIO(), 302, "Found", {}, target)
    assert e.value.code is ErrorCode.OUTSIDE_CAPABILITY


def test_low5_a_redirect_inside_the_sec_is_followed_and_the_transport_uses_that_opener(monkeypatch):
    req = urllib.request.Request("https://www.sec.gov/a", headers={"User-Agent": UA})
    nxt = sec._SecRedirectsOnly().redirect_request(req, io.BytesIO(), 301, "Moved", {}, "https://data.sec.gov/b")
    assert nxt.full_url == "https://data.sec.gov/b"
    assert any(isinstance(h, sec._SecRedirectsOnly) for h in sec._OPENER.handlers)
    seen = []

    class Opener:
        def open(self, request, timeout=None):
            seen.append(request.full_url)
            raise sec.urllib.error.URLError("offline")
    monkeypatch.setattr(sec, "_OPENER", Opener())
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("urlopen follows any redirect"))
    with pytest.raises(sec.DataError):
        URLLIB_TRANSPORT("https://www.sec.gov/x", {"User-Agent": UA})
    assert seen == ["https://www.sec.gov/x"]


# ---- LOW 6: OSDev6's surviving mutants, pinned ----------------------------------------------------------------------
def test_low6_a_13f_filed_on_the_as_of_day_is_public_that_day():
    sub = {"filings": {"recent": {"form": ["13F-HR", "13F-HR", "13F-HR"], "accessionNumber": ["a", "b", "c"],
                                  "filingDate": ["2026-08-14", "2026-08-15", "not a day"],
                                  "reportDate": ["2026-06-30"] * 3, "primaryDocument": ["p"] * 3}}}
    assert [f["accession"] for f in e13.list_filings(sub, as_of=date(2026, 8, 14))] == ["a"]
    assert [f["accession"] for f in e13.list_filings(sub, as_of=date(2026, 8, 15))] == ["b", "a"]


def test_low6_a_fact_with_no_readable_filing_date_is_never_read():
    fact = {"start": "2026-01-01", "end": "2026-03-31", "val": 100, "form": "10-Q", "accn": "x", "fy": 2026,
            "fp": "Q1"}
    for filed in ("", "2026-13-01", "soon"):
        for as_of in (None, date(2027, 1, 1)):
            assert facts.periods([dict(fact, filed=filed)], as_of) == {}, (filed, as_of)
    assert list(facts.periods([dict(fact, filed="2026-05-01")], date(2026, 5, 1))) == [("2026-01-01", "2026-03-31")]


# ---- LOW 7: records say the ticker list is today's -------------------------------------------------------------------
def test_low7_records_say_tickers_come_from_todays_list(tmp_path):
    past = facts.fetch(client(tmp_path, ANALYST), "AAPL", as_of=date(2026, 6, 1), filings=False)
    assert "TICKERS_TODAY" in {lim.code for lim in past.limitations}
    by_cik = facts.fetch(client(tmp_path / "2", ANALYST), cik="320193", as_of=date(2026, 6, 1), filings=False)
    today = facts.fetch(client(tmp_path / "3", ANALYST), "AAPL", as_of=T0.date(), filings=False)
    assert "TICKERS_TODAY" not in {lim.code for lim in (*by_cik.limitations, *today.limitations)}
    _, text, _ = main("holdings", BRK, "--as-of", "2026-11-01", as_json=True)
    assert "TICKERS_TODAY" in {lim["code"] for lim in json.loads(text)["result"]["limitations"]}
    rows = scr.from_reports([past])
    assert "TICKERS_TODAY" in {lim.code for lim in scr.run(rows, GROWTH, as_of=date(2026, 6, 1), now=T0)[0]
                               .limitations}
    assert "TICKERS_TODAY" not in {lim.code for lim in scr.run(scr.from_reports([today]), GROWTH, as_of=T0.date(),
                                                               now=T0)[0].limitations}


# ---- named changes with a behaviour to pin ---------------------------------------------------------------------------
def test_your_cusip_map_gives_tickers_as_text(tmp_path):
    p = tmp_path / "map.json"
    p.write_text(json.dumps({"650111107": 7}))
    with pytest.raises(ValidationError) as e:                    # Upstream: AttributeError
        e13.load_overrides([p])
    assert "650111107" in e.value.message
    p.write_text(json.dumps({"650111107": "nyt", "not-a-cusip": 7}))
    assert e13.load_overrides([p]) == {"650111107": "NYT"}       # its rule: other keys are not CUSIPs, skipped


def test_a_large_information_table_is_read_whole():
    rows = "".join(f"<infoTable><nameOfIssuer>C{i}</nameOfIssuer><cusip>{i:09d}</cusip><value>1</value>"
                   f"<sshPrnamt>1</sshPrnamt></infoTable>" for i in range(20_001))
    assert len(e13.parse_info_table(f"<informationTable>{rows}</informationTable>".encode())) == 20_001


# ---- rules of the port that the committed mutant list found unpinned ------------------------------------------------
def _fact(start, end, val, filed=None):
    return {"start": start, "end": end, "val": val, "fy": 2026, "fp": "Q1", "form": "10-Q", "filed": filed or end,
            "accn": "a" + end}


def test_upstream_quarter_is_80_to_100_days_at_both_ends():
    # 2026-01-01 + 79, 80, 100 and 101 days
    q = facts.quarterly([_fact("2026-01-01", end, 1) for end in ("2026-03-21", "2026-03-22", "2026-04-11",
                                                                  "2026-04-12")])
    assert sorted(q) == ["2026-03-22", "2026-04-11"]


@pytest.mark.parametrize("val", [float("nan"), "abc", D("Infinity"), None, True])
def test_a_value_that_is_not_a_finite_number_is_skipped(val):
    q = facts.quarterly([_fact("2026-01-01", "2026-03-31", val), _fact("2026-04-01", "2026-06-30", D("5"))])
    assert list(q) == ["2026-06-30"] and q["2026-06-30"]["value"] == 5


def test_min_pct_skips_a_position_under_it_and_keeps_one_at_it():
    rows = [{"issuer": n, "title": "COM", "cusip": c, "figi": "", "value": D(v), "shares": D(1), "share_type": "SH",
             "put_call": "", "manager": ""}
            for n, c, v in (("A", "000000001", "9800"), ("B", "000000002", "100"), ("C", "000000003", "99.9"))]
    positions, _ = e13.aggregate(rows)                            # 98.00, 1.00 and 1.00 (0.999 rounds up) % of it
    for p in positions:
        p["ticker"] = p["issuer"]
    weights, skipped = e13.copy_weights(positions, min_pct=D("1"))
    assert [w["ticker"] for w in weights] == ["A", "B", "C"]      # its rule: the table figure, 1.00, is not under 1
    weights, skipped = e13.copy_weights(positions, min_pct=D("1.01"))
    assert [w["ticker"] for w in weights] == ["A"] and [s["why"] for s in skipped] == ["below --min-pct 1.01"] * 2


def test_at_most_500_companies_in_one_screen(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("ticker,pe\n" + "".join(f"T{i},1\n" for i in range(500)))
    assert len(scr.load_metrics(p)) == 500
    p.write_text("ticker,pe\n" + "".join(f"T{i},1\n" for i in range(501)))
    with pytest.raises(ValidationError) as e:
        scr.load_metrics(p)
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED
