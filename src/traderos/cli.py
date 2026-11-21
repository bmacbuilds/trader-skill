"""The `traderos` command. Spelling and exit codes are frozen in docs/cli-contract-v1.md.

Each part registers its own commands in its own module (listed in MODULES), so parts never edit one shared file.
A frozen command no module has registered yet answers CAPABILITY_DISABLED and names its owner.

Exit codes (frozen): 0 ok; 2 usage error; 3 typed refusal (a TraderOSError); 4 an unexpected internal error (a bug:
reported as one, never disguised as a result).
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from traderos import __version__
from traderos.contracts import CONTRACTS_VERSION, CapabilityDisabled, TraderOSError, to_dict
from traderos.contracts.records import is_record

# Command modules, in help order. Add yours here (one line) and register your commands in it.
MODULES = (
    "traderos.commands.core",
    "traderos.commands.data",
    "traderos.commands.intel",
    "traderos.commands.lab",
    "traderos.commands.transfer",
    "traderos.commands.demo",
    "traderos.commands.decisions",
    "traderos.commands.fundamentals",
    "traderos.commands.config",
)

COMMANDS = (
    # (command, what it does, owner): docs/cli-contract-v1.md, in the order of spec 13.2
    ("doctor", "check Python, skill files, version, local store and adapters", "core, OSDev4"),
    ("init", "create the local store with a no-key tutorial setup", "core, OSDev4"),
    ("demo", "offline tutorial: market, rules, test, challenge, Passport, reopen", "integration, OSDev4"),
    ("data", "import <csv> | fetch <symbol> (network) | check <dataset-id> | list", "data, OSDev4"),
    ("market", "Market Pulse on eligible data", "part B, OSDev6"),
    ("scan", "Chart Scanner on eligible data", "part B, OSDev6"),
    ("entry", "Entry Timer, research only", "part B, OSDev6"),
    ("exit", "Exit Planner, hypothetical only", "part B, OSDev6"),
    ("alerts", "Price Alerts: evaluate rules over stored data", "part B, OSDev6"),
    ("strategy", "draft | validate | approve | show", "lab, OSDev4"),
    ("research", "run <strategy-version-id>: preregister, run and challenge", "lab, OSDev4"),
    ("timemachine", "historical replay, resampling and stress", "lab, OSDev4"),
    ("passport", "show | reproduce | explain | check-claims", "lab, OSDev4"),     # share: once it is built
    ("memory", "search <query>: your local research memory", "lab, OSDev4"),
    ("risk", "assess: hypothetical sizing and risk; no broker", "part C, OSDev4"),
    ("ticket", "a paper ticket: your yes, then place sends it to your Alpaca PAPER account only", "part C, OSDev4"),
    ("journal", "add | list: your local append-only journal", "part C, OSDev4"),
    ("export", "portable bundles: not in this release (first update)", "lab, OSDev4"),
    ("import", "portable bundles: not in this release (first update)", "lab, OSDev4"),
    ("capabilities", "exactly what this build enables", "core, OSDev4"),
    ("earnings", "quarterly results from SEC filings (network; your SEC identification)", "part C, OSDev4"),
    ("holdings", "a manager's latest 13F, as reported (network)", "part C, OSDev4"),
    ("fundamentals", "screen: ratios from dated SEC filings (network)", "part C, OSDev4"),
    ("config", "set | show: your SEC identification and Alpaca paper keys (keys shown masked)", "core, OSDev4"),
)


@dataclass
class Output:
    """What a command returns: the result for --json, and the words for a person. `after` runs once the output is
    printed (an alert's state is saved only after its ring was shown, so a lost print never loses a ring)."""
    result: Any
    text: str
    after: Callable[[], None] | None = None


Handler = Callable[[argparse.Namespace], Output]


def _plain(value: Any) -> Any:
    if is_record(value):
        return to_dict(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # usage errors exit 2 with one line, not argparse's banner
        raise _Usage(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="traderos", add_help=False)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the result or refusal as JSON")
    parser.add_argument("--home", help="the TraderOS folder (default $TRADEROS_HOME or ~/.traderos)")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)
    for name in MODULES:
        importlib.import_module(name).register(sub)
    for command, what, owner in COMMANDS:
        if command not in sub.choices:
            p = sub.add_parser(command, help=what, add_help=False)
            p.add_argument("rest", nargs=argparse.REMAINDER)
            p.set_defaults(run=_not_built(command, owner))
    return parser


def _not_built(command: str, owner: str) -> Handler:
    def run(args: argparse.Namespace) -> Output:
        raise CapabilityDisabled(f"`traderos {command}` is not built yet ({owner})",
                                 details={"command": command, "owner": owner})
    return run


def help_text() -> str:
    lines = [f"traderos {__version__} (contracts v{CONTRACTS_VERSION})",
             "Research and hypothetical planning. TraderOS never places a live order: a paper ticket goes only to your",
             "Alpaca PAPER account, and only after your explicit yes for it.", "", "Commands:"]
    width = max(len(c) for c, _, _ in COMMANDS)
    lines += [f"  {c.ljust(width)}  {what}" for c, what, _ in COMMANDS]
    lines += ["", "Every command accepts --json. A refusal exits 3 with a typed code."]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    # Upstream PR #49 (taken as is, ADR-009): output as UTF-8 even where the console code page is not (Windows):
    # one character outside it would otherwise crash the whole run, and in-page characters would go out as lone
    # bytes. The guard keeps StringIO (the test suite's redirect_stdout) working, since it has no reconfigure.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(help_text())
        return 0
    if argv[0] in ("--version", "version"):
        print(f"traderos {__version__}")
        return 0
    try:
        args = build_parser().parse_args(argv)
    except _Usage as e:
        print(f"traderos: {e}. Run `traderos help`.", file=sys.stderr)
        return 2
    if args.version:
        print(f"traderos {__version__}")
        return 0
    if not getattr(args, "run", None):
        print(help_text())
        return 0
    try:
        out = args.run(args)
    except TraderOSError as err:
        return _refuse(err, as_json=as_json)
    except Exception as exc:  # a bug is reported as a bug, never as a result
        print(f"traderos: internal error ({type(exc).__name__}: {exc}). This is a bug.", file=sys.stderr)
        return 4
    if as_json:
        print(json.dumps({"ok": True, "result": _plain(out.result)}, sort_keys=True, ensure_ascii=False))
    else:
        print(out.text)
    if out.after is not None:
        try:
            out.after()
        except Exception as exc:  # the output stands; the bug is still reported as one
            print(f"traderos: internal error after the output ({type(exc).__name__}: {exc}). This is a bug.",
                  file=sys.stderr)
            return 4
    return 0


def _refuse(err: TraderOSError, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"ok": False, "failure": to_dict(err.to_failure())}, sort_keys=True, ensure_ascii=False))
    else:
        print(f"refused [{err.code.value}]: {err.message}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
