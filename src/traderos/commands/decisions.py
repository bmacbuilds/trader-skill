"""Part C commands: `risk assess`, `ticket`, `journal add|list` (docs/cli-contract-v1.md; S15-S17, S19, I05; ADR-009,
ADR-012).

Every file is read as strictly as every other input (no floats, no NaN, no duplicate or unknown keys):

    plan     {"symbol": "TSLA", "direction": "long", "date": "2026-10-19", "entry": "364.18", "stop": "358.32",
              "target": "370.04"}                       (optional: multiplier, tick_size, currency, instrument_id)
    policy   your limits, the upstream config keys: budget, cash_buffer_pct, max_position_pct, max_order_usd,
             min_order_usd, max_loss_per_trade_usd, max_daily_loss_usd, max_trades_per_day, max_orders_per_day,
             min_reward_risk, no_entries_before, no_entries_after, blocked, pdt_guard (each required; no default) and
             allow_options, allow_margin, allow_short, require_stop, flat_by, fractional, paper (only true), broker
             (only alpaca) (optional)
    account  {"settled_cash": "5000", "reserved_cash": "0", "positions": [], "orders": []}: what you hold (a
             position may state its contract "multiplier"), and the orders of the last few days you placed yourself
             (optional: without it, exposure is UNKNOWN and no ticket can be drafted). TraderOS's own paper orders
             and fills are in the journal.
    prices   {"prices": {"TSLA": "366.475"}, "detail": {"TSLA": {"time": "2026-10-19T14:52:13Z"}}} (a market entry
             needs a fresh one: without it the check BLOCKs, MISSING_DATA; a limit order does not)

Every number is under 10^15 in size; a larger one, and any arithmetic the assessment cannot do, is a typed refusal
(exit 3) that is recorded and supersedes the older assessment (review of #10, item 5, ADR-009).

A ticket is drafted from the LATEST assessment while its check is still valid (15 minutes, and for an entry no later
than the cutoff or the close). The upstream flow, its statuses and its words (ADR-012 restores its order half):

    ticket <assessment-id>             draft it                       (trader-budget's orders.json, then show)
    ticket show <id>                   the ticket, its status and the exact body TraderOS would send
    ticket approve <id> --reply "yes"  your explicit yes for that one ticket (`sign-off` is the F1 name for it)
    ticket decline <id>                a no
    ticket place <id> --policy FILE    sends it to your Alpaca PAPER account: upstream's "Allow once"
    (commands/paper.py) ticket result <id> --status S      what happened, when you know better than TraderOS (placed,
    denied, failed) ticket fill [<id>] --account FILE  a fill into your account statement (from the paper endpoint, or
    by hand) ticket flatten <symbol> ...        the upstream sell that closes a day trade by flat_by, checked as an exit
    ticket account                     your Alpaca paper account's status
    ticket log [--all]                 the day's decisions

An approved ticket counts as placed today until a result is recorded (by `place`, by `result`, or by your account
statement naming it: "ticket": "PT-..."). Live trading is refused in code: see traderos.adapters.alpaca_paper.

The journal is append-only (S19): an entry is a research-memory event, never edited; plans, assessments, tickets,
approvals, results, fills and no-trade records are linked to it by id.
"""
from __future__ import annotations

from datetime import UTC, datetime

from traderos.capabilities import Capabilities
from traderos.cli import Output
from traderos.contracts import (
    ActorRole,
    AuthorizationError,
    CapabilityId,
    ErrorCode,
    HypotheticalRiskAssessment,
    PaperTicket,
    ResearchMemoryEvent,
    RiskPlan,
    TraderOSError,
    ValidationError,
    canonical_bytes,
    from_dict,
    record_hash,
    sha256_bytes,
    with_hash,
)
from traderos.contracts.canonical import parse_utc
from traderos.store import Store

PLAN_KEYS = {"symbol": "symbol", "direction": "direction", "date": "plan_date", "entry": "entry_price",
             "stop": "stop_price", "target": "target_price", "multiplier": "multiplier", "currency": "currency",
             "instrument_id": "instrument_id", "tick_size": "tick_size"}
PLAN_NUMBERS = ("entry", "stop", "target", "multiplier", "tick_size")
MOVED_KEYS = {"equity": "policy budget", "risk_per_trade": "policy max_loss_per_trade_usd",
              "quantity": "the size now comes from your policy", "whole_units": "sizes are whole units",
              "settled_cash": "account settled_cash", "reserved_cash": "account reserved_cash",
              "existing_exposure": "account positions and orders", "prices_observed_at": "a prices file's times"}
MAX_PLAN_BYTES = 64 * 1024
ATTEMPTS = ("journal.plan", "journal.plan.failed")
# The upstream statuses (approved, declined, placed, denied, failed), plus "indeterminate" (a send whose outcome is
# unknown: the approval stays unresolved) and the F1 name of an approval, "signed_off" (read as approved).
DECISIONS = ("journal.ticket.approved", "journal.ticket.signed_off", "journal.ticket.declined",
             "journal.ticket.placed", "journal.ticket.denied", "journal.ticket.failed",
             "journal.ticket.indeterminate")
FILLS = ("journal.ticket.filled",)
CANCELS = ("journal.ticket.canceled",)                # a flatten's cancels: in the log, not a decision
RESULTS = ("placed", "denied", "failed")
ACTIONS = ("show", "approve", "sign-off", "decline", "place", "result", "fill", "flatten", "account", "log")


def register(sub) -> None:
    p = sub.add_parser("risk", help="hypothetical sizing and risk; no broker")
    rs = p.add_subparsers(dest="risk_command", required=True)
    x = rs.add_parser("assess", help="size a hypothetical plan, check it against your policy, show the four measures")
    x.add_argument("--plan", required=True, help="a JSON plan file")
    x.add_argument("--policy", required=True, help="a JSON file of your limits (no limit has a default)")
    x.add_argument("--account", help="a JSON statement of your account and today's orders (else exposure is UNKNOWN)")
    x.add_argument("--prices", help="a JSON prices file; a price under 15 minutes old is fresh")
    x.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                   help="a limit order instead of market, at the plan's entry or at PRICE")
    x.add_argument("--now", help="the time to assess at, RFC 3339 with its zone (default: now)")
    x.add_argument("--stress", default="0.05", help="the gap beyond the stop to stress, as a fraction")
    x.set_defaults(run=risk_assess)
    p = sub.add_parser("ticket", help="a paper ticket: draft, show, approve, decline, place (Alpaca PAPER only), "
                                      "result, fill, flatten, account, log")
    p.add_argument("what", help="an assessment id to draft a ticket from, or " + " | ".join(ACTIONS))
    p.add_argument("ticket_id", nargs="?", help="the ticket (the symbol, for flatten)")
    p.add_argument("--reply", help="for approve and decline: your reply, word for word")
    p.add_argument("--now", help="the time, RFC 3339 with its zone (default: now; place always uses the real clock)")
    p.add_argument("--all", action="store_true", help="for log: every day, not only today")
    p.add_argument("--policy", help="for place and flatten: your policy file (place needs \"paper\": true)")
    p.add_argument("--account", help="for flatten, fill and result: your account statement (a fill updates it)")
    p.add_argument("--prices", help="for flatten: a prices file")
    p.add_argument("--qty", help="for flatten: the shares you hold (default: your account statement)")
    p.add_argument("--status", choices=RESULTS, help="for result: placed, denied or failed")
    p.add_argument("--broker-id", help="for result: Alpaca's order id")
    p.add_argument("--note", help="for result: what happened, in a few words")
    p.add_argument("--filled-qty", help="for result: the shares filled")
    p.add_argument("--filled-price", help="for result: the fill price")
    p.add_argument("--symbol", help="for fill by hand")
    p.add_argument("--side", choices=("buy", "sell"), help="for fill by hand")
    p.add_argument("--price", help="for fill by hand: the fill price")
    p.add_argument("--date", help="for fill by hand: YYYY-MM-DD (default: today in New York)")
    p.set_defaults(run=ticket)
    p = sub.add_parser("journal", help="your local append-only journal")
    js = p.add_subparsers(dest="journal_command", required=True)
    x = js.add_parser("add", help="add an entry (it can never be edited, only followed)")
    x.add_argument("text")
    x.add_argument("--ref", action="append", default=[], help="an id this entry is about (repeatable)")
    x.add_argument("--kind", choices=["note", "decision", "lesson"], default="note")
    x.set_defaults(run=journal_add)
    x = js.add_parser("list", help="the newest entries first")
    x.add_argument("--limit", type=int, default=50)
    x.set_defaults(run=journal_list)


def _store(args) -> Store:
    return Store(args.home).init()


def _now(args) -> datetime:
    return parse_utc(args.now, field="--now") if getattr(args, "now", None) else datetime.now(UTC).replace(
        microsecond=0)


def _remember(store: Store, kind: str, refs: tuple[str, ...], summary: str, payload, *, actor=ActorRole.HUMAN,
              codes: tuple[str, ...] = ("hypothetical",), at: datetime | None = None) -> ResearchMemoryEvent:
    at = at or datetime.now(UTC)
    previous = store.last_memory_hash()          # in the id too: the same note again (a pinned --now) is a new event
    event = with_hash(ResearchMemoryEvent(
        event_id=f"ME-{kind}-{sha256_bytes(canonical_bytes([kind, list(refs), summary, at, previous]))[:16]}",
        actor_role=actor, kind=kind, subject_refs=refs, outcome_codes=codes, summary=summary,
        payload_hash=_hash_marker(payload) if isinstance(payload, _Marker) else record_hash(payload),
        previous_event_hash=previous, at=at))
    store.remember(event)
    return event


def read_plan(path: str, *, now: datetime | None = None, limit: str | None = None) -> RiskPlan:
    from traderos.risk.inputs import num, read_json, text
    data = read_json(path, "plan", max_bytes=MAX_PLAN_BYTES)
    if not isinstance(data, dict):
        raise ValidationError("a plan is a JSON object")
    moved = sorted(k for k in data if k in MOVED_KEYS)
    if moved:
        raise ValidationError("plan key(s) moved out of the plan (ADR-009): " + "; ".join(
            f"{k} -> {MOVED_KEYS[k]}" for k in moved))
    unknown = sorted(set(data) - set(PLAN_KEYS))
    if unknown:
        raise ValidationError(f"unknown plan key(s): {', '.join(unknown)}; allowed: {', '.join(PLAN_KEYS)}")
    if "symbol" not in data or "direction" not in data or "entry" not in data:
        raise ValidationError("a plan needs symbol, direction and entry")
    for k in PLAN_NUMBERS:
        if data.get(k) is not None:
            num(data[k], f"plan.{k}")             # bounded: 1E+99 is refused here, typed (review item 5)
    fields = {PLAN_KEYS[k]: v for k, v in data.items()}
    symbol = text(fields["symbol"], "plan.symbol").upper()
    fields["symbol"] = symbol
    fields.setdefault("instrument_id", f"INS-{symbol}-UNKNOWN-{fields.get('currency', 'USD')}")
    if limit is not None:
        fields["limit_price"] = fields["entry_price"] if limit == "plan" else str(num(limit, "--limit"))
    now = now or datetime.now(UTC)
    fields["as_of"] = now.isoformat()
    fields["schema_version"] = RiskPlan.SCHEMA
    fields["plan_id"] = "RP-" + sha256_bytes(canonical_bytes(fields))[:20]   # each assessment is a new plan
    return from_dict(RiskPlan, fields)


# ---- the store's view: attempts, sign-offs, tickets -------------------------------------------------------------
def _latest_attempt(store: Store) -> ResearchMemoryEvent | None:
    found = store.search_memory("", kinds=ATTEMPTS, limit=1)
    return found[0] if found else None


def _superseded_by(store: Store, assessment_id: str) -> str | None:
    last = _latest_attempt(store)
    if last is None or assessment_id in last.subject_refs:
        return None
    what = next((r for r in last.subject_refs if r.startswith("RA-")), None)
    return f"{what or 'a refused attempt'} at {_ny(last.at)} New York"


def _ny(at: datetime) -> str:
    from traderos.risk.check import to_eastern
    return to_eastern(at).strftime("%H:%M")


def _ny_date(at: datetime) -> str:
    from traderos.risk.check import to_eastern
    return to_eastern(at).date().isoformat()


def _decisions(store: Store) -> list[dict]:
    """Every approval, decline and result, oldest first, as the upstream log records."""
    from traderos.risk.tickets import ny_iso
    out = []
    for e in reversed(store.search_memory("", kinds=DECISIONS, limit=200)):
        ticket_id = next((r for r in e.subject_refs if r.startswith("PT-")), None)
        status = e.kind.rsplit(".", 1)[1]
        out.append({"ticket": ticket_id, "status": "approved" if status == "signed_off" else status,
                    "at": ny_iso(e.at), "date": _ny_date(e.at), "event_id": e.event_id, "summary": e.summary,
                    "payload_hash": e.payload_hash})
    return out


def _fills(store: Store) -> list[dict]:
    """Every fill recorded, oldest first, as the upstream log_fill records."""
    from traderos.risk.tickets import ny_iso
    out = []
    for e in reversed(store.search_memory("", kinds=FILLS, limit=200)):
        rec = dict(payload(store, e.payload_hash))
        rec.update({"at": ny_iso(e.at), "date": rec.get("date") or _ny_date(e.at), "event_id": e.event_id,
                    "summary": e.summary})
        out.append(rec)
    return out


def payload(store: Store, digest: str) -> dict:
    """The payload a journal event points at (stored content-addressed beside it)."""
    import json
    data = json.loads(store.read_artifact(digest).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValidationError("a journal payload is not a JSON object", code=ErrorCode.INTEGRITY_MISMATCH)
    return data


def remember_payload(store: Store, kind: str, refs: tuple[str, ...], summary: str, data: dict, *,
                     codes: tuple[str, ...], at: datetime, actor=ActorRole.HUMAN) -> ResearchMemoryEvent:
    """A journal event whose payload (the upstream log record) is kept content-addressed, so the log can be read
    back."""
    store.put_artifact(canonical_bytes(data), "application/json")
    return _remember(store, kind, refs, summary, _Marker(data), actor=actor, codes=codes, at=at)


def _log(store: Store) -> list[dict]:
    """The journal as the upstream broker.json log, oldest first, for the risk check: each approval (with the approved
    ticket's qty, kind, max loss and stop, as its approve wrote them), each result (placed, denied, failed, which
    carry the same) and each fill. A send whose outcome is unknown adds nothing, so its approval stays unresolved and
    counts as placed (#2); a decline is not read by the check."""
    from traderos.risk.tickets import kind_of, ny_iso, plain
    rows = []
    for e in reversed(store.search_memory("", kinds=DECISIONS + FILLS, limit=200)):
        status = e.kind.rsplit(".", 1)[1]
        status = "approved" if status == "signed_off" else status
        ticket_id = next((r for r in e.subject_refs if r.startswith("PT-")), None)
        at, date = ny_iso(e.at), _ny_date(e.at)
        if status == "approved":
            t, a, _, order = _load_ticket(store, ticket_id)
            rows.append({"symbol": t.symbol, "side": order["side"], "status": "approved", "date": date, "at": at,
                         "fingerprint": t.ticket_id, "qty": t.quantity, "kind": kind_of(t),
                         "max_loss": a.planned_loss.value, "stop": t.stop_price, "plain": plain(order)})
        elif status in (*RESULTS, "filled"):
            rec = {k: v for k, v in payload(store, e.payload_hash).items() if k in LOG_KEYS}
            rec.update({"status": status, "date": rec.get("date") or date, "at": at})
            if status != "filled":
                rec["fingerprint"] = ticket_id
            rows.append(rec)
    return rows


LOG_KEYS = ("symbol", "side", "status", "date", "at", "kind", "max_loss", "qty", "price", "stop", "plain",
            "fingerprint", "broker_order_id")


# ---- risk assess -------------------------------------------------------------------------------------------------
def risk_assess(args) -> Output:
    from traderos import risk
    from traderos.risk import inputs
    Capabilities().require(CapabilityId.S15)
    Capabilities().require(CapabilityId.S16)
    store = _store(args)
    now = None
    try:
        try:
            now = _now(args)
            plan = read_plan(args.plan, now=now, limit=args.limit)
            policy = inputs.read_policy(args.policy)
            account = inputs.read_account(args.account) if args.account else None
            prices = inputs.read_prices(args.prices) if args.prices else None
            ev = risk.evaluate(plan, policy, account=account, prices=prices,
                               approvals=_log(store) if account is not None else (), now=now,
                               stress_gap=inputs.num(args.stress, "--stress"))
        except ArithmeticError as e:
            # A number no input bound caught (decimal.InvalidOperation, an overflow) is a refusal of the input,
            # typed and recorded like any other, so it supersedes the older assessment (review item 5).
            raise risk.out_of_range(e) from None
    except TraderOSError as err:
        # The upstream drop_stale, on every failure (upstream #2, review change 6): a failed attempt is recorded, and it
        # supersedes every earlier assessment, so an older plan cannot become a ticket after a newer one failed.
        _remember(store, "journal.plan.failed", (), f"Risk assessment refused [{err.code.value}]: "
                  f"{err.message[:300]}", _Marker({"code": err.code.value, "message": err.message}),
                  actor=ActorRole.SYSTEM, codes=("hypothetical", err.code.value), at=now)
        raise
    plan, assessment = ev.plan, ev.assessment
    store.put(plan)
    store.put(assessment)
    refused = risk.no_trade(plan, assessment)
    if refused is not None:
        store.put(refused)
    _remember(store, "journal.plan", (plan.plan_id, assessment.assessment_id) +
              ((refused.record_id,) if refused else ()),
              f"Hypothetical {plan.direction.value} {plan.symbol} at {plan.entry_price}: "
              + ("ticket eligible" if assessment.ticket_eligible else
                 "refused (" + ", ".join(assessment.refusal_reasons) + ")"), assessment, actor=ActorRole.SYSTEM,
              at=now)
    text = risk.describe(ev)
    if assessment.ticket_eligible:
        text += f"\nA paper ticket: traderos ticket {assessment.assessment_id}"
    return Output(assessment, text)


# ---- ticket ------------------------------------------------------------------------------------------------------
def ticket(args) -> Output:
    Capabilities().require(CapabilityId.S17)
    action = args.what
    if action in ("log", "account"):
        if action == "log":
            return _ticket_log(args)
        from traderos.commands import paper
        return paper.account(args)
    if action in ACTIONS:
        if not args.ticket_id and action != "fill":
            what = "<symbol>" if action == "flatten" else "<ticket-id>"
            raise ValidationError(f"say which {'symbol' if action == 'flatten' else 'ticket'}: traderos ticket "
                                  f"{action} {what}")
        if action in ("show", "approve", "sign-off", "decline"):
            return {"show": _ticket_show, "approve": _ticket_approve, "sign-off": _ticket_approve,
                    "decline": _ticket_decline}[action](args)
        from traderos.commands import paper
        return {"place": paper.place, "result": paper.result, "fill": paper.fill,
                "flatten": paper.flatten}[action](args)
    if args.ticket_id:
        raise ValidationError(f"unknown ticket action {action!r}: use an assessment id, or {' | '.join(ACTIONS)}")
    return _ticket_draft(args)


def _load_ticket(store: Store, ticket_id: str):
    """(ticket, assessment, plan, the upstream order). A flatten ticket (an exit) has no RiskPlan: its plan is its
    order."""
    from traderos import risk
    from traderos.risk.tickets import kind_of
    t = store.get(PaperTicket, ticket_id)
    a = store.get(HypotheticalRiskAssessment, t.assessment_id)
    plan = None if kind_of(t) == "exit" else store.get(RiskPlan, a.plan_id)
    return t, a, plan, risk.ticket_order(t, a, plan)


def _ticket_draft(args) -> Output:
    from traderos import risk
    from traderos.risk.tickets import render, status_of
    store = _store(args)
    now = _now(args)
    assessment = store.get(HypotheticalRiskAssessment, args.what)
    existing = store.find(PaperTicket, f"PT-{assessment.assessment_id[3:]}")
    if existing is None:
        plan = store.get(RiskPlan, assessment.plan_id)
        t = risk.ticket(plan, assessment, now=now, superseded_by=_superseded_by(store, assessment.assessment_id))
        store.put(t)
        _remember(store, "journal.ticket", (t.ticket_id, assessment.assessment_id),
                  f"Paper ticket {t.ticket_id}: {t.direction.value} {t.quantity} {t.symbol} {t.order_type}"
                  + (f" {t.limit_price}" if t.limit_price is not None else "")
                  + f", stop {t.stop_price}, target {t.target_price}", t, actor=ActorRole.SYSTEM, at=now)
    t, assessment, _, order = _load_ticket(store, f"PT-{assessment.assessment_id[3:]}")
    decision = _decision_for(store, t.ticket_id)
    ready, label = status_of(assessment, decision, now, _superseded_by(store, assessment.assessment_id))
    return Output(t, render(t, order, label, ready))


def _decision_for(store: Store, ticket_id: str):
    from traderos.risk.tickets import latest
    return latest(_decisions(store), ticket_id)


def _ticket_show(args) -> Output:
    from traderos.risk.tickets import render, status_of, ticket_json
    store = _store(args)
    now = _now(args)
    t, a, _, order = _load_ticket(store, args.ticket_id)
    ready, label = status_of(a, _decision_for(store, t.ticket_id), now, _superseded_by(store, a.assessment_id))
    return Output(ticket_json(t, order, label, ready), render(t, order, label, ready))


def _open_symbols(store: Store, today: str, but: str) -> list[str]:
    """The symbols of the other tickets drafted today that nobody has decided on: upstream's "batch"."""
    decided = {d["ticket"] for d in _decisions(store)}
    return [t.symbol for t in store.all(PaperTicket, limit=None) if t.ticket_id != but and t.ticket_id not in decided
            and _ny_date(t.created_at) == today]


def _ticket_approve(args) -> Output:
    """The upstream cmd_approve: your explicit yes for ONE ticket, word for word, bound to that ticket's exact order
    (its fingerprint). It sends nothing: `traderos ticket place` does, after it (upstream's "Allow once")."""
    import json

    from traderos.risk.tickets import (
        body,
        contract_multiplier,
        fingerprint,
        kind_of,
        max_loss,
        parse_yes,
        plain,
        status_of,
    )
    store = _store(args)
    now = _now(args)
    t, a, _, order = _load_ticket(store, args.ticket_id)
    ready, label = status_of(a, _decision_for(store, t.ticket_id), now, _superseded_by(store, a.assessment_id))
    if not ready:
        raise AuthorizationError(f"ticket {t.ticket_id} is not ready: {label}")
    ok, why = parse_yes(args.reply, order, [*_open_symbols(store, _ny_date(now), t.ticket_id), t.symbol])
    if not ok:
        raise AuthorizationError(f"not recorded: {why}. Ask again with the ticket {t.ticket_id}.",
                                 code=ErrorCode.INVALID_APPROVAL)
    loss, stop = max_loss(order)
    data = {"ticket": t.ticket_id, "status": "approved", "reply": args.reply, "plain": plain(order),
            "order_fingerprint": fingerprint(order), "symbol": t.symbol, "side": order["side"],
            "qty": str(order["qty"]), "kind": kind_of(t), "max_loss": None if loss is None else str(loss),
            "stop": None if stop is None else str(stop), "paper": True, "broker": "alpaca"}
    event = remember_payload(store, "journal.ticket.approved", (t.ticket_id, a.assessment_id),
                             f"Approved paper ticket {t.ticket_id}: {plain(order)}; reply {args.reply!r}", data,
                             at=now, codes=("paper", "approved"))
    m = contract_multiplier(order)
    if m is not None:                 # `ticket place` refuses it: never say it will be sent (review of #10, 2nd round)
        how = (f"TraderOS will not send it: it is sized in contracts of {m}, and an Alpaca paper order is a number "
               f"of shares, so `traderos ticket place` refuses it. Place it at your broker yourself, then record what "
               f"happened: `traderos ticket result {t.ticket_id} --status placed` (or denied).\n")
    else:
        how = ("TraderOS will send exactly this to Alpaca's paper endpoint when you run `traderos ticket "
               f"place {t.ticket_id} --policy FILE`:\n" + json.dumps(body(order), indent=2) + "\n"
               "Nothing is sent before that. ")
    return Output(event, f"Recorded your yes for {t.ticket_id}: {plain(order)}.\n" + how
                         + "Until a result is recorded it counts as placed today and a new entry is refused; if you "
                         f"decide not to place it: `traderos ticket result {t.ticket_id} --status denied`.")


def _ticket_decline(args) -> Output:
    from traderos.risk.tickets import plain
    store = _store(args)
    now = _now(args)
    t, a, _, order = _load_ticket(store, args.ticket_id)
    reply = args.reply or "no"
    event = _remember(store, "journal.ticket.declined", (t.ticket_id, a.assessment_id),
                      f"Declined paper ticket {t.ticket_id}: {plain(order)}; reply {reply!r}",
                      _Marker({"ticket": t.ticket_id, "reply": reply, "plain": plain(order)}), at=now,
                      codes=("paper", "declined"))
    return Output(event, f"Recorded: you said no to {t.ticket_id} ({plain(order)}). Nothing was sent.")


def _ticket_log(args) -> Output:
    """The upstream cmd_log: the day's decisions, results and fills, and how many were placed."""
    from traderos.risk.tickets import ny_iso
    store = _store(args)
    today = _ny_date(_now(args))
    rows = []
    for e in reversed(store.search_memory("", kinds=DECISIONS + FILLS + CANCELS, limit=200)):
        status = e.kind.rsplit(".", 1)[1]
        if args.all or _ny_date(e.at) == today:
            rows.append({"ticket": next((r for r in e.subject_refs if r.startswith("PT-")), None),
                         "status": "approved" if status == "signed_off" else status, "at": ny_iso(e.at),
                         "date": _ny_date(e.at), "event_id": e.event_id, "summary": e.summary})
    if not rows:
        return Output([], "No decisions recorded" + ("." if args.all else " today."))
    lines = [f"{d['at']}  {d['status']:<13} {d['summary']}" for d in rows]
    placed = sum(1 for d in rows if d["status"] == "placed")
    lines.append(f"{placed} placed" + ("." if args.all else " today."))
    return Output(rows, "\n".join(lines))


# ---- journal -----------------------------------------------------------------------------------------------------
def journal_add(args) -> Output:
    Capabilities().require(CapabilityId.S19)
    store = _store(args)
    text = args.text.strip()
    if not text or len(text) > 2000:
        raise ValidationError("a journal entry is 1 to 2000 characters")
    refs = tuple(r for r in args.ref if r)[:20]
    marker = {"text": text, "refs": list(refs)}
    event = _remember(store, f"journal.{args.kind}", refs, text, _Marker(marker))
    return Output(event, f"Journal entry {event.event_id} added (append-only).")


def journal_list(args) -> Output:
    Capabilities().require(CapabilityId.S19)
    events = [e for e in _store(args).search_memory("", limit=min(max(args.limit, 1), 200) * 4)
              if e.kind.startswith("journal.")][:args.limit]
    if not events:
        return Output([], "The journal is empty. `traderos journal add \"...\"` starts it.")
    return Output(events, "\n".join(f"{e.at:%Y-%m-%d %H:%M}  {e.kind[8:]:<9} {e.summary}"
                                     + (f"  [{', '.join(e.subject_refs)}]" if e.subject_refs else "")
                                     for e in events))


class _Marker:
    """A plain payload for a journal note: its hash is the canonical hash of the note itself."""

    def __init__(self, data: dict) -> None:
        self.data = data


def _hash_marker(m: _Marker) -> str:
    return sha256_bytes(canonical_bytes(m.data))


__all__ = ["read_plan", "register"]
