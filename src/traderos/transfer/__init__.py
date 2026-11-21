"""I07 portable bundles: export a `.traderos` file, preview it, import it without ever overwriting (spec 10 I07, 11.3).

A bundle is a ZIP with one `manifest.json` (a `bundle/1` record listing every other file with its SHA-256) and:
  records/<type>/<id>.json     each stored record, byte for byte as the store holds it (canonical JSON)
  records/dataset-1/<id>.json  each dataset's manifest, as this profile recorded it
  datasets/<hash>.json         the same dataset as recorded on another machine (ADR-013), named by its canonical hash
  bars/<dataset>.jsonl         a dataset's bars, only where its rights allow raw export and it is not the tutorial
  raw/<sha256>.csv             the original file a dataset came from, under the same rule
  runs/<run>.json              each experiment run (the `runs` table): what reproduce and the holdout gate read
  artifacts/<sha256>.json      the files those runs reference (trades, equity), by their hashes (spec 11.2-5)
  memory/<n>-<event>.json      research memory, numbered in its original order (full_private only)
  passports/<id>-r<n>.md       each Passport rendered for a person to read

Two profiles (11.3: a shareable copy is distinct from a private backup):
  full_private     everything in this profile that its rights allow: a backup to restore on another machine.
  share_redacted   chosen Passports with their trials, challenges and dataset manifests, and their Markdown. Records
                   that name a person (a strategy version's approver, an experiment's registrant) are left out and
                   listed as omissions. Dataset names and rights labels are free text a person typed, so in this copy
                   they read "dataset 1" and "rights: see original": the manifests and Passports that carry them are
                   redacted copies with their own hashes, and the stored records are never touched.
Never exported in either: secrets, keys or tokens (the store holds none; the settings file is not read), vendor raw
data whose rights forbid it, the synthetic tutorial's bars (`traderos init` regenerates them, byte for byte).
An export is byte-identical for the same store: fixed entry times, sorted entries, and no time in the manifest
unless the caller gives one.

Import reads the archive as untrusted: allowlisted relative paths, extensions and compressions only, no links, the
total size and ratio capped from the central directory before anything is read and again while reading, every file's
hash and every record's own hash checked (a missing hash is refused), the file list equal to the manifest, and each
dataset's bars recomputed into the manifest's normalized hash and row count exactly as `data import` builds them.
`preview` runs every check `apply` does and writes nothing (not even the profile folder). `apply` repeats the plan
inside one store transaction and writes everything in it, or nothing: a conflict (same id, different content, in the
store or within the bundle), a failed check, or a Passport whose references are absent refuses the whole bundle.
Applying the same bundle again is a no-op replay.

A dataset whose bars the bundle cannot carry is never created empty: its records come in, the dataset is listed as
"re-import your file to restore it", and a later `data import` of the same file (content-addressed, so the same id)
completes it. Until then a Passport on it is refused MISSING_DATA, naming it.
"""
from __future__ import annotations

import dataclasses
import io
import json
import pathlib
import re
import stat
import zipfile
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from traderos import __version__
from traderos.contracts import (
    RECORD_TYPES,
    ActorRole,
    Bar,
    BundleEntry,
    CapabilityDisabled,
    DataError,
    DatasetManifest,
    ErrorCode,
    EvidencePassport,
    ExportProfile,
    ImportReport,
    IntegrityError,
    Omission,
    PortableBundleManifest,
    Record,
    ResearchMemoryEvent,
    StrategyVersion,
    TraderOSError,
    ValidationError,
    canonical_bytes,
    from_json,
    record_hash,
    sha256_bytes,
    to_json,
    verify_hash,
    with_hash,
)
from traderos.store import MANIFEST_MEDIA, manifest_bytes, record_id, reimport_hint, same_dataset

# OSDev1's cut ruling (wall, 2026-11-20 21:46): bundles do not ship in this release. Export, preview and import answer
# CAPABILITY_DISABLED (exit 3) until the first update turns this on; the code stays built and tested (ADR-013).
RELEASED = False
WAITS_FOR = "the first update after this release (ADR-013)"


def require_released(command: str) -> None:
    """Refuse a bundle command while bundles are not in this release, before anything is read or written."""
    if not RELEASED:
        raise CapabilityDisabled(f"`traderos {command}` is not in this release: portable bundles come in "
                                 f"{WAITS_FOR}. Nothing was read or written.",
                                 details={"command": command, "owner": "lab, OSDev4", "status": "deferred",
                                          "waits_for": WAITS_FOR})


SUFFIX = ".traderos"
MAX_MEMBERS = 200_000
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_RATIO = 200                                  # uncompressed / compressed, per file and for the whole archive
RATIO_FLOOR = 1024 * 1024                        # below this many bytes the ratio is not judged
MAX_BARS = 5_000_000                             # as `data import` (traderos.data.MAX_ROWS)
CHUNK = 1024 * 1024
EXTENSIONS = (".json", ".jsonl", ".md", ".csv")
COMPRESSIONS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
PERSONAL = {"strategy-version/1": "names the person who approved it",
            "experiment-spec/1": "names who registered it", "strategy-draft/1": "a working draft, not evidence",
            "memory-event/1": "private research memory"}
NEUTRAL_RIGHTS = "rights: see original"
RUN_FIELDS = ("run_id", "experiment_id", "spec_hash", "status", "started_us", "ended_us", "artifacts", "error")
RUN_STATUS = ("pending", "running", "completed", "failed", "canceled")
JSON_MEDIA = "application/json"
_SLUG = re.compile(r"[^A-Za-z0-9._@-]")
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)
_HEX = re.compile(r"^[0-9a-f]{64}$")


def _slug(text: str) -> str:
    s = _SLUG.sub("_", text)
    return s if s == text else f"{s}-{sha256_bytes(text.encode())[:8]}"


def _record_path(rtype: str, rid: str) -> str:
    return f"records/{_slug(rtype.replace('/', '-'))}/{_slug(rid)}.json"


def _rights(m: DatasetManifest) -> str:
    return (f"{m.rights.label!r} (stated by {m.rights.stated_by}; raw export "
            f"{'allowed' if m.rights.export_raw_allowed else 'not allowed'})")


def _has_hash_field(cls) -> bool:
    return any(f.name == "canonical_hash" for f in dataclasses.fields(cls))


# ---- export --------------------------------------------------------------------------------------------------------
def _rows(store, schema: str | None = None) -> list[tuple[str, str, str, str]]:
    with store.connect() as db:
        sql = "SELECT record_type, record_id, canonical_hash, body FROM records"
        rows = db.execute(sql + (" WHERE record_type = ?" if schema else "") + " ORDER BY record_type, record_id",
                          (schema,) if schema else ()).fetchall()
    return [tuple(r) for r in rows]


def _passports(store) -> list[tuple[EvidencePassport, str]]:
    """Every Passport revision with its stored body, oldest revision first within each Passport."""
    found = [(from_json(EvidencePassport, body), body) for _, _, _, body in _rows(store, "passport/1")]
    return sorted(found, key=lambda pb: (pb[0].passport_id, pb[0].revision))


def _closure(store, passport_ids: tuple[str, ...]) -> dict[tuple[str, str], str]:
    """The records a set of Passports stands on: every revision, its trials, challenges, specs and versions."""
    want: dict[tuple[str, str], str] = {}
    every = [p for p, _ in _passports(store)]
    for pid in passport_ids:
        revisions = [p for p in every if p.passport_id == pid]
        if not revisions:
            raise DataError(f"no passport {pid!r}", code=ErrorCode.MISSING_DATA)
        for p in revisions:
            want[("passport/1", f"{p.passport_id}@{p.revision}")] = ""
            want.update({("trial/1", t): "" for t in p.trial_ids})
            want.update({("challenge/1", c): "" for c in p.challenge_result_ids})
            want.update({("experiment-spec/1", e): "" for e in p.experiment_ids})
            want[("strategy-version/1", p.strategy_version_id)] = ""
            want.update({("dataset/1", d): "" for d in p.dataset_ids})
    return want


def _runs(store) -> list[dict]:
    with store.connect() as db:
        rows = db.execute("SELECT run_id, experiment_id, spec_hash, status, started_us, ended_us, artifacts, error "
                          "FROM runs ORDER BY run_id").fetchall()
    out = []
    for row in rows:
        run = dict(zip(RUN_FIELDS, row, strict=True))
        run["artifacts"] = json.loads(run["artifacts"])
        out.append(run)
    return out


def export(store, refs: tuple[str, ...] = (), *, profile: ExportProfile | str = ExportProfile.FULL_PRIVATE,
           out: pathlib.Path | None = None, now: datetime | None = None) -> tuple[PortableBundleManifest, pathlib.Path]:
    """Write a bundle. `now` is the only time it carries, and only when given (the export is byte-identical for the
    same store otherwise)."""
    profile = ExportProfile(profile)
    share = profile is ExportProfile.SHARE_REDACTED
    if share and not refs:
        raise ValidationError("a shareable copy names the Passports to share (traderos export PP-... --profile "
                              "share_redacted)")
    from traderos import passports as pp
    files: dict[str, tuple[bytes, str, str]] = {}            # path -> (bytes, record_type, record_id)
    omissions: list[Omission] = []
    rights: list[str] = []
    selected = _closure(store, refs) if refs else None

    def add(path: str, data: bytes, rtype: str, rid: str) -> None:
        files[path] = (data, rtype, rid)

    def wanted(rtype: str, rid: str) -> bool:
        return selected is None or (rtype, rid) in selected

    for rtype, rid, _, body in _rows(store):
        if not wanted(rtype, rid) or rtype == "passport/1":   # Passports are written below, with their Markdown
            continue
        if share and rtype in PERSONAL:
            omissions.append(Omission(ref=f"{rtype} {rid}", reason=PERSONAL[rtype]))
            continue
        add(_record_path(rtype, rid), body.encode("utf-8"), rtype, rid)

    # ---- datasets: this profile's manifest, the same dataset as recorded elsewhere, and bars where rights allow
    local = {m.dataset_id: m for m in store.datasets()}
    kept: dict[str, list[DatasetManifest]] = defaultdict(list)
    for k in store.kept_manifests():
        kept[k.dataset_id].append(k)
    ids = [d for d in sorted(set(local) | set(kept)) if wanted("dataset/1", d)]
    by_file: dict[str, list[DatasetManifest]] = defaultdict(list)
    for m in local.values():
        by_file[m.raw_sha256].append(m)
    redacted: dict[str, str] = {}                           # a manifest's canonical hash -> its redacted copy's
    for n, did in enumerate(ids, 1):
        mine = local.get(did)
        primary = mine or min(kept[did], key=lambda k: k.canonical_hash)
        others = sorted((k for k in kept.get(did, ()) if k.canonical_hash != primary.canonical_hash),
                        key=lambda k: k.canonical_hash)
        if share:
            copy = with_hash(dataclasses.replace(primary, name=f"dataset {n}", rights=dataclasses.replace(
                primary.rights, label=NEUTRAL_RIGHTS, notes="")))
            redacted.update({k.canonical_hash: copy.canonical_hash for k in (primary, *others)})
            add(_record_path("dataset/1", did), to_json(copy).encode("utf-8"), "dataset/1", did)
            rights.append(f"{did} (dataset {n}): {NEUTRAL_RIGHTS}")
            omissions.append(Omission(ref=f"dataset/1 {did}", reason=f"its name and rights label read 'dataset {n}' "
                                      f"and '{NEUTRAL_RIGHTS}' in this copy; the stored record is unchanged"))
            omissions.append(Omission(ref=f"bars {did}", reason="raw market data is not exported in a shareable copy"))
            continue
        add(_record_path("dataset/1", did), to_json(primary).encode("utf-8"), "dataset/1", did)
        for k in others:
            add(f"datasets/{k.canonical_hash}.json", manifest_bytes(k), "dataset-version", k.canonical_hash)
        rights.append(f"{did}: {primary.rights.label} (stated by {primary.rights.stated_by}); raw export "
                      f"{'allowed' if primary.rights.export_raw_allowed else 'not allowed'}")
        if mine is None:
            omissions.append(Omission(ref=f"bars {did}", reason="this profile does not hold the dataset's bars yet: "
                                      + reimport_hint(primary)))
            continue
        forbids = [o for o in by_file[mine.raw_sha256] if o.rights != mine.rights and not o.rights.export_raw_allowed]
        if mine.synthetic:
            omissions.append(Omission(ref=f"bars {did}", reason="synthetic tutorial data: `traderos init` "
                                      "regenerates it byte for byte"))
        elif not mine.rights.export_raw_allowed:
            omissions.append(Omission(ref=f"bars {did}", reason=f"raw market data is not exported under its rights "
                                      f"({mine.rights.label})"))
        elif forbids:
            omissions.append(Omission(ref=f"bars {did}", reason=f"the same file is also {forbids[0].dataset_id}, "
                                      f"whose rights ({forbids[0].rights.label}) do not allow raw export"))
        else:
            with store.connect() as db:
                lines = [r[0] for r in db.execute("SELECT body FROM bars WHERE dataset_id = ? ORDER BY "
                                                  "instrument_id, end_us", (did,))]
            add(f"bars/{_slug(did)}.jsonl", ("\n".join(lines) + "\n").encode("utf-8"), "bars", did)
            try:
                add(f"raw/{mine.raw_sha256}.csv", store.read_artifact(mine.raw_sha256), "raw", mine.raw_sha256)
            except DataError:
                omissions.append(Omission(ref=f"raw {did}", reason="the original file is not in this profile"))

    # ---- runs and the files they reference: what reproduce and the holdout gate read (spec 11.2-5)
    if not share:
        experiments = None if selected is None else {rid for t, rid in selected if t == "experiment-spec/1"}
        with store.connect() as db:
            media = dict(db.execute("SELECT sha256, media_type FROM artifacts").fetchall())
        for run in _runs(store):
            if experiments is not None and run["experiment_id"] not in experiments:
                continue
            add(f"runs/{_slug(run['run_id'])}.json", canonical_bytes(run), "run", run["run_id"])
            for digest in run["artifacts"]:
                try:
                    if media.get(digest) != JSON_MEDIA:
                        raise DataError("not a JSON artifact")
                    add(f"artifacts/{digest}.json", store.read_artifact(digest), "artifact", digest)
                except (DataError, IntegrityError, ValidationError) as e:
                    omissions.append(Omission(ref=f"artifact {digest}", reason=f"run {run['run_id']}: {e.message}"))

    # ---- memory, numbered in its original order
    if profile is ExportProfile.FULL_PRIVATE and selected is None:
        with store.connect() as db:
            events = db.execute("SELECT event_id, body FROM memory ORDER BY seq").fetchall()
        for n, (event_id, body) in enumerate(events, 1):
            add(f"memory/{n:08d}-{_slug(event_id)}.json", body.encode("utf-8"), "memory-event/1", event_id)

    # ---- Passports, and each in Markdown (a shareable copy's are redacted copies, chained to each other)
    chain: dict[str, str] = {}
    for p, body in _passports(store):
        rid = f"{p.passport_id}@{p.revision}"
        if not wanted("passport/1", rid):
            continue
        if share:
            original = p.canonical_hash
            p = with_hash(dataclasses.replace(
                p, data_rights=tuple(NEUTRAL_RIGHTS for _ in p.data_rights),
                dataset_hashes=tuple(redacted.get(h, h) for h in p.dataset_hashes),
                previous_revision_hash=chain.get(p.previous_revision_hash or "", p.previous_revision_hash)))
            chain[original or ""] = p.canonical_hash or ""
            omissions.append(Omission(ref=f"passport/1 {rid}", reason=f"its data rights read '{NEUTRAL_RIGHTS}' in "
                                      "this copy, so its hash differs from the stored record's"))
            body = to_json(p)
        add(_record_path("passport/1", rid), body.encode("utf-8"), "passport/1", rid)
        add(f"passports/{_slug(p.passport_id)}-r{p.revision}.md", pp.render(store, p).encode("utf-8"), "markdown", rid)
    if not files:
        raise DataError("nothing to export", code=ErrorCode.MISSING_DATA)
    entries = tuple(BundleEntry(path=path, record_type=rtype, record_id=rid, sha256=sha256_bytes(data))
                    for path, (data, rtype, rid) in sorted(files.items()))
    bundle_id = "BU-" + sha256_bytes(canonical_bytes([e.sha256 for e in entries] + [profile.value]))[:20]
    manifest = with_hash(PortableBundleManifest(
        bundle_id=bundle_id, package_release=__version__, export_profile=profile, entries=entries,
        omissions=tuple(omissions), rights_notes=tuple(rights), created_at=now))
    target = out or (store.home / "exports" / f"{bundle_id}{SUFFIX}")
    target.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in [("manifest.json", to_json(manifest).encode("utf-8"))] + \
                [(p, files[p][0]) for p in sorted(files)]:
            info = zipfile.ZipInfo(path, date_time=_FIXED_TIME)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(target)
    return manifest, target


# ---- reading an untrusted archive ----------------------------------------------------------------------------------
def _budget(text: str) -> DataError:
    return DataError(f"refused: {text}", code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)


def _open(path: pathlib.Path) -> tuple[PortableBundleManifest, dict[str, bytes]]:
    """The manifest and every listed file, read as untrusted input. Every fault is a typed refusal."""
    if not path.is_file():
        raise ValidationError(f"{path} is not a bundle", code=ErrorCode.INVALID_INPUT)
    try:
        with zipfile.ZipFile(path) as z:
            return _read(z)
    except TraderOSError:
        raise
    except zipfile.BadZipFile as e:
        if "CRC" in str(e):
            raise IntegrityError(f"{path.name}: a file in the bundle is corrupted ({e})") from None
        raise ValidationError(f"{path.name} is not a readable bundle ({e})", code=ErrorCode.INVALID_INPUT) from None
    except (NotImplementedError, zlib.error, EOFError, OSError, ValueError, UnicodeDecodeError, RecursionError,
            zipfile.LargeZipFile) as e:
        raise ValidationError(f"{path.name} is not a readable bundle ({type(e).__name__}: {str(e)[:200]})",
                              code=ErrorCode.INVALID_INPUT) from None


def _read(z: zipfile.ZipFile) -> tuple[PortableBundleManifest, dict[str, bytes]]:
    infos = z.infolist()
    if len(infos) > MAX_MEMBERS:
        raise _budget(f"the bundle has {len(infos)} files; the limit is {MAX_MEMBERS}")
    total = packed = 0
    for info in infos:
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or info.is_dir():
            raise ValidationError(f"refused: {info.filename!r} is a link or a folder", code=ErrorCode.INVALID_INPUT)
        if info.compress_type not in COMPRESSIONS or info.flag_bits & 0x1:
            raise ValidationError(f"refused: {info.filename!r} is encrypted or uses a compression this reader does not "
                                  "accept", code=ErrorCode.INVALID_INPUT)
        if info.filename != "manifest.json":
            BundleEntry(path=info.filename, record_type="x", record_id="x", sha256="0" * 64)   # path rules
            if not info.filename.endswith(EXTENSIONS):
                raise ValidationError(f"refused: {info.filename!r} is not a data file", code=ErrorCode.INVALID_INPUT)
        if info.file_size > MAX_MEMBER_BYTES or (
                info.file_size > RATIO_FLOOR and info.file_size > MAX_RATIO * max(info.compress_size, 1)):
            raise _budget(f"{info.filename!r} expands too far")
        total += info.file_size
        packed += info.compress_size
    if total > MAX_TOTAL_BYTES or (total > RATIO_FLOOR and total > MAX_RATIO * max(packed, 1)):
        raise _budget(f"the bundle expands to {total} bytes from {packed}")
    names = [i.filename for i in infos]
    if len(names) != len(set(names)) or "manifest.json" not in names:
        raise ValidationError("the bundle has no manifest, or a file twice", code=ErrorCode.INVALID_INPUT)
    by_name = {i.filename: i for i in infos}
    left = [total]                               # what the central directory promised, spent while reading

    def member(info: zipfile.ZipInfo) -> bytes:
        out = bytearray()
        with z.open(info) as f:
            while chunk := f.read(CHUNK):
                out += chunk
                left[0] -= len(chunk)
                if len(out) > info.file_size or left[0] < 0:
                    raise _budget(f"{info.filename!r} expands past what the archive says")
        return bytes(out)

    manifest = from_json(PortableBundleManifest, member(by_name["manifest.json"]).decode("utf-8"))
    verify_hash(manifest)
    listed = {e.path for e in manifest.entries}
    if listed != set(names) - {"manifest.json"} or len(listed) != len(manifest.entries):
        raise IntegrityError("the files in the bundle are not the files its manifest lists")
    return manifest, {e.path: member(by_name[e.path]) for e in manifest.entries}


# ---- what the bundle holds, checked on its own -----------------------------------------------------------------------
@dataclass
class _Bundle:
    manifest: PortableBundleManifest
    records: dict[tuple[str, str], Record] = field(default_factory=dict)
    datasets: dict[str, DatasetManifest] = field(default_factory=dict)
    versions: dict[str, DatasetManifest] = field(default_factory=dict)      # canonical hash -> manifest
    bars: dict[str, list[Bar]] = field(default_factory=dict)
    raw: dict[str, bytes] = field(default_factory=dict)
    runs: dict[str, dict] = field(default_factory=dict)
    artifacts: dict[str, bytes] = field(default_factory=dict)
    events: dict[str, tuple[str, ResearchMemoryEvent]] = field(default_factory=dict)   # event id -> (path, event)
    memory: list[ResearchMemoryEvent] = field(default_factory=list)                    # in the source's order
    failures: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def manifest_hashed(self, digest: str) -> DatasetManifest | None:
        for m in (*self.datasets.values(), *self.versions.values()):
            if m.canonical_hash == digest:
                return m
        return None


def _once(store: dict, key, value, same: bool, b: _Bundle, ref: str) -> None:
    """Keep the first copy; a second copy of the same id with other content is a conflict within the bundle."""
    if key in store and not same:
        b.conflicts.append(f"{ref}: the bundle holds it twice, with different content")
    store.setdefault(key, value)


def _no_float(text: str):
    raise ValidationError(f"{text}: a run holds no fractional numbers")


def _parse_run(text: str, rid: str) -> dict:
    run = json.loads(text, parse_float=_no_float, parse_constant=_no_float)
    if not isinstance(run, dict) or tuple(sorted(run)) != tuple(sorted(RUN_FIELDS)):
        raise ValidationError(f"a run has the fields {', '.join(RUN_FIELDS)}")
    ints = [run["started_us"]] + ([] if run["ended_us"] is None else [run["ended_us"]])
    if not all(isinstance(run[k], str) for k in ("run_id", "experiment_id", "spec_hash", "status")) or \
            any(type(x) is not int or not 0 <= x <= 2**53 - 1 for x in ints) or run["status"] not in RUN_STATUS or \
            not (run["error"] is None or isinstance(run["error"], str)) or not isinstance(run["artifacts"], list) or \
            not all(isinstance(d, str) and _HEX.match(d) for d in run["artifacts"]):
        raise ValidationError("a run's fields are not of their types")
    if run["run_id"] != rid:
        raise IntegrityError(f"the file holds run {run['run_id']}, not {rid}")
    return run


def _bars(m: DatasetManifest, bars: list[Bar]) -> list[Bar]:
    """The bars exactly as `data import` would store them for this manifest, or a refusal (H2)."""
    known = {i.instrument_id for i in m.instruments}
    seen: set = set()
    for x in bars:
        if x.instrument_id not in known:
            raise ValidationError(f"a bar for {x.instrument_id}, which the dataset's manifest does not list")
        if x.interval is not m.interval:
            raise ValidationError(f"a {x.interval.value} bar in a {m.interval.value} dataset")
        if (x.instrument_id, x.end) in seen:
            raise ValidationError(f"two bars for {x.instrument_id} ending {x.end.isoformat()}")
        seen.add((x.instrument_id, x.end))
    ordered = sorted(bars, key=lambda x: (x.instrument_id, x.end))
    if len(ordered) != m.row_count or sha256_bytes(canonical_bytes(ordered)) != m.normalized_sha256:
        raise IntegrityError(f"its {len(ordered)} bars are not the {m.row_count} bars its manifest was built from "
                             "(the normalized hash differs)")
    return ordered


def _parse(manifest: PortableBundleManifest, files: dict[str, bytes]) -> _Bundle:
    """Every file parsed and checked against the manifest and the rest of the bundle. Touches no store."""
    b = _Bundle(manifest)
    bars_raw: dict[str, list[Bar]] = {}
    for e in manifest.entries:
        data = files[e.path]
        if sha256_bytes(data) != e.sha256:
            b.failures.append(f"{e.path}: bytes do not match the manifest")
            continue
        if e.record_type == "markdown":
            continue
        try:
            _entry(b, e, data, bars_raw)
        except (TraderOSError, UnicodeDecodeError, RecursionError, ValueError) as err:
            b.failures.append(f"{e.path}: {str(getattr(err, 'message', err))[:200]}")
    for did, bars in sorted(bars_raw.items()):
        m = b.datasets.get(did)
        if m is None:
            b.failures.append(f"bars {did}: the bundle carries no manifest for this dataset; bars never travel alone")
            continue
        if not m.rights.export_raw_allowed:
            b.failures.append(f"bars {did}: the dataset's rights ({m.rights.label}) do not allow its bars to be "
                              "exported")
            continue
        try:
            b.bars[did] = _bars(m, bars)
        except TraderOSError as err:
            b.failures.append(f"bars {did}: {err.message}")
    for sha in sorted(b.raw):
        owners = [m for m in b.datasets.values() if m.raw_sha256 == sha]
        if not owners:
            b.failures.append(f"raw {sha}: no dataset in the bundle was built from this file")
        elif not all(m.rights.export_raw_allowed for m in owners):
            b.failures.append(f"raw {sha}: the rights of {owners[0].dataset_id} do not allow this file to be exported")
    for digest, v in sorted(b.versions.items()):
        m = b.datasets.get(v.dataset_id)
        if m is None or not same_dataset(m, v):
            b.failures.append(f"datasets/{digest}.json: not the same dataset as any manifest in the bundle")
    ms = sorted(b.datasets.values(), key=lambda m: m.dataset_id)
    for i, m in enumerate(ms):
        for other in ms[:i]:
            if other.raw_sha256 == m.raw_sha256 and other.rights != m.rights:
                b.conflicts.append(f"dataset {m.dataset_id}: the same file (sha256 {m.raw_sha256[:12]}...) is also "
                                   f"{other.dataset_id} in the bundle, under rights {_rights(other)} against "
                                   f"{_rights(m)}; one file has one set of rights")
    referenced = {d for run in b.runs.values() for d in run["artifacts"]}
    for digest in sorted(set(b.artifacts) - referenced):
        b.failures.append(f"artifact {digest}: no run in the bundle references it")
    b.memory = [ev for _, ev in sorted(b.events.values(), key=lambda pe: pe[0])]   # numbered by the exporter
    return b


def _entry(b: _Bundle, e: BundleEntry, data: bytes, bars_raw: dict[str, list[Bar]]) -> None:
    text = data.decode("utf-8")
    kind = e.record_type
    if kind == "bars":
        if text.count("\n") > MAX_BARS + 1:
            raise _budget(f"more than {MAX_BARS} bars")
        bars = [from_json(Bar, line) for line in text.split("\n") if line]
        _once(bars_raw, e.record_id, bars, False, b, f"bars {e.record_id}")
    elif kind == "raw":
        if sha256_bytes(data) != e.record_id:
            raise IntegrityError("raw file does not match its name")
        b.raw[e.record_id] = data
    elif kind == "artifact":
        if sha256_bytes(data) != e.record_id:
            raise IntegrityError("the file does not match its name")
        b.artifacts[e.record_id] = data
    elif kind == "dataset-version":
        m = from_json(DatasetManifest, data)
        if m.canonical_hash not in (None, e.record_id) or record_hash(m) != e.record_id:
            raise IntegrityError("the manifest does not match its name")
        b.versions[e.record_id] = dataclasses.replace(m, canonical_hash=e.record_id)
    elif kind == "dataset/1":
        m = from_json(DatasetManifest, text)
        verify_hash(m)                                       # a manifest without its hash is refused
        if m.dataset_id != e.record_id:
            raise IntegrityError(f"the file holds {m.dataset_id}, not {e.record_id}")
        have = b.datasets.get(m.dataset_id)
        _once(b.datasets, m.dataset_id, m, have is None or have.canonical_hash == m.canonical_hash, b,
              f"dataset/1 {m.dataset_id}")
    elif kind == "memory-event/1":
        ev = from_json(ResearchMemoryEvent, text)
        verify_hash(ev)
        if ev.event_id != e.record_id:
            raise IntegrityError(f"the file holds {ev.event_id}, not {e.record_id}")
        have = b.events.get(ev.event_id)
        _once(b.events, ev.event_id, (e.path, ev), have is None or have[1].canonical_hash == ev.canonical_hash, b,
              f"memory-event/1 {ev.event_id}")
    elif kind == "run":
        run = _parse_run(text, e.record_id)
        _once(b.runs, run["run_id"], run, b.runs.get(run["run_id"], run) == run, b, f"run {run['run_id']}")
    else:
        cls = RECORD_TYPES.get(kind)
        if cls is None or kind in ("bundle/1", "import-report/1"):
            raise ValidationError(f"unknown record type {kind}", code=ErrorCode.SCHEMA_UNSUPPORTED)
        rec = from_json(cls, text)
        if _has_hash_field(cls):
            verify_hash(rec)                                 # a record without its hash is refused
        if record_id(rec) != e.record_id:
            raise IntegrityError(f"the file holds {record_id(rec)}, not {e.record_id}")
        key = (cls.SCHEMA, e.record_id)
        have = b.records.get(key)
        _once(b.records, key, rec, have is None or record_hash(have) == record_hash(rec), b, f"{kind} {e.record_id}")


# ---- what the bundle would do to this profile ----------------------------------------------------------------------
class _Here:
    """What this profile already holds, read through one connection; or nothing, for a profile not made yet."""

    def __init__(self, store, db) -> None:
        self.store, self.db = store, db
        self._manifests: list[DatasetManifest] | None = None

    def one(self, sql: str, args: tuple):
        return None if self.db is None else self.db.execute(sql, args).fetchone()

    def record_hash(self, rtype: str, rid: str) -> str | None:
        row = self.one("SELECT canonical_hash FROM records WHERE record_type = ? AND record_id = ?", (rtype, rid))
        return row[0] if row else None

    def record(self, cls, rid: str):
        row = self.one("SELECT body FROM records WHERE record_type = ? AND record_id = ?", (cls.SCHEMA, rid))
        return from_json(cls, row[0]) if row else None

    def dataset(self, did: str) -> DatasetManifest | None:
        row = self.one("SELECT body FROM datasets WHERE dataset_id = ?", (did,))
        return from_json(DatasetManifest, row[0]) if row else None

    def kept(self, digest: str) -> DatasetManifest | None:
        row = self.one("SELECT 1 FROM artifacts WHERE sha256 = ? AND media_type = ?", (digest, MANIFEST_MEDIA))
        return self.store.kept_manifest(digest) if row else None

    def manifests(self) -> list[DatasetManifest]:
        """Every manifest this profile knows: its datasets and the kept ones."""
        if self._manifests is None:
            self._manifests = [] if self.db is None else [
                *(from_json(DatasetManifest, r[0]) for r in self.db.execute("SELECT body FROM datasets")),
                *(m for r in self.db.execute("SELECT sha256 FROM artifacts WHERE media_type = ?", (MANIFEST_MEDIA,))
                  if (m := self.store.kept_manifest(r[0])) is not None)]
        return self._manifests

    def memory(self, event_id: str) -> ResearchMemoryEvent | None:
        row = self.one("SELECT body FROM memory WHERE event_id = ?", (event_id,))
        return from_json(ResearchMemoryEvent, row[0]) if row else None

    def run(self, run_id: str) -> dict | None:
        row = self.one("SELECT run_id, experiment_id, spec_hash, status, started_us, ended_us, artifacts, error FROM "
                       "runs WHERE run_id = ?", (run_id,))
        if row is None:
            return None
        run = dict(zip(RUN_FIELDS, row, strict=True))
        run["artifacts"] = json.loads(run["artifacts"])
        return run

    def artifact(self, digest: str) -> bool:
        return self.one("SELECT 1 FROM artifacts WHERE sha256 = ?", (digest,)) is not None


@dataclass
class _Plan:
    bundle: _Bundle
    additions: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    datasets: list[tuple[DatasetManifest, list[Bar]]] = field(default_factory=list)
    keep: dict[str, DatasetManifest] = field(default_factory=dict)
    records: list[Record] = field(default_factory=list)
    raw: list[bytes] = field(default_factory=list)
    artifacts: list[bytes] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    memory: list[ResearchMemoryEvent] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.failures

    @property
    def changes(self) -> bool:
        return bool(self.additions or self.keep)

    def report(self, mode: str, *, ok: bool | None = None) -> ImportReport:
        return ImportReport(bundle_id=self.bundle.manifest.bundle_id, mode=mode, additions=tuple(self.additions),
                            duplicates=tuple(self.duplicates), conflicts=tuple(self.conflicts),
                            excluded=self.bundle.manifest.omissions, missing_sources=tuple(self.missing),
                            hash_failures=tuple(self.failures), ok=self.ok if ok is None else ok,
                            created_at=datetime.now(UTC))


def _plan(store, db, b: _Bundle) -> _Plan:
    """Classify every item against this profile: new, already here (the same content), or a conflict."""
    here = _Here(store, db)
    plan = _Plan(b, conflicts=list(b.conflicts), failures=list(b.failures))
    _plan_datasets(plan, here, b)
    for (rtype, rid), rec in sorted(b.records.items()):
        _classify(plan, f"{rtype} {rid}", here.record_hash(rtype, rid), record_hash(rec), rec, plan.records)
    for passport in (r for (t, _), r in sorted(b.records.items()) if t == "passport/1"):
        _references(plan, here, b, passport)
    for run_id, run in sorted(b.runs.items()):
        spec = b.records.get(("experiment-spec/1", run["experiment_id"]))
        stated = spec.canonical_hash if spec is not None else here.record_hash("experiment-spec/1",
                                                                                 run["experiment_id"])
        if stated is None:
            plan.failures.append(f"run {run_id}: its experiment {run['experiment_id']} is neither in the bundle nor in "
                                 "this profile")
        elif stated != run["spec_hash"]:
            plan.failures.append(f"run {run_id}: it ran another version of experiment {run['experiment_id']}")
        plan.missing += [f"artifact {d} of run {run_id}: not in the bundle (the source profile did not have it)"
                         for d in run["artifacts"] if d not in b.artifacts and not here.artifact(d)]
        have = here.run(run_id)
        _classify(plan, f"run {run_id}", None if have is None else canonical_bytes(have), canonical_bytes(run), run,
                  plan.runs)
    for digest, data in sorted(b.artifacts.items()):
        _classify(plan, f"artifact {digest}", "here" if here.artifact(digest) else None, "here", data, plan.artifacts)
    for sha, data in sorted(b.raw.items()):
        _classify(plan, f"raw {sha}", "here" if here.artifact(sha) else None, "here", data, plan.raw)
    for ev in b.memory:
        have = here.memory(ev.event_id)
        same = have is not None and (have.canonical_hash == ev.canonical_hash or _same_fact(have, ev, here, b))
        _classify(plan, f"memory-event/1 {ev.event_id}", None if have is None else ("same" if same else "other"),
                  "same", ev, plan.memory)
    return plan


def _classify(plan: _Plan, ref: str, have, mine, item, todo: list) -> None:
    if have is None:
        plan.additions.append(ref)
        todo.append(item)
    elif have == mine:
        plan.duplicates.append(ref)
    else:
        plan.conflicts.append(ref)


def _plan_datasets(plan: _Plan, here: _Here, b: _Bundle) -> None:
    for did, m in sorted(b.datasets.items()):
        mine = here.dataset(did)
        split = [o for o in here.manifests() if o.raw_sha256 == m.raw_sha256 and o.rights != m.rights]
        if split:                                          # one file, one set of rights (ADR-013)
            plan.conflicts.append(f"dataset {did}: the same file (sha256 {m.raw_sha256[:12]}...) is in this profile "
                                  f"as {split[0].dataset_id} under rights {_rights(split[0])}; the bundle brings it "
                                  f"under {_rights(m)}. One file has one set of rights.")
            continue
        versions = [v for v in b.versions.values() if v.dataset_id == did]
        if mine is not None:
            if mine.canonical_hash != m.canonical_hash and not same_dataset(mine, m):
                plan.conflicts.append(f"dataset/1 {did}")
                continue
            plan.duplicates.append(f"dataset/1 {did}")         # the same dataset, perhaps recorded at another time
            if did in b.bars:
                plan.duplicates.append(f"bars {did}")
            for v in (m, *versions):
                if v.canonical_hash != mine.canonical_hash and here.kept(v.canonical_hash) is None:
                    plan.keep[v.canonical_hash or ""] = v
            continue
        if any(k.dataset_id == did and not same_dataset(k, m) for k in here.manifests()):
            plan.conflicts.append(f"dataset/1 {did}: this profile already knows it with other bars")
            continue
        for ins in m.instruments if did in b.bars else ():
            h = record_hash(ins)
            theirs = b.records.get(("instrument/1", ins.instrument_id))
            if here.record_hash("instrument/1", ins.instrument_id) not in (None, h) or \
                    (theirs is not None and record_hash(theirs) != h):
                plan.conflicts.append(f"instrument/1 {ins.instrument_id}: dataset {did} describes it otherwise")
        if did in b.bars:
            plan.additions += [f"dataset/1 {did}", f"bars {did}"]
            plan.datasets.append((m, b.bars[did]))
            kept = versions
        else:                                              # never created empty (H1): re-import completes it
            plan.missing.append(f"dataset {did} {m.name!r}: {m.row_count} bars from a file with sha256 "
                                f"{m.raw_sha256}; {reimport_hint(m)}")
            kept = [m, *versions]
        for v in kept:
            if here.kept(v.canonical_hash or "") is None:
                plan.keep[v.canonical_hash or ""] = v


def _resolves(here: _Here, b: _Bundle, did: str, digest: str) -> bool:
    """A dataset reference (id, canonical hash) that this profile can answer after the import, now or after the file
    is re-imported."""
    m = b.manifest_hashed(digest)
    if m is not None and m.dataset_id == did:
        return True
    mine = here.dataset(did)
    if mine is not None and mine.canonical_hash == digest:
        return True
    kept = here.kept(digest)
    return kept is not None and kept.dataset_id == did


def _references(plan: _Plan, here: _Here, b: _Bundle, p: EvidencePassport) -> None:
    """A Passport comes in only with everything it names, here or in the bundle (I01: it must reopen)."""
    me = f"passport/1 {p.passport_id}@{p.revision}"

    def find(cls, rid: str):
        return b.records.get((cls.SCHEMA, rid)) or here.record(cls, rid)

    def absent(what: str) -> None:
        plan.failures.append(f"{me} stands on {what}, which is neither in the bundle nor in this profile"
                             + (" (a shareable copy is for reading: open its Markdown)"
                                if b.manifest.export_profile is ExportProfile.SHARE_REDACTED else ""))

    from traderos.contracts import ChallengeResult, ExperimentSpec, Trial
    version = find(StrategyVersion, p.strategy_version_id)
    if version is None or version.semantic_hash != p.strategy_semantic_hash:
        absent(f"strategy-version/1 {p.strategy_version_id}")
    for eid, h in zip(p.experiment_ids, p.experiment_spec_hashes, strict=True):
        spec = find(ExperimentSpec, eid)
        if spec is None or record_hash(spec) != h:
            absent(f"experiment-spec/1 {eid}")
    for did, h in zip(p.dataset_ids, p.dataset_hashes, strict=True):
        if not _resolves(here, b, did, h):
            absent(f"dataset/1 {did} (manifest {h[:12]}...)")
    for tid in p.trial_ids:
        if find(Trial, tid) is None:
            absent(f"trial/1 {tid}")
    for cid in p.challenge_result_ids:
        if find(ChallengeResult, cid) is None:
            absent(f"challenge/1 {cid}")
    if p.revision > 1:
        prev = find(EvidencePassport, f"{p.passport_id}@{p.revision - 1}")
        if prev is None or prev.canonical_hash != p.previous_revision_hash:
            absent(f"passport/1 {p.passport_id}@{p.revision - 1}")


def _same_fact(have: ResearchMemoryEvent, ev: ResearchMemoryEvent, here: _Here, b: _Bundle) -> bool:
    """The same memory event recorded on another machine: the same fact (who, what, about which records, with which
    outcome, in which words) about the same content. When it was recorded and where it sat in the chain differ;
    an event about a dataset recorded at another time points at that dataset's other manifest (ADR-013)."""
    def fact(e: ResearchMemoryEvent):
        return e.actor_role, e.kind, e.subject_refs, e.outcome_codes, e.summary
    if fact(have) != fact(ev):
        return False
    if have.payload_hash == ev.payload_hash:
        return True

    def manifest(digest: str) -> DatasetManifest | None:
        found = b.manifest_hashed(digest) or here.kept(digest)
        for did in ev.subject_refs:
            mine = here.dataset(did)
            if mine is not None and mine.canonical_hash == digest:
                found = mine
        return found
    a, c = manifest(have.payload_hash), manifest(ev.payload_hash)
    return a is not None and c is not None and same_dataset(a, c)


# ---- import --------------------------------------------------------------------------------------------------------
class _Rollback(Exception):
    def __init__(self, report: ImportReport) -> None:
        super().__init__("rollback")
        self.report = report


def import_bundle(store, path: pathlib.Path, *, apply: bool = False) -> ImportReport:
    """Preview (writes nothing, creates nothing) or apply (all of it in one store transaction, or none of it)."""
    manifest, files = _open(pathlib.Path(path))
    b = _parse(manifest, files)
    if store.db_path.exists():
        with store.connect() as db:
            plan = _plan(store, db, b)
    else:
        plan = _plan(store, None, b)                       # no profile yet: everything would be new
    if not apply:
        return plan.report("preview")
    if not plan.ok:                                        # refused before anything, the profile folder included
        return plan.report("apply")
    store.init()
    try:
        with store.transaction() as db:                    # the plan again, under the write lock, then every write
            plan = _plan(store, db, b)
            if not plan.ok or not plan.changes:            # refused, or a replay with nothing new: nothing written
                raise _Rollback(plan.report("apply"))
            _write(store, db, plan)
            done = plan.report("apply")
            digest = store.put_artifact(to_json(done).encode("utf-8"), JSON_MEDIA, db=db)   # the report, as a file
            store.remember(with_hash(ResearchMemoryEvent(
                event_id=f"ME-bundle.imported-{manifest.bundle_id}-{digest[:12]}", actor_role=ActorRole.HUMAN,
                kind="bundle.imported", subject_refs=(manifest.bundle_id, digest),
                summary=f"Imported bundle {manifest.bundle_id}: {len(done.additions)} added, {len(done.duplicates)} "
                        "already here", payload_hash=record_hash(done),
                previous_event_hash=store.last_memory_hash(db=db), at=done.created_at)), db=db)
    except _Rollback as r:
        return r.report
    return done


def _write(store, db, plan: _Plan) -> None:
    """Files first (content-addressed, atomic), then every row, in the caller's one transaction (spec 11.2)."""
    for data in plan.raw:
        store.put_artifact(data, "text/csv", db=db)
    for data in plan.artifacts:
        store.put_artifact(data, JSON_MEDIA, db=db)
    for m in plan.keep.values():
        store.keep_manifest(m, db=db)
    for m, bars in plan.datasets:
        store.put_dataset(m, bars, db=db)
    for rec in plan.records:
        store.put(rec, db=db)
    for run in plan.runs:
        db.execute("INSERT INTO runs(run_id, experiment_id, spec_hash, status, started_us, ended_us, artifacts, error) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (run["run_id"], run["experiment_id"], run["spec_hash"], run["status"], run["started_us"],
                    run["ended_us"], json.dumps(run["artifacts"]), run["error"]))
    for ev in plan.memory:                                 # in the order the source recorded them
        store.remember(ev, db=db)


def describe(r: ImportReport) -> str:
    if r.mode == "preview":
        head, verb = "Preview (nothing written)", "to add"
    elif r.ok:
        head, verb = "Imported", "added"
    else:
        head, verb = "Apply refused (nothing written)", "to add"
    lines = [f"{head}: bundle {r.bundle_id}: {len(r.additions)} {verb}, {len(r.duplicates)} already here, "
             f"{len(r.conflicts)} conflicts, {len(r.hash_failures)} failed checks."]
    lines += [f"  conflict (never overwritten): {c}" for c in r.conflicts[:20]]
    lines += [f"  failed check: {h}" for h in r.hash_failures[:20]]
    lines += [f"  needs its source: {m}" for m in r.missing_sources[:20]]
    lines += [f"  not in the bundle: {o.ref} ({o.reason})" for o in r.excluded[:20]]
    if r.mode == "preview" and r.ok:
        lines.append("  Run again with --apply to import.")
    elif not r.ok:
        lines.append("  Refused: nothing was imported.")
    return "\n".join(lines)


__all__ = ["SUFFIX", "describe", "export", "import_bundle"]
