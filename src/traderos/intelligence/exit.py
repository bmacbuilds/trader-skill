"""S14 Exit Planner: hypothetical exit scenarios and their risks. No order is ever submitted (spec 12.1 S14).

* **Geometry is checked before anything is planned** (upstream PR #48, RISK-002; spec RC08): a long's stop is below
  its entry and its target above; a short's the other way round. A plan that breaks this is refused, never repaired.
* **A stop is not a guaranteed price.** Beside "stop" sits "gap through stop": the worst adverse SESSION gap (a
  session's first open against the previous session's last close) of the last GAP_LOOKBACK sessions, applied past
  the stop, because a stop order fills at the open when price gaps through it. Bar-to-bar gaps inside a session are
  not used: on 5-minute data they would hide the overnight risk.
* **Fresh data or a stated limit.** A plan marked at a close that is behind the calendar is PARTIAL, says DATA_STALE,
  and does not offer that close as "the last close".
* **No stop means no bound on the loss**: the plan says NO_PROTECTION instead of inventing one.
* Prices must be finite, positive and on the instrument's tick. A computed price is rounded to the tick against the
  position (down for a long, up for a short), never in its favour. Each scenario's difference from the entry is
  per unit, in the instrument's currency (contract v1's `pnl_per_unit`). As the upstream rule says, the text never calls
  it a P&L and prints no return or money total.

With a `stop_rule` or `target_rule` it plans the upstream way (traderos.intelligence.exit_rules, ported from the tank's
Exit Planner): or-mid / or-low / price:X / pct:X / atr:K stops, 2R / 1R / price:X targets, the 0.05% risk floor, a
"time exit" at flat_by (15:55 New York, moved up on an early close) and a warning under min_reward_risk. Longs only,
as the upstream planner is.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from traderos.contracts import (
    BarReader,
    Completeness,
    DataError,
    ErrorCode,
    ExchangeCalendar,
    ExitPlan,
    Limitation,
    MetricValue,
    ValidationError,
    parse_decimal,
    with_hash,
)
from traderos.contracts.canonical import decimal_str
from traderos.contracts.intel import ExitScenario
from traderos.contracts.risk import Direction
from traderos.features import feature
from traderos.intelligence import exit_rules, triggers
from traderos.intelligence._common import (
    complete_bars,
    dataset_hash,
    find_instrument,
    is_stale,
    labels,
    now_utc,
    resolve_as_of,
    session_gaps,
    stable_id,
    staleness_bound,
    usable,
)

GAP_LOOKBACK = 60                                # sessions
ATR_PERIOD = 14
MAX_PRICE = Decimal(10) ** 12                     # an input bound (spec 04.2), far above any real price
HYPOTHETICAL = Limitation(code="HYPOTHETICAL_ONLY", detail="Exit scenarios are hypothetical; no order is placed or "
                                                           "sent.")


def _price(raw, name: str, tick: Decimal | None = None) -> Decimal | None:
    if raw is None:
        return None
    value = raw if isinstance(raw, Decimal) else parse_decimal(raw, field=name)
    if not value.is_finite():
        raise ValidationError(f"{name} must be a finite number, not {value}", code=ErrorCode.INVALID_INPUT)
    if not 0 < value < MAX_PRICE:
        raise ValidationError(f"{name} must be a positive price below {MAX_PRICE:,}", code=ErrorCode.INVALID_INPUT)
    if tick is not None and value % tick != 0:
        raise ValidationError(f"{name} {value} is not on the instrument's tick of {tick}", code=ErrorCode.INVALID_INPUT)
    return value


def _against(price: Decimal, tick: Decimal, direction: Direction) -> Decimal:
    """Round a computed price to the tick against the position: down for a long, up for a short."""
    rounding = ROUND_FLOOR if direction is Direction.LONG else ROUND_CEILING
    return (price / tick).to_integral_value(rounding=rounding) * tick


def check_geometry(direction: Direction, entry: Decimal, stop: Decimal | None, target: Decimal | None) -> None:
    """RISK-002: refuse an impossible plan rather than plan it."""
    if Direction(direction) is Direction.LONG:
        if stop is not None and not stop < entry:
            raise ValidationError(f"a long's stop ({stop}) must be below its entry ({entry})")
        if target is not None and not target > entry:
            raise ValidationError(f"a long's target ({target}) must be above its entry ({entry})")
    else:
        if stop is not None and not stop > entry:
            raise ValidationError(f"a short's stop ({stop}) must be above its entry ({entry})")
        if target is not None and not target < entry:
            raise ValidationError(f"a short's target ({target}) must be below its entry ({entry})")


def _rule_levels(reader, calendar, manifest, ins, at, entry, *, stop_rule, target_rule, setup, or_minutes, or_high,
                  or_low, flat_by, entry_at, min_reward_risk):
    """The upstream stop, target and flat-by, in this plan's terms: (stop, target, stop note, target note, warnings,
    the flat-by instant and its note)."""
    if setup not in ("orb", "level"):
        raise ValidationError("setup is orb or level", code=ErrorCode.INVALID_INPUT)
    if stop_rule is None and setup != "orb":
        raise ValidationError(f"a {setup} setup has no opening-range stop to default to: give a stop rule price:X, "
                              "pct:X or atr:K", code=ErrorCode.INVALID_INPUT)
    s_rule, s_param = exit_rules.parse_stop(stop_rule or "or-mid")
    t_rule, t_param = exit_rules.parse_target(target_rule or "2R")
    if (or_high is None) != (or_low is None):
        raise ValidationError("give both or_high and or_low, or neither", code=ErrorCode.INVALID_INPUT)
    or_high, or_low = _price(or_high, "or_high", ins.tick_size), _price(or_low, "or_low", ins.tick_size)
    # Nothing after the entry is read (QT01; upstream #2 fixed the same look-ahead in the upstream planner).
    if entry_at is not None and entry_at > at:
        raise ValidationError(f"entry_at {entry_at.isoformat()} is after as_of {at.isoformat()}: a plan cannot read "
                              "an entry that has not happened", code=ErrorCode.INVALID_INPUT)
    if entry_at is not None and triggers.ny_date(entry_at) != triggers.ny_date(at):
        raise ValidationError(f"entry_at is on {triggers.ny_date(entry_at)} and as_of on {triggers.ny_date(at)}: "
                              "a plan is for one trading day (it is flat by the close)",
                              code=ErrorCode.INVALID_INPUT)
    upto = min(at, entry_at) if entry_at is not None else at
    # The plan's day is the New York day; on a weekend or holiday evening, the next session after it.
    day = triggers.trading_day(calendar, upto) or calendar.next_session(triggers.ny_date(upto))
    atr = atr_n = None
    if s_rule in ("or_mid", "or_low", "atr") and (s_rule == "atr" or or_high is None):
        step = triggers.STEP.get(manifest.interval)
        if step is None:
            raise ValidationError(f"a {stop_rule or 'or-mid'} stop reads the session's 1-minute or 5-minute bars; "
                                  f"{manifest.dataset_id} holds {manifest.interval.value} bars",
                                  code=ErrorCode.INVALID_INPUT)
        open_, close = calendar.open_close(day)
        by_slot = triggers.session_slots(
            complete_bars(reader.bars(manifest.dataset_id, ins.instrument_id, as_of=upto, start=open_)),
            open_, close, step, upto)
        if s_rule == "atr":
            session = [by_slot[k] for k in sorted(by_slot)]
            if len(session) < exit_rules.MIN_ATR_BARS:
                raise DataError(f"an atr stop needs at least {exit_rules.MIN_ATR_BARS} closed bars of the session by "
                                f"the entry; found {len(session)}", code=ErrorCode.MISSING_DATA)
            atr, atr_n = exit_rules.atr_of(session)
        else:
            if or_minutes <= 0 or (or_minutes * 60) % step:
                raise ValidationError(f"or_minutes must be a whole number of {manifest.interval.value} bars",
                                      code=ErrorCode.INVALID_INPUT)
            if upto < open_ + timedelta(minutes=or_minutes):
                raise DataError(f"the {or_minutes}-minute opening range is set at "
                                f"{(open_ + timedelta(minutes=or_minutes)).isoformat()}", code=ErrorCode.MISSING_DATA)
            or_high, or_low = triggers.opening_range(by_slot, or_minutes * 60 // step)
            # Upstream rounded a range read from the bars to the cent (`oh, ol = cents(...)`, half up); here, to the
            # tick.
            or_high = exit_rules._to_tick(or_high, ins.tick_size, ROUND_HALF_UP)
            or_low = exit_rules._to_tick(or_low, ins.tick_size, ROUND_HALF_UP)
    lv = exit_rules.compute(entry, ins.tick_size, s_rule, s_param, t_rule, t_param, or_high=or_high, or_low=or_low,
                            atr=atr, atr_bars=atr_n,
                            atr_to="the entry" if entry_at is not None else upto.isoformat())
    flat_at, flat_note = exit_rules.flat_by(calendar, day, flat_by, entry_at)
    warnings = []
    mrr = (exit_rules.DEFAULTS["min_reward_risk"] if min_reward_risk is None
           else triggers.number(min_reward_risk, "min_reward_risk", allow_zero=True))
    if lv.reward_risk < mrr:
        warnings.append(f"Reward:risk {lv.reward_risk} is under the min_reward_risk {decimal_str(mrr)}.")
    stop_note = f"stop: {lv.stop_label}" + (f"; {lv.stop_detail}" if lv.stop_detail else "")
    return lv.stop, lv.target, stop_note, f"target: {lv.target_label}", warnings, flat_at, flat_note


def plan_exit(reader: BarReader, calendar: ExchangeCalendar, dataset_id: str, symbol: str, *, direction: str,
              entry_price, stop_price=None, target_price=None, as_of: datetime | None = None,
              max_staleness_bars: int = 1, now: datetime | None = None, stop_rule: str | None = None,
              target_rule: str | None = None, setup: str = "orb", or_minutes: int = 15, or_high=None, or_low=None,
              flat_by: str | None = None, entry_at: datetime | None = None, min_reward_risk=None) -> ExitPlan:
    try:
        direction = Direction(direction)
    except ValueError:
        raise ValidationError(f"direction must be 'long' or 'short', not {direction!r}") from None
    staleness_bound(max_staleness_bars)
    by_rule = stop_rule is not None or target_rule is not None
    if by_rule and direction is not Direction.LONG:
        raise ValidationError("stop and target rules plan longs only: give a short's stop "
                              "and target as prices", code=ErrorCode.INVALID_INPUT)
    if (stop_rule is not None and stop_price is not None) or (target_rule is not None and target_price is not None):
        raise ValidationError("give a stop (or target) price or a rule, not both", code=ErrorCode.INVALID_INPUT)
    for raw, name in ((entry_price, "entry_price"), (stop_price, "stop_price"), (target_price, "target_price")):
        _price(raw, name)                        # finite and positive before anything is read
    manifest = usable(reader.manifest(dataset_id))
    ins = find_instrument(manifest, symbol)
    if ins is None:
        raise DataError(f"{symbol} is not in {dataset_id}", code=ErrorCode.MISSING_DATA)
    entry, stop, target = (_price(entry_price, "entry_price", ins.tick_size),
                           _price(stop_price, "stop_price", ins.tick_size),
                           _price(target_price, "target_price", ins.tick_size))
    if not by_rule:
        check_geometry(direction, entry, stop, target)          # before anything is read
    at = resolve_as_of(reader, dataset_id, as_of)
    notes: dict[str, str] = {}
    warnings: list[str] = []
    flat_at = None
    if by_rule:
        # A price given beside a rule is kept, as the upstream price:X (already on the tick, so unchanged).
        stop_rule = stop_rule if stop is None else f"price:{stop}"
        target_rule = target_rule if target is None else f"price:{target}"
        stop, target, notes["stop"], notes["target"], warnings, flat_at, flat_note = _rule_levels(
            reader, calendar, manifest, ins, at, entry, stop_rule=stop_rule, target_rule=target_rule, setup=setup,
            or_minutes=or_minutes, or_high=or_high, or_low=or_low, flat_by=flat_by,
            entry_at=None if entry_at is None else entry_at.astimezone(UTC), min_reward_risk=min_reward_risk)
        check_geometry(direction, entry, stop, target)
    sign = Decimal(1) if direction is Direction.LONG else Decimal(-1)
    unit = ins.currency                          # per unit; money is its ISO code (contracts v1 section 2)
    atr = feature("atr", ATR_PERIOD)
    bars = complete_bars(reader.bars(dataset_id, ins.instrument_id, as_of=at, limit=atr.definition.lookback + 5))
    first_needed = at
    for _ in range(GAP_LOOKBACK + 1):            # the opens of the last GAP_LOOKBACK + 1 sessions, for session gaps
        first_needed = calendar.open_close(calendar.previous_session(first_needed.astimezone(UTC).date()))[0]
    gap_bars = complete_bars(reader.bars(dataset_id, ins.instrument_id, as_of=at, start=first_needed))
    limitations, safe_for = labels(manifest)
    last = bars[-1] if bars else None
    stale, behind = (is_stale(calendar, manifest.interval, last.end, at, max_staleness_bars) if last
                     else (True, 0))

    def pnl(price: Decimal | None, why: str = "") -> MetricValue:
        if price is None:
            return MetricValue(value=None, unit=unit, unavailable_reason=why or "no price")
        return MetricValue(value=(price - entry) * sign, unit=unit)

    def px(price: Decimal | None, why: str) -> MetricValue:
        return (MetricValue(value=price, unit=ins.currency) if price is not None
                else MetricValue(value=None, unit=ins.currency, unavailable_reason=why))

    scenarios: list[ExitScenario] = []
    risks: list[str] = []
    risk_per_unit = (entry - stop) * sign if stop is not None else None
    if stop is not None:
        scenarios.append(ExitScenario(name="stop", trigger=f"price trades through {stop}", price=px(stop, ""),
                                      pnl_per_unit=pnl(stop),
                                      assumptions=("fills exactly at the stop; a real fill can be worse",
                                                   *([notes["stop"]] if "stop" in notes else []))))
        gaps = session_gaps(calendar, gap_bars, GAP_LOOKBACK)
        adverse = [g for g in gaps if g * sign < 0]
        if adverse:
            worst = min(adverse, key=lambda g: g * sign)
            gapped = _against(stop * (Decimal(1) + worst), ins.tick_size, direction)
            scenarios.append(ExitScenario(
                name="gap through stop", trigger="the session opens beyond the stop", price=px(gapped, ""),
                pnl_per_unit=pnl(gapped),
                assumptions=(f"the worst adverse session gap of the last {len(gaps)} sessions "
                             f"({worst.quantize(Decimal('1e-6'))}) happens past the stop; a stop order fills at the "
                             "open, not at the stop",)))
        else:
            why = f"no adverse session gap in the last {len(gaps)} sessions; one can still happen"
            scenarios.append(ExitScenario(name="gap through stop", trigger="the session opens beyond the stop",
                                          price=px(None, why), pnl_per_unit=pnl(None, why)))
        risks.append("A stop is not a guaranteed price: the market can open beyond it.")
    else:
        risks.append(f"{ErrorCode.NO_PROTECTION.value}: no stop price, so this plan's loss has no bound.")
    if target is not None:
        reward = (target - entry) * sign
        r = f"; {(reward / risk_per_unit).quantize(Decimal('0.01'))}R against the stop" if risk_per_unit else ""
        scenarios.append(ExitScenario(name="target", trigger=f"price trades through {target}", price=px(target, ""),
                                      pnl_per_unit=pnl(target),
                                      assumptions=(f"fills exactly at the target{r}",
                                                   *([notes["target"]] if "target" in notes else []))))
    if flat_at is not None:
        why = "the price at the flat-by time is not known in advance"
        scenarios.append(ExitScenario(
            name="time exit", trigger=f"out by {flat_at.isoformat()}, win or lose", price=px(None, why),
            pnl_per_unit=pnl(None, why), assumptions=(f"flat_by {flat_by or exit_rules.DEFAULTS['flat_by']} New "
                                                      "York" + (f", {flat_note}" if flat_note else ""),)))
    risks.extend(warnings)
    if last is None:
        why_last = "no complete bars"
    elif stale:
        why_last = (f"{ErrorCode.DATA_STALE.value}: the newest complete close ended {last.end.isoformat()}, "
                    f"{behind} bar(s) behind")
        risks.append(why_last)
        limitations = (*limitations, Limitation(code=ErrorCode.DATA_STALE.value, detail=why_last))
    else:
        why_last = ""
    mark = last.close if last is not None and not stale else None
    scenarios.append(ExitScenario(
        name="exit at the last close", trigger=f"close as of {at.isoformat()}",
        price=px(mark, why_last), pnl_per_unit=pnl(mark, why_last),
        assumptions=("marks the position at the newest complete close; not a fill",)))
    a = atr.compute(bars)[-1] if bars else None
    one_atr = _against(entry - sign * a, ins.tick_size, direction) if a is not None else None
    why_atr = f"atr({ATR_PERIOD}) needs {atr.definition.min_history} complete bars, has {len(bars)}"
    scenarios.append(ExitScenario(
        name="one ATR against", trigger=f"price moves atr({ATR_PERIOD}) of {manifest.interval.value} bars against "
                                        "the entry",
        price=px(one_atr, why_atr), pnl_per_unit=pnl(one_atr, why_atr),
        assumptions=(f"atr({ATR_PERIOD}) = {a}" if a is not None else why_atr,)))
    risks.append("Hypothetical only: nothing here is an order, and no order is sent.")

    # A time exit's price is in the future by definition: its absence is not missing data.
    complete = all(s.price.value is not None for s in scenarios if s.name != "time exit") and not stale
    return with_hash(ExitPlan(
        plan_id=stable_id("XP", dataset_hash(manifest), ins.instrument_id, at.isoformat(), direction.value, entry,
                          stop, target, *([flat_at.isoformat()] if flat_at is not None else [])),
        dataset_id=dataset_id, dataset_hash=dataset_hash(manifest), instrument_id=ins.instrument_id,
        symbol=ins.symbol, as_of=at, direction=direction, entry_price=entry,
        scenarios=tuple(scenarios), risks=tuple(risks),
        completeness=Completeness.COMPLETE if complete else Completeness.PARTIAL,
        limitations=(*limitations, HYPOTHETICAL), safe_for=safe_for, created_at=now_utc(now)))
