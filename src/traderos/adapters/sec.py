"""SEC EDGAR, read only (spec 06.1 adapter (d), 03.5): the one network client in the Free package.

It runs only when a person asks for an SEC-backed result (earnings, 13F holdings, a fundamental screen). It:
  * IDENTIFIES THE PERSON, as the SEC's fair-access policy asks: their own "Name email" from the SEC_USER_AGENT
    environment variable or `traderos config set sec_user_agent "..."`. Without it, it refuses (NOT_AUTHORIZED); it
    never invents an identity;
  * stays well under the SEC's limit of 10 requests a second (here at most 4), with a timeout and a size cap that
    holds AFTER decompression too (a small gzip can expand a thousandfold);
  * talks only to www.sec.gov, data.sec.gov and efts.sec.gov (EDGAR's filer-name search) over HTTPS; any other host
    is refused, and so is a redirect to one (it would carry the person's identification there);
  * caches every response under ~/.traderos/cache/sec with the time it was fetched, so a rerun reads the same bytes
    (`max_age` decides when a cached copy is too old to reuse);
  * sends nothing about the person's research: only the public URL and their identification.

SEC data is public; the SEC does not license it. Each result still carries where it came from and when.

The ported SEC tools (traderos.fundamentals.facts, .edgar13f, ADR-011) fetch only through this client: the upstream two
Fetchers (facts.py:92-122, edgar13f.py:100-174) have no cache and no size cap, and spec 04.3 requires all internet
access through a registered adapter, and 06.1(d) requires caching.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from traderos.contracts import AuthorizationError, DataError, ErrorCode, ValidationError, sha256_bytes

HOSTS = ("www.sec.gov", "data.sec.gov", "efts.sec.gov")
MAX_BYTES = 60 * 1024 * 1024
TIMEOUT_S = 30
MIN_INTERVAL_S = 0.25                            # at most 4 requests a second
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?keysTyped="
PLACEHOLDER = "you@example.com"
Transport = Callable[[str, dict[str, str]], bytes]


def sec_address(url: str) -> bool:
    """True for an HTTPS URL on one of the SEC hosts, with no user name and no port."""
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.hostname in HOSTS and not parts.username and not parts.port


class _SecRedirectsOnly(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another SEC address: urllib would otherwise send the request, the person's
    identification included, wherever the answer points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not sec_address(newurl):
            raise ValidationError(f"refused: the SEC redirected {req.full_url} to {newurl}, which is not an SEC "
                                  "address", code=ErrorCode.OUTSIDE_CAPABILITY)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SecRedirectsOnly)


def _urllib_transport(url: str, headers: dict[str, str]) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            data = resp.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise DataError(f"{url} is larger than {MAX_BYTES} bytes", code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
            if resp.headers.get("Content-Encoding") == "gzip":
                data = _gunzip(data, url)
            return data
    except urllib.error.HTTPError as e:
        code = ErrorCode.NOT_AUTHORIZED if e.code in (401, 403) else ErrorCode.MISSING_DATA
        raise DataError(f"SEC answered {e.code} for {url}", code=code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DataError(f"could not reach the SEC ({type(e).__name__}); nothing was changed",
                        code=ErrorCode.MISSING_DATA) from None


def _gunzip(data: bytes, url: str, limit: int | None = None) -> bytes:
    """gzip-decoded bytes, never more than `limit` of them: the cap holds after decompression, not only before. The
    free-prices adapter reuses it with its own cap (ADR-014)."""
    limit = MAX_BYTES if limit is None else limit
    out, rest, members = bytearray(), data, 0
    try:
        while rest[:2] == b"\x1f\x8b":                # one gzip member after another; trailing padding is ignored
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out += d.decompress(rest, limit + 1 - len(out))
            if len(out) > limit or d.unconsumed_tail:
                raise DataError(f"{url} expands to more than {limit} bytes",
                                code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
            if not d.eof:
                raise DataError(f"{url} ended inside its compressed data", code=ErrorCode.INTEGRITY_MISMATCH)
            rest, members = d.unused_data, members + 1
    except zlib.error:
        raise DataError(f"{url} is not valid gzip", code=ErrorCode.INTEGRITY_MISMATCH) from None
    if not members:
        raise DataError(f"{url} is not valid gzip", code=ErrorCode.INTEGRITY_MISMATCH)
    return bytes(out)


def normalise_ticker(ticker: str) -> str:
    """One spelling for a share class: BRK.B, BRK-B and BRK/B are the same ticker (the SEC's list writes BRK-B)."""
    return str(ticker).strip().upper().replace("-", ".").replace("/", ".")


def is_placeholder(ua: str) -> bool:
    """The example identification from the help text is not a person (the upstream rule, facts.py:87,
    edgar13f.py:95)."""
    return PLACEHOLDER in str(ua).lower()


def user_agent(home: pathlib.Path | None = None) -> str:
    """The person's own identification for the SEC, or AuthorizationError. Never invented."""
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua and home is not None:
        settings = home / "config" / "settings.json"
        if settings.exists():
            try:
                ua = str(json.loads(settings.read_text(encoding="utf-8")).get("sec_user_agent", "")).strip()
            except (json.JSONDecodeError, OSError):
                ua = ""
    if not ua or "@" not in ua or len(ua) > 200 or is_placeholder(ua):
        raise AuthorizationError("The SEC asks every program to identify the person using it. Set yours once: "
                                 f"traderos config set sec_user_agent \"Your Name {PLACEHOLDER}\" with YOUR name and "
                                 "email (or the "
                                 "SEC_USER_AGENT environment variable). It is sent only to sec.gov.",
                                 code=ErrorCode.NOT_AUTHORIZED)
    return ua


class SecClient:
    def __init__(self, cache_dir: pathlib.Path, *, ua: str, transport: Transport | None = None,
                 max_age: timedelta = timedelta(hours=12), clock: Callable[[], datetime] | None = None) -> None:
        self.cache_dir = cache_dir
        self.ua = ua
        self.transport = transport or _urllib_transport
        self.max_age = max_age
        self.clock = clock or (lambda: datetime.now(UTC))
        self._last = 0.0
        self._lock = threading.Lock()

    def _check(self, url: str) -> None:
        if not sec_address(url):
            raise ValidationError(f"refused: {url} is not an SEC address", code=ErrorCode.OUTSIDE_CAPABILITY)

    def get(self, url: str) -> tuple[bytes, datetime]:
        """The bytes at `url` and when they were fetched (from the cache when fresh enough)."""
        self._check(url)
        key = sha256_bytes(url.encode())
        body, meta = self.cache_dir / f"{key}.body", self.cache_dir / f"{key}.json"
        if body.exists() and meta.exists():
            fetched = datetime.fromisoformat(json.loads(meta.read_text())["fetched_at"])
            if self.clock() - fetched <= self.max_age:
                return body.read_bytes(), fetched
        with self._lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        data = self.transport(url, {"User-Agent": self.ua, "Accept-Encoding": "gzip"})
        fetched = self.clock()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = body.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, body)
        meta.write_text(json.dumps({"url": url, "fetched_at": fetched.isoformat(), "sha256": sha256_bytes(data)}))
        return data, fetched

    def json(self, url: str):
        data, fetched = self.get(url)
        try:
            return json.loads(data, parse_float=Decimal), fetched     # SEC numbers stay exact
        except json.JSONDecodeError:
            raise DataError(f"the SEC returned something that is not JSON for {url}",
                            code=ErrorCode.INTEGRITY_MISMATCH) from None

    # ---- the few endpoints used ----------------------------------------------------------------------------------
    def cik_for(self, ticker: str) -> tuple[str, str]:
        """(10-digit CIK, entity name) for a ticker, from the SEC's own ticker list. A share class is found however
        it is written (BRK.B, BRK-B, BRK/B)."""
        data, _ = self.json(TICKERS_URL)
        want = normalise_ticker(ticker)
        for row in (data.values() if isinstance(data, dict) else []):
            if normalise_ticker(row.get("ticker", "")) == want:
                return f"{int(row['cik_str']):010d}", str(row.get("title", ""))
        raise DataError(f"the SEC's ticker list has no {want}", code=ErrorCode.MISSING_DATA)

    def tickers(self):
        """The SEC's company_tickers.json (ticker, CIK and title of every listed company) and when it was fetched."""
        return self.json(TICKERS_URL)

    def search(self, query: str):
        """EDGAR's filer-name search (the company search box): every filer whose name matches `query`."""
        query = str(query).strip()
        if not query or len(query) > 200:
            raise ValidationError("a filer search needs 1 to 200 characters")
        return self.json(SEARCH_URL + urllib.parse.quote(query))

    def companyfacts(self, cik: str):
        return self.json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json")

    def submissions(self, cik: str):
        return self.json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")

    def filing_index(self, cik: str, accession: str):
        return self.json(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/index.json")

    def filing_file(self, cik: str, accession: str, name: str) -> tuple[bytes, datetime, str]:
        if "/" in name or ".." in name or not name:
            raise ValidationError(f"unsafe file name {name!r}")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{name}"
        data, fetched = self.get(url)
        return data, fetched, url


def client(home: pathlib.Path, *, transport: Transport | None = None) -> SecClient:
    return SecClient(home / "cache" / "sec", ua=user_agent(home), transport=transport)


__all__ = ["HOSTS", "SEARCH_URL", "SecClient", "TICKERS_URL", "client", "normalise_ticker", "user_agent"]
