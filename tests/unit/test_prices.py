"""`traderos data fetch` (ADR-014): the upstream Yahoo and Stooq fetch, ported, behind the adapter's allowlist and caps.

Offline only: every answer is a recorded fixture (tests/fixtures/prices/README.md) served by a fake transport, and
tests/conftest.py fails any test that reaches the real transport.
"""
from __future__ import annotations

import gzip
import io
import json
import pathlib
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from traderos import cli
from traderos.adapters import prices
from traderos.contracts import DataError, ErrorCode, Interval, SourceKind, ValidationError
from traderos.data import ImportOptions, build
from traderos.store import Store

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "prices"
DAILY = gzip.decompress((FIX / "yahoo-NVDA-5y1d.json.gz").read_bytes()).decode()
M5 = gzip.decompress((FIX / "yahoo-NVDA-60d5m.json.gz").read_bytes()).decode()
NO_SUCH = (FIX / "yahoo-NOSUCHTICKERZZ-404.json").read_text()
STOOQ = gzip.decompress((FIX / "stooq-NVDA-1d-SHAPE-ONLY.csv.gz").read_bytes()).decode()
NOW = datetime(2026, 11, 21, 18, 27, tzinfo=UTC)          # when the fixtures were recorded (a Saturday)


class Fake:
    """A transport: answers by host (a list is served in order, its last answer repeating), records every call."""

    def __init__(self, **by_host):
        self.by_host = {h.replace("_", "."): (a if isinstance(a, list) else [a]) for h, a in by_host.items()}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        prices.check_url(url)
        self.calls.append((url, headers))
        answers = self.by_host.get(prices._host(url))
        if not answers:
            raise AssertionError(f"unexpected request: {url}")
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return status, body.encode() if isinstance(body, str) else body

    def hosts(self):
        return [prices._host(u) for u, _ in self.calls]


def yahoo(text=DAILY, status=200):
    return Fake(query2_finance_yahoo_com=(status, text), query1_finance_yahoo_com=(status, text))


def edited(text, fn) -> str:
    doc = json.loads(text)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def fetch_into(tmp_path, transport, symbols=("NVDA",), **kw):
    kw.setdefault("now", NOW)
    return prices.fetch_into(Store(tmp_path / "home").init(), list(symbols), transport=transport,
                             sleep=lambda s: None, **kw)


# ---- parse -------------------------------------------------------------------------------------------------------
def test_upstream_names_and_urls_are_kept():
    assert prices.YAHOO_HOSTS == ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
    assert prices.yahoo_symbol("brk.b") == "BRK-B"
    assert prices.yahoo_url("BRK.B", rng="60d", interval="5m") == \
        "https://query2.finance.yahoo.com/v8/finance/chart/BRK-B?range=60d&interval=5m"
    assert prices.stooq_url("BRK.B") == "https://stooq.com/q/d/l/?s=brk-b.us&i=d"


def test_the_daily_chart_parses_into_the_csv_contract_and_through_the_gate():
    raw = prices.chart_csv(DAILY, "NVDA", interval=Interval.D1, adjustment="split")
    lines = raw.decode().splitlines()
    assert lines[0] == prices.HEADER + ",adjustment"
    assert lines[1].startswith("NVDA,2021-11-22T14:30:00Z,") and lines[1].endswith(",true,split")
    manifest, bars = build(raw, ImportOptions(name="t", interval=Interval.D1), now=NOW)
    assert manifest.row_count == len(bars) == 1255
    assert manifest.adjustment.value == "split" and all(b.complete for b in bars)
    assert abs(float(bars[-1].close) - 125.53) < 1e-4          # Yahoo sends float32-rounded prices


def test_the_5m_chart_leaves_out_yahoos_closing_print_stub():
    left = Counter()
    raw = prices.chart_csv(M5, "NVDA", interval=Interval.M5, left_out=left)
    assert left == {"closing_print": 1}
    assert "2026-11-20T21:00:00Z" not in raw.decode()
    manifest, _ = build(raw, ImportOptions(name="t", interval=Interval.M5), now=NOW)
    assert manifest.row_count == 4680
    assert not [i for i in manifest.quality_issues if i.code == "OUTSIDE_SESSION"]


def test_a_bar_in_progress_is_kept_incomplete():
    def mid_session(res):                      # Yahoo's clock at 11:02 New York on the last day
        res["meta"]["regularMarketTime"] = int(datetime(2026, 11, 20, 16, 2, tzinfo=UTC).timestamp())
    last5 = prices.chart_csv(edited(M5, mid_session), "NVDA", interval=Interval.M5).decode().splitlines()
    by_ts = {ln.split(",")[1]: ln.split(",")[7] for ln in last5[1:]}
    assert by_ts["2026-11-20T15:55:00Z"] == "true" and by_ts["2026-11-20T16:00:00Z"] == "false"
    daily = prices.chart_csv(edited(DAILY, mid_session), "NVDA", interval=Interval.D1).decode().splitlines()
    assert daily[-1].endswith(",false") and daily[-2].endswith(",true")


def test_a_bar_with_a_missing_price_is_left_out_and_counted():
    def hole(res):
        res["indicators"]["quote"][0]["close"][5] = None
    left = Counter()
    raw = prices.chart_csv(edited(DAILY, hole), "NVDA", interval=Interval.D1, left_out=left)
    assert left == {"no_price": 1} and len(raw.decode().splitlines()) == 1 + 1254


def test_a_late_duplicate_of_the_day_wins_as_in_upstream_daily_closes():
    def tick(res):                             # the live tick: a second bar for the last day, later in the day
        res["timestamp"].append(res["timestamp"][-1] + 3600)
        for k, v in res["indicators"]["quote"][0].items():
            v.append(v[-1] + (1 if k != "volume" else 0))
    left = Counter()
    lines = prices.chart_csv(edited(DAILY, tick), "NVDA", interval=Interval.D1, left_out=left).decode().splitlines()
    assert left == {"earlier_duplicate": 1} and len(lines) == 1 + 1255
    assert lines[-1].split(",")[1] == "2026-11-20T15:30:00Z"


def test_stooqs_daily_csv_parses_and_its_no_data_answers_are_typed():
    rows = prices.parse_stooq(STOOQ, "NVDA")
    assert len(rows) == 1255 and rows[0][0].isoformat() == "2021-11-22"
    for body in ("No data", "Symbol,Date,Time,Open,High,Low,Close,Volume\nZZZZ.US,N/D,N/D,N/D,N/D,N/D,N/D,N/D",
                 "Date,Open,High,Low,Close,Volume\n", "<html>busy</html>"):
        with pytest.raises(DataError) as e:
            prices.parse_stooq(body, "ZZZZ")
        assert e.value.code is ErrorCode.MISSING_DATA and "ZZZZ" in e.value.message


# ---- the adapter's walls -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["https://example.com/v8/finance/chart/NVDA", "http://query2.finance.yahoo.com/x",
                                 "https://query2.finance.yahoo.com:8443/x", "https://u@stooq.com/q/d/l/",
                                 "https://finance.yahoo.com.example.net/x", "file:///etc/passwd"])
def test_a_host_not_on_the_allowlist_is_refused_before_any_request(url, monkeypatch):
    def opened(*a, **k):
        raise AssertionError("the request was made")
    monkeypatch.setattr(prices._OPENER, "open", opened)
    with pytest.raises(ValidationError) as e:
        prices.urllib_transport(url, {})
    assert e.value.code is ErrorCode.OUTSIDE_CAPABILITY


def test_a_redirect_elsewhere_is_refused_and_one_to_an_allowed_host_is_followed():
    handler = prices._AllowedRedirectsOnly()
    req = urllib.request.Request("https://query2.finance.yahoo.com/v8/finance/chart/NVDA")
    with pytest.raises(ValidationError) as e:
        handler.redirect_request(req, None, 302, "Found", {}, "https://consent.example.com/collect")
    assert e.value.code is ErrorCode.OUTSIDE_CAPABILITY and "consent.example.com" in e.value.message
    nxt = handler.redirect_request(req, None, 302, "Found", {}, "https://query1.finance.yahoo.com/v8/finance/chart/X")
    assert nxt.full_url.startswith("https://query1.finance.yahoo.com/")


@pytest.mark.parametrize("raised", [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out")),
                                    urllib.error.URLError(ConnectionRefusedError()), ConnectionResetError()])
def test_no_answer_is_a_typed_refusal_never_a_crash(raised, monkeypatch):
    def opened(req, timeout):
        assert timeout == prices.TIMEOUT_S
        raise raised
    monkeypatch.setattr(prices._OPENER, "open", opened)
    with pytest.raises(DataError) as e:
        prices.urllib_transport("https://query2.finance.yahoo.com/v8/finance/chart/NVDA", {})
    assert e.value.code is ErrorCode.MISSING_DATA and "nothing was stored" in e.value.message
    if isinstance(getattr(raised, "reason", raised), TimeoutError):
        assert f"did not answer within {prices.TIMEOUT_S} s" in e.value.message


class _Resp(io.BytesIO):
    def __init__(self, data, encoding=None):
        super().__init__(data)
        self.status, self.headers = 200, {"Content-Encoding": encoding} if encoding else {}


def test_the_size_cap_holds_before_and_after_gzip(monkeypatch):
    monkeypatch.setattr(prices, "MAX_BYTES", 1000)
    url = "https://query2.finance.yahoo.com/v8/finance/chart/NVDA"
    monkeypatch.setattr(prices._OPENER, "open", lambda req, timeout: _Resp(b"x" * 1001))
    with pytest.raises(DataError) as e:
        prices.urllib_transport(url, {})
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED
    bomb = gzip.compress(b"0" * 100_000)                     # 100 KB that travels as a few hundred bytes
    assert len(bomb) < 1000
    monkeypatch.setattr(prices._OPENER, "open", lambda req, timeout: _Resp(bomb, "gzip"))
    with pytest.raises(DataError) as e:
        prices.urllib_transport(url, {})
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED
    monkeypatch.setattr(prices._OPENER, "open", lambda req, timeout: _Resp(gzip.compress(b'{"ok":1}'), "gzip"))
    assert prices.urllib_transport(url, {}) == (200, b'{"ok":1}')


def test_the_user_agent_is_generic_and_names_traderos_and_its_version(tmp_path):
    from traderos import __version__
    fake = yahoo()
    fetch_into(tmp_path, fake)
    ua = fake.calls[0][1]["User-Agent"]
    assert f"TraderOS/{__version__}" in ua and "@" not in ua and "http" not in ua


# ---- the fetch ---------------------------------------------------------------------------------------------------
def test_a_fetch_goes_through_the_import_path_and_carries_unofficial_data(tmp_path):
    manifest, got = fetch_into(tmp_path, yahoo())
    assert got.source is SourceKind.YAHOO_CHART and got.fallback_reason is None
    assert manifest.dataset_id.startswith("DS-") and manifest.row_count == 1255
    assert manifest.source_kind is SourceKind.YAHOO_CHART and manifest.provider == "yahoo-chart"
    assert manifest.recorded_at == NOW and "fetched 2026-11-21" in manifest.name
    codes = [lim.code for lim in manifest.limitations]
    assert "UNOFFICIAL_DATA" in codes and ErrorCode.UNOFFICIAL_DATA.value == "UNOFFICIAL_DATA"
    assert "free and unofficial" in next(lim.detail for lim in manifest.limitations
                                         if lim.code == "UNOFFICIAL_DATA").lower()
    assert not manifest.rights.export_raw_allowed and not manifest.rights.redistribution_allowed
    assert manifest.rights.stated_by == "traderos"
    store = Store(tmp_path / "home")
    assert store.bar_reader().manifest(manifest.dataset_id) == manifest


def test_429_waits_and_retries_then_query1_answers():
    waits = []
    fake = Fake(query2_finance_yahoo_com=(429, ""), query1_finance_yahoo_com=(200, DAILY))
    assert prices.fetch_yahoo("NVDA", transport=fake, sleep=waits.append) == DAILY
    assert fake.hosts() == ["query2.finance.yahoo.com"] * 2 + ["query1.finance.yahoo.com"]
    assert waits == [prices.BACKOFF]


def test_daily_falls_back_to_stooq_when_yahoo_cannot_answer_and_says_so(tmp_path):
    fake = Fake(query2_finance_yahoo_com=(503, "busy"), query1_finance_yahoo_com=DataError(
        "query1.finance.yahoo.com did not answer within 20 s; nothing was stored", code=ErrorCode.MISSING_DATA),
        stooq_com=(200, STOOQ))
    manifest, got = fetch_into(tmp_path, fake, rng="1y")
    assert fake.hosts() == ["query2.finance.yahoo.com", "query1.finance.yahoo.com", "stooq.com"]
    assert got.source is SourceKind.STOOQ and "did not answer" in got.fallback_reason
    assert manifest.source_kind is SourceKind.STOOQ and manifest.provider == "stooq"
    assert "Stooq" in next(lim.detail for lim in manifest.limitations if lim.code == "UNOFFICIAL_DATA")
    first = min(c.first_bar_end for c in manifest.coverage)
    assert first.date() >= (NOW - timedelta(days=366)).date()            # trimmed to the asked range
    assert manifest.adjustment.value == "unknown"                         # Stooq does not say: ADJUSTMENT_UNKNOWN


def test_the_fallback_failing_too_names_both_and_stores_nothing(tmp_path):
    fake = Fake(query2_finance_yahoo_com=(500, ""), query1_finance_yahoo_com=(500, ""), stooq_com=(200, "No data"))
    with pytest.raises(DataError) as e:
        fetch_into(tmp_path, fake)
    assert "HTTP 500" in e.value.message and "Stooq has no data for NVDA" in e.value.message
    assert [m for m in Store(tmp_path / "home").datasets() if m.source_kind is not SourceKind.TUTORIAL] == []


def test_5m_has_no_stooq_fallback(tmp_path):
    fake = Fake(query2_finance_yahoo_com=(503, ""), query1_finance_yahoo_com=(503, ""))
    with pytest.raises(DataError) as e:
        fetch_into(tmp_path, fake, interval="5m")
    assert e.value.code is ErrorCode.MISSING_DATA and "no 5-minute bars" in e.value.message
    assert "stooq.com" not in fake.hosts()


def test_5m_fetch_defaults_to_60_days_and_stores_a_5m_dataset(tmp_path):
    fake = yahoo(M5)
    manifest, got = fetch_into(tmp_path, fake, interval="5m")
    assert "range=60d&interval=5m" in fake.calls[0][0]
    assert manifest.interval is Interval.M5 and manifest.row_count == 4680 and got.left_out == {"closing_print": 1}


def test_a_bad_symbol_is_typed_with_yahoos_reason_and_no_fallback(tmp_path):
    fake = Fake(query2_finance_yahoo_com=(404, NO_SUCH), stooq_com=(200, STOOQ))
    with pytest.raises(DataError) as e:
        fetch_into(tmp_path, fake, symbols=("NOSUCHTICKERZZ",))
    assert e.value.code is ErrorCode.MISSING_DATA and "No data found" in e.value.message
    assert fake.hosts() == ["query2.finance.yahoo.com"]
    for bad in ("EURUSD=X", "1ABC", "", "A" * 16):
        with pytest.raises(ValidationError):
            fetch_into(tmp_path, Fake(), symbols=(bad,))
    with pytest.raises(ValidationError):
        fetch_into(tmp_path, Fake(), rng="5 years")
    with pytest.raises(ValidationError):
        fetch_into(tmp_path, Fake(), interval="1h")


def test_fetching_again_replays_the_same_bars_and_a_later_day_is_a_new_dated_dataset(tmp_path):
    first, _ = fetch_into(tmp_path, yahoo())
    again, _ = fetch_into(tmp_path, yahoo(), now=NOW + timedelta(hours=2))
    assert again == first                                   # the stored dataset, replayed: no integrity failure
    def more(res):                                          # Monday: one more session
        res["timestamp"].append(res["timestamp"][-1] + 3 * 86400)
        for v in res["indicators"]["quote"][0].values():
            v.append(v[-1])
    later, _ = fetch_into(tmp_path, yahoo(edited(DAILY, more)), now=NOW + timedelta(days=2))
    assert later.dataset_id != first.dataset_id and "fetched 2026-11-23" in later.name
    same_day, _ = fetch_into(tmp_path, yahoo(edited(DAILY, more)), now=NOW + timedelta(hours=3))
    assert same_day.dataset_id not in (first.dataset_id, later.dataset_id)   # new bars, same day: a new id too
    ids = {m.dataset_id for m in Store(tmp_path / "home").datasets()}
    assert {first.dataset_id, later.dataset_id, same_day.dataset_id} <= ids


def test_several_symbols_make_one_dataset_with_a_pause_between_requests(tmp_path):
    waits = []
    fake = yahoo()
    manifest, got = prices.fetch_into(Store(tmp_path / "home").init(), ["nvda", "NVDA", "SPY"], transport=fake,
                                      sleep=waits.append, now=NOW)
    assert got.symbols == ("NVDA", "SPY") and waits == [prices.PAUSE]
    assert {i.symbol for i in manifest.instruments} == {"NVDA", "SPY"}


def test_the_cli_names_the_source_and_the_limitation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prices, "_urllib_transport", yahoo())
    monkeypatch.setattr(prices.time, "sleep", lambda s: None)
    assert cli.main(["--home", str(tmp_path / "h"), "--json", "data", "fetch", "NVDA"]) == 0
    out = json.loads(capsys.readouterr().out)["result"]
    assert out["source"] == "yahoo_chart" and out["unofficial"] is True and "UNOFFICIAL_DATA" in out["limitations"]
    assert out["dataset_id"] == out["dataset"]["dataset_id"] and out["dataset"]["rights"]["export_raw_allowed"] is False
    assert cli.main(["--home", str(tmp_path / "h"), "data", "fetch", "NVDA"]) == 0
    text = capsys.readouterr().out
    assert "Yahoo Finance's public chart endpoint" in text and "free and unofficial" in text
    monkeypatch.setattr(prices, "_urllib_transport", Fake(query2_finance_yahoo_com=(404, NO_SUCH)))
    assert cli.main(["--home", str(tmp_path / "h"), "--json", "data", "fetch", "ZZZZZ"]) == 3
    assert json.loads(capsys.readouterr().out)["failure"]["code"] == "MISSING_DATA"
