"""The Falsification Engine, registry fs/1 (spec 10 I03): ten named challenges, each pass, fail, not_applicable,
not_performed_due_to_data or error. A required challenge that cannot run never counts as a pass.

The MATCHED BASELINE (FS01, FS08): random-entry controls. Each control enters at random with the strategy's own
entry rate on each instrument, then exits by the strategy's own exit rules and stop, with the same costs, cash and
sizing. So it is in the market about as often and as long as the strategy, and a strategy that is merely long a
rising market does not beat it. The strategy's edge is its daily return in excess of the controls' mean.
"""
from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from traderos.backtest import CostModel, SimResult, simulate
from traderos.contracts import (
    AccountAssumptions,
    Bar,
    ChallengeOutcome,
    ChallengeResult,
    MetricValue,
    decimal_from_float,
    with_hash,
)
from traderos.research import statistics as stats
from traderos.strategies import conditions as cond
from traderos.strategies.rulebook import Rulebook, compile_rules

REGISTRY_VERSION = "fs/1"
CONTROLS = 8
REQUIRED = ("FS01", "FS02", "FS04", "FS08", "FS09", "FS10")   # must PASS for candidate_for_paper (ADR-005)
NAMES = {
    "FS01": "a no-edge negative control does not qualify",
    "FS02": "the edge survives doubled and tripled costs",
    "FS03": "nearby parameters behave alike",
    "FS04": "the sealed holdout agrees",
    "FS05": "the edge holds in different market regimes",
    "FS06": "the result does not rest on a few winners (diagnostic)",
    "FS07": "a more conservative fill keeps the edge",
    "FS08": "the strategy beats a matched baseline, with uncertainty",
    "FS09": "later data cannot change earlier decisions (look-ahead poisoning)",
    "FS10": "every trial is recorded and within budget",
}


@dataclass
class Context:
    """Everything a challenge may read: the frozen rules, the bars, the windows, the costs and the runs so far."""
    book: Rulebook
    bars: Mapping[str, Sequence[Bar]]
    in_sample: tuple[date, date]
    holdout: tuple[date, date] | None
    account: AccountAssumptions
    costs: CostModel
    seed: int
    experiment_id: str
    main: SimResult
    main_summary: stats.Summary
    holdout_run: SimResult | None
    holdout_compromised: bool
    trials_recorded: int
    trials_attempted: int
    trial_budget: int
    id_suffix: str = ""                           # "-r2"... on a verification replay
    controls: list[SimResult] = field(default_factory=list)
    neighbor_runs: list[tuple[dict, SimResult]] = field(default_factory=list)

    def run(self, book: Rulebook | None = None, *, window: tuple[date, date] | None = None,
            costs: CostModel | None = None, fill_model: str = "next_open/1",
            entry_override: Mapping[str, Sequence[bool | None]] | None = None) -> SimResult:
        start, end = window or self.in_sample
        return simulate(book or self.book, self.bars, start=start, end=end, account=self.account,
                        costs=costs or self.costs, fill_model=fill_model, run_id=f"{self.experiment_id}-x",
                        entry_override=entry_override)


def _m(v, unit="fraction", reason=None) -> MetricValue:
    if v is None:
        return MetricValue(value=None, unit=unit, unavailable_reason=reason or "not available")
    d = v if isinstance(v, Decimal) else decimal_from_float(v, 8)
    return MetricValue(value=d, unit=unit) if d is not None else MetricValue(value=None, unit=unit,
                                                                             unavailable_reason="not finite")


def random_entries(book: Rulebook, bars: Mapping[str, Sequence[Bar]], *, seed: int,
                   window: tuple[date, date]) -> dict[str, list[bool | None]]:
    """Entry series at random with the strategy's own entry rate per instrument inside the window (seeded)."""
    rng = random.Random(seed)
    out: dict[str, list[bool | None]] = {}
    for symbol in book.universe:
        series = list(bars.get(symbol, ()))
        if not series:
            continue
        real = cond.evaluate_series(book.entry, series)
        inside = [i for i, b in enumerate(series) if window[0] <= b.end.date() <= window[1]]
        rate = sum(1 for i in inside if real[i] is True) / len(inside) if inside else 0.0
        out[symbol] = [None] * len(series)
        for i in inside:
            out[symbol][i] = rng.random() < rate
    return out


def ensure_controls(ctx: Context) -> list[SimResult]:
    if not ctx.controls:
        for k in range(CONTROLS):
            override = random_entries(ctx.book, ctx.bars, seed=ctx.seed * 1000 + k, window=ctx.in_sample)
            ctx.controls.append(ctx.run(entry_override=override))
    return ctx.controls


def control_daily(controls: Sequence[SimResult]) -> list[float]:
    series = [stats.daily_returns(c) for c in controls]
    return [sum(day) / len(day) for day in zip(*series, strict=True)]


def excess_ci(run: SimResult, baseline: Sequence[float], seed: int) -> tuple[float, float, float] | None:
    daily = stats.daily_returns(run)
    if len(daily) < stats.MIN_OBSERVATIONS or run.exposure_sessions == 0:
        return None
    excess = [a - b for a, b in zip(daily, baseline, strict=True)]
    lo, hi, _ = stats.stationary_bootstrap_mean(excess, seed=seed)
    return sum(excess) / len(excess), lo, hi


def _result(ctx: Context, cid: str, outcome: ChallengeOutcome, finding: str, *, metrics=None, scope="in-sample",
            adverse="") -> ChallengeResult:
    return with_hash(ChallengeResult(
        result_id=f"CR-{ctx.experiment_id}-{cid}{ctx.id_suffix}", challenge_id=cid, registry_version=REGISTRY_VERSION,
        experiment_id=ctx.experiment_id, hypothesis=NAMES[cid], scope=scope, outcome=outcome, finding=finding,
        adverse_scenario=adverse, metrics=metrics or {}, created_at=datetime.now(UTC)))


# ---- the ten ---------------------------------------------------------------------------------------------------
def fs01(ctx: Context) -> ChallengeResult:
    controls = ensure_controls(ctx)
    probe = ctx.run(entry_override=random_entries(ctx.book, ctx.bars, seed=ctx.seed * 1000 + 999,
                                                  window=ctx.in_sample))
    got = excess_ci(probe, control_daily(controls), ctx.seed + 1)
    if got is None:
        return _result(ctx, "FS01", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA,
                       "The control never traded or the window is too short.")
    mean, lo, hi = got
    qualified = lo > 0
    return _result(ctx, "FS01", ChallengeOutcome.FAIL if qualified else ChallengeOutcome.PASS,
                   ("A random-entry control QUALIFIED against the matched baseline: this test cannot tell an edge "
                    "from noise here." if qualified else "A random-entry control did not qualify, as it should not."),
                   metrics={"control_excess_mean_daily": _m(mean), "control_ci95_low": _m(lo),
                            "control_ci95_high": _m(hi)})


def fs02(ctx: Context) -> ChallengeResult:
    base = control_daily(ensure_controls(ctx))
    out, metrics = ChallengeOutcome.PASS, {}
    for k in (Decimal(2), Decimal(3)):
        run = ctx.run(costs=ctx.costs.scaled(k))
        got = excess_ci(run, base, ctx.seed + int(k))
        metrics[f"net_return_{k}x"] = _m(run.final_equity / run.starting_cash - 1)
        metrics[f"excess_mean_daily_{k}x"] = _m(got[0] if got else None, reason="too few sessions or no trades")
        if k == 2 and (got is None or got[0] <= 0):
            out = ChallengeOutcome.FAIL
    finding = ("The edge over the matched baseline stays positive at 2x costs." if out is ChallengeOutcome.PASS
               else "At 2x costs the edge over the matched baseline is gone.")
    return _result(ctx, "FS02", out, finding, metrics=metrics, adverse="spread, slippage and fees doubled and tripled")


def _numbers(node: Any, path=()):
    """Every numeric knob in a rules document: feature periods and decimal thresholds, with their paths."""
    if isinstance(node, dict):
        for k, v in node.items():
            if (k in ("sma", "ema", "rsi", "atr", "ret", "rvol", "relvol", "highest", "lowest") and isinstance(v, int) \
                    and not isinstance(v, bool)) or (k == "bars_held" and isinstance(v, int)):
                yield (*path, k), v
            else:
                yield from _numbers(v, (*path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, (int, str)) and not isinstance(v, bool) and path and path[-1] in ("above", "below"):
                try:
                    Decimal(str(v))
                    yield (*path, i), v
                except ArithmeticError:
                    pass
            else:
                yield from _numbers(v, (*path, i))


def _set(doc: Any, path, value):
    import copy
    out = copy.deepcopy(doc)
    node = out
    for p in path[:-1]:
        node = node[p]
    node[path[-1]] = value
    return out


def fs03(ctx: Context) -> ChallengeResult:
    knobs = list(_numbers({"entry": ctx.book.document["entry"], "exit": ctx.book.document["exit"]}))[:4]
    if not knobs:
        return _result(ctx, "FS03", ChallengeOutcome.NOT_APPLICABLE, "The rules have no numeric parameter to move.")
    base = control_daily(ensure_controls(ctx))
    tried, kept = [], 0
    for path, value in knobs:
        for factor in (Decimal("0.8"), Decimal("1.2")):
            if isinstance(value, int):
                new = max(1, int((Decimal(value) * factor).to_integral_value()))
                if new == value:
                    new = value + (1 if factor > 1 else -1) if value > 1 or factor > 1 else value
            else:
                new = str((Decimal(str(value)) * factor).normalize())
            doc = dict(ctx.book.document)
            sub = _set({"entry": doc["entry"], "exit": doc["exit"]}, path, new)
            doc["entry"], doc["exit"] = sub["entry"], sub["exit"]
            try:
                neighbor = compile_rules(doc)
            except Exception:                    # a neighbor outside the grammar's bounds is skipped, and said
                tried.append({"path": "/".join(map(str, path)), "value": str(new), "result": "invalid"})
                continue
            run = ctx.run(book=neighbor)
            ctx.neighbor_runs.append(({"path": "/".join(map(str, path)), "value": str(new)}, run))
            got = excess_ci(run, base, ctx.seed + len(tried))
            ok = got is not None and got[0] > 0
            kept += ok
            tried.append({"path": "/".join(map(str, path)), "value": str(new), "result": "edge" if ok else "no edge"})
    valid = [t for t in tried if t["result"] != "invalid"]
    if not valid:
        return _result(ctx, "FS03", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA, "No neighbor could be run.")
    share = Decimal(kept) / len(valid)
    out = ChallengeOutcome.PASS if share >= Decimal("0.75") else ChallengeOutcome.FAIL
    finding = f"{kept} of {len(valid)} neighbors kept a positive edge: " + "; ".join(
        f"{t['path']}={t['value']} {t['result']}" for t in tried)
    return _result(ctx, "FS03", out, finding, metrics={"neighbors_with_edge": _m(share, "fraction")},
                   adverse="each numeric parameter moved 20% down and up")


def fs04(ctx: Context) -> ChallengeResult:
    if ctx.holdout is None or ctx.holdout_run is None:
        return _result(ctx, "FS04", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA, "No holdout window.", scope="holdout")
    if ctx.holdout_compromised:
        return _result(ctx, "FS04", ChallengeOutcome.FAIL, "This holdout was already opened by an earlier experiment "
                       "in the same research family: it is not unseen data (HOLDOUT_COMPROMISED).", scope="holdout")
    controls = [ctx.run(window=ctx.holdout, entry_override=random_entries(ctx.book, ctx.bars, seed=ctx.seed * 7 + k,
                                                                         window=ctx.holdout)) for k in range(CONTROLS)]
    got = excess_ci(ctx.holdout_run, control_daily(controls), ctx.seed + 4)
    if got is None or not ctx.holdout_run.trades:
        return _result(ctx, "FS04", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA,
                       "The holdout is too short or the strategy never traded in it.", scope="holdout")
    mean, lo, hi = got
    out = ChallengeOutcome.PASS if mean > 0 else ChallengeOutcome.FAIL
    return _result(ctx, "FS04", out, ("The holdout's edge over its matched baseline is positive, as in-sample."
                                      if mean > 0 else "In the holdout the edge over the matched baseline is not "
                                      "positive."), scope="holdout",
                   metrics={"holdout_excess_mean_daily": _m(mean), "holdout_ci95_low": _m(lo),
                            "holdout_ci95_high": _m(hi),
                            "holdout_trades": _m(Decimal(len(ctx.holdout_run.trades)), "trades")})


def fs05(ctx: Context) -> ChallengeResult:
    lead = next((s for s in ctx.book.universe if s in ctx.bars), None)
    if lead is None:
        return _result(ctx, "FS05", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA, "No bars.")
    from traderos.features import feature
    series = list(ctx.bars[lead])
    sma = feature("sma", 200).compute(series)
    regime = {b.end.date(): (None if s is None else b.close > s) for b, s in zip(series, sma, strict=True)}
    up = [t for t in ctx.main.trades if regime.get(t.entry_signal_at.date()) is True]
    down = [t for t in ctx.main.trades if regime.get(t.entry_signal_at.date()) is False]
    if len(up) < 5 or len(down) < 5:
        return _result(ctx, "FS05", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA,
                       f"Too few trades in one regime to compare ({len(up)} with {lead} above its 200-session "
                       f"average, {len(down)} below; 5 each needed).")
    e_up = sum((t.net_pnl for t in up), Decimal(0)) / len(up)
    e_down = sum((t.net_pnl for t in down), Decimal(0)) / len(down)
    out = ChallengeOutcome.PASS if e_up > 0 and e_down > 0 else ChallengeOutcome.FAIL
    return _result(ctx, "FS05", out, f"Average trade P&L {e_up:.2f} with {lead} above its 200-session average "
                   f"({len(up)} trades), {e_down:.2f} below ({len(down)} trades).",
                   metrics={"expectancy_up": _m(e_up, "USD"), "expectancy_down": _m(e_down, "USD")})


def fs06(ctx: Context) -> ChallengeResult:
    trades = sorted(ctx.main.trades, key=lambda t: t.net_pnl, reverse=True)
    if len(trades) < 10:
        return _result(ctx, "FS06", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA, "Fewer than 10 trades.")
    cut = max(1, len(trades) // 10)
    total = sum((t.net_pnl for t in trades), Decimal(0))
    rest = sum((t.net_pnl for t in trades[cut:]), Decimal(0))
    top_share = (total - rest) / total if total > 0 else None
    out = ChallengeOutcome.PASS if rest > 0 else ChallengeOutcome.FAIL
    return _result(ctx, "FS06", out, f"Without its best {cut} trades the strategy's P&L is {rest:.2f} "
                   f"(with them {total:.2f}). Diagnostic: a positively skewed strategy can legitimately fail this.",
                   metrics={"pnl_without_top_decile": _m(rest, "USD"),
                            "top_decile_share": _m(top_share, "fraction", "total P&L not positive")})


def fs07(ctx: Context) -> ChallengeResult:
    run = ctx.run(fill_model="next_close/1")
    got = excess_ci(run, control_daily(ensure_controls(ctx)), ctx.seed + 7)
    if got is None:
        return _result(ctx, "FS07", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA, "Too short or no trades.")
    out = ChallengeOutcome.PASS if got[0] > 0 else ChallengeOutcome.FAIL
    return _result(ctx, "FS07", out, "Filling at the next CLOSE instead of the next open "
                   + ("keeps" if got[0] > 0 else "removes") + " the edge over the matched baseline.",
                   metrics={"excess_mean_daily_next_close": _m(got[0])}, adverse="fills one session later in the day")


def fs08(ctx: Context) -> ChallengeResult:
    controls = ensure_controls(ctx)
    got = excess_ci(ctx.main, control_daily(controls), ctx.seed + 8)
    if got is None:
        return _result(ctx, "FS08", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA,
                       "Too few sessions or the strategy never traded.")
    mean, lo, hi = got
    out = ChallengeOutcome.PASS if lo > 0 else ChallengeOutcome.FAIL
    return _result(ctx, "FS08", out, (f"Mean daily excess over {CONTROLS} random-entry controls is {mean:.6f}, "
                                      f"95% interval {lo:.6f} to {hi:.6f} (stationary bootstrap)."
                                      + (" The interval is above zero." if lo > 0 else
                                         " The interval includes or is below zero: no demonstrated edge.")),
                   metrics={"excess_mean_daily": _m(mean), "ci95_low": _m(lo), "ci95_high": _m(hi),
                            "controls": _m(Decimal(CONTROLS), "runs")})


def fs09(ctx: Context) -> ChallengeResult:
    """Poison every bar after a cut and check no decision at or before the cut changes (QT01, live, on these rules)."""
    rng = random.Random(ctx.seed + 9)
    for symbol in ctx.book.universe:
        series = list(ctx.bars.get(symbol, ()))
        if len(series) < 20:
            continue
        cut = rng.randrange(len(series) // 2, len(series) - 1)
        before = cond.evaluate_series(ctx.book.entry, series)[:cut + 1]
        poisoned = series[:cut + 1] + [replace(b, open=b.open * 3, high=b.high * 3, low=b.low * 3, close=b.close * 3,
                                               volume=b.volume * 7) for b in series[cut + 1:]]
        after = cond.evaluate_series(ctx.book.entry, poisoned)[:cut + 1]
        if before != after:
            return _result(ctx, "FS09", ChallengeOutcome.FAIL,
                           f"Changing {symbol}'s bars after {series[cut].end:%Y-%m-%d} changed an earlier decision: "
                           "LOOKAHEAD_RISK.")
    return _result(ctx, "FS09", ChallengeOutcome.PASS, "Tripling every later price and volume changed no earlier "
                   "decision, on every instrument.", adverse="all bars after a random cut multiplied")


def fs10(ctx: Context) -> ChallengeResult:
    ok = ctx.trials_recorded == ctx.trials_attempted and ctx.trials_attempted <= ctx.trial_budget
    return _result(ctx, "FS10", ChallengeOutcome.PASS if ok else ChallengeOutcome.FAIL,
                   f"{ctx.trials_recorded} of {ctx.trials_attempted} selectable trials recorded, budget "
                   f"{ctx.trial_budget}; {len(ctx.neighbor_runs)} FS03 neighbors and {len(ctx.controls)} controls "
                   "ran as diagnostics and cannot be selected.",
                   metrics={"trials_recorded": _m(Decimal(ctx.trials_recorded), "trials"),
                            "trial_budget": _m(Decimal(ctx.trial_budget), "trials")})


CHALLENGES: dict[str, Callable[[Context], ChallengeResult]] = {
    "FS09": fs09, "FS01": fs01, "FS08": fs08, "FS02": fs02, "FS03": fs03, "FS04": fs04, "FS05": fs05, "FS06": fs06,
    "FS07": fs07, "FS10": fs10,
}


def run_all(ctx: Context) -> list[ChallengeResult]:
    out = []
    for cid, fn in CHALLENGES.items():
        try:
            out.append(fn(ctx))
        except Exception as e:                   # a challenge that crashes is an ERROR, never a pass
            out.append(_result(ctx, cid, ChallengeOutcome.ERROR, f"{type(e).__name__}: {e}"))
    return sorted(out, key=lambda r: r.challenge_id)


__all__ = ["CHALLENGES", "CONTROLS", "NAMES", "REGISTRY_VERSION", "REQUIRED", "Context", "control_daily",
           "ensure_controls", "excess_ci", "random_entries", "run_all"]
