# ADR-001: Contracts are standard-library dataclasses; the core has no runtime dependencies

- **Status:** proposed by OSDev4 (integration owner), 2026-11-20. Not yet independently reviewed.
- **Spec:** §04.3 leaves "Pydantic v2 or standard typed dataclasses" to an ADR. §03.5 requires a license review for
  every dependency. §01.1 requires a non-author to be able to install the product on a documented system.

## Decision
- Contracts are frozen, slotted, keyword-only dataclasses (`traderos.contracts.records.contract`).
- Validation is explicit: strict parsing in `from_dict` and invariants in `validate()`.
- The `traderos` core declares **no runtime dependencies**. Its development dependencies are pytest (MIT) and
  hypothesis (MPL-2.0, development only), recorded in `legal/dependency-ledger.csv`.

## Why
- **Install:** `pip install` of the wheel works offline once the wheel is local, on any CPython 3.12 or newer. No
  compiled dependency can fail on a customer's machine before the demo runs.
- **Licensing:** the shipped core has nothing to clear but its own code and the upstream MIT notices.
- **Exact numbers:** Decimal and timestamp rules are ours to freeze (ADR-002). A library's coercion defaults, such as
  float to Decimal or naive-datetime acceptance, cannot change them.

## Costs and what would change this
- We maintain about 200 lines of parsing and validation, covered by `tests/contract/`.
- If numerical work later needs NumPy or Pandas (spec §04.3), they enter through an ADR, a ledger row and a
  benchmark. They never enter the contracts, which stay dependency-free.
