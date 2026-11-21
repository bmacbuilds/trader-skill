"""Build the /trader skill as a ZIP that a person uploads to their own Claude account.

    python tools/release/build_claude_app_zip.py [--out dist/trader-claude-app.zip]

The person uploads it in the Claude app (Customize > Skills > + > Create skill > Upload a skill), and it then works
wherever they use Claude, a phone included. The skill runs in Claude's code-execution sandbox, so it carries its own
engine. The ZIP holds one top-level folder, `trader/`, because the app does not recognise a SKILL.md at a ZIP's root:

    trader/SKILL.md              the sandbox variant, tools/release/claude_app/SKILL.md (not the CLI skill's)
    trader/reference/*.md        the CLI skill's reference pages, unchanged
    trader/engine/traderos/...   the package: its .py files and the package data pyproject lists, nothing else
    trader/engine/run.py         puts its own folder on sys.path and runs traderos.cli.main
    trader/NOTICE                the repository's NOTICE

The ZIP is reproducible: fixed timestamps and permissions, sorted entries, deflate. Stdlib only. It refuses to build
(exit 1) when SKILL.md's frontmatter does not fit the documented limits, when the ZIP is over its size budget, or
when a file names what it may not (GUARDED).
"""
from __future__ import annotations

import argparse
import io
import pathlib
import re
import sys
import tomllib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SKILL_SOURCE = ROOT / "tools" / "release" / "claude_app" / "SKILL.md"
REFERENCE = ROOT / ".claude" / "skills" / "trader" / "reference"
PACKAGE = ROOT / "src" / "traderos"
NOTICE = ROOT / "NOTICE"
DEFAULT_OUT = ROOT / "dist" / "trader-claude-app.zip"

FOLDER = "trader"                  # the folder name must match the skill's `name` (support.claude.com)
NOTICE_ENTRY = f"{FOLDER}/NOTICE"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # the earliest time a ZIP can hold
FILE_MODE = 0o100644
DIR_MODE = 0o040755
LEVEL = 9

# The documented limits. Claude's help centre ("How to create custom Skills"): a name of at most 64 characters, a
# description of at most 200, and the folder named as the skill. The Agent Skills docs: lowercase letters, digits and
# hyphens; no XML tags; not the words "anthropic" or "claude"; a total upload under 30 MB uncompressed. The help
# centre gives no ZIP size, so the ZIP keeps to a budget far below that.
NAME_MAX = 64
DESCRIPTION_MAX = 200
NAME_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
RESERVED = ("anthropic", "claude")
UNCOMPRESSED_MAX = 30 * 1024 * 1024
ZIP_BUDGET = 1024 * 1024
ALLOWED_KEYS = ("name", "description")

def notice_holder_words() -> tuple[str, ...]:
    """The words of the copyright holder NOTICE names. They may appear in NOTICE and nowhere else, and they are read
    from NOTICE so that no other file, this one included, spells them out."""
    for line in (ROOT / "NOTICE").read_text(encoding="utf-8").splitlines():
        if line.startswith("Copyright (c) "):
            return tuple(w.lower() for w in line.split()[3:])
    raise RuntimeError("NOTICE has no copyright line")


# Words no file may carry, and the one entry each may appear in. "own" + "box" is spelled in parts so that this file
# does not carry the word either.
GUARDED: dict[str, frozenset[str]] = {
    **{word: frozenset({NOTICE_ENTRY}) for word in notice_holder_words()},
    "own" + "box": frozenset(),
}

RUN_PY = '''"""Run the TraderOS engine that ships in this skill: python3 run.py --home /tmp/traderos <command> ..."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from traderos.cli import main  # noqa: E402

sys.exit(main())
'''


class BuildError(Exception):
    """The ZIP was not built: the message says why."""


def frontmatter(text: str) -> dict[str, str]:
    """SKILL.md's frontmatter, checked against the documented limits. Each field is one plain `key: value` line, so the
    app's YAML reader cannot read it differently from this one."""
    lines = text.split("\n")
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise BuildError("SKILL.md must open with frontmatter between two `---` lines")
    fields: dict[str, str] = {}
    for line in lines[1:lines.index("---", 1)]:
        key, sep, value = line.partition(": ")
        if not sep or key not in ALLOWED_KEYS or key in fields:
            raise BuildError(f"frontmatter line {line!r}: only one `name: ...` and one `description: ...` line each")
        value = value.strip()
        if not value or value[0] in "-?:,[]{}#&*!|>'\"%@`" or ": " in value or " #" in value or value.endswith(":"):
            raise BuildError(f"frontmatter {key}: {value!r} is not a plain one-line YAML value")
        fields[key] = value
    name, description = fields.get("name", ""), fields.get("description", "")
    if not (0 < len(name) <= NAME_MAX and NAME_PATTERN.fullmatch(name)):
        raise BuildError(f"name {name!r}: 1 to {NAME_MAX} lowercase letters, digits and single hyphens")
    if any(word in name for word in RESERVED):
        raise BuildError(f"name {name!r}: may not contain {' or '.join(RESERVED)}")
    if name != FOLDER:
        raise BuildError(f"name {name!r} must match the folder name {FOLDER!r}")
    if not 0 < len(description) <= DESCRIPTION_MAX:
        raise BuildError(f"description: {len(description)} characters; the app takes 1 to {DESCRIPTION_MAX}")
    if re.search(r"<[^>]*>", description):
        raise BuildError("description: may not contain XML tags")
    return fields


def guard(entries: dict[str, bytes]) -> None:
    """Refuse when an entry, by its path or its content, carries a GUARDED word outside the entries allowed it."""
    found = [f"{path}: {word!r}" for path, data in sorted(entries.items()) for word, allowed in GUARDED.items()
             if path not in allowed and word.encode() in path.lower().encode() + b"\n" + data.lower()]
    if found:
        raise BuildError("these files may not carry these words:\n  " + "\n  ".join(found))


def package_files() -> list[pathlib.Path]:
    """The files a wheel of traderos carries: every .py file and the package data pyproject lists, no __pycache__."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    data = [PACKAGE / p for p in config["tool"]["setuptools"]["package-data"]["traderos"]]
    missing = [str(p.relative_to(ROOT)) for p in data if not p.is_file()]
    if missing:
        raise BuildError(f"package data pyproject lists is missing: {', '.join(missing)}")
    code = [p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(set(code) | set(data))


def entries() -> dict[str, bytes]:
    """Every file the ZIP holds, by its path in the ZIP."""
    skill = SKILL_SOURCE.read_bytes()
    frontmatter(skill.decode("utf-8"))
    files = {f"{FOLDER}/SKILL.md": skill, NOTICE_ENTRY: NOTICE.read_bytes(),
             f"{FOLDER}/engine/run.py": RUN_PY.encode("utf-8")}
    pages = sorted(REFERENCE.glob("*.md"))
    if not pages:
        raise BuildError(f"no reference pages in {REFERENCE.relative_to(ROOT)}")
    files.update({f"{FOLDER}/reference/{p.name}": p.read_bytes() for p in pages})
    files.update({f"{FOLDER}/engine/traderos/{p.relative_to(PACKAGE).as_posix()}": p.read_bytes()
                  for p in package_files()})
    guard(files)
    if sum(len(b) for b in files.values()) > UNCOMPRESSED_MAX:
        raise BuildError(f"the skill is over {UNCOMPRESSED_MAX // (1024 * 1024)} MB uncompressed")
    return files


def _info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
    info.create_system = 3                       # unix, so the permission bits read the same everywhere
    info.external_attr = mode << 16
    if name.endswith("/"):
        info.external_attr |= 0x10               # the MS-DOS directory flag
    else:
        info.compress_type = zipfile.ZIP_DEFLATED
    return info


def zip_bytes(files: dict[str, bytes]) -> bytes:
    """The ZIP, byte for byte the same for the same files: each folder, then each file, in sorted order."""
    folders = {f"{'/'.join(path.split('/')[:i])}/" for path in files for i in range(1, path.count("/") + 1)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in sorted(folders | set(files)):
            if name in folders:
                zf.writestr(_info(name, DIR_MODE), b"")
            else:
                zf.writestr(_info(name, FILE_MODE), files[name], compress_type=zipfile.ZIP_DEFLATED,
                            compresslevel=LEVEL)
    data = buf.getvalue()
    if len(data) > ZIP_BUDGET:
        raise BuildError(f"the ZIP is {len(data)} bytes, over its budget of {ZIP_BUDGET}")
    return data


def build(out: pathlib.Path) -> tuple[int, int]:
    """Write the ZIP to `out`. Returns its size in bytes and its number of entries."""
    data = zip_bytes(entries())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    with zipfile.ZipFile(out) as zf:
        return len(data), len(zf.infolist())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the /trader skill as a ZIP for the Claude app.")
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT,
                        help=f"default {DEFAULT_OUT.relative_to(ROOT)}")
    args = parser.parse_args(argv)
    try:
        size, count = build(args.out)
    except BuildError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"{args.out}: {size} bytes, {count} entries. Upload it in the Claude app: Customize > Skills > + > "
          "Create skill > Upload a skill.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
