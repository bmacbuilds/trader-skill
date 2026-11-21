"""The shipped capability registry (spec 01.2, 12.1)."""
from __future__ import annotations

import dataclasses
import json

import pytest

from traderos.capabilities import Capabilities, load_registry, registry
from traderos.contracts import (
    CapabilityDisabled,
    CapabilityId,
    ErrorCode,
    IntegrityError,
    UserCapability,
    to_json,
    verify_hash,
)


def test_every_roadmap_id_is_listed_once():
    ids = [e.capability_id for e in registry().entries]
    assert sorted(ids) == sorted(CapabilityId) and len(ids) == len(set(ids))


def test_registry_hash_verifies_and_an_edit_is_refused():
    reg = registry()
    verify_hash(reg)
    data = json.loads(to_json(reg))
    data["entries"][1]["user_status"] = "verified"            # someone flips S01 on by hand
    with pytest.raises(IntegrityError):
        load_registry(json.dumps(data))
    assert load_registry(to_json(reg)) == reg


def test_held_capabilities_stay_held():
    caps = Capabilities()
    for cid in (CapabilityId.S05, CapabilityId.S20, CapabilityId.S12, CapabilityId.S02):
        assert caps.entry(cid).user_status is UserCapability.DEFERRED


def test_require_refuses_what_is_not_usable_and_names_the_gate():
    caps = Capabilities()
    with pytest.raises(CapabilityDisabled) as e:
        caps.require(CapabilityId.S05)
    assert e.value.code is ErrorCode.CAPABILITY_DISABLED and "later update" in e.value.details["next_gate"]


def test_nothing_is_verified_without_a_release_manifest():
    assert all(e.user_status is not UserCapability.VERIFIED for e in registry().entries)


def test_experimental_entries_say_so():
    for e in registry().entries:
        if e.user_status is UserCapability.EXPERIMENTAL:
            assert any(lim.code == "EXPERIMENTAL" for lim in e.limitations)
    caps = Capabilities(dataclasses.replace(registry()))
    assert caps.entries() == registry().entries
