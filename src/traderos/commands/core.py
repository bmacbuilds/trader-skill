"""Core commands: init, doctor, capabilities (docs/cli-contract-v1.md)."""
from __future__ import annotations

import argparse
import pathlib
import platform
import sqlite3
import sys
from datetime import date

from traderos import __version__
from traderos.capabilities import USABLE, Capabilities
from traderos.cli import Output
from traderos.clock import XNYS
from traderos.contracts import CONTRACTS_VERSION, ErrorCode, TraderOSError
from traderos.store import SCHEMA_VERSION, Store

MIN_PYTHON = (3, 12)
# The typed code `doctor` refuses with, by the first check that failed (exit 3, docs/cli-contract-v1.md).
DOCTOR_CODES = {"python": ErrorCode.OUTSIDE_CAPABILITY, "sqlite": ErrorCode.OUTSIDE_CAPABILITY,
                "store": ErrorCode.MISSING_DATA, "calendar": ErrorCode.CALENDAR_UNKNOWN,
                "capabilities": ErrorCode.INTEGRITY_MISMATCH}


def store(args: argparse.Namespace) -> Store:
    return Store(args.home)


def register(sub) -> None:
    p = sub.add_parser("init", help="create the local store")
    p.set_defaults(run=init)
    p = sub.add_parser("doctor", help="check this installation")
    p.set_defaults(run=doctor)
    p = sub.add_parser("capabilities", help="what this build enables")
    p.add_argument("--all", action="store_true", help="include planned, deferred and held capabilities")
    p.set_defaults(run=capabilities)


def init(args: argparse.Namespace) -> Output:
    from traderos.data import tutorial
    s = store(args)
    existed = s.initialized()
    s.init()
    tut = tutorial.load(s)                       # the no-key tutorial setup (spec 13.2); idempotent
    text = (f"TraderOS store {'already set up' if existed else 'created'} at {s.home}\n"
            f"Tutorial dataset ready: {tut.dataset_id} (synthetic, labelled; never market evidence).\n"
            "Next: `traderos demo` runs the offline tutorial; `traderos data import <csv>` loads your own data.")
    return Output({"home": str(s.home), "created": not existed, "tutorial_dataset_id": tut.dataset_id}, text)


def _store_check(s: Store) -> tuple[bool, str, ErrorCode]:
    """(ok, detail, the code a FAIL refuses with). A damaged or newer store is a FAIL with its reason, never a crash."""
    if not s.db_path.exists():
        return False, f"no store at {s.home}; run `traderos init`", ErrorCode.MISSING_DATA
    try:
        with s.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        have = int(row[0]) if row else 0
    except (sqlite3.DatabaseError, ValueError) as e:
        return (False, f"{s.db_path} is not a TraderOS store ({e}); move it aside, then run `traderos init`",
                ErrorCode.INTEGRITY_MISMATCH)
    if have > SCHEMA_VERSION:
        return (False, f"the store at {s.home} was made by a newer TraderOS (schema {have}, this one reads "
                f"{SCHEMA_VERSION}); upgrade TraderOS", ErrorCode.SCHEMA_UNSUPPORTED)
    if have < SCHEMA_VERSION:
        return False, f"the store at {s.home} is not set up yet; run `traderos init`", ErrorCode.MISSING_DATA
    return True, f"{s.home} (schema current)", ErrorCode.MISSING_DATA


def doctor(args: argparse.Namespace) -> Output:
    checks: list[dict] = []
    codes: dict[str, ErrorCode] = dict(DOCTOR_CODES)

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    py = sys.version_info
    check("python", py[:2] >= MIN_PYTHON, f"{platform.python_implementation()} {platform.python_version()}"
          + ("" if py[:2] >= MIN_PYTHON else f"; TraderOS needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer"))
    check("traderos", True, f"{__version__}, contracts v{CONTRACTS_VERSION}")
    check("sqlite", sqlite3.sqlite_version_info >= (3, 35), f"SQLite {sqlite3.sqlite_version}")
    s = store(args)                              # #10's, #11's and #16's doctor lines read `s.home`: keep the name
    ok_store, detail, codes["store"] = _store_check(s)
    check("store", ok_store, detail)
    cal = XNYS()
    today = date.today()
    in_range = cal.coverage[0] <= today <= cal.coverage[1]
    check("calendar", in_range, f"XNYS {cal.version}, {cal.coverage[0]} to {cal.coverage[1]}"
          + ("" if in_range else "; today is outside coverage: upgrade TraderOS"))
    try:
        caps = Capabilities()
        usable = sum(1 for e in caps.entries() if e.user_status in USABLE)
        check("capabilities", True, f"registry for {caps.reg.release}: {usable} of {len(caps.entries())} usable")
    except Exception as e:  # doctor reports, it does not crash
        check("capabilities", False, f"registry unreadable: {e}")
    try:
        from traderos.adapters.sec import user_agent
        user_agent(s.home)
        sec_detail = "your SEC identification is set (used only by earnings, holdings and fundamentals screen)"
    except Exception:
        sec_detail = ("not set; needed only for earnings, holdings and fundamentals screen: "
                      "traderos config set sec_user_agent \"Your Name you@example.com\"")
    check("sec", True, sec_detail)
    try:
        home = pathlib.Path.home()
    except RuntimeError:                         # no home folder to look in: the project skill is still found
        home = None
    check("skill", True, skill_detail(pathlib.Path.cwd(), home))
    name, ok_paper, detail, codes["alpaca paper"] = _alpaca_paper(s.home)
    check(name, ok_paper, detail)
    check("network", True, "TraderOS made no network call; adapters run only when you ask for one")
    ok = all(c["ok"] for c in checks)
    lines = [f"{'ok  ' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}" for c in checks]
    if not ok:                                   # a FAIL exits 3, so a script or the skill cannot read it as ok
        failed = [c for c in checks if not c["ok"]]
        raise TraderOSError(f"doctor: {len(failed)} check(s) failed.\n" + "\n".join(lines),
                            code=codes.get(failed[0]["check"], ErrorCode.INTEGRITY_MISMATCH),
                            details={c["check"]: f"{'ok' if c['ok'] else 'FAIL'}: {c['detail']}" for c in checks})
    lines.append("All good.")
    return Output({"ok": ok, "checks": checks}, "\n".join(lines))


SKILL_FILE = pathlib.Path(".claude", "skills", "trader", "SKILL.md")


def _names_trader(path: pathlib.Path) -> bool:
    """The file opens with frontmatter whose `name` is `trader` (read from its first 4 KB only)."""
    try:
        with path.open("rb") as f:
            head = f.read(4096).decode("utf-8", errors="replace")
    except OSError:
        return False
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if line.split(":", 1)[0].strip() == "name":
            return line.split(":", 1)[1].strip().strip("\"'") == "trader"
    return False


def skill_detail(cwd: pathlib.Path, home: pathlib.Path | None) -> str:
    """Where the /trader Claude skill is (spec 13.2: doctor checks the skill files). Never a failure: the CLI works
    without Claude, and doctor cannot see a plugin Claude Code installed (`/traderos:trader`)."""
    # The home folder's .claude/skills is the user-level (personal) place, never a project, even when the session
    # starts inside the home folder.
    found = next(((d / SKILL_FILE, "project") for d in (cwd, *cwd.parents)
                  if d != home and (d / SKILL_FILE).is_file()), None)
    if found is None and home is not None and (home / SKILL_FILE).is_file():
        found = (home / SKILL_FILE, "personal")
    if found is None:
        return (f"no /trader skill in {cwd} or the folders above it, or in ~/.claude/skills: install it for every "
                "Claude Code session by copying .claude/skills/trader into ~/.claude/skills (the README's install "
                "does this). Doctor cannot see a plugin. The CLI works without it")
    path, kind = found
    if not _names_trader(path):
        return f"{kind} skill file at {path}, but its frontmatter does not name `trader`: replace it from the release"
    return f"{kind} skill /trader at {path}"
def _alpaca_paper(home) -> tuple[str, bool, str, ErrorCode]:
    """Whether the Alpaca PAPER keys are set (masked), and whether anything would make `ticket place` refuse, with the
    adapter's own refusal code for a FAIL (doctor exits 3 with it). No request is made."""
    import os

    from traderos import settings
    from traderos.adapters import alpaca_paper
    from traderos.contracts import TraderOSError
    try:
        alpaca_paper.check_environment()
    except TraderOSError as e:
        return "alpaca paper", False, e.message, e.code
    env_key = os.environ.get(alpaca_paper.KEY_ENV[0], "").strip()
    try:
        saved = settings.read(home) if home is not None else {}
    except TraderOSError as e:
        return "alpaca paper", False, e.message, e.code
    if env_key and os.environ.get(alpaca_paper.KEY_ENV[1], "").strip():
        key, where = env_key, "your environment"
    elif saved.get(alpaca_paper.KEY_SETTINGS[0]) and saved.get(alpaca_paper.KEY_SETTINGS[1]):
        key, where = str(saved[alpaca_paper.KEY_SETTINGS[0]]), "traderos config"
    else:
        return ("alpaca paper", True, "paper keys not set; needed only for `traderos ticket place`: traderos config "
                "set alpaca_paper_key_id ... and alpaca_paper_secret_key ... (live trading is refused in this build)",
            ErrorCode.MISSING_DATA)
    try:
        alpaca_paper.credentials(home)
    except TraderOSError as e:
        return "alpaca paper", False, e.message, e.code
    return ("alpaca paper", True, f"paper keys set from {where}: alpaca_paper_key_id "
            f"{settings.mask('alpaca_paper_key_id', key)}; orders go only to {alpaca_paper.PAPER_HOST}",
            ErrorCode.MISSING_DATA)


def capabilities(args: argparse.Namespace) -> Output:
    caps = Capabilities()
    rows = [e for e in caps.entries() if args.all or e.user_status in USABLE]
    lines = [f"TraderOS {caps.reg.release}: capabilities {'(all)' if args.all else 'you can use'}"]
    for e in rows:
        note = f"  [{e.user_status.value}]" + (f"  waiting for: {e.next_gate}" if e.next_gate else "")
        lines.append(f"  {e.capability_id.value:<6} {e.name:<22}{note}")
    if not rows:
        lines.append("  none yet in this build. `traderos capabilities --all` shows the roadmap and what each "
                     "waits for.")
    return Output(list(rows), "\n".join(lines))
