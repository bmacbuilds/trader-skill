"""S03 Market Pulse: timestamped, feed-aware context (spec 12.1 S03).

* Each instrument's last close and its change over `lookback_bars`, flagged stale against the exchange calendar.
* **Breadth only when coverage truly allows it.** The share of instruments that rose is a market reading only with
  at least BREADTH_MIN fresh instruments; below that it is unavailable with the reason, never invented from seven ETFs.
* **Regime, observable at as_of only** (method `sma_slope/1`): on the benchmark, close against its SMA(50) and that
  SMA's change over 10 bars. Uptrend when both point up, downtrend when both point down, range when they disagree,
  unknown when there is not enough history. `unknown` is a valid answer.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from traderos.contracts import (
    BarReader,
    Completeness,
    ErrorCode,
    ExchangeCalendar,
    MarketContext,
    MetricValue,
    with_hash,
)
from traderos.contracts.intel import InstrumentMove, RegimeReading
from traderos.features import feature
from traderos.intelligence._common import (
    bars_behind,
    complete_bars,
    dataset_hash,
    expected_last_end,
    find_instrument,
    is_stale,
    labels,
    now_utc,
    resolve_as_of,
    stable_id,
    staleness_bound,
    usable,
)

BREADTH_MIN = 20
REGIME_SMA = 50
REGIME_SLOPE_BARS = 10
# The first one the dataset holds is the benchmark. TUTMKT is the tutorial's synthetic broad market, so the bundled
# tutorial shows a regime too; anything read from it stays TUTORIAL_ONLY.
BENCHMARKS = ("SPY", "QQQ", "IWM", "TUTMKT")


def _unavailable(unit: str, why: str) -> MetricValue:
    return MetricValue(value=None, unit=unit, unavailable_reason=why)


def market_context(reader: BarReader, calendar: ExchangeCalendar, dataset_id: str, *, as_of: datetime | None = None,
                   lookback_bars: int = 1, max_staleness_bars: int = 1, benchmark: str | None = None,
                   now: datetime | None = None) -> MarketContext:
    staleness_bound(max_staleness_bars)
    manifest = usable(reader.manifest(dataset_id))
    at = resolve_as_of(reader, dataset_id, as_of)
    expected = expected_last_end(calendar, manifest.interval, at)
    ret = feature("ret", lookback_bars)
    moves, fresh_changes = [], []
    for ins in manifest.instruments:
        bars = complete_bars(reader.bars(dataset_id, ins.instrument_id, as_of=at, limit=lookback_bars + 6))
        if not bars:
            moves.append(InstrumentMove(instrument_id=ins.instrument_id, symbol=ins.symbol,
                                        last_close=_unavailable(ins.currency, "no bars available"),
                                        change_pct=_unavailable("fraction", "no bars available"),
                                        lookback_bars=lookback_bars, stale=True))
            continue
        last = bars[-1]
        stale = bars_behind(calendar, manifest.interval, last.end, expected) > max_staleness_bars - 1
        change = ret.compute(bars)[-1]
        change_mv = (MetricValue(value=change, unit="fraction") if change is not None
                     else _unavailable("fraction", f"needs {lookback_bars + 1} complete bars"))
        moves.append(InstrumentMove(instrument_id=ins.instrument_id, symbol=ins.symbol,
                                    last_close=MetricValue(value=last.close, unit=ins.currency),
                                    change_pct=change_mv, lookback_bars=lookback_bars, last_bar_end=last.end,
                                    stale=stale))
        if not stale and change is not None:
            fresh_changes.append(change)

    if len(fresh_changes) >= BREADTH_MIN:
        breadth = MetricValue(value=(Decimal(sum(1 for c in fresh_changes if c > 0)) / len(fresh_changes))
                              .quantize(Decimal("0.0001")), unit="fraction")
    else:
        breadth = _unavailable("fraction", f"{len(fresh_changes)} fresh instruments; breadth needs at least "
                                                  f"{BREADTH_MIN} before it describes a market")

    regime = _regime(reader, calendar, manifest, dataset_id, at, benchmark, max_staleness_bars)
    limitations, safe_for = labels(manifest)
    judged = [m for m in moves if not m.stale and m.change_pct.value is not None]
    completeness = (Completeness.COMPLETE if len(judged) == len(moves) and moves
                    else Completeness.UNAVAILABLE if not judged else Completeness.PARTIAL)
    return with_hash(MarketContext(
        context_id=stable_id("MC", dataset_hash(manifest), at.isoformat(), lookback_bars, regime.method),
        dataset_id=dataset_id, dataset_hash=dataset_hash(manifest), as_of=at, provider=manifest.provider,
        feed=manifest.feed, moves=tuple(moves), breadth=breadth, regime=regime, completeness=completeness,
        limitations=limitations, safe_for=safe_for, created_at=now_utc(now)))


def _regime(reader, calendar, manifest, dataset_id, at, benchmark, max_staleness_bars) -> RegimeReading:
    method = f"sma_slope/1 (sma {REGIME_SMA}, slope over {REGIME_SLOPE_BARS} bars)"
    names = (benchmark,) if benchmark else BENCHMARKS
    ins = next((i for i in (find_instrument(manifest, n) for n in names) if i is not None), None)
    if ins is None:
        ccy = manifest.instruments[0].currency if manifest.instruments else "USD"
        return RegimeReading(label="unknown", method=method,
                             inputs={"close": _unavailable(ccy, f"no benchmark: none of {', '.join(names)} is in "
                                                                "the dataset")})
    need = REGIME_SMA + REGIME_SLOPE_BARS
    # Running bars are dropped BEFORE the count, so one live bar never costs the regime its history.
    bars = complete_bars(reader.bars(dataset_id, ins.instrument_id, as_of=at, limit=need + 5))[-need:]
    if bars:
        stale, behind = is_stale(calendar, manifest.interval, bars[-1].end, at, max_staleness_bars)
        if stale:
            why = (f"{ErrorCode.DATA_STALE.value}: {ins.symbol}'s newest complete bar ended "
                   f"{bars[-1].end.isoformat()}, {behind} bar(s) behind")
            return RegimeReading(label="unknown", method=method, inputs={"close": _unavailable(ins.currency, why)})
    sma = feature("sma", REGIME_SMA).compute(bars)
    now_sma = sma[-1] if sma else None
    then_sma = sma[-1 - REGIME_SLOPE_BARS] if len(sma) > REGIME_SLOPE_BARS else None
    close = bars[-1].close if bars else None
    short = f"needs {need} complete bars of {ins.symbol}, has {len(bars)}"
    inputs = {
        "close": (MetricValue(value=close, unit=ins.currency) if close is not None
                  else _unavailable(ins.currency, short)),
        f"sma_{REGIME_SMA}": (MetricValue(value=now_sma, unit=ins.currency) if now_sma is not None
                              else _unavailable(ins.currency, short)),
        "sma_slope": (MetricValue(value=now_sma - then_sma, unit=ins.currency)
                      if now_sma is not None and then_sma is not None else _unavailable(ins.currency, short)),
    }
    if close is None or now_sma is None or then_sma is None:
        return RegimeReading(label="unknown", inputs=inputs, method=method)
    above, rising = close > now_sma, now_sma > then_sma
    label = "uptrend" if above and rising else "downtrend" if not above and not rising else "range"
    return RegimeReading(label=label, inputs=inputs, method=method)
