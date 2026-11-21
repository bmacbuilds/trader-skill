"""S08 Quant Analyst: read a Passport's own numbers back in words, and refuse any number it cannot tie to one.

Spec 12.1 S08: "Interpret a real experiment's statistics, negative controls and uncertainty without replacing engine
calculations. Must cite canonical metrics; refuse unsupported numerical claims."

Two functions, both deterministic and offline:
  explain(store, passport_id)   findings in plain words. Every number in them is one the engine stored, cited by the
                                record it lives in. Nothing is computed here: two numbers are put side by side, never
                                subtracted, and only numbers measured on the same window are put side by side.
  check_claims(store, passport_id, text)
                                ties each number in someone's text (a person's, or Claude's before it answers) to the
                                one Passport field it states, and passes it only when its sign, its magnitude and its
                                unit all agree with that field (HOW_CHECKED). A number that cannot be tied to a field is
                                UNVERIFIED; one that disagrees with its field FAILs. Either refuses the whole text
                                (INSUFFICIENT_EVIDENCE), naming each number, why, and what the field holds.
The /trader skill runs `passport explain` before it talks about a result, and `passport check-claims` on its own
answer before it sends it.
"""
from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from traderos.contracts import (
    ChallengeOutcome,
    ChallengeResult,
    ErrorCode,
    EvidencePassport,
    MetricValue,
    ResearchError,
    ValidationError,
    decimal_str,
)

MAX_TEXT = 64 * 1024
HEADLINE = ("net_return", "trades", "win_rate", "max_drawdown", "sharpe")
ORDER = ("holdout", "validation", "development", "in_sample")
LABEL = {"holdout": "the untouched holdout", "in_sample": "development and validation together"}
# Lab._baselines (traderos.research) measures every baseline on development and validation together, never on the
# holdout; the Passport's own page says so ("Baselines (in-sample, same costs)"). A baseline is set beside that window
# only (tests/unit/test_quant_analyst.py proves the window from the stored numbers).
BASELINE_WINDOW = "in_sample"
PASS, FAIL, UNVERIFIED = "PASS", "FAIL", "UNVERIFIED"

HOW_CHECKED = (
    "Each number is tied to one field: the metric named just before it (at most six plain words between) or just "
    "after it, for the window, baseline or challenge its clause names (else its sentence; a baseline only by the word "
    "'baseline' in the same clause, and never on any window but the one baselines are measured on); or an explicit "
    "[ref] after it, such as [holdout.net_return]; or a sentence quoted verbatim from a stored finding, the test line, "
    "a limitation or an assumption.",
    "It passes only when its sign, its unit and its magnitude agree with that field: a fraction as % or bps, a ratio "
    "plain or as x, money in $ or USD, a count plain or with its own word; written at the precision the Passport "
    "states (0.01% for a fraction, a cent for money, as stored otherwise) or finer, and rounded correctly.",
    "A written sign (+, -, \u2212, (21.98%), or a word such as 'down' or 'gained' just before it) must match; an "
    "unsigned number is positive, except a maximum drawdown's depth.",
    "UNVERIFIED, never PASS: a number with no field or two; an unknown unit (250pct, 2nd, 1e5); a negation or "
    "condition in its sentence (not, never, if, would, before); a word beside it that changes what it measures "
    "(a year, annual, average, gross) unless the field itself is that; a number in words before a unit (five percent).",
)
NOT_CHECKED = (
    "words that compare two numbers (above, below, beat): each number is checked, the comparison is not",
    "dates (2024-01-16) and identifiers (FS08, r2, rsi(2), ADR-005, hashes)",
    "the words around a verbatim quote: its numbers are the stored record's own",
    "a word that changes what a number measures beyond those listed, and a number in words not followed by a unit",
)


@dataclass(frozen=True)
class Cite:
    ref: str                                     # where the value lives, e.g. "PP-x r2 metrics.holdout.net_return"
    value: Decimal
    unit: str


@dataclass
class Finding:
    topic: str
    text: str
    cites: list[Cite] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"topic": self.topic, "text": self.text,
                "cites": [{"ref": c.ref, "value": decimal_str(c.value), "unit": c.unit} for c in self.cites]}


@dataclass(frozen=True)
class Field:
    """One number the Passport holds, and how a text can name it."""
    kind: str        # "window", "baseline", "challenge", "passport" or "rules"
    subject: str     # the window, "baseline:<name>", the challenge id, "passport" or "rules"
    key: str         # the metric's own key, e.g. "net_return"
    path: str        # how an explicit [ref] names it, e.g. "holdout.net_return"
    ref: str         # where it is stored, e.g. "PP-x r1 metrics.holdout.net_return"
    value: Decimal
    unit: str


# ---- words ------------------------------------------------------------------------------------------------------
_ALIASES = {"max_drawdown": ("drawdown", "maximum drawdown"), "sharpe": ("sharpe ratio",),
            "sortino": ("sortino ratio",), "excess_mean_daily_ci95_low": ("interval low",),
            "excess_mean_daily_ci95_high": ("interval high",), "trials_attempted": ("trials",),
            "ci_level": ("interval", "confidence interval", "ci"), "spread_bps": ("spread",),
            "slippage_bps": ("slippage",)}
_LOSS = frozenset(("down", "lost", "lose", "loses", "losing", "loss", "losses", "fell", "fall", "falls", "fallen",
                   "falling", "dropped", "drop", "drops", "declined", "decline", "declines", "negative", "minus"))
_GAIN = frozenset(["up", "gained", "gain", "gains", "rose", "rise", "rises", "risen", "positive", "plus"])
_FILLER = frozenset(("is", "was", "were", "are", "be", "been", "of", "at", "by", "a", "an", "the", "in", "on", "for",
                     "its", "it", "their", "after", "costs", "stood", "stands", "reads", "equals", "equalled",
                     "equaled", "has", "had", "have", "with", "strategy", "strategy's", "window", "window's",
                     "overall", "measured", "both", "baseline", "baseline's", "baselines", "untouched", "together",
                     "and", "sample"))
_QUALIFIERS = frozenset(("annual", "annually", "annualized", "annualised", "yearly", "year", "years", "annum",
                         "monthly", "month", "months", "weekly", "week", "weeks", "quarterly", "quarter", "daily",
                         "day", "per", "average", "avg", "mean", "median", "cumulative", "total", "compound",
                         "compounded", "gross", "excess", "relative", "real", "nominal", "pretax"))
_NUMBER_WORDS = re.compile(
    r"(?<![\w\-])(?:(?:minus|negative|plus)\s+)?(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundred|thousand|million|billion|half|a\s+quarter)(?:[\s\-]+(?:and\s+)?(?:one|two|three|four|five|"
    r"six|seven|eight|nine|hundred|thousand|million|billion))*\s+(?:percent|per\s+cent|percentage\s+points?|"
    r"basis\s+points?|bps|times|dollars?|trades?|sessions?|bars?|trials?|runs?|days?)(?![\w])", re.I)
_NEGATION = frozenset(["not", "no", "never", "neither", "nor", "none", "without", "cannot"])
_CONDITION = frozenset(("if", "unless", "would", "could", "might", "will", "should", "expect", "expected", "expects",
                        "forecast", "forecasts", "predict", "predicted", "projected", "assume", "assuming", "assumed",
                        "suppose", "hypothetical", "hypothetically", "before", "estimate", "estimated"))
_SIGNS = {"+": 1, "\uff0b": 1, "-": -1, "\u2212": -1, "\u2012": -1, "\u2013": -1, "\u2014": -1, "\ufe63": -1,
          "\uff0d": -1}
_SUFFIX = {"x": ("x", 1), "bps": ("bps", 1), "bp": ("bps", 1), "k": ("", 10 ** 3), "m": ("", 10 ** 6),
           "mm": ("", 10 ** 6), "b": ("", 10 ** 9), "bn": ("", 10 ** 9)}
_WORD_UNIT = re.compile(r"[ \t]+(percent|per[ \t]+cent|basis[ \t]+points?|bps|bp|usd|dollars?|times)(?![\w])", re.I)
_COUNT_WORD = re.compile(r"[ \t]+(trades?|sessions?|bars?|trials?|runs?|days?)(?![\w])", re.I)
_PERCENT = re.compile(r"[ ]?[%\uff05]")
_REF = re.compile(r"[ \t]*(\[[A-Za-z_][A-Za-z0-9_.\-]*\])")
_REF_TOKEN = re.compile(r"\[[A-Za-z_][A-Za-z0-9_.\-]*\]")
_SCALE = {"percent": Decimal("0.01"), "bps": Decimal("0.0001")}
_ALLOWED = {"fraction": {"percent", "bps"}, "ratio": {"plain", "x"}, "USD": {"usd"}}
_TOKEN = re.compile(
    r"(?P<ref>\[[A-Za-z_][A-Za-z0-9_.\-]*\])"
    r"|(?P<date>\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\w))"
    r"|(?P<hash>(?<!\w)(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}(?!\w))"
    r"|(?P<ident>(?>[A-Za-z_]\w*\(\d+(?:\.\d+)?\)|[A-Za-z_]\w*(?:/\w+(?:\.\d+)*)+|[A-Z][A-Z0-9_]*(?:-[A-Za-z0-9_]+)+"
    r"|[A-Za-z_]+\d\w*(?:\.\d+)*)(?![ ]?%|\.\d))"
    r"|(?P<num>(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?|\.\d+)"
    r"|(?P<word>[A-Za-z]+(?:['\u2019][A-Za-z]+)*)"
    r"|(?P<punct>\S)")


@dataclass(frozen=True)
class _Tok:
    kind: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class _Num:
    start: int                   # the written number, from its sign or "(" to its unit word
    end: int
    tok: int                     # the token index of its digits
    before: int                  # the index of the last token before it (-1 at the start)
    after: int                   # where a metric named after it may start
    magnitude: Decimal           # unsigned, in the unit written, scaled by k/m/b
    step: Decimal                # one unit of the last digit written, in the same unit
    sign: int | None             # from a written sign or accounting parentheses; None when unsigned
    unit: str                    # plain, percent, bps, x, usd, count:<unit> or unknown
    problem: str = ""
    ref: str = ""


def _quotable(sentence: str) -> bool:
    """A stored sentence a text may quote: it holds a number and at least four words, so a bare "30 sessions" or
    "100000 USD" cannot vouch for a number taken out of its context."""
    return bool(re.search(r"\d", sentence)) and len(re.findall(r"[A-Za-z]{2,}", sentence)) >= 4


def _sub(text: str) -> list[str]:
    return [w for w in re.split(r"[_\-]+", text.lower().replace("\u2019", "'")) if w]


def _name(key: str) -> str:
    return key.replace("_", " ")


def _names(key: str) -> list[tuple[str, ...]]:
    return [tuple(_sub(n.replace(" ", "_"))) for n in (_name(key), *_ALIASES.get(key, ()))]


def _phrase(words: str) -> str:
    return r"[\s_\-]+".join(re.escape(w) for w in words.split())


def _variants(name: str) -> list[str]:
    return sorted({name, name.replace("_", " "), name.replace("_", "-")}, key=len, reverse=True)


class Evidence:
    """Every number a Passport and its challenge results hold, with the words that name each, and the stored sentences
    a text may quote. Built once per check; pure, so a test can build one from any values."""

    def __init__(self, fields: list[Field], quotes: list[tuple[str, str]] = (), label: str = "the Passport",
                 baseline_window: str = BASELINE_WINDOW):
        self.fields, self.label, self.baseline_window = list(fields), label, baseline_window
        self.quotes = [(ref, s) for ref, s in quotes if _quotable(s)]
        self.by_path: dict[str, Field] = {}
        self.names: dict[str, dict[tuple[str, ...], Field]] = {}
        for f in self.fields:
            prefix = {"window": "metrics", "baseline": "baselines", "challenge": "challenges"}.get(f.kind, f.kind)
            for p in (f.path, f"{prefix}.{f.path}"):
                self.by_path.setdefault(p.lower(), f)
            if f.kind != "rules":
                for n in _names(f.key):
                    self.names.setdefault(f.subject, {}).setdefault(n, f)
        windows = sorted({f.subject for f in self.fields if f.kind == "window"})
        alias = {v: w for w in windows for v in _variants(w)}
        alias |= {"holdout": "holdout", "hold out": "holdout", "hold-out": "holdout"} if "holdout" in windows else {}
        if "in_sample" in windows:
            alias["development and validation together"] = "in_sample"
        self._windows, self.window_names = alias, set(windows)
        self._window_rx = self._rx(alias)
        self._challenges = {f.subject: f.subject for f in self.fields if f.kind == "challenge"}
        self._challenge_rx = self._rx(self._challenges, case=True)
        names = sorted({f.subject.split(":", 1)[1] for f in self.fields if f.kind == "baseline"})
        self._baselines = {v.replace("-", " ").replace("_", " "): n for n in names for v in _variants(n)}
        self._baseline_rx = self._rx(self._baselines)
        self.subject_words = {w for a in (*alias, *self._baselines) for w in _sub(a.replace(" ", "_"))}
        self._quote_rx = [(ref, re.compile(r"(?<!\w)" + r"\s+".join(re.escape(t) for t in s.split()) + r"(?!\w)"))
                          for ref, s in self.quotes]

    @staticmethod
    def _rx(words: dict[str, str], case: bool = False) -> re.Pattern | None:
        if not words:
            return None
        alts = "|".join(_phrase(w.replace("_", " ").replace("-", " ")) for w in sorted(words, key=len, reverse=True))
        return re.compile(rf"(?<![\w\-])(?:{alts})(?![\w\-])", 0 if case else re.I)

    def _found(self, rx, table, text: str, start: int, end: int) -> set[str]:
        if rx is None:
            return set()
        return {table[re.sub(r"[\s_\-]+", " ", m.group(0) if table is self._challenges else m.group(0).lower())]
                for m in rx.finditer(text, start, end)}

    def windows_in(self, text: str, *spans: tuple[int, int]) -> set[str]:
        return {w for start, end in spans for w in self._found(self._window_rx, self._windows, text, start, end)}

    def subjects(self, text: str, clause: tuple[int, int], sentence: tuple[int, int]) -> tuple[set[str], str]:
        """The window, baseline or challenge a number's clause names (else its sentence), and why none, if none."""
        if re.search(r"(?<![\w\-])baselines?(?:['\u2019]s)?(?![\w\-])", text[clause[0]:clause[1]], re.I):
            named = self._found(self._baseline_rx, self._baselines, text, *clause) or \
                self._found(self._baseline_rx, self._baselines, text, *sentence)
            if len(named) != 1:                          # a clause about a baseline is never about the strategy
                return {"passport"}, "it names a baseline but not which one"
            return {f"baseline:{named.pop()}", "passport"}, ""
        found = self._found(self._window_rx, self._windows, text, *clause) | \
            self._found(self._challenge_rx, self._challenges, text, *clause)
        if not found:
            found = self._found(self._window_rx, self._windows, text, *sentence) | \
                self._found(self._challenge_rx, self._challenges, text, *sentence)
        return found | {"passport"}, ""

    def quoted(self, text: str) -> list[tuple[int, int, str]]:
        return [(m.start(), m.end(), ref) for ref, rx in self._quote_rx for m in rx.finditer(text)]


# ---- reading a text ---------------------------------------------------------------------------------------------
def _lex(text: str) -> list[_Tok]:
    return [_Tok(m.lastgroup, m.start(), m.end(), m.group(0)) for m in _TOKEN.finditer(text)]


def _sign_here(text: str, k: int) -> int | None:
    """The sign text[k] writes, when it is one: not a hyphen joining a word or a range (T+1, 5-10)."""
    if k < 0 or text[k] not in _SIGNS or (k > 0 and (text[k - 1].isalnum() or text[k - 1] in "._%)")):
        return None
    return _SIGNS[text[k]]


def _numbers(text: str, toks: list[_Tok] | None = None) -> list[_Num]:
    """Every number written in the text, with its sign, unit and precision; dates and identifiers are not numbers."""
    toks = _lex(text) if toks is None else toks
    out = []
    for i, t in enumerate(toks):
        if t.kind != "num":
            continue
        a, b, sign, cur = t.start, t.end, None, False
        if (s := _sign_here(text, a - 1)) is not None:
            sign, a = s, a - 1
        if a > 0 and text[a - 1] == "$":
            cur, a = True, a - 1
            if sign is None and (s := _sign_here(text, a - 1)) is not None:
                sign, a = s, a - 1
        core = t.text.replace(",", "")
        written = Decimal("0" + core if core.startswith(".") else core)
        step = Decimal(1).scaleb(written.as_tuple().exponent)
        problem, suffix_unit, scale, j = "", "", 1, i + 1
        if j < len(toks) and toks[j].start == b and (toks[j].kind in ("word", "ident") or toks[j].text == "\u00d7"):
            suffix = "x" if toks[j].text == "\u00d7" else toks[j].text
            suffix_unit, scale = _SUFFIX.get(suffix.lower(), ("?", 1))
            if suffix_unit == "?":
                problem = f"its unit {suffix!r} is not one the check reads"
            b = toks[j].end
        pct = _PERCENT.match(text, b)
        if pct:
            b = pct.end()
        opened = sign is None and a > 0 and text[a - 1] == "("
        if opened and b < len(text) and text[b] == ")":
            sign, a, b, opened = -1, a - 1, b + 1, False               # accounting: (21.98%) is a loss
        after = b
        word = _WORD_UNIT.match(text, b)
        unit_word = ""
        if word:
            w = re.sub(r"\s+", " ", word.group(1).lower())
            unit_word = {"percent": "percent", "per cent": "percent", "usd": "usd", "dollar": "usd", "dollars": "usd",
                         "times": "x"}.get(w, "bps")
            b = word.end()
            if opened and b < len(text) and text[b] == ")":
                sign, a, b = -1, a - 1, b + 1                          # and so is (50 bps) or (382.05 USD)
        units = {u for u in ("usd" if cur else "", suffix_unit if suffix_unit not in ("", "?") else "",
                             "percent" if pct else "", unit_word) if u}
        count = _COUNT_WORD.match(text, b)
        if problem:
            unit = "unknown"
        elif len(units) > 1:
            unit, problem = "unknown", "it is written with two units"
        elif units:
            unit = units.pop()
        elif count:
            unit = "count:" + {"trade": "trades", "session": "sessions", "bar": "bars", "trial": "trials",
                               "run": "runs", "day": "days"}.get(count.group(1).lower().rstrip("s"),
                                                                 count.group(1).lower())
            b = count.end()
        else:
            unit = "plain"
        if scale != 1 and unit not in ("plain", "usd"):
            unit, problem = "unknown", "a k, m or b scale is read only on a plain number or money"
        ref = _REF.match(text, b)
        before = i - 1
        while before >= 0 and toks[before].start >= a:
            before -= 1
        out.append(_Num(start=a, end=b, tok=i, before=before, after=after, magnitude=written * scale,
                        step=step * scale, sign=sign, unit=unit, problem=problem,
                        ref=ref.group(1)[1:-1] if ref else ""))
    return out


class _Text:
    """A text cut into tokens, clauses (at , ;) and sentences (at . ! ? and line breaks)."""

    def __init__(self, text: str):
        self.text, self.toks = text, _lex(text)
        self.starts = [t.start for t in self.toks]
        self.clause, self.sentence = [], []
        c = s = 0
        prev = None
        for t in self.toks:
            if prev is not None and "\n" in text[prev.end:t.start]:
                c, s = c + 1, s + 1
            self.clause.append(c)
            self.sentence.append(s)
            if t.kind == "punct" and t.text in ".!?" and (t.end == len(text) or text[t.end].isspace()):
                c, s = c + 1, s + 1
            elif t.kind == "punct" and t.text in ",;":
                c += 1
            prev = t
        self._cspan = self._spans(self.clause)
        self._sspan = self._spans(self.sentence)

    def _spans(self, ids: list[int]) -> dict[int, tuple[int, int]]:
        spans: dict[int, tuple[int, int]] = {}
        for t, k in zip(self.toks, ids, strict=True):
            lo, _ = spans.get(k, (t.start, t.end))
            spans[k] = (lo, t.end)
        return spans

    def spans(self, tok: int) -> tuple[tuple[int, int], tuple[int, int]]:
        return self._cspan[self.clause[tok]], self._sspan[self.sentence[tok]]

    def words_back(self, j: int, clause: int) -> list[tuple[str, int]]:
        """The words that end at token j, read backwards to the start of the clause (at most eight), each with its
        token."""
        out: list[tuple[str, int]] = []
        while j >= 0 and self.clause[j] == clause and self.toks[j].kind in ("word", "ident") and len(out) < 8:
            out[:0] = [(w, j) for w in _sub(self.toks[j].text)]
            k = j - 1
            if k >= 1 and self.toks[k].text in "_-" and self.toks[k].end == self.toks[j].start \
                    and self.toks[k - 1].end == self.toks[k].start:
                j = k - 1
            elif k >= 0 and self.toks[k].end < self.toks[j].start and \
                    not self.text[self.toks[k].end:self.toks[j].start].strip():
                j = k
            else:
                break
        return out

    def words_on(self, pos: int, clause: int) -> list[tuple[str, int]]:
        """The words that start at pos (after spaces only), read forwards to the end of the clause (at most eight),
        each with its token."""
        m = re.compile(r"[ \t]*").match(self.text, pos)
        j = bisect.bisect_left(self.starts, m.end())
        if j == len(self.toks) or self.toks[j].start != m.end():
            return []
        out: list[tuple[str, int]] = []
        while j < len(self.toks) and self.clause[j] == clause and self.toks[j].kind in ("word", "ident") \
                and len(out) < 8:
            out += [(w, j) for w in _sub(self.toks[j].text)]
            k = j + 1
            if k + 1 < len(self.toks) and self.toks[k].text in "_-" and self.toks[k].start == self.toks[j].end \
                    and self.toks[k + 1].start == self.toks[k].end:
                j = k + 1
            elif k < len(self.toks) and self.toks[k].start > self.toks[j].end and \
                    not self.text[self.toks[j].end:self.toks[k].start].strip():
                j = k
            else:
                break
        return out

    def gap(self, n: _Num, plain: frozenset[str] | set[str]) -> list[int]:
        """Token indexes of the plain words (and : or =) between a number and the name before it, nearest first."""
        out, words, j = [], 0, n.before
        while j >= 0 and self.clause[j] == self.clause[n.tok]:
            t = self.toks[j]
            if t.kind == "word" and t.text.lower().replace("\u2019", "'") in plain:
                words += 1
            elif not (t.kind == "punct" and t.text in ":="):
                break
            if words > 6:
                break
            out.append(j)
            j -= 1
        return out

    def around(self, first: int, last: int) -> list[str]:
        """The two words just before tokens first..last and the three just after, in their clause, up to a number."""
        out, clause = [], self.clause[first]
        for step, j, limit in ((-1, first - 1, 2), (1, last + 1, 3)):
            seen = 0
            while 0 <= j < len(self.toks) and self.clause[j] == clause and seen < limit and self.toks[j].kind != "num":
                if self.toks[j].kind in ("word", "ident"):
                    out += _sub(self.toks[j].text)
                    seen += 1
                j += step
        return out

    def last(self, end: int) -> int:
        return bisect.bisect_left(self.starts, end) - 1

    def hedges(self, quotes: list[tuple[int, int, str]]) -> dict[int, str]:
        """Each sentence's first negation or condition, outside a verbatim quote."""
        out: dict[int, str] = {}
        for i, t in enumerate(self.toks):
            if t.kind != "word" or self.sentence[i] in out or any(a <= t.start and t.end <= b for a, b, _ in quotes):
                continue
            w = t.text.lower().replace("\u2019", "'")
            if w in _NEGATION or w in _CONDITION or w.endswith("n't"):
                out[self.sentence[i]] = w
        return out


_Hit = tuple[tuple[str, ...], Field, int, int]          # the name, its field, its first and last token


def _match(names: dict[tuple[str, ...], Field], words: list[tuple[str, int]], back: bool) -> _Hit | None:
    for n in range(min(len(words), 8), 0, -1):
        part = words[-n:] if back else words[:n]
        key = tuple(w for w, _ in part)
        if key in names:
            return key, names[key], part[0][1], part[-1][1]
    return None


def _pick(hits: list[_Hit]) -> tuple[set[Field], int, int]:
    """The field(s) with the longest name, and where that name sits."""
    if not hits:
        return set(), -1, -1
    longest = max(len(h[0]) for h in hits)
    best = [h for h in hits if len(h[0]) == longest]
    return {h[1] for h in best}, best[0][2], best[0][3]


def _may_say(f: Field) -> set[str]:
    """The words that may stand beside a field without changing what it measures: its own name's, and its period."""
    words = {w for n in _names(f.key) for w in n}
    if f.key == "cagr" or "per_year" in f.key:
        words |= {"annual", "annually", "annualized", "annualised", "yearly", "year", "per"}
    if f.key in ("sharpe", "sortino"):
        words |= {"annualized", "annualised"}
    if words & {"avg", "mean", "expectancy"}:
        words |= {"average", "avg", "mean"}
    if "daily" in words:
        words |= {"day", "per"}
    if f.unit in ("trades", "sessions", "bars", "trials", "runs"):
        words |= {"total"}
    return words


def _depth(f: Field) -> bool:
    """A maximum drawdown is a depth: written without a sign, it is read as the loss it is."""
    return f.key.endswith("max_drawdown") and f.unit == "fraction"


def _stated(f: Field) -> Decimal:
    """The precision the Passport states a field at: 0.01% for a fraction, a cent for money, as stored otherwise."""
    if f.unit == "fraction":
        return Decimal("0.0001")
    if f.unit == "USD":
        return Decimal("0.01")
    exponent = f.value.as_tuple().exponent
    return Decimal(1).scaleb(min(exponent, 0) if isinstance(exponent, int) else 0)


def _fmt(value: Decimal, unit: str) -> str:
    if unit == "fraction":
        pct = value.scaleb(2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{pct.copy_abs() if pct == 0 else pct}%"                 # never "-0.00%"
    if unit == "USD":
        cents = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{cents.copy_abs() if cents == 0 else cents} USD"
    if unit in ("number", "ratio", "trades", "sessions", "bars", "trials"):
        return decimal_str(value)
    return f"{decimal_str(value)} {unit}"


def _allowed(field_unit: str, written: str) -> bool:
    if field_unit in _ALLOWED:
        return written in _ALLOWED[field_unit]
    return written in ("plain", f"count:{field_unit}")


def _judge(n: _Num, f: Field, sign: int | None) -> tuple[str, str]:
    """PASS, or FAIL and why: the written number against the one field it is tied to."""
    shown = _fmt(f.value, f.unit)
    if not _allowed(f.unit, n.unit):
        return FAIL, f"its unit ({n.unit}) is not the unit of {f.path} ({f.unit}), which holds {shown}"
    k = _SCALE.get(n.unit, Decimal(1))
    step = n.step * k
    value = n.magnitude * k * (sign if sign is not None else (-1 if _depth(f) else 1))
    if value == f.value:
        return PASS, ""
    if abs(value - f.value) * 2 > step:
        if value != 0 and abs(-value - f.value) * 2 <= step:
            return FAIL, f"its sign is wrong: {f.path} holds {shown}"
        return FAIL, f"{f.path} holds {shown}"
    if step > _stated(f):
        return FAIL, f"it is written less precisely than the Passport states {f.path}: {shown}"
    return PASS, ""


def judge(ev: Evidence, text: str) -> list[dict]:
    """Every number in the text, tied to its field and judged: PASS, FAIL or UNVERIFIED, with why (HOW_CHECKED)."""
    tx = _Text(text)
    quotes = ev.quoted(text)
    hedges = tx.hedges(quotes)
    plain = _FILLER | _LOSS | _GAIN | ev.subject_words
    subjects: dict[int, tuple[set[str], str]] = {}
    cited_at: set[int] = set()
    bare = _REF_TOKEN.sub(lambda m: " " * len(m.group(0)), text)     # a [ref] names no window, baseline or challenge
    out = []
    for n in _numbers(text, tx.toks):
        row = {"number": text[n.start:n.end].strip(), "status": UNVERIFIED, "ref": None, "holds": None,
               "reason": "", "context": re.sub(r"\s+", " ", text[max(0, n.start - 40): n.end + 40]).strip()}
        out.append(row)
        quote = next((ref for a, b, ref in quotes if a <= n.start and n.end <= b), None)
        if quote is not None:
            row.update(status=PASS, ref=quote, reason="quoted verbatim")
            continue
        if n.ref:
            cited_at.add(_REF.match(text, n.end).end())
        if tx.clause[n.tok] not in subjects:
            subjects[tx.clause[n.tok]] = ev.subjects(bare, *tx.spans(n.tok))
        cands, problem = subjects[tx.clause[n.tok]]
        gap = tx.gap(n, plain)
        before: set[Field] = set()
        signs = [tx.toks[j].text.lower() for j in gap]
        first, last = n.before + 1, tx.last(_REF.match(text, n.end).end() if n.ref else n.end)
        for g in range(len(gap) + 1):
            end = gap[g - 1] - 1 if g else n.before
            words = tx.words_back(end, tx.clause[n.tok]) if end >= 0 else []
            before, at, _ = _pick([h for s in cands if (h := _match(ev.names.get(s, {}), words, True))])
            if before:
                signs, first = [tx.toks[j].text.lower() for j in gap[:g]], at
                break
        words = tx.words_on(n.after, tx.clause[n.tok])
        after, _, to = _pick([h for s in cands if (h := _match(ev.names.get(s, {}), words, False))])
        if after and not before:
            last = max(last, to)
        tied = before or after
        if before and after and before != after:
            tied = before | after
        if n.ref:
            cited = ev.by_path.get(n.ref.lower())
            if cited is None:
                row["reason"] = f"it cites [{n.ref}], which {ev.label} does not hold"
                continue
            if tied and tied != {cited}:
                row["reason"] = f"it cites [{n.ref}] but names {', '.join(sorted(f.path for f in tied))}"
                continue
            if cited.kind == "window" and (named := cands & ev.window_names) and cited.subject not in named:
                row["reason"] = f"it cites [{n.ref}] but its sentence is about {_and(sorted(map(_label, named)))}"
                continue
            tied = {cited}
        if not tied:
            near = tx.words_back(n.before, tx.clause[n.tok]) if n.before >= 0 else []
            named = sorted({_name(h[1].key) for s in ev.names if (h := _match(ev.names[s], near, True))})
            hedge = hedges.get(tx.sentence[n.tok])
            row["reason"] = n.problem or problem or (f"its sentence says '{hedge}'" if hedge else "") or (
                f"it names {' or '.join(named)} but not the window, baseline or challenge it is for" if named else
                "no metric is named next to it, and it is not a quote of a stored finding")
            continue
        if len(tied) > 1:
            row["reason"] = "it could be " + " or ".join(sorted(f.path for f in tied))
            continue
        f = tied.pop()
        row.update(ref=f.ref, holds=_fmt(f.value, f.unit))
        if hedge := hedges.get(tx.sentence[n.tok]):
            row["reason"] = f"its sentence says '{hedge}', so it does not state the stored value"
            continue
        if n.problem:
            row["reason"] = n.problem
            continue
        if word := next((w for w in tx.around(first, last) if w in _QUALIFIERS and w not in _may_say(f)), None):
            row["reason"] = f"'{word}' beside it changes what it measures, so it does not state {f.path}"
            continue
        if f.kind == "baseline" and (elsewhere := ev.windows_in(bare, *tx.spans(n.tok)) - {ev.baseline_window}):
            row["status"] = FAIL
            row["reason"] = (f"the baselines are measured on {_label(ev.baseline_window)}, not on "
                             f"{_and(sorted(_label(w) for w in elsewhere))}")
            continue
        said = {+1 if w in _GAIN else -1 for w in signs if w in _GAIN or w in _LOSS}
        if len(said) > 1 or (said and n.sign is not None and said != {n.sign}):
            row["reason"] = "its sign and the word before it disagree"
            continue
        row["status"], row["reason"] = _judge(n, f, n.sign if n.sign is not None else next(iter(said), None))
    for m in _NUMBER_WORDS.finditer(text):           # a number in words is a number: it cannot be checked
        if not any(a <= m.start() and m.end() <= b for a, b, _ in quotes):
            out.append({"number": m.group(0), "status": UNVERIFIED, "ref": None, "holds": None,
                        "reason": "a number written in words: write it in digits next to its metric",
                        "context": m.group(0)})
    for t in tx.toks:                     # a bracketed name with digits in it that cites no number hides them
        if t.kind == "ref" and re.search(r"\d", t.text) and t.end not in cited_at:
            out.append({"number": t.text, "status": UNVERIFIED, "ref": None, "holds": None,
                        "reason": "a bracketed reference that follows no number", "context": t.text})
    return out


# ---- the Passport -----------------------------------------------------------------------------------------------
def _load(store, passport_id: str, revision: int | None) -> tuple[EvidencePassport, list[ChallengeResult]]:
    from traderos import passports
    p = passports.reopen(store, passport_id, revision)          # every reference is proved before it is read
    return p, [store.get(ChallengeResult, cid) for cid in p.challenge_result_ids]


def _show(m: MetricValue) -> str:
    if m.value is None:
        return f"unavailable ({m.unavailable_reason})"
    return _fmt(m.value, m.unit)


def _sentences(text: str) -> list[str]:
    return [s.strip().rstrip(".").strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def _leaves(value: Any, path: str):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            yield from _leaves(v, f"{path}.{i}")
    elif isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        yield path, Decimal(str(value))
    elif isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        yield path, Decimal(value)


def evidence(p: EvidencePassport, challenges: list[ChallengeResult]) -> Evidence:
    """Every number the Passport and its challenge results hold, where it lives, and the sentences they store."""
    tag = f"{p.passport_id} r{p.revision}"

    def own(key: str, value: Decimal, unit: str, path: str | None = None) -> Field:
        return Field("passport", "passport", key, path or key, f"{tag} {path or key}", value, unit)

    fields = [own("trials_attempted", Decimal(p.trials_attempted), "trials"), own("revision", Decimal(p.revision),
                                                                                  "number")]
    for key, m in sorted(p.metrics.items()):
        if m.value is not None and "." in key:
            w, k = key.split(".", 1)
            fields.append(Field("window", w, k, key, f"{tag} metrics.{key}", m.value, m.unit))
    if any("ci95" in k for k in p.metrics):
        fields.append(Field("passport", "passport", "ci_level", "ci_level",
                            f"{tag} metrics.*_ci95_* (the level each interval names)", Decimal("0.95"), "fraction"))
    for name, block in sorted(p.baselines.items()):
        for k, m in sorted(block.items()):
            if m.value is not None:
                fields.append(Field("baseline", f"baseline:{name}", k, f"{name}.{k}", f"{tag} baselines.{name}.{k}",
                                    m.value, m.unit))
    for c in challenges:
        for k, m in sorted(c.metrics.items()):
            if m.value is not None:
                fields.append(Field("challenge", c.challenge_id, k, f"{c.challenge_id}.{k}",
                                    f"{c.result_id} ({c.challenge_id}) metrics.{k}", m.value, m.unit))
    for key, raw in sorted(p.assumptions.items()):
        cash = re.fullmatch(r"(\d+(?:\.\d+)?) USD", raw)
        if key.startswith("cost.") and key.endswith("_bps") and re.fullmatch(r"\d+(?:\.\d+)?", raw):
            fields.append(own(key[5:], Decimal(raw).scaleb(-4), "fraction", f"assumptions.{key}"))
        elif key.startswith("cost.") and re.fullmatch(r"\d+(?:\.\d+)?", raw):
            fields.append(own(key[5:], Decimal(raw), "USD", f"assumptions.{key}"))
        elif key == "starting_cash" and cash:
            fields.append(own(key, Decimal(cash.group(1)), "USD", f"assumptions.{key}"))
    for path, value in _leaves(p.rules, "rules"):
        fields.append(Field("rules", "rules", path.rsplit(".", 1)[-1], path, f"{tag} {path}", value, "number"))
    quotes = [(f"{c.result_id} ({c.challenge_id}) finding", s) for c in challenges for s in _sentences(c.finding)]
    u = p.uncertainty
    quotes += [(f"{tag} uncertainty.{a}", s) for a in ("test_execution", "data_quality", "operational_freshness")
               for s in _sentences(getattr(u, a))]
    quotes += [(f"{tag} limitations[{i}] {lim.code}", s) for i, lim in enumerate(p.limitations)
               for s in _sentences(lim.detail)]
    quotes += [(f"{tag} assumptions.{k}", s) for k, raw in sorted(p.assumptions.items()) for s in _sentences(raw)]
    quotes += [(f"{tag} metrics.{k} unavailable", f"{_name(k.split('.', 1)[-1])} ({m.unavailable_reason})")
               for k, m in sorted(p.metrics.items()) if m.value is None and m.unavailable_reason]
    return Evidence(fields, quotes, label=f"{p.passport_id} revision {p.revision}", baseline_window=BASELINE_WINDOW)


def check_claims(store, passport_id: str, text: str, *, revision: int | None = None) -> dict:
    if len(text.encode("utf-8")) > MAX_TEXT:
        raise ValidationError(f"the text is over {MAX_TEXT // 1024} KB")
    p, challenges = _load(store, passport_id, revision)
    claims = judge(evidence(p, challenges), text)
    bad = [c for c in claims if c["status"] != PASS]
    result = {"passport_id": p.passport_id, "revision": p.revision, "passport_hash": p.canonical_hash,
              "claims": claims, "supported": [{"number": c["number"], "ref": c["ref"]} for c in claims
                                              if c["status"] == PASS],
              "unsupported": bad, "how_checked": list(HOW_CHECKED), "not_checked": list(NOT_CHECKED)}
    if bad:
        listed = "; ".join(f"{c['number']} {c['status']}: {c['reason']} (in \"{c['context']}\")" for c in bad[:10])
        raise ResearchError(
            f"{len(bad)} number(s) are not borne out by {p.passport_id} revision {p.revision}: {listed}. Say only "
            "what the Passport holds, each number next to its metric and its window, baseline or challenge, or quote "
            "a stored finding verbatim.", code=ErrorCode.INSUFFICIENT_EVIDENCE,
            details={"failed": str(sum(1 for c in bad if c["status"] == FAIL)),
                     "unverified": str(sum(1 for c in bad if c["status"] == UNVERIFIED)),
                     "claims": json.dumps(bad, ensure_ascii=False)})
    return result


def _label(w: str) -> str:
    return LABEL.get(w, w.replace("_", " "))


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def explain(store, passport_id: str, *, revision: int | None = None) -> dict:
    p, challenges = _load(store, passport_id, revision)
    tag = f"{p.passport_id} r{p.revision}"
    findings: list[Finding] = []
    why = [lim.detail for lim in p.limitations if lim.code == "NOT_A_CANDIDATE_BECAUSE"]
    findings.append(Finding("verdict", f"The research verdict is {p.state.value.replace('_', ' ')}"
                            + (": " + "; ".join(w.rstrip(".") for w in why) if why else "")
                            + ". This is research, not advice."))
    windows = sorted({k.split(".", 1)[0] for k in p.metrics})
    order = [w for w in ORDER if w in windows] + [w for w in windows if w not in ORDER]
    for w in order:
        block = {k.split(".", 1)[1]: m for k, m in p.metrics.items() if k.startswith(w + ".")}
        shown = [(k, block[k]) for k in HEADLINE if k in block and block[k].value is not None]
        missing = [(k, m) for k, m in sorted(block.items()) if m.value is None]
        text = (f"In {_label(w)}, after costs: " + ", ".join(f"{_name(k)} {_show(m)}" for k, m in shown) + "."
                if shown else f"In {_label(w)}, no headline result is available.")
        lo, hi = block.get("excess_mean_daily_ci95_low"), block.get("excess_mean_daily_ci95_high")
        cites = [Cite(f"{tag} metrics.{w}.{k}", m.value, m.unit) for k, m in shown]
        if lo is not None and hi is not None and lo.value is not None and hi.value is not None:
            where = ("above zero: this window shows a daily edge over cash" if lo.value > 0 else
                     "below zero: this window lost to cash" if hi.value < 0 else
                     "across zero: this window does not show an edge over cash")
            text += (f" In {_label(w)}, the daily return over cash has a 95% interval: interval low {_show(lo)}, "
                     f"interval high {_show(hi)}. It runs {where}.")
            cites += [Cite(f"{tag} metrics.*_ci95_* (the level each interval names)", Decimal("0.95"), "fraction"),
                      Cite(f"{tag} metrics.{w}.excess_mean_daily_ci95_low", lo.value, lo.unit),
                      Cite(f"{tag} metrics.{w}.excess_mean_daily_ci95_high", hi.value, hi.unit)]
        if missing:
            text += " Unavailable, never zero: " + "; ".join(f"{_name(k)} ({m.unavailable_reason})"
                                                           for k, m in missing) + "."
        findings.append(Finding(f"results.{w}", text, cites))
    if p.baselines:
        if BASELINE_WINDOW not in windows:
            findings.append(Finding("baseline.window", f"The baselines are measured on {_label(BASELINE_WINDOW)}, "
                                    "which this Passport holds no results for, so nothing is set beside them."))
        for name, block in sorted(p.baselines.items()) if BASELINE_WINDOW in windows else ():
            pairs, cites = [], []
            for key, m in sorted(block.items()):
                mine = p.metrics.get(f"{BASELINE_WINDOW}.{key}")
                if mine is None or mine.value is None or m.value is None:
                    continue
                if key == "max_drawdown":                         # drawdowns are negative: higher is shallower
                    side = "shallower than" if mine.value > m.value else "deeper than" if mine.value < m.value else \
                        "the same as"
                else:
                    side = "above" if mine.value > m.value else "below" if mine.value < m.value else "equal to"
                pairs.append(f"{_name(key)} {_show(mine)}, {side} the baseline's {_name(key)} {_show(m)}")
                cites += [Cite(f"{tag} metrics.{BASELINE_WINDOW}.{key}", mine.value, mine.unit),
                          Cite(f"{tag} baselines.{name}.{key}", m.value, m.unit)]
            if pairs:
                findings.append(Finding(f"baseline.{name}", f"Against the {_name(name)} baseline, both measured on "
                                        f"{_label(BASELINE_WINDOW)}: " + "; ".join(pairs) + ".", cites))
        others = [_label(w) for w in order if w != BASELINE_WINDOW]
        if others:
            findings.append(Finding("baseline.none", f"No baseline is measured on {_and(others)}, so none is set "
                                    f"beside {'it' if len(others) == 1 else 'them'}: every baseline is measured on "
                                    f"{_label(BASELINE_WINDOW)}."))
    counts = {o: [c.challenge_id for c in challenges if c.outcome is o] for o in ChallengeOutcome}
    findings.append(Finding("challenges", p.uncertainty.test_execution + " "
                            + " ".join(f"{o.value.replace('_', ' ')}: {', '.join(sorted(ids))}."
                                       for o, ids in counts.items() if ids)))
    for c in sorted(challenges, key=lambda c: c.challenge_id):
        if c.outcome in (ChallengeOutcome.FAIL, ChallengeOutcome.ERROR, ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA):
            findings.append(Finding(f"challenge.{c.challenge_id}", f"{c.challenge_id} {c.outcome.value}: {c.finding}",
                                    [Cite(f"{c.result_id} ({c.challenge_id}) metrics.{k}", m.value, m.unit)
                                     for k, m in sorted(c.metrics.items()) if m.value is not None]))
    u = p.uncertainty
    findings.append(Finding("uncertainty", f"Four separate answers, never one badge. Data: {u.data_quality}. Tests: "
                            f"{u.test_execution} Merit: {u.strategy_merit.value}. Freshness: "
                            f"{u.operational_freshness}."))
    return {"passport_id": p.passport_id, "revision": p.revision, "passport_hash": p.canonical_hash,
            "findings": [f.as_dict() for f in findings],
            "not_claimed": ["no forecast", "no recommendation to trade", "no number the Passport does not hold",
                            "no comparison across windows: a baseline is set only beside the window it was measured "
                            "on"]}


def describe(result: dict) -> str:
    lines = [f"Quant Analyst on {result['passport_id']} revision {result['revision']}"]
    lines += [f"- {f['text']}" for f in result["findings"]]
    lines.append(f"Every number above is stored in {result['passport_id']} revision {result['revision']} or its "
                 "challenge results (`--json` lists each source). Not claimed: a forecast, or a reason to trade.")
    return "\n".join(lines)


__all__ = ["BASELINE_WINDOW", "FAIL", "HOW_CHECKED", "NOT_CHECKED", "PASS", "UNVERIFIED", "Cite", "Evidence", "Field",
           "Finding", "check_claims", "describe", "evidence", "explain", "judge"]
