"""The SEC client under S04, S06 and S07 (traderos.adapters.sec), the commands on top, and upstream PR #49.

Everything runs offline: the upstream project's own SEC fixtures (tests/fixtures/upstream-trader-skill) are served by an
injected transport (tests/unit/sec_offline.py). The tool tests themselves are in test_analyst.py, test_copycat.py and
test_screener.py.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request
import zlib
from contextlib import redirect_stdout
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.unit.sec_offline import ANALYST, COPYCAT, T0, UA, Offline, client
from traderos import cli
from traderos.adapters import sec
from traderos.contracts import (
    AuthorizationError,
    DataError,
    ErrorCode,
    IntegrityError,
    ValidationError,
    from_json,
    verify_hash,
)
from traderos.contracts.fundamentals import EarningsReport, FundamentalScreen, HoldingsReport

URLLIB_TRANSPORT = sec._urllib_transport          # the real one, kept before conftest swaps in its network guard
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


# ---- the client ------------------------------------------------------------------------------------------------------
def test_identification_is_the_persons_own_and_never_invented(tmp_path, monkeypatch):
    with pytest.raises(AuthorizationError) as e:
        sec.user_agent(tmp_path)
    assert e.value.code is ErrorCode.NOT_AUTHORIZED
    for bad in ("no email here", "Your Name you@example.com", "YOUR NAME YOU@EXAMPLE.COM"):
        monkeypatch.setenv("SEC_USER_AGENT", bad)
        with pytest.raises(AuthorizationError):
            sec.user_agent(tmp_path)
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    assert sec.user_agent(tmp_path) == UA
    monkeypatch.delenv("SEC_USER_AGENT")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text(json.dumps({"sec_user_agent": "A B a@b.example"}))
    assert sec.user_agent(tmp_path) == "A B a@b.example"
    (tmp_path / "config" / "settings.json").write_text("{not json")
    with pytest.raises(AuthorizationError):
        sec.user_agent(tmp_path)
    addresses = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", pathlib.Path(sec.__file__).read_text()))
    assert addresses == {"you@example.com"}                       # no address of ours anywhere in the client


@pytest.mark.parametrize("url", ["http://www.sec.gov/files/x.json", "https://example.com/x.json",
                                 "https://www.sec.gov.example.com/x", "https://user@www.sec.gov/x",
                                 "https://www.sec.gov:8443/x", "file:///etc/passwd", "https://efts.sec.gov.evil/x",
                                 "https://api.openfigi.com/v3/mapping"])
def test_only_sec_hosts_over_https(tmp_path, url):
    c = client(tmp_path)
    with pytest.raises(ValidationError) as e:
        c.get(url)
    assert e.value.code is ErrorCode.OUTSIDE_CAPABILITY and c.transport.calls == []


def test_efts_is_an_sec_host_for_the_filer_search(tmp_path):
    c = client(tmp_path)
    data, _ = c.search("scion asset")
    assert c.transport.calls[0][0] == sec.SEARCH_URL + "scion%20asset"
    assert data["hits"]["hits"][1]["_id"] == "1649339"
    for bad in ("", "x" * 201):
        with pytest.raises(ValidationError):
            c.search(bad)


def test_sends_the_identification_caches_and_refetches_when_old(tmp_path):
    now = [T0]
    c = client(tmp_path, clock=lambda: now[0])
    data, fetched = c.json(sec.TICKERS_URL)
    assert c.transport.calls[0][1]["User-Agent"] == UA and fetched == T0
    assert "AAPL" in {row["ticker"] for row in data.values()}
    now[0] = T0 + timedelta(hours=11)
    assert c.get(sec.TICKERS_URL)[1] == T0 and len(c.transport.calls) == 1        # fresh enough: from the cache
    now[0] = T0 + timedelta(hours=13)
    assert c.get(sec.TICKERS_URL)[1] == now[0] and len(c.transport.calls) == 2    # too old: fetched again


def test_paces_requests_under_the_sec_limit(tmp_path):
    c = client(tmp_path)
    start = time.monotonic()
    c.get(sec.TICKERS_URL)
    c.get("https://data.sec.gov/submissions/CIK0001067983.json")
    c.get("https://www.sec.gov/Archives/edgar/data/1067983/000119312526352200/index.json")
    assert time.monotonic() - start >= 2 * sec.MIN_INTERVAL_S - 0.01


def test_json_is_exact_and_garbage_is_refused(tmp_path):
    c = sec.SecClient(tmp_path, ua=UA, transport=lambda url, h: b'{"v": 0.1}')
    assert c.json(sec.TICKERS_URL)[0]["v"] == Decimal("0.1")
    c = sec.SecClient(tmp_path / "2", ua=UA, transport=lambda url, h: b"<html>busy</html>")
    with pytest.raises(DataError) as e:
        c.json(sec.TICKERS_URL)
    assert e.value.code is ErrorCode.INTEGRITY_MISMATCH


def test_ticker_lookup_share_classes_and_unsafe_file_names(tmp_path):
    c = client(tmp_path)
    assert c.cik_for("aapl") == ("0000320193", "Apple Inc.")
    for spelling in ("BRK.B", "BRK-B", "brk/b", " brk.b "):                    # was: no BRK.B in the SEC's list
        assert c.cik_for(spelling) == ("0001067983", "BERKSHIRE HATHAWAY INC")
    assert c.cik_for("LEN.B")[0] == c.cik_for("LEN")[0] == "0000920760"
    with pytest.raises(DataError) as e:
        c.cik_for("NOPE")
    assert e.value.code is ErrorCode.MISSING_DATA
    for name in ("../x.xml", "a/b.xml", ""):
        with pytest.raises(ValidationError):
            c.filing_file("1067983", "0001193125-26-352200", name)


class _Response:
    def __init__(self, body: bytes, gz: bool = True) -> None:
        self.body, self.headers = io.BytesIO(body), {"Content-Encoding": "gzip"} if gz else {}

    def read(self, n=-1):
        return self.body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """sec._OPENER's stand-in: answers every request with one response, and reaches nothing."""

    def __init__(self, response) -> None:
        self.response, self.requests = response, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return self.response


def _serve(monkeypatch, response) -> _Opener:
    opener = _Opener(response)
    monkeypatch.setattr(sec, "_OPENER", opener)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the transport must use sec._OPENER, whose redirects stay on SEC hosts")))
    return opener


def _bomb(megabytes: int) -> bytes:
    z = zlib.compressobj(9, zlib.DEFLATED, 31)                   # gzip framing
    return b"".join(z.compress(b"\0" * (1 << 20)) for _ in range(megabytes)) + z.flush()


def test_the_size_cap_holds_after_decompression(monkeypatch):
    bomb = _bomb(126)                                            # ~128 KB on the wire, 126 MB unpacked
    assert len(bomb) < 200_000
    _serve(monkeypatch, _Response(bomb))
    with pytest.raises(DataError) as e:                          # was: 132,120,576 bytes returned past a 60 MB cap
        URLLIB_TRANSPORT("https://www.sec.gov/files/x.json", {})
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED
    monkeypatch.setattr(sec, "MAX_BYTES", 1000)
    assert sec._gunzip(gzip.compress(b"a" * 1000), "u") == b"a" * 1000             # exactly at the cap: kept
    with pytest.raises(DataError):
        sec._gunzip(gzip.compress(b"a" * 600) + gzip.compress(b"b" * 401), "u")   # two members over the cap
    assert sec._gunzip(gzip.compress(b"ab") + gzip.compress(b"cd"), "u") == b"abcd"
    for bad in (b"not gzip", gzip.compress(b"abcdef")[:-8]):
        with pytest.raises(DataError) as e:
            sec._gunzip(bad, "u")
        assert e.value.code is ErrorCode.INTEGRITY_MISMATCH


def test_an_uncompressed_body_over_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr(sec, "MAX_BYTES", 10)
    _serve(monkeypatch, _Response(b"x" * 11, gz=False))
    with pytest.raises(DataError) as e:
        URLLIB_TRANSPORT("https://www.sec.gov/files/x.json", {})
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED


def test_the_mit_notice_is_at_the_repository_root():
    assert "MIT License" in (SRC.parent / "NOTICE").read_text()


# ---- the commands ------------------------------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(sec, "MIN_INTERVAL_S", 0)
    return str(tmp_path / "home")


def run(capsys, *argv):
    code = cli.main(["--json", *argv])
    return code, json.loads(capsys.readouterr().out)


def serve(monkeypatch, folder):
    fake = Offline(folder)
    monkeypatch.setattr(sec, "_urllib_transport", fake)
    return fake


def test_commands_refuse_without_identification_and_reach_nothing(home, capsys, monkeypatch):
    fake = serve(monkeypatch, ANALYST)
    capsys.readouterr()
    for argv in (["earnings", "AAPL"], ["holdings", "1067983"], ["holdings", "--find", "x", "--sec"]):
        code, out = run(capsys, "--home", home, *argv)
        assert code == 3 and out["failure"]["code"] == ErrorCode.NOT_AUTHORIZED.value and fake.calls == []
    code, out = run(capsys, "--home", home, "holdings", "--find", "buffett")      # the bundled list: no network
    assert code == 0 and out["result"][0]["cik"] == "0001067983" and fake.calls == []
    code, out = run(capsys, "--home", home, "doctor")
    sec_check = next(c for c in out["result"]["checks"] if c["check"] == "sec")
    assert "not set" in sec_check["detail"] and out["result"]["ok"] is True


def test_config_then_earnings_holdings_and_screen(home, capsys, monkeypatch, tmp_path):
    for bad in ("no-email", "Your Name you@example.com"):
        assert run(capsys, "--home", home, "config", "set", "sec_user_agent", bad)[0] == 3
    assert run(capsys, "--home", home, "config", "set", "sec_user_agent", UA)[0] == 0
    settings = pathlib.Path(home) / "config" / "settings.json"
    assert settings.stat().st_mode & 0o777 == 0o600
    fake = serve(monkeypatch, ANALYST)
    code, out = run(capsys, "--home", home, "earnings", "AAPL", "--as-of", "2026-11-01")
    assert code == 0 and len(out["result"]["quarters"]) == 6
    assert all(headers["User-Agent"] == UA for _, headers in fake.calls)
    report = from_json(EarningsReport, json.dumps(out["result"]))
    code, again = run(capsys, "--home", home, "earnings", "AAPL", "--as-of", "2026-11-01")
    assert code == 0 and again["result"]["report_id"] == report.report_id          # cached: the same record
    code, both = run(capsys, "--home", home, "earnings", "AAPL", "KO", "--as-of", "2026-11-01", "--quarters", "2")
    assert code == 0 and [r["ticker"] for r in both["result"]] == ["AAPL", "KO"]
    assert all(len(r["quarters"]) == 2 for r in both["result"])
    code, out = run(capsys, "--home", home, "earnings", "--cik", "320193", "--as-of", "2026-11-01", "--no-filings")
    assert code == 0 and out["result"]["latest_report"] is None and out["result"]["ticker"] is None
    serve(monkeypatch, COPYCAT)
    home = home + "-13f"                        # The upstream two fixture sets carry different SEC ticker lists
    assert run(capsys, "--home", home, "config", "set", "sec_user_agent", UA)[0] == 0
    code, out = run(capsys, "--home", home, "holdings", "1067983", "--as-of", "2026-11-01", "--combine")
    held = from_json(HoldingsReport, json.dumps(out["result"]))
    assert code == 0 and held.total_value_usd == Decimal("299253556246") and held.combined and len(held.holdings) == 26
    assert run(capsys, "--home", home, "holdings", "BRK.B", "--as-of", "2026-11-01", "--combine")[0] == 0
    code, out = run(capsys, "--home", home, "holdings", "1067983", "--filings", "--as-of", "2026-06-01")
    assert code == 0 and out["result"]["filings"][0]["filed"] == "2026-05-15"
    code, out = run(capsys, "--home", home, "holdings", "1067983", "--diff", "--as-of", "2026-11-01")
    assert code == 0 and out["result"]["prior_period_of_report"] == "2026-03-31"
    code, out = run(capsys, "--home", home, "holdings", "1067983", "--copy", "--top", "3", "--exclude", "axp",
                    "--min-pct", "1", "--as-of", "2026-11-01")
    assert code == 0 and [w["ticker"] for w in out["result"]["weights"]] == ["AAPL", "GOOGL", "KO"]
    assert any(s["why"] == "excluded by you" for s in out["result"]["skipped"])
    serve(monkeypatch, ANALYST)
    home = home.removesuffix("-13f")
    rules = tmp_path / "r.json"
    rules.write_text(json.dumps({"rules": [{"metric": "net_margin", "op": ">=", "value": 25}]}))
    code, out = run(capsys, "--home", home, "fundamentals", "screen", "--tickers", "AAPL,KO", "--criteria",
                    str(rules), "--as-of", "2026-11-01")
    screen = from_json(FundamentalScreen, json.dumps(out["result"]))
    assert code == 0 and [r.passed for r in screen.rows] == [True, True]


@pytest.mark.parametrize("argv", [
    ["earnings", "AAPL", "--as-of", "yesterday"], ["earnings", "AAPL", "aapl"], ["earnings"],
    ["earnings", "AAPL", "--quarters", "0"], ["earnings", "AAPL", "--cik", "320193"], ["earnings", "--cik", "x1"],
    ["holdings"], ["holdings", "1067983", "--min-pct", "-1"], ["holdings", "1067983", "--min-pct", "nan"],
    ["holdings", "1067983", "--top", "0"], ["holdings", "1067983", "--period", "June"],
    ["holdings", "12345678901"],
])
def test_bad_input_is_refused_before_any_request(home, capsys, monkeypatch, argv):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    fake = serve(monkeypatch, ANALYST)
    code, out = run(capsys, "--home", home, *argv)
    assert code == 3 and out["ok"] is False and fake.calls == []


def test_holdings_refuses_diff_and_copy_together(home, capsys):
    assert cli.main(["--home", home, "holdings", "1067983", "--diff", "--copy"]) == 2


# ---- upstream PR #49 (the review's cases, PR #49 section 10) -------------------------------------------------------
def _subprocess(*argv, home):
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONPATH=str(SRC), TRADEROS_HOME=str(home),
               SEC_USER_AGENT=UA)
    return subprocess.run([sys.executable, "-m", "traderos.cli", *argv], env=env, capture_output=True, timeout=60)


def test_pr49_a_character_outside_the_console_code_page_still_prints(tmp_path):
    rocket = "\U0001F680"
    done = _subprocess("config", "set", "sec_user_agent", f"Ada {rocket} ada@example.org", home=tmp_path)
    assert done.returncode == 0, done.stderr
    done = _subprocess("config", "show", home=tmp_path)
    assert done.returncode == 0, done.stderr                     # was: UnicodeEncodeError under cp1252
    assert b"\xf0\x9f\x9a\x80" in done.stdout


def test_pr49_an_error_message_goes_to_stderr_as_utf8_with_the_normal_code(tmp_path):
    done = _subprocess("earnings", "AAPL", "--as-of", "\U0001F680", home=tmp_path)
    assert done.returncode == 3 and b"Traceback" not in done.stderr
    assert b"\xf0\x9f\x9a\x80" in done.stderr


def test_pr49_main_in_process_under_redirect_stdout(tmp_path):
    out = io.StringIO()
    with redirect_stdout(out):                                   # StringIO has no reconfigure: no AttributeError
        code = cli.main(["--home", str(tmp_path), "capabilities"])
    assert code == 0 and "S04" in out.getvalue()


# ---- contracts v1.3 is additive: a record written by the pre-port build still reads ---------------------------------
@pytest.mark.parametrize("name", ["earnings_v1", "holdings_v1", "fundamental-screen_v1"])
def test_a_v1_2_record_still_parses(name):
    from traderos.contracts import RECORD_TYPES
    golden = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "compat-v1.2" / f"{name}.json"
    text = golden.read_text(encoding="utf-8")
    record = from_json(RECORD_TYPES[json.loads(text)["schema_version"]], text)
    # ADR-011, stated consequence: v1.3's canonical form lists the new fields (as null), so the v1.2 hash no longer
    # verifies. No release ever stored one (S04/S06/S07 are experimental on an unmerged branch).
    with pytest.raises(IntegrityError):
        verify_hash(record)
