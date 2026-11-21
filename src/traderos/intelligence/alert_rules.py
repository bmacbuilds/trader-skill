"""The upstream price alerts, ported from the tank (tank/day-trader-skill/trader-alerts/alerts.py `evaluate` at
3d0f4ef).

The rules are upstream's, line for line:
* **above / target**: the price is at or above `price`. **below / stop**: at or below. Each rings ONCE, then stays
  quiet until it is reset, unless `"rearm": true` (it re-arms when the price crosses back).
* **pct_move**: the price moved `pct` percent or more from the previous close (or from `ref`); `"direction"` "up" or
  "down" narrows it. At most once per New York day.
* **opening_bell**: the NYSE opened today; once per trading day.
* **time**: the New York clock passed `at` (HH:MM) on a trading day; once per trading day. `"before_close": true` moves
  it up with an early close, so a 15:55 "flat by close" rings at 12:55 on those days.
* A rule with `date` is watched on that New York date only. The state is upstream's: `{"rung": {rule id: UTC stamp}}`.

Changed, each with its reason:
* **The price is the newest complete stored bar's close** through the as-of reader, and the previous close is the
  last close of the session before it, not a Yahoo or Stooq quote. Flaw: both sources are held (spec 06.1). A symbol
  whose newest bar is stale gets no price, so no level rings on old data (part B's "fresh data or no answer").
* **XNYS sessions and the clock's Eastern-time rules**, not a 2026-2027 table. Flaw: the same hand-kept holidays.
* **The flat-by alert from a plan uses the plan's own flat-by instant, shifted once.** Flaw: the upstream
  `rules_from_plan` copied the plan's flat_by, already moved to 12:55 on an early close, and set `before_close`, which
  moved it three hours again: on the day after Thanksgiving it rang at 9:55 a.m.
* **The early-close shift is the session's own (regular close minus the early close)**, not a fixed three hours; on
  every NYSE early close they are equal (a better method that cannot drift).
* **No message points at an order.** The upstream flat-by message named its broker skill's sell ticket; TraderOS free
  never builds, sends or recommends an order (spec 12.1).
* **Decimal prices; a float literal is refused** (contracts v1).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from traderos.contracts import (
    AlertEvent,
    BarReader,
    Completeness,
    DataError,
    ErrorCode,
    ExchangeCalendar,
    ExitPlan,
    SafeFor,
    ValidationError,
    record_hash,
    with_hash,
)
from traderos.contracts.canonical import canonical_bytes, decimal_str, sha256_bytes
from traderos.intelligence._common import (
    complete_bars,
    dataset_hash,
    is_stale,
    labels,
    last_close_at_or_before,
    stable_id,
    usable,
)
from traderos.intelligence.triggers import REGULAR_SESSION, ny_date, ny_time, trading_day

TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}
MAX_RULES = 200
LOOKBACK_SESSIONS = 5                            # a touch since the last check is looked for this far back at most
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_FLAT = re.compile(r"^out by (\S+), win or lose$")


def _bad(message: str, code: ErrorCode = ErrorCode.INVALID_INPUT) -> ValidationError:
    return ValidationError(message, code=code)


def _hhmm(text: Any) -> time | None:
    m = _HHMM.match(str(text or "").strip())
    return time(int(m.group(1)), int(m.group(2))) if m else None


def _number(value: Any) -> Decimal | None:
    """An int or a decimal string; a float (or a bool) is not a number here."""
    if isinstance(value, (bool, float)):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    return None


def validate(rule: dict) -> None:
    """The upstream `validate`, with numbers as contracts v1 write them (an int or a decimal string). A price rule names
    its instrument_id: a ticker is not identity (contracts v1 section 5.2); `symbol` is only shown."""
    if not isinstance(rule, dict):
        raise _bad("a rule is a JSON object")
    t = rule.get("type")
    if t not in TYPES:
        raise _bad(f"rule {rule.get('id')}: unknown type {t!r}")
    for key in ("id", "instrument_id", "symbol", "dataset_id", "label", "note"):
        if rule.get(key) is not None and (not isinstance(rule[key], str) or not rule[key].strip()):
            raise _bad(f"rule {rule.get('id')}: {key} must be a non-empty string")
    if t not in NO_QUOTE and not rule.get("instrument_id"):
        raise _bad(f"rule {rule.get('id')} needs an instrument_id (a ticker is not identity)")
    if t in ("above", "below", "stop", "target") and (_number(rule.get("price")) is None
                                                     or _number(rule.get("price")) <= 0):
        raise _bad(f"rule {rule.get('id')} needs a price above 0, as a number or a decimal string")
    if t == "pct_move" and (_number(rule.get("pct")) is None or _number(rule.get("pct")) <= 0):
        raise _bad(f"rule {rule.get('id')} needs a pct above 0")
    if t == "pct_move" and rule.get("direction") not in (None, "up", "down"):
        raise _bad(f"rule {rule.get('id')}: direction is up or down")
    if t == "pct_move" and rule.get("ref") is not None and (_number(rule["ref"]) is None or _number(rule["ref"]) <= 0):
        raise _bad(f"rule {rule.get('id')}: ref must be a price above 0")
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise _bad(f"rule {rule.get('id')} needs \"at\" as a New York time like \"15:55\"")
    if rule.get("watch_from") is not None:
        _when(rule["watch_from"], f"rule {rule.get('id')}: watch_from")
    for key in ("rearm", "before_close"):
        if rule.get(key) is not None and not isinstance(rule[key], bool):
            raise _bad(f"rule {rule.get('id')}: {key} is true or false")
    if rule.get("date") is not None and not _DATE.match(str(rule["date"])):
        raise _bad(f"rule {rule.get('id')}: date must be YYYY-MM-DD")


def rule_id(rule: dict) -> str:
    return str(rule.get("id") or f"{rule.get('instrument_id', 'market')}-{rule['type']}")


def _stamp_time(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _when(text: Any, what: str = "watch_from") -> datetime:
    """A timezone-aware instant from RFC 3339 text, or INVALID_INPUT."""
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        raise _bad(f"{what} is not an RFC 3339 instant: {text!r}") from None
    if t.tzinfo is None:
        raise _bad(f"{what} needs a timezone: {text!r}")
    return t.astimezone(UTC)


def validate_state(state: Any) -> dict:
    """The upstream state file, checked: {"rung": {rule id: UTC stamp}, "checked": {rule id: its last check},
    "watching": {rule id: its first check}, "last_check": UTC stamp}. Anything else, an impossible date included, is
    refused as INVALID_INPUT, never guessed at."""
    bad = _bad('the alert state is {"rung": {...}, "checked": {...}, "watching": {...}, "last_check": "..."} with UTC '
               'stamps like 2026-11-23T15:00:00Z')
    if not isinstance(state, dict) or set(state) - {"rung", "checked", "watching", "last_check"}:
        raise bad
    maps = [state.get("rung", {}), state.get("checked", {}), state.get("watching", {})]
    if not all(isinstance(m, dict) and all(isinstance(k, str) for k in m) for m in maps):
        raise bad
    for v in [*(v for m in maps for v in m.values()), state.get("last_check", "2000-01-01T00:00:00Z")]:
        if not isinstance(v, str) or not _STAMP.match(v):
            raise bad
        try:
            _stamp_time(v)
        except ValueError:                   # 2026-02-30, month 13, hour 25: refused, not an internal error
            raise bad from None
    return copy.deepcopy(state)


# ---- the New York clock -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Status:
    """The upstream `market_status`: the New York date, whether the NYSE is open, and today's session if there is
    one."""
    date: date
    now_new_york: time
    open: bool
    opens: datetime | None
    closes: datetime | None
    early_close: bool


def market_status(calendar: ExchangeCalendar, at: datetime) -> Status:
    day = ny_date(at)
    if not calendar.is_session(day):
        return Status(day, ny_time(at), False, None, None, False)
    o, c = calendar.open_close(day)
    return Status(day, ny_time(at), o <= at < c, o, c, calendar.is_early_close(day))


# ---- prices from the store --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    """The newest regular-session close, the reference prices for pct_move, and the recent bars a touch is read from."""
    price: Decimal
    prev_close: Decimal | None
    session_open: Decimal | None
    observed_at: datetime
    bars: tuple[tuple[datetime, datetime, Decimal, Decimal], ...] = ()   # (start, end, low, high), oldest first
    floor: datetime | None = None                              # the earliest instant `bars` cover

    def extremes(self, since: datetime | None, watched_from: datetime | None = None
                 ) -> tuple[Decimal, Decimal, bool]:
        """(low, high, missed) over the bars that closed after `since` and began at or after `watched_from`, the
        moment the rule began to be watched. A bar that began before that counts not at all, even when it closed
        after: its low may have come before the rule existed (the price at each check covers the rest of it, as
        the upstream quote did). With no `since` (a rule's first check and no plan time), only the price now."""
        if since is None:
            return self.price, self.price, False
        missed = self.floor is not None and since < self.floor
        window = [(lo, hi) for start, end, lo, hi in self.bars
                  if end > since and (watched_from is None or start >= watched_from)]
        if not window:                       # nothing closed since: only the price now (no bar rings twice)
            return self.price, self.price, missed
        return min(lo for lo, _ in window), max(hi for _, hi in window), missed


def quotes(reader: BarReader, calendar: ExchangeCalendar, dataset_id: str, instrument_ids, at: datetime, *,
           max_staleness_bars: int = 1) -> tuple[dict[str, Quote], dict[str, str]]:
    """({instrument_id: Quote}, {instrument_id: why it has no price}). Regular-session bars only, as the upstream
    regular-market quote was: an after-hours bar is never the price, the previous close or a touch.

    An instrument the dataset does not hold is refused (MISSING_DATA), as `alerts check` refuses it."""
    manifest = usable(reader.manifest(dataset_id))
    by_id = {i.instrument_id: i for i in manifest.instruments}
    unknown = sorted(set(instrument_ids) - set(by_id))
    if unknown:
        raise DataError(f"{unknown[0]} is not an instrument_id in {dataset_id} (a ticker is not identity)",
                        code=ErrorCode.MISSING_DATA)
    out: dict[str, Quote] = {}
    missing: dict[str, str] = {}
    for iid in sorted(instrument_ids):
        newest_any = complete_bars(reader.bars(dataset_id, iid, as_of=at, limit=1))
        if not newest_any:
            missing[iid] = f"{ErrorCode.MISSING_DATA.value}: no complete bars"
            continue
        # The regular sessions from LOOKBACK_SESSIONS back, through the newest bar's session.
        newest_day = calendar.session_of(newest_any[-1].start) or last_close_at_or_before(
            calendar, newest_any[-1].end)[0]
        bound = newest_day
        for _ in range(LOOKBACK_SESSIONS):
            bound = calendar.previous_session(bound)
        floor = calendar.open_close(bound)[0]
        regular = []
        for b in complete_bars(reader.bars(dataset_id, iid, as_of=at, start=floor)):
            day = calendar.session_of(b.start)
            if day is not None and b.end <= calendar.open_close(day)[1]:
                regular.append((day, b))
        if not regular:
            missing[iid] = f"{ErrorCode.MISSING_DATA.value}: no complete regular-session bars"
            continue
        day, last = regular[-1]
        stale, behind = is_stale(calendar, manifest.interval, last.end, at, max_staleness_bars)
        if stale:
            missing[iid] = (f"{ErrorCode.DATA_STALE.value}: the newest regular-session close ended "
                            f"{last.end.isoformat()}, {behind} bar(s) behind")
            continue
        # The previous close is the previous SESSION's closing bar, as the upstream regularMarketPreviousClose was:
        # never an older session's (a missing Friday must not make Thursday's close Monday's reference), and never a bar
        # before that session's close. Without it, pct_move falls back to today's open, as the upstream one did.
        prev_day = calendar.previous_session(day)
        prev_bars = [b for d, b in regular if d == prev_day]
        prev_close = (prev_bars[-1].close if prev_bars and prev_bars[-1].end == calendar.open_close(prev_day)[1]
                      else None)
        today = [b for d, b in regular if d == day]
        out[iid] = Quote(last.close, prev_close, today[0].open if today else None,
                         last.end, tuple((b.start, b.end, b.low, b.high) for _, b in regular), floor)
    return out, missing


# ---- the rules --------------------------------------------------------------------------------------------------

def _rang_today(stamp: str | None, today: date) -> bool:
    return bool(stamp) and ny_date(_stamp_time(stamp)) == today


def ring_time(rule: dict, status: Status) -> time:
    """The time a time rule rings today, moved up with an early close if "before_close"."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and status.early_close and status.opens is not None:
        shift = status.opens + REGULAR_SESSION - status.closes
        at = (datetime.combine(status.date, at) - shift).time()
    return at


def _money(x: Decimal) -> str:
    return f"${x.quantize(Decimal('0.01'))}"


def _n(x: Decimal) -> str:
    return str(int(x)) if x == x.to_integral_value() else str(x.quantize(Decimal("0.01")))


@dataclass(frozen=True)
class Outcome:
    """One rule's answer at one check: triggered None means it could not be checked (never "nothing hit")."""
    rule_id: str
    triggered: bool | None
    rang: str | None                             # the message, when it rang at this check
    reasons: tuple[str, ...] = ()
    observed_at: datetime | None = None
    missed: bool = False


def evaluate(rules: list[dict], prices: dict[str, Quote], status: Status, state: dict | None, at: datetime, *,
             missing: dict[str, str] | None = None) -> tuple[list[Outcome], dict]:
    """The upstream `evaluate`, pure: (one Outcome per rule, the new state)."""
    if len(rules) > MAX_RULES:
        raise _bad(f"at most {MAX_RULES} rules")
    state = validate_state(state or {})
    rung = state.setdefault("rung", {})
    checked = state.setdefault("checked", {})                    # each rule's own last check (its touch window)
    watching = state.setdefault("watching", {})                  # a plain rule's first check: when watching began
    missing = missing or {}
    today = status.date
    local = status.now_new_york
    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[Outcome] = []
    for r in rules:
        validate(r)
        rid = rule_id(r)
        if r.get("date") is not None and str(r["date"]) != today.isoformat():
            if str(r["date"]) < today.isoformat():
                rung.pop(rid, None)                              # its day has passed: the mark goes with it
                checked.pop(rid, None)
                watching.pop(rid, None)
            out.append(Outcome(rid, False, None, (f"watched on {r['date']} only",)))
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        checked_before = checked.get(rid)                        # this rule's own previous check
        if typ in ("opening_bell", "time"):
            checked[rid] = stamp
        if typ == "opening_bell":
            msg = None
            if status.open and not _rang_today(rung.get(rid), today):
                msg = (f"Opening bell: the NYSE is open ({local.strftime('%H:%M')} New York)"
                       + (f", early close today at {ny_time(status.closes).strftime('%H:%M')}" if status.early_close
                          else "") + ".")
                rung[rid] = stamp
            out.append(Outcome(rid, status.open, msg, _quiet(r, rid, rung, msg)))
            continue
        if typ == "time":
            when = ring_time(r, status)
            due = status.opens is not None and local >= when
            msg = None
            if due and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = (f"{label[:1].upper()}{label[1:]}: it is {local.strftime('%H:%M')} New York (set for "
                       f"{when.strftime('%H:%M')})")
                msg += (f", the market closes at {ny_time(status.closes).strftime('%H:%M')}." if status.open
                        else ", the market has closed.")
                if r.get("symbol"):
                    msg += f" If you still hold {r['symbol']}, this is the time you planned to be out."
                rung[rid] = stamp
            out.append(Outcome(rid, due, msg, _quiet(r, rid, rung, msg)))
            continue
        iid = r["instrument_id"]
        sym = r.get("symbol") or iid
        q = prices.get(iid)
        if q is None:
            out.append(Outcome(rid, None, None, (missing.get(iid, f"{ErrorCode.MISSING_DATA.value}: no price"),)))
            continue
        p = q.price
        checked[rid] = stamp                     # only a check that had a price: a missing one never skips bars
        # This rule's window: from its own last check, else from when its plan was made, else (a first check) only
        # the price now. Watching began at the plan's time, or at the rule's first check; a bar that began before
        # that never counts as a touch, even one that closed after (OSDev4's re-review, R1).
        watched = _when(r["watch_from"]) if r.get("watch_from") else (
            _stamp_time(watching[rid]) if rid in watching else None)
        since = _stamp_time(checked_before) if checked_before else watched
        low, high, was_missed = q.extremes(since, watched)
        if not r.get("watch_from"):
            watching.setdefault(rid, stamp)
        missed = (f"MISSED_WINDOW: this rule was last checked more than {LOOKBACK_SESSIONS} sessions ago; only the "
                  "bars since then were read",) if was_missed else ()
        msg = None
        if typ in ("above", "below"):
            level = _number(r["price"])
            touched = high >= level if typ == "above" else low <= level
            now_hit = p >= level if typ == "above" else p <= level
            word = "at or above" if typ == "above" else "at or below"
            what = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
            if touched and rid not in rung:
                note = f" ({r['note']})" if r.get("note") else ""
                if now_hit:
                    msg = f"{sym} {'is' if status.open else 'last'} {_money(p)}: {word} your {what} ${_n(level)}{note}."
                else:
                    extreme = high if typ == "above" else low
                    msg = (f"{sym} traded {_money(extreme)} since {since.isoformat()}: {word} your {what} "
                           f"${_n(level)}{note}; it is {_money(p)} now.")
                rung[rid] = stamp
            elif not now_hit and rid in rung and r.get("rearm"):
                del rung[rid]
            out.append(Outcome(rid, touched, msg, (*missed, *_quiet(r, rid, rung, msg)), q.observed_at, was_missed))
        else:                                                    # pct_move
            ref = _number(r["ref"]) if r.get("ref") is not None else (q.prev_close or q.session_open)
            if not ref:
                out.append(Outcome(rid, None, None, (f"{ErrorCode.MISSING_DATA.value}: no previous close or open "
                                                     "to measure the move from",), q.observed_at))
                continue
            move = 100 * (p / ref - 1)
            want = r.get("direction")
            hit = abs(move) >= _number(r["pct"]) and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                msg = f"{sym} moved {move.quantize(Decimal('0.01')):+}% to {_money(p)} (from {_money(ref)})."
                rung[rid] = stamp
            out.append(Outcome(rid, hit, msg, (*missed, *_quiet(r, rid, rung, msg)), q.observed_at, was_missed))
    state["last_check"] = stamp
    return out, state


def _quiet(rule: dict, rid: str, rung: dict, msg: str | None) -> tuple[str, ...]:
    if msg is not None:
        return (f"RANG: {msg}",)
    if rid not in rung:
        return ()
    if rule["type"] in ("opening_bell", "time", "pct_move"):
        if rule.get("date") is not None:                 # a plan's flat-by: watched on its own date only
            return (f"rang at {rung[rid]}; it was set for {rule['date']} only",)
        return (f"rang at {rung[rid]}; it rings again the next trading day",)
    if rule.get("rearm"):
        return (f"rang at {rung[rid]}; quiet until the price crosses back, or `traderos alerts reset {rid}`",)
    return (f"rang at {rung[rid]}; quiet until `traderos alerts reset {rid}`",)


def check(rules: list[dict], *, at: datetime, state: dict | None, calendar: ExchangeCalendar,
          reader: BarReader | None = None, dataset_id: str | None = None) -> tuple[list[AlertEvent], dict]:
    """Every rule's answer at `at` as an alert-event/1, with the new state. Price rules read `dataset_id`; a rule
    that names another dataset is refused."""
    state = validate_state(state or {})
    for r in rules:
        validate(r)
    price_rules = [r for r in rules if r["type"] not in NO_QUOTE]
    prices: dict[str, Quote] = {}
    missing: dict[str, str] = {}
    if price_rules:
        if reader is None or dataset_id is None:
            raise _bad("price alerts need a dataset")
        other = {r["dataset_id"] for r in price_rules if r.get("dataset_id")} - {dataset_id}
        if other:
            raise _bad(f"a rule names dataset {sorted(other)[0]}, but {dataset_id} is being read")
        prices, missing = quotes(reader, calendar, dataset_id, {r["instrument_id"] for r in price_rules}, at)
    outcomes, new_state = evaluate(rules, prices, market_status(calendar, at), state, at, missing=missing)
    if reader is not None and dataset_id is not None:
        manifest = usable(reader.manifest(dataset_id))
        limitations, safe_for = labels(manifest)
        ds, ds_hash, provider = dataset_id, dataset_hash(manifest), manifest.provider
    else:
        limitations, safe_for, ds, ds_hash, provider = (), (SafeFor.DISPLAY,), "", "", "XNYS calendar"
    own = state.get("checked", {})                    # before this check: each rule's own previous check
    events = []
    for r, o in zip(rules, outcomes, strict=True):
        previous = _stamp_time(own[o.rule_id]) if o.rule_id in own else None
        events.append(with_hash(AlertEvent(
            event_id=stable_id("AJ", o.rule_id, sha256_bytes(canonical_bytes(r)), ds_hash, at.isoformat(),
                               state.get("rung", {}).get(o.rule_id, "")),
            rule_id=o.rule_id, dataset_id=ds, dataset_hash=ds_hash, provider=provider, evaluated_at=at,
            previous_evaluated_at=previous if previous is not None and previous <= at else None,
            data_observed_at=o.observed_at, triggered=o.triggered, missed=o.missed, reasons=o.reasons,
            completeness=Completeness.COMPLETE if o.triggered is not None else Completeness.UNAVAILABLE,
            limitations=limitations, safe_for=safe_for)))
    return events, new_state


def reset(state: dict | None, ids) -> tuple[dict, list[str], list[str]]:
    """The upstream `reset`: clear rung marks, so those alerts can ring again. (new state, cleared, not found)."""
    state = validate_state(state or {})
    rung = state.setdefault("rung", {})
    cleared = [i for i in ids if rung.pop(i, None) is not None]
    return state, cleared, [i for i in ids if i not in cleared]


def rules_from_plan(plan: ExitPlan, calendar: ExchangeCalendar) -> list[dict]:
    """An exit plan -> a stop alert, a target alert and a flat-by alert, for the plan's New York trading day.

    The plan's hash is checked first: an edited plan (a stop changed by hand) is refused, not watched. The ids carry
    the plan's id, so a new plan's alerts never inherit an earlier plan's rung marks, and its price alerts watch from
    the plan's own time: a bar from before the plan existed is never a touch."""
    if plan.canonical_hash is None or record_hash(plan) != plan.canonical_hash:
        raise _bad("the plan's content does not match its canonical_hash: it was edited after it was made",
                   ErrorCode.INTEGRITY_MISMATCH)
    by = {s.name: s for s in plan.scenarios}
    sym = plan.symbol
    base = f"{sym.lower()}-{plan.plan_id.lower()}"
    out = []
    for key in ("stop", "target"):
        s = by.get(key)
        if s is None or s.price.value is None:
            raise _bad(f"the plan has no {key} price")
        note = next((a.split(": ", 1)[1] for a in s.assumptions if a.startswith(f"{key}: ")), "")
        out.append({"id": f"{base}-{key}", "instrument_id": plan.instrument_id, "dataset_id": plan.dataset_id,
                    "watch_from": plan.as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "symbol": sym, "type": key, "price": decimal_str(s.price.value),
                    "note": f"plan {key}" + (f": {note}" if note else "")})
    t = by.get("time exit")
    if t is not None:
        m = _FLAT.match(t.trigger)
        if not m:
            raise _bad(f"the plan's time exit {t.trigger!r} has no time")
        flat = datetime.fromisoformat(m.group(1))
        day = ny_date(flat)
        # The plan's instant is already moved for an early close: no before_close, or it would move twice.
        out.append({"id": f"{base}-flat-by", "symbol": sym, "type": "time", "at": ny_time(flat).strftime("%H:%M"),
                    "label": "flat by close"})
    else:
        # A plan given as prices has no time exit; upstream always added 15:55, moved up on an early close.
        day = trading_day(calendar, plan.as_of) or calendar.next_session(ny_date(plan.as_of))
        out.append({"id": f"{base}-flat-by", "symbol": sym, "type": "time", "at": "15:55", "label": "flat by close",
                    "before_close": True})
    for r in out:
        r["date"] = day.isoformat()
    return out
