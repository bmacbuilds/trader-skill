"""The upstream stop and target rules, ported from the tank (tank/day-trader-skill/trader-exit/exit.py at 3d0f4ef).

The rules are upstream's, line for line:
* **Stops** (a long's stop rounds DOWN, away from the entry): `or-mid` (the opening-range midpoint, the default for an
  orb entry), `or-low`, `price:X`, `pct:X` (X percent under the entry), `atr:K` (K times the ATR of the session's bars
  up to the entry: the last 14, or all of them with fewer, and at least 3; the session's first bar uses its own high
  minus low). A level setup has no opening-range default: it needs a stop.
* **Targets** (round half up): `2R` (the default), `1R`, `1.5R` ... = entry + R x risk; or `price:X`.
* **Refused, with the reason:** a stop at or above the entry, a target at or below it, a risk under 0.05% of the price
  ("too tight to survive the spread"), a short (the upstream planner plans longs only).
* **flat_by** (default 15:55 New York): out by then, win or lose. On an early close it moves up with the close
  (12:55); a flat-by given at or after the close is refused. `min_reward_risk` (default 1.0) only warns.

Changed, each with its reason:
* **The instrument's tick, not the cent.** Flaw: a cent assumed every instrument trades in cents; contracts v1 carry
  each instrument's tick. On a $0.01 tick the prices are identical.
* **An entry off the tick is refused, not rounded.** Flaw: rounding a typed entry silently plans a different trade
  (spec RC08: refuse rather than repair).
* **The opening range and the ATR from the stored session bars** through the as-of reader, not Yahoo (spec 06.1); a
  range missing a bar is refused (upstream PR #23). `or_high`/`or_low` can still be given, as the upstream flags
  allowed.
* **XNYS sessions and the 09:30 open** place 15:55 and the early close, not a 2026-2027 table (the flaw named in
  traderos.intelligence.triggers).
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation

from traderos.contracts import ErrorCode, ExchangeCalendar, ValidationError
from traderos.contracts.canonical import decimal_str
from traderos.contracts.market import Bar
from traderos.intelligence.triggers import early_cutoff, local_time

DEFAULTS = {"flat_by": "15:55", "min_reward_risk": Decimal("1.0")}       # The upstream DEFAULTS
ATR_BARS = 14
MIN_ATR_BARS = 3
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
CENT = Decimal("0.01")
_NUM = r"\$?([0-9][0-9,]*\.?[0-9]*)"
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _bad(message: str) -> ValidationError:
    return ValidationError(message, code=ErrorCode.INVALID_INPUT)


def _dec(text: str, what: str) -> Decimal:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise _bad(f"{what} is not a number: {text!r}") from None


def parse_stop(text: str) -> tuple[str, Decimal | None]:
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(rf"^(price|pct|atr):\s*{_NUM}$", t)
    if not m:
        raise _bad(f"the stop is or-mid, or-low, price:X, pct:X or atr:K (got {text!r})")
    kind, val = m.group(1), _dec(m.group(2), "the stop")
    if val <= 0:
        raise _bad(f"a {kind} stop needs a number above 0")
    if kind == "pct" and val >= 100:
        raise _bad("a pct:X stop needs X under 100")
    return kind, val


def parse_target(text: str) -> tuple[str, Decimal]:
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = _dec(m.group(1), "the target")
        if r <= 0:
            raise _bad("the target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(rf"^price:\s*{_NUM}$", t.lower())
    if m:
        return "price", _dec(m.group(1), "the target")
    raise _bad(f"the target is 2R, 1R, 1.5R ... or price:X (got {text!r})")


def _to_tick(x: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (x / tick).to_integral_value(rounding=rounding) * tick


def atr_of(bars: Sequence[Bar], n: int = ATR_BARS) -> tuple[Decimal, int]:
    """The upstream ATR: the average true range of the last n of the session's bars (all of them if fewer). The
    session's first bar uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b.high - b.low
        if prev is not None:
            tr = max(tr, abs(b.high - prev), abs(b.low - prev))
        trs.append(tr)
        prev = b.close
    use = trs[-n:]
    return sum(use, Decimal(0)) / len(use), len(use)


@dataclass(frozen=True)
class Levels:
    stop: Decimal
    stop_label: str
    stop_detail: str
    target: Decimal
    target_label: str
    risk: Decimal
    reward_risk: Decimal


def compute(entry: Decimal, tick: Decimal, stop_rule: str, stop_param: Decimal | None, target_rule: str,
            target_param: Decimal, *, or_high: Decimal | None = None, or_low: Decimal | None = None,
            atr: Decimal | None = None, atr_bars: int | None = None, atr_to: str = "the entry") -> Levels:
    """The upstream `compute`: the stop, the target and the risk, on the tick. Raises ValidationError with the
    reason."""
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise _bad(f"an {stop_rule.replace('_', '-')} stop needs the opening range")
        if or_high <= or_low:
            raise _bad(f"the opening-range high {or_high} must be above its low {or_low}")
        if stop_rule == "or_mid":
            raw, label = (or_high + or_low) / 2, "opening-range midpoint"
            detail = f"({or_high} + {or_low}) / 2 = {decimal_str(raw)}"
        else:
            raw, label = or_low, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = entry * (1 - stop_param / 100)
        label = f"{decimal_str(stop_param)}% under the entry"
        detail = f"{entry} x (1 - {decimal_str(stop_param)}%) = {decimal_str(raw)}"
    elif stop_rule == "atr":
        if atr is None:
            raise _bad("an atr stop needs the session's bars")
        a = atr.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)       # Upstream: D(round(atr, 6))
        raw = entry - stop_param * a
        label = f"{decimal_str(stop_param)} x ATR({atr_bars}) under the entry"
        detail = (f"ATR {a.quantize(Decimal('0.0001'))} over the {atr_bars} bars to {atr_to}"
                  + (f" (fewer than {ATR_BARS} so far)" if atr_bars < ATR_BARS else ""))
    else:
        raise _bad(f"unknown stop rule {stop_rule!r}")
    stop = _to_tick(raw, tick, ROUND_FLOOR)
    if stop != raw:
        detail = detail + ", rounded down" if detail else f"{decimal_str(raw)} rounded down"
    if stop >= entry:
        raise _bad(f"the stop {stop} ({label}) is at or above the entry {entry}: a long's stop goes under the entry")
    if stop <= 0:
        raise _bad(f"the stop works out to {stop}: pick a stop above 0")
    risk = entry - stop
    floor_risk = entry * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise _bad(f"the stop is {risk} under the entry, less than 0.05% of the price "
                   f"({floor_risk.quantize(CENT, rounding=ROUND_HALF_UP)} on {entry}): too tight to survive the "
                   "spread, normal wiggle would stop you out. Widen it.")
    if target_rule == "r":
        target = _to_tick(entry + target_param * risk, tick, ROUND_HALF_UP)
        t_label = f"{decimal_str(target_param)}R ({decimal_str(target_param)} times the risk)"
    else:
        target, t_label = _to_tick(target_param, tick, ROUND_HALF_UP), ""
    if target <= entry:
        raise _bad(f"the target {target} is at or below the entry {entry}: a long's target goes above it")
    rr = ((target - entry) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    return Levels(stop=stop, stop_label=label, stop_detail=detail, target=target,
                  target_label=t_label or f"your price ({rr}R)", risk=risk, reward_risk=rr)


def flat_by(calendar: ExchangeCalendar, day, explicit: str | None, entry_at: datetime | None = None
            ) -> tuple[datetime, str]:
    """The upstream `resolve_flat_by`: (the instant to be out by, a note). The default moves up with an early close; one
    you give at or after the close is refused, as is an entry at or after it."""
    text = explicit or DEFAULTS["flat_by"]
    m = _HHMM.match(text)
    if not m:
        raise _bad(f"flat_by wants HH:MM, not {text!r}")
    open_, close = calendar.open_close(day)
    at = local_time(open_, (int(m.group(1)), int(m.group(2))))
    note = ""
    if at >= close:
        if explicit:
            raise _bad(f"flat_by {text} is at or after the close on {day.isoformat()} ({close.isoformat()})")
        moved = early_cutoff(at, open_, close)
        if moved >= close or moved <= open_:
            raise _bad(f"flat_by {text} is at or after the close")
        at, note = moved, f"moved up for the early close at {close.isoformat()}"
    if entry_at is not None and entry_at >= at:
        raise _bad(f"the entry at {entry_at.isoformat()} is at or after the flat-by {at.isoformat()}: there is no "
                   "time left to hold it")
    return at, note
