"""The event-ordered daily simulator (spec 09.1-09.3, S10). Research only: it simulates; it cannot trade.

CLOCK (spec 09.1), each session in order:
  1. at the OPEN: fill the intents created at the previous close, at that open plus half the spread and the
     slippage (a buy pays up, a sell gets less); the fee is charged on top. An intent is never filled at a price
     from before it was created;
  2. during the session: a position's hypothetical stop is checked against the bar. If the session OPENS through
     the stop, the fill is that open, not the stop (a gap is not an assured stop price). A stop touched on the same
     bar as its entry is assumed hit (the conservative ordering) and the trade is labelled AMBIGUOUS_BAR_ASSUMPTION;
  3. at the CLOSE: the bar becomes available; positions are marked; entry and exit conditions are evaluated on
     bars available at that instant only; new intents wait for the next open.
A bar missing for a held instrument leaves its mark at the last close and counts a STALE_MARK limitation.

MONEY (spec 09.2/09.3): Decimal throughout; whole shares unless the account allows fractions; a cash account
spends only settled, unreserved cash; a sale settles after `settlement_days` sessions (T+1). Every cash and
position change is a LedgerEntry, and the ledger is reconciled against the final state at the end of the run.

The simulator is deterministic: the same rules, bars, costs and window give the same trades, byte for byte.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, localcontext

from traderos.clock import XNYS
from traderos.contracts import (
    AccountAssumptions,
    Bar,
    ErrorCode,
    IntegrityError,
    LedgerEntry,
    LedgerKind,
    ResearchError,
    ValidationError,
)
from traderos.features import feature
from traderos.strategies import conditions as cond
from traderos.strategies.rulebook import Rulebook

CENT = Decimal("0.01")
BP = Decimal("0.0001")
ZERO = Decimal(0)


@dataclass(frozen=True)
class CostModel:
    """bps_spread_fee/1: a full spread (half paid on each side), slippage per side, and fees."""
    spread_bps: Decimal = Decimal(2)
    slippage_bps: Decimal = Decimal(1)
    fee_per_share: Decimal = ZERO
    fee_bps: Decimal = ZERO
    model_id: str = "bps_spread_fee/1"

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> CostModel:
        known = {"spread_bps", "slippage_bps", "fee_per_share", "fee_bps"}
        unknown = set(params) - known
        if unknown:
            raise ValidationError(f"unknown cost parameter(s) {sorted(unknown)}")
        values = {k: Decimal(str(v)) for k, v in params.items()}
        if any(v < 0 for v in values.values()):
            raise ValidationError("costs cannot be negative")
        return cls(**values)

    def params(self) -> dict[str, str]:
        return {"spread_bps": str(self.spread_bps), "slippage_bps": str(self.slippage_bps),
                "fee_per_share": str(self.fee_per_share), "fee_bps": str(self.fee_bps)}

    def scaled(self, k: Decimal) -> CostModel:
        return replace(self, spread_bps=self.spread_bps * k, slippage_bps=self.slippage_bps * k,
                       fee_per_share=self.fee_per_share * k, fee_bps=self.fee_bps * k)

    def price(self, reference: Decimal, side: int) -> Decimal:
        """The fill price for a reference price: side +1 buys (pays up), -1 sells (receives less)."""
        adj = (self.spread_bps / 2 + self.slippage_bps) * BP
        return (reference * (1 + side * adj)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)

    def fee(self, quantity: Decimal, price: Decimal) -> Decimal:
        return (quantity * self.fee_per_share + quantity * price * self.fee_bps * BP).quantize(
            CENT, rounding=ROUND_HALF_EVEN)


FILL_MODELS = ("next_open/1", "next_close/1")    # next_close/1 is FS07's alternative, more conservative timing


@dataclass
class Trade:
    symbol: str
    instrument_id: str
    entry_signal_at: datetime
    entry_at: datetime
    entry_price: Decimal
    quantity: Decimal
    entry_fee: Decimal
    stop_price: Decimal | None
    exit_signal_at: datetime | None = None
    exit_at: datetime | None = None
    exit_price: Decimal | None = None
    exit_fee: Decimal = ZERO
    exit_reason: str = ""
    bars_held: int = 0
    ambiguous: bool = False
    entry_cost: Decimal = ZERO                   # price x quantity as booked in the ledger, in cents
    exit_gross: Decimal = ZERO                   # the same at the exit

    @property
    def booked_pnl(self) -> Decimal:
        """P&L as the ledger booked it, in whole cents: the ledger must equal this exactly."""
        if self.exit_price is None:
            raise ResearchError("an open trade has no realized P&L")
        return self.exit_gross - self.entry_cost - self.entry_fee - self.exit_fee

    @property
    def cost_basis(self) -> Decimal:
        return self.entry_price * self.quantity

    @property
    def net_pnl(self) -> Decimal:
        if self.exit_price is None:
            raise ResearchError("an open trade has no realized P&L")
        return (self.exit_price - self.entry_price) * self.quantity - self.entry_fee - self.exit_fee

    @property
    def net_return(self) -> Decimal:
        return self.net_pnl / self.cost_basis

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "instrument_id": self.instrument_id, "entry_signal_at": self.entry_signal_at,
                "entry_at": self.entry_at, "entry_price": self.entry_price, "quantity": self.quantity,
                "entry_fee": self.entry_fee, "stop_price": self.stop_price, "exit_signal_at": self.exit_signal_at,
                "exit_at": self.exit_at, "exit_price": self.exit_price, "exit_fee": self.exit_fee,
                "exit_reason": self.exit_reason, "bars_held": self.bars_held, "ambiguous": self.ambiguous,
                "net_pnl": self.net_pnl, "net_return": self.net_return.quantize(Decimal("0.000001"))}


@dataclass
class SimResult:
    trades: list[Trade]
    equity: list[tuple[date, Decimal]]           # end-of-session equity, one row per session in the window
    ledger: list[LedgerEntry]
    exposure_sessions: int
    limitations: dict[str, int] = field(default_factory=dict)
    skipped_entries: dict[str, int] = field(default_factory=dict)
    starting_cash: Decimal = ZERO

    @property
    def final_equity(self) -> Decimal:
        return self.equity[-1][1] if self.equity else self.starting_cash


@dataclass
class _Position:
    trade: Trade
    held: int = 0


@dataclass
class _Intent:
    kind: str                                    # "enter" | "exit"
    symbol: str
    created_at: datetime
    quantity: Decimal = ZERO
    stop_distance: Decimal | None = None


class _ExitEvaluator:
    """Exit conditions at bar i with the position's age: features computed once over the series."""

    def __init__(self, condition: cond.Condition, bars: Sequence[Bar]) -> None:
        self.condition = condition
        self.values = cond._Values(bars, condition)

    def at(self, i: int, held: int) -> bool | None:
        return cond._eval(self.condition, self.values, i, held, None)


def simulate(book: Rulebook, bars_by_symbol: Mapping[str, Sequence[Bar]], *, start: date, end: date,
             account: AccountAssumptions, costs: CostModel, calendar: XNYS | None = None,
             fill_model: str = "next_open/1", run_id: str = "RUN", entry_override: Mapping[str, Sequence[bool | None]]
             | None = None) -> SimResult:
    """Simulate `book` over sessions start..end. `bars_by_symbol` holds each symbol's complete history up to `end`
    (older bars are warm-up for features, never traded). `entry_override` replaces the entry signal series (the
    negative control uses it); everything else is the same machinery."""
    if fill_model not in FILL_MODELS:
        raise ValidationError(f"unknown fill model {fill_model!r}")
    for symbol, bars in bars_by_symbol.items():
        if any(b.interval is not book.frequency for b in bars):
            raise ResearchError(f"{symbol} has bars that are not {book.frequency.value}; the simulator reads one "
                                "bar per session", code=ErrorCode.OUTSIDE_CAPABILITY)
    cal = calendar or XNYS()
    sessions = cal.sessions(start, end)
    if not sessions:
        raise ResearchError("the window has no sessions", code=ErrorCode.INSUFFICIENT_EVIDENCE)
    with localcontext() as ctx:
        ctx.prec = 28
        ctx.rounding = ROUND_HALF_EVEN
        return _run(book, bars_by_symbol, sessions, account, costs, cal, fill_model, run_id, entry_override)


def _run(book, bars_by_symbol, sessions, account, costs, cal, fill_model, run_id, entry_override) -> SimResult:
    symbols = [s for s in book.universe if s in bars_by_symbol]
    series: dict[str, list[Bar]] = {s: sorted(bars_by_symbol[s], key=lambda b: b.end) for s in symbols}
    index: dict[str, dict[date, int]] = {s: {cal.session_containing_close(b.end) or b.end.date(): i
                                             for i, b in enumerate(series[s])} for s in symbols}
    entries = {s: (list(entry_override[s]) if entry_override and s in entry_override
                   else list(cond.evaluate_series(book.entry, series[s]))) for s in symbols}
    exits = {s: _ExitEvaluator(book.exit, series[s]) for s in symbols}
    atr = ({s: feature("atr", book.stop.atr_period).compute(series[s]) for s in symbols} if book.stop else {})

    settled = account.starting_cash
    unsettled: list[tuple[int, Decimal]] = []    # (session index when it settles, amount)
    positions: dict[str, _Position] = {}
    intents: list[_Intent] = []
    trades: list[Trade] = []
    ledger: list[LedgerEntry] = []
    equity: list[tuple[date, Decimal]] = []
    last_close: dict[str, Decimal] = {}
    limits: dict[str, int] = {}
    skipped: dict[str, int] = {}
    exposure = 0
    seq = 0

    def book_entry(kind: LedgerKind, at: datetime, *, symbol: str | None = None, qty: Decimal = ZERO,
                   price: Decimal | None = None, cash: Decimal = ZERO, settled_delta: Decimal = ZERO, ref: str,
                   note: str = "") -> None:
        nonlocal seq
        seq += 1
        ledger.append(LedgerEntry(entry_id=f"{run_id}-L{seq:06d}", run_id=run_id, sequence=seq, at=at, kind=kind,
                                  instrument_id=series[symbol][0].instrument_id if symbol else None,
                                  quantity_delta=qty, price=price, cash_delta=cash, settled_cash_delta=settled_delta,
                                  currency=account.currency, causal_ref=ref, note=note))

    book_entry(LedgerKind.DEPOSIT, cal.open_close(sessions[0])[0], cash=account.starting_cash,
               settled_delta=account.starting_cash, ref="start")

    def unit(q: Decimal) -> Decimal:
        return q if account.fractional_shares else q.to_integral_value(rounding=ROUND_DOWN)

    def close_trade(pos: _Position, at: datetime, ref_price: Decimal, reason: str, k: int, signal_at=None) -> None:
        nonlocal settled
        t = pos.trade
        price = costs.price(ref_price, -1)
        fee = costs.fee(t.quantity, price)
        proceeds = (price * t.quantity - fee).quantize(CENT, rounding=ROUND_HALF_EVEN)
        t.exit_at, t.exit_price, t.exit_fee, t.exit_reason, t.bars_held = at, price, fee, reason, pos.held
        t.exit_gross = (price * t.quantity).quantize(CENT, rounding=ROUND_HALF_EVEN)
        t.exit_signal_at = signal_at
        if account.settlement_days <= 0:
            settled += proceeds
            settled_delta = proceeds
        else:
            unsettled.append((k + account.settlement_days, proceeds))
            settled_delta = ZERO
        book_entry(LedgerKind.FILL, at, symbol=t.symbol, qty=-t.quantity, price=price,
                   cash=t.exit_gross, settled_delta=settled_delta
                   + (fee if account.settlement_days <= 0 else ZERO), ref=f"exit:{t.symbol}:{reason}")
        if fee:
            book_entry(LedgerKind.FEE, at, symbol=t.symbol, cash=-fee,
                       settled_delta=-fee if account.settlement_days <= 0 else ZERO, ref=f"fee:{t.symbol}")
        trades.append(t)

    for k, day in enumerate(sessions):
        opened_at, closed_at = cal.open_close(day)
        # settle sales whose day has come
        due = [(d, a) for d, a in unsettled if d <= k]
        if due:
            unsettled[:] = [(d, a) for d, a in unsettled if d > k]
            amount = sum((a for _, a in due), ZERO)
            settled += amount
            book_entry(LedgerKind.SETTLE, opened_at, settled_delta=amount, ref="settlement")
        # 1. fills at the open (or the close, under next_close/1) of this session
        fill_at = opened_at if fill_model == "next_open/1" else closed_at
        pending, intents = intents, []
        for it in pending:
            i = index[it.symbol].get(day)
            if i is None:                                       # no bar today: an exit waits, an entry lapses
                if it.kind == "exit":
                    intents.append(it)
                else:
                    skipped["no_bar_at_fill"] = skipped.get("no_bar_at_fill", 0) + 1
                continue
            bar = series[it.symbol][i]
            ref = bar.open if fill_model == "next_open/1" else bar.close
            if it.kind == "exit":
                pos = positions.pop(it.symbol, None)
                if pos is not None:
                    close_trade(pos, fill_at, ref, "rule", k, signal_at=it.created_at)
                continue
            price = costs.price(ref, +1)
            qty = min(it.quantity, unit(settled / price)) if settled > 0 else ZERO   # never unsettled or borrowed
            step = Decimal(1) if not account.fractional_shares else Decimal("0.0001")
            while qty > 0 and (price * qty).quantize(CENT) + costs.fee(qty, price) > settled:
                qty -= step
            fee = costs.fee(qty, price) if qty > 0 else ZERO
            if qty < it.quantity and qty > 0:
                skipped["reduced_to_settled_cash"] = skipped.get("reduced_to_settled_cash", 0) + 1
            if qty <= 0:
                skipped["insufficient_settled_cash"] = skipped.get("insufficient_settled_cash", 0) + 1
                continue
            cost = (price * qty).quantize(CENT, rounding=ROUND_HALF_EVEN)
            settled -= cost + fee
            stop = None
            if it.stop_distance is not None:
                stop = (price - it.stop_distance).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
            t = Trade(symbol=it.symbol, instrument_id=bar.instrument_id, entry_signal_at=it.created_at,
                      entry_at=fill_at, entry_price=price, quantity=qty, entry_fee=fee, stop_price=stop,
                      entry_cost=cost)
            positions[it.symbol] = _Position(trade=t, held=0)
            book_entry(LedgerKind.FILL, fill_at, symbol=it.symbol, qty=qty, price=price, cash=-cost,
                       settled_delta=-cost, ref=f"enter:{it.symbol}")
            if fee:
                book_entry(LedgerKind.FEE, fill_at, symbol=it.symbol, cash=-fee, settled_delta=-fee,
                           ref=f"fee:{it.symbol}")
        # 2. stops, against today's bar
        for symbol in list(positions):
            pos = positions[symbol]
            i = index[symbol].get(day)
            if i is None or pos.trade.stop_price is None:
                continue
            bar = series[symbol][i]
            stop = pos.trade.stop_price
            entered_today = pos.trade.entry_at == fill_at and pos.held == 0
            if entered_today and fill_model == "next_close/1":
                continue                                        # filled at the close: today's range came before it
            if bar.low <= stop:
                if bar.open <= stop and not entered_today:
                    ref, why = bar.open, "stop_gap"             # opened through the stop: the open, not the stop
                else:
                    ref, why = stop, "stop"
                if entered_today:
                    pos.trade.ambiguous = True                  # entry and stop on one bar: order unknown
                    limits["AMBIGUOUS_BAR_ASSUMPTION"] = limits.get("AMBIGUOUS_BAR_ASSUMPTION", 0) + 1
                pos.held += 1
                positions.pop(symbol)
                close_trade(pos, opened_at if why == "stop_gap" else closed_at, ref, why, k)
                # an exit intent for this symbol, if any, is now moot
                intents = [x for x in intents if not (x.symbol == symbol and x.kind == "exit")]
        # 3. the close: marks, then decisions on bars available now
        value = settled + sum((a for _, a in unsettled), ZERO)
        for symbol, pos in positions.items():
            i = index[symbol].get(day)
            if i is not None:
                last_close[symbol] = series[symbol][i].close
                pos.held += 1
            else:
                limits["STALE_MARK"] = limits.get("STALE_MARK", 0) + 1
            value += pos.trade.quantity * last_close.get(symbol, pos.trade.entry_price)
        equity.append((day, value.quantize(CENT, rounding=ROUND_HALF_EVEN)))
        if positions:
            exposure += 1
        if k == len(sessions) - 1:
            break                                               # no decision can be filled inside the window
        pending_symbols = {x.symbol for x in intents}
        for symbol in symbols:
            i = index[symbol].get(day)
            if i is None or not series[symbol][i].complete:
                continue
            next_fill = cal.open_close(sessions[k + 1])[0 if fill_model == "next_open/1" else 1]
            if series[symbol][i].available_at > next_fill:      # not usable before the next fill: no decision
                limits["LATE_AVAILABILITY"] = limits.get("LATE_AVAILABILITY", 0) + 1
                continue
            if symbol in positions:
                if symbol not in pending_symbols and exits[symbol].at(i, positions[symbol].held) is True:
                    intents.append(_Intent("exit", symbol, closed_at))
                continue
            if symbol in pending_symbols or entries[symbol][i] is not True:
                continue
            reference = series[symbol][i].close
            target = value * book.fraction_of_equity
            qty = unit(target / costs.price(reference, +1))
            if qty <= 0:
                skipped["position_too_small"] = skipped.get("position_too_small", 0) + 1
                continue
            distance = None
            if book.stop:
                a = atr[symbol][i]
                if a is None:
                    skipped["stop_unavailable"] = skipped.get("stop_unavailable", 0) + 1
                    continue                                    # no stop can be set: no entry (fail closed)
                distance = a * book.stop.atr_multiple
            intents.append(_Intent("enter", symbol, closed_at, quantity=qty, stop_distance=distance))
    # the window ends: open positions are closed at the last close, labelled so
    last_day = sessions[-1]
    _, last_close_at = cal.open_close(last_day)
    for symbol in list(positions):
        pos = positions.pop(symbol)
        close_trade(pos, last_close_at, last_close.get(symbol, pos.trade.entry_price), "window_end", len(sessions))
    if unsettled:
        amount = sum((a for _, a in unsettled), ZERO)
        settled += amount
        book_entry(LedgerKind.SETTLE, last_close_at, settled_delta=amount, ref="settlement at window end")
        unsettled.clear()
    result = SimResult(trades=trades, equity=equity, ledger=ledger, exposure_sessions=exposure, limitations=limits,
                       skipped_entries=skipped, starting_cash=account.starting_cash)
    _reconcile(result, settled)
    if equity:
        equity[-1] = (equity[-1][0], settled.quantize(CENT, rounding=ROUND_HALF_EVEN))
    return result


def _reconcile(result: SimResult, final_cash: Decimal) -> None:
    """Spec 09.3: the ledger must add up to the final state, every unit and every cent."""
    cash = sum((e.cash_delta for e in result.ledger), ZERO)
    units: dict[str | None, Decimal] = {}
    for e in result.ledger:
        units[e.instrument_id] = units.get(e.instrument_id, ZERO) + e.quantity_delta
    expected_cash = result.starting_cash + sum((t.booked_pnl for t in result.trades), ZERO)
    if any(v != 0 for k, v in units.items() if k is not None):
        raise IntegrityError("ledger reconciliation: positions remain after the window closed them all")
    if cash != expected_cash:                   # exact: every amount the ledger books is whole cents
        raise IntegrityError(f"ledger reconciliation: cash {cash} != start + booked P&L {expected_cash}")
    if final_cash != cash:
        raise IntegrityError(f"ledger reconciliation: final cash {final_cash} != ledger {cash}")
    for t in result.trades:                     # the economic P&L differs from the booked one only by rounding
        if abs(t.booked_pnl - t.net_pnl) > CENT:
            raise IntegrityError(f"ledger reconciliation: {t.symbol} booked {t.booked_pnl} vs {t.net_pnl}")


__all__ = ["FILL_MODELS", "CostModel", "SimResult", "Trade", "simulate"]
