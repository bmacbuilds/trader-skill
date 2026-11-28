"""The public installer (storefront/install.sh), run for real in a throwaway home with stand-ins for uv and curl.

What is checked:
  * the release it downloads (TAG) is this package's version, and the README names the same release;
  * it runs the traderos command from the folder uv reports (`uv tool dir --bin`), not an assumed ~/.local/bin;
  * a skill already called trader that is not TraderOS is kept, unchanged, outside the skills folder, and the
    installer says where; an earlier TraderOS install is replaced, and nothing is kept for it.
The stand-ins never reach the network: curl serves a tarball of this repository's own skill folder.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tarfile

import pytest

import traderos

ROOT = pathlib.Path(__file__).resolve().parents[2]
INSTALL = ROOT / "storefront" / "install.sh"
README = ROOT / "storefront" / "README.md"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="the installer is a bash script")


def test_the_installer_downloads_this_package_s_release_and_the_readme_names_it():
    tag = re.search(r"^TAG=v(\S+)$", INSTALL.read_text(encoding="utf-8"), re.M)
    assert tag and tag.group(1) == traderos.__version__
    assert '"https://github.com/bmacbuilds/trader-skill/archive/refs/tags/$TAG.tar.gz"' in INSTALL.read_text("utf-8")
    named = re.findall(r"current release \(v([0-9.]+)\)", README.read_text(encoding="utf-8"))
    assert named == [traderos.__version__]


def _sandbox(tmp_path: pathlib.Path) -> tuple[dict, pathlib.Path, pathlib.Path]:
    home, fake, tools = tmp_path / "home", tmp_path / "fake-bin", tmp_path / "uv-tools-bin"
    for d in (home, fake, tools):
        d.mkdir()
    tarball = tmp_path / "release.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(ROOT / ".claude" / "skills" / "trader", arcname="trader-skill/.claude/skills/trader")
    stub = tmp_path / "traderos-stub"
    stub.write_text(f'#!/bin/bash\necho "$1" >> "{tmp_path}/traderos.log"\n', encoding="utf-8")
    (fake / "curl").write_text(f'#!/bin/bash\ncat "{tarball}"\n', encoding="utf-8")
    (fake / "uv").write_text(
        "#!/bin/bash\n"
        f'if [ "$1 $2 $3" = "tool dir --bin" ]; then echo "{tools}"; exit 0; fi\n'
        f'if [ "$1 $2" = "tool install" ]; then cp "{stub}" "{tools}/traderos"; exit 0; fi\n'
        "exit 3\n", encoding="utf-8")
    for f in (*fake.iterdir(), stub):
        f.chmod(0o755)
    env = {"HOME": str(home), "PATH": f"{fake}:/usr/bin:/bin", "LC_ALL": "C"}
    return env, home, tools


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(INSTALL)], env=env, capture_output=True, text=True, timeout=60)


def test_it_runs_traderos_from_uv_s_own_folder(tmp_path):
    env, home, tools = _sandbox(tmp_path)
    done = _run(env)
    assert done.returncode == 0, done.stderr
    assert (tools / "traderos").exists()
    assert not (home / ".local" / "bin" / "traderos").exists()
    assert (tmp_path / "traderos.log").read_text().split() == ["init", "doctor"]
    assert (home / ".claude" / "skills" / "trader" / "SKILL.md").read_bytes() == \
        (ROOT / ".claude" / "skills" / "trader" / "SKILL.md").read_bytes()


def test_a_skill_called_trader_that_is_someone_else_s_is_kept_and_named(tmp_path):
    env, home, _ = _sandbox(tmp_path)
    theirs = home / ".claude" / "skills" / "trader"
    theirs.mkdir(parents=True)
    (theirs / "SKILL.md").write_text("---\nname: trader\ndescription: my own notes on trades\n---\n", encoding="utf-8")
    (theirs / "notes.md").write_text("keep me\n", encoding="utf-8")
    done = _run(env)
    assert done.returncode == 0, done.stderr
    kept = list((home / ".traderos-app").glob("kept-skill-*"))
    assert len(kept) == 1
    assert (kept[0] / "notes.md").read_text() == "keep me\n"
    assert str(kept[0]) in done.stdout
    assert "TraderOS" in (theirs / "SKILL.md").read_text(encoding="utf-8")
    assert not any(p.name.startswith("trader") and p.name != "trader" for p in (home / ".claude" / "skills").iterdir())


def test_an_earlier_traderos_install_is_replaced_and_nothing_is_kept_for_it(tmp_path):
    env, home, _ = _sandbox(tmp_path)
    assert _run(env).returncode == 0
    (home / ".claude" / "skills" / "trader" / "old.md").write_text("from the last install\n", encoding="utf-8")
    again = _run(env)
    assert again.returncode == 0, again.stderr
    assert not (home / ".claude" / "skills" / "trader" / "old.md").exists()
    assert not list((home / ".traderos-app").glob("kept-skill-*"))
    assert "kept" not in again.stdout
