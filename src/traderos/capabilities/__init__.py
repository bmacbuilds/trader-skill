"""The release capability registry (spec 01.2). Read-only at runtime; compiled per release.

`capabilities.json` beside this file is the registry this build ships. Its canonical_hash is verified on load, so an
edited file is refused (INTEGRITY_MISMATCH) rather than activating anything. A capability is usable when its
user status is `verified`, or `experimental` (shown with an EXPERIMENTAL limitation, research scope only, spec
00.3). Anything else answers CAPABILITY_DISABLED with the gate it waits for.

Parts change their own entries in the same PR that lands the capability, and regenerate the hash with
`python tools/release/capabilities.py`.
"""
from __future__ import annotations

import functools
from importlib import resources

from traderos.contracts import (
    CapabilityDisabled,
    CapabilityEntry,
    CapabilityId,
    CapabilityRegistry,
    UserCapability,
    from_json,
    verify_hash,
)

USABLE = (UserCapability.VERIFIED, UserCapability.EXPERIMENTAL)


def load_registry(text: str) -> CapabilityRegistry:
    """Parse and verify a compiled registry. An edited file fails its hash: INTEGRITY_MISMATCH."""
    reg = from_json(CapabilityRegistry, text)
    verify_hash(reg)
    return reg


@functools.cache
def registry() -> CapabilityRegistry:
    return load_registry(resources.files(__package__).joinpath("capabilities.json").read_text(encoding="utf-8"))


class Capabilities:
    """Implements traderos.contracts.Capabilities over the shipped registry."""

    def __init__(self, reg: CapabilityRegistry | None = None) -> None:
        self.reg = reg or registry()
        self._by_id = {e.capability_id: e for e in self.reg.entries}

    def entry(self, capability_id: CapabilityId) -> CapabilityEntry:
        return self._by_id[CapabilityId(capability_id)]

    def require(self, capability_id: CapabilityId) -> CapabilityEntry:
        e = self.entry(capability_id)
        if e.user_status not in USABLE:
            raise CapabilityDisabled(f"{e.capability_id} {e.name} is {e.user_status.value} in this build"
                                     + (f"; waiting for: {e.next_gate}" if e.next_gate else ""),
                                     details={"capability": e.capability_id.value, "next_gate": e.next_gate})
        return e

    def entries(self) -> tuple[CapabilityEntry, ...]:
        return self.reg.entries
