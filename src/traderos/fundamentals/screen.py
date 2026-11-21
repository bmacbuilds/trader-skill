"""Stock screener (S07 Fundamental Screener), ported from the upstream trader-screener.

Provenance: tank/trader-skill/trader-screener/screen.py (upstream trader-skill at 5189e79; tank commit
92d084f), MIT (notice: the NOTICE file). It exists only in v1;
none of the ten reviewed upstream PRs touches it.

The upstream text, kept: checks every company against your rules, says exactly why each one passes or fails, and ranks
the ones that pass.

Metrics: the companies' own SEC filings (traderos fundamentals screen --tickers, through the ported facts reader),
or a file: JSON ({"companies": [...]}, or a plain list of objects) or a CSV with a header row. Each company needs a
"ticker"; every other column is a metric. Numbers in a CSV can carry $, % and thousands commas.

Prices (optional, your own file): {"as_of": "YYYY-MM-DD", "prices": {"AAPL": "341.27"}}. With prices, the screener
adds:
    price          the price you gave it
    pe             price / eps_ttm (only when eps_ttm is above zero)
    earnings_yield 100 * eps_ttm / price, in %

Rules:
    {"name": "...",
     "rules": [{"metric": "net_margin", "op": ">=", "value": 15}, ...],
     "rank":  [{"metric": "revenue_growth_yoy", "order": "desc", "weight": 2}, ...],
     "missing": "fail",            # or "skip": where a rule on a missing metric is listed
     "top": 10}
    ops: > >= < <= == != between (value [low, high]) in / not in (a list)
    A rule may say its "unit" ("percent", "USD", ...); a value written "25%" is in percent.

Ranking: among the companies that pass, each rank metric gives a score from 0 (worst) to 1 (best); the final score
is their weighted average. A company missing a rank metric is scored on the others and the gap is shown.

A screen is a filter, not a recommendation.

What changed from the upstream file, each with its reason (ADR-011):
  1. to_number refuses NaN, infinity and true/false (04.2): "inf" passed `pe >= 10`, and true passed `> 0`.
  2. A rule on a missing metric never decides the row: the row is `passed: null`, flagged, unless another rule
     failed (09.4: unknown is never a verdict). With "missing": "skip" upstream let the row PASS on a rule never
     checked; with "fail" upstream failed it on a number nobody had. "missing" now only says where the rule is listed.
  3. Units per metric. Margins and growth are percent (the upstream convention: 16.4 means 16.4%), money is USD, EPS is
     USD/shares, pe is a ratio. A column that writes some values with % and some without, or a rule whose unit is
     not its metric's, is refused: upstream read "25%" as 25 and "0.25" as 0.25 in the same column.
  4. Point in time (QT01): the screen has an `as_of`, and nothing from after it is used. A file, a row or prices
     dated after as_of are refused (LOOKAHEAD_RISK): a row's `filed`, its own `as_of` and its `period_end` are
     each checked. Where the input cannot say when a row's figures were known (no `filed`, no `as_of` of its own
     or of its file), a screen dated before today refuses rather than guess; so do undated prices. A metrics
     file keeps the as_of of the data it was built from: a later `earnings --metrics` into it never lowers it.
     A row whose newest quarter is over two quarters old, or a quarter behind the others, is flagged
     STALE_PERIOD or PERIOD_MISMATCH.
  5. Decimal, not float (04.3).
  6. Input is the ported facts records or a size-capped file; the output is a fundamental-screen/1 record. Exit
     codes follow the TraderOS CLI contract (a refusal is 3), not the upstream 0/1/2.
  7. Text where a number belongs never decides a row: in a numeric metric ("#DIV/0!" in net_margin) the rule
     cannot be checked and counts as missing (change 2). Upstream compared the text as a string, so "#DIV/0!"
     passed `!= 0`. Text metrics (sector) keep its string comparisons.
  8. Untrusted files are refused where the upstream code crashed or misread them (04.2): a JSON key given twice, a list
     of companies that are not objects, a rule or rank entry that is not an object, a rank that is not a list,
     a rank weight that is not a number of zero or more, a "top" that is not a whole number of 1 or more, a
     "name" that is not text, a comparison value that is not a number.
  9. The closing line names `traderos earnings TICKER` instead of its /trader-analyst and /trader-budget skills,
     which do not exist here (F1 has no budget or order path).
"""
from __future__ import annotations

import csv
import io
import json
import os
import pathlib
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from traderos.contracts import (
    Completeness,
    ErrorCode,
    Limitation,
    MetricValue,
    SafeFor,
    ValidationError,
    canonical_bytes,
    sha256_bytes,
    with_hash,
)
from traderos.contracts.canonical import to_jsonable
from traderos.contracts.fundamentals import EarningsReport, FundamentalScreen, ScreenRow

OPS = (">", ">=", "<", "<=", "==", "!=", "between", "in", "not in")
UNITS = {"revenue_ttm": "USD", "net_income_ttm": "USD", "eps_ttm": "USD/shares", "price": "USD", "pe": "ratio",
         "revenue_growth_yoy": "percent", "eps_growth_yoy": "percent", "net_income_growth_yoy": "percent",
         "gross_margin": "percent", "operating_margin": "percent", "net_margin": "percent",
         "earnings_yield": "percent"}
NOT_METRICS = ("ticker", "symbol", "name", "cik", "period_end", "filed", "as_of", "derived", "_why")
MAX_FILE = 1024 * 1024
MAX_COMPANIES = 500
MAX_TICKERS = 50
STALE_DAYS = 182                 # two quarters: the next 10-Q or 10-K is due well within that
MISMATCH_DAYS = 45               # half a quarter behind the newest company in the screen


class ScreenError(ValidationError):
    """The upstream error type, now a typed TraderOS refusal (INVALID_INPUT)."""


# ---------------------------------------------------------------- inputs

def to_number(v):
    if v is None:
        return None
    if isinstance(v, bool):
        raise ScreenError("%r is not a number" % v)                # change 1
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(v) if isinstance(v, int) else Decimal(str(v))
        if not d.is_finite():
            raise ScreenError("%s is not a number" % v)
        return d
    s = str(v).strip()
    if s == "" or s.lower() in ("na", "n/a", "none", "null", "-"):
        return None
    t = s.replace(",", "").replace("$", "").replace("%", "")
    try:
        d = Decimal(t)
    except InvalidOperation:
        return s
    if not d.is_finite():
        raise ScreenError("%r is not a number" % s)                # change 1
    return d


def _hint(v):
    """The unit a written value states: "25%" is percent, "$8.71" is USD."""
    if isinstance(v, str):
        if "%" in v:
            return "percent"
        if "$" in v:
            return "USD"
    return None


class Companies(list):
    """The companies to screen, with the unit of each metric column (change 3)."""

    def __init__(self, rows=(), units=None, as_of=None):
        super().__init__(rows)
        self.units = dict(units or {})
        self.as_of = as_of


def _units(raw_rows):
    """Each column's unit; a column mixing % values with bare numbers (or a unit its metric cannot have) is refused."""
    seen = {}
    for r in raw_rows:
        for k, v in r.items():
            if k in NOT_METRICS:
                continue
            n = to_number(v)
            if not isinstance(n, Decimal):
                continue
            seen.setdefault(k, set()).add(_hint(v))
    units = {}
    for k, hints in seen.items():
        stated = hints - {None}
        if len(stated) > 1 or (stated and None in hints):
            raise ScreenError("column %s mixes values written with %s and without: one unit per column (is "
                              "0.25 a quarter of a percent or 25%%?)" % (k, " and ".join(sorted(stated))))
        unit = next(iter(stated)) if stated else UNITS.get(k, "number")
        known = UNITS.get(k)
        if known and stated and unit != known and not (known == "USD/shares" and unit == "USD"):
            raise ScreenError("column %s is %s, but %s is measured in %s" % (k, unit, k, known))
        units[k] = known or unit
    return units


def _read_capped(path):
    p = pathlib.Path(path)
    if not p.is_file():
        raise ScreenError("no such file: %s" % path, code=ErrorCode.MISSING_DATA)
    if p.stat().st_size > MAX_FILE:
        raise ScreenError("%s is larger than %d bytes" % (path, MAX_FILE), code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
    try:
        return p.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ScreenError("%s is not UTF-8 text" % path) from None


def _json(text, path):
    def no_constant(s):
        raise ScreenError("%s: %s is not a number" % (path, s))

    def no_dupes(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ScreenError("%s has a duplicate key" % path)
        return dict(pairs)
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=no_constant, object_pairs_hook=no_dupes)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ScreenError):
            raise
        raise ScreenError("%s is not valid JSON: %s" % (path, type(exc).__name__)) from None


def load_metrics(path):
    text = _read_capped(path)
    as_of = None
    if str(path).lower().endswith(".csv"):
        raw = [{(k or "").strip(): v for k, v in r.items()} for r in csv.DictReader(io.StringIO(text))]
    else:
        data = _json(text, path)
        rows = data.get("companies", data.get("rows")) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ScreenError("%s: expected a list of companies or {\"companies\": [...]}" % path)
        raw = [dict(r) for r in rows]
        if isinstance(data, dict) and data.get("as_of"):
            as_of = _day(data["as_of"], "%s as_of" % path)
    if len(raw) > MAX_COMPANIES:
        raise ScreenError("at most %d companies in one screen" % MAX_COMPANIES,
                          code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
    units = _units(raw)
    out = []
    for i, r in enumerate(raw):
        t = str(r.get("ticker") or r.get("symbol") or "").strip().upper()
        if not t:
            raise ScreenError("company #%d has no ticker" % (i + 1))
        row = {k: (to_number(v) if k not in NOT_METRICS else v) for k, v in r.items()}
        row["ticker"] = t
        out.append(row)
    return Companies(out, units, as_of)


def _day(v, what):
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ScreenError("%s %r is not YYYY-MM-DD" % (what, v)) from None


def add_prices(rows, prices_path, as_of=None, today=None):
    data = _json(_read_capped(prices_path), prices_path)
    if not isinstance(data, dict):
        raise ScreenError("%s: expected {\"prices\": {...}}" % prices_path)
    stated = _day(data["as_of"], "prices as_of") if data.get("as_of") else None
    if as_of is not None and stated is not None and stated > as_of:
        raise ScreenError("prices as of %s are later than the screen's as_of %s" % (stated, as_of),
                          code=ErrorCode.LOOKAHEAD_RISK)                  # change 4
    if as_of is not None and stated is None and as_of < (today or datetime.now(UTC).date()):
        raise ScreenError("%s states no as_of day, so TraderOS cannot tell whether its prices are from after the "
                          "screen's as_of %s: add \"as_of\" to the file" % (prices_path, as_of),
                          code=ErrorCode.LOOKAHEAD_RISK)                  # change 4: never guessed
    table = data.get("prices", data) or {}
    if not isinstance(table, dict):
        raise ScreenError("%s: prices are {\"TICKER\": price}" % prices_path)
    prices = {k.upper(): v for k, v in table.items()}
    for r in rows:
        p = to_number(prices.get(r["ticker"]))
        if not isinstance(p, Decimal) or p <= 0:
            continue
        r["price"] = p
        eps = to_number(r.get("eps_ttm"))
        if isinstance(eps, Decimal):
            r["earnings_yield"] = _round(100 * eps / p, Decimal("0.01"))
            if eps > 0:
                r["pe"] = _round(p / eps, Decimal("0.1"))
    if isinstance(rows, Companies):
        rows.units.update({"price": "USD", "pe": "ratio", "earnings_yield": "percent"})
    return stated


def load_rules(path):
    spec = _json(_read_capped(path), path)
    check_rules(spec)
    return spec


def check_rules(spec):
    if not isinstance(spec, dict) or not isinstance(spec.get("rules", []), list):
        raise ScreenError("rules must be {\"rules\": [...]}")
    if not isinstance(spec.get("rank", []), list):
        raise ScreenError("rank must be a list")
    for i, r in enumerate(spec.get("rules", [])):
        if not isinstance(r, dict) or "metric" not in r or "op" not in r or "value" not in r:
            raise ScreenError("rule #%d needs metric, op and value: %r" % (i + 1, r))
        if r["op"] not in OPS:
            raise ScreenError("rule #%d: unknown op %r (use one of %s)" % (i + 1, r["op"],
                                                                          ", ".join(OPS)))
        if r["op"] == "between" and not (isinstance(r["value"], list) and len(r["value"]) == 2):
            raise ScreenError("rule #%d: between needs [low, high]" % (i + 1))
        if r["op"] in ("in", "not in") and not isinstance(r["value"], list):
            raise ScreenError("rule #%d: %s needs a list" % (i + 1, r["op"]))
        rule_unit(r)
    for i, k in enumerate(spec.get("rank", [])):
        if not isinstance(k, dict) or "metric" not in k or k.get("order", "desc") not in ("asc", "desc"):
            raise ScreenError("rank #%d needs a metric and order asc or desc" % (i + 1))
        if not isinstance(to_number(k.get("weight", 1)), Decimal) or to_number(k.get("weight", 1)) < 0:
            raise ScreenError("rank #%d: weight is a number, zero or more" % (i + 1))
    if spec.get("missing", "fail") not in ("fail", "skip"):
        raise ScreenError("missing must be \"fail\" or \"skip\"")
    top = spec.get("top")
    if top is not None and (isinstance(top, bool) or not isinstance(top, int) or top < 1):
        raise ScreenError("top is a whole number, 1 or more")
    if spec.get("name") is not None and not isinstance(spec["name"], str):
        raise ScreenError("name is text")


def rule_unit(rule):
    """The unit a rule states ("unit", or a value written "25%"), or None when it states none."""
    stated = rule.get("unit")
    values = rule["value"] if isinstance(rule["value"], list) else [rule["value"]]
    hints = {_hint(v) for v in values} - {None}
    if len(hints) > 1 or (stated and hints and stated not in hints):
        raise ScreenError("rule on %s states two units" % rule.get("metric"))
    if rule["op"] not in ("in", "not in", "==", "!="):
        for v in values:
            if not isinstance(to_number(v), Decimal):                  # NaN and true refused here too
                raise ScreenError("rule on %s: %r is not a number" % (rule.get("metric"), v))
    return stated or (next(iter(hints)) if hints else None)


def check_units(spec, units):
    """Change 3: a rule's unit must be its metric's."""
    for r in spec.get("rules", []):
        want, have = rule_unit(r), units.get(r["metric"], UNITS.get(r["metric"]))
        if want and have and want != have:
            raise ScreenError("rule on %s is in %s, but %s is measured in %s" % (r["metric"], want, r["metric"],
                                                                                 have))


# ---------------------------------------------------------------- screening

def _round(x, step):
    return x.quantize(step, rounding=ROUND_HALF_EVEN)


def _fmt(v):
    if isinstance(v, Decimal):
        return format(_round(v, Decimal("0.01")), "f").rstrip("0").rstrip(".")
    if isinstance(v, list):
        return "[%s]" % ", ".join(_fmt(to_number(x)) for x in v)
    return str(v)


def check(company, rule, numeric=()):
    """(passed, reason). passed is None when the metric is missing, or is text in a `numeric` metric (change 7)."""
    m, op, want = rule["metric"], rule["op"], rule["value"]
    have = company.get(m)
    if isinstance(have, (bool, int, float)):
        have = to_number(have)                                         # changes 1 and 5
    if have is None or have == "":
        return None, "%s is missing" % m
    if m in numeric and not isinstance(have, Decimal):
        return None, "%s is %r, not a number" % (m, have)              # change 7
    if op in ("in", "not in"):
        bag = [str(x).upper() for x in want]
        inside = str(have).upper() in bag
        ok = inside if op == "in" else not inside
        return ok, "%s %s %s %s" % (m, _fmt(have), "is in" if inside else "is not in", _fmt(want))
    if op in ("==", "!=") and not isinstance(have, Decimal):
        ok = (str(have).upper() == str(want).upper()) == (op == "==")
        return ok, "%s %s %s %s" % (m, _fmt(have), op, _fmt(want))
    if not isinstance(have, Decimal):
        return False, "%s is %r, not a number" % (m, have)
    if op == "between":
        lo, hi = (to_number(x) for x in want)
        ok = lo <= have <= hi
        return ok, "%s %s %s %s to %s" % (m, _fmt(have), "is within" if ok else "is outside",
                                          _fmt(lo), _fmt(hi))
    want = to_number(want)
    if not isinstance(want, Decimal):
        return False, "%s: %r is not a number to compare with" % (m, rule["value"])
    ok = {">": have > want, ">=": have >= want, "<": have < want, "<=": have <= want,
          "==": have == want, "!=": have != want}[op]
    if ok:
        return True, "%s %s %s %s" % (m, _fmt(have), op, _fmt(want))
    return False, "%s %s, needs %s %s" % (m, _fmt(have), op, _fmt(want))


ORDER = {True: 0, None: 1, False: 2}


def screen(companies, spec):
    """Every company with pass/fail/unknown and reasons; passing ones ranked."""
    rules = spec.get("rules", [])
    missing = spec.get("missing", "fail")
    numeric = set(UNITS) | set(getattr(companies, "units", None) or ())
    results = []
    for c in companies:
        fails, passes, gaps, unknown, failed = [], [], [], False, False
        if c.get("_why"):
            fails.append("no data: %s" % c["_why"])
            unknown = True
        for r in rules:
            ok, why = check(c, r, numeric)
            if ok is None:
                (fails if missing == "fail" else gaps).append(why)
                unknown = True                                         # change 2
            elif ok:
                passes.append(why)
            else:
                fails.append(why)
                failed = True
        results.append({"ticker": c["ticker"], "name": c.get("name"),
                        "passed": False if failed else (None if unknown else True),
                        "fails": fails, "passes": passes, "skipped": gaps, "metrics": c})
    rank(results, spec.get("rank", []))
    results.sort(key=lambda r: (ORDER[r["passed"]], -(r.get("score") or 0), len(r["fails"]),
                                r["ticker"]))
    return results


def rank(results, keys):
    winners = [r for r in results if r["passed"] is True]
    for r in winners:
        r["score"], r["rank_parts"] = Decimal(0), {}
    if not keys or not winners:
        for r in winners:
            r["score"] = None
        return
    for k in keys:
        m, desc = k["metric"], k.get("order", "desc") == "desc"
        vals = [(r, r["metrics"].get(m)) for r in winners]
        vals = [(r, v) for r, v in vals if isinstance(v, Decimal)]
        if not vals:
            continue
        ordered = sorted(v for _, v in vals)
        n = len(ordered)
        for r, v in vals:
            below = sum(1 for x in ordered if x < v)
            equal = sum(1 for x in ordered if x == v)
            pos = (below + Decimal(equal - 1) / 2) / (n - 1) if n > 1 else Decimal(1)
            r["rank_parts"][m] = _round(pos if desc else 1 - pos, Decimal("0.001"))
    weights = {k["metric"]: to_number(k.get("weight", 1)) for k in keys}
    for r in winners:
        parts = r["rank_parts"]
        total = sum((weights[m] for m in parts), Decimal(0))
        r["score"] = _round(sum((parts[m] * weights[m] for m in parts), Decimal(0)) / total,
                            Decimal("0.001")) if total else None
        r["rank_gaps"] = [k["metric"] for k in keys if k["metric"] not in parts]


# ---------------------------------------------------------------- output

def render(results, spec, top=None, prices_as_of=None):
    name = spec.get("name") or "screen"
    passed = [r for r in results if r["passed"] is True]
    out = ["%s: %d of %d companies pass." % (name, len(passed), len(results))]
    if prices_as_of:
        out.append("Prices as of %s." % prices_as_of)
    out.append("")
    shown = passed[:top] if top else passed
    for i, r in enumerate(shown, 1):
        score = "" if r.get("score") is None else "  score %.2f" % r["score"]
        out.append("%2d. PASS %-6s %s%s" % (i, r["ticker"], (r.get("name") or "")[:32], score))
        for why in r["passes"]:
            out.append("        ok    %s" % why)
        for why in r["skipped"]:
            out.append("        skip  %s" % why)
        if r.get("rank_gaps"):
            out.append("        not ranked on: %s" % ", ".join(r["rank_gaps"]))
        for flag in r.get("flags", ()):
            out.append("        flag  %s" % flag)
    if top and len(passed) > top:
        out.append("    ... %d more pass" % (len(passed) - top))
    unknown = [r for r in results if r["passed"] is None]
    if unknown:
        out.append("")
        for r in unknown:
            out.append("    N/A  %-6s %s (cannot tell: a rule's metric is missing)" % (
                r["ticker"], "; ".join(r["fails"] + r["skipped"])))
    failed = [r for r in results if r["passed"] is False]
    if failed:
        out.append("")
        for r in failed:
            out.append("    FAIL %-6s %s" % (r["ticker"], "; ".join(r["fails"])))
    out.append("")
    out.append("A screen is a filter on reported numbers, not a recommendation. Read the filings "
               "(traderos earnings TICKER) before anything else.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- facts records in, a record out (change 6)

def metrics_from_report(report: EarningsReport):
    """The upstream metrics row (facts.metrics_row), from an earnings/1 record, with each missing value's reason."""
    latest = report.quarters[-1] if report.quarters else None
    ttm = report.ttm or {}
    pick = {"revenue_ttm": ttm.get("revenue"), "net_income_ttm": ttm.get("net_income"),
            "eps_ttm": ttm.get("eps_diluted")}
    if latest is not None:
        pick.update({"revenue_growth_yoy": latest.revenue_yoy, "eps_growth_yoy": latest.eps_yoy,
                     "net_income_growth_yoy": latest.net_income_yoy, "gross_margin": latest.gross_margin,
                     "operating_margin": latest.operating_margin, "net_margin": latest.net_margin})
    row = {"ticker": report.ticker or report.cik, "name": report.entity, "cik": report.cik,
           "period_end": latest.period_end if latest else None, "filed": latest.filed if latest else None,
           "as_of": report.as_of, "derived": [], "_reasons": {}}
    names = {"revenue": "revenue_ttm", "net_income": "net_income_ttm", "eps_diluted": "eps_ttm"}
    row["derived"] = [names[k] for k in report.ttm_derived if k in names]
    if latest is not None and latest.derived:
        row["derived"].append("latest quarter: " + ", ".join(latest.derived_series))
    for k, mv in pick.items():
        row[k] = mv.value if mv is not None else None
        if row[k] is None:
            row["_reasons"][k] = mv.unavailable_reason if mv is not None else "not in the record"
    return row


def write_metrics(path, reports):
    """facts.py --metrics FILE: merge one row per ticker into a JSON file the screener reads (--metrics FILE).

    Numbers are written as decimal strings (exact). Change 4: every row keeps the as_of of the data it was built
    from (a row already in the file without one takes the file's), and the file's as_of is the latest of them and
    of the file's own, so a later command never lowers it and a screen dated earlier refuses the file. When a row
    has no date at all (the file had none to give it), the file states no as_of, rather than vouch for that row."""
    p = pathlib.Path(path)
    existing, file_as_of = {}, None
    if p.is_file():
        data = _json(_read_capped(p), p)
        rows = data.get("companies", []) if isinstance(data, dict) else []
        if isinstance(data, dict) and data.get("as_of"):
            file_as_of = _day(data["as_of"], "%s as_of" % p).isoformat()
        for r in rows:
            if isinstance(r, dict) and r.get("ticker"):
                r = to_jsonable(r)
                if not r.get("as_of") and file_as_of:
                    r["as_of"] = file_as_of                            # the day its file said it was built
                existing[str(r["ticker"]).upper()] = r
    for report in reports:
        row = metrics_from_report(report)
        row.pop("_reasons")
        row["source"] = "SEC XBRL company facts"
        existing[str(row["ticker"]).upper()] = to_jsonable(row)
    days = [str(r["as_of"]) for r in existing.values() if r.get("as_of")]
    undated = len(days) < len(existing)
    body = {"as_of": None if undated else max([*days, *([file_as_of] if file_as_of else [])], default=None),
            "companies": sorted(existing.values(), key=lambda r: r["ticker"])}
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return len(existing)


def from_reports(reports, unavailable=()):
    """Companies to screen from earnings/1 records, and (ticker, cik, entity, why) for companies with no data."""
    rows = [metrics_from_report(r) for r in reports]
    for ticker, cik, entity, why in unavailable:
        rows.append({"ticker": ticker, "name": entity, "cik": cik, "_why": why[:300], "derived": [],
                     "_reasons": {}})
    return Companies(rows, dict(UNITS))


def _flags(row, newest, as_of):
    out = []
    end = row.get("period_end")
    end = _day(end, "period_end") if end is not None and not isinstance(end, date) else end
    if row.get("_why"):
        out.append("NO_DATA")
    elif end is None:
        out.append("PERIOD_UNKNOWN: the input gives no period_end")
    else:
        if as_of and (as_of - end).days > STALE_DAYS:
            out.append("STALE_PERIOD: newest quarter ended %s, over two quarters before %s" % (end, as_of))
        if newest and (newest - end).days > MISMATCH_DAYS:
            out.append("PERIOD_MISMATCH: newest quarter ended %s; others end as late as %s" % (end, newest))
    derived = row.get("derived")
    out += ["DERIVED: %s" % d for d in (derived if isinstance(derived, (list, tuple)) else ())]
    return out


def run(companies, spec, *, as_of=None, top=None, prices_as_of=None, from_filings=True, now=None):
    """(fundamental-screen/1 record, the upstream text report)."""
    check_rules(spec)
    units = getattr(companies, "units", None) or dict(UNITS)
    check_units(spec, units)
    today = (now or datetime.now(UTC)).date()
    as_of = as_of or getattr(companies, "as_of", None) or today
    file_as_of = getattr(companies, "as_of", None)
    if file_as_of and file_as_of > as_of:
        raise ScreenError("metrics as of %s are later than the screen's as_of %s" % (file_as_of, as_of),
                          code=ErrorCode.LOOKAHEAD_RISK)
    ends = []
    for c in companies:
        for key in ("period_end", "filed", "as_of"):
            v = c.get(key)
            if v is not None and not isinstance(v, date):
                c[key] = _day(v, "%s %s" % (c["ticker"], key))
        if c.get("_why"):
            continue                                                   # no figures: nothing to date
        for key, what in (("filed", "were filed"), ("as_of", "are as of"), ("period_end", "cover a quarter ending")):
            if c.get(key) and c[key] > as_of:                          # change 4: nothing from after as_of
                raise ScreenError("%s's figures %s %s, after the screen's as_of %s" % (
                    c["ticker"], what, c[key], as_of), code=ErrorCode.LOOKAHEAD_RISK)
        if not (c.get("filed") or c.get("as_of") or file_as_of) and as_of < today:
            raise ScreenError("cannot tell when %s's figures were filed: the input gives no filed or as_of day, "
                              "so they may be from after the screen's as_of %s. Date the rows (filed, as_of) or "
                              "the file (as_of), or screen as of today" % (c["ticker"], as_of),
                              code=ErrorCode.LOOKAHEAD_RISK)           # change 4: never guessed
        if c.get("period_end"):
            ends.append(c["period_end"])
    newest = max(ends) if ends else None
    results = screen(companies, spec)
    for r in results:
        r["flags"] = _flags(r["metrics"], newest, as_of)
    top = top or spec.get("top")
    rows = tuple(_row(r, units) for r in results)
    limits = [Limitation(code="AS_FILED" if from_filings else "YOUR_FILE",
                         detail="Figures from the companies' own XBRL filings, as of their filing dates."
                         if from_filings else "Figures from the file you gave; TraderOS did not check them."),
              Limitation(code="SELECTION_BIAS", detail="The universe is the companies you listed today; it is not a "
                         "point-in-time universe."),
              Limitation(code="NOT_A_RECOMMENDATION", detail="A filter on reported numbers, not a recommendation.")]
    if any(c.get("price") is not None for c in companies) and prices_as_of is None:
        limits.append(Limitation(code="PRICES_UNDATED", detail="The prices file states no as_of day."))
    if from_filings and as_of < today:
        limits.append(Limitation(code="TICKERS_TODAY", detail="Tickers were matched to companies with the SEC's "
                                 "current ticker list, not the list on %s." % as_of))
    created = now or datetime.now(UTC)
    key = [[r.ticker for r in rows], to_jsonable(spec), as_of, created]
    record = with_hash(FundamentalScreen(
        screen_id="FS-" + sha256_bytes(canonical_bytes(key))[:20], as_of=as_of, criteria=to_jsonable(spec),
        rows=rows, completeness=Completeness.PARTIAL if any(r.passed is None for r in rows) else
        Completeness.COMPLETE, limitations=tuple(limits), safe_for=(SafeFor.RESEARCH,), created_at=created,
        name=spec.get("name"), prices_as_of=prices_as_of, top=top))
    return record, render(results, spec, top, prices_as_of)


def _row(r, units):
    c = r["metrics"]
    reasons = c.get("_reasons", {})
    metrics, labels = {}, {}
    for k, v in c.items():
        if k in NOT_METRICS or k.startswith("_"):
            continue
        if isinstance(v, Decimal):
            metrics[k] = MetricValue(value=v, unit=units.get(k, UNITS.get(k, "number")))
        elif v is None:
            why = c["_why"] if c.get("_why") else reasons.get(k, "missing in the input")
            metrics[k] = MetricValue(value=None, unit=units.get(k, UNITS.get(k, "number")),
                                     unavailable_reason=why[:300])
        else:
            labels[k] = str(v)[:200]
    return ScreenRow(cik=str(c.get("cik") or ""), ticker=r["ticker"], entity=str(r.get("name") or ""),
                     metrics=metrics, passed=r["passed"], reasons=tuple(r["fails"]), labels=labels or None,
                     passes=tuple(r["passes"]), skipped=tuple(r["skipped"]), score=r.get("score"),
                     rank_parts=r.get("rank_parts") or None, rank_gaps=tuple(r.get("rank_gaps", ())),
                     flags=tuple(r["flags"]), period_end=c.get("period_end"), filed=c.get("filed"))


__all__ = ["MAX_TICKERS", "OPS", "UNITS", "Companies", "ScreenError", "add_prices", "check", "check_rules",
           "check_units", "from_reports", "load_metrics", "load_rules", "metrics_from_report", "rank", "render", "run",
           "screen", "to_number", "write_metrics"]
