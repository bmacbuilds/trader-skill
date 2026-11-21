"""The BarReader interface tests. Every BarReader (the in-memory fake now, the SQLite store later) must pass them:
QT01's foundation is that a read at `as_of` never returns a bar that was not yet available."""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from traderos.contracts import BarReader, DataError, ErrorCode
from traderos.contracts.examples import all_examples, bar
from traderos.testing import MemoryBarReader


def _readers():
    manifest = all_examples()["dataset/1"]
    first = bar()
    bars = []
    for i in range(5):
        shift = timedelta(days=i)
        bars.append(dataclasses.replace(first, start=first.start + shift, end=first.end + shift,
                                        available_at=first.available_at + shift + timedelta(minutes=15)))
    memory = MemoryBarReader()
    memory.add(manifest, reversed(bars))
    return manifest, bars, memory


@pytest.fixture(params=["memory", "sqlite"])
def reader(request, tmp_path):
    manifest, bars, memory = _readers()
    if request.param == "memory":
        return memory, manifest, bars
    from traderos.store import Store
    store = Store(tmp_path / "home").init()
    store.put_dataset(manifest, list(reversed(bars)))
    return store.bar_reader(), manifest, bars


def test_is_a_bar_reader(reader):
    r, _, _ = reader
    assert isinstance(r, BarReader)


def test_as_of_never_returns_a_bar_not_yet_available(reader):
    r, m, bars = reader
    sid = bars[0].instrument_id
    assert r.bars(m.dataset_id, sid, as_of=bars[2].end) == tuple(bars[:2])    # bar 2 ended, not yet available
    assert r.bars(m.dataset_id, sid, as_of=bars[2].available_at) == tuple(bars[:3])
    assert r.bars(m.dataset_id, sid, as_of=bars[0].start) == ()


def test_oldest_first_and_limit_keeps_the_newest(reader):
    r, m, bars = reader
    got = r.bars(m.dataset_id, bars[0].instrument_id, as_of=datetime(2030, 1, 1, tzinfo=UTC), limit=2)
    assert got == tuple(bars[3:])


def test_unknown_dataset_or_instrument_is_missing_data(reader):
    r, m, bars = reader
    for ds, ins in (("nope", bars[0].instrument_id), (m.dataset_id, "INS-NOPE")):
        with pytest.raises(DataError) as e:
            r.bars(ds, ins, as_of=bars[-1].available_at)
        assert e.value.code is ErrorCode.MISSING_DATA


def test_naive_as_of_is_refused(reader):
    r, m, bars = reader
    with pytest.raises(DataError):
        r.bars(m.dataset_id, bars[0].instrument_id, as_of=datetime(2030, 1, 1))
