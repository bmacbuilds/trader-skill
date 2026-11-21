"""Write src/traderos/capabilities/capabilities.json from the table below, with its canonical hash.

Every roadmap ID is listed whether or not it ships (spec 12.1). A part flips its own row in the PR that lands the
capability; only a founder-approved release manifest may set `verified` (spec 01.2, 17.3).

    python tools/release/capabilities.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from traderos import __version__  # noqa: E402
from traderos.contracts import (  # noqa: E402
    CAPABILITY_NAMES,
    CapabilityEntry,
    CapabilityId,
    CapabilityImplementation,
    CapabilityRegistry,
    ExecutionMode,
    Limitation,
    UserCapability,
    to_json,
    with_hash,
)

P = CapabilityImplementation
U = UserCapability
M = ExecutionMode
EXPERIMENTAL = Limitation(code="EXPERIMENTAL", detail="Built and tested by its author; not independently verified "
                          "or release approved. Research use only.")

# Part B (OSDev6) landed tested and experimental; only independent review and a founder-approved release go further.
B_GATE = "independent review (W07), then a founder-approved release"

# id: (implementation, user status, modes, prerequisites, next gate)
TABLE = {
    CapabilityId.TRADER: (P.PLANNED, U.UNAVAILABLE, (), (),
                          "one full /trader session checked by someone other than its authors (W06); "
                          "the /trader skill and the 16 experimental capabilities work now"),
    CapabilityId.S01: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.RESEARCH), ("bars",), B_GATE),
    CapabilityId.S02: (P.DEFERRED, U.DEFERRED, (), ("news feeds",),
                       "a later update: the News Monitor (P2)"),
    CapabilityId.S03: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.RESEARCH), ("bars",), B_GATE),
    CapabilityId.S04: (P.TEST_PASSING, U.EXPERIMENTAL, (M.RESEARCH,), ("SEC EDGAR", "your own SEC identification"),
                       "a live end-to-end run against EDGAR by an independent tester; fixtures pass"),
    CapabilityId.S05: (P.DEFERRED, U.DEFERRED, (), (), "a later update: the Congress trade tracker (P2)"),
    CapabilityId.S06: (P.TEST_PASSING, U.EXPERIMENTAL, (M.RESEARCH,), ("SEC EDGAR", "your own SEC identification"),
                       "a live end-to-end run against EDGAR by an independent tester; fixtures pass"),
    CapabilityId.S07: (P.TEST_PASSING, U.EXPERIMENTAL, (M.RESEARCH,), ("SEC EDGAR", "your own SEC identification"),
                       "a live end-to-end run against EDGAR by an independent tester; fixtures pass"),
    CapabilityId.S08: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.RESEARCH), (),
                       "independent review; reads and checks Passports only, computes nothing new"),
    CapabilityId.S09: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.RESEARCH), (),
                       "independent review; JSON only (YAML deferred), long only, daily bars"),
    CapabilityId.S10: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.RESEARCH), ("bars",),
                       "independent QT03-QT08 review and ADR-005 sign-off"),
    CapabilityId.S11: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.RESEARCH), ("bars",),
                       "independent review of the resampling and stress methods"),
    CapabilityId.S12: (P.DEFERRED, U.DEFERRED, (), ("options data rights",), "licensed options data (O12)"),
    CapabilityId.S13: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.RESEARCH), ("bars",), B_GATE),
    CapabilityId.S14: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.HYPOTHETICAL), (), B_GATE),
    CapabilityId.S15: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.HYPOTHETICAL), (),
                       "independent review of the hand-worked fixtures"),
    CapabilityId.S16: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.HYPOTHETICAL), (),
                       "independent review of the hand-worked fixtures"),
    CapabilityId.S17: (P.TEST_PASSING, U.EXPERIMENTAL, (M.HYPOTHETICAL, M.PAPER), ("your own Alpaca paper keys",),
                       "independent review of the paper order path (ADR-012): paper accounts only, live refused in "
                       "code"),
    CapabilityId.S18: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.RESEARCH), ("bars",), B_GATE),
    CapabilityId.S19: (P.TEST_PASSING, U.EXPERIMENTAL, (M.TUTORIAL, M.HYPOTHETICAL), (),
                       "independent review of the hand-worked fixtures"),
    CapabilityId.S20: (P.DEFERRED, U.DEFERRED, (), (), "a later update (P2)"),
    CapabilityId.S21: (P.PLANNED, U.UNAVAILABLE, (), (), "a later update (P2)"),
}


def build() -> CapabilityRegistry:
    entries = []
    for cid in CapabilityId:
        impl, user, modes, prereq, gate = TABLE[cid]
        limits = (EXPERIMENTAL,) if user is U.EXPERIMENTAL else ()
        entries.append(CapabilityEntry(capability_id=cid, name=CAPABILITY_NAMES[cid], implementation=impl,
                                       user_status=user, implementation_version=__version__ if user in
                                       (U.EXPERIMENTAL, U.VERIFIED) else None, modes=modes, prerequisites=prereq,
                                       limitations=limits, next_gate=gate if user is not U.VERIFIED else ""))
    return with_hash(CapabilityRegistry(release=__version__, entries=tuple(entries)))


def main() -> int:
    out = ROOT / "src" / "traderos" / "capabilities" / "capabilities.json"
    out.write_text(to_json(build()) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
