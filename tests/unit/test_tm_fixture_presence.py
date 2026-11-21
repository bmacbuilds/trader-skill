"""The Yahoo TSLA fixture stays in traderos-dev, so the plan-replay tests that need it keep running here.

The public tree leaves it out (rights unverified, ADR-010) and those tests skip there. This test makes sure the skip
can never quietly happen in the private repository, which is the one tree that holds handoff/.
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TSLA = ROOT / "tests" / "fixtures" / "timemachine" / "TSLA-60d5m.json.gz"


def test_the_yahoo_fixture_is_present_in_the_private_repository():
    if not (ROOT / "handoff").is_dir():
        pytest.skip("public tree: the Yahoo TSLA fixture is left out (rights unverified, ADR-010)")
    assert TSLA.is_file(), "traderos-dev must keep the TSLA fixture, or the plan-replay tests skip unnoticed"
