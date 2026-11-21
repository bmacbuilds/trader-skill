"""Statistics of a simulated run (spec 09.4, S08). Python computes every number; nothing here is AI-produced.

Rules:
  * a metric the data cannot support is UNAVAILABLE with its reason, never zero (no trades, no losing trades, too
    few observations, zero variance);
  * annualization uses 252 sessions per year on daily data and says so;
  * uncertainty is a dependence-aware STATIONARY BOOTSTRAP of daily excess returns (Politis-Romano), seeded, with
    the method, block length, resample count and seed recorded. Floats are used inside the computation only and
    enter records through `decimal_from_float` (spec 04.3);
  * baselines run through the same simulator with the same costs (spec 09.4 "matching assumptions").
"""
from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from traderos.backtest import SimResult
from traderos.contracts import MetricValue, decimal_from_float

SESSIONS_PER_YEAR = 252
MIN_OBSERVATIONS = 30                            # below this a Sharpe or a bootstrap is not reported (ADR-005)
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_METHOD = "stationary_bootstrap/1"


def _m(value: float | Decimal | None, unit: str, reason: str | None = None, places: int = 6) -> MetricValue:
    if value is None:
        return MetricValue(value=None, unit=unit, unavailable_reason=reason or "not available")
    if isinstance(value, Decimal):
        return MetricValue(value=value.quantize(Decimal(1).scaleb(-places)), unit=unit)
    d = decimal_from_float(value, places)
    if d is None:
        return MetricValue(value=None, unit=unit, unavailable_reason=reason or "not a finite number")
    return MetricValue(value=d, unit=unit)


def daily_returns(result: SimResult) -> list[float]:
    """Session-to-session equity returns, the first from the starting cash."""
    out, prev = [], result.starting_cash
    for _, value in result.equity:
        out.append(float(value / prev - 1) if prev else 0.0)
        prev = value
    return out


def _mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def _stdev(xs: Sequence[float]) -> float:
    m = _mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def block_length(n: int) -> int:
    """Expected block length for the stationary bootstrap: n^(1/3), at least 5 (documented, ADR-005)."""
    return max(5, round(n ** (1 / 3)))


def stationary_bootstrap_mean(xs: Sequence[float], *, seed: int, resamples: int = BOOTSTRAP_RESAMPLES,
                              block: int | None = None) -> tuple[float, float, int]:
    """95% percentile interval of the mean of xs under the stationary bootstrap. -> (low, high, block length)."""
    n = len(xs)
    b = block or block_length(n)
    p = 1.0 / b
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        total, i = 0.0, rng.randrange(n)
        for _ in range(n):
            total += xs[i]
            i = rng.randrange(n) if rng.random() < p else (i + 1) % n
        means.append(total / n)
    means.sort()
    lo = means[int(0.025 * resamples)]
    hi = means[min(resamples - 1, math.ceil(0.975 * resamples) - 1)]
    return lo, hi, b


@dataclass(frozen=True)
class Summary:
    metrics: dict[str, MetricValue]
    daily: list[float]


def summarize(result: SimResult, *, seed: int, baseline_daily: Sequence[float] | None = None,
              bootstrap: bool = True) -> Summary:
    """Every metric of one run. `baseline_daily` (aligned daily returns of a baseline) makes the bootstrap test the
    strategy's EXCESS return over it; without it, the excess is over cash (zero)."""
    daily = daily_returns(result)
    n = len(daily)
    trades = result.trades
    met: dict[str, MetricValue] = {}
    start, end = result.starting_cash, result.final_equity
    met["net_return"] = _m(end / start - 1, "fraction")
    years = n / SESSIONS_PER_YEAR
    if years >= 1 and end > 0:
        met["cagr"] = _m(float(end / start) ** (1 / years) - 1, "fraction")
    else:
        met["cagr"] = _m(None, "fraction", "less than one year of sessions: not annualized")
    met["sessions"] = _m(Decimal(n), "sessions", places=0)
    met["exposure"] = _m(Decimal(result.exposure_sessions) / n if n else None, "fraction", "no sessions")
    # drawdown
    peak, worst, length, longest = float(start), 0.0, 0, 0
    for _, v in result.equity:
        v = float(v)
        if v >= peak:
            peak, length = v, 0
        else:
            length += 1
            longest = max(longest, length)
            worst = min(worst, v / peak - 1)
    met["max_drawdown"] = _m(worst, "fraction")
    met["max_drawdown_sessions"] = _m(Decimal(longest), "sessions", places=0)
    # trades
    met["trades"] = _m(Decimal(len(trades)), "trades", places=0)
    if trades:
        rets = [float(t.net_return) for t in trades]
        wins = [t.net_pnl for t in trades if t.net_pnl > 0]
        losses = [t.net_pnl for t in trades if t.net_pnl < 0]
        met["win_rate"] = _m(Decimal(len(wins)) / len(trades), "fraction")
        met["avg_trade_return"] = _m(_mean(rets), "fraction")
        met["expectancy_per_trade"] = _m(sum((t.net_pnl for t in trades), Decimal(0)) / len(trades), "USD", places=2)
        met["profit_factor"] = (_m(sum(wins, Decimal(0)) / -sum(losses, Decimal(0)), "ratio") if losses
                                else _m(None, "ratio", "no losing trades: profit factor undefined"))
        met["avg_bars_held"] = _m(Decimal(sum(t.bars_held for t in trades)) / len(trades), "bars", places=2)
        traded = sum((t.cost_basis + t.exit_price * t.quantity for t in trades), Decimal(0))
        met["turnover_per_year"] = (_m(traded / start / Decimal(years), "ratio") if years > 0
                                    else _m(None, "ratio", "no sessions"))
    else:
        for k, u in (("win_rate", "fraction"), ("avg_trade_return", "fraction"), ("expectancy_per_trade", "USD"),
                     ("profit_factor", "ratio"), ("avg_bars_held", "bars"), ("turnover_per_year", "ratio")):
            met[k] = _m(None, u, "no trades")
    # risk-adjusted, only where the method is valid
    if n >= MIN_OBSERVATIONS and _stdev(daily) > 0:
        sd = _stdev(daily)
        met["sharpe"] = _m(_mean(daily) / sd * math.sqrt(SESSIONS_PER_YEAR), "ratio")
        downside = [min(x, 0.0) for x in daily]
        dd = math.sqrt(math.fsum(x * x for x in downside) / n)
        met["sortino"] = (_m(_mean(daily) / dd * math.sqrt(SESSIONS_PER_YEAR), "ratio") if dd > 0
                          else _m(None, "ratio", "no down days"))
    else:
        reason = (f"fewer than {MIN_OBSERVATIONS} sessions" if n < MIN_OBSERVATIONS else "returns never varied")
        met["sharpe"] = _m(None, "ratio", reason)
        met["sortino"] = _m(None, "ratio", reason)
    # uncertainty: the mean daily excess return, stationary bootstrap
    if bootstrap and n >= MIN_OBSERVATIONS and result.exposure_sessions > 0:
        base = list(baseline_daily) if baseline_daily is not None else [0.0] * n
        excess = [a - b for a, b in zip(daily, base, strict=True)]
        lo, hi, b = stationary_bootstrap_mean(excess, seed=seed)
        met["excess_mean_daily"] = _m(_mean(excess), "fraction", places=8)
        met["excess_mean_daily_ci95_low"] = _m(lo, "fraction", places=8)
        met["excess_mean_daily_ci95_high"] = _m(hi, "fraction", places=8)
        met["bootstrap_block_length"] = _m(Decimal(b), "sessions", places=0)
    else:
        reason = ("bootstrap not run" if not bootstrap else f"fewer than {MIN_OBSERVATIONS} sessions"
                  if n < MIN_OBSERVATIONS else "never in the market")
        for k in ("excess_mean_daily", "excess_mean_daily_ci95_low", "excess_mean_daily_ci95_high"):
            met[k] = _m(None, "fraction", reason)
        met["bootstrap_block_length"] = _m(None, "sessions", reason)
    return Summary(metrics=met, daily=daily)


__all__ = ["BOOTSTRAP_METHOD", "BOOTSTRAP_RESAMPLES", "MIN_OBSERVATIONS", "SESSIONS_PER_YEAR", "Summary",
           "block_length", "daily_returns", "stationary_bootstrap_mean", "summarize"]
