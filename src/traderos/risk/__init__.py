"""Hypothetical sizing and risk (S15, S16), the paper ticket (S17) and No-Trade records (I05).

The adapter between the upstream ported rules and the TraderOS records (ADR-009):
  * traderos.risk.size: the upstream plan-mode sizing (the four limits, the smallest wins, whole shares, never up);
  * traderos.risk.check: the upstream check() rules (format, asset, short, size, position, cash, stop, loss,
    reward:risk, daily loss, trades, window, pdt, duplicates, the daily order limit), with the upstream PRs and the
    named changes;
  * traderos.risk.tickets: the upstream ticket (plain words, an explicit yes for one ticket, and since ADR-012 its
    order half: the exact arguments, the result and the fill);
  * traderos.risk.policy: the person's limits, the upstream config keys, never defaulted;
  * traderos.risk.weights: the upstream weights mode, carried unchanged and NOT wired (S21 is P2).

The assessment is CALCULATIONS ONLY (spec 12.2 C): no broker is read and everything about the account is what the
person states, and is labelled so. Since the owner's ruling of 2026-11-20 (ADR-012) a ticket can be sent, after the
person's explicit yes, to their Alpaca PAPER account only (traderos.adapters.alpaca_paper); live is refused in code.
`flatten` is the upstream flatten ticket: a market sell of what you hold, checked by its check() as an exit.

Kept from the earlier TraderOS module (each still tested):
  * THE FOUR MEASURES (spec 13.2): planned loss at the stop (the upstream max loss), stressed loss (a gap through the
    stop by the stated stress), theoretical worst case (a long to zero) and the exposure status;
  * UNKNOWN EXPOSURE IS UNKNOWN (RISK-003, S16): no account statement, no log of today's orders, a signed-off ticket
    with no outcome, or an unknown loss in the log: UNKNOWN_EXPOSURE, never zero, and no ticket;
  * NO STOP, NO PROTECTION: without a stop the loss is not bounded; the plan is refused (NO_PROTECTION);
  * typed codes: each of the upstream reasons maps by its prefix to a spec 05.3 code (REASON_CODES);
  * A REFUSED PLAN IS REMEMBERED as a NoTradeRecord with its reasons (I05), never counted as a trade.

Since the review of #10 (ADR-009): the ticket carries the plan's exact prices, never rounded (item 1); the sizing is
given the account's total and what you hold, so it sizes to the stricter of the budget and the account as the check
measures them (LOW b); and a plan that states the v1.0 fields the assessment no longer reads (quantity, equity,
settled or reserved cash, risk_per_trade, exposure, whole_units, prices_observed_at), or a currency other than USD,
is refused instead of silently ignored (LOW a).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from traderos.contracts import (
    AuthorizationError,
    Completeness,
    DataError,
    Direction,
    DomainError,
    ErrorCode,
    ExposureStatus,
    HypotheticalRiskAssessment,
    MetricValue,
    NoTradeRecord,
    PaperTicket,
    RiskPlan,
    SafeFor,
    ValidationError,
    canonical_bytes,
    record_hash,
    sha256_bytes,
    with_hash,
)
from traderos.contracts.canonical import parse_utc
from traderos.contracts.common import Limitation, Severity
from traderos.risk import tickets as _ticket
from traderos.risk.check import account as account_value
from traderos.risk.check import aware, exposure, load_prices, price_text, report, to_eastern
from traderos.risk.check import check as run_check
from traderos.risk.policy import exact, number
from traderos.risk.policy import policy as read_policy
from traderos.risk.size import bracket_order, plan_report, price_notes, size_trade

CENT = Decimal("0.01")
DEFAULT_STRESS = Decimal("0.05")                 # a gap 5% beyond the stop
DISCLAIMER = ("Paper trading only: TraderOS sends a ticket only to your Alpaca PAPER account, after your explicit yes "
              "for it. It never places a live order.")

# The upstream reasons start with the name of the check that gave them. First match wins; anything unlisted is
# NOT_AUTHORIZED (a rule of the person's policy refused it), so a new reason can never pass as permission.
REASON_CODES = (
    ("exposure:", ErrorCode.UNKNOWN_EXPOSURE),
    ("daily loss: total risk from today's entries is unknown", ErrorCode.UNKNOWN_EXPOSURE),
    ("stop: no stop", ErrorCode.NO_PROTECTION),
    ("window: the exchange calendar", ErrorCode.CALENDAR_UNKNOWN),
    ("price: no price", ErrorCode.MISSING_DATA),
    ("price:", ErrorCode.DATA_STALE),
    ("format:", ErrorCode.INVALID_INPUT),
    ("stop:", ErrorCode.INVALID_INPUT),
    ("target:", ErrorCode.INVALID_INPUT),
    ("asset:", ErrorCode.OUTSIDE_CAPABILITY),
    ("short:", ErrorCode.NOT_AUTHORIZED),
    ("blocked:", ErrorCode.NOT_AUTHORIZED),
    ("size:", ErrorCode.NOT_AUTHORIZED),
    ("sizing:", ErrorCode.NOT_AUTHORIZED),
    ("position:", ErrorCode.NOT_AUTHORIZED),
    ("cash:", ErrorCode.NOT_AUTHORIZED),
    ("loss:", ErrorCode.NOT_AUTHORIZED),
    ("reward:risk:", ErrorCode.NOT_AUTHORIZED),
    ("daily loss:", ErrorCode.NOT_AUTHORIZED),
    ("trades:", ErrorCode.NOT_AUTHORIZED),
    ("window:", ErrorCode.NOT_AUTHORIZED),
    ("pdt:", ErrorCode.NOT_AUTHORIZED),
    ("duplicate:", ErrorCode.NOT_AUTHORIZED),
    ("daily limit:", ErrorCode.NOT_AUTHORIZED),
)


def code_of(reason: str) -> ErrorCode:
    for prefix, code in REASON_CODES:
        if reason.startswith(prefix):
            return code
    return ErrorCode.NOT_AUTHORIZED


@dataclass
class Evaluation:
    """Everything one assessment computed: the record, and the upstream sizing and check for the words."""
    plan: RiskPlan
    assessment: HypotheticalRiskAssessment
    policy_used: dict[str, Any]
    sizing: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    checked: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


def _money(x: Decimal, currency: str) -> MetricValue:
    return MetricValue(value=Decimal(x).quantize(CENT, rounding=ROUND_HALF_EVEN), unit=currency)


def _none(unit: str, why: str) -> MetricValue:
    return MetricValue(value=None, unit=unit, unavailable_reason=why)


def _plain(value: Any) -> Any:
    """Plain JSON for a hash: a float a caller passed becomes the Decimal it was written as."""
    if isinstance(value, float):
        d = number(value)
        return d if d is not None else repr(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, bool, Decimal)) or value is None:
        return value
    return str(value)


def _digest(value: Any) -> str:
    return sha256_bytes(canonical_bytes(_plain(value)))


def policy_digest(checked: dict[str, Any]) -> str:
    """The hash an assessment records as its policy_hash, of a checked policy (`ticket place` compares them)."""
    return _digest(checked)


def plan_dict(plan: RiskPlan) -> dict[str, Any]:
    """The plan as the upstream plan.json fields."""
    return {"symbol": plan.symbol, "side": "buy" if plan.direction is Direction.LONG else "sell",
            "date": plan.plan_date.isoformat() if plan.plan_date else None, "entry": plan.entry_price,
            "stop": plan.stop_price, "target": plan.target_price, "multiplier": plan.multiplier,
            "tick_size": plan.tick_size}


# The v1.0 RiskPlan fields the assessment does not read since ADR-009 (the limits are in the policy, the account in
# the account statement), with the value that means "not stated". The CLI refuses them as plan keys; the API refused
# nothing and silently ignored them (review LOW a).
V1_FIELDS = {"quantity": None, "account_equity": None, "settled_cash": None, "reserved_cash": Decimal(0),
             "risk_per_trade": None, "existing_exposure": None, "whole_units": True, "prices_observed_at": None}


def check_plan_fields(plan: RiskPlan) -> None:
    """Refuse a plan that states what the assessment would ignore, or a currency the policy's limits are not in."""
    stated = sorted(k for k, unset in V1_FIELDS.items() if getattr(plan, k) != unset)
    if stated:
        raise ValidationError(f"RiskPlan field(s) {', '.join(stated)} are not read since contracts v1.1 (ADR-009): "
                              "the size comes from your policy, the account from your account statement, prices "
                              "from a prices file; leave them unset")
    if plan.currency != "USD":
        raise ValidationError(f"the plan is in {plan.currency}, and your policy's limits are U.S. dollars "
                              "(max_loss_per_trade_usd, ...): only a USD plan can be measured against them",
                              code=ErrorCode.OUTSIDE_CAPABILITY)


def split_account(account: dict[str, Any] | None, approvals=()) -> tuple[dict | None, dict | None]:
    """An account statement -> (the upstream portfolio, the upstream log). `approvals` is the journal's part of the log,
    oldest first (the upstream broker.json): approvals, results (placed, denied, failed) and fills. It goes first, so an
    outcome the person states for a ticket ("ticket": id) resolves it. As the upstream `result` copied them, a stated
    outcome inherits the approved ticket's qty, kind, max loss and stop when it does not state them. A stated outcome
    the journal already holds (the same ticket and status) is not counted twice."""
    if account is None:
        return None, None
    portfolio = {"settled_cash": account["settled_cash"], "reserved_cash": account["reserved_cash"],
                 "positions": account["positions"]}
    if account.get("unsettled_cash") is not None:          # in the account's total, never spendable (review item 7)
        portfolio["unsettled_cash"] = account["unsettled_cash"]
    by_ticket = {a["fingerprint"]: a for a in approvals if a.get("status") == "approved" and a.get("fingerprint")}
    journal = {(a.get("fingerprint"), a.get("status")) for a in approvals if a.get("fingerprint")}
    stated = []
    for r in account["orders"]:
        rec = dict(r)
        if rec.get("ticket") and (rec["ticket"], rec.get("status")) in journal:
            continue
        if rec.get("ticket"):
            rec["fingerprint"] = rec["ticket"]
            appr = by_ticket.get(rec["ticket"])
            for k in ("qty", "kind", "max_loss", "stop"):
                if appr is not None and rec.get(k) is None and appr.get(k) is not None:
                    rec[k] = appr[k]
        stated.append(rec)
    return portfolio, {"orders": [*approvals, *stated]}


@exact
def evaluate(plan: RiskPlan, policy_cfg: Any, *, account: dict[str, Any] | None = None,
             prices: dict[str, Any] | None = None, approvals=(), now: datetime | None = None,
             stress_gap: Decimal = DEFAULT_STRESS) -> Evaluation:
    """assess_hypothetical_plan (spec 04.4): the upstream sizing and check, then the four measures and every refusal."""
    if not (0 <= stress_gap < 1):
        raise ValidationError("stress_gap is a fraction from 0 to 1")
    check_plan_fields(plan)
    now = aware(now or plan.as_of)
    c = read_policy(policy_cfg)
    today = to_eastern(now).date().isoformat()
    price_list, times = load_prices(prices) if prices is not None else ({}, {})
    portfolio, state = split_account(account, approvals)
    cur = plan.currency
    limits: list[Limitation] = [Limitation(code="HYPOTHETICAL", detail="Calculated from what you stated; no broker "
                                           "or account was read. Nothing here places an order.")]
    details: list[tuple[ErrorCode, str]] = []
    sizing = order = checked = None
    notes: list[str] = []
    if plan.stop_price is None:
        details.append((ErrorCode.NO_PROTECTION, "stop: no stop. The size comes from the loss at the stop, and "
                        "without a stop the loss is not bounded"))
    else:
        price, notes = price_notes(prices, plan.symbol, plan.entry_price, now, plan.limit_price)
        cash = total = held = None
        if portfolio is not None:
            acct, _ = account_value(c, portfolio, price_list)
            cash = acct["cash"]
            total = acct["cash"] + acct["unsettled"] + sum(acct["values"].values(), Decimal(0))
            held = acct["values"].get(plan.symbol, Decimal(0))
        sizing = size_trade(plan_dict(plan), c, limit=plan.limit_price, price=price, cash=cash, today=today,
                            account_total=total, held_value=held)
        order = bracket_order(sizing)
        if sizing["refusal"]:
            details.append((ErrorCode.NOT_AUTHORIZED, "sizing: " + sizing["refusal"]))
        for lim in sizing["limits"]:
            limits.append(Limitation(code="SIZE_LIMIT", severity=Severity.INFO,
                                     detail=f"{lim['name']}: {lim['how']} = {lim['shares']}"
                                            + (" (binds)" if lim["binds"] else "")))
    if order is not None:
        checked = run_check([order], c, portfolio, price_list, state, today, now, times)
        result = checked["results"][0]
        details += [(code_of(r), r) for r in result["reasons"]]
        limits += [Limitation(code="CHECK_WARNING", detail=w) for w in checked["warnings"] + result["warnings"]]
    else:
        reasons, _, unknown, _, warns = exposure(portfolio, state, today)
        details += [(code_of(r), r) for r in reasons]
        if unknown:
            reason = ("daily loss: total risk from today's entries is unknown ({}); reconcile the trade log before "
                      "a new entry".format(", ".join(unknown)))
            details.append((code_of(reason), reason))
        limits += [Limitation(code="CHECK_WARNING", detail=w) for w in warns]
    limits += [Limitation(code="PRICE", severity=Severity.INFO, detail=n) for n in notes]
    if account is not None:
        limits.append(Limitation(code="EXPOSURE_STATED", detail="existing exposure is what you stated in your "
                                 "account statement, not read from any broker"))
    limits += [Limitation(code=code.value, detail=text, severity=Severity.BLOCKING) for code, text in details]

    # the four measures
    if sizing is None:
        quantity = _none("units", "no stop: the size comes from the loss at the stop")
        planned = _none(cur, "no stop: the loss is not bounded by a plan")
        stressed = _none(cur, "no stop")
        worst = _none(cur, "quantity unknown: no stop to size from")
    else:
        qty, ref, m = Decimal(sizing["qty"]), sizing["entry"], plan.multiplier
        quantity = MetricValue(value=qty, unit="units")
        planned = _money(sizing["max_loss"], cur)
        gapped = sizing["stop"] * (1 - stress_gap)
        stressed = _money(abs(ref - gapped) * m * qty, cur)
        worst = _money(ref * m * qty, cur)
    codes = sorted({code.value for code, _ in details})
    exposure_status = ExposureStatus.UNKNOWN if ErrorCode.UNKNOWN_EXPOSURE.value in codes else ExposureStatus.KNOWN
    if plan.limit_price is not None:
        fresh_words = f"a limit order: sized at your limit {price_text(plan.limit_price)}"
    elif sizing is not None and sizing["entry_from"] == "price":
        fresh_words = notes[0] if notes else "sized at a fresh latest price"
    elif notes:
        fresh_words = notes[0]
    else:
        fresh_words = "no fresh latest price: sized at the plan's entry"
    freshness = "plan for {} (today is {} in New York); {}".format(plan.plan_date or "no date", today, fresh_words)
    valid_until = None
    if checked is not None:
        valid_until = parse_utc(checked["results"][0]["valid_until"])
    complete = all(x.value is not None for x in (quantity, planned, stressed)) and \
        exposure_status is ExposureStatus.KNOWN
    plan_hash = record_hash(plan)
    policy_hash = _digest(c)
    account_hash = _digest({"portfolio": portfolio, "log": state}) if account is not None else None
    prices_hash = _digest(prices) if prices is not None else None
    assessment = with_hash(HypotheticalRiskAssessment(
        assessment_id="RA-" + sha256_bytes(canonical_bytes([plan_hash, stress_gap, policy_hash, account_hash,
                                                             prices_hash]))[:20],
        plan_id=plan.plan_id, plan_hash=plan_hash, quantity=quantity, planned_loss=planned, stressed_loss=stressed,
        theoretical_worst_case=worst, exposure_status=exposure_status, input_freshness=freshness,
        refusal_reasons=tuple(codes), ticket_eligible=not codes,
        completeness=Completeness.COMPLETE if complete else Completeness.PARTIAL, limitations=tuple(limits),
        safe_for=(SafeFor.HYPOTHETICAL_PLANNING,), created_at=now, valid_until=valid_until,
        policy_hash=policy_hash, account_hash=account_hash))
    return Evaluation(plan=plan, assessment=assessment, policy_used=c, sizing=sizing, order=order, checked=checked,
                      notes=notes)


def out_of_range(e: ArithmeticError) -> ValidationError:
    """A number no input bound caught (a 1E-99 tick or multiplier, an overflow) is a refusal of the input, typed."""
    return ValidationError(f"a number in the plan, policy, account or prices is out of range ({type(e).__name__}); "
                           "nothing was assessed")


def assess(plan: RiskPlan, policy_cfg: Any, **kw) -> HypotheticalRiskAssessment:
    """The assessment, or a typed refusal: an arithmetic failure is never an untyped crash (review of #10, second
    round: `api.assess_hypothetical_plan` with a tick_size or multiplier of 1E-99 raised decimal.InvalidOperation)."""
    try:
        return evaluate(plan, policy_cfg, **kw).assessment
    except ArithmeticError as e:
        raise out_of_range(e) from None


def ticket(plan: RiskPlan, assessment: HypotheticalRiskAssessment, *, now: datetime | None = None,
           superseded_by: str | None = None) -> PaperTicket:
    """S17: a paper ticket for an eligible, unexpired, latest assessment. Drafting sends nothing; only `traderos
    ticket place`, after an explicit yes, sends it, and only to an Alpaca paper account (ADR-012)."""
    now = aware(now)
    if assessment.plan_hash != record_hash(plan):
        raise ValidationError("this assessment is not of this plan", code=ErrorCode.INTEGRITY_MISMATCH)
    if not assessment.ticket_eligible:
        code = ErrorCode(assessment.refusal_reasons[0]) if assessment.refusal_reasons and \
            assessment.refusal_reasons[0] in ErrorCode.__members__ else ErrorCode.NOT_AUTHORIZED
        raise AuthorizationError("no ticket: " + ", ".join(assessment.refusal_reasons), code=code)
    ready, label = _ticket.status_of(assessment, None, now, superseded_by)
    if not ready:
        raise DataError("no ticket: " + label.removeprefix("NOT READY: "), code=ErrorCode.DATA_STALE)
    cur = plan.currency
    stop = plan.stop_price                      # the exact prices the risk was measured at (review item 1)
    return with_hash(PaperTicket(disclaimer=DISCLAIMER,
        ticket_id=f"PT-{assessment.assessment_id[3:]}", assessment_id=assessment.assessment_id,
        plan_hash=assessment.plan_hash, symbol=plan.symbol, direction=plan.direction,
        quantity=assessment.quantity.value, order_type="limit" if plan.limit_price is not None else "market",
        limit_price=plan.limit_price, stop_price=stop, target_price=plan.target_price,
        notes=(f"planned loss {assessment.planned_loss.value} {cur} if the stop fills at the stop",
               f"stressed loss {assessment.stressed_loss.value} {cur} if it gaps through the stop",
               f"theoretical worst case {assessment.theoretical_worst_case.value} {cur} (the position to zero)",
               "or copy it into your own broker yourself: TraderOS sends only to an Alpaca paper account"),
        created_at=now))


def ticket_order(t: PaperTicket, assessment: HypotheticalRiskAssessment, plan: RiskPlan | None) -> dict[str, Any]:
    """The ticket as the upstream order fields (its plain words, its yes rules, its fingerprint and tool_input). A
    flatten ticket (an exit) has no RiskPlan: its plan is the order itself."""
    if plan is None:
        return _ticket.order_of(t)
    levels = {"entry": plan.entry_price, "stop": plan.stop_price, "target": plan.target_price}
    return _ticket.order_of(t, assessment.planned_loss.value, plan.multiplier, levels)


@dataclass
class Flatten:
    """The upstream flatten ticket and the records that hold it."""
    assessment: HypotheticalRiskAssessment
    ticket: PaperTicket
    order: dict[str, Any]
    checked: dict[str, Any]
    source: str
    flat_by: str


@exact
def flatten(symbol: str, policy_cfg: Any, *, account: dict[str, Any], prices: dict[str, Any] | None = None,
            log=(), now: datetime | None = None, qty: Any = None, flat_by: str | None = None) -> Flatten:
    """The upstream cmd_flatten: a market sell, by flat_by, of the shares you hold in a symbol (the qty from your
    account statement, or from `qty`), then its check() on it, which passes a sell that only closes what you hold
    whatever the daily limits and the entry window say. In TraderOS a ticket exists only for a checked order (ADR-009),
    so its two steps (flatten writes orders.json, then check.py) are one: a refusal is raised, never ticketed."""
    now = aware(now)
    c = read_policy(policy_cfg)
    sym = str(symbol).strip().upper()
    if not sym:
        raise ValidationError("say which symbol to flatten")
    held_m = next((p["multiplier"] for p in account["positions"]
                   if p["symbol"] == sym and p.get("multiplier") is not None and p["multiplier"] != 1), None)
    if held_m is not None:
        # an Alpaca paper order is a number of shares: 10 contracts of 100 would be sent as 10 shares, and the fill
        # booked at a hundredth of the holding (review of #10, second round; ADR-009)
        raise DomainError(f"no flatten ticket: your account statement holds {sym} at a contract multiplier of "
                          f"{_ticket._s(held_m)}, and an Alpaca paper order is a number of shares, so TraderOS cannot "
                          "send its sell. Close it at your broker, then record the fill: traderos ticket fill "
                          f"--symbol {sym} --side sell --qty Q --price P --account FILE",
                          code=ErrorCode.OUTSIDE_CAPABILITY)
    if qty is not None:
        q = number(qty)
        if q is None or q <= 0:
            raise ValidationError("--qty wants the number of shares you hold, like --qty 8")
        source = "--qty"
    else:
        q = Decimal(0)
        for p in account["positions"]:
            if p["symbol"] == sym:
                q += p["shares"] if "shares" in p else sum((lot["shares"] for lot in p["lots"]), Decimal(0))
        source = "your account statement"
        if q <= 0:
            raise DataError(f"nothing to flatten: no {sym} shares in your account statement. Check your paper account "
                            f"(`traderos ticket account`) and pass --qty if you do hold some",
                            code=ErrorCode.MISSING_DATA)
    flat_by = flat_by or c.get("flat_by") or "15:55"
    order = {"symbol": sym, "side": "sell", "qty": _ticket._str_num(q), "type": "market", "time_in_force": "day",
             "why": f"flat by {flat_by} New York: closes the {sym} day trade before the close (qty from {source})"}
    today = to_eastern(now).date().isoformat()
    price_list, times = load_prices(prices) if prices is not None else ({}, {})
    portfolio, state = split_account(account, log)
    checked = run_check([order], c, portfolio, price_list, state, today, now, times)
    r = checked["results"][0]
    if r["verdict"] != "PASS" or r["kind"] != "exit":
        why = r["reasons"] or ["it would not only close what you hold"]
        raise AuthorizationError("no flatten ticket: " + "; ".join(why), code=code_of(why[0]))
    cur = "USD"
    plan_hash = _digest({k: v for k, v in order.items()})
    policy_hash, account_hash = _digest(c), _digest({"portfolio": portfolio, "log": state})
    prices_hash = _digest(prices) if prices is not None else None
    assessment = with_hash(HypotheticalRiskAssessment(
        assessment_id="RA-" + sha256_bytes(canonical_bytes([plan_hash, policy_hash, account_hash, prices_hash,
                                                             now]))[:20],
        plan_id="FL-" + plan_hash[:20], plan_hash=plan_hash, quantity=MetricValue(value=q, unit="units"),
        planned_loss=_none(cur, "an exit: it closes what you hold, so it adds no loss at a stop"),
        stressed_loss=_none(cur, "an exit"), theoretical_worst_case=_none(cur, "an exit"),
        exposure_status=ExposureStatus.KNOWN, input_freshness=f"flat by {flat_by} New York; qty from {source}",
        refusal_reasons=(), ticket_eligible=True, completeness=Completeness.PARTIAL,
        limitations=(Limitation(code="EXIT", severity=Severity.INFO, detail="a sell that only closes what you hold: "
                                "checked for format, asset and short sales; every other rule is a warning"),
                     *(Limitation(code="CHECK_WARNING", detail=w) for w in checked["warnings"] + r["warnings"])),
        safe_for=(SafeFor.HYPOTHETICAL_PLANNING,), created_at=now, valid_until=parse_utc(r["valid_until"]),
        policy_hash=policy_hash, account_hash=account_hash))
    t = with_hash(PaperTicket(
        ticket_id=f"PT-{assessment.assessment_id[3:]}", assessment_id=assessment.assessment_id, plan_hash=plan_hash,
        symbol=sym, direction=Direction.LONG, kind="exit", quantity=q, order_type="market", time_in_force="day",
        notes=(order["why"],), disclaimer=DISCLAIMER, created_at=now))
    return Flatten(assessment=assessment, ticket=t, order=order, checked=checked, source=source, flat_by=flat_by)


def no_trade(plan: RiskPlan, assessment: HypotheticalRiskAssessment, *, authority: str | None = None,
             now: datetime | None = None) -> NoTradeRecord | None:
    """I05: a refused plan is remembered with its reasons. Returns None when nothing was refused."""
    if not assessment.refusal_reasons:
        return None
    why = [lim.detail for lim in assessment.limitations if lim.severity is Severity.BLOCKING]
    return with_hash(NoTradeRecord(
        record_id=f"NT-{assessment.assessment_id[3:]}", candidate_ref=plan.plan_id,
        decided_at=aware(now or assessment.created_at),
        decision_authority=authority or f"your risk policy {(assessment.policy_hash or '')[:12]}".strip(),
        evidence_refs=(assessment.assessment_id,), rejection_codes=assessment.refusal_reasons,
        rationale="Refused for a ticket: " + "; ".join(why or assessment.refusal_reasons)
        + ". A refusal is never a trade."))


def describe(ev: Evaluation) -> str:
    plan, a = ev.plan, ev.assessment

    def v(m):
        return f"{m.value} {m.unit}" if m.value is not None else f"n/a ({m.unavailable_reason})"
    lines = [f"Hypothetical plan {plan.plan_id}: {plan.direction.value} {plan.symbol} at {plan.entry_price}"
             + (f", stop {plan.stop_price}" if plan.stop_price else ", NO STOP")
             + (f", target {plan.target_price}" if plan.target_price else ""), ""]
    if ev.sizing is not None:
        lines += [plan_report(ev.sizing, ev.order, ev.notes), ""]
    if ev.checked is not None:
        lines += ["Risk check:", report(ev.checked), ""]
    lines += [f"  quantity:              {v(a.quantity)}",
              f"  planned loss:          {v(a.planned_loss)}",
              f"  stressed loss:         {v(a.stressed_loss)}",
              f"  theoretical worst:     {v(a.theoretical_worst_case)}",
              f"  existing exposure:     {a.exposure_status.value}",
              f"  price freshness:       {a.input_freshness}"]
    if a.valid_until is not None:
        lines.append(f"  valid until:           {to_eastern(a.valid_until):%H:%M} New York")
    lines.append("  ticket: " + ("eligible (paper only: nothing is sent without your yes)" if a.ticket_eligible else
                                 "refused: " + ", ".join(a.refusal_reasons)))
    lines += [f"  - [{lim.code}] {lim.detail}" for lim in a.limitations if lim.severity is Severity.BLOCKING]
    return "\n".join(lines)


__all__ = ["DEFAULT_STRESS", "DISCLAIMER", "REASON_CODES", "Evaluation", "Flatten", "assess", "code_of", "describe",
           "evaluate", "flatten", "no_trade", "plan_dict", "policy_digest", "split_account", "ticket",
           "ticket_order"]
