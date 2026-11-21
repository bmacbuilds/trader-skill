"""I07 portable bundles: export, preview, import; never overwrite; never trust the archive (spec 10 I07, 11.3).

OSDev6's review of #15 (changes needed at b6564a3) is pinned here item by item: H1 a dataset is never restored empty,
H2 bars are recomputed into their manifest's hashes, H3 preview runs every check and apply is one transaction, H4 runs
and their files travel so reproduce and the holdout gate agree; the five mediums and the lows. tools/mutants/transfer.py
holds the mutants these tests kill.
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import pathlib
import shutil
import stat
import struct
import types
import zipfile
from datetime import UTC, datetime

import pytest

from traderos import challenges as fs
from traderos import cli, transfer
from traderos.contracts import (
    BundleEntry,
    DataError,
    ErrorCode,
    EvidencePassport,
    ExperimentSpec,
    IntegrityError,
    OwnerAction,
    PortableBundleManifest,
    ResearchMemoryEvent,
    StrategyVersion,
    Trial,
    ValidationError,
    from_json,
    sha256_bytes,
    to_json,
    with_hash,
)
from traderos.data import ImportOptions, import_into, tutorial
from traderos.research import Lab
from traderos.store import Store
from traderos.strategies import rulebook


@pytest.fixture(autouse=True)
def bundles_released(monkeypatch):
    """Bundles are gated off for this release (OSDev1's cut ruling, ADR-013); these tests exercise the built code that
    the first update switches on. tests/contract/test_bundles_gated.py proves the shipped refusal."""
    monkeypatch.setattr(transfer, "RELEASED", True)

DOC = {"schema_version": "strategy/1", "name": "life", "hypothesis": "pullbacks revert", "universe": ["TUTMKT"],
       "frequency": "1d", "direction": "long",
       "entry": {"all": [{"above": ["close", {"sma": 50}]}, {"below": [{"rsi": 2}, 15]}]},
       "exit": {"any": [{"above": ["close", {"sma": 5}]}, {"bars_held": 8}]}}
CSV = b"symbol,timestamp_utc,open,high,low,close,volume\nSPY,2026-11-19T21:00:00Z,600,604,598,603,100\n" \
      b"SPY,2026-11-20T21:00:00Z,603,606,600,605,120\n"
PERSON_FILE = "Jane Q Customer 4111222233334444.csv"         # a file named like a person and an account number
PERSON_RIGHTS = "acct 98765432 broker statement"
LEAKS = (b"Jane", b"Customer", b"4111222233334444", b"98765432", b"broker statement", b"Pat Example")


def _slice(tut: bytes, symbol: str, name: str) -> bytes:
    """One synthetic symbol, three years, renamed: a user's own CSV, as `data import` would take it."""
    lines = tut.decode().splitlines()
    rows = [x.replace(symbol, name) for x in lines[1:] if x.startswith(f"{symbol},") and x.split(",")[1] >= "2023"]
    return ("\n".join([lines[0], *rows]) + "\n").encode()


def _approve(store, doc):
    d = rulebook.draft(doc)
    v = rulebook.approve(d, OwnerAction(actor="Pat Example", method="cli_confirm", at=datetime.now(UTC),
                                        statement=d.semantic_hash[:12]))
    store.put(d)
    store.put(v)
    return v


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setattr(fs, "CONTROLS", 3)
    base = tmp_path_factory.mktemp("transfer")
    tut = tutorial.csv_bytes()
    blank = Store(base / "blank").init()
    m = tutorial.load(blank)                                      # what `traderos init` makes: the tutorial only
    store = Store(base / "src")
    shutil.copytree(blank.home, store.home)
    import_into(store, CSV, ImportOptions(name="mine", export_raw_allowed=True))
    import_into(store, CSV.replace(b"SPY", b"QQQ"), ImportOptions(name="vendor", rights_label="vendor-x"))
    csv_path = base / PERSON_FILE                                 # rights forbid raw export, as for every CLI import
    csv_path.write_bytes(_slice(tut, "TUTMKT", "ABC"))
    person = import_into(store, csv_path.read_bytes(), ImportOptions(name=PERSON_FILE, rights_label=PERSON_RIGHTS))
    open_data = import_into(store, _slice(tut, "TUTTEC", "XYZ"),   # rights allow raw export: its bars travel
                            ImportOptions(name="open data", rights_label="cc-by", export_raw_allowed=True))
    lab = Lab(store)
    p = lab.run(lab.register(_approve(store, DOC), m.dataset_id).experiment_id).passport
    q = lab.run(lab.register(_approve(store, dict(DOC, name="life-abc", universe=["ABC"])),
                             person.dataset_id).experiment_id).passport
    r = lab.run(lab.register(_approve(store, dict(DOC, name="life-xyz", universe=["XYZ"])),
                             open_data.dataset_id).experiment_id).passport
    yield types.SimpleNamespace(store=store, p=p, q=q, r=r, person=person, open=open_data, csv=csv_path, blank=blank,
                                tutorial=m, tutorial_csv=tut)
    mp.undo()


def profile(world, where: pathlib.Path) -> Store:
    """A fresh profile with the tutorial, as `traderos init` leaves it."""
    shutil.copytree(world.blank.home, where)
    return Store(where)


def snapshot(home: pathlib.Path) -> dict[str, str]:
    """Every file in a profile, by its bytes (SQLite's transient -wal/-shm files aside)."""
    return {str(f.relative_to(home)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(home.rglob("*"))
            if f.is_file() and not f.name.endswith(("-wal", "-shm"))}


def names(path):
    with zipfile.ZipFile(path) as z:
        return z.namelist()


def read(path) -> tuple[PortableBundleManifest, dict[str, list]]:
    with zipfile.ZipFile(path) as z:
        manifest = from_json(PortableBundleManifest, z.read("manifest.json"))
        return manifest, {e.path: [e.record_type, e.record_id, z.read(e.path)] for e in manifest.entries}


def write(out, manifest, items, *, rehash=True) -> pathlib.Path:
    """A bundle an attacker could make: any files, a manifest listing them, its hash recomputed."""
    entries = tuple(BundleEntry(path=p, record_type=t, record_id=i, sha256=sha256_bytes(d))
                    for p, (t, i, d) in sorted(items.items()))
    m = dataclasses.replace(manifest, entries=entries)
    m = with_hash(m) if rehash else m
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", to_json(m))
        for p, (_, _, d) in sorted(items.items()):
            z.writestr(p, d)
    out.write_bytes(buf.getvalue())
    return out


def forge(path, out, edit) -> pathlib.Path:
    manifest, items = read(path)
    edit(items)
    return write(out, manifest, items)


def run_cli(capsys, *args) -> tuple[int, dict]:
    code = cli.main(["--json", *args])
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else {})


@pytest.fixture(scope="module")
def full(world, tmp_path_factory):
    _, path = transfer.export(world.store, out=tmp_path_factory.mktemp("full") / "all.traderos")
    return path


# ---- the happy path, and what a backup carries ---------------------------------------------------------------------
def test_full_backup_restores_on_a_clean_profile_and_the_passport_reopens(world, full, tmp_path):
    from traderos import passports
    store, p = world.store, world.p
    secret = store.home / "config" / "settings.toml"
    secret.write_text('api_key = "sk-never-exported-123"\n')
    manifest, path = transfer.export(store, out=tmp_path / "all.traderos")
    secret.unlink()
    files = names(path)
    assert "manifest.json" in files and any(f.startswith("memory/") for f in files)
    assert len([f for f in files if f.startswith("bars/")]) == 2          # mine and open data; not the others
    assert len([f for f in files if f.startswith("raw/")]) == 2
    assert len([f for f in files if f.startswith("runs/")]) == 3          # H4: every experiment's run travels
    assert any(f.startswith("artifacts/") for f in files)
    assert any("vendor-x" in o.reason for o in manifest.omissions)         # the vendor file's rights forbid raw export
    assert not any("settings" in f or "config" in f for f in files)
    with zipfile.ZipFile(path) as z:
        assert not any(b"sk-never-exported" in z.read(n) for n in z.namelist())
    fresh = profile(world, tmp_path / "second")                           # bars of the tutorial come from init
    preview = transfer.import_bundle(fresh, path)
    assert preview.mode == "preview" and preview.ok and preview.additions and not fresh.all(EvidencePassport)
    done = transfer.import_bundle(fresh, path, apply=True)
    assert done.mode == "apply" and done.ok
    person = next(x for x in done.missing_sources if world.person.dataset_id in x)   # H1: named, never created empty
    assert PERSON_FILE in person and f"{world.person.row_count} bars" in person and world.person.raw_sha256 in person
    assert world.person.dataset_id not in {m.dataset_id for m in fresh.datasets()}
    before = snapshot(fresh.home)
    again = transfer.import_bundle(fresh, path, apply=True)
    assert again.ok and again.additions == () and again.duplicates               # H3: a replay writes nothing
    assert snapshot(fresh.home) == before
    assert passports.reopen(fresh, p.passport_id).canonical_hash == p.canonical_hash
    assert fresh.search_memory("bundle imported")
    with store.connect() as db:                                           # LOW: memory in its original order
        source = [r[0] for r in db.execute("SELECT event_id FROM memory ORDER BY seq")]
    with fresh.connect() as db:
        restored = [r[0] for r in db.execute("SELECT event_id FROM memory ORDER BY seq") if r[0] in set(source)]
    assert restored == source


def test_export_is_byte_identical_and_holds_no_time_unless_given(world, tmp_path, capsys):
    store, p = world.store, world.p
    a, pa = transfer.export(store, out=tmp_path / "a.traderos")
    b, pb = transfer.export(store, out=tmp_path / "b.traderos")
    assert pa.read_bytes() == pb.read_bytes()                            # the manifest included
    assert a.created_at is None and a == b
    with zipfile.ZipFile(pa) as z:
        assert {i.date_time for i in z.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert from_json(PortableBundleManifest, z.read("manifest.json")).created_at is None
    when = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    c, pc = transfer.export(store, (p.passport_id,), out=tmp_path / "c.traderos", now=when)
    assert c.created_at == when
    assert not any(f.startswith("memory/") for f in names(pc))             # a Passport's export carries no memory
    code, out = run_cli(capsys, "--home", str(store.home), "export", p.passport_id, "--out", str(tmp_path / "d"),
                        "--now", "2026-11-20T18:00:00Z")
    assert code == 0 and out["result"]["bundle"]["created_at"] == "2026-11-20T18:00:00Z"


# ---- MEDIUM 3: a shareable copy names no one -------------------------------------------------------------------------
def test_a_shareable_copy_names_no_person_or_account_and_never_edits_a_stored_record(world, tmp_path):
    store, q = world.store, world.q
    manifest, path = transfer.export(store, (q.passport_id, world.r.passport_id), profile="share_redacted",
                                     out=tmp_path / "s.traderos")
    files = names(path)
    assert any(f.startswith("passports/") and f.endswith(".md") for f in files)
    assert not any(f.startswith(("bars/", "raw/", "memory/", "runs/", "artifacts/", "datasets/")) for f in files)
    reasons = {o.ref.split()[0]: o.reason for o in manifest.omissions}
    assert "strategy-version/1" in reasons and "experiment-spec/1" in reasons
    with zipfile.ZipFile(path) as z:
        body = b"".join(z.read(n) for n in z.namelist())
        shared = from_json(EvidencePassport, z.read(f"records/passport-1/{q.passport_id}@{q.revision}.json"))
        dataset = json.loads(z.read(f"records/dataset-1/{world.person.dataset_id}.json"))
        datasets = [json.loads(z.read(n)) for n in files if n.startswith("records/dataset-1/")]
    assert not [w for w in (*LEAKS, b"open data", b"cc-by") if w in body]
    assert sorted(d["name"] for d in datasets) == ["dataset 1", "dataset 2"]
    assert {d["rights"]["label"] for d in datasets} == {transfer.NEUTRAL_RIGHTS}
    assert shared.data_rights == (transfer.NEUTRAL_RIGHTS,)
    assert shared.dataset_hashes == (dataset["canonical_hash"],)          # the copy is consistent with itself
    assert dataclasses.replace(shared, data_rights=q.data_rights, dataset_hashes=q.dataset_hashes,
                               canonical_hash=q.canonical_hash) == q        # only what names someone differs
    assert store.get(EvidencePassport, f"{q.passport_id}@{q.revision}") == q   # the stored record is untouched
    assert world.store.bar_reader().manifest(world.person.dataset_id).name == PERSON_FILE
    with pytest.raises(ValidationError):
        transfer.export(store, (), profile="share_redacted", out=tmp_path / "x.traderos")
    with pytest.raises(DataError):
        transfer.export(store, ("PP-nope",), out=tmp_path / "y.traderos")


# ---- conflicts -------------------------------------------------------------------------------------------------------
def test_a_conflict_is_refused_and_nothing_is_written(world, tmp_path, capsys):
    store, p = world.store, world.p
    _, path = transfer.export(store, (p.passport_id,), out=tmp_path / "c.traderos")
    other = Store(tmp_path / "other").init()
    _, items = read(path)
    trial = from_json(Trial, next(d for k, (_, _, d) in items.items() if k.startswith("records/trial-1/")))
    other.put(dataclasses.replace(trial, selection_trace="something else"))
    before = snapshot(other.home)
    report = transfer.import_bundle(other, path, apply=True)
    assert not report.ok and report.conflicts and report.mode == "apply"
    assert snapshot(other.home) == before and not other.all(EvidencePassport)
    assert transfer.describe(report).startswith("Apply refused")          # LOW: never labelled a preview
    nobody = Store(tmp_path / "nobody")
    _, shared = transfer.export(store, (p.passport_id,), profile="share_redacted", out=tmp_path / "s.traderos")
    assert not transfer.import_bundle(nobody, shared, apply=True).ok and not nobody.home.exists()
    code = cli.main(["--home", str(other.home), "import", str(path), "--apply"])
    err = capsys.readouterr().err
    assert code == 3 and "Apply refused" in err and "Preview" not in err


# ---- H1: a dataset whose bars cannot travel is never created empty ---------------------------------------------------
def test_restore_then_reimport_the_file_completes_the_dataset_and_reproduce_passes(world, full, tmp_path, capsys):
    home, q, person = str(profile(world, tmp_path / "t").home), world.q, world.person
    code, out = run_cli(capsys, "--home", home, "import", str(full), "--apply")
    assert code == 0 and out["result"]["ok"]
    hint = next(x for x in out["result"]["missing_sources"] if person.dataset_id in x)
    assert PERSON_FILE in hint and f"{person.row_count} bars" in hint and person.raw_sha256 in hint
    assert "re-import your file" in hint
    code, out = run_cli(capsys, "--home", home, "data", "list")
    assert person.dataset_id not in json.dumps(out)                       # never created without its bars
    code, out = run_cli(capsys, "--home", home, "passport", "reproduce", q.passport_id)
    assert code == 3 and out["failure"]["code"] == "MISSING_DATA"         # never INTEGRITY_MISMATCH
    assert person.dataset_id in out["failure"]["message"] and "re-import" in out["failure"]["message"]
    code, out = run_cli(capsys, "--home", home, "data", "import", str(world.csv), "--rights-label", PERSON_RIGHTS)
    assert code == 0 and out["result"]["dataset_id"] == person.dataset_id, out
    assert out["result"]["row_count"] == person.row_count
    with Store(home).connect() as db:
        assert db.execute("SELECT count(*) FROM bars WHERE dataset_id = ?", (person.dataset_id,)).fetchone()[0] \
            == person.row_count
    code, out = run_cli(capsys, "--home", home, "passport", "reproduce", q.passport_id)
    assert code == 0 and out["result"]["reproduced"] is True


def test_import_the_file_first_then_restore_and_reproduce_passes(world, full, tmp_path, capsys):
    home, q, person = str(profile(world, tmp_path / "t").home), world.q, world.person
    code, out = run_cli(capsys, "--home", home, "data", "import", str(world.csv), "--rights-label", PERSON_RIGHTS)
    assert code == 0 and out["result"]["canonical_hash"] != person.canonical_hash   # recorded at another time
    code, out = run_cli(capsys, "--home", home, "import", str(full), "--apply")
    assert code == 0 and out["result"]["ok"] and not out["result"]["conflicts"]
    assert f"dataset/1 {person.dataset_id}" in out["result"]["duplicates"]
    code, out = run_cli(capsys, "--home", home, "passport", "reproduce", q.passport_id)
    assert code == 0 and out["result"]["reproduced"] is True
    third = Store(tmp_path / "third").init()                      # a restore of a restore, with a third recording
    import_into(third, world.csv.read_bytes(), ImportOptions(name=PERSON_FILE, rights_label=PERSON_RIGHTS),
                now=datetime(2026, 2, 2, tzinfo=UTC))
    _, again = transfer.export(Store(home), out=tmp_path / "again.traderos")
    assert any(f.startswith("datasets/") for f in names(again))   # the source's recording travels beside this one
    report = transfer.import_bundle(third, again, apply=True)
    assert report.ok, (report.conflicts, report.hash_failures)
    from traderos import passports
    assert passports.reopen(third, q.passport_id, q.revision).canonical_hash == q.canonical_hash
    assert passports.reopen(third, q.passport_id).revision == 2             # the replay's revision, on this recording


# ---- H2: bars are recomputed into their manifest's hashes ------------------------------------------------------------
def _mine(world):
    return next(m for m in world.store.datasets() if m.name == "mine")


def _planted(items, mine) -> None:
    key = next(k for k, (t, i, _) in items.items() if t == "bars" and i == mine.dataset_id)
    items[key][2] = items[key][2].replace(b'"close":"603"', b'"close":"598"').replace(b'"close":"605"',
                                                                                     b'"close":"600"')


def _closes(store, dataset_id):
    with store.connect() as db:
        return [json.loads(r[0])["close"] for r in db.execute("SELECT body FROM bars WHERE dataset_id = ? "
                                                               "ORDER BY end_us", (dataset_id,))]


def test_planted_bars_under_a_genuine_manifest_refuse_the_whole_bundle(world, full, tmp_path):
    mine = _mine(world)
    bad = forge(full, tmp_path / "planted.traderos", lambda items: _planted(items, mine))
    target = Store(tmp_path / "t").init()
    before = snapshot(target.home)
    preview = transfer.import_bundle(target, bad)
    assert not preview.ok and any("normalized hash" in f for f in preview.hash_failures)
    report = transfer.import_bundle(target, bad, apply=True)
    assert not report.ok and report.mode == "apply" and snapshot(target.home) == before
    m = import_into(target, CSV, ImportOptions(name="mine", export_raw_allowed=True))
    assert m.dataset_id == mine.dataset_id and _closes(target, m.dataset_id) == ["603", "605"]


def test_a_planted_dataset_never_shadows_the_genuine_file(world, full, tmp_path):
    """The attacker also rewrites the manifest to fit the planted bars: the bundle is self-consistent, so it comes in,
    but `data import` of the genuine file then refuses and names the dataset instead of returning the planted one."""
    mine = _mine(world)

    def edit(items):
        _planted(items, mine)
        key = next(k for k, (t, i, _) in items.items() if t == "bars" and i == mine.dataset_id)
        from traderos.contracts import Bar, canonical_bytes
        bars = sorted((from_json(Bar, x) for x in items[key][2].decode().split("\n") if x),
                      key=lambda x: (x.instrument_id, x.end))
        forged = with_hash(dataclasses.replace(mine, normalized_sha256=sha256_bytes(canonical_bytes(bars))))
        mk = next(k for k, (t, i, _) in items.items() if t == "dataset/1" and i == mine.dataset_id)
        items[mk][2] = to_json(forged).encode()
    bad = forge(full, tmp_path / "forged.traderos", edit)
    genuine = Store(tmp_path / "g").init()
    import_into(genuine, CSV, ImportOptions(name="mine", export_raw_allowed=True))
    refused = transfer.import_bundle(genuine, bad)
    assert not refused.ok and f"dataset/1 {mine.dataset_id}" in refused.conflicts
    target = Store(tmp_path / "t").init()
    assert transfer.import_bundle(target, bad, apply=True).ok
    with pytest.raises(IntegrityError) as e:
        import_into(target, CSV, ImportOptions(name="mine", export_raw_allowed=True))
    assert mine.dataset_id in e.value.message


# ---- H3: preview runs every check; apply is one transaction ----------------------------------------------------------
@pytest.mark.parametrize("fault", ["duplicate", "instrument"])
def test_what_apply_would_refuse_the_preview_refuses_and_apply_writes_nothing(world, full, tmp_path, fault):
    if fault == "duplicate":                                  # two trial/1 files, one id, different content
        _, path = transfer.export(world.store, (world.p.passport_id,), out=tmp_path / "p.traderos")

        def edit(items):
            k = next(k for k in items if k.startswith("records/trial-1/"))
            t = from_json(Trial, items[k][2])
            items["records/trial-1/zz-copy.json"] = ["trial/1", t.trial_id,
                                                     to_json(dataclasses.replace(t, selection_trace="x")).encode()]
        bad = forge(path, tmp_path / "dup.traderos", edit)
    else:                                                     # a bar for an instrument the manifest does not list
        mine = _mine(world)

        def edit(items):
            k = next(k for k, (t, i, _) in items.items() if t == "bars" and i == mine.dataset_id)
            items[k][2] = items[k][2].replace(b"INS-SPY-UNKNOWN-USD", b"INS-ZZZ-UNKNOWN-USD", 1)
        bad = forge(full, tmp_path / "ins.traderos", edit)
    target = profile(world, tmp_path / "t")
    preview = transfer.import_bundle(target, bad)
    assert not preview.ok
    assert any("twice" in c for c in preview.conflicts) if fault == "duplicate" else \
        any("does not list" in f for f in preview.hash_failures)
    before = snapshot(target.home)
    for _ in range(2):                                        # and again: refused the same way, never half-written
        report = transfer.import_bundle(target, bad, apply=True)
        assert not report.ok and report.mode == "apply"
        assert snapshot(target.home) == before


# ---- H4: runs and their files travel: reproduce and the holdout gate agree -------------------------------------------
def test_a_restored_profile_reproduces_and_remembers_opened_holdouts_like_the_source(world, full, tmp_path, capsys):
    source = Store(tmp_path / "source")
    shutil.copytree(world.store.home, source.home)
    restored = profile(world, tmp_path / "restored")
    assert transfer.import_bundle(restored, full, apply=True).ok
    runs = ("SELECT run_id, experiment_id, spec_hash, status, started_us, ended_us, artifacts, error FROM runs "
            "ORDER BY run_id")
    with source.connect() as a, restored.connect() as b:
        assert a.execute(runs).fetchall() == b.execute(runs).fetchall()
    for s in (source, restored):
        lab = Lab(s)
        spec = s.get(ExperimentSpec, world.p.experiment_ids[0])
        later = lab.register(s.get(StrategyVersion, spec.strategy_version_id), spec.dataset_id, seed=8)
        assert lab._holdout_compromised(later) is True                    # QT02: the opened holdout is remembered
    answers = []
    for s in (source, restored):
        code, out = run_cli(capsys, "--home", str(s.home), "passport", "reproduce", world.r.passport_id)
        assert code == 0, out
        answers.append((out["result"]["reproduced"], out["result"]["passport"]["revision"],
                        out["result"]["passport"]["trial_ids"]))
    assert answers[0] == answers[1] and answers[0][0] is True


def test_a_run_that_cannot_record_its_results_fails_and_is_never_left_running(world, tmp_path):
    store = Store(tmp_path / "s")
    shutil.copytree(world.store.home, store.home)
    spec0 = store.get(ExperimentSpec, world.r.experiment_ids[0])
    lab = Lab(store)
    spec = lab.register(store.get(StrategyVersion, spec0.strategy_version_id), spec0.dataset_id, seed=11)
    store.put(Trial(trial_id=f"TR-{spec.experiment_id}-development", experiment_id=spec.experiment_id, ordinal=1,
                    parameters={}, split="development", status="completed", selection_trace="already here"))
    with pytest.raises(IntegrityError):
        lab.run(spec.experiment_id)
    with store.connect() as db:
        assert [r[0] for r in db.execute("SELECT status FROM runs WHERE experiment_id = ?",
                                         (spec.experiment_id,))] == ["failed"]


# ---- MEDIUM 1: every record hash, memory included -------------------------------------------------------------------
def _edit_json(items, prefix: str, change) -> str:
    k = next(k for k in sorted(items) if k.startswith(prefix))
    doc = json.loads(items[k][2])
    change(doc)
    items[k][2] = json.dumps(doc).encode()
    return k


@pytest.mark.parametrize("fault", ["record", "record-null", "dataset", "record-id", "raw", "memory", "memory-null"])
def test_every_record_is_checked_and_a_missing_hash_is_refused(world, full, tmp_path, fault):
    """The manifest's file hashes are recomputed each time (an attacker can): what must catch it is the record's own
    hash, the dataset's, the raw file's name and the record's id."""
    def edit(items):
        if fault == "record":
            _edit_json(items, "records/passport-1/", lambda d: d.update(hypothesis="edited"))
        elif fault == "record-null":
            _edit_json(items, "records/passport-1/", lambda d: d.update(canonical_hash=None))
        elif fault == "dataset":                         # the person's: no bars travel to catch it otherwise
            _edit_json(items, f"records/dataset-1/{world.person.dataset_id}",
                       lambda d: d.update(row_count=d["row_count"] + 1))
        elif fault == "record-id":                       # a draft: no Passport names it, so only its id can
            k = next(k for k in sorted(items) if k.startswith("records/strategy-draft-1/"))
            items[k][1] = "DR-somebody-else"
        elif fault == "raw":
            k = next(k for k in sorted(items) if k.startswith("raw/"))
            items[k][2] = items[k][2].replace(b"603", b"598")
        elif fault == "memory":
            _edit_json(items, "memory/", lambda d: d.update(summary=d["summary"] + " (edited)"))
        else:
            _edit_json(items, "memory/", lambda d: d.update(canonical_hash=None))
    bad = forge(full, tmp_path / f"{fault}.traderos", edit)
    target = Store(tmp_path / "t").init()
    before = snapshot(target.home)
    report = transfer.import_bundle(target, bad, apply=True)
    assert not report.ok and report.hash_failures and snapshot(target.home) == before


def test_a_memory_event_that_differs_from_a_stored_one_is_a_conflict(world, full, tmp_path):
    target = Store(tmp_path / "t").init()
    _, items = read(full)
    ev = from_json(ResearchMemoryEvent, next(d for k, (_, _, d) in items.items() if k.startswith("memory/")))
    target.remember(with_hash(dataclasses.replace(ev, summary="something else happened")))
    report = transfer.import_bundle(target, full)
    assert f"memory-event/1 {ev.event_id}" in report.conflicts and not report.ok
    assert f"memory-event/1 {ev.event_id}" not in report.duplicates


# ---- MEDIUM 4: the decompression budget ------------------------------------------------------------------------------
def test_osdev6s_bomb_is_refused_before_anything_is_read(tmp_path, monkeypatch):
    path = tmp_path / "bomb.traderos"
    zero = b"\0" * (1024 * 1024)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for n in range(1023):                                 # 1,023 files of 1 MiB: each under the per-file ratio
            z.writestr(f"records/x-1/{n:04d}.json", zero)
        z.writestr("manifest.json", b"{}")
    assert path.stat().st_size < 2 * 1024 * 1024

    def never(*a, **k):
        raise AssertionError("a member was read")
    monkeypatch.setattr(zipfile.ZipFile, "open", never)
    with pytest.raises(DataError) as e:
        transfer.import_bundle(Store(tmp_path / "t"), path)
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED


def _sizes(path: pathlib.Path, out: pathlib.Path, members: dict[bytes, tuple[int, int]]) -> pathlib.Path:
    """The archive with its central directory claiming other sizes (compressed, uncompressed) for some members."""
    data = bytearray(path.read_bytes())
    for name, (packed, size) in members.items():
        at = _central(bytes(data), name)
        struct.pack_into("<II", data, at + 20, packed, size)
    out.write_bytes(bytes(data))
    return out


@pytest.mark.parametrize("cap", ["count", "member", "total"])
def test_count_and_size_caps_are_read_from_the_central_directory(world, tmp_path, monkeypatch, cap):
    _, path = transfer.export(world.store, (world.p.passport_id,), out=tmp_path / "p.traderos")
    members = [n.encode() for n in names(path) if n != "manifest.json"]
    if cap == "count":
        monkeypatch.setattr(transfer, "MAX_MEMBERS", len(members))
        bad = path
    elif cap == "member":                                     # one file claims 300 MiB, from 2 MiB: ratio 150
        bad = _sizes(path, tmp_path / "big.traderos", {members[0]: (2 << 20, 300 << 20)})
    else:                                                     # five of 250 MiB each: every one under its caps
        bad = _sizes(path, tmp_path / "big.traderos", {m: (2 << 20, 250 << 20) for m in members[:5]})

    def never(*a, **k):
        raise AssertionError("a member was read")
    monkeypatch.setattr(zipfile.ZipFile, "open", never)
    with pytest.raises(DataError) as e:
        transfer.import_bundle(Store(tmp_path / "t"), bad)
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED


def test_a_name_twice_or_another_compression_is_refused(world, tmp_path):
    _, path = transfer.export(world.store, (world.p.passport_id,), out=tmp_path / "p.traderos")
    manifest, items = read(path)
    twice = tmp_path / "twice.traderos"
    with zipfile.ZipFile(twice, "w", zipfile.ZIP_DEFLATED) as z, pytest.warns(UserWarning):
        z.writestr("manifest.json", to_json(manifest))
        for p, (_, _, d) in sorted(items.items()):
            z.writestr(p, d)
        z.writestr(sorted(items)[0], items[sorted(items)[0]][2])
    with pytest.raises(ValidationError) as e:
        transfer.import_bundle(Store(tmp_path / "t"), twice)
    assert e.value.code is ErrorCode.INVALID_INPUT and "twice" in e.value.message
    other = tmp_path / "bz.traderos"
    with zipfile.ZipFile(other, "w", zipfile.ZIP_BZIP2) as z:
        z.writestr("manifest.json", to_json(manifest))
        for p, (_, _, d) in sorted(items.items()):
            z.writestr(p, d)
    with pytest.raises(ValidationError) as e:
        transfer.import_bundle(Store(tmp_path / "t"), other)
    assert "compression" in e.value.message


def test_the_budget_holds_while_reading_too(world, tmp_path, monkeypatch):
    """A reader that hands back more than the archive declared is stopped as it reads."""
    _, path = transfer.export(world.store, (world.p.passport_id,), out=tmp_path / "p.traderos")
    real = zipfile.ZipFile.open

    def generous(self, name, *a, **k):
        f = real(self, name, *a, **k)
        data = f.read() + b" " * 16
        return io.BytesIO(data)
    monkeypatch.setattr(zipfile.ZipFile, "open", generous)
    with pytest.raises(DataError) as e:
        transfer.import_bundle(Store(tmp_path / "t"), path)
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED


# ---- MEDIUM 5: the same dataset recorded at another time is the same -------------------------------------------------
def test_profiles_holding_the_same_data_exchange_bundles_whenever_each_imported_it(world, full, tmp_path):
    from traderos import passports
    old = Store(tmp_path / "old").init()                       # a profile made before the tutorial's time was fixed
    earlier = import_into(old, tutorial.csv_bytes(), tutorial.OPTIONS, now=datetime(2026, 1, 5, tzinfo=UTC))
    assert earlier.dataset_id == world.tutorial.dataset_id and earlier.canonical_hash != world.tutorial.canonical_hash
    report = transfer.import_bundle(old, full, apply=True)
    assert report.ok and not report.conflicts, report.conflicts
    assert f"dataset/1 {earlier.dataset_id}" in report.duplicates
    assert f"memory-event/1 ME-{earlier.dataset_id}" in report.duplicates   # its import event: the same fact
    assert passports.reopen(old, world.p.passport_id).canonical_hash == world.p.canonical_hash
    assert old.dataset_for(earlier.dataset_id, world.tutorial.canonical_hash) == earlier
    back = Store(tmp_path / "back")                            # and the other way: old -> new, through a bundle
    shutil.copytree(world.blank.home, back.home)
    _, path = transfer.export(old, out=tmp_path / "old.traderos")
    assert transfer.import_bundle(back, path, apply=True).ok
    assert passports.reopen(back, world.p.passport_id).canonical_hash == world.p.canonical_hash


# ---- LOW -------------------------------------------------------------------------------------------------------------
def _central(data: bytes, name: bytes) -> int:
    """Offset of `name`'s central directory header."""
    at = 0
    while (at := data.find(b"PK\x01\x02", at)) >= 0:
        n = struct.unpack_from("<H", data, at + 28)[0]
        if data[at + 46:at + 46 + n] == name:
            return at
        at += 4
    raise LookupError(name)


@pytest.mark.parametrize("fault", ["crc", "lying-size", "not-utf8", "deep-json", "compression"])
def test_archive_faults_are_typed_refusals(world, tmp_path, capsys, fault):
    _, path = transfer.export(world.store, (world.p.passport_id,), out=tmp_path / "p.traderos")
    data = bytearray(path.read_bytes())
    if fault in ("crc", "lying-size", "compression"):
        at = _central(bytes(data), b"manifest.json")
        if fault == "crc":
            struct.pack_into("<I", data, at + 16, struct.unpack_from("<I", data, at + 16)[0] ^ 1)
        elif fault == "lying-size":
            struct.pack_into("<I", data, at + 24, struct.unpack_from("<I", data, at + 24)[0] - 7)
        else:
            struct.pack_into("<H", data, at + 10, 99)
            struct.pack_into("<H", data, struct.unpack_from("<I", data, at + 42)[0] + 8, 99)
        bad = tmp_path / "bad.traderos"
        bad.write_bytes(bytes(data))
    else:
        manifest, items = read(path)
        bad = write(tmp_path / "bad.traderos", manifest, items)
        body = b"\xff\xfe not utf-8" if fault == "not-utf8" else b"[" * 200_000 + b"]" * 200_000
        with zipfile.ZipFile(bad) as z:
            rest = [(i.filename, z.read(i.filename)) for i in z.infolist() if i.filename != "manifest.json"]
        with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", body)
            for n, d in rest:
                z.writestr(n, d)
    code = cli.main(["--home", str(tmp_path / "t"), "import", str(bad)])
    err = capsys.readouterr().err
    assert code == 3 and err.startswith("refused ["), err


def test_a_preview_never_creates_the_profile_folder(world, tmp_path, capsys):
    _, path = transfer.export(world.store, (world.p.passport_id,), out=tmp_path / "p.traderos")
    home = tmp_path / "nobody-here"
    code, out = run_cli(capsys, "--home", str(home), "import", str(path))
    assert code == 0 and out["result"]["mode"] == "preview" and out["result"]["additions"]
    assert not home.exists()
    from traderos import api
    assert api.import_portable_bundle(str(path), store=Store(home)).mode == "preview" and not home.exists()


def test_bars_never_travel_without_their_dataset(world, full, tmp_path):
    mine = _mine(world)

    def edit(items):
        del items[next(k for k, (t, i, _) in items.items() if t == "dataset/1" and i == mine.dataset_id)]
    bad = forge(full, tmp_path / "orphan.traderos", edit)
    report = transfer.import_bundle(Store(tmp_path / "t").init(), bad)
    assert not report.ok and any(f.startswith(f"bars {mine.dataset_id}") for f in report.hash_failures)
    assert any(f.startswith(f"raw {mine.raw_sha256}") for f in report.hash_failures)
    assert f"bars {mine.dataset_id}" not in report.additions


def test_bars_whose_rights_forbid_export_are_refused(world, full, tmp_path):
    vendor = next(m for m in world.store.datasets() if m.name == "vendor")
    with world.store.connect() as db:
        lines = [r[0] for r in db.execute("SELECT body FROM bars WHERE dataset_id = ? ORDER BY instrument_id, end_us",
                                          (vendor.dataset_id,))]
    bad = forge(full, tmp_path / "vendor.traderos", lambda items: items.update(
        {f"bars/{vendor.dataset_id}.jsonl": ["bars", vendor.dataset_id, ("\n".join(lines) + "\n").encode()]}))
    report = transfer.import_bundle(Store(tmp_path / "t").init(), bad)
    assert not report.ok and any("vendor-x" in f for f in report.hash_failures)


def test_one_file_has_one_set_of_rights(world, full, tmp_path, capsys):
    home = str(tmp_path / "t")
    code, out = run_cli(capsys, "--home", home, "data", "import", str(world.csv), "--rights-label", "mine to share")
    assert code == 0
    other = out["result"]["dataset_id"]
    code, out = run_cli(capsys, "--home", home, "data", "import", str(world.csv), "--rights-label", PERSON_RIGHTS)
    assert code == 3 and out["failure"]["code"] == "DATA_RIGHTS_UNKNOWN" and other in out["failure"]["message"]
    report = transfer.import_bundle(Store(home), full)
    assert not report.ok and any(world.person.dataset_id in c and other in c for c in report.conflicts)


def test_an_older_profile_with_one_file_under_two_rights_exports_neither_copys_data(tmp_path):
    from traderos.data import build
    store = Store(tmp_path / "old").init()
    for opts in (ImportOptions(name="shared", rights_label="mine", export_raw_allowed=True),
                 ImportOptions(name="licensed", rights_label="vendor-y")):
        m, bars = build(CSV, opts)                            # as `data import` stored them before it refused this
        store.put_artifact(CSV, "text/csv")
        store.put_dataset(m, bars)
    manifest, path = transfer.export(store, out=tmp_path / "b.traderos")
    assert not any(f.startswith(("bars/", "raw/")) for f in names(path))
    licensed = next(m for m in store.datasets() if m.name == "licensed")
    assert any(licensed.dataset_id in o.reason for o in manifest.omissions)


def test_a_passport_comes_in_only_with_what_it_stands_on(world, tmp_path):
    store, p = world.store, world.p
    _, shared = transfer.export(store, (p.passport_id,), profile="share_redacted", out=tmp_path / "s.traderos")
    report = transfer.import_bundle(Store(tmp_path / "t").init(), shared, apply=True)
    assert not report.ok and any("strategy-version/1" in f and "shareable copy" in f for f in report.hash_failures)
    _, path = transfer.export(store, (p.passport_id,), out=tmp_path / "p.traderos")
    gone = forge(path, tmp_path / "gone.traderos",
                 lambda items: items.pop(next(k for k in items if k.startswith("records/trial-1/"))))
    report = transfer.import_bundle(Store(tmp_path / "u").init(), gone)
    assert not report.ok and any("trial/1" in f for f in report.hash_failures)


# ---- the archive itself ----------------------------------------------------------------------------------------------
def _rewrite(path, out, change):
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in change(items):
            z.writestr(info, data)
    out.write_bytes(buf.getvalue())
    return out


@pytest.mark.parametrize("attack", ["tamper", "extra", "traversal", "symlink", "exe", "bomb", "missing", "manifest"])
def test_an_untrusted_archive_is_refused(world, tmp_path, attack):
    store, p = world.store, world.p
    _, path = transfer.export(store, (p.passport_id,), out=tmp_path / "ok.traderos")

    def change(items):
        out = list(items)
        if attack == "tamper":
            i = next(k for k, (info, _) in enumerate(out) if info.filename.startswith("records/trial-1/"))
            out[i] = (out[i][0], out[i][1].replace(b'"trial/1"', b'"trial/1" '))
        elif attack == "extra":
            out.append((zipfile.ZipInfo("records/trial-1/zz.json"), b"{}"))
        elif attack == "traversal":
            out.append((zipfile.ZipInfo("../evil.json"), b"{}"))
        elif attack == "symlink":
            info = zipfile.ZipInfo("records/link.json")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            out.append((info, b"/etc/passwd"))
        elif attack == "exe":
            out.append((zipfile.ZipInfo("records/run.sh"), b"#!/bin/sh\n"))
        elif attack == "bomb":
            info = zipfile.ZipInfo("records/bomb.json")
            info.compress_type = zipfile.ZIP_DEFLATED              # 8 MB of zeros packs into a few KB
            out.append((info, b"0" * (8 * 1024 * 1024)))
        elif attack == "missing":
            out = [x for x in out if not x[0].filename.startswith("passports/")]
        elif attack == "manifest":                              # the file list intact, an omission reworded
            out = [(i, d.replace(b"regenerates it", b"regenerated it") if i.filename == "manifest.json" else d)
                   for i, d in out]
        return out
    bad = _rewrite(path, tmp_path / f"{attack}.traderos", change)
    fresh = Store(tmp_path / f"f-{attack}").init()
    if attack == "tamper":
        report = transfer.import_bundle(fresh, bad, apply=True)
        assert not report.ok and report.hash_failures and not fresh.all(EvidencePassport)
        return
    expected = {"extra": ErrorCode.INTEGRITY_MISMATCH, "missing": ErrorCode.INTEGRITY_MISMATCH,
                "manifest": ErrorCode.INTEGRITY_MISMATCH,
                "bomb": ErrorCode.RESOURCE_BUDGET_EXCEEDED}.get(attack, ErrorCode.INVALID_INPUT)
    with pytest.raises((ValidationError, DataError, IntegrityError)) as e:
        transfer.import_bundle(fresh, bad)
    assert e.value.code is expected, (attack, e.value)
    assert not fresh.all(EvidencePassport)


def test_not_a_bundle_and_a_future_schema(world, tmp_path):
    store, p = world.store, world.p
    junk = tmp_path / "junk.traderos"
    junk.write_bytes(b"not a zip")
    with pytest.raises(ValidationError):
        transfer.import_bundle(Store(tmp_path / "j").init(), junk)
    _, path = transfer.export(store, (p.passport_id,), out=tmp_path / "f.traderos")
    future = _rewrite(path, tmp_path / "future.traderos", lambda items: [
        (i, d.replace(b'"bundle/1"', b'"bundle/9"') if i.filename == "manifest.json" else d) for i, d in items])
    with pytest.raises(ValidationError) as e:
        transfer.import_bundle(Store(tmp_path / "k").init(), future)
    assert e.value.code is ErrorCode.SCHEMA_UNSUPPORTED


def test_the_commands(world, tmp_path, capsys):
    store, p = world.store, world.p
    home = str(store.home)
    out = tmp_path / "cli.traderos"
    assert cli.main(["--json", "--home", home, "export", p.passport_id, "--out", str(out)]) == 0
    capsys.readouterr()
    target = str(tmp_path / "target")
    assert cli.main(["--json", "--home", target, "import", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["mode"] == "preview"
    assert cli.main(["--json", "--home", target, "import", str(out), "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["mode"] == "apply"
    assert cli.main(["--home", target, "import", str(out), "--apply", "--dry-run"]) == 2
