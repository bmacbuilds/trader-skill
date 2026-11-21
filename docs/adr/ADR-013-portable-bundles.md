# ADR-013: Portable bundles (I07): what a restore must hold, and when two profiles hold the same dataset

- **Status:** proposed by OSDev4, 2026-11-20, for PR #15, in answer to OSDev6's review ("changes needed at b6564a3").
  The decisions on the blocking items were OSDev4's own; none of this is an owner ruling.
- **Spec:** §10 I07, I01, I04; §11.2 (5), §11.3; §08.4 and QT02 (the holdout gate).
- **Change to `bundle/1`:** `PortableBundleManifest.created_at` is now optional (`null` unless the exporter is given a
  time). Additive: every existing manifest still reads and hashes the same, and the golden file does not move. A
  reader on contracts v1.0 would refuse a manifest whose `created_at` is null; nothing on v1.0 has shipped.
- **No other contract changes.** No store migration: the schema stays at version 1.

## Why
A backup that cannot be trusted to restore is worse than none. At b6564a3 a restore emptied every dataset whose bars
the bundle could not carry (H1), took planted bars under a genuine manifest (H2), half-wrote a bundle the preview had
passed (H3), and lost the runs that `passport reproduce` and the holdout gate read (H4).

## Decision
1. **A dataset is never created without its bars.** When a bundle carries a dataset's manifest but not its bars (the
   tutorial, rights-restricted data, every CSV imported through the CLI), apply imports the records that name it and
   does not create the dataset. The report lists it to re-import, with its name, row count and the file's SHA-256.
   Dataset ids are content-addressed, so a later `data import` of the same file creates it under the same id and the
   restored records work. Until then a Passport on it is refused `MISSING_DATA`, naming it.
2. **Bars are recomputed into their manifest's hashes** exactly as `data import` builds them (sorted by instrument and
   end, canonical bytes, SHA-256), with the row count. Any mismatch refuses the whole bundle. `data import` of a file
   whose dataset id is already here with other bars refuses and names the dataset, so a self-consistent forgery cannot
   shadow the genuine file either.
3. **Preview runs every check apply runs**, and apply runs the plan again inside one store transaction
   (`BEGIN IMMEDIATE`) and writes everything in it: the content-addressed files first (temp, fsync, rename), then
   every row. A refused apply writes nothing, not even the profile folder; applying a good bundle again writes
   nothing. The store's writers (`put_dataset`, `put_artifact`, `remember`) take the caller's transaction for this;
   without one they keep their own, as before. `data import` now writes its file, dataset and memory event in one
   transaction too.
4. **Runs travel.** A bundle carries the `runs` rows and the artifacts they reference, by hash, so reproduce and
   `Lab._holdout_compromised` answer the same on the restored profile as on the source.
5. **Integrity.** Every record that carries a hash is verified, memory events included; a missing hash is refused. A
   memory event that differs from a stored one is a conflict. A Passport comes in only with everything it names.
6. **Deterministic export.** Sorted entries, fixed entry times (1980-01-01), and no time in the manifest unless the
   caller passes `--now`. The same store exports the same bytes.
7. **A shareable copy names no one.** Dataset names and rights labels are free text a person typed (a file named after
   a person or an account). In `share_redacted` they read "dataset 1" and "rights: see original". The manifest and the
   Passports that carry them are redacted copies with their own hashes, chained to each other; the stored records are
   never touched. A shareable copy is for reading: importing one is refused, because its Passports stand on records
   it leaves out.
8. **Budgets.** The total decompressed size, and its ratio to the compressed size, are capped from the central
   directory before anything is read, and again while reading. Only stored and deflated members are read.

## What changed in what "the same dataset" means
Until now two manifests were the same dataset only when their canonical hashes were equal. A manifest also records
when it was imported (`recorded_at`), so the same file imported on two machines, or the tutorial made before and after
#15 fixed its time, gave two hashes, and a bundle from one conflicted on the other.

Now **two manifests are the same dataset when they have the same id, the same source file, the same bars (normalized
hash and row count) and the same rights** (`store.same_dataset`). When each was recorded is not part of it. The
tutorial is no exception. Concretely:
- **Import:** such a dataset is "already here", never a conflict. The local manifest stands. The other one is kept as a
  content-addressed file named by its canonical hash (`Store.keep_manifest`, media type
  `application/vnd.traderos.dataset-manifest+json`), so the records that name it keep working.
- **References:** where a Passport or an experiment names a dataset by id and canonical hash (`passports.reopen`,
  `Lab.run`), the reference holds when the hash is this profile's manifest, or a kept manifest of the same dataset
  (`Store.dataset_for`). Anything else is still `INTEGRITY_MISMATCH`.
- **Memory:** the same event recorded on another machine (the same who, what, subjects, outcome and words, about the
  same content) is "already here", whatever its time or place in the chain. An event that says something else under
  the same id is a conflict.
- **Export:** a profile exports its own manifest of each dataset and the kept ones beside it (`datasets/<hash>.json`),
  so a restore of a restore still resolves.
- **Rights:** one file has one set of rights in a profile. The same file under other rights, by `data import` or in a
  bundle, is refused and named (`DATA_RIGHTS_UNKNOWN` on import; a conflict in a bundle). Where an older profile
  already holds a file under two rights, its export carries neither copy's bars or raw file unless both allow it.

## Consequences
- A full backup restores: Passports reopen and reproduce, the holdout gate remembers, and what the bundle could not
  carry is named with how to restore it.
- Records that name a dataset now pass when the dataset is the same content recorded at another time. A dataset whose
  bars or rights differ is still refused.
- `tools/mutants/transfer.py` pins each guard (OSDev6's 11 survivors and one mutant per fix).

## Not in this release (cut ruling, 2026-11-20)
OSDev1's re-review of #15 at 9003703 (wall, 21:46) found H1 to H4 held, and two new blocking forgeries plus one HIGH:
a forged bundle that plants bars and then breaks `init` and `demo`; a re-hashed challenge that turns a failure into a
pass in `passport show`; and two demo profiles that cannot exchange a backup. His ruling: bundles do not ship in this
release, and the fixes go in the first update.

So `traderos export`, `traderos import` (preview and apply) and `api.export_portable_bundle` /
`api.import_portable_bundle` answer `CAPABILITY_DISABLED`, exit 3, "not in this release", before anything is read or
written (not even the profile folder). One switch, `traderos.transfer.RELEASED`, is `False`; the first update turns it
on with those fixes. The code stays built: `tests/unit/test_transfer.py` switches it on for its own tests, and
`tests/contract/test_bundles_gated.py` proves the shipped refusal. Bundles have no row in the capability registry (I07 is
a workflow item, not one of the 21 skills), so the refusal and docs/cli-contract-v1.md carry the "deferred" status.

