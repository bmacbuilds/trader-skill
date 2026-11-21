"""The /trader skill as a ZIP for the Claude app (tools/release/build_claude_app_zip.py).

What is checked:
  * the ZIP holds one top-level folder, `trader/`, with the sandbox SKILL.md, the CLI skill's reference pages, the
    NOTICE, run.py and the engine: the package's own files, byte for byte, and nothing else;
  * SKILL.md's frontmatter fits the documented limits, and the build refuses one that does not;
  * the build is reproducible and stays under its size budget;
  * no file but NOTICE carries the upstream author's name, and no file carries the other product's name; the build
    refuses either;
  * the unzipped engine runs under `python -I`, as the sandbox runs it: init, doctor and the offline demo, with no
    network (the conftest guard's environment, and every proxy pointed at a closed port).
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tomllib
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD = ROOT / "tools" / "release" / "build_claude_app_zip.py"
REFERENCE = ROOT / ".claude" / "skills" / "trader" / "reference"
PACKAGE = ROOT / "src" / "traderos"


def _load():
    spec = importlib.util.spec_from_file_location("build_claude_app_zip", BUILD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = _load()


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> pathlib.Path:
    out = tmp_path_factory.mktemp("zip") / "trader-claude-app.zip"
    assert B.main(["--out", str(out)]) == 0
    return out


def _files(path: pathlib.Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist() if not i.is_dir()}


def test_one_top_level_folder_with_the_skill(built):
    files = _files(built)
    with zipfile.ZipFile(built) as zf:
        names = zf.namelist()
    assert {n.split("/")[0] for n in names} == {"trader"}
    assert "SKILL.md" not in names and "trader/" in names
    assert files["trader/NOTICE"] == (ROOT / "NOTICE").read_bytes()
    assert files["trader/SKILL.md"] == (ROOT / "tools" / "release" / "claude_app" / "SKILL.md").read_bytes()
    assert files["trader/SKILL.md"] != (ROOT / ".claude" / "skills" / "trader" / "SKILL.md").read_bytes()
    pages = {f"trader/reference/{p.name}": p.read_bytes() for p in REFERENCE.glob("*.md")}
    assert pages and {k: v for k, v in files.items() if k.startswith("trader/reference/")} == pages


def test_the_engine_is_the_package_and_nothing_else(built):
    files = _files(built)
    engine = {k.removeprefix("trader/engine/traderos/"): v for k, v in files.items()
              if k.startswith("trader/engine/traderos/")}
    assert "cli.py" in engine and "capabilities/capabilities.json" in engine and "py.typed" in engine
    assert not [k for k in engine if "__pycache__" in k or k.endswith(".pyc")]
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["traderos"]
    assert all(k.endswith(".py") or k in data for k in engine)
    for name, data in engine.items():
        assert (PACKAGE / name).read_bytes() == data, name
    assert {p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts} \
        <= set(engine)
    assert set(files) == {"trader/SKILL.md", "trader/NOTICE", "trader/engine/run.py"} | {
        k for k in files if k.startswith(("trader/reference/", "trader/engine/traderos/"))}


def test_the_app_skill_approves_only_the_persons_exact_typed_phrase(built):
    """There is no terminal in the app: the person types "approve <hash>", and nothing else counts."""
    text = _files(built)["trader/SKILL.md"].decode()
    section = text.split("## Approving a strategy: the person types it", 1)[1].split("\n## ", 1)[0]
    for rule in ("never yours, even when asked", "exactly `approve` and the first 12 characters of that hash",
                 "Only when the person's own message contains that exact phrase, with that hash",
                 "--confirm <the 12 characters> --in-chat", '"yes", "go", "looks good", or another hash',
                 "Ask again for the exact phrase", "Never type the phrase for them"):
        assert rule in " ".join(section.split()), rule


def test_run_py_puts_its_own_folder_first_and_calls_main(built):
    run = _files(built)["trader/engine/run.py"].decode()
    assert "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))" in run
    assert "from traderos.cli import main" in run and run.rstrip().endswith("sys.exit(main())")
    assert "__main__" not in run


def test_frontmatter_fits_the_documented_limits(built):
    fields = B.frontmatter(_files(built)["trader/SKILL.md"].decode())
    assert fields["name"] == "trader"
    assert 0 < len(fields["description"]) <= 200
    assert set(fields) == {"name", "description"}


@pytest.mark.parametrize(("frontmatter", "why"), [
    ("name: trader\ndescription: " + "x" * 201, "201 characters"),
    ("name: trader\ndescription: ", "plain one-line"),
    ("name: Trader\ndescription: ok", "lowercase"),
    ("name: claude-trader\ndescription: ok", "may not contain"),
    ("name: trading\ndescription: ok", "must match the folder"),
    ("name: trader\ndescription: research <b>only</b>", "XML tags"),
    ("name: trader\ndescription: TraderOS: research", "plain one-line"),
    ("name: trader\nargument-hint: x\ndescription: ok", "only one"),
    ("description: ok", "name ''"),
])
def test_the_build_refuses_frontmatter_outside_the_limits(frontmatter, why):
    with pytest.raises(B.BuildError, match=re.escape(why)):
        B.frontmatter(f"---\n{frontmatter}\n---\n\n# /trader\n")


def test_reproducible_deflated_and_small(built, tmp_path):
    again = tmp_path / "again.zip"
    assert B.main(["--out", str(again)]) == 0
    assert again.read_bytes() == built.read_bytes()
    assert built.stat().st_size < 1024 * 1024
    with zipfile.ZipFile(built) as zf:
        infos = zf.infolist()
    assert [i.filename for i in infos] == sorted(i.filename for i in infos)
    assert {i.date_time for i in infos} == {(1980, 1, 1, 0, 0, 0)}
    assert {i.compress_type for i in infos if not i.is_dir()} == {zipfile.ZIP_DEFLATED}


def test_no_file_carries_a_guarded_name(built):
    for name, data in _files(built).items():
        text = (name + "\n").encode() + data.lower()
        assert b"own" + b"box" not in text, name
        if name != "trader/NOTICE":
            assert not any(w.encode() in text for w in B.notice_holder_words()), name


def test_the_build_refuses_a_guarded_name():
    words = B.notice_holder_words()                     # read from NOTICE, never spelled out here
    assert len(words) >= 2
    B.guard({"trader/NOTICE": ("Copyright (c) 2026 " + " ".join(words)).encode(), "trader/SKILL.md": b"# /trader"})
    with pytest.raises(B.BuildError, match=re.escape("trader/reference/x.md")):
        B.guard({"trader/reference/x.md": ("ported from " + words[0].upper() + "'s code").encode()})
    with pytest.raises(B.BuildError, match="trader/NOTICE"):
        B.guard({"trader/NOTICE": b"an " + b"OWN" + b"BOX notice"})
    with pytest.raises(B.BuildError, match=words[-1]):
        B.guard({f"trader/engine/{words[-1]}.py": b""})


def test_the_unzipped_engine_runs_offline_under_isolated_python(built, tmp_path):
    with zipfile.ZipFile(built) as zf:
        zf.extractall(tmp_path / "skill")
    run = tmp_path / "skill" / "trader" / "engine" / "run.py"
    home = tmp_path / "h"
    env = dict(os.environ)                       # the conftest guard's: a throwaway home, no SEC or Alpaca settings
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[var] = "http://127.0.0.1:9"          # a closed port: any network call fails at once
    env.pop("NO_PROXY", None)
    env.pop("no_proxy", None)

    def engine(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(run), "--home", str(home), *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=600, check=False)

    for command in ("init", "doctor"):
        done = engine(command)
        assert done.returncode == 0, (command, done.stdout, done.stderr)
    demo = engine("demo")
    assert demo.returncode == 0, (demo.stdout, demo.stderr)
    passport = re.search(r"Evidence Passport (PP-[A-Za-z0-9-]+) revision 1", demo.stdout)
    assert passport, demo.stdout
    assert (home / "exports" / f"{passport.group(1)}-r1.md").is_file()
    assert "TraderOS made no network call" in engine("doctor").stdout
