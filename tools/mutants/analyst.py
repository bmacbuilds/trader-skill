"""Mutants of the S08 Quant Analyst's rules: edit one rule, run the analyst tests, put it back.

    python tools/mutants/analyst.py                 # every mutant, with this Python
    python tools/mutants/analyst.py --python PY     # with another interpreter
    python tools/mutants/analyst.py --only sign     # mutants whose name contains "sign"
    python tools/mutants/analyst.py --check         # only prove each mutant still applies (no tests run)

Each mutant replaces one exact piece of source (it must occur exactly once, or the run says NOT APPLIED) and runs
tests/unit/test_quant_analyst.py with bytecode off (`-B`, PYTHONDONTWRITEBYTECODE, every `__pycache__` removed before
and after), so a mutant can never be judged by the previous one's cached bytecode. KILLED means a test failed;
SURVIVED means the tests did not notice the change. The source is restored in a `finally`, whatever happens. The exit
code is 0 only when every mutant applied and was killed.

Groups: "osdev6" puts back each hole OSDev6 found in his review of #14 at 1f8e520 (a sign flip passes, a unit glued to
a number is not read, a number matches any cite, the holdout set beside in-sample baselines); "review" breaks each rule
added to close them.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
A = "src/traderos/analyst/__init__.py"
TESTS = ("tests/unit/test_quant_analyst.py",)

MUTANTS = [
    # ---- osdev6: his four holes, put back
    ("osdev6", "HIGH 1: only the magnitude is compared (a sign flip passes)", A,
     "value = n.magnitude * k * (sign if sign is not None else (-1 if _depth(f) else 1))",
     "value = n.magnitude * k * (-1 if f.value < 0 else 1)"),
    ("osdev6", "HIGH 2: a unit glued to a number is not read", A,
     'if j < len(toks) and toks[j].start == b and (toks[j].kind in ("word", "ident") or toks[j].text == "\\u00d7"):',
     "if False:"),
    ("osdev6", "MEDIUM 4: a number no name ties falls back to any field it matches", A,
     "        tied = before or after\n",
     "        tied = before or after or set([g for g in ev.fields if _judge(n, g, n.sign)[0] == PASS][:1])\n"),
    ("osdev6", "HIGH 3: explain sets the first window (the holdout) beside the baselines", A,
     'mine = p.metrics.get(f"{BASELINE_WINDOW}.{key}")', 'mine = p.metrics.get(f"{order[0]}.{key}")'),
    # ---- review: the sign
    ("review", "sign: a loss or gain word is ignored", A,
     "said = {+1 if w in _GAIN else -1 for w in signs if w in _GAIN or w in _LOSS}", "said = set()"),
    ("review", "sign: (21.98%) is not a loss", A,
     "sign, a, b, opened = -1, a - 1, b + 1, False", "a, b, opened = a - 1, b + 1, False"),
    ("review", "sign: the minus sign U+2212 is not a sign", A, '"\\u2212": -1, ', ""),
    ("review", "sign: an unsigned drawdown is not a depth", A,
     'return f.key.endswith("max_drawdown") and f.unit == "fraction"', "return False"),
    ("review", "sign: every unsigned fraction is read as a loss", A,
     'return f.key.endswith("max_drawdown") and f.unit == "fraction"', 'return f.unit == "fraction"'),
    ("review", "sign: a written sign that disagrees with its word is let through", A,
     "if len(said) > 1 or (said and n.sign is not None and said != {n.sign}):", "if len(said) > 1:"),
    ("review", "sign: a hyphen joining T+1 or 5-10 is read as a sign", A,
     '(k > 0 and (text[k - 1].isalnum() or text[k - 1] in "._%)"))', "False"),
    # ---- review: the unit
    ("review", "unit: an unknown suffix is read as plain", A,
     'suffix_unit, scale = _SUFFIX.get(suffix.lower(), ("?", 1))',
     'suffix_unit, scale = _SUFFIX.get(suffix.lower(), ("", 1))'),
    ("review", "unit: k, m and b do not scale", A, "magnitude=written * scale,", "magnitude=written,"),
    ("review", "unit: bps scaled as a percent", A,
     '_SCALE = {"percent": Decimal("0.01"), "bps": Decimal("0.0001")}',
     '_SCALE = {"percent": Decimal("0.01"), "bps": Decimal("0.01")}'),
    ("review", "unit: a fraction may be written as a plain number", A,
     '_ALLOWED = {"fraction": {"percent", "bps"},', '_ALLOWED = {"fraction": {"percent", "bps", "plain"},'),
    ("review", "unit: a ratio may be written as a percent", A,
     '"ratio": {"plain", "x"},', '"ratio": {"plain", "x", "percent"},'),
    ("review", "unit: a count word is not read", A, "        elif count:\n", "        elif False:\n"),
    # ---- review: the magnitude and its precision
    ("review", "precision: coarser than the Passport states is accepted", A, "if step > _stated(f):", "if False:"),
    ("review", "precision: the tolerance is a whole step, not half", A,
     "if abs(value - f.value) * 2 > step:", "if abs(value - f.value) > step:"),
    ("review", "precision: a fraction is stated at 1%, not 0.01%", A,
     '        return Decimal("0.0001")\n', '        return Decimal("0.01")\n'),
    ("review", "precision: shown with half-even rounding", A,
     'pct = value.scaleb(2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)',
     'pct = value.scaleb(2).quantize(Decimal("0.01"))'),
    ("review", "precision: a negative zero shows as -0.00%", A,
     'return f"{pct.copy_abs() if pct == 0 else pct}%"', 'return f"{pct}%"'),
    # ---- review: tying a number to one field
    ("review", "tie: a comparison word may stand between name and number", A,
     '_FILLER = frozenset(("is",', '_FILLER = frozenset(("above", "about", "is",'),
    ("review", "tie: two possible fields are settled by picking one", A,
     "        if len(tied) > 1:\n", "        if False:\n"),
    ("review", "tie: the sentence's windows are added to the clause's", A,
     "        if not found:\n            found = self._found(self._window_rx, self._windows, text, *sentence)",
     "        if True:\n            found |= self._found(self._window_rx, self._windows, text, *sentence)"),
    ("review", "tie: the word baseline anywhere in the sentence makes every clause a baseline's", A,
     "text[clause[0]:clause[1]], re.I):", "text[sentence[0]:sentence[1]], re.I):"),
    ("review", "tie: an explicit [ref] that contradicts the name wins", A,
     "            if tied and tied != {cited}:\n", "            if False:\n"),
    ("review", "tie: an explicit [ref] to another window than its sentence's wins", A,
     'if cited.kind == "window" and (named := cands & ev.window_names) and cited.subject not in named:',
     "if False:"),
    ("review", "tie: a [ref]'s own words count as the window its sentence names", A,
     "bare = _REF_TOKEN.sub(lambda m: \" \" * len(m.group(0)), text)", "bare = text"),
    ("review", "tie: a bracketed reference with digits that follows no number is let through", A,
     'if t.kind == "ref" and re.search(r"\\d", t.text) and t.end not in cited_at:', "if False:"),
    ("review", "tie: a number in words is let through", A,
     "    for m in _NUMBER_WORDS.finditer(text):", "    for m in []:"),
    ("review", "tie: a negation or condition in the sentence is ignored", A,
     "        if hedge := hedges.get(tx.sentence[n.tok]):\n", "        if hedge := None:\n"),
    ("review", "tie: a qualifier beside it (a year, annual, gross) is ignored", A,
     "if word := next((w for w in tx.around(first, last) if w in _QUALIFIERS and w not in _may_say(f)), None):",
     "if word := None:"),
    ("review", "tie: cagr may not be said a year", A, 'if f.key == "cagr" or "per_year" in f.key:', "if False:"),
    ("review", "tie: a baseline may be put in the holdout", A,
     'if f.kind == "baseline" and (elsewhere := ', 'if False and (elsewhere := '),
    ("review", "tie: a clause about an unnamed baseline falls back to the strategy", A,
     '                return {"passport"}, "it names a baseline but not which one"\n',
     '                return self._found(self._window_rx, self._windows, text, *clause) | {"passport"}, ""\n'),
    # ---- review: quotes
    ("review", "quote: a three-word sentence (or less) may vouch for a number", A,
     'len(re.findall(r"[A-Za-z]{2,}", sentence)) >= 4', 'len(re.findall(r"[A-Za-z]{2,}", sentence)) >= 1'),
    ("review", "quote: a verbatim finding is not recognised", A,
     "quote = next((ref for a, b, ref in quotes if a <= n.start and n.end <= b), None)", "quote = None"),
    # ---- review: the verdict of the whole text, and explain
    ("review", "check: an UNVERIFIED number lets the text through", A,
     'bad = [c for c in claims if c["status"] != PASS]', 'bad = [c for c in claims if c["status"] == FAIL]'),
    ("review", "explain: the interval's level is 90%", A,
     '(the level each interval names)", Decimal("0.95"), "fraction"))',
     '(the level each interval names)", Decimal("0.90"), "fraction"))'),
    ("review", "explain: windows without a baseline are not said", A, "        if others:\n", "        if False:\n"),
]


def clean() -> None:
    for d in ROOT.rglob("__pycache__"):
        shutil.rmtree(d, ignore_errors=True)


def run_one(python: str, rel: str, old: str, new: str, check: bool) -> str:
    path = ROOT / rel
    src = path.read_text(encoding="utf-8")
    if src.count(old) != 1:
        return f"NOT APPLIED ({src.count(old)} matches)"
    if check:
        return "APPLIES"
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
    ap.add_argument("--check", action="store_true", help="only prove each mutant applies")
    a = ap.parse_args(argv)
    chosen = [m for m in MUTANTS if a.only in m[1]]
    results = []
    for group, name, rel, old, new in chosen:
        verdict = run_one(a.python, rel, old, new, a.check)
        results.append((group, name, verdict))
        print(f"[{group}] {name}: {verdict}", flush=True)
    print()
    if a.check:
        applied = sum(1 for r in results if r[2] == "APPLIES")
        print(f"all: {applied} of {len(results)} apply")
        return 0 if applied == len(results) else 1
    for group in ("osdev6", "review"):
        mine = [r for r in results if r[0] == group]
        if mine:
            killed = sum(1 for r in mine if r[2].startswith("KILLED"))
            print(f"{group}: {killed} of {len(mine)} killed")
    killed = sum(1 for r in results if r[2].startswith("KILLED"))
    print(f"all: {killed} of {len(results)} killed")
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
