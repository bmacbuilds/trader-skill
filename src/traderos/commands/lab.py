"""The lab's commands: strategy, research, timemachine, passport, memory (docs/cli-contract-v1.md; spec 13.2)."""
from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
from datetime import UTC, datetime

from traderos.cli import Output
from traderos.contracts import (
    AuthorizationError,
    AuthorType,
    CapabilityDisabled,
    DataError,
    ErrorCode,
    IntegrityError,
    OwnerAction,
    StrategyDraft,
    StrategyVersion,
    ValidationError,
)
from traderos.store import Store, record_id
from traderos.strategies import rulebook


def register(sub) -> None:
    p = sub.add_parser("strategy", help="draft, validate, approve and show strategies")
    ss = p.add_subparsers(dest="strategy_command", required=True)
    x = ss.add_parser("validate", help="check a strategy/1 JSON file without saving anything")
    x.add_argument("path")
    x.set_defaults(run=strategy_validate)
    x = ss.add_parser("draft", help="save a validated draft (not yet approved)")
    x.add_argument("path")
    x.add_argument("--author", choices=["human", "ai"], default="human")
    x.set_defaults(run=strategy_draft)
    x = ss.add_parser("approve", help="approve a draft as an immutable strategy version")
    x.add_argument("draft_id")
    x.add_argument("--confirm", help="the first 12 characters of the rules hash you were shown")
    x.add_argument("--in-chat", action="store_true",
                   help="the person typed \"approve <hash>\" in the chat themselves (the Claude app): recorded as "
                        "approved in chat")
    x.set_defaults(run=strategy_approve)
    x = ss.add_parser("show", help="show a draft or version")
    x.add_argument("id")
    x.set_defaults(run=strategy_show)
    p = sub.add_parser("research", help="run experiments")
    rs = p.add_subparsers(dest="research_command", required=True)
    x = rs.add_parser("run", help="preregister and run an experiment with its challenges")
    x.add_argument("version_id")
    x.add_argument("--dataset", required=True)
    x.add_argument("--seed", type=int, default=7)
    x.add_argument("--cash", default="100000")
    x.set_defaults(run=research_run)
    p = sub.add_parser("timemachine", help="observed history, resampled paths and stress for a strategy, "
                                           "a planned intraday trade (`plan`) or the ORB study (`orb-study`)")
    p.add_argument("version_id", help="a strategy version id; or `plan` (ADR-010); or `orb-study` (ADR-010)")
    p.add_argument("--dataset", required=True)
    p.add_argument("--horizon", type=int)
    p.add_argument("--paths", type=int)
    p.add_argument("--seed", type=int, help="default 7 for a strategy, 19 for a plan or the study")
    g = p.add_argument_group("plan replay (`traderos timemachine plan`)")
    g.add_argument("--plan", metavar="FILE", help="the plan, trader-exit's plan.json shape")
    g.add_argument("--symbol", help="or give the plan as flags: ticker")
    g.add_argument("--side", choices=["buy", "sell"])
    g.add_argument("--date", help="the plan's day, YYYY-MM-DD (default: the as-of day, New York)")
    g.add_argument("--entry")
    g.add_argument("--stop")
    g.add_argument("--target")
    g.add_argument("--entry-time", help='the entry bar\'s close, e.g. "10:15 AM"')
    g.add_argument("--setup")
    g.add_argument("--or-minutes", type=int)
    g.add_argument("--stop-rule", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
    g.add_argument("--replays", type=int)
    g.add_argument("--cost-bps", type=float, help="costs in basis points a side, each leg (default 2)")
    g.add_argument("--chart", action="store_true", help="also draw the ASCII fan chart")
    g = p.add_argument_group("plan replay and ORB study")
    g.add_argument("--as-of", help='read the data as of this time ("2026-10-19 10:15" New York, or ISO with Z)')
    g.add_argument("--until", help="end each replay or trade at this New York time (plan default: its flat_by)")
    g = p.add_argument_group("ORB study (`traderos timemachine orb-study`)")
    g.add_argument("--symbols", nargs="+", help="these symbols only (default: every instrument in the dataset)")
    g.add_argument("--draws", type=int)
    p.set_defaults(run=timemachine_run)
    p = sub.add_parser("passport", help="the Evidence Passport")
    ps = p.add_subparsers(dest="passport_command", required=True, metavar="{show,reproduce,explain,check-claims}")
    x = ps.add_parser("show", help="reopen, verify and show a Passport")
    x.add_argument("passport_id")
    x.add_argument("--revision", type=int)
    x.add_argument("--out", help="also write the Markdown to this file")
    x.set_defaults(run=passport_show)
    x = ps.add_parser("reproduce", help="rerun the Passport's frozen experiment as a verification replay")
    x.add_argument("passport_id")
    x.set_defaults(run=passport_reproduce)
    x = ps.add_parser("explain", help="the Quant Analyst: a Passport's own numbers in words, each one cited")
    x.add_argument("passport_id")
    x.add_argument("--revision", type=int)
    x.set_defaults(run=passport_explain)
    x = ps.add_parser("check-claims", help="tie each number in a text to the Passport field it states; refuse any "
                                           "that disagrees with it or cannot be tied to one")
    x.add_argument("passport_id")
    x.add_argument("--text", required=True, help="a UTF-8 text file, at most 64 KB")
    x.add_argument("--revision", type=int)
    x.set_defaults(run=passport_check_claims)
    x = ps.add_parser("share")                   # frozen in the v1 contract, not in this release: a typed refusal
    x.add_argument("rest", nargs=argparse.REMAINDER)
    x.set_defaults(run=passport_share)
    p = sub.add_parser("memory", help="your local research memory")
    ms = p.add_subparsers(dest="memory_command", required=True)
    x = ms.add_parser("search", help="search what you have researched")
    x.add_argument("query", nargs="*")
    x.add_argument("--limit", type=int, default=20)
    x.set_defaults(run=memory_search)


def _store(args) -> Store:
    return Store(args.home).init()


def _read(path: str) -> bytes:
    p = pathlib.Path(path)
    if not p.is_file():
        raise DataError(f"{p} is not a file", code=ErrorCode.MISSING_DATA)
    return p.read_bytes()


def strategy_validate(args) -> Output:
    book = rulebook.compile_rules(_read(args.path))
    return Output({"valid": True, "semantic_hash": book.semantic_hash}, "Valid.\n" + rulebook.render(book))


# What a rerun may differ in: the time a draft was made, and the approval act (the first approval stands).
_WHEN = {StrategyDraft.SCHEMA: ("created_at",), StrategyVersion.SCHEMA: ("approved_by", "canonical_hash")}


def _replay(store: Store, record: StrategyDraft | StrategyVersion) -> tuple[StrategyDraft | StrategyVersion, bool]:
    """Store `record`, or return the one already stored under its id, and say which (True: already stored).

    Draft and version ids come from the rules hash, but a new draft or approval carries a new time, so the same
    command run again would put different bytes under an existing id. Records are never overwritten: the stored one
    is replayed instead. It is read through the store's hash check and its rules are hashed again, so a tampered
    record still refuses INTEGRITY_MISMATCH, as does the same id with other content (another author, say)."""
    stored = store.find(type(record), record_id(record))
    if stored is None:
        store.put(record)
        return record, False
    if (rulebook.compile_rules(stored.rules).semantic_hash != stored.semantic_hash
            or stored.semantic_hash != record.semantic_hash):
        raise IntegrityError(f"{record.SCHEMA} {record_id(record)} is stored with rules that do not match its hash")
    differ = [f.name for f in dataclasses.fields(record)
              if f.name not in _WHEN[record.SCHEMA] and getattr(stored, f.name) != getattr(record, f.name)]
    if differ:
        raise IntegrityError(f"{record.SCHEMA} {record_id(record)} already exists with a different "
                             f"{', '.join(differ)}; records are never overwritten")
    return stored, True


def strategy_draft(args) -> Output:
    d, again = _replay(_store(args), rulebook.draft(_read(args.path), author_type=AuthorType(args.author)))
    saved = (f"Draft {d.draft_id} was already saved on {d.created_at:%Y-%m-%d %H:%M} UTC (not approved); these are "
             "the same rules." if again else f"Saved draft {d.draft_id} (not approved).")
    return Output(d, f"{saved}\n{rulebook.render(d.rules)}\n"
                     f"To approve exactly these rules: traderos strategy approve {d.draft_id} "
                     f"--confirm {d.semantic_hash[:12]}")


def strategy_approve(args) -> Output:
    store = _store(args)
    d = store.get(StrategyDraft, args.draft_id)
    shown = d.semantic_hash[:12]
    confirm = args.confirm
    if confirm is None and sys.stdin.isatty():
        print(rulebook.render(d.rules))
        confirm = input(f"Type {shown} to approve exactly these rules: ").strip()
    if confirm != shown:
        raise AuthorizationError("not approved: confirm with the rules hash you were shown "
                                 f"(--confirm {shown})", code=ErrorCode.INVALID_APPROVAL)
    previous = None
    if d.parent_version_id:
        previous = store.get(StrategyVersion, d.parent_version_id)
    # In the Claude app there is no terminal: the person types "approve <hash>" in the chat, and the record says so.
    action = (OwnerAction(actor="local", method="skill_confirm", at=datetime.now(UTC),
                          statement=f"approve {d.name} rules {shown}, typed by the person in chat")
              if args.in_chat else
              OwnerAction(actor="local", method="cli_confirm", at=datetime.now(UTC),
                          statement=f"approve {d.name} rules {shown}"))
    v, again = _replay(store, rulebook.approve(d, action, previous=previous))
    done = (f"Already approved: {v.version_id} (revision {v.revision}) was approved on "
            f"{v.approved_by.at:%Y-%m-%d %H:%M} UTC; that approval stands." if again
            else f"Approved {v.version_id} (revision {v.revision}).")
    return Output(v, f"{done} Run it: traderos research run {v.version_id} --dataset <dataset-id>")


def strategy_show(args) -> Output:
    store = _store(args)
    rec = store.find(StrategyVersion, args.id) or store.find(StrategyDraft, args.id)
    if rec is None:
        raise DataError(f"no strategy draft or version {args.id!r}", code=ErrorCode.MISSING_DATA)
    status = "approved version" if isinstance(rec, StrategyVersion) else "draft (not approved)"
    return Output(rec, f"{args.id}: {status}\n{rulebook.render(rec.rules)}")


def research_run(args) -> Output:
    from traderos.research import Lab
    store = _store(args)
    version = store.get(StrategyVersion, args.version_id)
    lab = Lab(store)
    spec = lab.register(version, args.dataset, seed=args.seed, starting_cash=args.cash)
    out = lab.run(spec.experiment_id)
    p = out.passport
    lines = [f"Experiment {spec.experiment_id}: {out.verdict.value.replace('_', ' ')}"
             + (" (verification replay)" if out.verification_replay else "")]
    lines += [f"  not a candidate because: {r}" for r in out.reasons]
    lines += [f"  {c.challenge_id} {c.outcome.value:<26} {c.finding[:110]}" for c in out.challenges]
    lines.append(f"Passport {p.passport_id} revision {p.revision}: traderos passport show {p.passport_id}")
    return Output({"experiment": spec, "verdict": out.verdict.value, "reasons": out.reasons, "passport": p},
                  "\n".join(lines))


def passport_show(args) -> Output:
    from traderos import passports
    store = _store(args)
    p = passports.reopen(store, args.passport_id, args.revision)
    text = passports.render(store, p)
    if args.out:
        pathlib.Path(args.out).write_text(text, encoding="utf-8")
    return Output(p, text)


def passport_share(args) -> Output:
    raise CapabilityDisabled("`traderos passport share` is not in this release: a shared Passport is redacted and "
                             "opt-in (spec 18.3), and that is not built yet",
                             details={"command": "passport share", "owner": "lab, OSDev4"})


def passport_reproduce(args) -> Output:
    from traderos import passports
    from traderos.research import Lab
    store = _store(args)
    p = passports.reopen(store, args.passport_id)
    out = Lab(store).run(p.experiment_ids[0])
    same = not any(r.startswith("NONREPRODUCIBLE") for r in out.reasons)
    text = (f"Replayed {p.experiment_ids[0]}: {'reproduced exactly' if same else 'DID NOT reproduce'}; "
            f"Passport {out.passport.passport_id} revision {out.passport.revision} records the replay.")
    return Output({"reproduced": same, "passport": out.passport}, text)


def passport_explain(args) -> Output:
    from traderos import analyst
    from traderos.capabilities import Capabilities
    from traderos.contracts import CapabilityId
    Capabilities().require(CapabilityId.S08)
    result = analyst.explain(_store(args), args.passport_id, revision=args.revision)
    return Output(result, analyst.describe(result))


def passport_check_claims(args) -> Output:
    from traderos import analyst
    from traderos.capabilities import Capabilities
    from traderos.contracts import CapabilityId, DataError, ErrorCode, ValidationError
    Capabilities().require(CapabilityId.S08)
    path = pathlib.Path(args.text)
    if not path.is_file() or path.stat().st_size > analyst.MAX_TEXT:
        raise DataError(f"{path} is not a text file under {analyst.MAX_TEXT // 1024} KB", code=ErrorCode.MISSING_DATA)
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError(f"{path} is not UTF-8 text") from None
    result = analyst.check_claims(_store(args), args.passport_id, text, revision=args.revision)
    n = len(result["supported"])
    return Output(result, f"All {n} number(s) agree with {result['passport_id']} revision {result['revision']}, "
                  "each with the field it states:\n"
                  + "\n".join(f"  {s['number']}  <- {s['ref']}" for s in result["supported"]))


def memory_search(args) -> Output:
    events = _store(args).search_memory(" ".join(args.query), limit=args.limit)
    if not events:
        return Output([], "Nothing found in your research memory.")
    return Output(events, "\n".join(f"{e.at:%Y-%m-%d %H:%M}  {e.kind:<22} {e.summary}  [{e.event_id}]"
                                     for e in events))


_PLAN_FLAGS = ("plan", "symbol", "side", "date", "entry", "stop", "target", "entry_time", "setup", "or_minutes",
               "stop_rule", "replays", "cost_bps", "chart")
_PLAN_AS_FLAGS = ("symbol", "side", "date", "entry", "stop", "target", "entry_time", "setup", "or_minutes",
                  "stop_rule")            # the plan itself, as flags: never beside --plan FILE (it would be ignored)
_STUDY_FLAGS = ("symbols", "draws")
_SHARED_FLAGS = ("as_of", "until")
_STRATEGY_FLAGS = ("horizon", "paths")


def _only(args, mode: str, allowed: tuple[str, ...]) -> None:
    every = _PLAN_FLAGS + _STUDY_FLAGS + _SHARED_FLAGS + _STRATEGY_FLAGS
    wrong = [f for f in every if f not in allowed and getattr(args, f, None) not in (None, False)]
    if wrong:
        raise ValidationError(f"`traderos timemachine {mode}` does not take "
                              + ", ".join("--" + f.replace("_", "-") for f in wrong))


def _or(value, default):
    """A flag's value, or the default when the flag was not given (an explicit 0 is refused downstream, not
    replaced)."""
    return default if value is None else value


def timemachine_run(args) -> Output:
    if args.version_id == "plan":
        return timemachine_plan(args)
    if args.version_id == "orb-study":
        return timemachine_orb_study(args)
    from traderos import timemachine
    _only(args, "<strategy-version-id>", _STRATEGY_FLAGS)
    store = _store(args)
    report = timemachine.run(store, store.get(StrategyVersion, args.version_id), args.dataset,
                             horizon=_or(args.horizon, 252), paths=_or(args.paths, 1000),
                             seed=7 if args.seed is None else args.seed)
    return Output(report, timemachine.describe(report))


def timemachine_plan(args) -> Output:
    """The upstream plan replay on a stored 5-minute dataset (ADR-010). Hypothetical: nothing here is an order."""
    from traderos.timemachine import plan
    _only(args, "plan", _PLAN_FLAGS + _SHARED_FLAGS)
    now = plan.parse_now(args.as_of)
    as_flags = [f for f in _PLAN_AS_FLAGS if getattr(args, f, None) is not None]
    if args.plan and as_flags:
        raise ValidationError("give the plan as --plan FILE or as --symbol and its flags, not both (the file was "
                              "given with " + ", ".join("--" + f.replace("_", "-") for f in as_flags) + ")")
    if args.plan:
        raw, src = plan.read_plan(args.plan), args.plan
    elif args.symbol:
        raw = plan.plan_from_flags(args.symbol, side=args.side or "buy", date=args.date, entry=args.entry,
                                   stop=args.stop, target=args.target, entry_time=args.entry_time,
                                   setup=args.setup or "orb", or_minutes=_or(args.or_minutes, 15),
                                   stop_rule=args.stop_rule or "auto", now_utc=now)
        src = "flags"
    else:
        raise ValidationError("`traderos timemachine plan` needs --plan FILE, or --symbol --date --entry --stop "
                              "--target --entry-time", code=ErrorCode.MISSING_DATA)
    store = _store(args)
    record, doc = plan.replay(store, raw, args.dataset, now_utc=now, replays=_or(args.replays, 10000),
                              seed=19 if args.seed is None else args.seed, until=args.until,
                              cost_bps=plan.DEFAULT_COST_BPS if args.cost_bps is None else args.cost_bps, src=src)
    lines = plan.report(doc)
    if args.chart:
        lines += ["", *plan.render_chart(doc)]
    lines += [f"Recorded {record.report_id} (timemachine/1). Hypothetical research only: nothing here is an order."]
    return Output(record, "\n".join(lines))


def timemachine_orb_study(args) -> Output:
    """The upstream cross-section ORB study (upstream #53's backtest.py) on a stored dataset (ADR-010)."""
    from traderos.timemachine import orb_study, plan
    _only(args, "orb-study", _STUDY_FLAGS + _SHARED_FLAGS)
    store = _store(args)
    body, doc = orb_study.study(store, args.dataset, now_utc=plan.parse_now(args.as_of),
                               until=args.until or "15:55", seed=19 if args.seed is None else args.seed,
                               draws=orb_study.DRAWS if args.draws is None else args.draws, symbols=args.symbols)
    return Output(body, "\n".join(orb_study.describe(doc)))
