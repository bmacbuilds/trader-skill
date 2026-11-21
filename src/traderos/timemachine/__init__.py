"""The Time Machine (S11, spec 08.6 Q09, B08): what happened, what could plausibly vary, and what a shock would do.

Three kinds of scenario, never blended:

  OBSERVED   the strategy simulated over the recorded history (the same simulator, costs and cash as the lab);
  RESAMPLED  GENERATED paths: a stationary block bootstrap of the strategy's own observed daily returns over a stated
             horizon. Blocks keep runs of consecutive days together, so streaks and volatility clustering survive; it
             never shuffles single days independently (spec 12.1 S11). It assumes the strategy's own pattern of
             returns persists, which is an assumption, not a forecast. The same paths are reported for three
             block lengths, so the choice's effect is visible;
  STRESS     GENERATED shocks applied to the bars, then re-simulated: a one-day crash gap, doubled volatility and
             five times the slippage. Each says exactly what was changed. Shocked bars are checked to be valid bars.

Deterministic: everything generated comes from the stated seed.

THE HOLDOUT (spec 08.4, QT02): with no window given, the Time Machine reads the development and validation windows
only, and stops before the earliest sealed holdout of the strategy's research family on that dataset: this
version's own default holdout and the holdout of every experiment registered for the family on that dataset (a
revision with a longer warm-up has later default splits, but shares its family's holdouts). The report says which
holdout it stopped before. (It used to run to the holdout's last day while the lab still counted that holdout as
unseen.) A window a person sets that reaches into any of those holdouts is reported with HOLDOUT_READ, and the lab
then counts that holdout as opened (HOLDOUT_COMPROMISED for later experiments of the family on that dataset).

IDENTITY: `report_id` names the inputs (version, dataset, window, horizon, paths, seed, costs, starting cash). The
record's canonical hash also covers `created_at`, so two stores that run the same inputs hold records with the same
report_id and different hashes; within one store the first record stands and a rerun returns it.

The upstream intraday plan replay is `traderos.timemachine.plan` (ADR-010), and its opening-range study is
`traderos.timemachine.orb_study`.
"""
from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from traderos.backtest import CostModel, SimResult, simulate
from traderos.contracts import (
    AccountAssumptions,
    Bar,
    ErrorCode,
    ExperimentSpec,
    Limitation,
    MetricValue,
    ResearchError,
    SplitName,
    StrategyVersion,
    canonical_bytes,
    decimal_from_float,
    sha256_bytes,
    with_hash,
)
from traderos.contracts.research import Scenario, ScenarioKind, TimeMachineReport
from traderos.research import DEFAULT_COSTS
from traderos.research import statistics as stats
from traderos.strategies.rulebook import Rulebook, compile_rules

BLOCKS = (5, 10, 20)
DEFAULT_PATHS = 1000
DEFAULT_HORIZON = 252
CRASH = Decimal("0.10")                          # a 10% gap down at one open, held thereafter
VOL_MULTIPLIER = 2
SLIPPAGE_MULTIPLIER = Decimal(5)


def _m(x, unit="fraction", places=6) -> MetricValue:
    d = decimal_from_float(x, places) if not isinstance(x, Decimal) else x
    return MetricValue(value=d, unit=unit) if d is not None else MetricValue(value=None, unit=unit,
                                                                             unavailable_reason="not finite")


def _max_drawdown(rets: Sequence[float]) -> float:
    level, peak, worst = 1.0, 1.0, 0.0
    for r in rets:
        level *= 1 + r
        peak = max(peak, level)
        worst = min(worst, level / peak - 1)
    return worst


def block_paths(daily: Sequence[float], *, horizon: int, paths: int, block: int, seed: int) -> list[list[float]]:
    """Stationary bootstrap paths of `horizon` days from `daily`: geometric blocks with mean length `block`."""
    n = len(daily)
    if n < 2:
        raise ResearchError("too few observed sessions to resample", code=ErrorCode.INSUFFICIENT_EVIDENCE)
    rng = random.Random(seed)
    p = 1.0 / block
    out = []
    for _ in range(paths):
        i = rng.randrange(n)
        path = []
        for _ in range(horizon):
            path.append(daily[i])
            i = rng.randrange(n) if rng.random() < p else (i + 1) % n
        out.append(path)
    return out


def _distribution(paths: list[list[float]]) -> dict[str, MetricValue]:
    terminal = sorted(math.prod(1 + r for r in p) - 1 for p in paths)
    dds = sorted(_max_drawdown(p) for p in paths)
    k = len(terminal)

    def q(xs, f):
        return xs[min(k - 1, max(0, round(f * (k - 1))))]
    return {"terminal_return_p05": _m(q(terminal, 0.05)), "terminal_return_p50": _m(q(terminal, 0.50)),
            "terminal_return_p95": _m(q(terminal, 0.95)),
            "probability_of_loss": _m(sum(1 for t in terminal if t < 0) / k),
            "max_drawdown_p50": _m(q(dds, 0.50)), "max_drawdown_p05": _m(q(dds, 0.05))}


def _observed_metrics(run: SimResult, seed: int) -> dict[str, MetricValue]:
    m = stats.summarize(run, seed=seed, bootstrap=False).metrics
    return {k: m[k] for k in ("net_return", "cagr", "max_drawdown", "trades", "exposure", "sharpe")}


# ---- stresses on the bars ---------------------------------------------------------------------------------------
def crash(bars: Mapping[str, Sequence[Bar]], on: date, drop: Decimal = CRASH) -> dict[str, list[Bar]]:
    """Every instrument gaps down by `drop` at the open of session `on`, and stays down: every price from that bar
    on is scaled by (1 - drop). Earlier bars are untouched."""
    k = 1 - drop
    return {s: [b if b.end.date() < on else replace(b, open=b.open * k, high=b.high * k, low=b.low * k,
                                                     close=b.close * k) for b in series]
            for s, series in bars.items()}


def amplify(bars: Mapping[str, Sequence[Bar]], start: date, multiplier: int = VOL_MULTIPLIER) -> dict[str, list[Bar]]:
    """From `start`, each close-to-close log move is multiplied by `multiplier`; each bar is rescaled by its new
    close over its old close, so its intrabar shape is kept. Drift is amplified too, and the report says so."""
    out = {}
    for s, series in bars.items():
        new, prev_old, prev_new = [], None, None
        for b in series:
            if b.end.date() < start or prev_old is None:
                new.append(b)
                prev_old, prev_new = b.close, b.close
                continue
            ratio = float(b.close / prev_old)
            close = prev_new * Decimal(repr(math.exp(multiplier * math.log(ratio))))
            f = close / b.close
            new.append(replace(b, open=(b.open * f).quantize(Decimal("0.0001")),
                               high=(b.high * f).quantize(Decimal("0.0001")),
                               low=(b.low * f).quantize(Decimal("0.0001")), close=close.quantize(Decimal("0.0001"))))
            prev_old, prev_new = b.close, new[-1].close
        out[s] = new
    return out


def family_holdouts(store, version: StrategyVersion, dataset_id: str, splits=()) -> list:
    """Every sealed holdout of this strategy's research family on this dataset: this version's own default one
    (`splits`) and the holdout of every experiment registered for the family on that dataset (spec 08.4)."""
    family = f"RF-{version.strategy_id}"
    out = [s for s in splits if s.name is SplitName.HOLDOUT]
    for spec in store.all(ExperimentSpec, limit=None):
        if spec.research_family_id == family and spec.dataset_id == dataset_id:
            out += [s for s in spec.splits if s.name is SplitName.HOLDOUT]
    return out


def holdouts_read(holdouts, start: date, end: date) -> list[tuple[date, date]]:
    """The holdouts a window from `start` to `end` reaches into, as (start, end), each once, in date order. Both
    edges count: a window ending on a holdout's first day, or starting on its last, has read it."""
    return sorted({(h.start, h.end) for h in holdouts if h.start <= end and start <= h.end})


def run(store, version: StrategyVersion, dataset_id: str, *, start: date | None = None, end: date | None = None,
        horizon: int = DEFAULT_HORIZON, paths: int = DEFAULT_PATHS, seed: int = 7, starting_cash: str = "100000",
        costs: dict[str, str] | None = None) -> TimeMachineReport:
    from traderos.clock import XNYS
    from traderos.research import default_splits
    cal = XNYS()
    reader = store.bar_reader()
    manifest = reader.manifest(dataset_id)
    book: Rulebook = compile_rules(version.rules)
    defaulted, end_defaulted = start is None or end is None, end is None
    try:
        splits = default_splits(manifest, book.history_needed(), cal)
    except ResearchError:
        if defaulted:
            raise
        splits = ()
    holdouts = family_holdouts(store, version, dataset_id, splits)
    earliest = min(holdouts, key=lambda h: h.start, default=None)
    if defaulted:
        start = start or splits[0].start
        if end_defaulted:
            end = max(s.end for s in splits if s.name is not SplitName.HOLDOUT)
            if earliest is not None and earliest.start <= end:
                end = cal.previous_session(earliest.start)
        if end < start:
            why = (f"the strategy family's earliest sealed holdout on {dataset_id} starts {earliest.start}"
                   if earliest is not None else f"the window ends {end}, before it starts {start}")
            raise ResearchError(f"{why}: no unsealed window is left after warm-up; give a window",
                                code=ErrorCode.INSUFFICIENT_EVIDENCE)
    if not (1 <= horizon <= 2520) or not (100 <= paths <= 20000):
        raise ResearchError("horizon is 1 to 2520 sessions and paths 100 to 20000",
                            code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
    ids = {i.symbol: i.instrument_id for i in manifest.instruments}
    missing = [s for s in book.universe if s not in ids]
    if missing:
        raise ResearchError(f"{', '.join(missing)} not in {dataset_id}", code=ErrorCode.MISSING_DATA)
    end_close = cal.open_close(end if cal.is_session(end) else cal.previous_session(end))[1]
    bars = {s: list(reader.bars(dataset_id, ids[s], as_of=end_close)) for s in book.universe}
    account = AccountAssumptions(starting_cash=Decimal(starting_cash))
    cost_model = CostModel.from_params(costs or DEFAULT_COSTS)
    observed = simulate(book, bars, start=start, end=end, account=account, costs=cost_model, run_id="TM-observed")
    daily = stats.daily_returns(observed)
    window = f"window {start} to {end}"
    if end_defaulted and earliest is not None:
        window += (f": the development and validation windows, before the earliest sealed holdout of this strategy "
                   f"family on this dataset ({earliest.start} to {earliest.end}), which is not read")
    scenarios = [Scenario(name="observed history", kind=ScenarioKind.OBSERVED,
                          method="the lab's simulator over the recorded bars",
                          assumptions=(window, f"costs {cost_model.params()}",
                                       "next-open fills; cash account, T+1"),
                          metrics=_observed_metrics(observed, seed))]
    if observed.exposure_sessions == 0:
        scenarios.append(Scenario(name="resampled", kind=ScenarioKind.RESAMPLED, method="not run", paths=0,
                                  assumptions=("the strategy never held a position: nothing to resample",)))
    else:
        for b in BLOCKS:
            generated = block_paths(daily, horizon=horizon, paths=paths, block=b, seed=seed * 100 + b)
            scenarios.append(Scenario(
                name=f"resampled, mean block {b} sessions", kind=ScenarioKind.RESAMPLED,
                method=f"stationary_bootstrap/1 of the observed daily strategy returns, mean block {b}, "
                       f"horizon {horizon}, seed {seed * 100 + b}",
                assumptions=("the strategy's own pattern of returns, as observed, persists",
                             "not a forecast: plausible variation of what was observed"),
                paths=paths, metrics=_distribution(generated)))
    sessions = cal.sessions(start, end)
    mid = sessions[len(sessions) // 2]
    held = next((cal.next_session(t.entry_at.date()) for t in observed.trades
                 if t.entry_at.date() >= mid and t.bars_held >= 2), None) or \
        next((cal.next_session(t.entry_at.date()) for t in observed.trades if t.bars_held >= 2), None) or mid
    stresses = (
        ("crash gap", crash(bars, held), cost_model,
         (f"every instrument opens {CRASH * 100:.0f}% lower on {held} and stays lower",
          "the date is the first session after mid-window on which the observed strategy held a position"
          if held != mid else "no position was held across a session: the date is mid-window")),
        ("doubled volatility", amplify(bars, mid), cost_model,
         (f"from {mid}, every daily move is doubled (drift included)",)),
        ("five times the slippage", bars, CostModel.from_params({**cost_model.params(), "slippage_bps": str(
            Decimal(cost_model.params()["slippage_bps"]) * SLIPPAGE_MULTIPLIER)}),
         (f"slippage {SLIPPAGE_MULTIPLIER}x per side, everything else unchanged",)),
    )
    for name, stressed, model, assumptions in stresses:
        result = simulate(book, stressed, start=start, end=end, account=account, costs=model, run_id=f"TM-{name}")
        metrics = _observed_metrics(result, seed)
        metrics["change_vs_observed"] = _m(float(result.final_equity / observed.final_equity - 1))
        scenarios.append(Scenario(name=name, kind=ScenarioKind.STRESS, method="the same simulator on shocked bars",
                                  assumptions=assumptions, metrics=metrics))
    limitations = [
        *manifest.limitations,
        Limitation(code="GENERATED_NOT_FORECAST", detail="Resampled and stressed scenarios are generated from stated "
                   "assumptions; they are not forecasts and not observed history."),
        Limitation(code="RESAMPLING_ASSUMES_PERSISTENCE", detail="Resampling the strategy's own returns assumes its "
                   "observed behaviour persists; a regime it never met is not represented.")]
    for h in holdouts_read(holdouts, start, end):
        limitations.append(Limitation(code="HOLDOUT_READ", detail=f"This window reaches into the sealed holdout "
                                      f"{h[0]} to {h[1]} that the lab uses for this strategy family on this "
                                      "dataset. The lab now counts it as opened: a later experiment of this "
                                      "strategy family on this dataset reports HOLDOUT_COMPROMISED."))
    identity = sha256_bytes(canonical_bytes([version.version_id, dataset_id, start, end, horizon, paths, seed,
                                             cost_model.params(), str(account.starting_cash)]))[:20]
    report = with_hash(TimeMachineReport(
        report_id=f"TM-{identity}", strategy_version_id=version.version_id,
        strategy_semantic_hash=version.semantic_hash, dataset_id=dataset_id, dataset_hash=manifest.canonical_hash,
        window_start=start, window_end=end, horizon_sessions=horizon, seed=seed, scenarios=tuple(scenarios),
        limitations=tuple(limitations), created_at=datetime.now(UTC)))
    if store.find(TimeMachineReport, report.report_id) is None:
        store.put(report)
    return store.get(TimeMachineReport, report.report_id)


def describe(report: TimeMachineReport) -> str:
    def v(m):
        if m is None:
            return "-"
        if m.value is None:
            return "n/a"
        return f"{m.value * 100:.1f}%" if m.unit == "fraction" else str(m.value)
    lines = [f"Time Machine {report.report_id}: {report.strategy_version_id} on {report.dataset_id}, "
             f"{report.window_start} to {report.window_end}"]
    for s in report.scenarios:
        tag = {"observed": "OBSERVED ", "resampled": "GENERATED", "stress": "STRESS   "}[s.kind.value]
        lines.append(f"  [{tag}] {s.name}")
        if s.kind is ScenarioKind.RESAMPLED and s.metrics:
            mm = s.metrics
            lines.append(f"      {s.paths} paths x {report.horizon_sessions} sessions: return p05 "
                         f"{v(mm.get('terminal_return_p05'))}, median {v(mm.get('terminal_return_p50'))}, p95 "
                         f"{v(mm.get('terminal_return_p95'))}; chance of a loss {v(mm.get('probability_of_loss'))}; "
                         f"median worst drawdown {v(mm.get('max_drawdown_p50'))}")
        elif s.metrics:
            mm = s.metrics
            lines.append(f"      net {v(mm.get('net_return'))}, max drawdown {v(mm.get('max_drawdown'))}, trades "
                         f"{v(mm.get('trades'))}" + (f", vs observed {v(mm['change_vs_observed'])}"
                                                    if "change_vs_observed" in mm else ""))
        for a in s.assumptions:
            lines.append(f"      assumes: {a}")
    lines += [f"  limit {lim.code}: {lim.detail}" for lim in report.limitations]
    return "\n".join(lines)


__all__ = ["BLOCKS", "amplify", "block_paths", "crash", "describe", "family_holdouts", "holdouts_read", "run"]
