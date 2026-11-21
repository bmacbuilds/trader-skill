"""Fakes for parallel builders: code against the frozen interfaces before the core store lands (spec 02.3, 07.3).

`MemoryBarReader` is a complete, as-of-safe `BarReader` over records held in memory. Part B can test scan and
pulse logic with it today; the core's SQLite reader must pass the same interface tests
(tests/contract/test_bar_reader.py), so code written against this works unchanged against the real one.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from traderos.contracts import Bar, DataError, DatasetManifest, ErrorCode, Instrument


class MemoryBarReader:
    """A BarReader over in-memory datasets: {dataset_id: (manifest, bars)}."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[DatasetManifest, dict[str, tuple[Bar, ...]]]] = {}

    def add(self, manifest: DatasetManifest, bars: Iterable[Bar]) -> None:
        by_instrument: dict[str, list[Bar]] = {}
        for b in bars:
            by_instrument.setdefault(b.instrument_id, []).append(b)
        self._data[manifest.dataset_id] = (manifest, {k: tuple(sorted(v, key=lambda b: b.end))
                                                      for k, v in by_instrument.items()})

    def _get(self, dataset_id: str):
        try:
            return self._data[dataset_id]
        except KeyError:
            raise DataError(f"no dataset {dataset_id!r}", code=ErrorCode.MISSING_DATA) from None

    def manifest(self, dataset_id: str) -> DatasetManifest:
        return self._get(dataset_id)[0]

    def instruments(self, dataset_id: str) -> tuple[Instrument, ...]:
        return self._get(dataset_id)[0].instruments

    def bars(self, dataset_id: str, instrument_id: str, *, as_of: datetime, start: datetime | None = None,
             limit: int | None = None) -> tuple[Bar, ...]:
        if as_of.tzinfo is None:
            raise DataError("as_of must be timezone-aware", code=ErrorCode.INVALID_INPUT)
        manifest, series = self._get(dataset_id)
        if instrument_id not in {i.instrument_id for i in manifest.instruments}:
            raise DataError(f"{instrument_id} is not in {dataset_id}", code=ErrorCode.MISSING_DATA)
        got = [b for b in series.get(instrument_id, ())
               if b.available_at <= as_of and (start is None or b.end >= start)]
        if limit is not None:
            got = got[-limit:] if limit > 0 else []
        return tuple(got)
