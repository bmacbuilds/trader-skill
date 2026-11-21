"""Portable bundles are not in this release (OSDev1's cut ruling, wall 2026-11-20 21:46; ADR-013).

`traderos export`, `traderos import` (preview and apply) and their API functions answer CAPABILITY_DISABLED, exit 3,
saying they come in the first update, before anything is read or written. The code stays built and tested
(tests/unit/test_transfer.py switches it on).
"""
from __future__ import annotations

import json

import pytest

from traderos import api, cli, transfer
from traderos.contracts import CapabilityDisabled, ErrorCode


def test_bundles_are_switched_off_in_this_release():
    assert transfer.RELEASED is False


@pytest.mark.parametrize("argv", [["export"], ["export", "PP-x", "--profile", "share_redacted"],
                                  ["import", "b.traderos"], ["import", "b.traderos", "--dry-run"],
                                  ["import", "b.traderos", "--apply"]])
def test_every_bundle_command_refuses_and_writes_nothing(argv, tmp_path, capsys):
    home = tmp_path / "home"
    assert cli.main(["--json", "--home", str(home), *argv]) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == ErrorCode.CAPABILITY_DISABLED.value
    assert "not in this release" in failure["message"] and failure["details"]["status"] == "deferred"
    assert not home.exists()                     # refused before the profile folder is made


def test_the_api_refuses_too(tmp_path):
    with pytest.raises(CapabilityDisabled):
        api.export_portable_bundle((), store=None)
    with pytest.raises(CapabilityDisabled):
        api.import_portable_bundle(str(tmp_path / "b.traderos"), mode="apply")


def test_help_says_not_in_this_release(capsys):
    for command in ("export", "import"):
        with pytest.raises(SystemExit) as done:
            cli.main([command, "-h"])
        assert done.value.code == 0 and "not in this release" in capsys.readouterr().out
    assert cli.main(["help"]) == 0
    listing = [line for line in capsys.readouterr().out.splitlines() if line.lstrip().startswith(("export", "import"))]
    assert len(listing) == 2 and all("not in this release" in line for line in listing)
