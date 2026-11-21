"""The upstream position sizing, plan mode, ported (S15). Source: tank/day-trader-skill/trader-budget/size.py at 3d0f4ef
(MIT; the NOTICE file), with upstream PR #49 (as is) and PR #2 (with the review's
change 6) applied. Weights mode is carried, unchanged, in traderos.risk.weights (S21, not wired in F1).

Brian's rule (11-19): code is rewritten only for a clear flaw or a clearly better method. The four limits, the
"smallest one wins" rule, whole shares rounded down, the entry it sizes at, every refusal and its wording are
upstream's, except these changes, each with its reason (ADR-009):

* **positive_number refuses NaN and infinity** (PR #2 with the review's change 6). A NaN max loss raised a
  traceback and kept the stale orders.json; Infinity raised OverflowError.
* **"drop_stale on every failure"** becomes: only the latest assessment can become a ticket, and every attempt,
  refused or failed, is recorded first (commands/decisions.py). There is no orders.json to delete in TraderOS; a
  newer attempt supersedes an older eligible one, so yesterday's (or ten minutes ago's) plan cannot be signed off.
* **Decimal money, exact prices** (spec 04.3; review of #10, items 1 and 3). Upstream rounded every price to the cent
  (half up) and counted in integer cents: entry 0.5049 / stop 0.4851 sized 5000 shares on a 0.01 risk, a real loss
  of $99 at the stop against a $50 limit, and the stop on the order moved to 0.49; a multiplier of 12.5 on a 0.01
  risk rounded 0.125 a contract to 0.12 and sized 416 contracts ($52.00). Now the prices are used exactly and the
  quantity is the floor of the exact quotient (`shares_for`), so the real loss at the plan's stop is never over the
  limit, and an order price is never rounded.
* **An order price off the tick is refused** (review of #10, item 1), typed (DataError, INVALID_INPUT): the entry,
  a limit price, the stop and the target must sit on the plan's `tick_size` if it states one, else on the U.S.
  equity rule (whole cents at $1.00 and above, at most four decimals below). Rounding it would change the order
  approved; refusing it lets the person write the price they mean. A quote is not an order price and is used as is.
* **The stricter basis** (review of #10, LOW b). Given the account (`account_total`, `held_value`), the cash row
  also keeps the buffer on the account's total and the position cap is measured on the account less what you hold,
  as the risk check measures them; upstream measured both on the budget only, so a sized quantity could fail its own
  check (3,413.44 settled: 8 sized, 85.4% of the account, blocked).
* **The contract multiplier** (S15: whole-contract sizing). One contract risks (entry - stop) x multiplier and costs
  entry x multiplier; with a multiplier of 1 every number is upstream's.
* **The cash row is settled cash less reservations** (S15, RC17). Upstream read portfolio "cash", which its own fills
  credited with a sale's unsettled proceeds.
* **No DEFAULTS** (spec 15.6): every limit comes from the person's policy (traderos.risk.policy); a missing one is
  MISSING_DATA. Demonstrated: a config of {"budget": 5000} was sized by a $50 max loss nobody set.
* **Typed refusals**: SizeError is a ValidationError; a short plan is OUTSIDE_CAPABILITY, a plan for another day is
  DATA_STALE, a blocked ticker NOT_AUTHORIZED, a missing limit MISSING_DATA.
* **F1 wording and no order file.** bracket_order is the order a paper ticket documents; plan_report no longer says
  "place_stock_order arguments" or hands over to trader-broker. main(), run_plan, write_orders, read_json,
  load_config and the clock helpers are replaced by the traderos CLI and the store; --budget (an override of the
  policy) is not offered, so the policy is the one source of each limit.

How many shares (upstream). Whole shares only:

    shares = floor of the smallest of
      max loss       max_loss_per_trade_usd / risk per share (entry minus stop)
      position cap   budget x max_position_pct / entry
      max order      max_order_usd / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than settled cash less reservations, if you stated an account, less the buffer
                     on the account's total)
    (the position cap is also no more than max_position_pct of your account less what you hold of it)

The entry it sizes at: a limit order's limit price; for a market order the plan's entry, or a fresh latest price
(under 15 minutes old) if higher, because a price that ran up since the plan means a bigger loss at the same stop.
Zero shares (the stop is too far away for your max loss) is a plain refusal, and so is a reward:risk under
min_reward_risk (the price ran toward the target since the plan), an order under min_order_usd, a plan with no date
or for another day, and a budget, max_loss_per_trade_usd, max_position_pct or max_order_usd that is 0, negative or
not a number (never "no limit").
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from traderos.contracts import DataError, ErrorCode, ValidationError
from traderos.risk.check import FRESH_MINUTES, load_prices, price_text, to_eastern
from traderos.risk.policy import exact, policy, positive_number
from traderos.risk.policy import number as _f

SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
PENNY = Decimal("0.01")
SUB_PENNY = Decimal("0.0001")                    # U.S. equities under $1.00 quote to four decimals (Reg NMS 612)


class SizeError(ValidationError):
    pass


# ---------------------------------------------------------------- prices

def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, time, fresh) for sym from a prices document, or (None, None, False). A price with no time is never
    fresh (the upstream rule, kept)."""
    prices, times = load_prices(doc)
    p = prices.get(sym)
    if p is None:
        return None, None, False
    t = times.get(sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def tick_of(price, tick_size=None):
    """The tick an order price must sit on: the plan's tick_size when it states one, else the U.S. equity rule,
    whole cents at $1.00 and above and at most four decimals below it."""
    if tick_size is not None:
        return tick_size
    return PENNY if price >= 1 else SUB_PENNY


def on_tick(name, price, tick_size=None):
    """Refuse an order price off its tick. Upstream rounded every price to the cent before measuring the risk, so entry
    0.5049 and stop 0.4851 became 0.50 and 0.49: half the risk, twice the shares, and the stop on the ticket moved
    (review item 1). An order price is never rounded: it is refused, and the person writes the price they mean."""
    tick = tick_of(price, tick_size)
    if price % tick != 0:
        rule = ("its tick_size %s" % price_text(tick) if tick_size is not None else
                "whole cents at $1.00 and above" if price >= 1 else "at most four decimals below $1.00")
        raise DataError("the %s %s is off the tick (%s): an order price is never rounded; write the price you mean%s"
                        % (name, format(price.normalize(), "f"), rule,
                           "" if tick_size is not None else ", or state the plan's tick_size"),
                        code=ErrorCode.INVALID_INPUT)


def shares_for(usd, per_share):
    """Whole shares that fit in usd at per_share: the exact quotient, floored. Upstream counted in integer cents, which
    rounded a per-share amount finer than a cent (a sub-dollar price, a fractional multiplier) and could size past
    the limit (review items 1 and 3); Decimal division is exact here, so nothing is rounded but the result."""
    usd, per_share = Decimal(usd), Decimal(per_share)
    if per_share <= 0 or usd <= 0:
        return 0
    return int(usd // per_share)


def _price(x):
    """358.32 -> "358.32", 0.4851 -> "0.4851": prices are written as strings, exactly."""
    return price_text(x)


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13"."""
    x = Decimal(x).quantize(PENNY, rounding=ROUND_HALF_EVEN)
    if x == x.to_integral_value():
        return str(int(x))
    return format(x, "f")


def money(x):
    x = Decimal(x)
    if x == x.to_integral_value() or abs(x - x.to_integral_value()) < Decimal("0.005"):
        return "$" + "{:,.0f}".format(x)
    return "$" + "{:,.2f}".format(x)


# ---------------------------------------------------------------- plan mode

@exact
def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None, account_total=None,
               held_value=None):
    """Pure function: one plan + your policy -> the sizing (a dict). qty may be 0.

    limit          the limit price for a limit order (None = market)
    price          a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash           settled cash less reservations, from your account statement, if you stated one
    today          New York date (YYYY-MM-DD); a plan for another day is refused
    account_total  with held_value: your account's total value (settled, unsettled and positions) and what you
                   already hold of this symbol. The check measures the cash buffer and the position cap against the
                   account; given them, sizing uses the stricter of that and the upstream budget, so a sized quantity
                   never fails its own check on either (review LOW b)"""
    c = policy(cfg)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side,
                        code=ErrorCode.OUTSIDE_CAPABILITY)
    if not plan.get("date"):
        raise SizeError("the plan has no date: give it the New York trading day it is for (\"date\": "
                        "\"2026-10-19\")")
    if today and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in New York: make a new plan"
                        % (plan["date"], today), code=ErrorCode.DATA_STALE)
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s; a bracket order needs entry, stop and target" % name)
    mult = _f(plan.get("multiplier", 1))
    if mult is None or mult <= 0:
        raise SizeError("the multiplier must be a positive number, got %r" % plan.get("multiplier"))
    tick = None
    if plan.get("tick_size") is not None:
        tick = _f(plan.get("tick_size"))
        if tick is None or tick <= 0:
            raise SizeError("the tick_size must be a positive number, got %r" % plan.get("tick_size"))
    lim = None
    if limit is not None:
        lim = _f(limit)
        if lim is None or lim <= 0:
            raise SizeError("the limit price must be a positive number, got %r" % (limit,))
    for name, v in (("entry", entry_plan), ("limit price", lim), ("stop", stop), ("target", target)):
        if v is not None:
            on_tick(name, v, tick)
    blocked = {str(b).upper() for b in c["blocked"]}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym, code=ErrorCode.NOT_AUTHORIZED)

    budget = positive_number("budget", budget if budget is not None else c["budget"])
    buf_pct = c["cash_buffer_pct"]

    quote = _f(price) if price is not None else None
    if lim is not None:
        ref, ref_from = lim, "limit"
    elif quote is not None and quote > entry_plan:     # the quote exactly: it is not an order price (review item 1)
        ref, ref_from = quote, "price"
    else:
        ref, ref_from = entry_plan, "plan"
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan" % (_price(target), _price(ref)))
    risk = ref - stop                                  # exact: the upstream integer cents rounded it (review item 1)
    reward = target - ref
    unit = ("" if mult == 1 else " x %s" % _num(mult))

    cash_buffer = (budget * buf_pct / 100).quantize(PENNY, rounding=ROUND_HALF_EVEN)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - Decimal("0.005"):
        available = cash - cash_buffer
        cash_note = "settled cash less reservations %s minus the %s buffer" % (money(cash), money(cash_buffer))
    if cash is not None and account_total is not None:
        acct_buffer = account_total * buf_pct / 100
        if cash - acct_buffer < available - Decimal("0.005"):
            available = cash - acct_buffer
            cash_note = ("settled cash less reservations %s minus the %s buffer on your account's %s"
                         % (money(cash), money(acct_buffer), money(account_total)))
    available = max(available.quantize(PENNY, rounding=ROUND_HALF_EVEN), Decimal(0))

    limits = []
    max_loss = c["max_loss_per_trade_usd"]
    limits.append({"name": "max loss", "usd": max_loss, "per": risk * mult, "shares": shares_for(max_loss, risk * mult),
                   "how": "%s max loss / %s risk a share%s" % (money(max_loss), _price(risk), unit)})
    cap_pct = c["max_position_pct"]
    cap = (budget * cap_pct / 100).quantize(PENNY, rounding=ROUND_HALF_EVEN)
    cap_how = "%s%% of %s = %s / %s%s" % (_num(cap_pct), money(budget), money(cap), _price(ref), unit)
    if account_total is not None:
        room = account_total * cap_pct / 100 - (held_value or 0)
        if room < cap:
            cap = max(room, Decimal(0))
            cap_how = ("%s%% of your account's %s%s = %s / %s%s"
                       % (_num(cap_pct), money(account_total),
                          (" less the %s you hold" % money(held_value)) if held_value else "", money(cap),
                          _price(ref), unit))
    limits.append({"name": "position cap", "usd": cap, "per": ref * mult, "shares": shares_for(cap, ref * mult),
                   "how": cap_how})
    max_order = c["max_order_usd"]
    limits.append({"name": "max order", "usd": max_order, "per": ref * mult,
                   "shares": shares_for(max_order, ref * mult),
                   "how": "%s / %s%s" % (money(max_order), _price(ref), unit)})
    limits.append({"name": "cash", "usd": available, "per": ref * mult, "shares": shares_for(available, ref * mult),
                   "how": "%s = %s / %s%s" % (cash_note, money(available), _price(ref), unit)})

    qty = max(0, min(lim["shares"] for lim in limits))
    bound = [lim["name"] for lim in limits if lim["shares"] == qty]
    for lim in limits:
        lim["binds"] = lim["name"] == bound[0]
    cost = (qty * ref * mult).quantize(PENNY, rounding=ROUND_HALF_EVEN)
    loss = (qty * risk * mult).quantize(PENNY, rounding=ROUND_HALF_EVEN)
    refusal = None
    min_rr = c["min_reward_risk"]
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks $%s (entry %s minus stop %s%s), more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop."
                       % (_price(risk * mult), _price(ref), _price(stop), unit, money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref * mult), bound[0], limits[[lim["name"] for lim in limits]
                                                                   .index(bound[0])]["how"]))
    elif min_rr and reward < min_rr * risk:
        refusal = ("reward:risk %s (target %s, entry %s, stop %s) is under your minimum %s, so the risk check "
                   "would block it. No order. Skip it, or make a new plan."
                   % ((reward / risk).quantize(PENNY, rounding=ROUND_HALF_EVEN), _price(target),
                      _price(ref), _price(stop), _num(min_rr)))
    elif cost < c["min_order_usd"] - Decimal("0.005"):
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_usd"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": entry_plan,
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": (reward / risk).quantize(PENNY, rounding=ROUND_HALF_EVEN),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_usd": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "max_loss": loss, "flat_by": plan.get("flat_by") or c.get("flat_by"), "multiplier": mult,
        "tick_size": tick,
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
    }


def bracket_order(s):
    """The one proposed order (the fields a paper ticket documents, as strings) plus why and plan."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["symbol"], "side": "buy", "qty": str(int(s["qty"])), "type": s["order_type"],
         "time_in_force": "day"}
    if s["order_type"] == "limit":
        o["limit_price"] = _price(s["entry"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    why = ("%s on %s: %d shares, bound by your %s (%d x %s = %s at the stop)"
           % (setup, s["symbol"], s["qty"], {"max loss": "%s max loss per trade" % money(s["max_loss_per_trade_usd"]),
                                             "position cap": "position cap", "max order": "max order",
                                             "cash": "cash after the buffer"}[s["bound_by"]],
              s["qty"], _price(s["risk_per_share"]), money(s["max_loss"])))
    o.update({
        "order_class": "bracket",
        "take_profit_limit_price": _price(s["target"]),
        "stop_loss_stop_price": _price(s["stop"]),
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"], "setup": s["setup"],
                 "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    })
    if s["multiplier"] != 1:
        o["plan"]["multiplier"] = s["multiplier"]
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %s."
                 % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for lim in s["limits"]:
        lines.append("  %-13s %-58s %3d shares%s" % (lim["name"], lim["how"], lim["shares"],
                                                      "  <- binds" if lim["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s. Max loss at the stop: %d x %s = %s."
                 % (s["qty"], s["symbol"], money(s["cost"]), _price(s["entry"]), s["qty"],
                    _price(s["risk_per_share"]), money(s["max_loss"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _price(s["stop"]))
    lines.append("")
    lines.append("The order it would be (a hypothetical document; nothing has been or can be placed):")
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    lines.append("  buy %s %s, %s, day, take-profit %s, stop-loss %s"
                 % (order["qty"], order["symbol"], how, order["take_profit_limit_price"],
                    order["stop_loss_stop_price"]))
    if s["flat_by"]:
        lines.append("Flat by %s New York: plan to be out of it by then." % s["flat_by"])
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def price_notes(doc, sym, entry, now_utc, limit=None):
    """The upstream run_plan notes on the latest price, and the fresh price to size at (or None). Returns (price,
    notes)."""
    notes, price = [], None
    if doc is None or limit is not None or not sym:
        return None, notes
    p, t, fresh = fresh_price(doc, sym, now_utc)
    if p is not None and fresh:
        price = p
        if p > (_f(entry) or 0):
            notes.append("%s last %s (as of %s New York) is above the plan's entry %s, so the risk "
                         "is bigger: sized at %s." % (sym, _price(p), to_eastern(t).strftime("%H:%M"),
                                                      _price(entry), _price(p)))
    elif p is not None:
        notes.append("%s in your prices file is %s: not used (older than %d minutes). Sized at the plan's entry."
                     % (sym, ("from " + to_eastern(t).strftime("%Y-%m-%d %H:%M") + " New York")
                        if t else "undated", FRESH_MINUTES))
    return price, notes


__all__ = ["SETUP_WORDS", "SizeError", "bracket_order", "fresh_price", "on_tick", "plan_report", "price_notes",
           "shares_for", "size_trade"]
