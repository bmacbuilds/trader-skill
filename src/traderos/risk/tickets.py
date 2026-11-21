"""The upstream order tickets, ported (S17). Source: tank/day-trader-skill/trader-broker/ticket.py at 3d0f4ef (MIT;
the NOTICE file), with upstream PRs #49 (as is), #2 (as is here) and #21 (with the
review's change 1) applied.

**The order half is back (ADR-012).** The owner's ruling (OWNER post by OSDev1 on the wall, 2026-11-20 14:50 UTC;
Brian's own answer was "Y"): "OSDev4: restore [the upstream] paper-order half of ticket.py in #10, unchanged except what
must fit. It sends only to the paper endpoint, and live is refused in code with a test that proves it." It supersedes
spec v2.0's F1 "document only" for S17. The F1 port (ADR-009) had removed that half; this file and
commands/decisions.py restore it:

**Restored as upstream wrote them:** `tool_input` (the exact arguments: its ALPACA_FIELDS and STRING_FIELDS, numbers as
strings, "why" and "plan" never sent), `kind_of`, the fields of `ticket_json`, its statuses (approved, declined,
placed, denied, failed, filled), its labels, `apply_fill` (a cost-basis lot per buy, the oldest lots relieved first
on a sell) and `log_fill`'s record; and in commands/decisions.py its approve, result, fill, flatten, show, decline and
log. `plain_parts`, `plain`, `max_loss`, `amount_numbers` and `parse_yes` (an explicit yes for ONE ticket, refusing
"sure", "yes to all", a doubt, another ticket's symbol and a wrong amount) were kept by the F1 port and are unchanged.

**Changed, each with its reason (ADR-012):**
* **The sender** (the one design change). The upstream ticket.py never sends: it prints the exact arguments and Claude
  calls a separately installed Alpaca MCP server's `place_stock_order`, whose paper/live choice lives in that
  server's environment (ALPACA_PAPER_TRADE). Reason: "the endpoint was set in a separate MCP server's environment,
  outside code TraderOS can test; the owner's 11-20 ruling requires paper only, refused live, proven by a test."
  TraderOS sends the same arguments itself through traderos.adapters.alpaca_paper, which can reach only Alpaca's
  paper endpoint. So the render's block reads "TraderOS will send exactly this to Alpaca's paper endpoint" where
  the upstream read "Claude will call place_stock_order with exactly", it shows the REST body the MCP server built from
  those arguments (the bracket legs as Alpaca's take_profit / stop_loss objects; alpaca_paper.order_body), and its
  "Allow once" card is `traderos ticket place`.
* **One account line, "PAPER account, Alpaca".** Upstream's "LIVE ACCOUNT, REAL MONEY" cannot render: a policy whose
  "paper" is not true is refused (policy.broker_keys, policy.require_paper).
* **The IBKR branch is refused, not rendered**: TraderOS cannot prove an Interactive Brokers connector routes to a
  paper account.
* **`apply_fill` updates your account statement** (the file `risk assess --account` reads), not trader/portfolio.json:
  a buy is paid from settled cash, a sale's proceeds go to `unsettled_cash` (they settle T+1: S15 / RC17, the same
  reason as check.py's batch rule), and it never starts an account from the budget (P7: no guessed account).
* **Every decision, result and fill is a journal event** (S19 stays ours, ADR-009), not a broker.json line.
* Decimal money (spec 04.3).
* **Prices exactly as given** (review of #10, item 1, ADR-009). The upstream `_p` printed "%.2f" and `max_loss` rounded
  the risk a share to the cent, so a stop at 0.4851 read 0.49 on the ticket and its max loss halved; the ticket now
  shows, and `tool_input` sends, the exact price the risk was measured at.
* **A holding keeps its multiplier** (review of #10, item 2). `apply_fill` takes the fill's contract multiplier,
  costs a fill at qty x price x multiplier, writes `multiplier` on a new holding (when not 1) and refuses a fill at
  another multiplier than the holding's, so the account statement values it as the risk check values a plan.
* **A yes cannot come before its check** (review of #10, item 6). `status_of` says NOT READY when the sign-off's
  time is before the assessment's created_at: a `--now` in the past recorded a yes for a check that did not exist
  yet. Nothing is recorded.
* **A ticket sized in contracts says it is not sent** (review of #10, second round). `ticket place` refuses it (an
  Alpaca paper order is a number of shares), so `render` no longer says "TraderOS will send exactly this" for it.

**Kept from the F1 port (ADR-009):** a verdict with no readable `valid_until` counts as EXPIRED (review change 1 to
#21); the risk verdict is the stored assessment bound by the plan hash; a newer assessment supersedes an older one;
no separate "from another day" rule; `fingerprint` is the one in traderos.risk.check.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from traderos.adapters.alpaca_paper import BASE_URL, PAPER_HOST, order_body
from traderos.contracts import ValidationError
from traderos.risk.check import ALPACA_FIELDS, eastern_offset, fingerprint, price_text, to_eastern
from traderos.risk.policy import number as _f

STRING_FIELDS = ("qty", "notional", "limit_price", "stop_price", "trail_price", "trail_percent",
                 "take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
STATUSES = ("approved", "declined", "placed", "denied", "failed", "filled")       # The upstream
ACCOUNT_LINE = "PAPER account, Alpaca"            # the only account line: live is refused in this build

TIF_WORDS = {"day": "good for today", "gtc": "good until canceled", "opg": "at the open",
             "cls": "at the close", "ioc": "immediate or cancel", "fok": "fill or kill"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
PENNY = Decimal("0.01")


def ny_iso(now_utc):
    """An aware instant -> "2026-10-19T10:15:00-04:00" (New York wall clock with its offset). Upstream's, kept."""
    off = eastern_offset(now_utc)
    return "%s%+03d:00" % (to_eastern(now_utc).replace(microsecond=0).isoformat(), off)


# ---------------------------------------------------------------- the order, in words

def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
        return str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")
    return str(v)


def _money(v):
    d = Decimal(_f(v))
    if d != d.quantize(PENNY):                     # a sub-cent price is shown as it is, never rounded
        return "$" + "{:,f}".format(d.normalize())
    s = "{:,.2f}".format(d)
    return "$" + (s[:-3] if s.endswith(".00") else s)


def _p(v):
    """A price exactly as given (check.price_text): an order price is never rounded (ADR-009, review item 1). Upstream
    printed "%.2f", which moved a 0.4851 stop leg to 0.49."""
    return price_text(_f(v))


def tool_input(order):
    """The exact arguments for the order: Alpaca's parameter names, numbers as strings (place_stock_order typed qty,
    notional and prices as str). "why" and "plan" are ours and are never sent. Upstream's, unchanged."""
    out = {}
    for k in ALPACA_FIELDS:
        if k in order and order[k] is not None:
            out[k] = _str_num(order[k]) if k in STRING_FIELDS else order[k]
    return out


def body(order):
    """What TraderOS sends for this order: tool_input as Alpaca's REST body (alpaca_paper.order_body)."""
    return order_body(tool_input(order))


def is_bracket(order):
    oc = str(order.get("order_class") or "").lower()
    legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
    return oc == "bracket" or (not oc and legs)


def max_loss(order):
    """(dollars, stop) lost if the stop fills exactly at the stop, or (None, stop)."""
    stop = _f(order.get("stop_loss_stop_price")) or _f((order.get("plan") or {}).get("stop"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    mult = _f(plan.get("multiplier")) or Decimal(1)
    if str(order.get("type", "market")) == "limit" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    elif _f(plan.get("max_loss")) is not None:
        return _f(plan["max_loss"]).quantize(PENNY), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    return (q * risk * mult).quantize(PENNY, rounding=ROUND_HALF_EVEN), stop          # exact risk (review item 1)


def plain_parts(order):
    """The ticket in plain words, one part per line."""
    side = str(order.get("side", "")).upper()
    sym = order.get("symbol", "?")
    typ = str(order.get("type", "market"))
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    if is_bracket(order):
        how = "limit %s" % _p(order["limit_price"]) if typ == "limit" and order.get("limit_price") else typ
        parts = ["%s %s %s, %s, %s" % (side, _str_num(order.get("qty")), sym, how, tif)]
        if order.get("stop_loss_stop_price") is not None:
            leg = "stop-loss %s" % _p(order["stop_loss_stop_price"])
            if order.get("stop_loss_limit_price") is not None:
                leg += " (limit %s)" % _p(order["stop_loss_limit_price"])
            parts.append(leg)
        if order.get("take_profit_limit_price") is not None:
            parts.append("take-profit %s" % _p(order["take_profit_limit_price"]))
        loss, stop = max_loss(order)
        if loss is not None:
            parts.append("max loss about %s if the stop fills at %s (it can fill worse in a fast market)"
                         % (_money(loss), _p(stop)))
        return parts
    if order.get("notional") is not None:
        what = "%s of %s" % (_money(order["notional"]), sym)
    else:
        q = _str_num(order.get("qty"))
        what = "%s share%s of %s" % (q, "" if q == "1" else "s", sym)
    if typ == "limit":
        how = "limit order at %s" % _money(order.get("limit_price"))
    elif typ == "stop":
        how = "stop order at %s" % _money(order.get("stop_price"))
    elif typ == "stop_limit":
        how = "stop %s, limit %s" % (_money(order.get("stop_price")), _money(order.get("limit_price")))
    else:
        how = "%s order" % typ.replace("_", " ")
    return ["%s %s, %s, %s" % (side, what, how, tif)]


def plain(order):
    """BUY $1,250 of AAPL, market order, good for today
    BUY 8 TSLA, market, good for today / stop-loss 358.32 / take-profit 370.04 / max loss about $46.88 ..."""
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    for k in ("notional", "qty", "limit_price", "stop_loss_stop_price", "take_profit_limit_price",
              "stop_loss_limit_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one ticket? Returns (ok, why).

    Refused: an empty reply or a question, anything that does not start with yes,
    a batch word (all, both, the rest), a doubt (but, wait, if), a yes that names
    ANOTHER TICKET IN THE BATCH by its symbol (a ticker that is not in the batch
    is not checked), and a number that is not one of this ticket's amounts."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one ticket at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    tokens = set(re.findall(r"[A-Z][A-Z.\-]{0,6}", raw.upper()))
    others = {s.upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this ticket is for %s" % (", ".join(sorted(named)), sym)
    nums = [Decimal(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < Decimal("0.005") for n in nums for w in want):
            return False, "the reply says %s, but this ticket is %s" % (
                ", ".join(_str_num(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- status

def latest(decisions, ticket_id):
    """The latest decision recorded for this ticket (approved, declined, or a result: placed, denied, failed), or
    None. `decisions` oldest first."""
    found = None
    for rec in decisions:
        if rec.get("ticket") == ticket_id:
            found = rec
    return found


def risk_verdict(assessment, now: datetime):
    """(eligible assessment, None) or (None, why it is not usable now). A missing valid_until counts as expired."""
    if not assessment.ticket_eligible:
        return None, None
    until = assessment.valid_until
    if until is None:
        return None, "the risk check has no expiry recorded, so it counts as expired; run `traderos risk assess` again"
    if now > until:
        return None, ("the risk check expired at %s New York; run `traderos risk assess` again"
                      % to_eastern(until).strftime("%H:%M"))
    return assessment, None


def status_of(assessment, decision, now: datetime, superseded_by: str | None = None):
    """(ready, label). READY only when the assessment passed, has not expired, is the latest one, and nobody has
    decided on the ticket yet. The upstream labels; "approved" waits for `traderos ticket place` (its card's Allow
    once)."""
    if decision:
        if decision["status"] == "approved":
            return False, ("APPROVED by you at %s, waiting for `traderos ticket place %s`"
                           % (decision.get("at"), decision.get("ticket")))
        if decision["status"] == "indeterminate":
            return False, ("SENT at %s, OUTCOME UNKNOWN: check your Alpaca paper account and record it with "
                           "`traderos ticket result %s --status placed|failed`" % (decision.get("at"),
                                                                                  decision.get("ticket")))
        return False, "DONE: %s at %s" % (decision["status"], decision.get("at"))
    if not assessment.ticket_eligible:
        return False, "BLOCKED by the risk check: " + "; ".join(assessment.refusal_reasons)
    if superseded_by:
        return False, ("NOT READY: a newer risk assessment (%s) supersedes this one; only the latest can be "
                       "approved" % superseded_by)
    if now < assessment.created_at:
        # A yes dated before its own check (a backdated --now) would fall on another day and escape the
        # unresolved-approval rule. Upstream refused a check "from another day"; ADR-009 dropped that rule because
        # valid_until covers a LATER day only (review item 6).
        return False, ("NOT READY: the time given, %s, is before this ticket's risk check (%s); a yes cannot come "
                       "before its check" % (ny_iso(now), ny_iso(assessment.created_at)))
    rv, why = risk_verdict(assessment, now)
    if rv is None:
        return False, "NOT READY: " + why
    return True, "READY: risk check PASS, waiting for your yes"


def order_of(ticket, planned_loss=None, multiplier=None, levels=None):
    """The ticket as the upstream order fields, so its plain words, its yes rules, its fingerprint and its tool_input
    read it unchanged. `levels` is the plan's entry, stop and target, which the upstream fingerprint binds."""
    sells = (ticket.direction.value == "long") == (kind_of(ticket) == "exit")     # an exit sells the long
    o = {"symbol": ticket.symbol, "side": "sell" if sells else "buy",
         "qty": _str_num(ticket.quantity), "type": ticket.order_type, "time_in_force": ticket.time_in_force}
    if ticket.limit_price is not None:
        o["limit_price"] = _p(ticket.limit_price)
    if ticket.stop_price is not None or ticket.target_price is not None:
        o["order_class"] = "bracket"
    if ticket.target_price is not None:
        o["take_profit_limit_price"] = _p(ticket.target_price)
    if ticket.stop_price is not None:
        o["stop_loss_stop_price"] = _p(ticket.stop_price)
    plan = {k: v for k, v in (levels or {}).items() if v is not None}
    if planned_loss is not None:
        plan["max_loss"] = planned_loss
    if multiplier is not None and multiplier != 1:
        plan["multiplier"] = multiplier
    if plan:
        o["plan"] = plan
    return o


def contract_multiplier(order):
    """An order's contract multiplier when it is not 1, else None. An Alpaca paper order is a number of shares, so
    `traderos ticket place` refuses such a ticket (review of #10, item 2), and the ticket says so."""
    m = _f((order.get("plan") or {}).get("multiplier"))
    return _s(m) if m is not None and m != 1 else None


def kind_of(ticket):
    """entry or exit, as the risk check saw it (the upstream kind_of; a ticket records it)."""
    return getattr(ticket, "kind", None) or "entry"


def render(ticket, order, label, ready):
    lines = ["PAPER TICKET %s, %s" % (ticket.ticket_id, ACCOUNT_LINE)]
    for part in plain_parts(order):
        lines.append("  " + part)
    for note in ticket.notes:
        lines.append("  " + note)
    lines.append("  " + label)
    if kind_of(ticket) == "exit":
        lines.append("  TraderOS first cancels your open %s sell orders at Alpaca (a bracket's stop-loss and"
                     % order.get("symbol"))
        lines.append("  take-profit hold these shares), then sends the sell.")
    contracts = contract_multiplier(order)
    if contracts is not None:          # place refuses it: the ticket never says it will be sent (review of #10)
        lines.append("  TraderOS will not send this: it is sized in contracts of %s, and an Alpaca paper order is"
                     % contracts)
        lines.append("  a number of shares, so `traderos ticket place` refuses it. Place it at your broker yourself,")
        lines.append("  then record what happened: traderos ticket result %s --status placed (or denied)."
                     % ticket.ticket_id)
    else:
        lines.append("  TraderOS will send exactly this to Alpaca's paper endpoint (%s):" % PAPER_HOST)
        for line in json.dumps(body(order), indent=2).splitlines():
            lines.append("    " + line)
        if is_bracket(order):
            lines.append("  Alpaca places the entry, then the stop-loss and the take-profit as two orders")
            lines.append("  that cancel each other: when one fills, the other is canceled.")
    if ready:
        lines.append("  Reply \"yes\" to %s this one order, or \"no\" to skip it."
                     % ("approve" if contracts is not None else "place"))
        if contracts is None:
            lines.append("  Then `traderos ticket place %s` sends it; nothing is sent before that." % ticket.ticket_id)
    lines.append("  " + ticket.disclaimer)
    return "\n".join(lines)


def ticket_json(ticket, order, label, ready):
    """The upstream ticket_json fields; "tool": "place_stock_order" is now the endpoint TraderOS sends to itself."""
    loss, stop = max_loss(order)
    return {"ticket": ticket, "ready": ready, "status": label, "plain": plain(order),
            "fingerprint": fingerprint(order), "paper": True, "broker": "alpaca", "endpoint": BASE_URL,
            "tool_input": tool_input(order), "body": body(order), "why": "; ".join(ticket.notes),
            "kind": kind_of(ticket), "bracket": is_bracket(order), "max_loss": None if loss is None else str(loss),
            "stop": None if stop is None else str(stop)}


# ---------------------------------------------------------------- fills -> your account statement

def _shares(pos):
    if pos is None:
        return Decimal(0)
    for k in ("shares", "qty"):
        if pos.get(k) is not None:
            return Decimal(str(pos[k]))
    return sum((Decimal(str(lot.get("shares", 0))) for lot in pos.get("lots", [])), Decimal(0))


def _s(d):
    d = Decimal(d)
    return str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")


def _cash(d):
    return format(Decimal(d).quantize(PENNY, rounding=ROUND_HALF_EVEN), "f")


def apply_fill(account, symbol, side, qty, price, date, multiplier=None):
    """Record a filled order in your account statement: cash and shares, with a cost-basis lot per buy (oldest lots
    are relieved first on a sell). The upstream apply_fill on trader/portfolio.json, with two changes (ADR-012): a buy
    is paid from settled cash and a sale's proceeds go to unsettled_cash (T+1, S15 / RC17), and there must already be
    a statement (P7: no account is started from the budget). Returns the new statement; the caller writes it.

    `multiplier` is the contract multiplier of what was filled (the ticket's plan's; None = the holding's own, or 1).
    A holding keeps its multiplier in the statement and a fill costs qty x price x multiplier, so the risk check
    values what you hold as it values a plan (review item 2); a fill at another multiplier than the holding's is
    refused."""
    if not isinstance(account, dict):
        raise ValidationError("the account statement is not a JSON object")
    pf = copy.deepcopy(account)
    symbol = str(symbol).strip().upper()
    q, p = _f(qty), _f(price)
    if q is None or p is None or q <= 0 or p <= 0:
        raise ValidationError("a fill needs a positive qty and price")
    if side not in ("buy", "sell"):
        raise ValidationError("a fill is a buy or a sell, got %r" % (side,))
    pos = None
    for x in pf.setdefault("positions", []):
        if str(x.get("ticker") or x.get("symbol") or "").upper() == symbol:
            pos = x
    held_m = _f(pos.get("multiplier")) if pos is not None and pos.get("multiplier") is not None else None
    m = _f(multiplier) if multiplier is not None else (held_m or Decimal(1))
    if m is None or m <= 0:
        raise ValidationError("a fill's multiplier must be a positive number, got %r" % (multiplier,))
    if pos is not None and (held_m or Decimal(1)) != m:
        raise ValidationError("your account statement holds %s at multiplier %s; this fill is at %s: correct the "
                              "statement first" % (symbol, _s(held_m or 1), _s(m)))
    cost = (q * p * m).quantize(PENNY, rounding=ROUND_HALF_EVEN)
    key = "qty" if pos is not None and "qty" in pos and "shares" not in pos else "shares"
    if side == "buy":
        if pos is None:
            pos = {"symbol": symbol, "shares": "0", "lots": []}
            if m != 1:
                pos["multiplier"] = _s(m)
            pf["positions"].append(pos)
        held = _shares(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": _s(q), "cost": _cash(cost)})
        pos[key] = _s(held + q)
        pf["settled_cash"] = _cash(Decimal(str(pf.get("settled_cash", "0"))) - cost)
    else:
        held = _shares(pos)
        if pos is None or held < q:
            raise ValidationError("cannot record selling %s %s: the account holds %s" % (_s(q), symbol, _s(held)))
        left = q
        for lot in sorted(pos.get("lots", []), key=lambda lot: str(lot.get("date", ""))):
            if left <= 0:
                break
            have = Decimal(str(lot["shares"]))
            take = min(left, have)
            per = Decimal(str(lot["cost"])) / have if have else Decimal(0)
            lot["cost"] = _cash(Decimal(str(lot["cost"])) - per * take)
            lot["shares"] = _s(have - take)
            left -= take
        pos["lots"] = [lot for lot in pos.get("lots", []) if Decimal(str(lot["shares"])) > 0]
        pos[key] = _s(held - q)
        if held - q <= 0:
            pf["positions"].remove(pos)
        pf["unsettled_cash"] = _cash(Decimal(str(pf.get("unsettled_cash", "0"))) + cost)
    return pf


def log_fill(symbol, side, qty, price):
    """A fill as the upstream log record, so the risk check can count day trades (a buy and a sell the same day)."""
    q = _s(Decimal(str(qty)))
    return {"status": "filled", "symbol": str(symbol).upper(), "side": side, "qty": q, "price": _p(price),
            "plain": "%s %s %s at %s" % (side.upper(), q, str(symbol).upper(), _p(price))}


__all__ = ["ACCOUNT_LINE", "BATCH_WORDS", "DOUBT_WORDS", "STATUSES", "STRING_FIELDS", "YES_WORDS", "amount_numbers",
           "apply_fill", "body", "contract_multiplier", "fingerprint", "is_bracket", "kind_of", "latest", "log_fill",
           "max_loss", "ny_iso", "order_of", "parse_yes", "plain", "plain_parts", "render", "risk_verdict", "status_of",
           "ticket_json", "tool_input"]
