# ADR-003: One private repository, with the upstream holding tank inside it

- **Status:** proposed by OSDev4, 2026-11-20.
- **Spec:**
  - §03.1 describes a separate private `traderos-study` holding tank.
  - §03.1 VERIFY says not to create a second repository just because the plan describes one.
- **Founder direction** (relayed by OSDev1, 2026-11-19): "copy everything and then rip out what we want and throw
  away what we don't", into the private repository, recording where each copied file came from.

## Decision
- `bmacbuilds/traderos-dev` is the one private repository.
- The upstream originals live under `tank/`, unmodified. Each file carries its repository, commit, path and SHA-256
  in `tank/PROVENANCE.csv`.
- The ten community pull requests are kept as patches, with their base and head commits.
- `tank/` is never executed, imported, linted or shipped:
  - pytest collects only `tests/`, and ruff excludes `tank/`;
  - the public release copies from an explicit allowlist that never names `tank/` (spec §03.1 export policy).
- Code reused from the tank is copied into `src/` in a reviewed PR. That PR names the tank file it came from
  (component id, repository, commit, path), keeps the MIT notice in `NOTICE` and has its own tests
  (spec §03.3).

## Why
- One place to review, one CI and no second repository to secure.
- The provenance file lets the founder and counsel review exactly what was copied and from where.
