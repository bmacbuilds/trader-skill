"""The /trader Claude skill (W06; spec 13.1-13.3): its files, and every command line in them against the real CLI.

Two distribution trees, one text (spec 13.1):
  .claude/skills/trader/                    the project skill: /trader in a Claude Code session started in the folder
  claude-plugin/skills/trader/              the plugin's copy: /traderos:trader once the plugin is installed
  claude-plugin/.claude-plugin/plugin.json  the plugin manifest

What is checked:
  * the two trees are byte-identical, the manifest parses and names the plugin, and SKILL.md has valid frontmatter;
  * every link in the skill resolves, and every reference file is linked from SKILL.md;
  * every `traderos ...` line inside a code block is real. If its command is registered on this branch,
    `traderos.cli.build_parser().parse_args(...)` must accept it, so no flag is invented. If it is not built yet,
    the command must be in the frozen `traderos.cli.COMMANDS` list, and the line is reported as SKIPPED (pending)
    with the reason, so `pytest -rs` lists what the skill names ahead of the code. The same holds for a `ticket`
    verb that the paper-orders PR adds (PLANNED_TICKET_VERBS);
  * every `--flag` named in the skill's prose (outside code blocks) is a flag the CLI has, or one #13 adds
    (PLANNED_PART_B);
  * the bundles page names no command: export and import are not in this release (OSDev1's cut ruling);
  * no forbidden promise, no live broker endpoint and no web address but the repository's, with or without a scheme;
  * `traderos doctor` reports where the skill is, or that it is missing, without failing (spec 13.2).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex

import pytest

from traderos import cli
from traderos.capabilities import Capabilities
from traderos.commands.core import skill_detail
from traderos.contracts import UserCapability

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROJECT = ROOT / ".claude" / "skills" / "trader"
PLUGIN = ROOT / "claude-plugin"
PLUGIN_SKILL = PLUGIN / "skills" / "trader"
# The upstream copyright line, read from NOTICE (the MIT notice) rather than repeated here.
UPSTREAM_COPYRIGHT = next(line for line in (ROOT / "NOTICE").read_text(encoding="utf-8").splitlines()
                          if line.startswith("Copyright (c) 2026 "))
MANIFEST = PLUGIN / ".claude-plugin" / "plugin.json"

# Claude Code's SKILL.md frontmatter fields (code.claude.com/docs/en/skills); this skill uses three of them.
FRONTMATTER_FIELDS = {"name", "description", "when_to_use", "argument-hint", "arguments", "disable-model-invocation",
                      "user-invocable", "allowed-tools", "disallowed-tools", "model", "effort", "context", "agent",
                      "background", "hooks", "paths", "shell", "metadata", "license", "compatibility"}
LISTING_LIMIT = 1536                             # description + when_to_use, as the skill listing shows them

# Handled by traderos.cli.main before the parser: `traderos help`, `traderos version`.
MAIN_ONLY = {"help", "version"}
GLOBAL_FLAGS = {"--json": 0, "--version": 0, "--home": 1}

# The paper-orders PR (ADR-012, in review) adds these `ticket` verbs. Until the CLI's own ticket help lists one, a
# skill line that uses it is pending; once it does, the line is parsed like any other. Remove a verb here when its
# PR lands (nothing else needs to change).
PLANNED_TICKET_VERBS = {"approve", "place", "result", "fill", "flatten", "account"}

# The upstream part-B port (OSDev6's #13, in review) adds these verbs and flags to commands #7 already built. Until the
# CLI's own parser knows one, a skill line that uses it is pending, and a flag named in prose is accepted; once it does,
# the line is parsed like any other. Remove an entry when #13 lands (nothing else needs to change; an empty dict is
# fine).
PLANNED_PART_B: dict[str, set[str]] = {}       # #13 is on main: every part-B verb and flag is real now

# Not in this release (OSDev1's cut ruling): the page says so and offers nothing, so it has no command line.
NOT_IN_THIS_RELEASE = {"bundles.md"}
# The one web address the skill may name: the public repository the install step names.
OWN_REPO = r"github\.com/bmacbuilds/trader-skill(?![\w./-])"
WEB_TLDS = r"(?:com|org|net|io|gov|edu|markets|ai|app|dev|co|us|uk|info|biz|me)"

FORBIDDEN = {
    # "guaranteed", "guarantees", "guarantee profits"; the noun in "a stop is a plan, not a guarantee" stays allowed
    "a guarantee": re.compile(r"\bguarantee[ds]\b|\bguarantee\s+(?:an?\s+|the\s+|your\s+)?(?:profit|return|gain|income|"
                              r"win|money)", re.IGNORECASE),
    "no risk": re.compile(r"\brisk[- ]free\b", re.IGNORECASE),
    "a promised result": re.compile(r"\bwill make\b", re.IGNORECASE),
    "live trading switched on": re.compile(
        r"\blive (?:trading|orders?|accounts?)\s+(?:is|are)\s+(?:now\s+)?(?:enabled|available|allowed|supported|on)\b"
        r"|\b(?:enable[sd]?|enabling|turn(?:s|ed)? on|switch(?:es|ed)? to|go(?:es|ing)?) live\b", re.IGNORECASE),
    "a live broker endpoint": re.compile(
        r"(?<!paper-)api\.alpaca\.markets|api\.ibkr\.com|interactivebrokers\.com|api\.tradier\.com"
        r"|api\.schwabapi\.com|api\.tdameritrade\.com|api\.etrade\.com|api\.robinhood\.com|api\.tastytrade\.com"
        r"|api\.tastyworks\.com|api\.webull\.com", re.IGNORECASE),
    # one address only: the public repository the install step names (marketplace.json carries the same one)
    "a web address (the skill names only its own repository)": re.compile(rf"https?://(?!{OWN_REPO})", re.IGNORECASE),
    # ... and none without a scheme either: `example.com/x`, `www.x.org`, `paper-api.alpaca.markets`. An e-mail
    # address's domain (`you@example.com`, the SEC identification's format) is not a web address.
    "a web address without a scheme": re.compile(
        rf"(?<![@\w./-])(?!{OWN_REPO})(?:[a-z0-9-]+\.)+{WEB_TLDS}(?![\w-])(?!\.\w)", re.IGNORECASE),
}
PROSE_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


# ---- reading the skill ---------------------------------------------------------------------------------------------

def _tree(root: pathlib.Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _docs() -> list[pathlib.Path]:
    return sorted(PROJECT.rglob("*.md"))


def _frontmatter(text: str) -> dict[str, str]:
    """The YAML frontmatter of a SKILL.md, for the plain and folded (`>-`) scalars a skill uses."""
    lines = text.split("\n")
    assert lines[0] == "---", "SKILL.md must open with --- on its first line, or Claude Code reads no frontmatter"
    end = lines.index("---", 1)
    fields: dict[str, str] = {}
    key = None
    for line in lines[1:end]:
        if line[:1] in (" ", "\t"):
            assert key is not None, f"an indented line before any key: {line!r}"
            fields[key] = (fields[key] + " " + line.strip()).strip()
            continue
        k, sep, v = line.partition(":")
        assert sep and k.strip() and k == k.strip(), f"not a `key: value` line: {line!r}"
        assert k not in fields, f"{k} is given twice"
        key, v = k, v.strip()
        fields[key] = "" if v in (">", ">-", "|", "|-") else v.strip("\"'")
    return fields


def _prose_flags() -> list[tuple[str, str]]:
    """(file:line, --flag) for every flag named outside a fenced code block."""
    out = []
    for path in _docs():
        rel = path.relative_to(PROJECT).as_posix()
        fenced = False
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if not fenced:
                out += [(f"{rel}:{n}", flag) for flag in PROSE_FLAG.findall(line)]
    return out


def _command_lines() -> list[tuple[str, str]]:
    """(file:line, command) for every `traderos` line inside a fenced code block of the skill."""
    out = []
    for path in _docs():
        rel = path.relative_to(PROJECT).as_posix()
        fenced, pending, start = False, "", 0
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("```"):
                fenced, pending = not fenced, ""
                continue
            if not fenced:
                continue
            text = line.strip()
            if not pending:
                start = n
            if text.endswith("\\"):
                pending += text[:-1] + " "
                continue
            text, pending = pending + text, ""
            if text == "traderos" or text.startswith("traderos "):
                out.append((f"{rel}:{start}", text))
    return out


LINES = _command_lines()


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


def _is_placeholder(sub: argparse.ArgumentParser) -> bool:
    """A frozen command no module has registered yet: cli.build_parser gives it a `_not_built` handler."""
    run = sub.get_default("run")
    return run is not None and run.__qualname__.startswith("_not_built.")


def _knows(sub: argparse.ArgumentParser, token: str) -> bool:
    """Whether a command's own parser takes this flag, or this verb at any level below it."""
    if token in sub._option_string_actions:
        return True
    return any(token in a.choices or any(_knows(p, token) for p in a.choices.values())
               for a in sub._actions if isinstance(a, argparse._SubParsersAction))


def _all_flags(parser: argparse.ArgumentParser) -> set[str]:
    """Every option string of a parser and of every parser below it."""
    flags = {s for a in parser._actions for s in a.option_strings}
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            for sub in a.choices.values():
                flags |= _all_flags(sub)
    return flags


def _ticket_verbs(sub: argparse.ArgumentParser) -> set[str]:
    """The verbs the ticket command's own help lists: "... from, or show | sign-off | decline | log"."""
    what = next(a for a in sub._actions if a.dest == "what")
    verbs = set(what.help.rsplit(" or ", 1)[-1].split(" | "))
    assert {"show", "log"} <= verbs, f"cannot read the ticket verbs from the CLI's help: {what.help!r}"
    return verbs


def _split(where: str, line: str) -> tuple[list[str], int]:
    tokens = shlex.split(line, comments=True)
    assert tokens[0] == "traderos"
    argv, i = tokens[1:], 0
    while i < len(argv) and argv[i] in GLOBAL_FLAGS:
        i += 1 + GLOBAL_FLAGS[argv[i]]
    assert i < len(argv), f"{where}: no command in {line!r}"
    late = [t for t in argv[i + 1:] if t in GLOBAL_FLAGS]
    assert not late, (f"{where}: {late} after the command; global flags go right after `traderos` "
                      f"(`traderos --json doctor`), or the CLI refuses the line: {line!r}")
    return argv, i


# ---- the two trees and the manifest ----------------------------------------------------------------------------

def test_the_project_skill_and_the_plugin_copy_are_byte_identical():
    project, plugin = _tree(PROJECT), _tree(PLUGIN_SKILL)
    assert "SKILL.md" in project and "reference/SOURCES.md" not in project      # docs/provenance/SOURCES.md
    only = sorted(set(project) ^ set(plugin))
    differ = sorted(k for k in set(project) & set(plugin) if project[k] != plugin[k])
    assert not only and not differ, (f"files in one tree only: {only}; files that differ: {differ}. Edit "
                                     ".claude/skills/trader, then copy it: rm -r claude-plugin/skills/trader && "
                                     "cp -r .claude/skills/trader claude-plugin/skills/trader")


def test_the_plugin_manifest_parses_and_names_the_skill():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == "traderos" and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", manifest["name"])
    assert manifest["version"] == "2.0.0"
    assert "/traderos:trader" in manifest["description"]
    # No public repository, homepage or license yet, and no upstream author: those fields stay out.
    assert not {"author", "homepage", "repository", "license"} & set(manifest)
    assert set(manifest) <= {"name", "displayName", "description", "version", "keywords"}
    assert all(isinstance(k, str) for k in manifest.get("keywords", []))
    # The default skills/ scan finds skills/trader; a "skills" path would only add a second scan.
    assert "skills" not in manifest
    assert _frontmatter((PLUGIN_SKILL / "SKILL.md").read_text(encoding="utf-8"))["name"] == "trader"
    files = sorted(_tree(PLUGIN))
    assert [f for f in files if not f.startswith("skills/trader/")] == [".claude-plugin/plugin.json"]


def test_the_marketplace_lists_the_plugin_so_github_installs_it():
    """`/plugin marketplace add bmacbuilds/trader-skill` reads .claude-plugin/marketplace.json at the root."""
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    (entry,) = market["plugins"]
    assert entry["name"] == manifest["name"] == market["name"] == "traderos"
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").resolve() == MANIFEST.resolve()
    assert entry["homepage"] == "https://github.com/bmacbuilds/trader-skill" and market["owner"]["name"]
    assert entry["description"] == manifest["description"]           # one description, never a second pitch
    text = json.dumps(market).lower()
    kept_out = "own" + "box"                       # built at run time, so this file never carries the name itself
    assert kept_out not in text and "api.alpaca.markets" not in text


def test_the_root_license_is_mit_and_upstream_notice_is_kept():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n\nCopyright (c) ") and "Permission is hereby granted" in text
    assert UPSTREAM_COPYRIGHT in (ROOT / "NOTICE").read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", [PROJECT / "SKILL.md", PLUGIN_SKILL / "SKILL.md"], ids=["project", "plugin"])
def test_every_skill_md_has_valid_frontmatter(skill):
    fields = _frontmatter(skill.read_text(encoding="utf-8"))
    assert set(fields) <= FRONTMATTER_FIELDS, f"Claude Code ignores unknown fields: {set(fields) - FRONTMATTER_FIELDS}"
    assert fields["name"] == "trader" and re.fullmatch(r"[a-z0-9-]{1,64}", fields["name"])
    description = fields["description"]
    assert description, "the description is how Claude knows when to use the skill"
    assert len(description) + len(fields.get("when_to_use", "")) <= LISTING_LIMIT
    assert "<" not in description and ">" not in description
    assert "/trader" in description and "Not investment advice" in description


def test_every_link_resolves_and_every_reference_file_is_linked():
    linked: set[pathlib.Path] = set()
    for doc in _docs():
        for target in re.findall(r"\]\(([^)\s]+)\)", doc.read_text(encoding="utf-8")):
            path = (doc.parent / target.split("#")[0]).resolve()
            assert path.is_file(), f"{doc.relative_to(PROJECT)} links to {target}, which is not in the skill"
            assert PROJECT in path.parents, f"{doc.relative_to(PROJECT)} links outside the skill: {target}"
            if doc.name == "SKILL.md":
                linked.add(path)
    unlinked = sorted(p.name for p in (PROJECT / "reference").glob("*.md") if p.resolve() not in linked)
    assert not unlinked, f"reference files SKILL.md never names, so Claude never loads them: {unlinked}"


# ---- every command line against the real CLI ------------------------------------------------------------------

def test_the_skill_names_commands_and_starts_every_session_with_the_registry_and_doctor():
    assert len(LINES) >= 50
    first = [line for where, line in LINES if where.startswith("SKILL.md:")][:2]
    assert first == ["traderos --json capabilities", "traderos --json doctor"]
    names = {pathlib.PurePosixPath(w.split(":")[0]).name for w, _ in LINES}
    every = {p.name for p in (PROJECT / "reference").glob("*.md")} - NOT_IN_THIS_RELEASE
    assert every <= names, f"reference files with no command line: {sorted(every - names)}"
    offered = sorted(names & NOT_IN_THIS_RELEASE)
    assert not offered, f"not in this release, so these pages must offer no command: {offered}"


def test_every_flag_named_in_prose_is_a_real_flag():
    flags = _all_flags(cli.build_parser()) | set(GLOBAL_FLAGS)
    pending = {t for tokens in PLANNED_PART_B.values() for t in tokens if t.startswith("--")}
    named = _prose_flags()
    assert len(named) >= 20
    unknown = [f"{where}: {flag}" for where, flag in named if flag not in flags | pending]
    assert not unknown, "flags the CLI does not have:\n" + "\n".join(unknown)


@pytest.mark.parametrize(("where", "line"), LINES, ids=[w for w, _ in LINES])
def test_every_command_line_is_real(where, line):
    argv, i = _split(where, line)
    command = argv[i]
    if command in MAIN_ONLY:
        assert argv[i + 1:] == [], f"{where}: `traderos {command}` takes no arguments"
        return
    parser = cli.build_parser()
    commands = _subparsers(parser)
    frozen = [c for c, _, _ in cli.COMMANDS]
    assert command in commands, f"{where}: `traderos {command}` is not a TraderOS command (cli.COMMANDS: {frozen})"
    if _is_placeholder(commands[command]):
        assert command in frozen
        pytest.skip(f"pending: `traderos {command}` is frozen in cli.COMMANDS but not built on this branch: {line}")
    if command == "ticket" and i + 1 < len(argv):
        what = argv[i + 1]
        verbs = _ticket_verbs(commands["ticket"])
        if re.fullmatch(r"[a-z][a-z-]*", what) and what not in verbs:
            assert what in PLANNED_TICKET_VERBS, f"{where}: `ticket {what}` is not a ticket verb ({sorted(verbs)})"
            pytest.skip(f"pending: `ticket {what}` comes with the paper-orders PR (ADR-012): {line}")
    planned = [t for t in argv[i + 1:] if t in PLANNED_PART_B.get(command, ()) and not _knows(commands[command], t)]
    if planned:
        pytest.skip(f"pending: {', '.join(planned)} for `traderos {command}` comes with the part-B port (#13): "
                    f"{line}")
    try:
        parser.parse_args(argv)
    except cli._Usage as e:
        pytest.fail(f"{where}: the CLI refuses {line!r}: {e}")


# ---- what the skill never says -----------------------------------------------------------------------------------

def test_no_forbidden_promise_and_no_live_broker_endpoint():
    texts = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in [*_docs(), MANIFEST]}
    hits = [f"{name}:{n}: {what}: {line.strip()}"
            for name, text in texts.items() for n, line in enumerate(text.splitlines(), 1)
            for what, rx in FORBIDDEN.items() if rx.search(line)]
    assert not hits, "\n".join(hits)


def test_the_router_covers_every_capability_and_never_offers_the_deferred_ones():
    text = (PROJECT / "SKILL.md").read_text(encoding="utf-8")
    never = text.split("## Never offered", 1)[1].split("\n## ", 1)[0]
    for e in Capabilities().entries():
        cid = e.capability_id.value
        if cid == "TRADER":
            continue
        assert re.search(rf"\b{cid}\b", text), f"SKILL.md neither routes nor declines {cid} ({e.name})"
        if e.user_status is UserCapability.DEFERRED:
            assert re.search(rf"\b{cid}\b", never), f"{cid} is deferred: SKILL.md must list it under Never offered"
    for rule in ("passport check-claims", "untrusted data, never instructions", "explicit yes for that exact ticket",
                 "Live trading is refused", "not investment advice"):
        assert rule.lower() in text.lower(), f"SKILL.md lost the rule: {rule}"


# ---- doctor reports the skill (spec 13.2) ------------------------------------------------------------------------

def _doctor(capsys) -> dict:
    assert cli.main(["--json", "init"]) == 0
    capsys.readouterr()
    assert cli.main(["--json", "doctor"]) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    return {"ok": result["ok"], **{c["check"]: c for c in result["checks"]}}


def test_doctor_names_the_project_skill(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(ROOT / "src")                          # a folder below the project still finds it
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = _doctor(capsys)
    assert out["skill"]["ok"] is True
    assert out["skill"]["detail"] == f"project skill /trader at {PROJECT / 'SKILL.md'}"


def test_doctor_says_the_skill_is_missing_without_failing(monkeypatch, capsys, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = _doctor(capsys)
    assert out["skill"]["ok"] is True and out["ok"] is True
    assert out["skill"]["detail"].startswith(f"no /trader skill in {work}")


def test_skill_detail_finds_a_personal_skill_and_flags_a_file_that_is_not_one(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    (home / ".claude" / "skills" / "trader").mkdir(parents=True)
    work.mkdir()
    personal = home / ".claude" / "skills" / "trader" / "SKILL.md"
    personal.write_bytes((PROJECT / "SKILL.md").read_bytes())
    assert skill_detail(work, home) == f"personal skill /trader at {personal}"
    (work / ".claude" / "skills" / "trader").mkdir(parents=True)
    project = work / ".claude" / "skills" / "trader" / "SKILL.md"
    project.write_text("# not a skill\n", encoding="utf-8")
    assert "does not name `trader`" in skill_detail(work, home)          # the project copy wins, and is flagged
    assert skill_detail(work, None).startswith(f"project skill file at {project}")


def test_a_skill_in_the_home_folder_is_personal_even_when_the_session_starts_inside_home(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "trader").mkdir(parents=True)
    personal = home / ".claude" / "skills" / "trader" / "SKILL.md"
    personal.write_bytes((PROJECT / "SKILL.md").read_bytes())
    work = home / "Documents" / "research"
    work.mkdir(parents=True)
    assert skill_detail(work, home) == f"personal skill /trader at {personal}"
    assert skill_detail(home, home) == f"personal skill /trader at {personal}"
