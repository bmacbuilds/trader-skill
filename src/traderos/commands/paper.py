"""The upstream order half, restored (ADR-012): `ticket place | result | fill | flatten | account`.

The owner's ruling (OWNER post by OSDev1 on the wall, 2026-11-20 14:50 UTC; Brian's answer "Y"): paper accounts only,
live refused in code. These commands are the upstream cmd_result, cmd_fill and cmd_flatten, and in place of its "Claude
calls place_stock_order, then Allow once" the one command that sends: `traderos ticket place`.

`place` sends a ticket ONLY when every gate holds, checked in this order, each before any request is made:
  1. the policy file you name says "paper": true and names no broker but Alpaca (live and IBKR are refused), and it
     is the very policy the ticket's risk check used (its hash); the ticket is sized in shares, not in contracts of
     a multiplier other than 1 (an Alpaca order is a number of shares: review of #10, item 2, ADR-009);
  2. you approved THIS ticket, with an explicit yes recorded word for word (`ticket approve`), for exactly this order
     (the upstream fingerprint), and nothing has happened to it since;
  3. its risk check passed, has not expired and is the latest attempt, measured by the real clock: place takes no
     --now, and a check dated after the real time (a pinned --now) is refused;
  4. your own Alpaca PAPER keys are set and the environment points nowhere else (alpaca_paper.credentials);
  5. the account the paper endpoint answers with looks like a paper account (alpaca_paper.require_paper_account).
Then, for a flatten ticket, your open sell orders for that symbol are canceled (the upstream step 5), and the body is
sent. Every placement, broker response (order id, status), cancel and fill is a journal event; a fill updates the
account statement the risk check reads (tickets.apply_fill), and so needs --account (refused, typed, without it). A
holding stated in contracts (a multiplier other than 1) is never flattened: risk.flatten refuses it (review of #10,
second round). A send whose outcome is unknown is recorded as indeterminate and
is never sent again: you record what happened with `ticket result`.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
from datetime import UTC, datetime, timedelta

from traderos.cli import Output
from traderos.contracts import (
    ActorRole,
    AuthorizationError,
    DataError,
    DomainError,
    ErrorCode,
    IntegrityError,
    PaperTicket,
    TraderOSError,
    ValidationError,
)

CLOCK_SKEW = timedelta(seconds=60)                # Upstream's: a time up to a minute ahead is the clock, not the future
CANCEL_POLLS = 10
CANCEL_WAIT_S = 0.5
CANCELED = ("canceled", "expired", "rejected", "replaced")


def _real_now() -> datetime:
    """The real clock. `place` never takes --now: a risk check's expiry cannot be moved."""
    return datetime.now(UTC).replace(microsecond=0)


def _d():
    from traderos.commands import decisions
    return decisions


# ---- the gates ---------------------------------------------------------------------------------------------------
def gate(store, t: PaperTicket, a, order: dict, checked_policy: dict, now: datetime) -> dict:
    """Every gate but the keys and the account (which need the adapter), in order. Returns the approval's payload."""
    from traderos import risk
    from traderos.risk.policy import require_paper
    from traderos.risk.tickets import contract_multiplier, fingerprint, latest, risk_verdict
    d = _d()
    require_paper(checked_policy)
    mult = contract_multiplier(order)
    if mult is not None:
        raise DomainError(f"{t.ticket_id} is sized at a contract multiplier of {mult}; an Alpaca paper order is a "
                          "number of shares, so it cannot be sent (nothing was sent)",
                          code=ErrorCode.OUTSIDE_CAPABILITY)
    if risk.policy_digest(checked_policy) != a.policy_hash:
        raise IntegrityError(f"this policy is not the one {t.ticket_id}'s risk check used; nothing was sent. Assess "
                             "the plan again with the policy you will place under", code=ErrorCode.INTEGRITY_MISMATCH)
    decision = latest(d._decisions(store), t.ticket_id)
    if decision is None:
        raise AuthorizationError(f"no yes is recorded for {t.ticket_id}; nothing was sent. Show it, then record the "
                                 f"person's reply word for word: traderos ticket approve {t.ticket_id} --reply "
                                 "\"<their reply>\"", code=ErrorCode.NOT_AUTHORIZED)
    if decision["status"] == "indeterminate":
        raise DataError(f"{t.ticket_id} was sent at {decision['at']} and the outcome is unknown, so it is never sent "
                        "again. Check your Alpaca paper account and record it: traderos ticket result "
                        f"{t.ticket_id} --status placed --broker-id ID, or --status failed",
                        code=ErrorCode.BROKER_INDETERMINATE)
    if decision["status"] != "approved":
        raise AuthorizationError(f"{t.ticket_id} is DONE: {decision['status']} at {decision['at']}; it is never "
                                 "sent again (a new check and a new yes make a new ticket)",
                                 code=ErrorCode.NOT_AUTHORIZED)
    approval = d.payload(store, decision["payload_hash"]) if decision.get("payload_hash") else {}
    if approval.get("order_fingerprint") != fingerprint(order):
        raise IntegrityError(f"the yes recorded for {t.ticket_id} is not for this exact order; nothing was sent. "
                             "Show it and ask again", code=ErrorCode.INTEGRITY_MISMATCH)
    if a.created_at > now + CLOCK_SKEW:
        raise DataError(f"{t.ticket_id}'s risk check is dated {a.created_at.isoformat()}, after the real time "
                        f"{now.isoformat()} (a pinned --now); placing uses the real clock, so nothing was sent",
                        code=ErrorCode.DATA_STALE)
    rv, why = risk_verdict(a, now)
    if rv is None:
        raise DataError(f"not sent: {why or 'the risk check did not pass'}. Record that it was not placed "
                        f"(traderos ticket result {t.ticket_id} --status denied), then assess again",
                        code=ErrorCode.DATA_STALE)
    newer = d._superseded_by(store, a.assessment_id)
    if newer:
        raise DataError(f"not sent: a newer risk assessment ({newer}) supersedes {t.ticket_id}. Record that it was "
                        f"not placed (traderos ticket result {t.ticket_id} --status denied)",
                        code=ErrorCode.DATA_STALE)
    return approval


# ---- place -------------------------------------------------------------------------------------------------------
def place(args) -> Output:
    """Send ONE approved ticket to your Alpaca PAPER account (upstream's "Allow once")."""
    from traderos.adapters import alpaca_paper
    from traderos.risk import inputs
    from traderos.risk.tickets import body, kind_of, plain
    d = _d()
    if getattr(args, "now", None):
        raise ValidationError("`traderos ticket place` takes no --now: it sends at the real time, and the risk "
                              "check's expiry is measured against it")
    if not args.policy:
        raise ValidationError("say which policy: --policy FILE (it must say \"paper\": true; live trading is refused "
                              "in this build)")
    store = d._store(args)
    now = _real_now()
    t, a, _, order = d._load_ticket(store, args.ticket_id)
    checked = inputs.read_policy(args.policy)
    approval = gate(store, t, a, order, checked, now)
    client = alpaca_paper.client(store.home)               # keys and environment refused here, before any request
    account = client.account()                             # the paper endpoint, checked to be a paper account
    cancels = _clear_sell_orders(client, store, t, order, now) if kind_of(t) == "exit" else []
    sent = body(order)
    base = {k: approval.get(k) for k in ("symbol", "side", "qty", "kind", "max_loss", "stop", "plain")}
    base.update({"ticket": t.ticket_id, "order_fingerprint": approval.get("order_fingerprint"), "paper": True,
                 "endpoint": alpaca_paper.PAPER_HOST})
    _claim(store, t, a, base, plain(order), now)        # written BEFORE the send: a crash leaves it unknown, not new
    try:
        resp = client.submit(sent)
    except alpaca_paper.Indeterminate:
        raise                                               # the claim stands: sent, outcome unknown
    except TraderOSError as e:                              # Alpaca refused it, or it was never sent
        d.remember_payload(store, "journal.ticket.failed", (t.ticket_id, a.assessment_id),
                           f"Failed: {t.ticket_id} ({plain(order)}) was not placed: {e.message[:200]}",
                           dict(base, status="failed", note=e.message[:200]), at=now, codes=("paper", "failed"))
        raise
    oid, status = str(resp["id"]), str(resp.get("status", ""))
    d.remember_payload(store, "journal.ticket.placed", (t.ticket_id, a.assessment_id, f"alpaca-order:{oid}"),
                       f"Placed {t.ticket_id} on your Alpaca PAPER account: {plain(order)}; Alpaca order {oid} "
                       f"({status})", dict(base, status="placed", broker_order_id=oid, broker_status=status),
                       at=now, codes=("paper", "placed"))
    number = alpaca_paper.mask_account(account.get("account_number", ""))
    lines = [f"Placed {t.ticket_id} on your Alpaca PAPER account ({number}): {plain(order)}.",
             f"Alpaca order {oid}: {status}."]
    if cancels:
        lines.insert(0, f"Canceled {len(cancels)} open {order['symbol']} sell order(s) first: {', '.join(cancels)}.")
    lines.append(f"When it fills, record it: traderos ticket fill {t.ticket_id} --account FILE (asks the paper "
                 "endpoint for the fill and updates your account statement).")
    if kind_of(t) == "entry" and order.get("order_class") == "bracket":
        lines.append("When the stop-loss or the take-profit fills later, record that the same way.")
    return Output({"ticket": t.ticket_id, "status": "placed", "broker_order_id": oid, "broker_status": status,
                   "endpoint": alpaca_paper.BASE_URL, "body": sent, "canceled": cancels}, "\n".join(lines))


def _claim(store, t, a, base: dict, words: str, now) -> None:
    """Record the send before it happens (status indeterminate), so a crash or a timeout can never leave the ticket
    looking unsent; and make sure this is the only sender of it."""
    from traderos.risk.tickets import latest
    d = _d()
    if (latest(d._decisions(store), t.ticket_id) or {}).get("status") != "approved":
        raise DataError(f"{t.ticket_id} changed while it was being placed; nothing was sent",
                        code=ErrorCode.BROKER_INDETERMINATE)
    claim = d.remember_payload(store, "journal.ticket.indeterminate", (t.ticket_id, a.assessment_id),
                               f"Sending {t.ticket_id} ({words}) to Alpaca's paper endpoint: until it answers, the "
                               "outcome is unknown", dict(base, status="indeterminate", note="sending"), at=now,
                               codes=("paper", "sending"))
    since = []
    for dec in d._decisions(store):
        if dec["ticket"] == t.ticket_id:
            since = [] if dec["status"] == "approved" else [*since, dec]
    if not since or since[0]["event_id"] != claim.event_id:
        raise DataError(f"another `traderos ticket place {t.ticket_id}` is sending it; this one sent nothing",
                        code=ErrorCode.BROKER_INDETERMINATE)


def _clear_sell_orders(client, store, t, order, now) -> list[str]:
    """The upstream flatten step 5: a bracket's stop-loss and take-profit hold the shares, so Alpaca rejects a new sell
    until they are canceled. Cancel each open sell order for the symbol and wait until each is done. If one FILLED
    meanwhile, the position may already be closed: nothing is sent."""
    d = _d()
    sym = order["symbol"]
    ids = [str(o.get("id")) for o in client.open_orders(sym) if str(o.get("side", "")).lower() == "sell"]
    for oid in ids:
        client.cancel(oid)
        d.remember_payload(store, "journal.ticket.canceled", (t.ticket_id, f"alpaca-order:{oid}"),
                           f"Canceled open {sym} sell order {oid} at Alpaca's paper endpoint, for flatten "
                           f"{t.ticket_id}", {"ticket": t.ticket_id, "status": "canceled", "symbol": sym,
                                              "side": "sell", "broker_order_id": oid}, at=now,
                           codes=("paper", "canceled"))
    for _ in range(CANCEL_POLLS):
        if not ids:
            return ids
        states = {oid: str(client.order(oid).get("status", "")) for oid in ids}
        filled = [oid for oid, st in states.items() if st in ("filled", "partially_filled")]
        if filled:
            raise DataError(f"not sent: the open {sym} order {filled[0]} filled while TraderOS was canceling it, so "
                            "your position may already be closed. Record its fill (traderos ticket fill), check your "
                            "paper account, and flatten again if shares are left", code=ErrorCode.UNKNOWN_EXPOSURE)
        if all(st in CANCELED for st in states.values()):
            return ids
        client.sleep(CANCEL_WAIT_S)
    raise DataError(f"not sent: Alpaca has not finished canceling the open {sym} sell orders; run `traderos ticket "
                    f"place {t.ticket_id}` again in a moment", code=ErrorCode.DATA_STALE)


# ---- result (the upstream cmd_result)
# ----------------------------------------------------------------------------------
def result(args) -> Output:
    """Record what happened to an approved ticket when you know better than TraderOS: placed (with Alpaca's order
    id), denied (you did not send it) or failed. With --filled-qty and --filled-price the fill goes into --account."""
    from traderos.risk.tickets import latest, plain
    d = _d()
    if not args.status:
        raise ValidationError("say what happened: --status placed | denied | failed")
    store = d._store(args)
    now = d._now(args)
    t, a, _, order = d._load_ticket(store, args.ticket_id)
    decision = latest(d._decisions(store), t.ticket_id)
    if not decision or decision["status"] not in ("approved", "indeterminate"):
        raise AuthorizationError(f"{t.ticket_id} has no yes recorded (or already has a result); a result can only "
                                 "follow an approval", code=ErrorCode.NOT_AUTHORIZED)
    approval = _approval_of(store, t.ticket_id)
    rec = {k: approval.get(k) for k in ("symbol", "side", "qty", "kind", "max_loss", "stop", "plain")}
    rec.update({"ticket": t.ticket_id, "status": args.status})
    if args.broker_id:
        rec["broker_order_id"] = str(args.broker_id).strip()[:64]
    if args.note:
        rec["note"] = str(args.note).strip()[:200]
    fill = None
    if args.status == "placed" and args.filled_qty and args.filled_price:
        fill = _fill_into(args, rec["symbol"], rec["side"], args.filled_qty, args.filled_price, now,
                          multiplier=_multiplier_of(store, t.ticket_id))
    elif args.filled_qty or args.filled_price:
        raise ValidationError("a fill needs --status placed, --filled-qty and --filled-price together")
    refs = (t.ticket_id, a.assessment_id) + ((f"alpaca-order:{rec['broker_order_id']}",)
                                            if rec.get("broker_order_id") else ())
    d.remember_payload(store, f"journal.ticket.{args.status}", refs,
                       f"Result for {t.ticket_id} ({plain(order)}): {args.status}"
                       + (f", Alpaca order {rec['broker_order_id']}" if rec.get("broker_order_id") else "")
                       + (f"; {rec['note']}" if rec.get("note") else ""), rec, at=now,
                       codes=("paper", args.status))
    lines = [f"Recorded {t.ticket_id} as {args.status}."]
    if fill is not None:
        lines.append(_record_fill(store, fill, t.ticket_id, now))
    elif args.status == "placed":
        lines.append(f"When it fills, record it: traderos ticket fill {t.ticket_id} --account FILE, or by hand: "
                     f"traderos ticket fill --symbol {rec['symbol']} --side {rec['side']} --qty Q --price P "
                     "--account FILE")
    return Output(rec, "\n".join(lines))


def _approval_of(store, ticket_id: str) -> dict:
    d = _d()
    for dec in reversed(d._decisions(store)):
        if dec["ticket"] == ticket_id and dec["status"] == "approved" and dec.get("payload_hash"):
            try:
                return d.payload(store, dec["payload_hash"])
            except TraderOSError:
                break
    t, _, _, order = d._load_ticket(store, ticket_id)          # an F1 sign-off: rebuild the upstream record
    from traderos.risk.tickets import kind_of, max_loss, plain
    loss, stop = max_loss(order)
    return {"symbol": t.symbol, "side": order["side"], "qty": str(order["qty"]), "kind": kind_of(t),
            "max_loss": None if loss is None else str(loss), "stop": None if stop is None else str(stop),
            "plain": plain(order)}


# ---- fill (the upstream cmd_fill, apply_fill, log_fill)
# ----------------------------------------------------------------
def fill(args) -> Output:
    """A fill into your account statement and the journal. With a ticket id, the fills come from Alpaca's paper
    endpoint (the placed order and its legs); without one, by hand, as the upstream fill took them."""
    from traderos.adapters import alpaca_paper
    from traderos.risk.policy import number
    d = _d()
    store = d._store(args)
    now = d._now(args)
    if not args.account:
        raise ValidationError("say which account statement the fill goes into: --account FILE (the file risk "
                              "assess reads)")
    if not args.ticket_id:
        missing = [f"--{k}" for k in ("symbol", "side", "qty", "price") if not getattr(args, k)]
        if missing:
            raise ValidationError(f"a fill by hand needs {', '.join(missing)} (or a ticket id, to ask Alpaca)")
        rec = _fill_into(args, args.symbol, args.side, args.qty, args.price, now, date=args.date)
        return Output(rec, _record_fill(store, rec, None, now))
    t = store.get(PaperTicket, args.ticket_id)
    placed = [x for x in d._decisions(store) if x["ticket"] == t.ticket_id and x["status"] == "placed"]
    oid = d.payload(store, placed[-1]["payload_hash"]).get("broker_order_id") if placed else None
    if not oid:
        raise DataError(f"no Alpaca order is recorded for {t.ticket_id} (place it first, or record the fill by hand "
                        "with --symbol --side --qty --price)", code=ErrorCode.MISSING_DATA)
    client = alpaca_paper.client(store.home)
    order = client.order(oid)
    done = {f.get("broker_order_id") for f in d._fills(store)}
    out, waiting = [], []
    for x in [order, *(order.get("legs") or [])]:
        xid, status = str(x.get("id", "")), str(x.get("status", ""))
        qty, price = number(x.get("filled_qty")), number(x.get("filled_avg_price"))
        if xid in done or qty is None or qty <= 0 or price is None:
            continue
        if status not in alpaca_paper.DONE:
            waiting.append(f"{xid} is {status} ({x.get('filled_qty')} filled so far)")
            continue
        if str(x.get("symbol", "")).upper() != t.symbol or x.get("side") not in ("buy", "sell"):
            raise DataError(f"Alpaca's order {xid} is not a {t.symbol} buy or sell", code=ErrorCode.INTEGRITY_MISMATCH)
        rec = _fill_into(args, t.symbol, x["side"], qty, price, now, multiplier=_multiplier_of(store, t.ticket_id))
        rec["broker_order_id"] = xid
        out.append(_record_fill(store, rec, t.ticket_id, now))
    if not out:
        return Output({"recorded": 0, "waiting": waiting}, f"No new fill for {t.ticket_id} at Alpaca's paper "
                      "endpoint" + (": " + "; ".join(waiting) if waiting else "") + ". Nothing was changed.")
    return Output({"recorded": len(out), "waiting": waiting}, "\n".join(out + [f"Still working: {w}" for w in waiting]))


def _multiplier_of(store, ticket_id: str):
    """The contract multiplier of a ticket's plan (None for a flatten ticket: it sells the holding as it is)."""
    _, _, plan, _ = _d()._load_ticket(store, ticket_id)
    return plan.multiplier if plan is not None else None


def _fill_into(args, symbol, side, qty, price, now, *, date=None, multiplier=None) -> dict:
    """apply_fill on the account statement file (atomically, and only if the result is a statement the risk check
    can read), then the upstream log_fill record."""
    import re

    from traderos.risk import inputs
    from traderos.risk.tickets import apply_fill, log_fill
    d = _d()
    if not getattr(args, "account", None):          # a TypeError on None before (review of #10, second round)
        raise ValidationError("a fill goes into your account statement: say which with --account FILE (the file risk "
                              "assess reads); nothing was recorded")
    day = date or d._ny_date(now)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(day)):
        raise ValidationError("--date wants a date like 2026-10-19")
    path = pathlib.Path(args.account)
    raw = inputs.read_json(str(path), "account statement")
    new = apply_fill(raw, symbol, side, qty, price, day, multiplier)
    try:
        inputs.account(new)
    except TraderOSError as e:
        raise ValidationError(f"recording this fill would leave an account statement the risk check cannot read "
                              f"({e.message}); nothing was written") from None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".account-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(new, indent=2) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    rec = log_fill(symbol, side, qty, price)
    rec.update({"date": day, "account": path.name})
    return rec


def _record_fill(store, rec: dict, ticket_id: str | None, now) -> str:
    d = _d()
    refs = ((ticket_id,) if ticket_id else ()) + ((f"alpaca-order:{rec['broker_order_id']}",)
                                                  if rec.get("broker_order_id") else ())
    d.remember_payload(store, "journal.ticket.filled", refs, f"Filled: {rec['plain']} on {rec['date']}"
                       + (f" ({ticket_id})" if ticket_id else ""), rec, at=now, codes=("paper", "filled"))
    return f"Recorded {rec['plain']} on {rec['date']} in {rec['account']} and the journal."


# ---- flatten (the upstream cmd_flatten)
# --------------------------------------------------------------------------------
def flatten(args) -> Output:
    """A market sell, by flat_by, of what you hold in a symbol, checked at once as an exit; then show, approve and
    place it like any ticket (place cancels the bracket's open legs first)."""
    from traderos import risk
    from traderos.capabilities import Capabilities
    from traderos.contracts import CapabilityId
    from traderos.risk import inputs
    from traderos.risk.tickets import plain
    d = _d()
    Capabilities().require(CapabilityId.S16)
    if not args.policy or not args.account:
        raise ValidationError("flatten needs --policy FILE and --account FILE (the qty comes from your account "
                              "statement unless you pass --qty)")
    store = d._store(args)
    now = d._now(args)
    policy = inputs.read_json(args.policy, "policy")
    account = inputs.read_account(args.account)
    prices = inputs.read_prices(args.prices) if args.prices else None
    fl = risk.flatten(args.ticket_id, policy, account=account, prices=prices, log=d._log(store), now=now,
                      qty=args.qty)
    t, a = fl.ticket, fl.assessment
    open_before = [x.ticket_id for x in store.all(PaperTicket, limit=None)
                   if d._decision_for(store, x.ticket_id) is None and d._ny_date(x.created_at) == d._ny_date(now)]
    store.put(a)
    store.put(t)
    d._remember(store, "journal.plan", (a.plan_id, a.assessment_id), f"Flatten {t.symbol}: {plain(fl.order)} "
                f"(flat by {fl.flat_by} New York; qty from {fl.source})", a, actor=ActorRole.SYSTEM, at=now)
    d._remember(store, "journal.ticket", (t.ticket_id, a.assessment_id), f"Paper ticket {t.ticket_id} (flatten): "
                f"{plain(fl.order)}", t, actor=ActorRole.SYSTEM, at=now)
    lines = [f"Flatten {t.symbol}: {plain(fl.order)} (flat by {fl.flat_by} New York).",
             f"Ticket {t.ticket_id}: the risk check passed it as an exit (a sell that only closes what you hold is "
             "never blocked by the daily limits or the entry window)."]
    if open_before:
        lines.append(f"It replaced {len(open_before)} earlier proposal{'' if len(open_before) == 1 else 's'} "
                     "(only the latest can be approved).")
    if args.qty is None:
        lines.append("The qty comes from your account statement. Check it against your paper account (traderos "
                     "ticket account) before you say yes.")
    lines += ["Next:",
              f"  1. traderos ticket show {t.ticket_id}, then the person's yes for this one order.",
              f"  2. traderos ticket place {t.ticket_id} --policy FILE: it first cancels your open {t.symbol} sell "
              "orders at Alpaca (a bracket's stop-loss and take-profit hold these shares), then sends the sell."]
    return Output(t, "\n".join(lines))


# ---- account -----------------------------------------------------------------------------------------------------
def account(args) -> Output:
    """Your Alpaca paper account's status, read from the paper endpoint (nothing is changed)."""
    from traderos.adapters import alpaca_paper
    d = _d()
    store = d._store(args)
    acct = alpaca_paper.client(store.home).account()
    info = {"paper": True, "endpoint": alpaca_paper.BASE_URL,
            "account_number": alpaca_paper.mask_account(acct.get("account_number", ""))}
    for k in ("status", "currency", "cash", "buying_power", "equity", "pattern_day_trader", "trading_blocked",
              "daytrade_count"):
        if k in acct:
            info[k] = acct[k]
    text = (f"Alpaca PAPER account {info['account_number']}: {info.get('status', 'status unknown')}, cash "
            f"{info.get('cash', '?')}, buying power {info.get('buying_power', '?')}, equity {info.get('equity', '?')} "
            f"{info.get('currency', '')}".rstrip() + f" (read from {alpaca_paper.PAPER_HOST}; nothing was changed).")
    return Output(info, text)


__all__ = ["account", "fill", "flatten", "gate", "place", "result"]
