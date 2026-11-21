# ADR-002: Canonical JSON for record hashes

- **Status:** proposed by OSDev4, 2026-11-20. Frozen in contracts v1 once reviewed.
- **Spec:** §05.4 asks for RFC 8785 or a tested alternative, documented and versioned.

## Decision
- We use a tested alternative, close to RFC 8785, in `traderos/contracts/canonical.py`:
  - keys are sorted, the output is compact UTF-8, and non-ASCII characters are written as themselves;
  - **Decimals are normalized strings and floats are refused outright**;
  - datetimes are UTC with a `Z`, and ints stay within the safe range.
- Hashes are SHA-256 as 64 lowercase hex characters. A record's hash leaves out its `canonical_hash` field.

## Why not RFC 8785 exactly
- RFC 8785 canonicalizes IEEE-754 numbers. Our financial records hold no floats at all (spec §04.3), so its number
  rules would never apply. A string form of Decimal is lossless and simpler to reproduce in any language.
- RFC 8785 sorts keys by UTF-16 code units, and we sort by code point. The two differ only for characters outside the
  Basic Multilingual Plane, which no key here uses.

## Guard
- `tests/fixtures/contracts/` holds golden bytes and hashes for every record type.
  `tests/contract/test_contracts_v1.py` fails on any drift.
- A deliberate change needs a new ADR and `tools/contracts/regen_golden.py`.
