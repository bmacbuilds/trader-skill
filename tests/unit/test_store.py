"""The local store: append-only records, atomic datasets, content-addressed files, memory (spec 05.2, 11)."""
from __future__ import annotations

import dataclasses
import os
import stat
from datetime import datetime, timedelta

import pytest

from traderos.contracts import (
    DataError,
    DatasetManifest,
    ErrorCode,
    IntegrityError,
    ResearchMemoryEvent,
    StrategyVersion,
    ValidationError,
    record_hash,
    with_hash,
)
from traderos.contracts.examples import all_examples, bar
from traderos.store import Store

EX = all_examples()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "home").init()


def test_init_is_idempotent_and_owner_only(store):
    store.put(EX["strategy-version/1"])
    Store(store.home).init()
    assert store.get(StrategyVersion, "SV-1") == EX["strategy-version/1"]
    if os.name == "posix":
        assert stat.S_IMODE(store.home.stat().st_mode) == 0o700


def test_records_are_append_only(store):
    sv = EX["strategy-version/1"]
    store.put(sv)
    store.put(sv)                                              # the same record again: no-op
    changed = with_hash(dataclasses.replace(sv, hypothesis="changed"))
    with pytest.raises(IntegrityError):
        store.put(changed)
    assert store.get(StrategyVersion, "SV-1") == sv


def test_missing_record_is_missing_data(store):
    with pytest.raises(DataError) as e:
        store.get(StrategyVersion, "nope")
    assert e.value.code is ErrorCode.MISSING_DATA


def test_tampered_bytes_are_refused(store):
    store.put(EX["strategy-version/1"])
    with store.connect() as db:
        db.execute("UPDATE records SET body = replace(body, 'revert', 'REVERT')")
    with pytest.raises(IntegrityError):
        store.get(StrategyVersion, "SV-1")


def test_dataset_is_all_or_nothing(store):
    m = EX["dataset/1"]
    good = bar()
    store.put_dataset(m, [good])
    assert store.bar_reader().bars(m.dataset_id, good.instrument_id, as_of=good.available_at) == (good,)
    other = with_hash(dataclasses.replace(m, dataset_id="DS-2", canonical_hash=None))
    with pytest.raises(ValidationError):
        store.put_dataset(other, [good, good])                 # duplicate bar violates the key: nothing lands
    assert [d.dataset_id for d in store.datasets()] == [m.dataset_id]


def test_manifest_without_its_hash_is_refused(store):
    m = dataclasses.replace(EX["dataset/1"], canonical_hash=None)
    with pytest.raises(IntegrityError):
        store.put_dataset(m, [])


def test_artifacts_are_content_addressed_and_verified(store):
    digest = store.put_artifact(b"hello", "text/plain")
    assert store.read_artifact(digest) == b"hello"
    assert store.put_artifact(b"hello") == digest
    store.artifact_path(digest).write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        store.read_artifact(digest)
    assert not list(store.artifact_path(digest).parent.glob(".tmp-*"))


def test_memory_search_and_chain(store):
    first = EX["memory-event/1"]
    store.remember(first)
    second = with_hash(dataclasses.replace(first, event_id="ME-2", kind="no_trade", summary="SPY refused: unknown "
                                           "exposure", previous_event_hash=store.last_memory_hash(),
                                           at=first.at + timedelta(minutes=1), canonical_hash=None))
    store.remember(second)
    assert [e.event_id for e in store.search_memory("unknown exposure")] == ["ME-2"]
    assert [e.event_id for e in store.search_memory("")] == ["ME-2", "ME-1"]
    assert store.search_memory("100%_") == []                   # wildcards are literal
    assert [e.event_id for e in store.search_memory("", kinds=("experiment.completed",))] == ["ME-1"]
    assert second.previous_event_hash == record_hash(first)
    assert isinstance(store.search_memory("SPY")[0], ResearchMemoryEvent)


def test_a_newer_store_is_refused(store):
    with store.connect() as db:
        db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(DataError) as e:
        Store(store.home).init()
    assert e.value.code is ErrorCode.SCHEMA_UNSUPPORTED


def test_no_store_says_run_init(tmp_path):
    with pytest.raises(DataError, match="traderos init"):
        Store(tmp_path / "nothing").datasets()


def test_dataset_round_trip(store):
    m = EX["dataset/1"]
    store.put_dataset(m, [bar()])
    assert store.bar_reader().manifest(m.dataset_id) == m
    assert isinstance(store.datasets()[0], DatasetManifest)


def test_latest_is_the_newest_by_the_records_own_time_in_one_query(store):
    from traderos.contracts import AlertEvent
    base = EX["alert-event/1"]

    def event(n, rule, minutes, micro=0):
        at = base.evaluated_at + timedelta(minutes=minutes, microseconds=micro)
        return with_hash(dataclasses.replace(base, event_id=f"AE-{n}", rule_id=rule, evaluated_at=at,
                                             canonical_hash=None))
    assert store.latest(AlertEvent, where={"rule_id": "AR-1"}, order_by="evaluated_at") is None
    store.put(event(1, "AR-1", 10))
    store.put(event(2, "AR-1", 30))
    store.put(event(5, "AR-1", 30, micro=1))             # a fraction later than event 2: "...00.000001Z" vs "...00Z"
    store.put(event(3, "AR-1", 20))                      # stored last, but an earlier instant
    store.put(event(4, "AR-2", 99))                      # another rule
    got = store.latest(AlertEvent, where={"rule_id": "AR-1"}, order_by="evaluated_at")
    assert got.event_id == "AE-5"
    assert store.latest(AlertEvent, where={"rule_id": "AR-2"}, order_by="evaluated_at").event_id == "AE-4"
    assert store.latest(AlertEvent, where={"rule_id": "AR-1", "provider": "nope"}, order_by="evaluated_at") is None

    def before(at):
        got = store.latest(AlertEvent, where={"rule_id": "AR-1"}, order_by="evaluated_at", before=at)
        return got and got.event_id
    t = base.evaluated_at
    assert before(t + timedelta(minutes=99)) == "AE-5"                        # every AR-1 event is earlier
    assert before(t + timedelta(minutes=30, microseconds=1)) == "AE-2"       # strictly before: AE-5's own stamp is out
    assert before(t + timedelta(minutes=30)) == "AE-3"                       # "...00Z" bound: the padded key, not text
    assert before(t + timedelta(minutes=10, microseconds=1)) == "AE-1"
    assert before(t + timedelta(minutes=10)) is None                         # nothing is strictly earlier
    assert store.latest(AlertEvent, where={"rule_id": "AR-2"}, order_by="evaluated_at",
                        before=t + timedelta(minutes=50)) is None            # the bound and the where both hold
    with pytest.raises(ValidationError):
        store.latest(AlertEvent, where={"rule_id": "AR-1"}, order_by="evaluated_at",
                     before=datetime(2026, 1, 1))                            # naive: refused, never guessed
    for bad in ({"rule": "AR-1"}, {}, {"rule_id": 1}):
        with pytest.raises(ValidationError):
            store.latest(AlertEvent, where=bad, order_by="evaluated_at")
    with pytest.raises(ValidationError):
        store.latest(AlertEvent, where={"rule_id": "AR-1"}, order_by="rule_id")      # not a timestamp
    with store.connect() as db:                           # a tampered row is caught, as get() catches it
        db.execute("UPDATE records SET body = replace(body, 'AR-1', 'AR-1') , canonical_hash = 'x' "
                   "WHERE record_id = 'AE-5'")
    with pytest.raises(IntegrityError):
        store.latest(AlertEvent, where={"rule_id": "AR-1"}, order_by="evaluated_at")


def test_all_reads_every_record_when_asked_and_checks_never_cap_it(store):
    from traderos.contracts import AlertEvent
    base = EX["alert-event/1"]
    for n in range(3):
        store.put(with_hash(dataclasses.replace(base, event_id=f"AE-{n}", canonical_hash=None,
                                                evaluated_at=base.evaluated_at + timedelta(minutes=n))))
    assert [e.event_id for e in store.all(AlertEvent, limit=2)] == ["AE-0", "AE-1"]       # oldest stored first
    assert [e.event_id for e in store.all(AlertEvent, limit=None)] == ["AE-0", "AE-1", "AE-2"]
    with store.connect() as db:                              # past the default cap of 1000: None still reads all
        body, h, us = db.execute("SELECT body, canonical_hash, recorded_us FROM records WHERE record_id = 'AE-2'"
                                 ).fetchone()
        db.executemany("INSERT INTO records(record_type, record_id, canonical_hash, body, recorded_us) "
                       "VALUES (?, ?, ?, ?, ?)", [(AlertEvent.SCHEMA, f"AE-X{n:04d}", h, body, us + 1 + n)
                                                  for n in range(1001)])
    assert len(store.all(AlertEvent, limit=None)) == 1004 and len(store.all(AlertEvent)) == 1000
    # A holdout gate or a lookup by id that read only the oldest N would stop seeing new records once N were
    # stored, and pass silently. So every read in the product either reads all or is uncapped by construction.
    import ast
    import pathlib

    import traderos
    capped = []
    for path in pathlib.Path(traderos.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "all" \
                    and "store" in ast.unparse(node.func.value):
                limit = next((k.value for k in node.keywords if k.arg == "limit"), None)
                if not (isinstance(limit, ast.Constant) and limit.value is None) \
                        and not (isinstance(limit, ast.Name) and limit.id == "_ALL"):
                    capped.append(f"{path.name}:{node.lineno}")
    assert capped == []
