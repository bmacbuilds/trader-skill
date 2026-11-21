"""`traderos market | scan | entry | exit | alerts check` (docs/cli-contract-v1.md, part B).

Each command first asks the release registry whether its capability is usable in this build (spec 01.2); if not, it
refuses with CAPABILITY_DISABLED and the gate it waits for. Then it reads the local store as of the instant given (or
the newest available data) and prints the typed record. Nothing here can place an order: plans are hypothetical and
entries are research only, and each says so.

`--dataset` may be left out when the store holds exactly one dataset (after `traderos init`, the tutorial).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from datetime import UTC, datetime
from decimal import Decimal

from traderos import api
from traderos.capabilities import Capabilities
from traderos.cli import Output
from traderos.contracts import (
    CapabilityId,
    DataError,
    ErrorCode,
    ExitPlan,
    ScanCriteria,
    ValidationError,
    from_dict,
    from_json,
    parse_decimal,
    parse_utc,
)
from traderos.contracts.canonical import decimal_str
from traderos.contracts.intel import AlertEvent, AlertRule, Eligibility
from traderos.store import Store

MAX_FILE = 1024 * 1024                           # a criteria or rules file is small: bound it (spec 04.2)
MAX_RULES = 200


def register(sub) -> None:
    m = sub.add_parser("market", help="Market Pulse on eligible data")
    _common(m)
    m.set_defaults(run=market)

    s = sub.add_parser("scan", help="Chart Scanner, with per-symbol reasons")
    what = s.add_mutually_exclusive_group(required=True)
    what.add_argument("--criteria", help="a scan-criteria/1 JSON file")
    what.add_argument("--setups", choices=["daily", "orb"],
                      help="the classic setups: daily (on daily bars) or orb (opening-range breakout, 5-minute bars)")
    s.add_argument("--symbols", help="comma-separated; default every instrument in the dataset (--setups)")
    s.add_argument("--min-price", dest="min_price", help="--setups: floor on the last close (daily default 5)")
    s.add_argument("--min-volume", dest="min_volume", help="--setups: floor on volume (daily default 1000000)")
    s.add_argument("--or-minutes", dest="or_minutes", type=int, default=15,
                   help="--setups orb: the opening range, a multiple of 5 from 5 to 120")
    s.add_argument("--until", default="15:30",
                   help="--setups orb: the last New York time for new entries (default 15:30); later is expired")
    _common(s)
    s.set_defaults(run=scan)

    e = sub.add_parser("entry", help="Entry Timer: is a setup session-eligible now, or has it triggered? Research only")
    e.add_argument("symbol")
    e.add_argument("--setup", choices=["orb", "level"],
                   help="the entry trigger on 1- or 5-minute bars: orb (a close above the opening-range high) or level")
    e.add_argument("--level", help="--setup level: the price a bar must close above (for example a 20-day high)")
    e.add_argument("--or-minutes", dest="or_minutes", type=int, default=15,
                   help="--setup orb: the opening range in minutes (default 15)")
    e.add_argument("--min-volume-x", dest="min_volume_x",
                   help="--setup: the trigger bar needs this multiple of the range's average bar volume")
    _common(e)
    e.set_defaults(run=entry)

    x = sub.add_parser("exit", help="Exit Planner: hypothetical exit scenarios; no order is placed")
    x.add_argument("symbol")
    x.add_argument("--direction", required=True, choices=["long", "short"])
    x.add_argument("--entry", required=True, help="the hypothetical entry price")
    x.add_argument("--stop", help="a price, or a stop rule for a long: or-mid, or-low, price:X, pct:X, atr:K")
    x.add_argument("--target", help="a price, or a target rule for a long: 2R, 1R, 1.5R ..., price:X")
    x.add_argument("--setup", choices=["orb", "level"], default="orb",
                   help="with a rule: an orb entry defaults to an or-mid stop; a level entry needs --stop")
    x.add_argument("--or-minutes", dest="or_minutes", type=int, default=15,
                   help="with or-mid/or-low: the opening range read from the session's bars (default 15)")
    x.add_argument("--or-high", dest="or_high", help="with or-mid/or-low: give the range instead of reading it")
    x.add_argument("--or-low", dest="or_low")
    x.add_argument("--flat-by", dest="flat_by", help="with a rule: out by this New York time (default 15:55)")
    x.add_argument("--entry-at", dest="entry_at",
                   help="with a rule: when the entry filled (RFC 3339); an atr or range stop reads nothing after it")
    _common(x)
    x.set_defaults(run=exit_plan)

    a = sub.add_parser("alerts", help="Price Alerts: evaluate rules over stored data")
    asub = a.add_subparsers(dest="alerts_command", required=True)
    chk = asub.add_parser("check", help="evaluate each rule once, now or at --at")
    chk.add_argument("--rules", required=True, help="an alert-rule/1 JSON object, or a JSON array of them")
    chk.add_argument("--dataset")
    chk.add_argument("--at", help="the instant to evaluate at (RFC 3339); default now")
    chk.set_defaults(run=alerts_check)
    ring = asub.add_parser("ring", help="the price alerts: stop, target, pct_move, opening_bell, time; each rings once")
    src = ring.add_mutually_exclusive_group(required=True)
    src.add_argument("--rules", help="a JSON array of alert rules (or {\"rules\": [...]}); a price rule names its "
                     "instrument_id; prices as ints or strings")
    src.add_argument("--from-plan", dest="from_plan",
                     help="an exit-plan/1 JSON from `traderos exit --json`: its stop, target and flat-by")
    ring.add_argument("--state", help="the file that remembers what rang (default: alerts/ring-state.json in --home)")
    ring.add_argument("--dataset")
    ring.add_argument("--at", help="the instant to check at (RFC 3339); default now")
    ring.set_defaults(run=alerts_ring)
    rst = asub.add_parser("reset", help="let alerts that rang ring again")
    rst.add_argument("ids", nargs="+", help="the rule ids to reset")
    rst.add_argument("--state", help="the alert state file (default: alerts/ring-state.json in --home)")
    rst.set_defaults(run=alerts_reset)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dataset", help="the dataset id; optional when the store holds exactly one")
    p.add_argument("--as-of", dest="as_of", help="decide as of this instant (RFC 3339); default the newest data")


# ---- shared ------------------------------------------------------------------------------------------------------

def _store(args: argparse.Namespace) -> Store:
    return Store(args.home).init()


def _dataset(store: Store, given: str | None) -> str:
    if given:
        return given
    datasets = store.datasets()
    if len(datasets) == 1:
        return datasets[0].dataset_id
    if not datasets:
        raise DataError("no datasets yet: `traderos init` loads the tutorial, `traderos data import <csv>` loads "
                        "yours", code=ErrorCode.MISSING_DATA)
    raise ValidationError("several datasets: name one with --dataset ("
                          + ", ".join(m.dataset_id for m in datasets) + ")", code=ErrorCode.INVALID_INPUT)


def _instant(text: str | None, flag: str) -> datetime | None:
    return parse_utc(text, field=flag) if text else None


def _read(path: str, what: str) -> str:
    p = pathlib.Path(path)
    if not p.is_file():
        raise DataError(f"{what}: {path} is not a file", code=ErrorCode.MISSING_DATA)
    if p.stat().st_size > MAX_FILE:
        raise ValidationError(f"{what}: {path} is larger than {MAX_FILE} bytes",
                              code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValidationError(f"{what}: {path} is not UTF-8 text") from None


def _strict_json(text: str, what: str):
    """JSON as the contracts read it: no float literal, no NaN/Infinity, no duplicate key."""
    def no_float(s):
        raise ValidationError(f"{what}: float literal {s} refused; write decimals as strings")

    def no_constant(s):
        raise ValidationError(f"{what}: {s} refused")

    def no_duplicates(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValidationError(f"{what}: duplicate key {k!r}")
            out[k] = v
        return out
    try:
        return json.loads(text, parse_float=no_float, parse_constant=no_constant, object_pairs_hook=no_duplicates)
    except json.JSONDecodeError as e:
        raise ValidationError(f"{what}: not valid JSON: {e.msg} at line {e.lineno}") from None
    except RecursionError:
        raise ValidationError(f"{what}: nested too deeply") from None
    except ValueError as e:                      # e.g. an integer longer than Python's 4300-digit limit
        raise ValidationError(f"{what}: {e}") from None


def _gate(cid: CapabilityId) -> str:
    """Refuse unless the registry allows it; returns the label to show beside an experimental result."""
    entry = Capabilities().require(cid)
    return " [EXPERIMENTAL: research use only]" if entry.user_status.value == "experimental" else ""


def _limits(record) -> list[str]:
    return [f"  - {lim.code}: {lim.detail}" for lim in record.limitations]


def _mv(mv) -> str:
    if mv.value is None:
        return f"unavailable ({mv.unavailable_reason})"
    return f"{decimal_str(mv.value) if isinstance(mv.value, Decimal) else mv.value} {mv.unit}"


# ---- commands ----------------------------------------------------------------------------------------------------

def market(args: argparse.Namespace) -> Output:
    tag = _gate(CapabilityId.S03)
    store = _store(args)
    mc = api.market_context(_dataset(store, args.dataset), as_of=_instant(args.as_of, "--as-of"), store=store)
    lines = [f"Market Pulse{tag}", f"{mc.dataset_id}, as of {mc.as_of.isoformat()} ({mc.provider}"
             + (f", feed {mc.feed}" if mc.feed else "") + ")",
             f"Regime: {mc.regime.label} ({mc.regime.method})",
             f"Breadth: {_mv(mc.breadth)}", ""]
    for mv in mc.moves:
        lines.append(f"  {mv.symbol:8} close {_mv(mv.last_close)}  change {_mv(mv.change_pct)}"
                     + ("  STALE" if mv.stale else ""))
    lines += ["", f"Completeness: {mc.completeness.value}. Safe for: {', '.join(s.value for s in mc.safe_for)}."]
    return Output(mc, "\n".join(lines + _limits(mc)))


def scan(args: argparse.Namespace) -> Output:
    tag = _gate(CapabilityId.S01)
    store = _store(args)
    if args.setups:
        floors = {k: parse_decimal(v, field=f"--{k.replace('_', '-')}") if v is not None else None
                  for k, v in (("min_price", args.min_price), ("min_volume", args.min_volume))}
        symbols = tuple(x.strip() for x in args.symbols.split(",") if x.strip()) if args.symbols else None
        res = api.scan_setups(_dataset(store, args.dataset), args.setups, symbols=symbols,
                              as_of=_instant(args.as_of, "--as-of"), or_minutes=args.or_minutes,
                              until=args.until, store=store,
                              **floors)
        title = f"The classic {args.setups} setups"
    else:
        criteria = from_json(ScanCriteria, _read(args.criteria, "--criteria"))
        res = api.scan_market(_dataset(store, args.dataset), criteria, as_of=_instant(args.as_of, "--as-of"),
                              store=store)
        title = criteria.name
    lines = [f"Chart Scanner{tag}: {title}", f"{res.dataset_id}, as of {res.as_of.isoformat()}", ""]
    for c in res.candidates:
        lines.append(f"  {c.symbol:8} {c.eligibility.value}" + (f"  ({', '.join(c.reasons)})" if c.reasons else ""))
        for t in c.traces:
            mark = {True: "yes", False: "no", None: "unknown"}[t.passed]
            lines.append(f"      {t.condition}: {mark}  [{_mv(t.left)} vs {_mv(t.right)}]"
                         + (f"  {t.reason}" if t.reason else ""))
    eligible = sum(c.eligibility is Eligibility.ELIGIBLE for c in res.candidates)
    lines += ["", "NO_ELIGIBLE_SETUP: nothing qualified." if res.no_eligible_setup else f"{eligible} eligible.",
              f"Completeness: {res.completeness.value}. Safe for: {', '.join(s.value for s in res.safe_for)}."]
    return Output(res, "\n".join(lines + _limits(res)))


def entry(args: argparse.Namespace) -> Output:
    tag = _gate(CapabilityId.S13)
    store = _store(args)
    if args.setup is None and (args.level is not None or args.min_volume_x is not None):
        raise ValidationError("--level and --min-volume-x go with --setup", code=ErrorCode.INVALID_INPUT)
    e = api.assess_entry(_dataset(store, args.dataset), args.symbol, as_of=_instant(args.as_of, "--as-of"),
                         setup=args.setup, or_minutes=args.or_minutes,
                         level=parse_decimal(args.level, field="--level") if args.level is not None else None,
                         min_volume_x=(parse_decimal(args.min_volume_x, field="--min-volume-x")
                                       if args.min_volume_x is not None else None), store=store)
    lines = [f"Entry Timer{tag}: {e.symbol} {e.status.value}, as of {e.as_of.isoformat()}",
             f"Reference price: {_mv(e.reference_price)}. Session open: {e.session_open}."]
    lines += [f"  constraint: {c}" for c in e.constraints]
    lines += [f"  not eligible because: {r}" for r in e.invalidation_reasons]
    lines += ["Research only: this never places, sends or recommends an order."]
    return Output(e, "\n".join(lines + _limits(e)))


_BARE_PRICE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def exit_plan(args: argparse.Namespace) -> Output:
    tag = _gate(CapabilityId.S14)
    store = _store(args)
    # A bare number is a price, as before; anything else is a stop or target rule (or-mid, pct:1, 2R, price:X ...).
    rule = {k: v for k, v in (("stop", args.stop), ("target", args.target))
            if v is not None and not _BARE_PRICE.match(v.strip())}
    price = {k: parse_decimal(v, field=f"--{k}") if v is not None and k not in rule else None
             for k, v in (("entry", args.entry), ("stop", args.stop), ("target", args.target),
                          ("or_high", args.or_high), ("or_low", args.or_low))}
    if not rule and any(v is not None for v in (args.flat_by, args.or_high, args.or_low, args.entry_at)):
        raise ValidationError("--flat-by, --or-high, --or-low and --entry-at go with a stop or target rule",
                              code=ErrorCode.INVALID_INPUT)
    p = api.plan_exit(_dataset(store, args.dataset), args.symbol, direction=args.direction,
                      entry_price=price["entry"], stop_price=price["stop"], target_price=price["target"],
                      as_of=_instant(args.as_of, "--as-of"), stop_rule=rule.get("stop"),
                      target_rule=rule.get("target"), setup=args.setup, or_minutes=args.or_minutes,
                      or_high=price["or_high"], or_low=price["or_low"], flat_by=args.flat_by,
                      entry_at=_instant(args.entry_at, "--entry-at"), store=store)
    lines = [f"Exit Planner{tag}: HYPOTHETICAL {p.direction.value} {p.symbol} from {p.entry_price}, "
             f"as of {p.as_of.isoformat()}"]
    for s in p.scenarios:
        lines.append(f"  {s.name:24} price {_mv(s.price)}  vs the entry, per unit {_mv(s.pnl_per_unit)}")
    lines += [f"  risk: {r}" for r in p.risks]
    return Output(p, "\n".join(lines + _limits(p)))


def _rules(text: str) -> list[AlertRule]:
    data = _strict_json(text, "--rules")
    items = data if isinstance(data, list) else [data]
    if not items or len(items) > MAX_RULES:
        raise ValidationError(f"--rules: give 1 to {MAX_RULES} rules")
    rules = [from_dict(AlertRule, item) for item in items]
    ids = [r.rule_id for r in rules]
    if len(set(ids)) != len(ids):
        raise ValidationError("--rules: two rules share a rule_id")
    return rules


def _last_evaluated(store: Store, rule_id: str, dataset_id: str, before: datetime) -> datetime | None:
    """When this rule was last evaluated on this dataset before `before`, from the events this store kept.

    One query inside SQLite, newest evaluation first, with no cap: a capped read stops seeing new evaluations once
    the cap is reached, and every later run would report a false missed window. Only the one event is parsed, so a
    check costs the same with fifty events stored or fifty thousand."""
    last = store.latest(AlertEvent, where={"rule_id": rule_id, "dataset_id": dataset_id}, order_by="evaluated_at",
                        before=before)
    return last.evaluated_at if last else None


def alerts_check(args: argparse.Namespace) -> Output:
    tag = _gate(CapabilityId.S18)
    rules = _rules(_read(args.rules, "--rules"))
    store = _store(args)
    dataset_id = _dataset(store, args.dataset)
    at = _instant(args.at, "--at") or datetime.now(UTC)
    # Every rule is evaluated before any event is stored: a rule refused halfway leaves nothing behind, so no
    # earlier rule's next previous_at moves on a run that failed.
    events = [api.evaluate_alert(dataset_id, rule, at=at, previous_at=_last_evaluated(store, rule.rule_id,
                                                                                      dataset_id, at), store=store)
              for rule in rules]
    with store.transaction() as tx:
        for ev in events:
            store.put(ev, db=tx)
    lines = [f"Price Alerts{tag}: {len(events)} rule(s) on {dataset_id} at {at.isoformat()}"]
    for ev in events:
        state = {True: "TRIGGERED", False: "not triggered", None: "not evaluated"}[ev.triggered]
        lines.append(f"  {ev.rule_id:12} {state}" + ("  MISSED WINDOW" if ev.missed else "")
                     + (f"  ({'; '.join(ev.reasons)})" if ev.reasons else ""))
    lines.append("Free has no always-on service: run this from your own scheduler; a missed window is reported.")
    return Output(events, "\n".join(lines))



def _alert_rules(args: argparse.Namespace) -> list[dict]:
    from traderos import clock
    from traderos.intelligence import alert_rules
    if args.from_plan:
        data = _strict_json(_read(args.from_plan, "--from-plan"), "--from-plan")
        if isinstance(data, dict) and "result" in data:            # the whole `--json` envelope
            data = data["result"]
        return alert_rules.rules_from_plan(from_dict(ExitPlan, data), clock.calendar("XNYS"))
    data = _strict_json(_read(args.rules, "--rules"), "--rules")
    items = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items or len(items) > MAX_RULES:
        raise ValidationError(f"--rules: give 1 to {MAX_RULES} rules", code=ErrorCode.INVALID_INPUT)
    for r in items:
        alert_rules.validate(r)
    ids = [alert_rules.rule_id(r) for r in items]
    if len(set(ids)) != len(ids):
        raise ValidationError("--rules: two rules share an id", code=ErrorCode.INVALID_INPUT)
    return items


def _state_path(args: argparse.Namespace, store: Store) -> pathlib.Path:
    return pathlib.Path(args.state) if args.state else store.home / "alerts" / "ring-state.json"


def _load_state(path: pathlib.Path) -> dict:
    from traderos.intelligence import alert_rules
    return alert_rules.validate_state(_strict_json(_read(str(path), "--state"), "--state")) if path.exists() else {}


def _save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def alerts_ring(args: argparse.Namespace) -> Output:
    from traderos.intelligence import alert_rules
    tag = _gate(CapabilityId.S18)
    rules = _alert_rules(args)
    store = _store(args)
    path = _state_path(args, store)
    state = _load_state(path)
    dataset_id = None
    if any(r["type"] not in alert_rules.NO_QUOTE for r in rules):
        named = {r["dataset_id"] for r in rules if r.get("dataset_id")}
        dataset_id = args.dataset or (named.pop() if len(named) == 1 else _dataset(store, None))
    at = _instant(args.at, "--at") or datetime.now(UTC)
    events, new_state = api.ring_alerts(rules, at=at, state=state, dataset_id=dataset_id, store=store)
    rang = [(e.rule_id, r[len("RANG: "):]) for e in events for r in e.reasons if r.startswith("RANG: ")]
    unknown = [e for e in events if e.triggered is None]
    lines = [f"Price Alerts{tag}: {len(rules)} rule(s) at {at.isoformat()}"
             + (f" on {dataset_id}" if dataset_id else "")]
    lines += [f"  RANG {rid}: {msg}" for rid, msg in rang]
    lines += [f"  UNKNOWN {e.rule_id}: could not be checked ({'; '.join(e.reasons)})" for e in unknown]
    if not rang and not unknown:
        lines.append("  Nothing rang.")
    elif not rang:
        lines.append(f"  Nothing rang among the rules that could be checked; {len(unknown)} could not be.")
    lines.append("Free has no always-on service: run this from your own scheduler. A price alert that rang stays "
                 "quiet until the price crosses back (with rearm) or `traderos alerts reset ID`; the bell, a time and "
                 "a % move ring again the next trading day.")
    return Output(events, "\n".join(lines + [f"  - {lim.code}: {lim.detail}" for lim in
                                             (events[0].limitations if events else ())]),
                  after=lambda: _save_state(path, new_state))


def alerts_reset(args: argparse.Namespace) -> Output:
    from traderos.intelligence import alert_rules
    _gate(CapabilityId.S18)
    store = _store(args)
    path = _state_path(args, store)
    new_state, cleared, absent = alert_rules.reset(_load_state(path), args.ids)
    lines = [f"  reset {i}: it can ring again" for i in cleared] + [f"  {i}: had not rung" for i in absent]
    return Output({"reset": cleared, "not_rung": absent}, "\n".join(lines),
                  after=lambda: _save_state(path, new_state))
