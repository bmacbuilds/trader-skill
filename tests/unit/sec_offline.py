"""The upstream --offline DIR, as an SEC transport: every SEC URL is served from a fixture file, and nothing else is.

The fixtures are the upstream project's own (tests/fixtures/upstream-trader-skill, MIT, NOTICE beside them).
Its two tools named their offline files differently; this serves both layouts:
  facts.py     company_tickers.json, companyfacts-CIK##########.json, submissions-CIK##########.json,
               <accession folder>/index.json
  edgar13f.py  CIK##########.json (submissions), <accession folder>/<file>, search-<words>.json (EDGAR search)
"""
from __future__ import annotations

import json
import pathlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from traderos.adapters import sec
from traderos.contracts import DataError, ErrorCode

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "upstream-trader-skill"
ANALYST, COPYCAT, SCREENER = FIX / "trader-analyst", FIX / "trader-copycat", FIX / "trader-screener"
UA = "Test Person test@example.com"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class Offline:
    """Serves SEC URLs from files in `folder` (or `extra`, by exact URL) and records every request."""

    def __init__(self, folder: pathlib.Path, extra: dict[str, bytes] | None = None) -> None:
        self.folder, self.extra = folder, dict(extra or {})
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def names(url: str) -> list[str]:
        parsed = urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.hostname == "efts.sec.gov":
            q = parse_qs(parsed.query).get("keysTyped", [""])[0]
            words = re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")
            return [f"search-{words}.json"]
        if "Archives" in parts:
            return [f"{parts[-2]}/{parts[-1]}"]
        if "companyfacts" in parts:
            return ["companyfacts-" + parts[-1]]
        if "submissions" in parts:
            return ["submissions-" + parts[-1], parts[-1]]
        return [parts[-1]]

    def __call__(self, url: str, headers: dict) -> bytes:
        self.calls.append((url, headers))
        if url in self.extra:
            return self.extra[url]
        for name in self.names(url):
            path = self.folder / name
            if path.is_file():
                return path.read_bytes()
        raise DataError(f"SEC answered 404 for {url}", code=ErrorCode.MISSING_DATA)


def client(tmp_path: pathlib.Path, folder: pathlib.Path = COPYCAT, extra=None, clock=None) -> sec.SecClient:
    return sec.SecClient(tmp_path / "sec-cache", ua=UA, transport=Offline(folder, extra), clock=clock or (lambda: T0))


def companyfacts(cik: str):
    """A company-facts fixture, parsed as the adapter parses it (decimals exact)."""
    return json.loads((ANALYST / f"companyfacts-CIK{cik}.json").read_text(encoding="utf-8"), parse_float=Decimal)
