"""Mutants of the portable bundle's guards (I07, ADR-013): edit one guard, run the tests, put it back.

    python tools/mutants/transfer.py                 # every mutant, with this Python
    python tools/mutants/transfer.py --python PY     # with another interpreter
    python tools/mutants/transfer.py --only H1       # mutants whose name contains "H1"
    python tools/mutants/transfer.py --check         # only check that every mutant applies (no tests run)

Each mutant replaces one exact piece of source (it must occur exactly once, or the run says NOT APPLIED) and runs
the transfer tests with bytecode off (`-B`, PYTHONDONTWRITEBYTECODE, every `__pycache__` removed before and after),
so a mutant can never be judged by the previous one's cached bytecode. KILLED means a test failed; SURVIVED means the
tests did not notice the change. The source is restored in a `finally`, whatever happens. The exit code is 0 only
when every mutant applied and was killed.

Groups: "osdev6" are the 11 OSDev6 found surviving in his review of #15 (at b6564a3), restated against this code;
"review" are one mutant per fix for that review (H1-H4, the mediums and the lows).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
T = "src/traderos/transfer/__init__.py"
S = "src/traderos/store/__init__.py"
D = "src/traderos/data/__init__.py"
R = "src/traderos/research/__init__.py"
C = "src/traderos/commands/transfer.py"
TESTS = ("tests/unit/test_transfer.py",)

RUN = "            outcome = self._run(spec, book, bars, windows, costs, manifest, replay, compromised, suffix)\n"
RUN_BODY = (RUN +
            "            artifacts = self._store_artifacts(outcome)\n"
            "            for t in outcome.trials:\n                self.store.put(t)\n"
            "            for c in outcome.challenges:\n                self.store.put(c)\n"
            "        except Exception as e:\n"
            '            self._run_state(run_id, spec, "failed", error=f"{type(e).__name__}: {e}")\n'
            "            raise\n")
RUN_BODY_OLD = (RUN +
                "        except Exception as e:\n"
                '            self._run_state(run_id, spec, "failed", error=f"{type(e).__name__}: {e}")\n'
                "            raise\n"
                "        artifacts = self._store_artifacts(outcome)\n"
                "        for t in outcome.trials:\n            self.store.put(t)\n"
                "        for c in outcome.challenges:\n            self.store.put(c)\n")

MUTANTS = [
    # ---- osdev6: his 11 survivors at b6564a3, restated against this code
    ("osdev6", "a record's own hash not checked", T,
     "        if _has_hash_field(cls):\n            verify_hash(rec)",
     "        if False:\n            verify_hash(rec)"),
    ("osdev6", "a dataset manifest's hash not checked", T,
     "        verify_hash(m)                                       # a manifest without its hash is refused",
     "        pass"),
    ("osdev6", "a raw file not checked against its name", T,
     '        if sha256_bytes(data) != e.record_id:\n'
     '            raise IntegrityError("raw file does not match its name")',
     '        if False:\n            raise IntegrityError("raw file does not match its name")'),
    ("osdev6", "a record's id not checked against its entry", T,
     "        if record_id(rec) != e.record_id:\n            raise IntegrityError",
     "        if False:\n            raise IntegrityError"),
    ("osdev6", "a name twice in the archive accepted", T,
     '    if len(names) != len(set(names)) or "manifest.json" not in names:',
     '    if "manifest.json" not in names:'),
    ("osdev6", "the file-count cap", T, "    if len(infos) > MAX_MEMBERS:", "    if False:"),
    ("osdev6", "the per-file size cap", T, "        if info.file_size > MAX_MEMBER_BYTES or (",
     "        if False or ("),
    ("osdev6", "a dataset with other bars classified as the same", T,
     "            if mine.canonical_hash != m.canonical_hash and not same_dataset(mine, m):",
     "            if False:"),
    ("osdev6", "a shareable copy carries bars", T,
     '            omissions.append(Omission(ref=f"bars {did}", reason="raw market data is not exported in a shareable '
     'copy"))\n            continue\n',
     '            omissions.append(Omission(ref=f"bars {did}", reason="raw market data is not exported in a shareable '
     'copy"))\n'),
    ("osdev6", "zip entry times from the clock", T,
     "            info = zipfile.ZipInfo(path, date_time=_FIXED_TIME)",
     "            info = zipfile.ZipInfo(path, date_time=datetime.now().timetuple()[:6])"),
    ("osdev6", "memory in a Passport's export", T,
     "    if profile is ExportProfile.FULL_PRIVATE and selected is None:",
     "    if profile is ExportProfile.FULL_PRIVATE:"),
    # ---- review: one per fix
    ("review", "H1: a dataset without its bars is created empty", T,
     '        if did in b.bars:\n            plan.additions += [f"dataset/1 {did}", f"bars {did}"]\n'
     "            plan.datasets.append((m, b.bars[did]))",
     '        if True:\n            plan.additions += [f"dataset/1 {did}", f"bars {did}"]\n'
     "            plan.datasets.append((m, b.bars.get(did, [])))"),
    ("review", "H1: reproduce does not say what to re-import", S,
     "            if kept is None or kept.dataset_id != dataset_id:\n                raise",
     "            if True:\n                raise"),
    ("review", "H1: the bundle's recording of a dataset not kept", T,
     "    for m in plan.keep.values():\n        store.keep_manifest(m, db=db)",
     "    for m in ():\n        store.keep_manifest(m, db=db)"),
    ("review", "H2: bars not recomputed into the manifest's hash", T,
     "    if len(ordered) != m.row_count or sha256_bytes(canonical_bytes(ordered)) != m.normalized_sha256:",
     "    if len(ordered) != m.row_count:"),
    ("review", "H2: data import returns a planted dataset", D,
     "            if same_dataset(existing, manifest):\n                return existing",
     "            if True:\n                return existing"),
    ("review", "H3: an id twice in the bundle, with other content, accepted", T,
     '        _once(b.records, key, rec, have is None or record_hash(have) == record_hash(rec), b, f"{kind} '
     '{e.record_id}")',
     '        _once(b.records, key, rec, True, b, f"{kind} {e.record_id}")'),
    ("review", "H3: bars for an instrument the manifest does not list", T,
     "        if x.instrument_id not in known:\n            raise", "        if False:\n            raise"),
    ("review", "H3: a replay with nothing new writes a report", T,
     "            if not plan.ok or not plan.changes:", "            if not plan.ok:"),
    ("review", "H3: a refused apply creates the profile", T,
     "    if not plan.ok:                                        # refused before anything",
     "    if False:                                        # refused before anything"),
    ("review", "H4: runs not exported", T, "    if not share:\n        experiments =",
     "    if False:\n        experiments ="),
    ("review", "H4: the files runs reference not exported", T,
     '                    add(f"artifacts/{digest}.json", store.read_artifact(digest), "artifact", digest)',
     "                    pass"),
    ("review", "H4: runs not restored", T, "    for run in plan.runs:\n        db.execute(",
     "    for run in ():\n        db.execute("),
    ("review", "H4: a run that cannot record its results is left running", R, RUN_BODY, RUN_BODY_OLD),
    ("review", "M1: memory events not verified", T,
     "        ev = from_json(ResearchMemoryEvent, text)\n        verify_hash(ev)\n",
     "        ev = from_json(ResearchMemoryEvent, text)\n"),
    ("review", "M1: a record without its hash skips the check", T,
     "        if _has_hash_field(cls):", '        if getattr(rec, "canonical_hash", None):'),
    ("review", "M1: a memory event that differs counted as the same", T,
     "        same = have is not None and (have.canonical_hash == ev.canonical_hash or _same_fact(have, ev, here, b))",
     "        same = have is not None"),
    ("review", "M2: the manifest records the clock", T,
     "        omissions=tuple(omissions), rights_notes=tuple(rights), created_at=now))",
     "        omissions=tuple(omissions), rights_notes=tuple(rights), created_at=now or datetime.now(UTC)))"),
    ("review", "M3: a shareable copy keeps dataset names", T, 'name=f"dataset {n}"', "name=primary.name"),
    ("review", "M3: a shareable copy keeps rights labels", T, "primary.rights, label=NEUTRAL_RIGHTS, notes=",
     "primary.rights, notes="),
    ("review", "M3: a shared Passport keeps its data rights", T,
     "p, data_rights=tuple(NEUTRAL_RIGHTS for _ in p.data_rights),", "p, data_rights=p.data_rights,"),
    ("review", "M3: a shareable copy's rights notes name the label", T,
     '            rights.append(f"{did} (dataset {n}): {NEUTRAL_RIGHTS}")',
     '            rights.append(f"{did}: {primary.rights.label}")'),
    ("review", "M4: the archive's total ratio unchecked (the 1,023 x 1 MiB bomb)", T,
     "    if total > MAX_TOTAL_BYTES or (total > RATIO_FLOOR and total > MAX_RATIO * max(packed, 1)):",
     "    if total > MAX_TOTAL_BYTES:"),
    ("review", "M4: the total size cap", T, "    if total > MAX_TOTAL_BYTES or (total > RATIO_FLOOR",
     "    if False or (total > RATIO_FLOOR"),
    ("review", "M4: the budget not held while reading", T,
     "                if len(out) > info.file_size or left[0] < 0:", "                if False:"),
    ("review", "M5: a dataset recorded at another time is a conflict", T,
     "            if mine.canonical_hash != m.canonical_hash and not same_dataset(mine, m):",
     "            if mine.canonical_hash != m.canonical_hash:"),
    ("review", "M5: a reference holds only on an equal hash", S,
     "        if kept is not None and same_dataset(kept, local):\n            return local",
     "        if False:\n            return local"),
    ("review", "M5: the same memory fact recorded at another time is a conflict", T,
     "    return a is not None and c is not None and same_dataset(a, c)", "    return False"),
    ("review", "M5: other recordings of a dataset not exported", T,
     '        for k in others:\n            add(f"datasets/', '        for k in ():\n            add(f"datasets/'),
    ("review", "LOW: deeply nested JSON is an internal error", T,
     "OSError, ValueError, UnicodeDecodeError, RecursionError,", "OSError, ValueError, UnicodeDecodeError,"),
    ("review", "LOW: a bad CRC is an internal error", T, "    except zipfile.BadZipFile as e:",
     "    except zipfile.LargeZipFile as e:"),
    ("review", "LOW: another compression accepted", T,
     "        if info.compress_type not in COMPRESSIONS or info.flag_bits & 0x1:",
     "        if info.flag_bits & 0x1:"),
    ("review", "LOW: a preview creates the profile folder", C,
     "    store = Store(args.home)                     # a preview",
     "    store = Store(args.home).init()  # a preview"),
    ("review", "LOW: bars without their dataset dropped", T,
     '            b.failures.append(f"bars {did}: the bundle carries no manifest for this dataset; bars never travel '
     'alone")\n', ""),
    ("review", "LOW: a raw file no dataset names accepted", T,
     "        if not owners:\n            b.failures.append", "        if False:\n            b.failures.append"),
    ("review", "LOW: bars whose rights forbid export accepted", T,
     "        if not m.rights.export_raw_allowed:\n            b.failures.append(f\"bars {did}",
     "        if False:\n            b.failures.append(f\"bars {did}"),
    ("review", "LOW: data import takes a file under other rights", D,
     "        if other.raw_sha256 == manifest.raw_sha256 and other.rights != manifest.rights:",
     "        if False:"),
    ("review", "LOW: a bundle brings a file under other rights", T,
     "        if split:                                          # one file, one set of rights",
     "        if False:                                          # one file, one set of rights"),
    ("review", "LOW: an older profile's file under two rights leaks", T, "        elif forbids:",
     "        elif False:"),
    ("review", "LOW: memory restored in id order", T,
     "sorted(b.events.values(), key=lambda pe: pe[0])", "sorted(b.events.values(), key=lambda pe: pe[1].event_id)"),
    ("review", "LOW: memory exported unnumbered", T, 'add(f"memory/{n:08d}-{_slug(event_id)}.json"',
     'add(f"memory/{_slug(event_id)}.json"'),
    ("review", "LOW: a Passport's references unchecked", T,
     '    for passport in (r for (t, _), r in sorted(b.records.items()) if t == "passport/1"):',
     "    for passport in ():"),
    ("review", "LOW: a refused apply labelled a preview", T,
     '        head, verb = "Apply refused (nothing written)", "to add"',
     '        head, verb = "Preview (nothing written)", "to add"'),
]


def clean() -> None:
    for d in ROOT.rglob("__pycache__"):
        shutil.rmtree(d, ignore_errors=True)


def applies(rel: str, old: str) -> int:
    return (ROOT / rel).read_text(encoding="utf-8").count(old)


def run_one(python: str, rel: str, old: str, new: str) -> str:
    path = ROOT / rel
    src = path.read_text(encoding="utf-8")
    if src.count(old) != 1:
        return f"NOT APPLIED ({src.count(old)} matches)"
    env = dict(os.environ, PYTHONPATH="src", PYTHONDONTWRITEBYTECODE="1")
    try:
        path.write_text(src.replace(old, new), encoding="utf-8")
        clean()
        r = subprocess.run([python, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-q", "-x",
                            *TESTS], cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800)
        tail = (r.stdout.strip().splitlines() or ["?"])[-1]
        return ("KILLED   " if r.returncode else "SURVIVED ") + tail
    finally:
        path.write_text(src, encoding="utf-8")
        clean()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--only", default="")
    ap.add_argument("--check", action="store_true", help="only check that each mutant applies exactly once")
    a = ap.parse_args(argv)
    chosen = [m for m in MUTANTS if a.only in m[1]]
    if a.check:
        bad = [(name, applies(rel, old)) for _, name, rel, old, _ in chosen if applies(rel, old) != 1]
        for name, n in bad:
            print(f"NOT APPLIED ({n} matches): {name}")
        print(f"{len(chosen) - len(bad)} of {len(chosen)} apply")
        return 1 if bad else 0
    results = []
    for group, name, rel, old, new in chosen:
        verdict = run_one(a.python, rel, old, new)
        results.append((group, name, verdict))
        print(f"[{group}] {name}: {verdict}", flush=True)
    print()
    for group in ("osdev6", "review"):
        mine = [r for r in results if r[0] == group]
        if mine:
            killed = sum(1 for r in mine if r[2].startswith("KILLED"))
            print(f"{group}: {killed} of {len(mine)} killed")
    killed = sum(1 for r in results if r[2].startswith("KILLED"))
    print(f"all: {killed} of {len(results)} killed")
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
