"""Rewrite the golden contract fixtures from traderos.contracts.examples.

Run only for a deliberate, ADR-approved contract change; tests/contract/test_golden.py fails on any drift so that an
accidental change to serialization or hashing can never land quietly.

    python tools/contracts/regen_golden.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from traderos.contracts import record_hash, to_json  # noqa: E402
from traderos.contracts.examples import all_examples, variants  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "contracts"


def _write(folder: pathlib.Path, named: dict) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, record in sorted(named.items()):
        (folder / f"{name}.json").write_text(to_json(record) + "\n", encoding="utf-8")
        lines.append(f"{record_hash(record)}  {name}.json")
    (folder / "HASHES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def main() -> int:
    n = _write(OUT, {schema.replace("/", "_v"): record for schema, record in all_examples().items()})
    v = _write(OUT / "variants", variants())      # second shapes of a record after an additive change (ADR-010)
    print(f"wrote {n} golden records and {v} variants to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
