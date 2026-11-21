"""Mutants of the Time Machine's key rules (ADR-010): edit one rule, run the tests, put it back.

    python tools/mutants/timemachine.py                 # every mutant, with this Python
    python tools/mutants/timemachine.py --python PY     # with another interpreter
    python tools/mutants/timemachine.py --only walk     # mutants whose name contains "walk"

Each mutant replaces one exact piece of source (it must occur exactly once, or the run says NOT APPLIED) and runs
the Time Machine tests with bytecode off (`-B`, PYTHONDONTWRITEBYTECODE, every `__pycache__` removed before and
after), so a mutant can never be judged by the previous one's cached bytecode. KILLED means a test failed;
SURVIVED means the tests did not notice the change. The source is restored in a `finally`, whatever happens. The exit
code is 0 only when every mutant applied and was killed.

Groups: "port" are the 40 mutants of the first port (#9); "osdev6" are the 7 OSDev6 found surviving in his review of
#9 (with their short-side mirrors); "review" are the mutants of the fixes for that review.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
P = "src/traderos/timemachine/plan.py"
B = "src/traderos/timemachine/orb_study.py"
T = "src/traderos/timemachine/__init__.py"
C = "src/traderos/contracts/research.py"
L = "src/traderos/commands/lab.py"
R = "src/traderos/research/__init__.py"
TESTS = ("tests/unit/test_timemachine_plan.py", "tests/unit/test_orb_study.py", "tests/unit/test_timemachine.py",
         "tests/contract/test_contracts_v1.py")

WALK_LONG_TOUCHES = ('            if b["l"] <= stop:\n                return "stop", stop, b["h"] >= target\n'
                     '            if b["h"] >= target:\n                return "target", target, False\n        else:')
WALK_LONG_GAPS = ('            if gaps and b["o"] >= target:\n                return "target", b["o"], False\n'
                  '            if b["l"] <= stop:\n')
WALK_SHORT_GAPS = ('            if gaps and b["o"] <= target:\n                return "target", b["o"], False\n'
                   '            if b["h"] >= stop:\n')

MUTANTS = [
    # ---- port: the first 40 (#9)
    ("port", "session draw becomes independent", P, 'jump = 1.0 / block if method == "block" else 0.0',
     'jump = 1.0 / block if method == "block" else 1.0'),
    ("port", "fill at the signal close", P, 'e, gaps = round(D[kk + 1]["o"], 2), True', "e, gaps = signal, True"),
    ("port", "no rejected bracket", P, "        if not ((stop < e < target) if long else (target < e < stop)):\n"
     '            return "rejected"', '        if False:\n            return "rejected"'),
    ("port", "gap exits at the stop, not the open", P,
     'if gaps and b["o"] <= stop:\n                return "stop", b["o"], False',
     'if gaps and b["o"] <= stop:\n                return "stop", stop, False'),
    ("port", "walk: target touch checked before the stop's", P, WALK_LONG_TOUCHES,
     '            if b["h"] >= target:\n                return "target", target, False\n'
     '            if b["l"] <= stop:\n                return "stop", stop, b["h"] >= target\n        else:'),
    ("port", "ambiguous bar not flagged", P, 'return "stop", stop, b["h"] >= target', 'return "stop", stop, False'),
    ("port", "both cost legs on the fill", P, "cost_bps / 10000.0 * (fill + exit_price) / risk",
     "cost_bps / 10000.0 * (fill + fill) / risk"),
    ("port", "history default cost 0", P, 'def history_replay(sessions, rule, last, side="buy", fill="next_open", '
     "cost_bps=DEFAULT_COST_BPS):", 'def history_replay(sessions, rule, last, side="buy", fill="next_open", '
     "cost_bps=0.0):"),
    ("port", "R rounded before averaging", P,
     '"avg_R": statistics.mean(x["R"] for x in trades) if trades else None,',
     '"avg_R": statistics.mean(round(x["R"], 2) for x in trades) if trades else None,'),
    ("port", "iid bootstrap for the setup", P,
     "lo, hi, block = stationary_bootstrap_mean(setup, seed=seed, resamples=draws)",
     "lo, hi, block = stationary_bootstrap_mean(setup, seed=seed, resamples=draws, block=1)"),
    ("port", "edge against the pooled blind mean", P,
     'edges = [x["R"] - c for x, c in zip(trades, controls, strict=True) if c is not None]',
     'edges = [x["R"] - statistics.mean(rs) for x, c in zip(trades, controls, strict=True) if c is not None]'),
    ("port", "too-few uses <=", P, '    if n < b.get("min_trades", MIN_TRADES):\n        return ("Too few',
     '    if n <= b.get("min_trades", MIN_TRADES):\n        return ("Too few'),
    ("port", "MIN_TRADES 30", P, "MIN_TRADES = 100     #", "MIN_TRADES = 30     #"),
    ("port", "strong label before sign-off", P, "elif abs(d) < STRONG or not SIGNED_OFF:", "elif abs(d) < STRONG:"),
    ("port", "no not-a-qualification tail on a baseline line", P, 'tail = "" if SIGNED_OFF else (',
     'tail = "" if True else ('),
    ("port", "#25 settled trade not refused", P,
     '    if outcome:\n        raise TMError("the real bars already settled',
     '    if False:\n        raise TMError("the real bars already settled'),
    ("port", "#25 refusal code", P, "code=ErrorCode.LOOKAHEAD_RISK)\n    if later_bars:",
     "code=ErrorCode.INVALID_INPUT)\n    if later_bars:"),
    ("port", "incomplete bars used", P, "        if not b.complete:\n", "        if False:\n"),
    ("port", "incomplete entry bar code", P, "code=ErrorCode.INCOMPLETE_BAR)", "code=ErrorCode.MISSING_DATA)"),
    ("port", "reads past as_of", P, "reader.bars(dataset_id, ins.instrument_id, as_of=as_of)",
     "reader.bars(dataset_id, ins.instrument_id, as_of=as_of + datetime.timedelta(days=1))"),
    ("port", "today in the pool", P, "        if d >= before:\n            continue",
     "        if d > before:\n            continue"),
    ("port", "under-20 sessions code", P, "code=ErrorCode.INSUFFICIENT_EVIDENCE)\n    pool = build_pool",
     ")\n    pool = build_pool"),
    ("port", "crash at the fill bar", P, "return tuple(x * f for x in r) if j == 1 else r",
     "return tuple(x * f for x in r) if j == 0 else r"),
    ("port", "crash always down", P, "f = (1 - drop) if long else (1 + drop)", "f = 1 - drop"),
    ("port", "doubling keeps the intrabar shape only", P, "return tuple(x ** multiplier for x in r)",
     "return tuple(x * r[3] ** (multiplier - 1) for x in r)"),
    ("port", "#2 flat_by default ignored", P, '    fb = plan.get("flat_by")\n    if not fb:',
     "    fb = None\n    if not fb:"),
    ("port", "legacy run recorded", P, '    if doc.get("legacy"):\n        raise TMError("a legacy',
     '    if False:\n        raise TMError("a legacy'),
    ("port", "green against the entry", P, 'if o == "target" or (o == "close" and (p > f if long else p < f)))',
     'if o == "target" or (o == "close" and (p > entry if long else p < entry)))'),
    ("port", "signals not counted apart", P, '"signals": len(trades) + sum(skipped.values()),',
     '"signals": len(trades),'),
    ("port", "plan file unbounded", P, "    if p.stat().st_size > MAX_PLAN_BYTES:", "    if False:"),
    ("port", "TM default window reads the holdout", T,
     "            end = max(s.end for s in splits if s.name is not SplitName.HOLDOUT)\n"
     "            if earliest is not None and earliest.start <= end:\n"
     "                end = cal.previous_session(earliest.start)\n",
     "            end = splits[-1].end\n"),
    ("port", "HOLDOUT_READ never said", T, "    for h in holdouts_read(holdouts, start, end):",
     "    for h in []:"),
    ("port", "two subjects allowed", C,
     "        if self.plan_replay is not None and any(s is not None for s in strategy):", "        if False:"),
    ("port", "plan subject levels unchecked", C,
     "        if not ((self.stop < self.entry < self.target) if long else (self.target < self.entry < self.stop)):",
     "        if False:"),
    ("port", "study: any symbol text", B, "        if not SYMBOL.match(sym):", "        if False:"),
    ("port", "study: a symbol counted twice", B, "        if sym in seen:\n            continue",
     "        if False:\n            continue"),
    ("port", "study: draws unchecked", B, "not 1 <= draws <= MAX_DRAWS", "not -10 <= draws <= MAX_DRAWS"),
    ("port", "study: until unchecked", B,
     "if (until_min - tm.OPEN_MIN) % 5 or not tm.OPEN_MIN + 20 <= until_min <= tm.CLOSE_MIN:", "if False:"),
    ("port", "study: a replay counted as a new run", B,
     'identity = sha256_bytes(canonical_bytes({k: v for k, v in body.items() if k != "as_of"}))',
     "identity = sha256_bytes(canonical_bytes(body))"),
    ("port", "CLI: wrong-mode flags accepted", L, "    if wrong:\n        raise ValidationError",
     "    if False:\n        raise ValidationError"),
    # ---- osdev6: his 7 survivors in the review of #9, with short-side mirrors
    ("osdev6", "walk: stop touch exclusive (long)", P,
     'if b["l"] <= stop:\n                return "stop", stop, b["h"] >= target',
     'if b["l"] < stop:\n                return "stop", stop, b["h"] >= target'),
    ("osdev6", "walk: stop touch exclusive (short)", P,
     'if b["h"] >= stop:\n                return "stop", stop, b["l"] <= target',
     'if b["h"] > stop:\n                return "stop", stop, b["l"] <= target'),
    ("osdev6", "walk: ambiguous flag exclusive (long)", P, 'return "stop", stop, b["h"] >= target',
     'return "stop", stop, b["h"] > target'),
    ("osdev6", "walk: ambiguous flag exclusive (short)", P, 'return "stop", stop, b["l"] <= target',
     'return "stop", stop, b["l"] < target'),
    ("osdev6", "rejected bracket fills at the stop", P, "if not ((stop < e < target) if long",
     "if not ((stop <= e < target) if long"),
    ("osdev6", "rejected bracket fills at the target", P, "if not ((stop < e < target) if long",
     "if not ((stop < e <= target) if long"),
    ("osdev6", "real_outcome ignores the last bar", P, "avail = sorted(s for s in entry_day if k < s <= last)",
     "avail = sorted(s for s in entry_day if k < s < last)"),
    ("osdev6", "HOLDOUT_READ: window ending on the holdout's first day", T,
     "if h.start <= end and start <= h.end})", "if h.start < end and start <= h.end})"),
    ("osdev6", "HOLDOUT_READ: window starting on the holdout's last day", T,
     "if h.start <= end and start <= h.end})", "if h.start <= end and start < h.end})"),
    ("osdev6", "lab overlap: a report ending on the holdout's first day", R,
     "if not (report.window_start <= mine.end and mine.start <= report.window_end):",
     "if not (report.window_start <= mine.end and mine.start < report.window_end):"),
    ("osdev6", "lab overlap: a report starting on the holdout's last day", R,
     "if not (report.window_start <= mine.end and mine.start <= report.window_end):",
     "if not (report.window_start < mine.end and mine.start <= report.window_end):"),
    ("osdev6", "ORB signal on a close equal to the range high", P, '(D[i]["c"] > oh if long else D[i]["c"] < ol)',
     '(D[i]["c"] >= oh if long else D[i]["c"] < ol)'),
    ("osdev6", "ORB signal on a close equal to the range low (short)", P,
     '(D[i]["c"] > oh if long else D[i]["c"] < ol)', '(D[i]["c"] > oh if long else D[i]["c"] <= ol)'),
    # ---- review: the fixes for OSDev6's review of #9
    ("review", "HIGH 1: an orb entry inside its range is accepted", P,
     'if setup.lower() == "orb" and k < or_min // 5:', "if False:"),
    ("review", "HIGH 1: the range-end check is off by one bar", P,
     'if setup.lower() == "orb" and k < or_min // 5:', 'if setup.lower() == "orb" and k < or_min // 5 - 1:'),
    ("review", "HIGH 1: resolve_rule reads range bars after the entry", P,
     'if n <= plan["entry_bar"] and all(i in entry_day for i in range(n)):',
     "if all(i in entry_day for i in range(n)):"),
    ("review", "HIGH 2: registered family holdouts ignored", T,
     "            out += [s for s in spec.splits if s.name is SplitName.HOLDOUT]", "            out += []"),
    ("review", "HIGH 2: default end ignores the earliest holdout", T,
     "            if earliest is not None and earliest.start <= end:", "            if False:"),
    ("review", "MEDIUM 3: the target's gap checked after the stop's touch (long)", P, WALK_LONG_GAPS,
     '            if b["l"] <= stop:\n'),
    ("review", "MEDIUM 3: the target's gap checked after the stop's touch (short)", P, WALK_SHORT_GAPS,
     '            if b["h"] >= stop:\n'),
    ("review", "MEDIUM 4: crash reported when it cannot apply", P,
     'if name == "crash gap" and last - k < 2:', "if False:"),
    ("review", "MEDIUM 4: crash applicability off by one bar", P,
     'if name == "crash gap" and last - k < 2:', 'if name == "crash gap" and last - k < 1:'),
    ("review", "MEDIUM 5: identity without costs and cash", T,
     "                                             cost_model.params(), str(account.starting_cash)]))[:20]",
     "                                             ]))[:20]"),
    ("review", "LOW 6: duplicate keys accepted", P, "            if k in seen:\n                raise TMError(",
     "            if False:\n                raise TMError("),
    ("review", "LOW 7: plan flags beside --plan ignored again", L, "    if args.plan and as_flags:",
     "    if args.plan and args.symbol:"),
    ("review", "LOW 11: plan values not checked when read", P, "    plan_hash(out)          #", "    out          #"),
    ("review", "LOW 11: lists left unconverted", P, "        if isinstance(v, (list, tuple)):", "        if False:"),
    ("review", "LOW 11: a non-finite nested number accepted", P,
     "            if d is None:\n                raise TMError(\"the plan's", "            if False:\n"
     "                raise TMError(\"the plan's"),
    ("review", "A: the trade minimum's sign-off unsaid", P,
     'MIN_TRADES_SIGNED_OFF = "signed off by the founder 2026-11-20"', 'MIN_TRADES_SIGNED_OFF = "proposed"'),
    ("review", "A: the quant review no longer said pending", P,
     'PENDING = "the independent quant review is pending (spec 09.4)"', 'PENDING = "it is reviewed"'),
]


def clean() -> None:
    for d in ROOT.rglob("__pycache__"):
        shutil.rmtree(d, ignore_errors=True)


def run_one(python: str, rel: str, old: str, new: str) -> str:
    path = ROOT / rel
    src = path.read_text(encoding="utf-8")
    if src.count(old) != 1:
        return f"NOT APPLIED ({src.count(old)} matches)"
    env = dict(os.environ, PYTHONPATH="src", PYTHONDONTWRITEBYTECODE="1")
    try:
        path.write_text(src.replace(old, new), encoding="utf-8")
        clean()
        r = subprocess.run([python, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-q", "-x",
                            *TESTS], cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800)
        tail = (r.stdout.strip().splitlines() or ["?"])[-1]
        return ("KILLED   " if r.returncode else "SURVIVED ") + tail
    finally:
        path.write_text(src, encoding="utf-8")
        clean()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--only", default="")
    a = ap.parse_args(argv)
    chosen = [m for m in MUTANTS if a.only in m[1]]
    results = []
    for group, name, rel, old, new in chosen:
        verdict = run_one(a.python, rel, old, new)
        results.append((group, name, verdict))
        print(f"[{group}] {name}: {verdict}", flush=True)
    print()
    for group in ("port", "osdev6", "review"):
        mine = [r for r in results if r[0] == group]
        if mine:
            killed = sum(1 for r in mine if r[2].startswith("KILLED"))
            print(f"{group}: {killed} of {len(mine)} killed")
    killed = sum(1 for r in results if r[2].startswith("KILLED"))
    print(f"all: {killed} of {len(results)} killed")
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
