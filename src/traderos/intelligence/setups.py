"""The upstream chart-scanner setups, ported from the tank (tank/day-trader-skill/trader-scanner/scan.py at 3d0f4ef).

Brian's rule (11-19): code is rewritten only for a clear flaw or a clearly better method. The SETUPS, their
thresholds, the liquidity floors, the opening-range rules and the minimum history are upstream's, line for line. What
changed, each with its reason:

* **Data: stored bars through the as-of BarReader, not Yahoo's spark and v8 endpoints or Nasdaq Trader's lists.**
  Flaw: spec 06.1 holds Yahoo's unofficial collection until it is cleared, and the network reads had no point-in-time
  guarantee. The universe is the dataset's instruments (the Nasdaq list download is not an approved source).
* **One exchange calendar (traderos.clock XNYS, 2008-2027), not a 2026-2027 holiday table in each file.**
  Flaw: four hand-kept copies, unknown outside two years.
* **Timezone-aware instants (QT01), not naive New York clocks.** Better method: no DST guess at the edges.
* **Decimal prices, not floats.** Flaw: contracts v1 refuse float money.
* **The ORB scan reads real highs in one pass.** The upstream two stages (closes first, then at most 200 v8 charts)
  existed to stay under Yahoo's request limits; with stored bars the stage-2 test runs on every usable chart, and its
  own docstring calls stage 1 a necessary condition of stage 2, so the hits are the same.
* **RSI 14 is traderos.features rsi/1** (Wilder, ten periods of warm-up), so a recompute from the same store gives the
  same value; the upstream depended on how much history the endpoint returned. One difference, a named flaw: a perfectly
  flat window reads 50 here, not the upstream 100 (100 calls a flat series overbought).
* **Liquidity uses the newest complete bar's close and volume.** Upstream read regularMarketPrice/Volume, a live quote
  stored data does not have; it fell back to the last close itself when the quote was missing.

A scan is a list of charts that match a rule, not a list of stocks to buy (the upstream NOTE, kept).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from traderos.contracts import (
    BarReader,
    Completeness,
    DataError,
    DomainError,
    ErrorCode,
    ExchangeCalendar,
    Limitation,
    MetricValue,
    ScanResult,
    ValidationError,
    canonical_bytes,
    sha256_bytes,
    with_hash,
)
from traderos.contracts.intel import ConditionTrace, Eligibility, ScanCandidate
from traderos.contracts.market import Bar, Interval
from traderos.features import feature
from traderos.intelligence._common import (
    complete_bars,
    dataset_hash,
    find_instrument,
    is_stale,
    labels,
    last_close_at_or_before,
    now_utc,
    resolve_as_of,
    stable_id,
    usable,
)
from traderos.intelligence.triggers import _hhmm, local_time

DAILY_SETUPS = ("breakout_20d", "bull_flag", "reclaim_ma20", "oversold", "oversold_bounce")
MIN_DAILY_BARS = 30                              # Upstream: closes (oldest first, >= 30)
DAILY_MIN_PRICE = Decimal(5)                     # Upstream: --min-price 5
DAILY_MIN_VOLUME = Decimal(1_000_000)            # Upstream: --min-volume 1,000,000
ORB_MIN_PRICE = Decimal(5)                       # Upstream: orb --min-price 5 (last close)
ORB_MIN_VOLUME = Decimal(0)                      # Upstream: orb --min-volume 0
ORB_UNTIL = "15:30"                              # Upstream: --until, else no_entries_after, else 15:30 New York
BAR_MIN = 5                                      # Upstream: the ORB scan reads 5-minute bars
OR_MINUTES = 15                                  # Upstream: --or-minutes 15, "the classic"
NOTE = Limitation(code="NOT_A_BUY_LIST", detail="A scan is a list of charts that match a rule, not a list of stocks "
                                                "to buy.")
ILLIQUID = "ILLIQUID"                            # a filter's verdict on a name that was evaluated, not a data error
_ONE = Decimal(1)
_Q = Decimal(1).scaleb(-10)


def _q(x: Decimal) -> Decimal:
    """Ten places, half-even, as traderos.features rounds (the comparisons above read the rounded value)."""
    return x.quantize(_Q, rounding=ROUND_HALF_EVEN)


def _mv(value, unit: str, why: str = "") -> MetricValue:
    return MetricValue(value=value, unit=unit) if value is not None else MetricValue(value=None, unit=unit,
                                                                                    unavailable_reason=why)


def _trace(text: str, passed: bool | None, left: MetricValue, right: MetricValue, refs=(),
           reason="") -> ConditionTrace:
    return ConditionTrace(condition=text, passed=passed, left=left, right=right, feature_refs=tuple(refs),
                          reason=reason)


# ---- the daily setups (the upstream find_setups)
# --------------------------------------------------------------------------

def daily_setups(bars: Sequence[Bar], currency: str) -> tuple[list[str], tuple[ConditionTrace, ...]]:
    """The upstream five setups on complete daily bars (oldest first, at least MIN_DAILY_BARS). Returns the tags that
    matched and one trace per test, so a person sees why each setup did or did not match."""
    c = [b.close for b in bars]
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = _q(sum(c[-20:], Decimal(0)) / 20)
    ma20_prev = _q(sum(c[-21:-1], Decimal(0)) / 20)
    run = _q(c[-6] / c[-16] - _ONE)
    rng5 = _q((max(c[-5:]) - min(c[-5:])) / last)
    rsi_f = feature("rsi", 14)
    # rsi/1 over its declared lookback, exactly the window `scan --criteria` reads, so both scans agree.
    rsi = rsi_f.compute(bars[-rsi_f.definition.lookback:])
    r, r_prev = rsi[-1], rsi[-2]
    px = lambda v: _mv(v, currency)                                            # noqa: E731
    frac = lambda v: _mv(v, "fraction")                                         # noqa: E731
    tags: list[str] = []
    traces: list[ConditionTrace] = []

    def test(tag: str, ok: bool | None, *trs: ConditionTrace) -> None:
        traces.extend(trs)
        if ok:
            tags.append(tag)

    test("breakout_20d", last > hi20,
         _trace("breakout_20d: close > highest close of the 20 before it", last > hi20, px(last), px(hi20)))
    flag_run, flag_rng = run > Decimal("0.10"), rng5 < Decimal("0.05")
    test("bull_flag", flag_run and flag_rng,
         _trace("bull_flag: run over the 10 sessions before the last 5 > 10%", flag_run, frac(run),
                frac(Decimal("0.10"))),
         _trace("bull_flag: range of the last 5 closes / close < 5%", flag_rng, frac(rng5), frac(Decimal("0.05"))))
    below_then = c[-2] < ma20_prev
    above_now = last > ma20
    test("reclaim_ma20", below_then and above_now,
         _trace("reclaim_ma20: previous close < its 20-day average", below_then, px(c[-2]), px(ma20_prev)),
         _trace("reclaim_ma20: close > the 20-day average", above_now, px(last), px(ma20)))
    why = "rsi(14) needs 15 complete bars"
    oversold = None if r is None else r < 30
    test("oversold", oversold,
         _trace("oversold: rsi(14) < 30", oversold, _mv(r, "index_0_100", why), _mv(Decimal(30), "number"),
                ("rsi/1",), "" if r is not None else why))
    prev_oversold = None if r_prev is None else r_prev < 30
    up = last > c[-2]
    bounce = None if prev_oversold is None else prev_oversold and up
    test("oversold_bounce", bounce,
         _trace("oversold_bounce: previous rsi(14) < 30", prev_oversold, _mv(r_prev, "index_0_100", why),
                _mv(Decimal(30), "number"), ("rsi/1",), "" if r_prev is not None else why),
         _trace("oversold_bounce: close > previous close", up, px(last), px(c[-2])))
    return tags, tuple(traces)


# ---- the opening-range breakout (the upstream orb_window, session_bars, usable, orb_hit)
# ----------------------------------

def _orb_session_bars(calendar: ExchangeCalendar, bars: Sequence[Bar], day, cutoff: datetime) -> list[Bar]:
    """`day`'s complete 5-minute bars on the session grid, inside regular hours, finished by `cutoff`. One per slot."""
    open_, close = calendar.open_close(day)
    out: dict[datetime, Bar] = {}
    for b in bars:
        if not b.complete or calendar.session_of(b.start) != day:
            continue
        offset = (b.start - open_).total_seconds()
        if offset < 0 or offset % (BAR_MIN * 60) or b.start >= close:
            continue                             # off the grid, pre-market, after hours
        if b.end > cutoff:
            continue                             # still in progress at the cutoff
        out[b.start] = b
    return [out[k] for k in sorted(out)]


def _orb_usable(calendar: ExchangeCalendar, bars: list[Bar], day, cutoff: datetime, n_range: int) -> bool:
    """Every slot from the open to the cutoff is there, and at least the range. A missing bar is a halt or no
    trades (the upstream rule)."""
    open_, close = calendar.open_close(day)
    want = int((min(cutoff, close) - open_).total_seconds() // (BAR_MIN * 60))
    if want < n_range or len(bars) != want:
        return False
    return all(b.start == open_ + timedelta(minutes=BAR_MIN * k) for k, b in enumerate(bars))


def orb_window(calendar: ExchangeCalendar, at: datetime, or_minutes: int, until: str | None = ORB_UNTIL):
    """(day, cutoff) for an ORB read at `at`, or a typed refusal in the upstream words. `until` is the upstream last
    time for new entries, New York HH:MM (None: no expiry); a read exactly at the close is a replay of the whole
    session."""
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ValidationError("or_minutes wants a multiple of 5 from 5 to 120 (15 is the classic)",
                              code=ErrorCode.INVALID_INPUT)
    day = calendar.session_of(at)
    if day is None:
        closed_day, closed_at = last_close_at_or_before(calendar, at)
        day = closed_day if closed_at == at else None           # Upstream allowed --at 16:00
    if day is None:
        raise DomainError(f"no session at {at.isoformat()}: the market is closed. Run the opening-range scan on a "
                          "trading day after the opening range, or replay a past session with as_of.",
                          code=ErrorCode.OUTSIDE_CAPABILITY)
    open_, close = calendar.open_close(day)
    range_end = open_ + timedelta(minutes=or_minutes)
    first_close = range_end + timedelta(minutes=BAR_MIN)
    cutoff = at.replace(second=0, microsecond=0)
    if cutoff < first_close:
        raise DomainError(f"the {or_minutes}-minute opening range runs to {range_end.isoformat()} and the first bar "
                          f"after it closes at {first_close.isoformat()}; run again after that",
                          code=ErrorCode.INSUFFICIENT_EVIDENCE)
    last_entry = local_time(open_, _hhmm(until, "until")) if until is not None else None
    if last_entry is not None and cutoff > last_entry:
        raise DomainError(f"{cutoff.isoformat()} is after {until} New York, the last time for new entries: an "
                          "opening-range breakout found now is expired. Replay the session with an earlier as_of.",
                          code=ErrorCode.OUTSIDE_CAPABILITY)
    return day, min(cutoff, close)


def orb_hit(bars: list[Bar], n_range: int):
    """(range high, range low, the first completed bar after the range to CLOSE above the range high or None)."""
    rng = bars[:n_range]
    oh, ol = max(b.high for b in rng), min(b.low for b in rng)
    hit = next((b for b in bars[n_range:] if b.close > oh), None)
    return oh, ol, hit


# ---- the scan ---------------------------------------------------------------------------------------------------

def scan_setups(reader: BarReader, calendar: ExchangeCalendar, dataset_id: str, setups: str, *,
                symbols: Sequence[str] | None = None, as_of: datetime | None = None,
                min_price: Decimal | None = None, min_volume: Decimal | None = None, or_minutes: int = OR_MINUTES,
                until: str | None = ORB_UNTIL, max_staleness_bars: int = 1, now: datetime | None = None) -> ScanResult:
    """The upstream `scan.py daily` (setups="daily", daily bars) or `scan.py orb` (setups="orb", 5-minute bars)."""
    if setups not in ("daily", "orb"):
        raise ValidationError(f"setups is 'daily' or 'orb', not {setups!r}", code=ErrorCode.INVALID_INPUT)
    manifest = usable(reader.manifest(dataset_id))
    want = Interval.D1 if setups == "daily" else Interval.M5
    if manifest.interval is not want:
        raise ValidationError(f"the {setups} setups read {want.value} bars; {dataset_id} holds "
                              f"{manifest.interval.value} bars", code=ErrorCode.INVALID_INPUT)
    floor_price = min_price if min_price is not None else (DAILY_MIN_PRICE if setups == "daily" else ORB_MIN_PRICE)
    floor_volume = min_volume if min_volume is not None else (DAILY_MIN_VOLUME if setups == "daily" else ORB_MIN_VOLUME)
    if floor_price < 0 or floor_volume < 0:
        raise ValidationError("liquidity floors cannot be negative", code=ErrorCode.INVALID_INPUT)
    at = resolve_as_of(reader, dataset_id, as_of)
    names = list(symbols) if symbols else [i.symbol for i in manifest.instruments]
    if setups == "orb":
        day, cutoff = orb_window(calendar, at, or_minutes, until)
    candidates, ranked = [], []
    for name in names:
        ins = find_instrument(manifest, name)
        if ins is None:
            candidates.append(ScanCandidate(instrument_id=name, symbol=name, eligibility=Eligibility.UNAVAILABLE,
                                            traces=(), reasons=(ErrorCode.MISSING_DATA.value,)))
            continue
        if setups == "daily":
            bars = complete_bars(reader.bars(dataset_id, ins.instrument_id, as_of=at, limit=200))
            if not bars:
                candidates.append(ScanCandidate(instrument_id=ins.instrument_id, symbol=ins.symbol,
                                                eligibility=Eligibility.UNAVAILABLE, traces=(),
                                                reasons=(ErrorCode.MISSING_DATA.value,)))
                continue
            last = bars[-1]
            price, volume = last.close, last.volume
            reasons = []
            if is_stale(calendar, manifest.interval, last.end, at, max_staleness_bars)[0]:
                reasons.append(ErrorCode.DATA_STALE.value)
            # The upstream order: the liquidity floor first, then the history (it counted a thin, short name as
            # illiquid).
            if price < floor_price or (floor_volume > 0 and volume < floor_volume):
                reasons.append(ILLIQUID)
            if ILLIQUID not in reasons and len(bars) < MIN_DAILY_BARS:
                candidates.append(ScanCandidate(
                    instrument_id=ins.instrument_id, symbol=ins.symbol, eligibility=Eligibility.UNAVAILABLE,
                    traces=(_trace(f"history: at least {MIN_DAILY_BARS} daily closes", False,
                                   _mv(Decimal(len(bars)), "count"), _mv(Decimal(MIN_DAILY_BARS), "count")),),
                    reasons=(*reasons, ErrorCode.MISSING_DATA.value), last_bar_end=last.end,
                    last_bar_available_at=last.available_at))
                continue
            # Upstream does not scan a name under the floor: no setup is reported for it.
            tags, traces = daily_setups(bars, ins.currency) if ILLIQUID not in reasons else ([], ())
        else:
            open_, _ = calendar.open_close(day)
            raw = complete_bars(reader.bars(dataset_id, ins.instrument_id, as_of=cutoff, start=open_))
            bars = _orb_session_bars(calendar, raw, day, cutoff)
            n_range = or_minutes // BAR_MIN
            if not _orb_usable(calendar, bars, day, cutoff, n_range):
                want = int((cutoff - open_).total_seconds() // (BAR_MIN * 60))
                candidates.append(ScanCandidate(
                    instrument_id=ins.instrument_id, symbol=ins.symbol, eligibility=Eligibility.UNAVAILABLE,
                    traces=(_trace("orb: every 5-minute bar from the open to the cutoff is there (a missing bar is a "
                                   "halt or no trades)", False, _mv(Decimal(len(bars)), "count"),
                                   _mv(Decimal(want), "count")),),
                    reasons=(ErrorCode.MISSING_DATA.value,)))
                continue
            last = bars[-1]
            price, volume = last.close, sum((b.volume for b in bars), Decimal(0))
            reasons = [ILLIQUID] if price < floor_price or volume < floor_volume else []
            oh, _ol, hit = orb_hit(bars, n_range)
            shown = hit.close if hit is not None else max(b.close for b in bars[n_range:])
            traces = (_trace(f"orb: a completed bar after the first {or_minutes} minutes closes above the range high"
                             + (f" (first at {hit.end.isoformat()})" if hit is not None else ""),
                             hit is not None, _mv(shown, ins.currency), _mv(oh, ins.currency)),)
            tags = ["orb"] if hit is not None else []
            if reasons:                                          # under the floor: not scanned (upstream)
                tags, traces = [], ()
        # Missing or stale data is UNAVAILABLE; a name evaluated and found under the floor is NOT_ELIGIBLE.
        eligibility = (Eligibility.UNAVAILABLE if any(r != ILLIQUID for r in reasons)
                       else Eligibility.NOT_ELIGIBLE if reasons
                       else Eligibility.ELIGIBLE if tags else Eligibility.NOT_ELIGIBLE)
        cand = ScanCandidate(instrument_id=ins.instrument_id, symbol=ins.symbol, eligibility=eligibility,
                             traces=traces, reasons=tuple(reasons) + tuple(f"setup:{t}" for t in tags),
                             last_bar_end=last.end, last_bar_available_at=last.available_at)
        if eligibility is Eligibility.ELIGIBLE:
            # The upstream order: daily hits by dollars traded, most first; orb hits by when they triggered, earliest
            # first.
            key = (-(price * volume), ins.symbol) if setups == "daily" else (hit.end, ins.symbol)
            ranked.append((key, cand))
        else:
            candidates.append(cand)
    ordered = [c for _, c in sorted(ranked, key=lambda x: x[0])] + candidates
    judged = [c for c in ordered if c.eligibility in (Eligibility.ELIGIBLE, Eligibility.NOT_ELIGIBLE)]
    completeness = (Completeness.COMPLETE if judged and len(judged) == len(ordered)
                    else Completeness.UNAVAILABLE if not judged else Completeness.PARTIAL)
    limitations, safe_for = labels(manifest)
    spec = {"setups": setups, "symbols": names, "min_price": floor_price, "min_volume": floor_volume,
            "or_minutes": or_minutes if setups == "orb" else None, "until": until if setups == "orb" else None,
            "source": "upstream/scan.py@3d0f4ef"}
    criteria_hash = sha256_bytes(canonical_bytes(spec))
    return with_hash(ScanResult(
        scan_id=stable_id("SC", dataset_hash(manifest), criteria_hash, at.isoformat()), dataset_id=dataset_id,
        dataset_hash=dataset_hash(manifest), criteria_hash=criteria_hash, as_of=at, provider=manifest.provider,
        feed=manifest.feed, candidates=tuple(ordered),
        no_eligible_setup=not any(c.eligibility is Eligibility.ELIGIBLE for c in ordered),
        completeness=completeness, limitations=(*limitations, NOTE), safe_for=safe_for, created_at=now_utc(now)))


_ = DataError                                    # refusals for a missing dataset come from the reader itself
