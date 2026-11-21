"""The upstream pre-trade risk check, ported (S16). Source: tank/day-trader-skill/trader-risk/check.py at 3d0f4ef (MIT;
the NOTICE file), with the upstream PRs applied in the approved review's order:
#49 (as is), #2 (with changes), #21 (with changes), #48 (as is), #55 (with changes; its JSON "today" hunk merged
by hand next to #2's unresolved_approvals line).

Brian's rule (11-19): code is rewritten only for a clear flaw or a clearly better method. Every rule, threshold,
reason and its wording here is the upstream or the PRs', except these changes, each with its reason (ADR-009):

* **An unresolved approval BLOCKs new entries** (review change to #2, 4; P4). Counting it only hid it: the order
  may be at the broker. In F1 an "approval" is a signed-off paper ticket with no outcome in your account statement.
* **A stale price on a market entry BLOCKs, even with a plan** (review change to #2, 4; P5). The plan's entry cannot
  show whether the price already ran through the stop (#48 needs a fresh price to see it).
* **Unknown recorded risk BLOCKs new entries whatever the daily limit** (review change to #55, 1; spec 05.3). Exits
  are still allowed (change 2).
* **No account statement, or no log of today's orders, is UNKNOWN exposure** (P7; spec 15.2, 12.2 C). Upstream assumed a
  fresh account funded with the budget, and nothing placed today: a guess counted as zero.
* **No stop is NO_PROTECTION whatever require_stop says** (P10). `require_stop: false` let a stopless entry pass and
  left its loss out of the daily total.
* **Non-finite numbers are refused** (P8, P9): `qty: "NaN"` passed and printed "BUY NaN shares"; a NaN cash turned
  the cash checks off. `_f` now returns a finite Decimal or None.
* **Decimal money** (spec 04.3); floats are gone.
* **Prices are measured exactly** (review of #10, item 1). Upstream rounded the entry to the cent and the risk a share
  to integer cents, so entry 0.5049 / stop 0.4851 measured 0.01 a share instead of 0.0198 and passed twice the loss;
  the risk a share, the loss at the stop and reward:risk now use the exact prices, and only the money shown is
  rounded. A price is printed as written (`price_text`), never as "%.2f".
* **Settled cash in the batch** (S15 / RC17): a sell's proceeds are not settled, so they no longer fund a later buy
  in the same batch (the upstream line 776, and its test at test_risk.py:547-555). They are unsettled cash: still in the
  account's total, so a sale does not shrink the buffer and the cap of a later buy (review of #10, item 7; the
  first port took them out of the total, and a 10-share sale let a buy through the buffer). A stated
  `unsettled_cash` is counted the same way.
* **A market entry with no usable price BLOCKs** (review of #10, item 4), as a stale one does: no prices file, the
  symbol not in it, or a value that is not a positive number under 10^15 are MISSING_DATA. Upstream measured from the
  plan's entry, which cannot show whether the price already ran through the stop.
* **A holding with no price and no stated cost is unknown, never $0** (review of #10, second round). Upstream valued it
  at the cost basis of no lots, $0, so with a limit order (no prices file needed) 10 contracts of 100 held passed a
  75% cap at a true 85.7%. For an entry it is UNKNOWN_EXPOSURE; and when you hold the plan's own symbol, an entry
  needs its price even for a limit order (MISSING_DATA). A holding with lots is still valued at their cost.
* **A holding is valued with its multiplier** (review of #10, item 2): a position may state `multiplier` (default
  1), and is worth shares x price x multiplier. A plan whose multiplier differs from the holding's stated one, or a
  plan multiplier other than 1 on a holding that states none, is UNKNOWN_EXPOSURE: 10 contracts of 100 were valued
  at a hundredth of their size and a buy to 85.7% of the account passed a 75% cap.
* **One exchange calendar** (traderos.clock XNYS), not a 2026-2027 holiday table in each file; a day outside the
  calendar is a CALENDAR_UNKNOWN refusal (P13), where upstream only warned and let the entry through.
* **An untimed price is not fresh** (P16). The upstream `price_times=None` meant every price counted as fresh.
* **Timezone-aware UTC instants** (contracts v1, QT01). The upstream New York conversion rule is kept as written
  upstream.
* **The contract multiplier** (S15 whole-contract sizing) scales an order's value and its loss at the stop.
* **F1 wording.** F1 has no broker (spec 00.2, 15): the LIVE-account warning, the "paper" flag, "check get_orders at
  the broker" and the "trader-broker"/"ticket.py" hand-offs are gone; a fix points at your account statement.
* **Removed: main(), read_json, parse_now and the risk.json writer.** The `traderos` CLI reads the files strictly
  and the store keeps the records (commands/decisions.py).
* **Not done here, still gate P (ADR-012):** #2's change 5 (bind the fingerprint to the broker account; paper/live
  is now fixed by the adapter, which reaches only the paper endpoint) and #21's change 2 (revalidate immediately
  before submission: `ticket place` checks the verdict has not expired, it does not re-run this check).

A PASS only means the plan is allowed to become a paper ticket. It is not a yes, and nothing is placed: a ticket
is sent (to an Alpaca paper account only, ADR-012) after the person's explicit yes for it.

What blocks an order (every order), the upstream list:

  format        missing symbol or side, both or neither of qty / notional, notional on anything but a market day
                order, a limit order without a limit price, unknown type; a bracket that is not whole shares, not
                day / gtc, or misses a leg; a number that is not a positive finite number
  asset         options (OCC symbols) unless "allow_options": true; crypto and anything not a stock or ETF
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top): blocked, size, position, cash (settled cash less reservations), duplicate, daily
limit, stop (none: NO_PROTECTION; wrong side), loss, reward:risk, daily loss, trades, window (New York time; the
market closed; an early close moves a later cutoff up), pdt, price (no fresh price for a market entry, or none at
all), exposure (no account statement, no log of today's orders, a signed-off ticket with no outcome, a holding
whose multiplier is not the plan's).

Entries and exits: an EXIT only reduces what you hold, and getting out must always be possible: it is checked for
format, asset and short sales only; every other rule turns into a warning for it.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import re
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from traderos.clock import CLOSE, OPEN, XNYS, early_closes
from traderos.clock import EARLY_CLOSE as EARLY
from traderos.clock import eastern_to_utc as _et_to_utc
from traderos.contracts import DataError, ValidationError
from traderos.risk.policy import exact
from traderos.risk.policy import number as _f
from traderos.risk.policy import policy as read_policy

UTC = datetime.UTC
# The order's fields (Alpaca's place_stock_order names, as upstream wrote them). In F1 they are the vocabulary of a
# paper ticket document: nothing in TraderOS sends them anywhere.
ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
PRICE_FIELDS = ("qty", "notional", "limit_price", "stop_price", "take_profit_limit_price",
                "stop_loss_stop_price", "stop_loss_limit_price")
TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
TIFS = {"day", "gtc", "opg", "cls", "ioc", "fok"}
OCC = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
TICKER = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = Decimal("0.005")
PENNY = Decimal("0.01")
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the stop price"}
_CAL = XNYS()


class RiskError(ValidationError):
    pass


# ---------------------------------------------------------------- time (New York)

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.astimezone(UTC).replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    """An aware instant -> the New York wall clock (naive)."""
    return utc.astimezone(UTC).replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    """A New York wall clock (naive) -> an aware UTC instant (traderos.clock's rule, the same as upstream's)."""
    return _et_to_utc(local.date(), local.time())


def utc_iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def aware(now):
    if now is None:
        return datetime.datetime.now(UTC).replace(microsecond=0)
    if not isinstance(now, datetime.datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise RiskError("now must be a timezone-aware instant, got %r" % (now,))
    return now.astimezone(UTC)


def trading_day(d):
    """(is_trading_day, reason) from the XNYS calendar. Outside its coverage: DataError(CALENDAR_UNKNOWN)."""
    if _CAL.is_session(d):
        return True, ""
    if d.weekday() >= 5:
        return False, "weekend"
    return False, "NYSE holiday: " + (_CAL.holiday_name(d) or "closed")


def hhmm(value, key):
    """"09:45" -> time(9, 45); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be a New York time like \"09:45\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def early_cutoff(day, t, close):
    """Same rule as trader-entry/entry.py's early_cutoff: a cutoff after the early close
    moves up with it (15:30 -> 12:30 before a 1:00 p.m. close); one already before the
    early close stays as is."""
    at = datetime.datetime.combine(day, t)
    close_dt = datetime.datetime.combine(day, close)
    if at < close_dt:
        return t
    moved = at - (datetime.datetime.combine(day, CLOSE) - close_dt)
    return moved.time() if moved.time() > OPEN else close


def window_check(now_et, c):
    """Reasons an entry cannot be placed now (New York time): closed market or outside the
    regular session always apply; no_entries_before/after only when configured. A day the
    calendar does not cover is CALENDAR_UNKNOWN: refused, never guessed."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    warns = []
    d, t = now_et.date(), now_et.time().replace(second=0, microsecond=0)
    stamp = now_et.strftime("%a %Y-%m-%d %H:%M")
    try:
        ok, why = trading_day(d)
        early = _CAL.is_early_close(d)
    except DataError:
        return ["window: the exchange calendar covers %s to %s, not %s: the session is unknown"
                % (_CAL.coverage[0], _CAL.coverage[1], d.isoformat())], warns
    if not ok:
        return ["window: the market is closed today (%s, %s New York)" % (why, stamp)], warns
    close = EARLY if early else CLOSE
    if t < OPEN or now_et.time() >= close:
        return ["window: the market is closed (%s New York; the session is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), close.strftime("%H:%M"))], warns
    if close == EARLY and after is not None:
        adjusted = early_cutoff(d, after, close)
        if adjusted != after:
            warns.append("early close at 1:00 p.m. today (%s): no new entries after %s"
                         % (early_closes(d.year).get(d, "early close"), adjusted.strftime("%H:%M")))
        after = adjusted
    if before is not None and t < before:
        return ["window: no new entries before %s New York (it is %s)" % (before.strftime("%H:%M"),
                                                                          t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s New York (it is %s)" % (after.strftime("%H:%M"),
                                                                         t.strftime("%H:%M"))], warns
    return [], warns


def business_days(today, n=5):
    """The last n NYSE trading days up to and including today, newest first."""
    out, d = [], today
    for _ in range(40):
        if trading_day(d)[0]:
            out.append(d.isoformat())
            if len(out) == n:
                break
        d -= datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- helpers

def fingerprint(order):
    """The exact order: its fields plus the plan's entry, stop and target, so an edited
    plan invalidates a verdict. One copy in TraderOS (upstream kept two byte-identical ones,
    one per skill folder). "358.32", 358.32 and 358.320 are the same level."""
    core = {k: order[k] for k in ALPACA_FIELDS if k in order and order[k] is not None}
    plan = order.get("plan") if isinstance(order.get("plan"), dict) else {}
    for k in ("entry", "stop", "target"):
        if plan.get(k) is not None:
            d = _f(plan[k])
            core["plan." + k] = format(d.normalize(), "f") if d is not None else str(plan[k])
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def money(x):
    return "$" + "{:,.2f}".format(Decimal(x)).replace(".00", "")


def price_text(x):
    """A price as written: exact, never rounded, with at least two decimals ("364.1" -> "364.10", "0.4851" stays).
    Upstream printed "%.2f", which showed 0.4851 as 0.49: an order price is never rounded (ADR-009, review item 1)."""
    d = Decimal(x)
    if d == d.quantize(PENNY):
        return format(d.quantize(PENNY), "f")
    return format(d.normalize(), "f")


_p = price_text


def _pct(x):
    x = Decimal(x)
    return str(int(x)) if x == x.to_integral_value() else format(x.quantize(PENNY), "f")


def _num(x):
    x = Decimal(x)
    if x == x.to_integral_value():
        return str(int(x))
    return format(x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN), "f").rstrip("0")


def _age_words(age):
    """A price's age vs "fresh" (see fresh()): too old, or stamped ahead of now_utc, which
    almost always means the wrong --now or a time-zone mismatch, not a time machine."""
    if age is None:
        return "has no timestamp"
    if age < -60:
        return "is stamped %.0f minutes in the future (check the clock or time zone)" % (-age / 60.0)
    return "is older than %d minutes" % FRESH_MINUTES


def account(cfg, portfolio, prices):
    """Starting account: settled cash less reservations, and position values. No portfolio
    (None) = the account is UNKNOWN, never a fresh account funded with the budget."""
    warnings = []
    if portfolio is None:
        return None, warnings
    if not isinstance(portfolio, dict):
        raise RiskError("the account statement is not a JSON object")
    settled, reserved = _f(portfolio.get("settled_cash")), _f(portfolio.get("reserved_cash"))
    if settled is None or reserved is None or settled < 0 or reserved < 0:
        raise RiskError("the account needs settled_cash and reserved_cash as finite numbers of at least 0, got "
                        "%r and %r" % (portfolio.get("settled_cash"), portfolio.get("reserved_cash")))
    cash = settled - reserved
    unsettled = Decimal(0)
    if portfolio.get("unsettled_cash") is not None:
        unsettled = _f(portfolio.get("unsettled_cash"))
        if unsettled is None or unsettled < 0:
            raise RiskError("unsettled_cash must be a finite number of at least 0, got %r"
                            % (portfolio.get("unsettled_cash"),))
    shares, values, mults, unknown = {}, {}, {}, set()
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        m = None
        if p.get("multiplier") is not None:
            m = _f(p.get("multiplier"))
            if m is None or m <= 0:
                raise RiskError("%s: multiplier must be a positive number, got %r" % (t, p.get("multiplier")))
            if t in mults and mults[t] != m:
                raise RiskError("%s is stated twice with different multipliers" % t)
        mults[t] = mults.get(t, m)
        raw = p.get("shares", p.get("qty"))
        n = _f(raw)
        if n is None and raw is None:
            lots = [_f(lot.get("shares")) for lot in p.get("lots", [])]
            if any(x is None for x in lots):
                raise RiskError("a lot of %s has no finite number of shares" % t)
            n = sum(lots, Decimal(0))
        if n is None:
            raise RiskError("%s: shares must be a finite number, got %r" % (t, raw))
        shares[t] = shares.get(t, Decimal(0)) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t] * (mults[t] or 1)
        else:
            costs = [_f(lot.get("cost")) for lot in p.get("lots", [])]
            if any(x is None for x in costs):
                raise RiskError("a lot of %s has no finite cost" % t)
            basis = sum(costs, Decimal(0))
            values[t] = basis
            if costs:
                warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
            elif shares[t]:
                # Upstream valued it at a cost basis of $0: no price and no stated cost is an unknown value, never zero
                # (review of #10, second round; ADR-009)
                unknown.add(t)
                warnings.append("no price for %s and no stated cost: its value is unknown" % t)
    return {"cash": cash, "unsettled": unsettled, "shares": shares, "values": values, "mults": mults,
            "unknown": sorted(unknown)}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """The log records the kind the check gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


def unresolved_approvals(state, today):
    """Approvals recorded today with no later placed, denied or failed record for the
    same fingerprint: the order may be at the broker with no outcome recorded.
    They count as placed until an outcome is recorded."""
    recs = (state or {}).get("orders", [])
    out = []
    for i, rec in enumerate(recs):
        if rec.get("status") != "approved" or rec_day(rec) != today:
            continue
        fp = rec.get("fingerprint")
        if any(r.get("fingerprint") == fp and rec_day(r) == today
               and r.get("status") in ("placed", "denied", "failed") for r in recs[i + 1:]):
            continue
        out.append(rec)
    return out


def day_trades(state, today):
    """Day trades in the last 5 business days: a (day, symbol) with both a buy
    and a sell placed or filled that day. An entry placed today counts too: it
    is meant to be flat by the close. Returns (count, sorted list of "day SYM")."""
    days = set(business_days(datetime.date.fromisoformat(today)))
    sides = {}
    for rec in (state or {}).get("orders", []):
        if rec.get("status") not in ("placed", "filled"):
            continue
        d = rec_day(rec)
        if d not in days:
            continue
        key = (d, str(rec.get("symbol", "")).upper())
        sides.setdefault(key, set()).add(str(rec.get("side", "")).lower())
        if d == today and rec.get("status") == "placed" and rec_is_entry(rec):
            sides[key].add("pending")
    hits = sorted("%s %s" % k for k, s in sides.items()
                  if ("buy" in s and "sell" in s) or (k[0] == today and "pending" in s))
    return len(hits), hits


def exposure(portfolio, state, today):
    """What is known about today's exposure: (reasons an entry is blocked as UNKNOWN exposure, the unresolved
    approvals, the entries placed today with an unknown max loss, the known loss at risk, warnings). The reasons
    are the review's changes: an unresolved approval BLOCKs (#2, 4), an unknown loss BLOCKs (#55, 1), and a
    missing account statement or log is unknown (P7)."""
    reasons, warns = [], []
    if portfolio is None:
        reasons.append("exposure: no account statement: your cash and positions are unknown")
    if state is None:
        reasons.append("exposure: no log of today's orders: what you already placed today is unknown")
    pending = unresolved_approvals(state, today)
    for r in pending:
        what = r.get("plain") or "%s %s" % (r.get("side"), r.get("symbol"))
        warns.append("an approval for %s at %s has no result recorded" % (what, r.get("at")))
        reasons.append("exposure: existing exposure unknown: the approval for %s at %s has no outcome in your "
                       "account statement; record whether it was placed before a new entry" % (what, r.get("at")))
    entries_today = [r for r in placed_today(state, today) + pending if rec_is_entry(r)]
    loss_today, unknown = Decimal(0), []
    for r in entries_today:
        raw = r.get("max_loss")
        ml = _f(raw)
        if ml is None or ml < 0:
            label = "%s %s" % (r.get("side"), r.get("symbol"))
            unknown.append(label)
            warns.append("an entry placed today (%s) has no valid max loss in the log; total daily "
                         "risk is unknown" % label)
        else:
            loss_today += ml
    return reasons, pending, unknown, loss_today, warns


# ---------------------------------------------------------------- the check

@exact
def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    cfg          the person's policy (traderos.risk.policy: every limit stated)
    portfolio    {"settled_cash", "reserved_cash", "positions"}; None = UNKNOWN exposure
    state        {"orders": [the log of what was approved, placed, filled...]}; None = UNKNOWN exposure
    now          an aware instant (default: the real clock); its New York date is "today"
    price_times  {SYM: aware time of that price}; a price with no time is never fresh"""
    c = read_policy(cfg)
    prices = {str(k).upper(): _f(v) for k, v in (prices or {}).items() if _f(v) is not None and _f(v) > 0}
    price_times = price_times or {}
    now_utc = aware(now)
    now_et = to_eastern(now_utc)
    today = today or now_et.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c["blocked"]}
    unknown_exposure, pending, unknown_losses, loss_today, exp_warn = exposure(portfolio, state, today)
    top_warn.extend(exp_warn)
    done_today = placed_today(state, today) + pending
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    placed_keys = {(str(r.get("symbol", "")).upper(), r.get("side")) for r in done_today}
    win_reasons, win_warns = window_check(now_et, c)
    pdt_n, pdt_list = 0, []
    if c["pdt_guard"]:
        with contextlib.suppress(DataError):     # the window reason already refuses: the calendar does not cover today
            pdt_n, pdt_list = day_trades(state, today)
    seen = set()
    results = []
    batch_entries, batch_loss = 0, Decimal(0)
    top_warn.extend(win_warns)

    # How long each verdict stays good: 15 minutes everywhere, and for an entry also no
    # later than the (early-close-adjusted) no_entries_after cutoff or the close.
    fresh_until = now_utc + datetime.timedelta(minutes=FRESH_MINUTES)
    d = now_et.date()
    try:
        day_close = EARLY if _CAL.is_early_close(d) else CLOSE
    except DataError:
        day_close = CLOSE
    entry_until = eastern_to_utc(datetime.datetime.combine(d, day_close))
    after_t = hhmm(c.get("no_entries_after"), "no_entries_after")
    if after_t is not None:
        cutoff_utc = eastern_to_utc(datetime.datetime.combine(d, early_cutoff(d, after_t, day_close)))
        entry_until = min(entry_until, cutoff_utc)
    entry_until = min(entry_until, fresh_until)

    def fresh(sym):
        if sym not in prices:
            return None
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty, notional = o.get("qty"), o.get("notional")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        oc = str(o.get("order_class") or "").strip().lower()
        legs = o.get("take_profit_limit_price") is not None or o.get("stop_loss_stop_price") is not None
        bracket = oc == "bracket" or (not oc and legs)
        value = None

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if (qty is None) == (notional is None):
            reasons.append("format: give exactly one of qty or notional")
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not isinstance(o.get(k), str):
                    warns.append("write %s as a string (\"%s\")" % (k, o.get(k)))
        mult = Decimal(1)
        if plan.get("multiplier") is not None:
            mult = _f(plan.get("multiplier"))
            if mult is None or mult <= 0:
                reasons.append("format: multiplier must be a positive number, got %r" % plan.get("multiplier"))
                mult = Decimal(1)
        if typ not in TYPES:
            reasons.append("format: unknown order type %r" % typ)
        if tif not in TIFS:
            reasons.append("format: unknown time_in_force %r" % tif)
        if notional is not None and (typ != "market" or tif != "day"):
            reasons.append("format: a notional (dollar) order must be a market order with time_in_force day")
        if typ in ("limit", "stop_limit") and o.get("limit_price") is None:
            reasons.append("format: a %s order needs a limit_price" % typ)
        if typ in ("stop", "stop_limit") and o.get("stop_price") is None:
            reasons.append("format: a %s order needs a stop_price" % typ)
        if o.get("extended_hours") and not (typ == "limit" and tif in ("day", "gtc")):
            reasons.append("format: extended hours needs a limit order with time_in_force day or gtc")
        if o.get("stop_loss_limit_price") is not None and o.get("stop_loss_stop_price") is None:
            reasons.append("format: stop_loss_limit_price needs a stop_loss_stop_price")
        if bracket:
            if notional is not None:
                reasons.append("format: a bracket order needs a whole-share qty, not a notional amount")
            elif _f(qty) is not None and _f(qty) != _f(qty).to_integral_value():
                reasons.append("format: a bracket order takes whole shares only, got qty %r" % qty)
            if tif not in ("day", "gtc"):
                reasons.append("format: a bracket order needs time_in_force day or gtc, got %r" % tif)
            if o.get("take_profit_limit_price") is None or o.get("stop_loss_stop_price") is None:
                reasons.append("format: a bracket order needs both take_profit_limit_price and "
                               "stop_loss_stop_price")
        elif oc or o.get("advanced_instructions"):
            warns.append("advanced order (%s): check every leg before you sign it off"
                         % (oc or "advanced_instructions"))

        # asset
        if sym and OCC.match(sym.replace(" ", "")):
            if not c.get("allow_options"):
                reasons.append("asset: %s is an option contract; options are off "
                               "(\"allow_options\": false)" % sym)
            else:
                warns.append("option order: options can lose 100% of what you pay")
        elif sym and ("/" in sym or not TICKER.match(sym)):
            reasons.append("asset: %s does not look like a US stock or ETF ticker; "
                           "this pack trades stocks and ETFs only" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("limit", "stop_limit"):
            ref, ref_from = _f(o.get("limit_price")), "limit"
        elif typ == "stop":
            ref, ref_from = _f(o.get("stop_price")), "stop price"
        if ref is not None and ref <= 0:
            ref = None
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p and p > 0]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                if live and ref_from == "price" and plan_entry and live > plan_entry + CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
        price_age = None
        if live is None and sym in prices:
            t = price_times.get(sym)
            price_age = (now_utc - t).total_seconds() if t else None
        q = _f(qty)
        if notional is not None and _f(notional):
            value = _f(notional)
        elif q:  # noqa: SIM102 (the upstream structure)
            if ref:
                value = q * ref * mult

        # entry or exit
        held = acct["shares"].get(sym, Decimal(0)) if acct else Decimal(0)
        held_value = acct["values"].get(sym, Decimal(0)) if acct else Decimal(0)
        exiting = False
        if side == "sell" and held > 0:
            if q is not None:
                exiting = q <= held
            elif value is not None:
                exiting = value <= held_value + CENT
        elif side == "buy" and held < 0 and q is not None:
            exiting = q <= -held
        kind = "exit" if exiting else "entry"
        if plan_entry and live is None and typ == "market":
            if sym in prices:
                later.append("price: the %s price in your prices file %s: a market entry needs a fresh one (the "
                             "plan's entry cannot show whether the price already ran through the stop)"
                             % (sym, _age_words(price_age)))
            else:
                later.append("price: no price for %s: a market entry needs a fresh one (the plan's entry cannot show "
                             "whether the price already ran through the stop); give a prices file, or make it a "
                             "limit order" % sym)
        if kind == "entry" and value is None and q:
            if plan_entry is None and typ == "market" and sym in prices:
                later.append("price: the %s price in your prices file %s: no fresh price to size a "
                             "market entry" % (sym, _age_words(price_age)))
            else:
                later.append("price: no price for %s to size a %s-share order; add a fresh one to "
                             "your prices file" % (sym, qty))

        # a holding's multiplier must be the plan's: otherwise its value, and so the account, is unknown
        if acct is not None and sym in acct["shares"]:
            stated = acct["mults"].get(sym)
            if (mult != 1 and stated != mult) or (stated is not None and stated != mult):
                later.append("exposure: you hold %s, and this plan's multiplier is %s but your account statement %s: "
                             "the holding's value is unknown; state its multiplier"
                             % (sym, _num(mult), "states %s" % _num(stated) if stated is not None
                                else "states none for it"))

        # a holding with no price and no stated cost: its value, and so the account's, is unknown (upstream: $0). And
        # the plan's own symbol, held, needs its price even for a limit order: the position cap and the buffer are
        # measured on what you hold (review of #10, second round; ADR-009)
        if acct is not None:
            for t in acct["unknown"]:
                later.append("exposure: you hold %s with no price and no stated cost, so its value and your account's "
                             "are unknown; add its price to your prices file, or its lots' cost to your account "
                             "statement" % t)
            if acct["shares"].get(sym) and sym not in prices and typ != "market":
                later.append("price: no price for %s: you hold it, so its price is needed to measure your position, "
                             "even for a limit order; add it to your prices file" % sym)

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < c["min_order_usd"] - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_usd"])))
            if c["max_order_usd"] and value > c["max_order_usd"] + CENT:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(c["max_order_usd"])))

        # account effects, simulated in order (settled cash only)
        if acct is not None:
            cash = acct["cash"]
            total = cash + acct["unsettled"] + sum(acct["values"].values(), Decimal(0))
            buffer_usd = total * c["cash_buffer_pct"] / 100
            if value is not None and side == "buy" and kind == "entry":
                after_cash = cash - value
                if after_cash < -CENT and not c.get("allow_margin"):
                    later.append("cash: needs %s but only %s settled cash; that would be margin (borrowed money)"
                                 % (money(value), money(cash)))
                elif after_cash < buffer_usd - CENT:
                    later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                                 % (money(max(after_cash, Decimal(0))), _pct(c["cash_buffer_pct"]),
                                    money(buffer_usd)))
                pos_after = held_value + value
                limit = total * c["max_position_pct"] / 100
                if total > 0 and pos_after > limit + CENT:
                    later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                                 % (sym, money(pos_after), 100 * pos_after / total, money(total),
                                    _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            if q is not None:
                over = q > held
            elif value is not None:
                over = value > held_value + CENT
            else:
                over = held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, add it to your account statement first" % (sym, _num(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window, pdt
        max_loss = risk_ps = rr = stop = target = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("stop_loss_stop_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("take_profit_limit_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the bracket stop %s differs from the plan's stop %s: the bracket's is the one "
                             "the order carries" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not in the order: give the order its stop-loss "
                             "leg" % _p(stop))
            if not stop:
                later.append("stop: no stop. Every entry needs one: without a stop its loss is not bounded "
                             "(a bracket stop_loss_stop_price, or the plan's stop)")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_ps = abs(ref - stop)            # exact: upstream rounded it to the cent (review item 1)
                    shares = q if q is not None else ((value or Decimal(0)) / (ref * mult))
                    max_loss = (shares * risk_ps * mult).quantize(PENNY, rounding=ROUND_HALF_EVEN)
                    cap = c["max_loss_per_trade_usd"]
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share%s = %s at the stop, over your %s max loss "
                                     "per trade" % (_num(shares), _p(risk_ps),
                                                    (" x %s" % _num(mult)) if mult != 1 else "", money(max_loss),
                                                    money(cap)))
                    min_rr = c["min_reward_risk"]
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = (abs(target - ref) / risk_ps).quantize(PENNY, rounding=ROUND_HALF_EVEN)
                            if min_rr and rr < min_rr:
                                later.append("reward:risk: %s (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            # A market order fills at the last price, not the plan's entry: when the fresh
            # price is already through the stop or the target, the trade is over before it
            # starts. The checks above compare against ref (the higher of plan and price),
            # so a fallen price hides behind the plan's entry. A stale price is not used:
            # live is None for it, as above (and the entry is blocked for it).
            if typ == "market" and live is not None:
                if stop and ((long and live <= stop + CENT) or (not long and live >= stop - CENT)):
                    later.append("stop: the last price %s is already at or %s the stop %s"
                                 % (_p(live), "below" if long else "above", _p(stop)))
                if target and ((long and live >= target - CENT) or (not long and live <= target + CENT)):
                    later.append("target: the last price %s is already at or %s the target %s"
                                 % (_p(live), "above" if long else "below", _p(target)))
            daily = c["max_daily_loss_usd"]
            if unknown_losses:
                later.append("daily loss: total risk from today's entries is unknown (%s); reconcile "
                             "the trade log before a new entry%s"
                             % (", ".join(unknown_losses), (" (limit %s)" % money(daily)) if daily else ""))
            elif daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = c["max_trades_per_day"] or 0
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if c["pdt_guard"] and pdt_n >= 3:
                later.append("pdt: %d day trades in the last 5 business days (%s); a 4th trips the "
                             "pattern-day-trader rule your broker applies (\"pdt_guard\": true)"
                             % (pdt_n, ", ".join(pdt_list)))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s%s, reward:risk %s%s"
                            % (money(max_loss), (" (limit %s)" % money(c["max_loss_per_trade_usd"])),
                               rr if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if c["min_reward_risk"] else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               ("unknown (%s known)" % money(loss_today + batch_loss)) if unknown_losses
                               else money(loss_today + batch_loss), money(daily) if daily else "no limit on"))
            later.extend(unknown_exposure)

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = c["max_orders_per_day"] or 0
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or Decimal(0)
            if acct is not None:
                if side == "buy":
                    if value is not None:
                        acct["cash"] -= value
                        acct["values"][sym] = acct["values"].get(sym, Decimal(0)) + value
                    if q is not None:
                        acct["shares"][sym] = held + q
                    elif ref and value is not None:
                        acct["shares"][sym] = held + value / ref
                elif side == "sell":
                    # A sale's proceeds settle later (T+1): they are unsettled cash, counted in the account's
                    # total but never spendable, so they neither fund a later buy in this batch (upstream added them
                    # to cash here) nor shrink the total the buffer and the cap are measured on (review item 7).
                    if value is not None:
                        moved = min(acct["values"].get(sym, Decimal(0)), value)
                        acct["values"][sym] = acct["values"].get(sym, Decimal(0)) - moved
                        acct["unsettled"] += moved
                    if q is not None:
                        acct["shares"][sym] = held - q
                    elif ref and value is not None:
                        acct["shares"][sym] = held - value / ref
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": money(_f(notional) or 0) if notional is not None else ("%s shares" % qty),
            "value_usd": value.quantize(PENNY, rounding=ROUND_HALF_EVEN) if value is not None else None,
            "entry_ref": ref if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "reward_risk": rr, "multiplier": mult, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
            "valid_until": utc_iso(entry_until if kind == "entry" else fresh_until),
        })

    return {
        "date": today,
        "now_new_york": now_et.strftime("%Y-%m-%d %H:%M"),
        "checked_at": utc_iso(now_utc),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": acct["cash"].quantize(PENNY) if acct else None,
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "unresolved_approvals": len(pending),
                  "loss_at_risk": None if unknown_losses else loss_today.quantize(PENNY),
                  "known_loss_at_risk": loss_today.quantize(PENNY),
                  "entries_with_unknown_loss": len(unknown_losses),
                  "day_trades_5d": pdt_n if c["pdt_guard"] else None},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s New York. Settled cash after the passed orders: %s."
                 % (res["pass"], res["block"], res["now_new_york"],
                    money(res["cash_after"]) if res["cash_after"] is not None else "unknown"))
    if res["block"]:
        lines.append("A blocked plan gets no paper ticket. Fix the plan or your policy, then assess it again.")
    else:
        lines.append("Every order passed. A paper ticket takes your yes one at a time; nothing is sent without it, "
                     "and only to your Alpaca paper account.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """A prices document -> ({SYM: price}, {SYM: price time, aware, or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the document's as_of. A price that is not a positive finite
    number is not a price; a time that is not an aware RFC 3339 instant is no time."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = _utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        v = _f(p)
        if v is None or v <= 0:
            continue
        s = str(s).upper()
        out[s] = v
        d = detail.get(s) or {}
        times[s] = _utc(d.get("time")) or _utc(d.get("fetched")) or as_of
    return out, times


def _utc(text: Any):
    """"2026-10-19T14:52:13Z" -> an aware UTC instant, or None (naive or junk)."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(UTC)


__all__ = ["ALPACA_FIELDS", "FRESH_MINUTES", "RiskError", "account", "check", "day_trades", "early_cutoff",
           "exposure", "fingerprint", "load_prices", "report", "to_eastern", "unresolved_approvals",
           "window_check"]
