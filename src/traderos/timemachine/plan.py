"""The upstream Time Machine plan replay, ported from the tank (ADR-010).

Source: tank/day-trader-skill/trader-timemachine/timemachine.py at 3d0f4ef (upstream day-trader-skill, MIT),
with upstream PRs #49, #25 and #53 applied as published, and the Time Machine part of #2 merged by hand: its flat_by
default, its catch-all and its caveats are taken; its Monte Carlo next-open mode is not (it books a positive R on a
stop-out). The review that settled this is OSDev9's upstream PR review v3; the verdict list is the reuse plan's S11.

Brian's rule (11-19): code is rewritten only for a clear flaw or a clearly better method. The upstream plan checks, its
opening-range rule, its stop rules (cents_away, rule_stop, resolve_rule), the history replay, the #25 settled-trade
refusal, #53's walk and one_trade, the verdict and its notes, the report wording and the fan chart are its, line for
line where nothing below names a change. What changed, each with its reason:

* **The replays draw whole past sessions, not one bar at a time** (`simulate`). Flaw: S11 forbids independent-bar
  shuffling. Drawing each 5-minute bar on its own from any past session built paths no session ever produced
  (73.75 % of them were such mixes in the reuse comparison's demo), and the TSLA 5-to-95 range came out about
  22 % too wide. Each replay now walks one past full session's own bars from the entry slot on, re-based at the
  entry price (`session`). A stationary-block variant (`block`, mean 6 and 12 bars) is reported beside it, so the
  effect of the choice is visible (spec 08.6 Q09). The upstream independent draw is kept, unchanged, as
  `simulate_legacy`, only so a test can prove this port reads the same bars the reel read; no result is ever built
  from it.
* **The replays fill like the history** (`simulate` calls #53's `one_trade`). Flaw: the replays filled at the
  entry bar's own close and booked exactly -1R at a stop a bar had gapped through. Now the fill is the next drawn
  bar's open, a next open past the stop or the target is a bracket that is not filled (counted, not traded), a bar
  that opens past the stop exits at that open (QT04, QT05; spec 09.1), and costs come off every R.
* **Costs: each leg on its own price, one default everywhere** (`cost_r`, `DEFAULT_COST_BPS`). Flaw: #53 charged
  both legs on the fill price, and history_replay and baseline defaulted to 0 bps while the run defaulted to 2.
  Now the entry leg is charged on the fill and the exit leg on the exit (QT06), and 2 bps a side is the one
  default. It equals the lab's default per side (half of a 2 bps spread plus 1 bp of slippage).
* **R is kept unrounded for every average** (#53 review change 6). Flaw: averaging Rs already rounded to the cent of
  an R moves the average. Rounding happens only where a number is shown. Signals, fills and rejected brackets are
  reported separately (`signals`, `triggered`, `skipped`).
* **Uncertainty is a stationary bootstrap** (`baseline`: `traderos.research.statistics.stationary_bootstrap_mean`).
  Flaw: #53's iid bootstrap and its "random sets of blind trades" share treat trades on nearby days as
  independent (spec 09.4 asks for dependence-aware methods). The setup's 95 % range and the edge's 95 % range are
  both stationary-bootstrap intervals over the trades in date order. The edge is each trade's R minus the average
  R of its own blind entries, as the upstream backtest.py already defines it; the "blind trades did as well" share is
  replaced by the edge's interval.
* **A bar that touches both the stop and the target is labelled** (from #2's both_touched): it still counts as the
  stop, and the count is reported as AMBIGUOUS_BAR_ASSUMPTION (spec 09.1).
* **Stated stresses** (S11, QT10), copied from the daily Time Machine: a 10 % gap against the position at the first
  bar after the fill, every move doubled, and five times the costs. Each says exactly what it changed.
* **The trade minimum is signed off; the other thresholds are proposals** (ADR-010). The upstream MIN_TRADES (100) is
  kept, and the founder signed it off on 2026-11-20 for the lab and the plan replay alike (ADR-005 is 100 too).
  COIN_FLIP, STRONG, MANY_NEITHER, the interval and the cost stay proposed and the independent quant review is
  pending (spec 09.4): no result is called "strongly" anything, and a baseline line at 100 trades or more says it is
  not a qualification.
* **Data: a stored 5-minute dataset through the as-of BarReader, never Yahoo** (`read_bars`). Flaw: spec 06.1
  holds Yahoo's unofficial endpoint, and a network read has no point-in-time guarantee. A bar the store has not
  made available by `as_of` is never seen; a bar marked incomplete is dropped and counted, and an incomplete entry
  bar is its own refusal (INCOMPLETE_BAR). The upstream parse rules (5-minute grid, regular hours, full 78-bar sessions,
  today left out of the pool) are kept.
* **The pool is every full session the dataset holds before the plan's date.** Upstream's was the 60 days its one
  Yahoo request returned; with a stored dataset the dataset decides how far back it goes. The report and the record
  state the pool's first and last session and how many there are.
* **A bar's open decides first** (`walk`, #53's code). Flaw: the stop's touch was checked before the target's gap,
  so a bar that opened past the target and then traded through the stop was booked as an ambiguous stop. Both gaps
  are now checked before either touch (OSDev6's review of #9).
* **An opening-range plan cannot enter inside its range** (`normalize_plan`, `resolve_rule`). Flaw (the upstream too): a
  plan whose range ends after its entry bar made `resolve_rule` read range bars that closed after the entry, so the
  stop rule and the history changed with the as-of time (look-ahead, QT01; OSDev6's review). Such a plan is refused
  (LOOKAHEAD_RISK), and `resolve_rule` reads only bars closed by the entry.
* **A plan's prices go through Decimal, and a boolean is not a price** (`_num`). Flaw: float(True) read a JSON
  `true` as the price 1.00. A plan file names each key once, and a value the record cannot hold (a non-finite or
  oversized number, anywhere in the plan) is refused before any replay, with its place named.
* **One exchange calendar** (traderos.clock XNYS). Flaw: a hand-kept 2026-2027 early-close table and a hand-rolled
  DST rule in each file.
* **Typed refusals** (`TMError` is a ResearchError). Each refusal keeps the upstream words and gains a code:
  MISSING_DATA (no entry bar, no bars, symbol not in the dataset), INCOMPLETE_BAR, INSUFFICIENT_EVIDENCE (under 20
  sessions), LOOKAHEAD_RISK (#25: the real bars already settled the trade), OUTSIDE_CAPABILITY (not 5-minute bars), and
  INVALID_INPUT for a plan or a setting that does not make sense.
* **The result is a timemachine/1 record** with a plan-replay subject (ADR-010): the history and the blind baseline
  are OBSERVED, the replays RESAMPLED, the stresses STRESS, with the dataset hash and the code version recorded.
  The upstream `--write` file is replaced by the store; its argparse `main` by `traderos timemachine plan`.

IDENTITY: a record's `report_id` names its inputs (the plan's hash, the dataset and its hash, the as-of time, the
end, the replay count, the seed, the cost and the code version). Its canonical hash also covers `created_at`, so two
stores that run the same inputs hold records with the same report_id and different hashes; within one store the
first record stands and a rerun returns it.

A simulation built from a stock's own recent sessions is not a prediction (the upstream note, kept).
"""
from __future__ import annotations

import datetime
import json
import math
import pathlib
import random
import re
import statistics
from decimal import Decimal, InvalidOperation

from traderos import __version__
from traderos.clock import XNYS, eastern_to_utc
from traderos.contracts import (
    Direction,
    ErrorCode,
    Interval,
    Limitation,
    MetricValue,
    PlanReplaySubject,
    ResearchError,
    Scenario,
    ScenarioKind,
    TimeMachineReport,
    ValidationError,
    canonical_bytes,
    decimal_from_float,
    sha256_bytes,
    with_hash,
)
from traderos.research.statistics import BOOTSTRAP_METHOD, stationary_bootstrap_mean

INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = 78    # 9:30 to 16:00 in 5-minute bars
OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
EARLY_MIN = 13 * 60
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring: upstream's, proposed in ADR-010, not signed off yet
STRONG = 0.15
MANY_NEITHER = 0.40
FILLS = ("next_open", "close")       # "close" is old upstream accounting: a labelled legacy comparison, never a result
METHODS = ("session", "block")       # how a replay draws its bars; the upstream independent draw is simulate_legacy
BLOCKS = (6, 12)                     # mean block lengths, in bars, of the sensitivity replays
DEFAULT_COST_BPS = 2.0   # a side: half the spread plus slippage on a liquid stock, a guess to measure, not a fact
BASELINE_DRAWS = 10000
MIN_TRADES = 100     # fewer history trades than this: "too few to tell". Signed off by the founder 2026-11-20
MIN_TRADES_SIGNED_OFF = "signed off by the founder 2026-11-20"   # ADR-010 and ADR-005: the lab's minimum is the same
SIGNED_OFF = False   # ADR-010: coin flip, strongly, many neither, the interval and the cost are still proposed; no
#                     result is called "strongly" anything until they are signed off (independent quant review)
PENDING = "the independent quant review is pending (spec 09.4)"
CRASH = 0.10         # stress: a 10 % gap against the position at the first bar after the fill
VOL_MULTIPLIER = 2   # stress: every move from the previous close doubled in log terms
COST_MULTIPLIER = 5  # stress: five times the costs
MAX_PLAN_BYTES = 1_000_000
FILL_MODEL_ID = "next_open_bracket/1"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class TMError(ResearchError):
    """The upstream TMError, now a typed refusal. The default code is INVALID_INPUT; data refusals name their own."""

    default_code = ErrorCode.INVALID_INPUT


# ---------------------------------------------------------------- time (XNYS, not a hand-kept table)

def parse_now(text):
    """ISO time -> aware UTC. A time without an offset is taken as New York time. None: the real clock."""
    if not text:
        return datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TMError("--as-of wants a time like \"2026-10-19 10:15\" (New York) or ...Z (UTC)") from None
    if dt.tzinfo is None:
        return eastern_to_utc(dt.date(), dt.time())
    return dt.astimezone(datetime.UTC)


def parse_clock(text, what):
    """'10:15 AM', '10:15am', '15:55' or '3:55' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants a New York time like 10:15 AM or 15:55, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock12(minutes):
    h, m = divmod(minutes, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def at_new_york(day, minutes):
    """The UTC instant of a New York wall-clock time on a day."""
    return eastern_to_utc(day, datetime.time(*divmod(minutes, 60)))


def new_york(utc):
    """An aware UTC instant -> naive New York wall-clock time (the XNYS offset rule, not a hand-rolled one)."""
    utc = utc.astimezone(datetime.UTC)
    for hours in (-4, -5):
        local = (utc + datetime.timedelta(hours=hours)).replace(tzinfo=None)
        if eastern_to_utc(local.date(), local.time()) == utc:
            return local
    return (utc + datetime.timedelta(hours=-5)).replace(tzinfo=None)


# ---------------------------------------------------------------- data (the store, as of a time)

def read_bars(reader, dataset_id, sym, as_of, cal):
    """A stored 5-minute dataset, as of `as_of` -> (manifest, instrument, bars, dropped, incomplete).
    Regular-session bars only, oldest first, as the upstream dicts. Dropped (the upstream rules, counted): bars off the
    5-minute grid (a live tick), bars outside 9:30 to the session's close, and bars marked incomplete. A bar the
    store has not made available by `as_of` is never returned at all (the BarReader's as-of rule)."""
    manifest = reader.manifest(dataset_id)
    if manifest.interval is not Interval.M5:
        raise TMError("dataset %s has %s bars; the time machine needs 5-minute bars"
                      % (dataset_id, manifest.interval.value), code=ErrorCode.OUTSIDE_CAPABILITY)
    ins = next((i for i in manifest.instruments if i.symbol == sym), None)
    if ins is None:
        raise TMError("%s is not in dataset %s" % (sym, dataset_id), code=ErrorCode.MISSING_DATA)
    dropped = {"off_grid": 0, "not_complete": 0, "outside_hours": 0}
    incomplete = set()
    bars = {}
    opens = {}
    for b in reader.bars(dataset_id, ins.instrument_id, as_of=as_of):
        day = cal.session_of(b.start)
        if day is None:
            dropped["outside_hours"] += 1
            continue
        if day not in opens:
            opens[day] = cal.open_close(day)[0]
        secs = (b.start - opens[day]).total_seconds()
        if secs % INTERVAL:
            dropped["off_grid"] += 1
            continue
        slot = int(secs // INTERVAL)
        if slot >= SESSION_BARS:
            dropped["outside_hours"] += 1
            continue
        if not b.complete:
            dropped["not_complete"] += 1
            incomplete.add((day.isoformat(), slot))
            continue
        bars[b.start] = {"date": day.isoformat(), "slot": slot, "start": clock24(OPEN_MIN + 5 * slot),
                         "o": float(b.open), "h": float(b.high), "l": float(b.low), "c": float(b.close),
                         "v": float(b.volume)}
    return manifest, ins, [bars[t] for t in sorted(bars)], dropped, incomplete


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before):
    """Full sessions (all 78 bars) strictly before the plan's date, oldest first,
    and the short ones that were left out (half days, gaps)."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) == SESSION_BARS:
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


# ---------------------------------------------------------------- the simulation

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order.
    So pool[i][s] is session s's own bar i: a session path is pool[k+1][s], pool[k+2][s], ..."""
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate_legacy(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy"):
    """The upstream Monte Carlo, unchanged: every bar drawn on its own, filled at the entry, -1R at any stop, no costs.
    LEGACY: kept only so a test can prove the port reads the bars the reel read (bit for bit). S11 forbids building
    a result from it, and nothing in TraderOS does. Same draws and order as the reel's sim.py."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            _o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes, strict=True)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes, strict=True)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for _ in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            _o, _h, _l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    return _summary_legacy(entry, k, last, replays, seed, window, cnt, green, Rs, r_target, srt, allp, paths)


def _summary_legacy(entry, k, last, replays, seed, window, cnt, green, Rs, r_target, srt, allp, paths):
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "method": "independent", "legacy": True,
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


def crash_shock(long, drop=CRASH):
    """Stress: the first bar after the fill opens `drop` against the position and everything after it stays there
    (its four ratios scaled; later bars follow from its lower close). A valid bar stays a valid bar."""
    f = (1 - drop) if long else (1 + drop)

    def shock(j, r):
        return tuple(x * f for x in r) if j == 1 else r
    return shock


def amplify_shock(multiplier=VOL_MULTIPLIER):
    """Stress: every ratio to the previous close (open, high, low and close) raised to `multiplier`, so each move is
    multiplied in log terms, inside the bar too. Ordering is kept (x -> x**m is increasing), so bars stay valid.
    Drift is amplified too."""
    def shock(_j, r):
        return tuple(x ** multiplier for x in r)
    return shock


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, side="buy", method="session", block=None,
             fill="next_open", cost_bps=DEFAULT_COST_BPS, shock=None, bands=True):
    """Monte Carlo replays of bars k+1..last, each one a dependent path (spec S11):
      session  each replay is ONE past full session's own bars from slot k+1 on, re-based at the entry;
      block    a stationary block bootstrap aligned by time of day: a replay keeps its session from bar to bar
               and jumps to another past session at the same slot with probability 1 / block.
    Each replay is then traded exactly as the history is (`one_trade`): the fill is the next drawn bar's open, a
    next open past the stop or the target is a bracket that is not filled ("rejected", no position, no R), gaps
    exit at the open, the stop is checked first in every bar, costs come off each leg. R is in units of the planned
    risk (entry - stop). The fan-chart bands come from a second run of 2,000 paths with the same seed."""
    if method not in METHODS:
        raise TMError("the replay method is session or block, not %r" % method)
    if method == "block" and not (block and block >= 1):
        raise TMError("a block replay needs a mean block length of at least 1 bar")
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)
    sessions = len(pool[k + 1])
    jump = 1.0 / block if method == "block" else 0.0
    rng = random.Random(seed)

    def draw():
        s = rng.randrange(sessions)
        out = []
        for j, i in enumerate(range(k + 1, last + 1)):
            if j and jump and rng.random() < jump:
                s = rng.randrange(sessions)
            r = pool[i][s]
            out.append(shock(j, r) if shock else r)
        return out

    head = [None] * k + [{"c": entry}]
    outcomes, closes, fills, Rs, paths = [], [], [], [], []
    ambiguous = 0
    for n in range(replays):
        p = entry
        D = list(head)
        for o, h, l, c in draw():
            D.append({"o": p * o, "h": p * h, "l": p * l, "c": p * c})
            p = p * c
        tr = one_trade(D, k, entry, stop, target, last, long, fill, cost_bps)
        if isinstance(tr, str):
            outcomes.append("rejected")
            fills.append(None)
        else:
            outcomes.append(tr["outcome"])
            fills.append(tr["entry"])
            Rs.append(tr["R"])
            ambiguous += tr["ambiguous"]
        closes.append(p)
        if n < SAMPLE_PATHS:
            paths.append([entry] + [round(b["c"], 2) for b in D[k + 1:]])
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close", "rejected")}
    green = sum(1 for o, p, f in zip(outcomes, closes, fills, strict=True)
                if o == "target" or (o == "close" and (p > f if long else p < f)))
    srt = sorted(closes)
    out = {
        "method": method, "block": block if method == "block" else None, "sessions": sessions,
        "replays": replays, "seed": seed, "bars_left": last - k, "fill": fill, "cost_bps": cost_bps,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_rejected": round(cnt["rejected"] / replays, 4),
        "p_green": round(green / replays, 4), "ambiguous": ambiguous, "p_ambiguous": round(ambiguous / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": statistics.mean(Rs) if Rs else None,
        "r_at_target": r_target,
        "median_fill": round(pct(sorted(f for f in fills if f is not None), 0.5), 2) if Rs else None,
    }
    if not bands:
        return out
    rng = random.Random(seed)
    allp = []
    for _ in range(BAND_PATHS):
        p = entry
        path = [entry]
        for _o, _h, _l, c in draw():
            p = p * c
            path.append(p)
        allp.append(path)
    band = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        band.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return dict(out, band_paths=BAND_PATHS, bands=band, sample_paths=paths,
                hist={"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins})


def real_outcome(entry_day, k, last, entry, stop, target, long):
    """What actually happened after the entry, on the plan-day bars already in the data
    (same stop-before-target tie rule as simulate()). (outcome, slot, every later slot
    already in the data); outcome and slot are None when none of them resolved it."""
    avail = sorted(s for s in entry_day if k < s <= last)
    for s in avail:
        b = entry_day[s]
        hit_stop = b["l"] <= stop if long else b["h"] >= stop
        hit_target = b["h"] >= target if long else b["l"] <= target
        if hit_stop:
            return "stop", s, avail
        if hit_target:
            return "target", s, avail
    return None, None, avail


# ---------------------------------------------------------------- stop rules and the history replay

def cents_away(x, long):
    """Round a stop to the cent, away from the entry: down for a buy, up for a sell."""
    c = round(x * 100, 4)
    return (math.floor(c) if long else math.ceil(c)) / 100.0


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long):
    r = rule["stop_rule"]
    if r == "or_mid":
        return cents_away((oh + ol) / 2, long)
    if r in ("or_low", "or_high"):
        return cents_away(ol if long else oh, long)
    frac = rule["stop_pct"] / 100.0
    return cents_away(entry * (1 - frac) if long else entry * (1 + frac), long)


def cost_r(fill, exit_price, risk, cost_bps):
    """A round trip's costs in R: cost_bps on the entry leg's own price (the fill) plus cost_bps on the exit leg's
    own price. (#53 charged both legs on the fill; QT06.)"""
    return cost_bps / 10000.0 * (fill + exit_price) / risk


def walk(bars, stop, target, long, gaps):
    """Bar by bar from the fill: (outcome, exit price, ambiguous), or (None, None, False) when
    neither side is touched. With gaps, a bar's OPEN decides first: one that opens past the
    stop fills at that open (worse than the stop), one that opens past the target fills at
    that open (the limit gets the better price). (#53 checked the stop's touch before the
    target's gap, so a bar opening past the target and then trading through the stop was
    booked as an ambiguous stop; the open comes first in the bar, so it decides. OSDev6's
    review.) Then the stop's touch is checked before the target's in every bar.
    ambiguous: the deciding bar touched both sides and is counted as the stop
    (AMBIGUOUS_BAR_ASSUMPTION: a 5-minute bar does not say which came first)."""
    for b in bars:
        if long:
            if gaps and b["o"] <= stop:
                return "stop", b["o"], False
            if gaps and b["o"] >= target:
                return "target", b["o"], False
            if b["l"] <= stop:
                return "stop", stop, b["h"] >= target
            if b["h"] >= target:
                return "target", target, False
        else:
            if gaps and b["o"] >= stop:
                return "stop", b["o"], False
            if gaps and b["o"] <= target:
                return "target", b["o"], False
            if b["h"] >= stop:
                return "stop", stop, b["l"] <= target
            if b["l"] <= target:
                return "target", target, False
    return None, None, False


def one_trade(D, kk, signal, stop, target, last, long, fill, cost_bps):
    """One trade on one session, from the signal bar kk. Returns the trade dict,
    or a skip reason ("no_bar", "rejected").

    fill "next_open": the signal is the CLOSE of bar kk, so the order can only fill
    at the OPEN of bar kk+1. A next open at or past the stop or the target is a
    bracket the broker rejects: skipped. Gaps fill at the open.
    fill "close": the old accounting, filled at the signal close itself, stop and
    target at their exact prices. LEGACY: for comparing with earlier runs only.

    R is in units of the planned risk (signal minus stop), the risk the position
    was sized to, so a worse fill or a gap shows up as a worse R. R and cost_R are
    unrounded; round only to show them."""
    risk = (signal - stop) if long else (stop - signal)
    if fill == "close":
        e, gaps = signal, False
    else:
        if kk + 1 > last:
            return "no_bar"
        e, gaps = round(D[kk + 1]["o"], 2), True
        if not ((stop < e < target) if long else (target < e < stop)):
            return "rejected"
    oc, x, both = walk(D[kk + 1:last + 1], stop, target, long, gaps)
    if oc is None:
        x = D[last]["c"]
    gross = ((x - e) if long else (e - x)) / risk
    cost = cost_r(e, x, risk, cost_bps)
    return {"bar": kk, "signal": signal, "entry": e, "stop": stop, "target": target, "outcome": oc or "close",
            "exit": round(x, 2), "risk": round(risk, 4), "cost_R": cost, "R": gross - cost, "ambiguous": both}


def history_replay(sessions, rule, last, side="buy", fill="next_open", cost_bps=DEFAULT_COST_BPS):
    """The plan's rule on every past full session: the signal is the first bar after
    the opening range to close beyond it (up to the --until bar); the stop and the
    target are set from that close, as trader-exit does; the fill follows `fill`
    (see one_trade); then stop first, then target, bar by bar, else the close at
    --until, less the costs. Signals, fills (triggered) and skips are counted apart."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    trades = []
    skipped = {"no_bar": 0, "rejected": 0}
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        p = round(D[kk]["c"], 2)
        s = rule_stop(rule, oh, ol, p, long)
        risk = (p - s) if long else (s - p)
        if risk <= 0:
            continue
        t = round(p + rule["target_r"] * risk, 2) if long else round(p - rule["target_r"] * risk, 2)
        tr = one_trade(D, kk, p, s, t, last, long, fill, cost_bps)
        if isinstance(tr, str):
            skipped[tr] += 1
            continue
        tr = dict(tr, date=d, time=clock12(OPEN_MIN + 5 * (kk + 1)))
        trades.append(tr)
    return {
        "sessions": len(sessions), "signals": len(trades) + sum(skipped.values()), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "ambiguous": sum(1 for x in trades if x["ambiguous"]),
        "avg_R": statistics.mean(x["R"] for x in trades) if trades else None,
        "fill": fill, "cost_bps": cost_bps, "skipped": skipped,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the blind-entry baseline

def baseline(sessions, history, last, side="buy", fill="next_open", cost_bps=DEFAULT_COST_BPS, seed=19,
             draws=BASELINE_DRAWS):
    """The control: the same trades with the setup taken out. For every trade in the
    history, enter blind at the same bar on every OTHER past session, whether or not
    anything broke out, with the same stop width (as a fraction of the price), the
    same target R, the same fills and the same costs. If the setup carries an edge,
    its trades should beat these.

    Then: the setup's average R, and its edge (each trade's R minus the average R of its
    own blind entries), each with a 95 % range from a stationary bootstrap over the
    trades in date order (spec 09.4: trades on nearby days are not independent)."""
    long = side == "buy"
    trades = history["trades"]
    rs, controls = [], []
    skipped = {"no_bar": 0, "rejected": 0}
    for tr in trades:
        kk = tr["bar"]
        frac = tr["risk"] / tr["signal"]
        r_t = history["rule"]["target_r"]
        mine = []
        for d, D in sessions:
            if d == tr["date"]:
                continue
            p = round(D[kk]["c"], 2)
            s = cents_away(p * (1 - frac) if long else p * (1 + frac), long)
            risk = (p - s) if long else (s - p)
            if risk <= 0:
                continue
            t = round(p + r_t * risk, 2) if long else round(p - r_t * risk, 2)
            ct = one_trade(D, kk, p, s, t, last, long, fill, cost_bps)
            if isinstance(ct, str):
                skipped[ct] += 1
                continue
            mine.append(ct["R"])
        rs += mine
        controls.append(statistics.mean(mine) if mine else None)
    out = {"trades": len(rs), "skipped": skipped, "draws": draws, "seed": seed, "method": BOOTSTRAP_METHOD,
           "setup_trades": len(trades), "min_trades": MIN_TRADES}
    if not trades or not rs:
        return dict(out, avg_R=None, setup_avg_R=None)
    setup = [x["R"] for x in trades]
    edges = [x["R"] - c for x, c in zip(trades, controls, strict=True) if c is not None]
    lo, hi, block = stationary_bootstrap_mean(setup, seed=seed, resamples=draws)
    elo, ehi, _ = stationary_bootstrap_mean(edges, seed=seed + 1, resamples=draws)
    return dict(out, avg_R=statistics.mean(rs), control_R=statistics.mean(c for c in controls if c is not None),
                setup_avg_R=statistics.mean(setup), edge_R=statistics.mean(edges), edge_trades=len(edges),
                setup_ci=[lo, hi], edge_ci=[elo, ehi], block_length=block)


def baseline_verdict(b):
    """One line: does the setup beat a blind entry, or is there too little to tell?"""
    if not b or b.get("setup_avg_R") is None:
        return None
    n = b["setup_trades"]
    if n < b.get("min_trades", MIN_TRADES):
        return ("Too few trades to tell: %d trades, and at least %d are needed before a gap between the setup "
                "and a blind entry means much." % (n, b.get("min_trades", MIN_TRADES)))
    lo, hi = b["edge_ci"]
    rng = "%+.3f R a trade, 95 %% range %+.3f to %+.3f (stationary bootstrap, %s draws)" % (
        b["edge_R"], lo, hi, "{:,}".format(b["draws"]))
    tail = "" if SIGNED_OFF else (" Not a qualification: the interval and the verdict's other thresholds are "
                                  "proposals (ADR-010), and %s." % PENDING)
    if lo > 0:
        return "The setup beat a blind entry: %s, all above zero.%s" % (rng, tail)
    if hi < 0:
        return "The setup did worse than a blind entry: %s, all below zero.%s" % (rng, tail)
    return "No clear difference from a blind entry: %s.%s" % (rng, tail)


def threshold_note(b):
    """The trade minimum and what is still proposed (ADR-010)."""
    if not b or b.get("setup_avg_R") is None:
        return None
    return ("Trade minimum: %d trades, %s (the lab's is the same, ADR-005). The other thresholds, the interval and "
            "the cost are proposals (ADR-010), and %s." % (b["min_trades"], MIN_TRADES_SIGNED_OFF, PENDING))


# ---------------------------------------------------------------- the plan

def _num(v, what):
    if isinstance(v, bool):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    try:
        x = float(Decimal(str(v).strip()))
    except (TypeError, ValueError, InvalidOperation):
        raise TMError("the plan needs a number for %s, got %r" % (what, v)) from None
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def read_plan(path):
    """A plan file (trader-exit's plan.json shape): one JSON object, at most 1 MB (untrusted input, spec 04.2)."""
    p = pathlib.Path(path)
    if not p.is_file():
        raise TMError("no plan at %s: pass the plan as a file, or as --symbol --date --entry --stop --target "
                      "--entry-time" % path, code=ErrorCode.MISSING_DATA)
    if p.stat().st_size > MAX_PLAN_BYTES:
        raise TMError("%s is larger than %d bytes; a plan is one small JSON object" % (path, MAX_PLAN_BYTES),
                      code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
    def no_duplicates(pairs):
        seen = {}
        for k, v in pairs:
            if k in seen:
                raise TMError("%s names %r twice; a plan says each thing once" % (path, k))
            seen[k] = v
        return seen
    try:
        raw = json.loads(p.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    except (ValueError, UnicodeDecodeError) as e:
        raise TMError("%s is not valid JSON: %s" % (path, e)) from None
    if not isinstance(raw, dict):
        raise TMError("%s is not a plan" % path)
    return raw


def plan_from_flags(symbol, side="buy", date=None, entry=None, stop=None, target=None, entry_time=None,
                    setup="orb", or_minutes=15, stop_rule="auto", flat_by=None, now_utc=None):
    """The upstream flag path of load_plan: the plan as values instead of a file."""
    missing = [f for f, v in (("entry", entry), ("stop", stop), ("target", target), ("entry_time", entry_time))
               if v is None]
    if missing:
        raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
    if date is None:
        date = new_york(now_utc or parse_now(None)).date().isoformat()
    return {"symbol": symbol, "side": side, "date": date, "entry": entry, "stop": stop, "target": target,
            "entry_time": entry_time, "setup": setup, "opening_range": {"minutes": or_minutes},
            "stop_rule": stop_rule, "interval": "5m", "flat_by": flat_by}


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = str(raw.get("symbol") or "").strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", sym):
        raise TMError("the plan needs a ticker symbol, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): the replay pool is built from full sessions of "
                      "5-minute bars" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-19, got %r" % raw.get("date")) from None
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 10:15 AM)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"]) from None
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:15 AM) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("the entry must close between 9:35 AM and 3:55 PM to leave bars to replay "
                      "(got %s)" % clock12(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number") from None
    if or_min < 5 or or_min % 5 or or_min >= 390:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    setup = str(raw.get("setup") or "orb")
    if setup.lower() == "orb" and k < or_min // 5:
        raise TMError("the entry bar closes at %s, before the %d-minute opening range ends at %s: an opening-range "
                      "breakout enters after its range, and reading the rest of the range would be look-ahead"
                      % (clock12(OPEN_MIN + 5 * (k + 1)), or_min, clock12(OPEN_MIN + or_min)),
                      code=ErrorCode.LOOKAHEAD_RISK)
    risk = (entry - stop) if long else (stop - entry)
    out = {
        "symbol": sym, "side": side, "date": date, "setup": setup,
        "entry": entry, "entry_time": clock12(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "from": src,
    }
    plan_hash(out)          # a value the record cannot hold is refused now, before any replay, and says which
    return out


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if n <= plan["entry_bar"] and all(i in entry_day for i in range(n)):   # only bars closed by the entry
            oh, ol = opening_range(entry_day, n)
            if abs(cents_away((oh + ol) / 2, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(cents_away(ol if long else oh, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def _until12(mc):
    t = mc.get("until") or "16:00"
    return clock12(int(t[:2]) * 60 + int(t[3:]))


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds. Past STRONG the line
    says "Leans" until ADR-010 is signed off; verdict_notes says why."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until12(mc), _until12(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG or not SIGNED_OFF:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until12(mc), _until12(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    if lv and abs(lv[0]) >= STRONG and not SIGNED_OFF:
        notes.append("That is past the 15-point line for \"strongly\"; the word waits for ADR-010's "
                     "sign-off (that threshold is still proposed, and %s)." % PENDING)
    return notes


def fill_lines(fill, cost_bps):
    """(the header line, the caveat line) for a fill model (#2's fill_lines, with #53's fills and costs)."""
    if fill == "close":
        return ("LEGACY accounting: fills at the signal bar's close, stop and target at their exact prices, "
                "costs %g bps a side. A comparison with earlier runs, not an execution model." % cost_bps,
                "LEGACY fills at the signal bar's close; a real fill is the next bar's open or worse.")
    return ("Fills at the open of the bar after the entry bar; a bracket that opens past the stop or the target is "
            "not filled; a bar that opens past the stop exits at that open; costs %g bps a side, each leg on its "
            "own price." % cost_bps,
            "Fills at the next bar's open with %g bps a side; a real fill can be worse, and a touched price is not "
            "proof of a fill." % cost_bps)


def default_until(plan, end_min, warn):
    """The default --until in minutes: the plan's flat_by when it has one, else the
    close. A flat_by off the 5-minute grid or after an early close is moved to the
    last bar before it, with a warning. (#2, as published.)"""
    fb = plan.get("flat_by")
    if not fb:
        return end_min
    m = parse_clock(fb, "the plan's flat_by")
    moved = m
    if moved > end_min:
        moved = end_min
    moved -= (moved - OPEN_MIN) % 5
    if moved != m:
        warn.append("the plan's flat_by %s is %s; the replays end at %s instead (pass --until to choose)"
                    % (fb, "after the %s early close" % clock12(end_min) if m > end_min else "off the 5-minute grid",
                       clock12(moved)))
    return moved


def time_machine(reader, dataset_id, plan, now_utc, replays=10000, seed=19, window=2, until=None,
                 fill="next_open", cost_bps=DEFAULT_COST_BPS, method="session", cal=None):
    """A stored dataset + a normalized plan -> the time machine document (the upstream shape, extended).
    method "session" (the result) or "independent" (LEGACY: the upstream draw, for comparison tests only)."""
    cal = cal or XNYS()
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    fill = str(fill).replace("-", "_")
    if fill not in FILLS:
        raise TMError("--fill must be next-open or close, not %r" % fill)
    if not 0 <= cost_bps <= 100:
        raise TMError("--cost-bps must be between 0 and 100 (basis points a side)")
    if method not in ("session", "independent"):
        raise TMError("the replay method is session (or the legacy independent draw), not %r" % method)
    legacy = method == "independent" or fill == "close"
    sym = plan["symbol"]
    manifest, ins, bars, dropped, incomplete = read_bars(reader, dataset_id, sym, now_utc, cal)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym, code=ErrorCode.MISSING_DATA)
    k = plan["entry_bar"]
    day = datetime.date.fromisoformat(plan["date"])
    early = cal.is_early_close(day)
    end_min = EARLY_MIN if early else CLOSE_MIN
    warn = []
    until_min = default_until(plan, end_min, warn) if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > end_min:
        raise TMError("--until must be on the 5-minute grid and no later than %s%s"
                      % (clock12(end_min), " (an early close)" if early else ""))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("%s %s leaves nothing to replay after the %s entry"
                      % ("--until" if until is not None else "the plan's flat_by", clock12(until_min),
                         plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        if (plan["date"], k) in incomplete:
            raise TMError("the entry is not in the data: the %s bar (closing %s) on %s for %s is marked incomplete"
                          % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym),
                          code=ErrorCode.INCOMPLETE_BAR)
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason),
                      code=ErrorCode.MISSING_DATA)
    eb = entry_day[k]
    if not eb["l"] - 0.01 <= plan["entry"] <= eb["h"] + 0.01:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    outcome, resolved_at, later_bars = real_outcome(entry_day, k, last, plan["entry"], plan["stop"],
                                                     plan["target"], plan["side"] == "buy")
    if outcome:
        raise TMError("the real bars already settled this: %s was hit at %s (bar %d) on %s. Odds from "
                      "here would be hindsight, not a forecast; run the time machine before the entry."
                      % (outcome, clock24(OPEN_MIN + 5 * (resolved_at + 1)), resolved_at, plan["date"]),
                      code=ErrorCode.LOOKAHEAD_RISK)
    if later_bars:
        warn.append("%d bar(s) after the entry are already in the data and were not used: the odds are "
                    "from the entry bar only" % len(later_bars))
    sessions, short = full_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS), code=ErrorCode.INSUFFICIENT_EVIDENCE)
    pool = build_pool(sessions)
    long = plan["side"] == "buy"
    args = (pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed)
    sensitivity, stress = [], []
    if method == "independent":
        mc = simulate_legacy(*args, window, plan["side"])
    else:
        mc = simulate(*args, plan["side"], fill=fill, cost_bps=cost_bps)
        if not legacy:
            for b in BLOCKS:
                sensitivity.append(simulate(*args, plan["side"], method="block", block=b, cost_bps=cost_bps,
                                            bands=False))
            for name, kw, assumption in (
                    ("crash gap", {"shock": crash_shock(long)},
                     "the first bar after the fill opens %d %% %s and stays there, on every replay"
                     % (round(CRASH * 100), "lower" if long else "higher")),
                    ("doubled moves", {"shock": amplify_shock()},
                     "every move from the previous close (open, high, low and close) is doubled in log terms, "
                     "drift included"),
                    ("five times the costs", {"cost_bps": cost_bps * COST_MULTIPLIER},
                     "costs %g bps a side instead of %g, everything else unchanged"
                     % (cost_bps * COST_MULTIPLIER, cost_bps))):
                if name == "crash gap" and last - k < 2:
                    # the gap lands on the first bar AFTER the fill bar; with none before --until it cannot happen,
                    # and the stress says so instead of reporting the unshocked replays as shocked (OSDev6's review)
                    stress.append({"name": name, "assumption": assumption, "mc": None,
                                   "not_applied": "no bar after the fill bar before %s: the gap has nowhere to "
                                                  "happen" % clock12(until_min)})
                    continue
                kw = {"cost_bps": cost_bps, **kw}
                stress.append({"name": name, "assumption": assumption,
                               "mc": simulate(*args, plan["side"], bands=False, **kw)})
    mc = dict(mc, until=clock24(until_min), until_from="flag" if until is not None else
              ("flat_by" if plan.get("flat_by") else "close"),
              pool_sessions=len(sessions), pool_from=sessions[0][0], pool_to=sessions[-1][0],
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    base = None
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"], fill, cost_bps)
        if history["triggered"]:
            base = baseline(sessions, history, last, plan["side"], fill, cost_bps, seed)
            base["verdict"] = baseline_verdict(base)
            base["threshold_note"] = threshold_note(base)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    caveats = ["Built from the same %d sessions as the history: the two are not independent evidence."
               % len(sessions), fill_lines(fill, cost_bps)[1]]
    if legacy:
        caveats.insert(0, "LEGACY comparison run: %s. Not a result." % (
            "every bar drawn on its own (no session holds together)" if method == "independent"
            else "fills at the signal close"))
    last_bar = bars[-1]
    return {
        "created_by": "traderos.timemachine.plan",
        "as_of": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "dataset",
        "legacy": legacy,
        "data": {"dataset_id": manifest.dataset_id, "dataset_hash": manifest.canonical_hash,
                 "symbol": ins.symbol, "instrument_id": ins.instrument_id, "interval": "5m",
                 "last_bar": "%s %s ET" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "sensitivity": sensitivity,
        "stress": stress,
        "history": history,
        "baseline": base,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "caveats": caveats,
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: earnings, "
                 "news and halts can do things those sessions never did. Same seed, same numbers."
                 % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- the record (timemachine/1, ADR-010)

def _m(x, unit, places=6, reason="not available"):
    if x is None:
        return MetricValue(value=None, unit=unit, unavailable_reason=reason)
    d = decimal_from_float(float(x), places)
    if d is None:
        return MetricValue(value=None, unit=unit, unavailable_reason="not a finite number")
    return MetricValue(value=d, unit=unit)


def _count(n, unit):
    return MetricValue(value=Decimal(int(n)), unit=unit)


def _price(x):
    return decimal_from_float(float(x), 4).normalize()


def plan_hash(plan):
    """SHA-256 of the normalized plan, numbers as decimals wherever they are (the canonical form refuses floats).
    A value it cannot hold (not finite, an integer past 2**53, a type JSON has no word for) is a TMError naming it."""
    def plain(v, where):
        if isinstance(v, float):
            d = decimal_from_float(v, 6)
            if d is None:
                raise TMError("the plan's %s is %r, not a finite number" % (where, v))
            return d
        if isinstance(v, dict):
            return {k: plain(x, "%s.%s" % (where, k)) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [plain(x, "%s[%d]" % (where, i)) for i, x in enumerate(v)]
        return v
    body = {k: plain(v, k) for k, v in plan.items() if k != "from"}
    try:
        return sha256_bytes(canonical_bytes(body))
    except ValidationError as e:
        raise TMError("the plan holds a value that cannot be recorded: %s" % e.message) from None


def code_version():
    """The package version and this module's own source hash: a rerun names the code it ran (QT07)."""
    return "traderos %s; plan_replay/1 sha256 %s" % (
        __version__, sha256_bytes(pathlib.Path(__file__).read_bytes())[:16])


def _mc_metrics(mc):
    met = {"p_target": _m(mc["p_target"], "fraction", 4), "p_stop": _m(mc["p_stop"], "fraction", 4),
           "p_neither": _m(mc["p_neither"], "fraction", 4), "p_rejected": _m(mc["p_rejected"], "fraction", 4),
           "p_green": _m(mc["p_green"], "fraction", 4), "p_ambiguous": _m(mc["p_ambiguous"], "fraction", 4),
           "avg_R": _m(mc["avg_R"], "R", reason="no replay was filled"),
           "median_close": _m(mc["median_close"], "USD", 2), "p05_close": _m(mc["p5_close"], "USD", 2),
           "p95_close": _m(mc["p95_close"], "USD", 2), "r_at_target": _m(mc["r_at_target"], "R", 4),
           "distinct_sessions": _count(mc["sessions"], "sessions")}
    return met


def to_record(doc, plan, manifest_limitations=()):
    """The time machine document -> a timemachine/1 record with a plan-replay subject (ADR-010). The dataset's own
    limitations travel with it. A LEGACY comparison run is never recorded."""
    if doc.get("legacy"):
        raise TMError("a legacy comparison run is not a result and is not recorded")
    m, h, b = doc["mc"], doc["history"], doc["baseline"]
    day = datetime.date.fromisoformat(plan["date"])
    long = plan["side"] == "buy"
    as_of = datetime.datetime.fromisoformat(doc["as_of"].replace("Z", "+00:00"))
    until_min = int(m["until"][:2]) * 60 + int(m["until"][3:])
    cost = Decimal(repr(float(h.get("cost_bps", m["cost_bps"]))))
    version = code_version()
    subject = PlanReplaySubject(
        symbol=doc["data"]["symbol"], instrument_id=doc["data"]["instrument_id"],
        direction=Direction.LONG if long else Direction.SHORT, session=day, entry=_price(plan["entry"]),
        stop=_price(plan["stop"]), target=_price(plan["target"]),
        entry_bar_end=at_new_york(day, OPEN_MIN + 5 * (plan["entry_bar"] + 1)),
        replay_until=at_new_york(day, until_min), setup=plan["setup"], stop_rule=doc["plan"]["stop_rule"],
        opening_range_minutes=plan["opening_range"]["minutes"], plan_hash=plan_hash(plan), as_of=as_of,
        fill_model_id=FILL_MODEL_ID, cost_bps_per_side=cost.normalize(), code_version=version)
    pool = "%d full sessions, %s to %s" % (m["pool_sessions"], m["pool_from"], m["pool_to"])
    fills = fill_lines("next_open", float(cost))[0]
    scenarios = []
    if h.get("rule"):
        ru = h["rule"]
        scenarios.append(Scenario(
            name="history: the plan's rule on each past session", kind=ScenarioKind.OBSERVED,
            method="history_replay/1: %d-min opening-range breakout, stop rule %s, %gR target"
                   % (ru["or_minutes"], ru["stop_rule"], ru["target_r"]),
            assumptions=(pool, fills, "R in units of the planned risk; a bar that touches both the stop and the "
                         "target counts as the stop (AMBIGUOUS_BAR_ASSUMPTION)",
                         "the pool and this history are the same sessions: not independent evidence"),
            metrics={"sessions": _count(h["sessions"], "sessions"), "signals": _count(h["signals"], "trades"),
                     "trades": _count(h["triggered"], "trades"), "target": _count(h["target"], "trades"),
                     "stop": _count(h["stop"], "trades"), "neither": _count(h["close"], "trades"),
                     "rejected_brackets": _count(h["skipped"]["rejected"], "trades"),
                     "no_bar_to_fill": _count(h["skipped"]["no_bar"], "trades"),
                     "ambiguous_bars": _count(h["ambiguous"], "trades"),
                     "avg_R": _m(h["avg_R"], "R", reason="the rule never fired")}))
    else:
        scenarios.append(Scenario(name="history: the plan's rule on each past session", kind=ScenarioKind.OBSERVED,
                                  method="not run", paths=0, assumptions=(h.get("note", "skipped"),)))
    if b and b.get("setup_avg_R") is not None:
        scenarios.append(Scenario(
            name="blind-entry baseline", kind=ScenarioKind.OBSERVED,
            method="a blind entry at each trade's bar on every other past session; %s over the trades in date "
                   "order, mean block %d, %d resamples, seed %d" % (b["method"], b["block_length"], b["draws"],
                                                                    b["seed"]),
            assumptions=("same stop width, target R, fills and costs as the setup's trades",
                         "edge = each trade's R minus the average R of its own blind entries",
                         "minimum trades: %d, %s (ADR-010; the lab's is the same, ADR-005); the interval is "
                         "proposed, and %s" % (b["min_trades"], MIN_TRADES_SIGNED_OFF, PENDING)),
            metrics={"blind_trades": _count(b["trades"], "trades"), "blind_avg_R": _m(b["avg_R"], "R"),
                     "control_R": _m(b["control_R"], "R"), "setup_trades": _count(b["setup_trades"], "trades"),
                     "setup_avg_R": _m(b["setup_avg_R"], "R"), "setup_ci95_low": _m(b["setup_ci"][0], "R"),
                     "setup_ci95_high": _m(b["setup_ci"][1], "R"), "edge_R": _m(b["edge_R"], "R"),
                     "edge_ci95_low": _m(b["edge_ci"][0], "R"), "edge_ci95_high": _m(b["edge_ci"][1], "R"),
                     "min_trades": _count(b["min_trades"], "trades")}))
    scenarios.append(Scenario(
        name="replays: whole past sessions", kind=ScenarioKind.RESAMPLED,
        method="session_bootstrap/1: each replay is one past full session's own bars from the entry on, re-based "
               "at the entry; seed %d" % m["seed"],
        assumptions=(pool, "the rest of the day moves like one of those sessions did; %d distinct paths"
                     % m["sessions"], fills, "not a forecast: plausible variation of what was observed"),
        paths=m["replays"], metrics=_mc_metrics(m)))
    for s in doc["sensitivity"]:
        scenarios.append(Scenario(
            name="replays: blocks of %d bars on average" % s["block"], kind=ScenarioKind.RESAMPLED,
            method="stationary block bootstrap aligned by time of day, mean block %d bars; seed %d"
                   % (s["block"], s["seed"]),
            assumptions=(pool, "a replay jumps to another past session at the same time of day with probability "
                         "1/%d at each bar" % s["block"], "shown beside the session replays so the effect of the "
                         "choice is visible (Q09)"),
            paths=s["replays"], metrics=_mc_metrics(s)))
    for s in doc["stress"]:
        if s["mc"] is None:
            scenarios.append(Scenario(name=s["name"], kind=ScenarioKind.STRESS, method="not applied", paths=0,
                                      assumptions=(s["assumption"], "not applied: " + s["not_applied"])))
            continue
        met = _mc_metrics(s["mc"])
        ref = m["avg_R"]
        met["avg_R_change_vs_replays"] = _m(None if s["mc"]["avg_R"] is None or ref is None
                                            else s["mc"]["avg_R"] - ref, "R", reason="no replay was filled")
        scenarios.append(Scenario(name=s["name"], kind=ScenarioKind.STRESS,
                                  method="the session replays, same seed, on shocked bars",
                                  assumptions=(s["assumption"],), paths=s["mc"]["replays"], metrics=met))
    limitations = [
        *manifest_limitations,
        Limitation(code="GENERATED_NOT_FORECAST", detail="Replays and stresses are generated from stated "
                   "assumptions; they are not forecasts and not observed history."),
        Limitation(code="SAME_SESSIONS", detail=doc["caveats"][0]),
        Limitation(code="THRESHOLDS_NOT_SIGNED_OFF", detail="The trade minimum (%d) is %s. The verdict's other "
                   "thresholds (coin flip, strongly, many neither), the interval and the cost per side are still "
                   "proposals (ADR-010), and %s: no result here is a qualification." % (
                       MIN_TRADES, MIN_TRADES_SIGNED_OFF, PENDING)),
    ]
    amb = m["ambiguous"] + (h.get("ambiguous") or 0)
    if amb:
        limitations.append(Limitation(code="AMBIGUOUS_BAR_ASSUMPTION", detail="%d replay(s) and %d history "
                                      "trade(s) were decided by a bar that touched both the stop and the target; the "
                                      "stop is assumed first." % (m["ambiguous"], h.get("ambiguous") or 0)))
    limitations += [Limitation(code="PLAN_REPLAY_NOTE", detail=w) for w in doc["warnings"]]
    identity = sha256_bytes(canonical_bytes([subject.plan_hash, doc["data"]["dataset_id"],
                                             doc["data"]["dataset_hash"], doc["as_of"], m["until"], m["replays"],
                                             m["seed"], str(cost), version]))[:20]
    return with_hash(TimeMachineReport(
        report_id="TM-%s" % identity, plan_replay=subject, dataset_id=doc["data"]["dataset_id"],
        dataset_hash=doc["data"]["dataset_hash"], window_start=datetime.date.fromisoformat(m["pool_from"]),
        window_end=datetime.date.fromisoformat(m["pool_to"]), horizon_sessions=1, seed=m["seed"],
        scenarios=tuple(scenarios), limitations=tuple(limitations),
        created_at=datetime.datetime.now(datetime.UTC)))


def replay(store, raw_plan, dataset_id, *, now_utc=None, replays=10000, seed=19, until=None,
           cost_bps=DEFAULT_COST_BPS, src="flags"):
    """Normalize the plan, run the time machine on the store's dataset as of `now_utc`, and store the record.
    -> (record, document). The same plan, data, clock and settings give the same record id; the first one stands."""
    now_utc = now_utc or parse_now(None)
    plan = normalize_plan(raw_plan, src)
    reader = store.bar_reader()
    doc = time_machine(reader, dataset_id, plan, now_utc, replays=replays, seed=seed, until=until,
                       cost_bps=cost_bps)
    record = to_record(doc, plan, reader.manifest(dataset_id).limitations)
    existing = store.find(TimeMachineReport, record.report_id)
    if existing is not None:
        return existing, doc
    store.put(record)
    return store.get(TimeMachineReport, record.report_id), doc


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def _r(x):
    return "n/a" if x is None else "%+.3f" % x


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = clock12(int(m["until"][:2]) * 60 + int(m["until"][3:]))
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    why = {"flat_by": ", the plan's flat_by", "close": ", the close"}.get(m.get("until_from"), "")
    if m.get("legacy"):
        how = ("LEGACY: each bar drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its "
               "last %d full sessions (%s to %s), every bar on its own."
               % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]))
    else:
        how = ("Each replay is one of %s's last %d full sessions (%s to %s), its own bars from the entry on, "
               "re-based at the entry: %d distinct paths." % (p["symbol"], m["pool_sessions"], m["pool_from"],
                                                             m["pool_to"], m["pool_sessions"]))
    lines = [
        "%s %s, %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%sR, risk %.2f a share)."
        % (p["symbol"], p["side"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"],
           p["target"], ("%g" % r), p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s%s (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, why, m["bars_left"], m["seed"]),
        how,
        fill_lines(h.get("fill") or m.get("fill") or "close", h.get("cost_bps", m.get("cost_bps", 0)))[0],
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the %s at %s" % (pc(m["p_green"]), "above" if long else "below",
                                                                    "entry" if m.get("legacy") else "fill", until),
    ]
    if not m.get("legacy"):
        lines += ["  not filled     %-8s the next bar opened past the stop or the target" % pc(m["p_rejected"]),
                  "  both touched in one bar: %d (%s), counted as the stop (AMBIGUOUS_BAR_ASSUMPTION)"
                  % (m["ambiguous"], pc(m["p_ambiguous"]))]
    lines += [
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        ("  Average R: %+.3f (R = the %.2f risk: %sR at the target, -1R at the stop)."
         % (m["avg_R"], p["risk_per_share"], ("+%g" % m["r_at_target"])) if m.get("legacy") else
         "  Average R: %s (R = (exit - fill) / the %.2f planned risk, less costs)." % (_r(m["avg_R"]),
                                                                                     p["risk_per_share"])),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
        if h.get("fill") == "close":
            lines.append("  Fills: at the signal bar's close, stop and target at their exact prices "
                         "(the old accounting), costs %g bps a side." % h.get("cost_bps", 0))
        elif h.get("fill"):
            lines.append("  Fills: at the open of the bar after the signal close; a bar that opens past the stop "
                         "or the target fills at that open; costs %g bps a side, each leg on its own price, taken "
                         "off every R." % h["cost_bps"])
        sk = h.get("skipped") or {}
        if sk.get("rejected") or sk.get("no_bar"):
            lines.append("  Left out %d of %d signal(s): %d opened past the stop or the target on the next bar (a "
                         "bracket the broker rejects), %d had no bar left to fill on."
                         % (sk.get("rejected", 0) + sk.get("no_bar", 0), h["signals"], sk.get("rejected", 0),
                            sk.get("no_bar", 0)))
        if h.get("ambiguous"):
            lines.append("  %d trade(s) ended on a bar that touched both the stop and the target: counted as the "
                         "stop (AMBIGUOUS_BAR_ASSUMPTION)." % h["ambiguous"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    b = doc.get("baseline")
    if b and b.get("setup_avg_R") is not None:
        lines += [
            "Baseline: a blind entry at the same bars on the other sessions, with no breakout needed, the same "
            "stop width, target R, fills and costs: %s trades, average R %+.3f."
            % ("{:,}".format(b["trades"]), b["avg_R"]),
            "  Setup minus its own blind entries: %+.3f R a trade, 95 %% range %+.3f to %+.3f. The setup's average "
            "R %+.3f, 95 %% range %+.3f to %+.3f (stationary bootstrap, mean block %d, seed %d)."
            % (b["edge_R"], b["edge_ci"][0], b["edge_ci"][1], b["setup_avg_R"], b["setup_ci"][0], b["setup_ci"][1],
               b["block_length"], b["seed"]),
            b["verdict"],
            b["threshold_note"],
        ]
    for s in doc.get("sensitivity") or []:
        lines.append("Blocks of %d bars instead of whole sessions: target first %s, stop first %s, average R %s."
                     % (s["block"], pc(s["p_target"]), pc(s["p_stop"]), _r(s["avg_R"])))
    for s in doc.get("stress") or []:
        sm = s["mc"]
        if sm is None:
            lines.append("Stress, %s: not applied (%s)." % (s["name"], s["not_applied"]))
            continue
        lines.append("Stress, %s (%s): target first %s, stop first %s, average R %s."
                     % (s["name"], s["assumption"], pc(sm["p_target"]), pc(sm["p_stop"]), _r(sm["avg_R"])))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + list(doc.get("caveats") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (half days or gaps): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        t = times[j]
        hh, mm = int(t[:2]), t[3:]
        lab = "%d:%s" % (hh % 12 or 12, mm)
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


__all__ = ["BLOCKS", "DEFAULT_COST_BPS", "MIN_TRADES", "MIN_TRADES_SIGNED_OFF", "SIGNED_OFF", "TMError", "baseline",
           "baseline_verdict", "history_replay", "normalize_plan", "one_trade", "read_bars", "replay", "report",
           "simulate", "simulate_legacy", "time_machine", "to_record", "verdict", "walk"]
