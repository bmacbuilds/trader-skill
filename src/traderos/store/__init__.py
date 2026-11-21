"""The local store (spec 06.5, 11): one SQLite database and content-addressed files under ~/.traderos.

ONE TRANSACTIONAL AUTHORITY (spec 11.1): `registry.sqlite` holds datasets, bars, every research record, memory and
run state, with WAL, foreign keys and explicit migrations. Files (artifacts, raw imports) are content-addressed and
written atomically: temp file, fsync, rename (spec 11.2). A record is referenced only after its bytes are safely
on disk.

APPEND-ONLY (spec 05.2): a record is inserted once. Inserting the same record again is a no-op; inserting a
different record under an existing id is refused (INTEGRITY_MISMATCH), never an overwrite. Changes are new
revisions or new events.

ONE DATASET, MANY RECORDINGS (ADR-013): a dataset's id names its source file and import options, and its manifest
also records when it was imported, so two profiles that import the same file hold manifests whose canonical hashes
differ by that time alone. Those are one dataset (`same_dataset`). A manifest of a dataset recorded elsewhere, brought
by a bundle, is kept as a content-addressed file named by its canonical hash (`keep_manifest`), so a Passport or an
experiment that names that hash resolves to this profile's copy (`dataset_for`), or, while this profile has no copy,
says which file to re-import. Nothing is overwritten: the local manifest stands.

The home folder is $TRADEROS_HOME, else ~/.traderos. It is created owner-only (0700).
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import pathlib
import sqlite3
import tempfile
import typing
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TypeVar

from traderos.contracts import (
    RECORD_TYPES,
    Bar,
    DataError,
    DatasetManifest,
    ErrorCode,
    Instrument,
    IntegrityError,
    Record,
    ResearchMemoryEvent,
    ValidationError,
    canonical_bytes,
    from_json,
    iso_utc,
    record_hash,
    sha256_bytes,
    to_json,
    verify_hash,
)
from traderos.contracts.canonical import HASH_FIELD, to_jsonable

T = TypeVar("T", bound=Record)
SCHEMA_VERSION = 1
MANIFEST_MEDIA = "application/vnd.traderos.dataset-manifest+json"   # a dataset's manifest as recorded elsewhere

# The id field of every record type the store keeps in `records` (bars and datasets have their own tables).
ID_FIELD = {
    "failure/1": None, "receipt/1": "receipt_id", "instrument/1": "instrument_id", "quote/1": None,
    "provider-capabilities/1": None, "data-batch/1": None, "feature/1": "formula_version",
    "strategy-draft/1": "draft_id", "strategy-version/1": "version_id", "experiment-spec/1": "experiment_id",
    "trial/1": "trial_id", "challenge/1": "result_id", "passport/1": None, "ledger/1": "entry_id",
    "memory-event/1": "event_id", "no-trade/1": "record_id", "workflow/1": "workflow_id", "bundle/1": "bundle_id",
    "import-report/1": None, "scan-criteria/1": "criteria_id", "scan/1": "scan_id", "market-context/1": "context_id",
    "entry/1": "assessment_id", "exit-plan/1": "plan_id", "alert-rule/1": "rule_id", "alert-event/1": "event_id",
    "risk-plan/1": "plan_id", "risk-assessment/1": "assessment_id", "paper-ticket/1": "ticket_id",
    "capabilities/1": None, "bar/1": None, "dataset/1": "dataset_id", "timemachine/1": "report_id",
    "earnings/1": "report_id", "holdings/1": "report_id", "fundamental-screen/1": "screen_id",
    "holdings-diff/1": "diff_id", "copy-targets/1": "targets_id",
}

MIGRATIONS = {
    1: """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE datasets (
        dataset_id TEXT PRIMARY KEY,
        canonical_hash TEXT NOT NULL,
        body TEXT NOT NULL,
        recorded_us INTEGER NOT NULL
    );
    CREATE TABLE bars (
        dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
        instrument_id TEXT NOT NULL,
        end_us INTEGER NOT NULL,
        available_us INTEGER NOT NULL,
        body TEXT NOT NULL,
        PRIMARY KEY (dataset_id, instrument_id, end_us)
    ) WITHOUT ROWID;
    CREATE TABLE records (
        record_type TEXT NOT NULL,
        record_id TEXT NOT NULL,
        canonical_hash TEXT NOT NULL,
        body TEXT NOT NULL,
        recorded_us INTEGER NOT NULL,
        PRIMARY KEY (record_type, record_id)
    );
    CREATE INDEX records_by_time ON records(record_type, recorded_us);
    CREATE TABLE memory (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        at_us INTEGER NOT NULL,
        subjects TEXT NOT NULL,
        summary TEXT NOT NULL,
        canonical_hash TEXT NOT NULL,
        body TEXT NOT NULL
    );
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        experiment_id TEXT NOT NULL,
        spec_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed','canceled')),
        started_us INTEGER NOT NULL,
        ended_us INTEGER,
        artifacts TEXT NOT NULL DEFAULT '[]',
        error TEXT
    );
    CREATE TABLE artifacts (
        sha256 TEXT PRIMARY KEY,
        bytes INTEGER NOT NULL,
        media_type TEXT NOT NULL,
        recorded_us INTEGER NOT NULL
    );
    """,
}


def us(value: datetime) -> int:
    """Microseconds since the epoch: the store's sortable time column."""
    if value.tzinfo is None:
        raise ValidationError("naive datetime refused")
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def default_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("TRADEROS_HOME") or pathlib.Path.home() / ".traderos")


def same_dataset(a: DatasetManifest, b: DatasetManifest) -> bool:
    """Two manifests of one dataset (ADR-013): the same id (source file and import options), the same source file, the
    same bars (normalized hash and count) and the same rights. When each was recorded is not part of it."""
    return (a.dataset_id == b.dataset_id and a.raw_sha256 == b.raw_sha256 and a.normalized_sha256 == b.normalized_sha256
            and a.row_count == b.row_count and a.rights == b.rights)


def reimport_hint(m: DatasetManifest) -> str:
    """How a person restores a dataset whose bars a bundle could not carry (its rights, or the tutorial)."""
    if m.synthetic:
        return "run `traderos init` to restore it (the tutorial is rebuilt byte for byte)"
    flags = f' --name "{m.name}"' + ("" if m.interval.value == "1d" else f" --interval {m.interval.value}") + \
        ("" if m.rights.label == "user-provided" else f' --rights-label "{m.rights.label}"')
    return f"re-import your file to restore it: `traderos data import <file>{flags}` (its sha256 is {m.raw_sha256})"


def manifest_bytes(m: DatasetManifest) -> bytes:
    """A manifest's canonical form without its hash field: the bytes whose SHA-256 is its canonical hash."""
    return canonical_bytes({k: v for k, v in to_jsonable(m).items() if k != HASH_FIELD})


def record_id(record: Record) -> str:
    name = ID_FIELD.get(record.SCHEMA)
    if name is None:
        if record.SCHEMA == "passport/1":
            return f"{record.passport_id}@{record.revision}"            # type: ignore[attr-defined]
        raise ValidationError(f"{record.SCHEMA} records are not stored on their own")
    return str(getattr(record, name))


class Store:
    """The local store. `Store().init()` is idempotent; every other method expects an initialized home."""

    def __init__(self, home: pathlib.Path | str | None = None) -> None:
        self.home = pathlib.Path(home) if home else default_home()
        self.db_path = self.home / "registry.sqlite"

    # ---- setup -------------------------------------------------------------------------------------------------
    def init(self) -> Store:
        for sub in ("", "config", "datasets", "artifacts", "exports", "cache", "logs"):
            path = self.home / sub
            path.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o700)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            have = int(row[0]) if row else 0
            if have > SCHEMA_VERSION:
                raise DataError(f"this store was written by a newer TraderOS (schema {have}); upgrade TraderOS",
                                code=ErrorCode.SCHEMA_UNSUPPORTED)
            for version in range(have + 1, SCHEMA_VERSION + 1):
                script = MIGRATIONS[version]
                if version == 1:
                    script = script.replace("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);", "")
                db.executescript("BEGIN;" + script + f"""
                    INSERT INTO meta(key, value) VALUES('schema_version', '{version}')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                    COMMIT;""")
        return self

    def initialized(self) -> bool:
        if not self.db_path.exists():
            return False
        with self.connect() as db:
            try:
                row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            except sqlite3.DatabaseError:
                return False
        return bool(row) and int(row[0]) == SCHEMA_VERSION

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self.home.exists():
            raise DataError(f"no TraderOS store at {self.home}; run `traderos init`", code=ErrorCode.MISSING_DATA)
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = FULL")
            db.execute("PRAGMA busy_timeout = 30000")
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # ---- records (append-only) ---------------------------------------------------------------------------------
    def put(self, record: Record, *, db: sqlite3.Connection | None = None) -> str:
        """Insert once. Same id and hash again: no-op. Same id, different content: INTEGRITY_MISMATCH."""
        rid = record_id(record)
        digest = record_hash(record)
        body = to_json(record)
        if db is None:
            with self.transaction() as tx:
                return self._put(tx, record.SCHEMA, rid, digest, body)
        return self._put(db, record.SCHEMA, rid, digest, body)

    @staticmethod
    def _put(db: sqlite3.Connection, schema: str, rid: str, digest: str, body: str) -> str:
        row = db.execute("SELECT canonical_hash FROM records WHERE record_type = ? AND record_id = ?",
                         (schema, rid)).fetchone()
        if row:
            if row[0] != digest:
                raise IntegrityError(f"{schema} {rid} already exists with different content; records are never "
                                     "overwritten")
            return rid
        db.execute("INSERT INTO records(record_type, record_id, canonical_hash, body, recorded_us) "
                   "VALUES (?, ?, ?, ?, ?)", (schema, rid, digest, body, us(datetime.now(UTC))))
        return rid

    def get(self, cls: type[T], rid: str) -> T:
        with self.connect() as db:
            row = db.execute("SELECT body, canonical_hash FROM records WHERE record_type = ? AND record_id = ?",
                             (cls.SCHEMA, rid)).fetchone()
        if not row:
            raise DataError(f"no {cls.SCHEMA} record {rid!r}", code=ErrorCode.MISSING_DATA)
        record = from_json(cls, row[0])
        if record_hash(record) != row[1]:
            raise IntegrityError(f"{cls.SCHEMA} {rid}: stored bytes do not match their hash")
        return record

    def find(self, cls: type[T], rid: str) -> T | None:
        try:
            return self.get(cls, rid)
        except DataError:
            return None

    def all(self, cls: type[T], *, limit: int | None = 1000) -> list[T]:
        """Records of `cls`, oldest stored first. `limit=None` reads every one: a check that must see all of them
        (a holdout gate, a lookup by id) never passes a number, because a capped read stops seeing the newest
        records once the cap is reached and the check then passes silently."""
        sql = "SELECT body FROM records WHERE record_type = ? ORDER BY recorded_us, record_id"
        with self.connect() as db:
            rows = (db.execute(sql, (cls.SCHEMA,)) if limit is None
                    else db.execute(sql + " LIMIT ?", (cls.SCHEMA, limit))).fetchall()
        return [from_json(cls, r[0]) for r in rows]

    def latest(self, cls: type[T], *, where: dict[str, str], order_by: str,
               before: datetime | None = None) -> T | None:
        """The `cls` record whose `where` fields equal those strings, with the latest `order_by` timestamp.

        One query, answered inside SQLite: no row but the one returned is parsed, however many are stored. Ties on the
        timestamp go to the record stored last. Field names are checked against the record type, so a typo is an
        error rather than an empty answer. With `before`, only records whose `order_by` time is strictly earlier
        count. Example: the last evaluation of an alert rule before a check at `at` is
        `store.latest(AlertEvent, where={"rule_id": rule.rule_id}, order_by="evaluated_at", before=at)`.
        """
        hints = typing.get_type_hints(cls)
        for name in (*where, order_by):
            if name not in {f.name for f in dataclasses.fields(cls)}:
                raise ValidationError(f"{cls.__name__} has no field {name!r}")
        if datetime not in (hints[order_by], *typing.get_args(hints[order_by])):
            raise ValidationError(f"{cls.__name__}.{order_by} is not a timestamp")
        if not where or not all(isinstance(v, str) for v in where.values()):
            raise ValidationError("latest(where=...) needs at least one field, each compared to a string")
        clauses = "".join(" AND json_extract(body, ?) = ?" for _ in where)
        # iso_utc writes either no fraction or six digits: pad to six so the text sorts as the time does
        t = "json_extract(body, ?)"
        key = f"CASE WHEN instr({t}, '.') THEN {t} ELSE substr({t}, 1, length({t}) - 1) || '.000000Z' END"
        bound = f" AND {key} < ?" if before is not None else ""
        sql = (f"SELECT record_id FROM records WHERE record_type = ?{clauses} AND {t} IS NOT NULL{bound} "
               f"ORDER BY {key} DESC, recorded_us DESC, record_id DESC LIMIT 1")
        params: list[object] = [cls.SCHEMA]
        for name, value in where.items():
            params += [f"$.{name}", value]
        params += [f"$.{order_by}"]
        if before is not None:
            params += [f"$.{order_by}"] * 4 + [_sortable(iso_utc(before))]
        params += [f"$.{order_by}"] * 4
        with self.connect() as db:
            row = db.execute(sql, params).fetchone()
        return self.get(cls, row[0]) if row else None          # get() verifies the stored hash

    # ---- datasets and bars -------------------------------------------------------------------------------------
    def put_dataset(self, manifest: DatasetManifest, bars: list[Bar], *, db: sqlite3.Connection | None = None) -> None:
        """Manifest and bars in one transaction (the caller's, when given): a dataset is either wholly present or
        absent."""
        if not manifest.canonical_hash or manifest.canonical_hash != record_hash(manifest):
            raise IntegrityError("DatasetManifest must carry its canonical_hash")
        known = {i.instrument_id for i in manifest.instruments}
        seen: set[tuple[str, datetime]] = set()
        for b in bars:
            if b.instrument_id not in known:
                raise ValidationError(f"bar for {b.instrument_id}, which the manifest does not list")
            if (b.instrument_id, b.end) in seen:
                raise ValidationError(f"two bars for {b.instrument_id} ending {b.end.isoformat()}")
            seen.add((b.instrument_id, b.end))
        if db is None:
            with self.transaction() as tx:
                self._put_dataset(tx, manifest, bars)
        else:
            self._put_dataset(db, manifest, bars)

    def _put_dataset(self, db: sqlite3.Connection, manifest: DatasetManifest, bars: list[Bar]) -> None:
        row = db.execute("SELECT canonical_hash FROM datasets WHERE dataset_id = ?", (manifest.dataset_id,)).fetchone()
        if row:
            if row[0] != manifest.canonical_hash:
                raise IntegrityError(f"dataset {manifest.dataset_id} exists with different content")
            return
        db.execute("INSERT INTO datasets(dataset_id, canonical_hash, body, recorded_us) VALUES (?, ?, ?, ?)",
                   (manifest.dataset_id, manifest.canonical_hash, to_json(manifest), us(manifest.recorded_at)))
        db.executemany("INSERT INTO bars(dataset_id, instrument_id, end_us, available_us, body) VALUES (?, ?, ?, ?, ?)",
                       [(manifest.dataset_id, b.instrument_id, us(b.end), us(b.available_at), to_json(b))
                        for b in bars])
        for ins in manifest.instruments:
            self._put(db, ins.SCHEMA, ins.instrument_id, record_hash(ins), to_json(ins))

    def datasets(self) -> list[DatasetManifest]:
        with self.connect() as db:
            rows = db.execute("SELECT body FROM datasets ORDER BY recorded_us, dataset_id").fetchall()
        return [from_json(DatasetManifest, r[0]) for r in rows]

    def bar_reader(self) -> SqliteBarReader:
        return SqliteBarReader(self)

    def keep_manifest(self, manifest: DatasetManifest, *, db: sqlite3.Connection | None = None) -> str:
        """Keep a manifest of a dataset as recorded elsewhere (ADR-013), named by its canonical hash. Returns it."""
        verify_hash(manifest)
        return self.put_artifact(manifest_bytes(manifest), MANIFEST_MEDIA, db=db)

    def kept_manifest(self, digest: str) -> DatasetManifest | None:
        """The kept manifest whose canonical hash is `digest`, or None."""
        try:
            data = self.read_artifact(digest)
            manifest = from_json(DatasetManifest, data)
        except (DataError, ValidationError, IntegrityError, UnicodeDecodeError):
            return None
        if record_hash(manifest) != digest or manifest.canonical_hash not in (None, digest):
            return None
        return dataclasses.replace(manifest, canonical_hash=digest)

    def kept_manifests(self) -> list[DatasetManifest]:
        """Every kept manifest, ordered by canonical hash, each carrying it."""
        if not self.db_path.exists():
            return []
        with self.connect() as db:
            digests = [r[0] for r in db.execute("SELECT sha256 FROM artifacts WHERE media_type = ? ORDER BY sha256",
                                                (MANIFEST_MEDIA,))]
        return [m for m in (self.kept_manifest(d) for d in digests) if m is not None]

    def dataset_for(self, dataset_id: str, canonical_hash: str) -> DatasetManifest:
        """The dataset a record names by id and canonical hash, as this profile holds it: its own manifest when the
        hashes match, or when the named manifest is kept here and is the same dataset recorded elsewhere (ADR-013).
        MISSING_DATA when this profile has no copy (naming what to re-import); INTEGRITY_MISMATCH when it differs."""
        try:
            local = self.bar_reader().manifest(dataset_id)
        except DataError as e:
            if e.code is not ErrorCode.MISSING_DATA:
                raise
            kept = self.kept_manifest(canonical_hash) if _is_digest(canonical_hash) else None
            if kept is None or kept.dataset_id != dataset_id:
                raise
            raise DataError(f"dataset {dataset_id} ({kept.name!r}, {kept.row_count} bars) is not in this profile: "
                            f"{reimport_hint(kept)}", code=ErrorCode.MISSING_DATA,
                            details={"dataset_id": dataset_id, "raw_sha256": kept.raw_sha256}) from None
        if local.canonical_hash == canonical_hash:
            return local
        kept = self.kept_manifest(canonical_hash) if _is_digest(canonical_hash) else None
        if kept is not None and same_dataset(kept, local):
            return local
        raise IntegrityError(f"dataset {dataset_id} no longer matches the manifest it was named by "
                             f"({canonical_hash[:12]}...)")

    # ---- content-addressed files (spec 11.2) -------------------------------------------------------------------
    def put_artifact(self, data: bytes, media_type: str = "application/octet-stream", *,
                     db: sqlite3.Connection | None = None) -> str:
        """The bytes on disk first (temp file, fsync, rename), then their row: in the caller's transaction when given
        (spec 11.2). A file left by a transaction that then rolled back is unreferenced and harmless."""
        digest = sha256_bytes(data)
        path = self.artifact_path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                if sha256_bytes(pathlib.Path(tmp).read_bytes()) != digest:
                    raise IntegrityError("artifact bytes changed while writing")
                os.replace(tmp, path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
        row = (digest, len(data), media_type, us(datetime.now(UTC)))
        sql = "INSERT OR IGNORE INTO artifacts(sha256, bytes, media_type, recorded_us) VALUES (?, ?, ?, ?)"
        if db is None:
            with self.transaction() as tx:
                tx.execute(sql, row)
        else:
            db.execute(sql, row)
        return digest

    def artifact_path(self, digest: str) -> pathlib.Path:
        if not _is_digest(digest):
            raise ValidationError(f"not a sha256: {digest!r}")
        return self.home / "artifacts" / digest[:2] / digest

    def read_artifact(self, digest: str) -> bytes:
        path = self.artifact_path(digest)
        if not path.exists():
            raise DataError(f"artifact {digest[:12]}... is missing", code=ErrorCode.MISSING_DATA)
        data = path.read_bytes()
        if sha256_bytes(data) != digest:
            raise IntegrityError(f"artifact {digest[:12]}... is corrupted")
        return data

    # ---- research memory (I04) ---------------------------------------------------------------------------------
    def remember(self, event: ResearchMemoryEvent, *, db: sqlite3.Connection | None = None) -> None:
        if db is None:
            with self.transaction() as tx:
                self._remember(tx, event)
        else:
            self._remember(db, event)

    @staticmethod
    def _remember(db: sqlite3.Connection, event: ResearchMemoryEvent) -> None:
        digest = record_hash(event)
        row = db.execute("SELECT canonical_hash FROM memory WHERE event_id = ?", (event.event_id,)).fetchone()
        if row:
            if row[0] != digest:
                raise IntegrityError(f"memory event {event.event_id} exists with different content")
            return
        db.execute("INSERT INTO memory(event_id, kind, at_us, subjects, summary, canonical_hash, body) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (event.event_id, event.kind, us(event.at), " ".join(event.subject_refs), event.summary,
                    digest, to_json(event)))

    def last_memory_hash(self, *, db: sqlite3.Connection | None = None) -> str | None:
        sql = "SELECT canonical_hash FROM memory ORDER BY seq DESC LIMIT 1"
        if db is not None:
            row = db.execute(sql).fetchone()
        else:
            with self.connect() as conn:
                row = conn.execute(sql).fetchone()
        return row[0] if row else None

    def search_memory(self, query: str, *, kinds: tuple[str, ...] = (), limit: int = 20) -> list[ResearchMemoryEvent]:
        """Every word must appear in the summary, kind or subjects (case-insensitive). Newest first. Bounded."""
        words = [w for w in query.lower().split() if w][:12]
        limit = max(1, min(int(limit), 200))
        sql = "SELECT body FROM memory WHERE 1=1"
        args: list[object] = []
        for w in words:
            sql += " AND lower(summary || ' ' || kind || ' ' || subjects) LIKE ? ESCAPE '\\'"
            args.append("%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        with self.connect() as db:
            rows = db.execute(sql, args).fetchall()
        return [from_json(ResearchMemoryEvent, r[0]) for r in rows]


def _is_digest(text: object) -> bool:
    return isinstance(text, str) and len(text) == 64 and all(c in "0123456789abcdef" for c in text)


class SqliteBarReader:
    """The store's BarReader. Passes tests/contract/test_bar_reader.py like the in-memory fake."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def manifest(self, dataset_id: str) -> DatasetManifest:
        with self.store.connect() as db:
            row = db.execute("SELECT body, canonical_hash FROM datasets WHERE dataset_id = ?",
                             (dataset_id,)).fetchone()
        if not row:
            raise DataError(f"no dataset {dataset_id!r}", code=ErrorCode.MISSING_DATA)
        manifest = from_json(DatasetManifest, row[0])
        if manifest.canonical_hash != row[1] or record_hash(manifest) != row[1]:
            raise IntegrityError(f"dataset {dataset_id}: stored manifest does not match its hash")
        return manifest

    def instruments(self, dataset_id: str) -> tuple[Instrument, ...]:
        return self.manifest(dataset_id).instruments

    def bars(self, dataset_id: str, instrument_id: str, *, as_of: datetime, start: datetime | None = None,
             limit: int | None = None) -> tuple[Bar, ...]:
        if as_of.tzinfo is None:
            raise DataError("as_of must be timezone-aware", code=ErrorCode.INVALID_INPUT)
        manifest = self.manifest(dataset_id)
        if instrument_id not in {i.instrument_id for i in manifest.instruments}:
            raise DataError(f"{instrument_id} is not in {dataset_id}", code=ErrorCode.MISSING_DATA)
        sql = "SELECT body FROM bars WHERE dataset_id = ? AND instrument_id = ? AND available_us <= ?"
        args: list[object] = [dataset_id, instrument_id, us(as_of)]
        if start is not None:
            sql += " AND end_us >= ?"
            args.append(us(start))
        if limit is not None:
            if limit <= 0:
                return ()
            sql = sql.replace("SELECT body", "SELECT body, end_us", 1)
            sql = f"SELECT body FROM ({sql} ORDER BY end_us DESC LIMIT ?) ORDER BY end_us"
            args.append(limit)
        else:
            sql += " ORDER BY end_us"
        with self.store.connect() as db:
            rows = db.execute(sql, args).fetchall()
        return tuple(from_json(Bar, r[0]) for r in rows)


__all__ = ["ID_FIELD", "MANIFEST_MEDIA", "RECORD_TYPES", "SqliteBarReader", "Store", "default_home", "manifest_bytes",
           "record_id", "reimport_hint", "same_dataset", "us"]


def _sortable(stamp: str) -> str:
    """iso_utc text padded to six fractional digits, the same key `latest` sorts stored stamps by."""
    return stamp if "." in stamp else stamp[:-1] + ".000000Z"
