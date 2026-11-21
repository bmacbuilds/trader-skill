"""The upstream cross-section opening-range study, ported from the tank (ADR-010).

Source: tank/prs/day-trader-skill/pr-53/files/trader-timemachine/backtest.py, upstream PR #53 at 16133276
(upstream day-trader-skill, MIT; contributor Pjh0522, commits authored "Claude"). It is an intraday study
of one rule, not the S10 backtester: it books each trade as an R number with no cash, position, sizing or
settlement, so `traderos.backtest` stays the lab's engine and this stays beside the Time Machine.

Kept as the upstream PR wrote it: the four variants, the rule (15-minute range, stop at the midpoint, long only), the
per-trade blind control (`control_r`), the whole-day bootstrap (`day_bootstrap`), the halves, and the report's
words. What changed, each with its reason:

* **Bars from the store, as of a time; no fetch and no cache** (`load_sessions`). Flaw: spec 06.1 holds Yahoo's
  unofficial endpoint, and the S&P 500 list came from a community CSV with no point-in-time date (today's members
  tested on past days). The universe is the dataset's own instruments as the dataset records them, and the result
  carries the dataset's limitations (SELECTION_BIAS among them). With the cache gone, so are its two flaws: a
  truncated .json.gz raised an uncaught EOFError, and a half-written file was never replaced (the store writes
  atomically, spec 11.2).
* **Symbols are checked and counted once** (`universe`). Flaw: a symbol was used in a cache path unchecked, so
  "../ESCAPED" read a file outside the cache and two spellings of one file counted its trades twice. A symbol must
  match the ticker pattern and is resolved to one instrument; a symbol the dataset does not hold is left out and
  named.
* **--until and --draws are checked** (`run`). Flaw: neither was; a time off the grid or a negative draw count
  went straight into the arithmetic.
* **The result records what produced it** (QT07): the dataset id and hash, the as-of time, the seed, the draws and
  the code version. Flaw: it named only a source URL and a fetch time.
* **Costs: each leg on its own price** (through `traderos.timemachine.plan.one_trade`, see that module).
* **The four variants are one research family** (spec 09.4, FS10). Flaw: four variants were printed as four
  results, with nothing saying they were tried together. Every run reports all four, never only the best, says how
  many earlier runs of the family this store holds on the same dataset, and records itself in research memory.
* **The report names the dataset, not "S&P 500"**. Flaw: the words were hard-coded whatever the universe was.

Every stock here shares the same days: one market, one stretch of time. A result from it is a first look, not a
verdict for every year (the upstream note, kept).
"""
from __future__ import annotations

import contextlib
import datetime
import random
import re

from traderos.clock import XNYS
from traderos.contracts import (
    ActorRole,
    ErrorCode,
    IntegrityError,
    ResearchMemoryEvent,
    canonical_bytes,
    decimal_from_float,
    sha256_bytes,
    with_hash,
)
from traderos.timemachine import plan as tm

DRAWS = 2000
MAX_DRAWS = 100000
FAMILY = "RF-orb-15m-or_mid-long"     # one research family: the rule; the variants below are its members
VARIANTS = [  # (label, target R, fill, cost bps a side)
    ("1R, next open, 2 bps", 1.0, "next_open", 2.0),
    ("2R, next open, 2 bps", 2.0, "next_open", 2.0),
    ("1R, next open, no costs", 1.0, "next_open", 0.0),
    ("1R, signal close, no costs (old)", 1.0, "close", 0.0),
]
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


# ---------------------------------------------------------------- the universe (the dataset's, as of its date)

def universe(manifest, symbols=None):
    """The dataset's instruments, or the symbols you give that it holds, each once: ([(symbol, instrument_id)],
    [symbols not in the dataset]). A symbol that is not a ticker is refused."""
    held = {i.symbol: i.instrument_id for i in manifest.instruments}
    if symbols is None:
        return sorted(held.items()), []
    out, missing, seen = [], [], set()
    for raw in symbols:
        sym = str(raw).strip().strip('"').upper()
        if not SYMBOL.match(sym):
            raise tm.TMError("%r is not a ticker symbol" % raw)
        if sym in seen:
            continue
        seen.add(sym)
        if sym in held:
            out.append((sym, held[sym]))
        else:
            missing.append(sym)
    return out, missing


def load_sessions(reader, dataset_id, now_utc, cal, symbols=None):
    """{symbol: full sessions before the as-of New York date}, for symbols with at least MIN_SESSIONS of them."""
    manifest = reader.manifest(dataset_id)
    syms, missing = universe(manifest, symbols)
    today = tm.new_york(now_utc).date().isoformat()
    out = {}
    for s, _ in syms:
        _, _, bars, _, _ = tm.read_bars(reader, dataset_id, s, now_utc, cal)
        sessions, _ = tm.full_sessions(tm.split_sessions(bars), today)
        if len(sessions) >= tm.MIN_SESSIONS:
            out[s] = sessions
    return manifest, out, missing


# ---------------------------------------------------------------- run

def control_r(sessions, tr, last, rule, fill, cost_bps):
    """Average R of a blind entry at the trade's bar on the stock's other sessions."""
    kk, frac = tr["bar"], tr["risk"] / tr["signal"]
    rs = []
    for d, D in sessions:
        if d == tr["date"]:
            continue
        p = round(D[kk]["c"], 2)
        s = tm.cents_away(p * (1 - frac), True)
        risk = p - s
        if risk <= 0:
            continue
        t = round(p + rule["target_r"] * risk, 2)
        ct = tm.one_trade(D, kk, p, s, t, last, True, fill, cost_bps)
        if not isinstance(ct, str):
            rs.append(ct["R"])
    return sum(rs) / len(rs) if rs else None


def day_bootstrap(rows, key, rng, draws):
    """95 % range of the mean of row[key], resampling whole days."""
    by_day = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r[key])
    days = sorted(by_day)
    sums = [(sum(by_day[d]), len(by_day[d])) for d in days]
    means = []
    for _ in range(draws):
        tot = n = 0
        for _ in days:
            s, c = sums[rng.randrange(len(days))]
            tot += s
            n += c
        means.append(tot / n)
    means.sort()
    return tm.pct(means, 0.025), tm.pct(means, 0.975)


def run_variant(data, target_r, fill, cost_bps, last, seed, draws):
    rule = {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": target_r}
    rows = []
    skipped = {"no_bar": 0, "rejected": 0}
    for sym, sessions in data.items():
        h = tm.history_replay(sessions, rule, last, "buy", fill, cost_bps)
        for k in skipped:
            skipped[k] += h["skipped"][k]
        for tr in h["trades"]:
            c = control_r(sessions, tr, last, rule, fill, cost_bps)
            if c is None:
                continue
            rows.append({"symbol": sym, "date": tr["date"], "R": tr["R"], "control": c, "diff": tr["R"] - c,
                         "outcome": tr["outcome"]})
    n = len(rows)
    if not n:
        return {"trades": 0}
    rng = random.Random(seed)
    mean = lambda k: sum(r[k] for r in rows) / n  # noqa: E731
    out = {"trades": n, "symbols": len({r["symbol"] for r in rows}), "days": len({r["date"] for r in rows}),
           "skipped": skipped,
           "target": sum(r["outcome"] == "target" for r in rows) / n,
           "stop": sum(r["outcome"] == "stop" for r in rows) / n,
           "neither": sum(r["outcome"] == "close" for r in rows) / n,
           "avg_R": mean("R"), "control_R": mean("control"), "edge_R": mean("diff")}
    out["R_ci"] = day_bootstrap(rows, "R", rng, draws)
    out["edge_ci"] = day_bootstrap(rows, "diff", rng, draws)
    dates = sorted({r["date"] for r in rows})
    half = dates[len(dates) // 2]
    for name, part in (("first_half", [r for r in rows if r["date"] < half]),
                       ("second_half", [r for r in rows if r["date"] >= half])):
        if part:
            out[name] = {"trades": len(part), "avg_R": sum(r["R"] for r in part) / len(part),
                         "edge_R": sum(r["diff"] for r in part) / len(part),
                         "from": min(r["date"] for r in part),
                         "to": max(r["date"] for r in part)}
    return out


def run(reader, dataset_id, now_utc, *, until="15:55", seed=19, draws=DRAWS, symbols=None, cal=None):
    """Every variant on every symbol the dataset holds (or the ones you name) -> the study document."""
    cal = cal or XNYS()
    if isinstance(draws, bool) or not isinstance(draws, int) or not 1 <= draws <= MAX_DRAWS:
        raise tm.TMError("--draws must be a whole number from 1 to %d" % MAX_DRAWS)
    until_min = tm.parse_clock(until, "--until")
    if (until_min - tm.OPEN_MIN) % 5 or not tm.OPEN_MIN + 20 <= until_min <= tm.CLOSE_MIN:
        raise tm.TMError("--until must be on the 5-minute grid, from 9:50 AM (one bar after the 15-minute range) "
                         "to 4:00 PM, not %r" % until)
    manifest, data, missing = load_sessions(reader, dataset_id, now_utc, cal, symbols)
    if not data:
        raise tm.TMError("no symbol in %s has %d or more full sessions of 5-minute bars before %s"
                         % (dataset_id, tm.MIN_SESSIONS, tm.new_york(now_utc).date()),
                         code=ErrorCode.INSUFFICIENT_EVIDENCE)
    last = (until_min - tm.OPEN_MIN) // 5 - 1
    days = sorted({d for ss in data.values() for d, _ in ss})
    results = []
    for label, r, fill, cost in VARIANTS:
        res = run_variant(data, r, fill, cost, last, seed, draws)
        res["label"] = label
        results.append(res)
    return {"created_by": "traderos.timemachine.orb_study", "research_family_id": FAMILY,
            "variants_tried": len(VARIANTS), "dataset_id": manifest.dataset_id, "dataset_name": manifest.name,
            "dataset_hash": manifest.canonical_hash, "as_of": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stocks": len(data), "sessions": len(days), "from": days[0], "to": days[-1],
            "until": tm.clock24(until_min), "seed": seed,
            "draws": draws, "code_version": code_version(), "not_in_dataset": missing,
            "limitations": [lim.code for lim in manifest.limitations], "variants": results}


def code_version():
    import pathlib
    return "traderos %s; orb_study/1 sha256 %s; plan_replay/1 sha256 %s" % (
        tm.__version__, sha256_bytes(pathlib.Path(__file__).read_bytes())[:16],
        sha256_bytes(pathlib.Path(tm.__file__).read_bytes())[:16])


def fmt(v):
    lo, hi = v
    return "%+.3f to %+.3f" % (lo, hi)


def describe(doc):
    """The study in words (the upstream report, with the dataset's name and the research family)."""
    out = ["Opening-range breakout (15-min range, stop at the midpoint, long only) on %d stocks of %s, %s to %s, "
           "out by %s. Ranges: 95 %%, resampling whole days (%d draws, seed %d)."
           % (doc["stocks"], doc["dataset_name"], doc["from"], doc["to"],
              tm.clock12(int(doc["until"][:2]) * 60 + int(doc["until"][3:])), doc["draws"], doc["seed"]),
           "Research family %s: these %d variants were tried together; read any one of them with the other %d in "
           "mind.%s" % (doc["research_family_id"], doc["variants_tried"], doc["variants_tried"] - 1,
                        " This store holds %d earlier run(s) of this family on this dataset."
                        % doc["earlier_runs"] if doc.get("earlier_runs") else "")]
    if doc.get("not_in_dataset"):
        out.append("Not in the dataset, left out: %s." % ", ".join(doc["not_in_dataset"]))
    for v in doc["variants"]:
        if not v["trades"]:
            out.append("\n%s: no trades." % v["label"])
            continue
        out.append("\n%s: %s trades on %d stocks and %d days (left out: %d rejected brackets, %d with no bar)."
                   % (v["label"], "{:,}".format(v["trades"]), v["symbols"], v["days"], v["skipped"]["rejected"],
                      v["skipped"]["no_bar"]))
        out.append("  target first %.1f %%, stop first %.1f %%, neither %.1f %%"
                   % (100 * v["target"], 100 * v["stop"], 100 * v["neither"]))
        out.append("  average R        %+.3f   (%s)" % (v["avg_R"], fmt(v["R_ci"])))
        out.append("  blind entry R    %+.3f" % v["control_R"])
        out.append("  breakout adds    %+.3f R a trade   (%s)" % (v["edge_R"], fmt(v["edge_ci"])))
        for name in ("first_half", "second_half"):
            h = v.get(name)
            if h:
                out.append("  %s to %s: %s trades, average R %+.3f, breakout adds %+.3f"
                           % (h["from"], h["to"], "{:,}".format(h["trades"]), h["avg_R"], h["edge_R"]))
    out.append("\nEvery stock shares the same %d days: one market, one stretch of time. A first look, not a verdict."
               % doc["sessions"])
    return out


# ---------------------------------------------------------------- the record of a run (research memory)

def plain(v):
    """Floats -> decimal strings, so the document can be hashed and stored (contracts refuse floats)."""
    if isinstance(v, float):
        d = decimal_from_float(v, 6)
        return None if d is None else str(d.normalize())
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    return v


def study(store, dataset_id, *, now_utc=None, until="15:55", seed=19, draws=DRAWS, symbols=None):
    """Run the study on the store's dataset, keep its document as a content-addressed artifact and register the run
    in research memory under the family. -> (the stored document, decimals as strings; the float document)."""
    now_utc = now_utc or tm.parse_now(None)
    doc = run(store.bar_reader(), dataset_id, now_utc, until=until, seed=seed, draws=draws, symbols=symbols)
    body = plain(doc)
    # the same settings and the same results again are a replay of one run, whatever the clock said (spec 08.2)
    identity = sha256_bytes(canonical_bytes({k: v for k, v in body.items() if k != "as_of"}))
    event_id = "ME-orb_study.completed-%s" % identity[:16]
    earlier = {e.event_id for e in store.search_memory(FAMILY, kinds=("orb_study.completed",), limit=200)
               if dataset_id in e.subject_refs}
    digest = store.put_artifact(canonical_bytes(body), "application/json")
    doc["earlier_runs"] = body["earlier_runs"] = len(earlier - {event_id})
    best = max((v for v in doc["variants"] if v["trades"]), key=lambda v: v["avg_R"], default=None)
    summary = ("ORB study (%s) on %s: %d variants, %d stocks, %s to %s%s"
               % (FAMILY, dataset_id, len(VARIANTS), doc["stocks"], doc["from"], doc["to"],
                  "; highest average R %+.3f (%s), one of %d tried" % (best["avg_R"], best["label"], len(VARIANTS))
                  if best else "; no trades"))
    event = ResearchMemoryEvent(
        event_id=event_id, actor_role=ActorRole.SYSTEM, kind="orb_study.completed",
        subject_refs=(FAMILY, dataset_id), outcome_codes=tuple(doc["limitations"]), summary=summary,
        payload_hash=digest, previous_event_hash=store.last_memory_hash(),
        at=datetime.datetime.now(datetime.UTC))
    with contextlib.suppress(IntegrityError):     # the same run again: a replay, never a new member of the family
        store.remember(with_hash(event))
    return body, doc


__all__ = ["FAMILY", "VARIANTS", "describe", "run", "run_variant", "study", "universe"]
