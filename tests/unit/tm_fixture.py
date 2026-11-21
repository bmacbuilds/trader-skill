"""The upstream Yahoo TSLA fixture as a stored 5-minute dataset, for the plan-replay tests only.

TEST-ONLY: the prices are Yahoo's and their rights are unverified (tests/fixtures/timemachine/README.md). The file
goes through the same CSV gate as any user file (docs/data-csv-v1.md); nothing here reaches the network.
"""
from __future__ import annotations

import gzip
import io
import json
import pathlib
from datetime import UTC, datetime

import pytest

from traderos.adapters.prices import chart_csv
from traderos.contracts import Interval
from traderos.data import ImportOptions, build
from traderos.testing import MemoryBarReader

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "timemachine"
TSLA = FIX / "TSLA-60d5m.json.gz"     # Yahoo v8 chart, range=60d interval=5m, fetched 2026-10-19 10:49 ET
ABSENT = ("the Yahoo TSLA fixture is not in this tree: the public release leaves it out because its rights are "
          "unverified (ADR-010); these tests run in traderos-dev")
if not TSLA.is_file():          # a module that imports this one is skipped whole, never failed at collection
    pytest.skip(ABSENT, allow_module_level=True)
with gzip.open(TSLA, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
OPTIONS = ImportOptions(name="TSLA 5m test fixture (rights unverified)", interval=Interval.M5,
                        rights_label="test-only-rights-unverified")


def utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def yahoo_csv(text: str = TEXT) -> bytes:
    """A Yahoo chart response -> the CSV contract, by the adapter's own converter (moved there, ADR-014): a bar still
    in progress at the response's own regularMarketTime is written complete=false, as the store must keep it (spec
    06.4); a bar with a missing price is left out (a stored bar cannot lack one)."""
    return chart_csv(text, interval=Interval.M5)


def dataset(text: str = TEXT, *, name: str | None = None):
    """(manifest, bars) through the import gate."""
    opts = OPTIONS if name is None else ImportOptions(name=name, interval=Interval.M5,
                                                      rights_label="test-only-rights-unverified")
    return build(yahoo_csv(text), opts, now=datetime(2026, 10, 20, tzinfo=UTC))


def reader(text: str = TEXT):
    """(MemoryBarReader, dataset_id) holding the fixture."""
    manifest, bars = dataset(text)
    r = MemoryBarReader()
    r.add(manifest, bars)
    return r, manifest.dataset_id


def edited(fn) -> str:
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep) -> None:
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def rows_reader(rows, *, symbol: str = "TSLA", name: str = "rows"):
    """(MemoryBarReader, dataset_id) from rows of (start_utc: datetime, o, h, l, c[, complete]) through the gate."""
    out = io.StringIO()
    out.write("symbol,timestamp_utc,open,high,low,close,volume,complete\n")
    for row in rows:
        start, o, h, lo, c = row[:5]
        complete = row[5] if len(row) > 5 else True
        out.write(f"{symbol},{start.strftime('%Y-%m-%dT%H:%M:%SZ')},{o!r},{h!r},{lo!r},{c!r},100,"
                  f"{'true' if complete else 'false'}\n")
    manifest, bars = build(out.getvalue().encode("utf-8"), ImportOptions(
        name=name, interval=Interval.M5, rights_label="test-only"), now=datetime(2026, 12, 1, tzinfo=UTC))
    r = MemoryBarReader()
    r.add(manifest, bars)
    return r, manifest.dataset_id
