"""Free price bars: Yahoo Finance's public chart endpoint, with Stooq as the daily fallback (ADR-014).

It runs only when a person runs `traderos data fetch` (an explicitly named adapter command: docs/cli-contract-v1.md).

Ported from the upstream day-trader-skill (tank/day-trader-skill at 3d0f4ef, MIT; NOTICE), its logic and
names kept where they fit:
  * trader-alerts/alerts.py: YAHOO_HOSTS, BACKOFF, PAUSE, `_get`, `yahoo_url`, `fetch_yahoo`, `parse_yahoo`,
    `stooq_url`, `parse_stooq`;
  * trader-entry/entry.py: `yahoo_symbol`, and `yahoo_get`'s move to query1 when query2 cannot be reached;
  * trader-scanner/scan.py: `series_from_result` (the chart's columns) and `daily_closes`' rule for today's bar.
Every change, with its reason, is in docs/adr/ADR-014-data-fetch.md. The chart-to-CSV step was the test helper
tests/unit/tm_fixture.py's, moved here.

Both sources are free and unofficial (the upstream README): neither licenses its data or promises a service. Every
dataset built from them carries UNOFFICIAL_DATA, and its rights label allows neither raw export nor redistribution.

The adapter, like the SEC and Alpaca ones:
  * talks only to query2.finance.yahoo.com, query1.finance.yahoo.com and stooq.com over HTTPS. The host is checked
    before every request and again in the transport, and a redirect is followed only to one of them;
  * has a timeout, and a size cap that holds after gzip decompression too;
  * raises typed errors: a timeout or a refused connection is a typed refusal (MISSING_DATA), never a crash;
  * sends only the public URL and a generic User-Agent naming TraderOS and its version, never anything about the
    person or their research.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime

from traderos import __version__
from traderos.adapters.sec import _gunzip
from traderos.clock import XNYS, eastern_to_utc
from traderos.contracts import (
    DataError,
    ErrorCode,
    Interval,
    Limitation,
    SourceKind,
    TraderOSError,
    ValidationError,
)
from traderos.data import INTERVAL_DELTA, SYMBOL, ImportOptions, import_into

UA = "Mozilla/5.0 (compatible; TraderOS/%s)" % __version__   # generic: names no person and no address
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
STOOQ_HOST = "stooq.com"
HOSTS = (*YAHOO_HOSTS, STOOQ_HOST)
TIMEOUT_S = 20
MAX_BYTES = 16 * 1024 * 1024                     # a 60-day 5-minute chart is about 0.5 MB; daily "max" about 2 MB
INTERVALS = ("1d", "5m")                         # Stooq has no intraday bars, so 5m is Yahoo's alone
DEFAULT_RANGE = {"1d": "5y", "5m": "60d"}        # Yahoo keeps 5-minute bars for about 60 days
RANGE = re.compile(r"^(?:[1-9][0-9]{0,3}(?:d|mo|y)|ytd|max)$")
HEADER = "symbol,timestamp_utc,open,high,low,close,volume,complete"
UNOFFICIAL = {
    SourceKind.YAHOO_CHART: ("yahoo-chart", "Yahoo Finance's public chart endpoint"),
    SourceKind.STOOQ: ("stooq", "Stooq's daily CSV"),
}
RIGHTS_LABEL = "free-unofficial-source: personal research only; no export, no redistribution"
Transport = Callable[[str, dict[str, str]], tuple[int, bytes]]


# ---------------------------------------------------------------- the transport

def allowed(url: str) -> bool:
    """True for an HTTPS URL on one of HOSTS, with no user name, password or port."""
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme == "https" and parts.hostname in HOSTS and not parts.username and not parts.password
            and not parts.port)


def check_url(url: str) -> None:
    if not allowed(url):
        raise ValidationError(f"refused: {url} is not on the price adapter's allowlist ({', '.join(HOSTS)})",
                              code=ErrorCode.OUTSIDE_CAPABILITY)


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or url


class _AllowedRedirectsOnly(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another allowed address: urllib would otherwise go wherever the answer points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            raise ValidationError(f"refused: {_host(req.full_url)} redirected to {newurl}, which is not on the "
                                  "price adapter's allowlist", code=ErrorCode.OUTSIDE_CAPABILITY)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_AllowedRedirectsOnly)


def _body(resp, url: str) -> bytes:
    data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise DataError(f"{_host(url)} sent more than {MAX_BYTES} bytes; nothing was stored",
                        code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
    headers = getattr(resp, "headers", None)
    if headers is not None and (headers.get("Content-Encoding") or "").lower() == "gzip":
        data = _gunzip(data, url, MAX_BYTES)
    return data


def urllib_transport(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """(HTTP status, body). No answer at all (a timeout, a refused connection) is a typed MISSING_DATA."""
    check_url(url)                                # again, here: the transport itself never reaches another host
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            return resp.status, _body(resp, url)
    except urllib.error.HTTPError as e:           # the host answered: its status and its (capped) words
        try:
            data = _body(e, url)
        except (OSError, AttributeError, ValueError):
            data = b""
        finally:
            e.close()
        return e.code, data
    except (urllib.error.URLError, OSError) as e:  # TimeoutError is an OSError; so is a reset connection
        reason = getattr(e, "reason", e)
        why = (f"did not answer within {TIMEOUT_S} s" if isinstance(reason, TimeoutError)
               else f"could not be reached ({type(reason).__name__})")
        raise DataError(f"{_host(url)} {why}; nothing was stored", code=ErrorCode.MISSING_DATA,
                        details={"host": _host(url)}) from None


_urllib_transport = urllib_transport              # the one the fetch uses; tests swap it (tests/conftest.py)


def _get(url: str, transport: Transport | None = None) -> tuple[int, str]:
    """(HTTP status, body). A network failure is a typed DataError."""
    status, data = (transport or _urllib_transport)(url, {"User-Agent": UA, "Accept": "*/*",
                                                          "Accept-Encoding": "gzip"})
    return status, data.decode("utf-8", "replace")


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(sym):
    return sym.upper().replace(".", "-")


def yahoo_url(sym, host=YAHOO_HOSTS[0], rng="5y", interval="1d"):
    return "https://%s/v8/finance/chart/%s?range=%s&interval=%s" % (host, yahoo_symbol(sym), rng, interval)


def fetch_yahoo(sym, rng="5y", interval="1d", *, transport=None, sleep=None):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429, a 5xx or no answer after that
    moves on to query1. A 4xx that carries Yahoo's own error JSON comes back as the answer, so its reason is read."""
    sleep = sleep or time.sleep
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host, rng, interval)
        try:
            code, text = _get(url, transport)
            if code == 429:
                sleep(BACKOFF)
                code, text = _get(url, transport)
        except DataError as e:
            if e.code is not ErrorCode.MISSING_DATA:
                raise
            last = e.message.split(";")[0]
            continue
        if code == 200:
            return text
        if 400 <= code < 500 and code != 429 and text.strip().startswith("{"):
            return text
        last = "%s answered HTTP %d" % (host, code)
        if code != 429 and code < 500:
            break
    raise DataError("Yahoo did not answer for %s (%s): wait a minute and try again" % (sym, last),
                    code=ErrorCode.MISSING_DATA, details={"source": "yahoo"})


def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_yahoo(text, sym):
    """v8 chart JSON -> series. Yahoo's own "no data" answer is MISSING_DATA naming its reason."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise DataError("Yahoo returned something unexpected for %s" % sym, code=ErrorCode.INTEGRITY_MISMATCH,
                        details={"source": "yahoo"}) from None
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        why = str(err.get("description", "unknown ticker"))[:200]
        raise DataError("Yahoo has no data for %s: %s" % (sym, why), code=ErrorCode.MISSING_DATA,
                        details={"source": "yahoo", "yahoo_error": str(err.get("code", ""))[:40]})
    if not isinstance(res, list) or not isinstance(res[0], dict):
        raise DataError("Yahoo returned something unexpected for %s" % sym, code=ErrorCode.INTEGRITY_MISMATCH,
                        details={"source": "yahoo"})
    return series_from_result(res[0])


# ---------------------------------------------------------------- Stooq (daily only)

def stooq_url(sym):
    return "https://stooq.com/q/d/l/?s=%s.us&i=d" % sym.lower().replace(".", "-")


def parse_stooq(text, sym):
    """Date,Open,High,Low,Close,Volume -> [(date, open, high, low, close, volume)], oldest first. N/D means no data;
    an answer without daily rows (Stooq's "No data") is MISSING_DATA."""
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows or "date" not in {k.strip().lower() for k in rows[0] if k}:
        raise DataError("Stooq has no data for %s (check the ticker)" % sym, code=ErrorCode.MISSING_DATA,
                        details={"source": "stooq"})
    out = []
    for row in rows:
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}

        def f(k, r=r):
            v = r.get(k, "")
            try:
                return float(v)
            except ValueError:
                return None
        try:
            day = date.fromisoformat(r.get("date", ""))
        except ValueError:
            continue                              # N/D, or a line that is not a dated row
        out.append((day, f("open"), f("high"), f("low"), f("close"), f("volume")))
    if not any(row[4] is not None for row in out):
        raise DataError("Stooq has no data for %s (check the ticker)" % sym, code=ErrorCode.MISSING_DATA,
                        details={"source": "stooq"})
    return sorted(out)


# ---------------------------------------------------------------- bars -> the CSV contract (docs/data-csv-v1.md)

def _utc(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def new_york_day(ts: datetime) -> date:
    """The New York calendar day of a UTC instant."""
    for hours in (4, 5):
        local = ts - timedelta(hours=hours)
        if eastern_to_utc(local.date(), local.time()) == ts:
            return local.date()
    return (ts - timedelta(hours=5)).date()


def _lines(rows, symbol: str, interval: Interval, as_of: datetime | None, cal: XNYS, adjustment: str | None,
           left_out: Counter) -> list[str]:
    """CSV lines for rows of (start_utc, o, h, l, c, v). A bar still in progress at `as_of` (Yahoo's own
    regularMarketTime, or the fetch time) is written complete=false, as the store must keep it (spec 06.4). Left out,
    each one counted in `left_out`: a bar with a missing price (a stored bar cannot lack one); Yahoo's zero-volume
    closing-print stub that starts at the close; a bar before the XNYS calendar's coverage; and, for daily bars, an
    earlier bar of a day that has a later one (the upstream daily_closes: the late duplicate, the live tick, wins)."""
    daily = interval is Interval.D1
    as_of_day = new_york_day(as_of) if as_of is not None else None
    kept: dict = {}
    for ts, o, h, lo, c, v in rows:
        if None in (o, h, lo, c):
            left_out["no_price"] += 1
            continue
        day = new_york_day(ts)
        if day < cal.coverage[0]:
            left_out["before_calendar"] += 1
            continue
        if daily:
            complete = (as_of is None or as_of_day > day
                        or (as_of_day == day and (not cal.is_session(day) or as_of >= cal.open_close(day)[1])))
        else:
            if not v and cal.is_session(day) and ts == cal.open_close(day)[1]:
                left_out["closing_print"] += 1
                continue
            complete = as_of is None or ts + INTERVAL_DELTA[interval] <= as_of
        key = day if daily else ts
        if key in kept:
            left_out["earlier_duplicate"] += 1
        kept[key] = ",".join([symbol, _utc(ts), repr(o), repr(h), repr(lo), repr(c), str(int(v or 0)),
                              "true" if complete else "false", *([adjustment] if adjustment else [])])
    return list(kept.values())


def _header(adjustment: str | None) -> str:
    return HEADER + (",adjustment" if adjustment else "")


def chart_rows(series) -> list[tuple]:
    """A Yahoo series -> rows of (start_utc, o, h, l, c, v)."""
    n = len(series["t"])
    cols = [series.get(k) or [None] * n for k in ("o", "h", "l", "c", "v")]
    return [(datetime.fromtimestamp(ts, UTC), *(col[i] for col in cols)) for i, ts in enumerate(series["t"])]


def chart_csv(text: str, symbol: str | None = None, *, interval: Interval = Interval.M5,
              adjustment: str | None = None, cal: XNYS | None = None, left_out: Counter | None = None) -> bytes:
    """A Yahoo chart response -> the CSV contract (one symbol)."""
    series = parse_yahoo(text, symbol or "the chart")
    meta = series["meta"]
    rmt = meta.get("regularMarketTime")
    as_of = datetime.fromtimestamp(rmt, UTC) if rmt else None
    sym = (symbol or meta.get("symbol") or "").upper()
    lines = _lines(chart_rows(series), sym, interval, as_of, cal or XNYS(), adjustment,
                   left_out if left_out is not None else Counter())
    return ("\n".join([_header(adjustment), *lines]) + "\n").encode("utf-8")


def stooq_rows(parsed) -> list[tuple]:
    """parse_stooq's rows -> rows of (start_utc, o, h, l, c, v): a daily bar is stamped with its session's open."""
    return [(eastern_to_utc(day, dtime(9, 30)), o, h, lo, c, v) for day, o, h, lo, c, v in parsed]


# ---------------------------------------------------------------- `traderos data fetch`

def _range_start(rng: str, today: date) -> date | None:
    """The first day a Yahoo range covers, for trimming Stooq's full history to the same span."""
    if rng == "max":
        return None
    if rng == "ytd":
        return date(today.year, 1, 1)
    n, unit = int(re.match(r"\d+", rng).group()), rng.lstrip("0123456789")
    if unit == "d":
        return today - timedelta(days=n)
    months = n if unit == "mo" else 12 * n
    y, m = divmod(today.year * 12 + today.month - 1 - months, 12)
    return date(y, m + 1, min(today.day, monthrange(y, m + 1)[1]))


@dataclass(frozen=True)
class Fetch:
    """What one fetch got: the CSV (the contract's), its source, and why the fallback answered when it did."""
    raw: bytes
    source: SourceKind
    symbols: tuple[str, ...]
    interval: Interval
    range: str
    fetched_at: datetime
    left_out: dict[str, int]
    fallback_reason: str | None = None


def _yahoo_unavailable(e: TraderOSError) -> bool:
    """True when Yahoo could not give an answer; False when it answered that it has no data for the symbol."""
    return "yahoo_error" not in e.details


def fetch(symbols, *, interval: str = "1d", rng: str | None = None, now: datetime | None = None,
          transport: Transport | None = None, sleep: Callable[[float], None] | None = None) -> Fetch:
    """Bars for every symbol from one source: Yahoo's chart, or for daily bars Stooq when Yahoo cannot answer.
    One source per dataset, so a dataset never mixes two vendors' prices. Nothing is stored here."""
    if interval not in INTERVALS:
        raise ValidationError(f"--interval is one of {', '.join(INTERVALS)}, not {interval!r}")
    rng = rng or DEFAULT_RANGE[interval]
    if not RANGE.match(rng):
        raise ValidationError(f"--range {rng!r} is not a Yahoo range (e.g. 5d, 60d, 6mo, 1y, 5y, 10y, ytd, max)")
    wanted = []
    for s in symbols:
        sym = s.strip().upper()
        if not SYMBOL.match(sym):
            raise ValidationError(f"{s!r} is not a ticker TraderOS stores (a letter, then letters, digits, . or -; "
                                  "at most 15)")
        if sym not in wanted:
            wanted.append(sym)
    if not wanted:
        raise ValidationError("name at least one symbol")
    sleep = sleep or time.sleep
    now = now or datetime.now(UTC)
    cal, iv = XNYS(), Interval(interval)
    left_out: Counter = Counter()
    lines, reason = [], None
    for i, sym in enumerate(wanted):
        if i:
            sleep(PAUSE)
        try:
            text = fetch_yahoo(sym, rng, interval, transport=transport, sleep=sleep)
            series = parse_yahoo(text, sym)
        except TraderOSError as e:
            if not _yahoo_unavailable(e):
                raise
            if iv is not Interval.D1:
                raise DataError(f"{e.message}. Stooq, the fallback, has no 5-minute bars, so nothing was stored",
                                code=e.code, details=e.details) from None
            reason = e.message
            break
        rmt = series["meta"].get("regularMarketTime")
        lines += _lines(chart_rows(series), sym, iv, datetime.fromtimestamp(rmt, UTC) if rmt else None, cal,
                        "split", left_out)
    else:
        return Fetch(raw=("\n".join([_header("split"), *lines]) + "\n").encode("utf-8"),
                     source=SourceKind.YAHOO_CHART, symbols=tuple(wanted), interval=iv, range=rng, fetched_at=now,
                     left_out=dict(left_out))
    # Yahoo could not answer: Stooq for every symbol, so the dataset has one source
    start = _range_start(rng, new_york_day(now))
    left_out, lines = Counter(), []
    for sym in wanted:
        sleep(PAUSE)
        try:
            code, text = _get(stooq_url(sym), transport)
            if code != 200:
                raise DataError("Stooq answered HTTP %d for %s" % (code, sym), code=ErrorCode.MISSING_DATA)
            parsed = parse_stooq(text, sym)
        except TraderOSError as e:
            raise DataError(f"{reason}; then the fallback: {e.message}. Nothing was stored", code=e.code,
                            details={"source": "stooq"}) from None
        rows = [r for r in stooq_rows(parsed) if start is None or new_york_day(r[0]) >= start]
        lines += _lines(rows, sym, iv, now, cal, None, left_out)
    return Fetch(raw=("\n".join([_header(None), *lines]) + "\n").encode("utf-8"), source=SourceKind.STOOQ,
                 symbols=tuple(wanted), interval=iv, range=rng, fetched_at=now, left_out=dict(left_out),
                 fallback_reason=reason)


def unofficial(source: SourceKind) -> Limitation:
    return Limitation(code=ErrorCode.UNOFFICIAL_DATA.value, detail=(
        f"Free and unofficial: {UNOFFICIAL[source][1]}. It licenses nothing and promises no service; its bars can "
        "be wrong, late or revised. Research only: the raw bars are never exported or shared."))


def fetch_into(store, symbols, *, interval: str = "1d", rng: str | None = None, name: str | None = None,
               now: datetime | None = None, transport: Transport | None = None,
               sleep: Callable[[float], None] | None = None):
    """Fetch, then store through exactly the path `data import` takes: the quality gate, a DS- dataset with a
    content-addressed id, the raw CSV kept byte for byte. (manifest, Fetch).

    The default name carries the fetch date, so a later day's fetch is a new dated dataset; the same bars fetched
    again (the same day, the market closed) build the same id and replay the stored dataset."""
    got = fetch(symbols, interval=interval, rng=rng, now=now, transport=transport, sleep=sleep)
    provider = UNOFFICIAL[got.source][0]
    label = "Yahoo chart" if got.source is SourceKind.YAHOO_CHART else "Stooq"
    opts = ImportOptions(
        name=name or f"{' '.join(got.symbols)} {got.interval.value}, {label} (free, unofficial), fetched "
                     f"{got.fetched_at:%Y-%m-%d}",
        interval=got.interval, provider=provider, rights_label=RIGHTS_LABEL, rights_stated_by="traderos",
        source_kind=got.source, export_raw_allowed=False, redistribution_allowed=False,
        limitations=(unofficial(got.source),))
    return import_into(store, got.raw, opts, now=got.fetched_at), got


__all__ = ["BACKOFF", "DEFAULT_RANGE", "HOSTS", "INTERVALS", "MAX_BYTES", "PAUSE", "TIMEOUT_S", "UA", "YAHOO_HOSTS",
           "Fetch", "allowed", "chart_csv", "chart_rows", "check_url", "fetch", "fetch_into", "fetch_yahoo",
           "new_york_day", "parse_stooq", "parse_yahoo", "series_from_result", "stooq_rows", "stooq_url",
           "unofficial", "urllib_transport", "yahoo_symbol", "yahoo_url"]
