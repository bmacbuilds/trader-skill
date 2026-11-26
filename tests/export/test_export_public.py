"""The public repository is produced from this one by tools/release/export_public.sh, and only from it.

These checks run here and never reach the public repository (the export leaves tests/export/ out).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "release" / "export_public.sh"
PRIVATE_PREFIXES = ("handoff/", "legal/", "tank/", "tools/tank/", "tests/tank/", "tests/export/", "storefront/")
PUBLIC_HOME = "github.com/bmacbuilds/trader-skill"
OLD_HOME = "trader" + "skill/"  # the organization the skill left on 2026-11-26, spelled so this file never names it


@pytest.fixture(scope="module")
def exported() -> tuple[Path, list[str]]:
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("the export reads git's tracked files")
    done = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, capture_output=True, text=True, check=True)
    return ROOT / "dist" / "public", done.stdout.split()


def test_only_public_files_are_exported(exported: tuple[Path, list[str]]) -> None:
    out, files = exported
    assert files, "the export printed no files"
    assert not [f for f in files if f.startswith(PRIVATE_PREFIXES)]
    assert "tools/release/export_public.sh" not in files
    assert "tests/fixtures/timemachine/TSLA-60d5m.json.gz" not in files
    assert sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()) == sorted(files)


def test_the_storefront_supplies_the_public_readme_install_and_chatgpt_edition(
    exported: tuple[Path, list[str]],
) -> None:
    out, files = exported
    for name in ("README.md", "install.sh", "chatgpt/README.md", "chatgpt/LICENSE"):
        assert (out / name).read_bytes() == (ROOT / "storefront" / name).read_bytes(), name
    assert os.access(out / "install.sh", os.X_OK)
    assert {"LICENSE", "NOTICE", "pyproject.toml", ".claude-plugin/marketplace.json"} <= set(files)


def test_every_link_names_the_new_home(exported: tuple[Path, list[str]]) -> None:
    out, files = exported
    named = [f for f in files if OLD_HOME in (out / f).read_bytes().decode("utf-8", "replace")]
    assert named == []
    for name in ("README.md", "install.sh", "chatgpt/README.md", ".claude-plugin/marketplace.json"):
        assert PUBLIC_HOME in (out / name).read_text(encoding="utf-8"), name
