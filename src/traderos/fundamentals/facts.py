"""Earnings reader (S04 Earnings Analyst), ported from the upstream trader-analyst.

Provenance: tank/day-trader-skill/trader-analyst/facts.py (upstream day-trader-skill at 3d0f4ef; tank commit
92d084f), MIT (notice: the NOTICE file). v1's copy
(tank/trader-skill, 5189e79) differs only in one docstring line. Upstream PR #24 (ef390ed) is applied with the
review's change (1), and PR #49 (77c720b) as is: its eight lines sit at the top of traderos.cli.main, where this
module's output is printed (ADR-011).

The upstream text, kept: pulls a company's reported numbers from SEC's XBRL "company facts" API and lays out what
changed, quarter by quarter: revenue, gross, operating and net margins, net income, diluted EPS, and growth against the
same quarter a year earlier. It also finds the latest earnings press release (the EX-99 exhibit of the newest 8-K with
item 2.02) and the latest 10-Q or 10-K.

How the quarters are built (the upstream rules, kept):
  - A quarter is a reported value covering about three months (80 to 100 days). When a period was reported more
    than once (a later filing repeats it as a comparison), the latest filing wins, so restatements are used.
  - Companies do not file a fourth-quarter 10-Q. The fourth quarter is the full year (10-K) minus the three quarters
    inside it, and it is marked "derived". Derived EPS is approximate: share counts change during a year.
  - Revenue is read from the first concept a company uses of: RevenueFromContractWithCustomerExcludingAssessedTax,
    Revenues, RevenuesNetOfInterestExpense, SalesRevenueNet and a few more. Banks and insurers report revenue
    differently; margins for them can be blank.

Numbers only: this tool never says whether a stock is a buy.

What changed from the upstream file, each with its reason (spec v2.0 sections; ADR-011):
  1. POINT IN TIME: only facts filed on or before `as_of` are read (QT01). Upstream read every fact, so a restatement
     filed in September changed the answer for a day in May.
  2. Decimal, not float (04.3): 4.50 - (1.00 + 1.10 + 1.20) was 1.2000000000000002.
  3. USD (and USD/shares for EPS) only. Upstream took a concept's first unit when USD was missing, so a company that
     reports in CNY was printed as "US$ millions". Another currency is refused, by name.
  4. Each value carries `first_filed`, the day it was first public (FIN-001, "original availability"); upstream computed
     it and dropped it.
  5. Each value carries `amendment` when its filing is a 10-K/A or 10-Q/A.
  6. PR #24 with the review's change (1): a row is `derived` when ANY series shown in it was derived (revenue, cost
     of revenue, gross profit, operating income, net income, diluted EPS), and the row names them. Trailing totals
     name a derived quarter they include (`ttm_derived`), and so does the screener's metrics row (FIN-001).
  7. margin() is None when the denominator is not positive: margin(-50, -100) was +50%.
  8. Trailing twelve months use the SAME four quarter ends for every series, the four ending at the newest quarter
     (the metrics row's period_end). Upstream summed each series' own last four, so revenue and EPS could cover
     different quarters.
  9. Labels: a three-month value first filed in a 10-K is Q4 (upstream labelled it "FY"), and a value first seen as a
     comparative in a later filing is placed in its own fiscal period, never the later filing's (upstream labelled last
     year's quarter with this year's fy). The upstream rule, labels come from the first filing, is otherwise kept.
 10. A row's `filed` is the latest filing among the numbers shown in it, the day the row was complete (QT01); upstream
     used the revenue fact's filing date.
 11. Fetching goes through traderos.adapters.sec (04.3, 06.1(d)); the result is an `earnings/1` record.
 12. A fact whose value is not a finite number, or whose period dates do not read, is skipped: that period then
     shows as not filed. Upstream added a NaN into every sum it touched, and one unreadable date stopped the whole
     report with a traceback.
 13. A malformed row of the filings list is skipped (its filing date cannot be read, so it is never known to be
     public: change 1), and an exhibit lookup that fails in any way falls back to the filing index, as the upstream one
     did when its fetch failed (it caught only that error).
 14. The text names the day it answers for (", filed by 2026-11-01" in its first line): a past day's report must
     not read as today's (change 1).
 15. metrics_row() also carries `filed` and `as_of`, which the screener needs to refuse figures from after a
     screen's day (screen.py change 4).
 16. The record's fiscal_year is a whole number (the earnings/1 contract): a fact with no fy takes its period end's
     calendar year. Upstream printed it blank.
 17. Numbers are printed with the upstream project's own format strings; only the divisors are integers (10**6, 10**9),
     since a Decimal cannot be divided by a float (change 2).
 18. The record says when a ticker was matched with the SEC's current ticker list for a past day (TICKERS_TODAY).
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from traderos.contracts import (
    Completeness,
    DataError,
    ErrorCode,
    Limitation,
    MetricValue,
    SafeFor,
    canonical_bytes,
    sha256_bytes,
    with_hash,
)
from traderos.contracts.fundamentals import EarningsReport, FilingFact, FilingLink, QuarterLine

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/%d/%s/"

CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenuesNetOfInterestExpense", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
}
FLOWS = ("revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income")
FORMS = ("10-Q", "10-K", "20-F", "40-F")
SHOWN = ("revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income", "eps_diluted")
MAX_QUARTERS = 40
TENTH, CENT, THOUSANDTH = Decimal("0.1"), Decimal("0.01"), Decimal("0.001")


def unit_of(key):
    return "USD/shares" if key == "eps_diluted" else "USD"


def _round(x, step):
    return x.quantize(step, rounding=ROUND_HALF_EVEN)


def _dec(v):
    """A fact's value as an exact Decimal, or None when it is not a finite number."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


# ---------------------------------------------------------------- facts

def _date(s):
    return date.fromisoformat(str(s))


def _filed_by(f, as_of):
    try:
        filed = _date(f.get("filed", ""))
    except ValueError:
        return False                                   # no readable filing date: never known to be public
    return as_of is None or filed <= as_of


def facts_for(companyfacts, concept, unit_hint=None):
    """Raw facts for one us-gaap concept, in USD (USD/shares for EPS) only (change 3)."""
    node = companyfacts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return []
    return node.get("units", {}).get(unit_hint, []) if unit_hint else []


def other_units(companyfacts, key):
    """Units a metric's concepts are reported in other than the one TraderOS reads (e.g. CNY), so they can be named."""
    want = unit_of(key)
    found = set()
    for concept in CONCEPTS[key]:
        node = companyfacts.get("facts", {}).get("us-gaap", {}).get(concept) or {}
        found |= {u for u in node.get("units", {}) if u != want}
    return sorted(found)


def _quarter_index(fp):
    if fp == "FY":
        return 4
    if isinstance(fp, str) and re.fullmatch(r"Q[1-4]", fp):
        return int(fp[1])
    return None


def _label(f, first, own_end):
    """(fy, fp) for a period, from the first filing that reported it, placed in the period's own fiscal time.

    The SEC's fy and fp describe the FILING. Kept (upstream): a later filing that repeats a period as a comparison does
    not relabel it. Change 9: a three-month value first filed in a 10-K is Q4; a period first seen as a comparative
    (it ends well before the filing's own period) is moved back by the quarters or years between the two."""
    fy, fp = first.get("fy"), first.get("fp")
    try:
        days = (_date(f["end"]) - _date(f["start"])).days
        gap = (own_end - _date(f["end"])).days if own_end else 0
    except (KeyError, ValueError):
        return fy, fp
    if not isinstance(fy, int):
        return fy, fp
    if 350 <= days <= 380 and fp == "FY":
        return fy - round(gap / 365.25), "FY"
    base = _quarter_index(fp)
    if 80 <= days <= 100 and base is not None:
        idx = base - round(gap / 91.31)
        return fy + (idx - 1) // 4, "Q%d" % ((idx - 1) % 4 + 1)
    return fy, fp


def periods(facts, as_of=None):
    """{(start, end): fact} for duration facts from 10-Q/10-K filed on or before as_of.

    The value comes from the latest filing (restatements win). The fiscal label (fy, fp) comes from the first filing
    that reported the period: a later 10-Q that repeats it as a comparison carries its own fy and fp."""
    latest, first, own_end = {}, {}, {}
    for f in facts:
        if "start" not in f or f.get("form", "").split("/")[0] not in FORMS or not _filed_by(f, as_of):
            continue
        if _dec(f.get("val")) is None:
            continue                                   # not a finite number: never read as one
        key = (f["start"], f["end"])
        stamp = (f.get("filed", ""), f.get("accn", ""))
        if key not in latest or stamp > (latest[key].get("filed", ""), latest[key].get("accn", "")):
            latest[key] = f
        if key not in first or stamp < (first[key].get("filed", ""), first[key].get("accn", "")):
            first[key] = f
        try:
            end = _date(f["end"])
        except ValueError:
            continue
        accn = f.get("accn", "")
        if accn not in own_end or end > own_end[accn]:
            own_end[accn] = end                        # a filing's own period: the newest period it reports
    out = {}
    for key, f in latest.items():
        g = dict(f)
        g["val"] = _dec(f["val"])
        g["fy"], g["fp"] = _label(f, first[key], own_end.get(first[key].get("accn", "")))
        g["first_filed"] = first[key].get("filed")
        out[key] = g
    return out


def quarterly(facts, derive_q4=True, as_of=None):
    """{end: {"value", "start", "end", "derived", "fy", "fp", "form", "filed", "first_filed", "amendment", "accn",
    "sources"}} three-month values."""
    per = periods(facts, as_of)
    q = {}
    years = []
    for (start, end), f in per.items():
        try:
            days = (_date(end) - _date(start)).days
        except ValueError:
            continue
        if 80 <= days <= 100:
            q[end] = {"value": f["val"], "start": start, "end": end, "derived": False,
                      "fy": f.get("fy"), "fp": f.get("fp"), "form": f.get("form"),
                      "filed": f.get("filed"), "first_filed": f.get("first_filed"),
                      "amendment": str(f.get("form", "")).endswith("/A"), "accn": f.get("accn"),
                      "sources": (f.get("accn"),)}
        elif 350 <= days <= 380:
            years.append((start, end, f))
    if derive_q4:
        for start, end, f in years:
            if end in q:
                continue
            inside = [v for v in q.values() if v["start"] >= start and v["end"] < end]
            inside.sort(key=lambda v: v["end"])
            if len(inside) != 3:
                continue
            last_q_start = _date(inside[-1]["end"]) + timedelta(days=1)
            parts = [f, *inside]
            q[end] = {"value": f["val"] - sum((v["value"] for v in inside), Decimal(0)),
                      "start": last_q_start.isoformat(), "end": end, "derived": True,
                      "fy": f.get("fy"), "fp": "Q4", "form": f.get("form"),
                      "filed": max(str(p.get("filed")) for p in parts),
                      "first_filed": max(str(p.get("first_filed")) for p in parts),
                      "amendment": str(f.get("form", "")).endswith("/A") or any(v["amendment"] for v in inside),
                      "accn": f.get("accn"),
                      "sources": tuple(sorted({str(p.get("accn")) for p in parts}))}
    return q


def series(companyfacts, key, derive_q4=True, as_of=None):
    """Quarterly series for a metric, trying its concepts in order and filling gaps."""
    unit = unit_of(key)
    merged, used = {}, []
    for concept in CONCEPTS[key]:
        q = quarterly(facts_for(companyfacts, concept, unit), derive_q4, as_of)
        if not q:
            continue
        added = False
        for end, v in q.items():
            if end not in merged:
                merged[end] = dict(v, concept=concept)
                added = True
        if added:
            used.append(concept)
    return merged, used


def _yoy_match(series_, end, days=365, slack=10):
    target = _date(end) - timedelta(days=days)
    best = None
    for e in series_:
        gap = abs((_date(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, e)
    return series_[best[1]] if best else None


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return _round(100 * (a - b) / abs(b), TENTH)


def margin(part, whole):
    if part is None or whole is None or whole <= 0:    # change 7: no margin over a zero or negative revenue
        return None
    return _round(100 * part / whole, TENTH)


def analyse(companyfacts, quarters=6, name=None, as_of=None):
    """Every number in the report, as a dict."""
    s = {}
    concepts = {}
    for key in CONCEPTS:
        s[key], concepts[key] = series(companyfacts, key, derive_q4=True, as_of=as_of)
    refused = {key: other_units(companyfacts, key) for key in CONCEPTS if not s[key]}
    refused = {k: v for k, v in refused.items() if v}
    rev = s["revenue"]
    if not rev and not s["net_income"]:
        units = sorted(set(refused.get("revenue", []) + refused.get("net_income", [])))
        if units:
            raise DataError(f"revenue and net income are reported in {', '.join(units)}, not USD; TraderOS reads "
                            "USD figures only", code=ErrorCode.OUTSIDE_CAPABILITY)
        when = f" filed by {as_of}" if as_of else ""
        raise DataError(f"no quarterly revenue or net income in the XBRL facts{when}", code=ErrorCode.MISSING_DATA)
    all_ends = sorted(set(rev) | set(s["net_income"]), reverse=True)
    ends = all_ends[:quarters]
    rows = []
    for end in ends:
        r = rev.get(end, {}).get("value")
        gp = s["gross_profit"].get(end, {}).get("value")
        shown = {k: s[k].get(end) for k in SHOWN if k != "cost_of_revenue"}
        if gp is None and r is not None and end in s["cost_of_revenue"]:
            gp = r - s["cost_of_revenue"][end]["value"]
            shown["cost_of_revenue"] = s["cost_of_revenue"][end]
        oi = s["operating_income"].get(end, {}).get("value")
        ni = s["net_income"].get(end, {}).get("value")
        eps = s["eps_diluted"].get(end, {}).get("value")
        prior = {k: _yoy_match(s[k], end) for k in ("revenue", "net_income", "eps_diluted")}
        base = rev.get(end) or s["net_income"].get(end) or {}
        values = {k: v for k, v in shown.items() if v}
        rows.append({
            "end": end, "start": base.get("start"),
            "fiscal": "%s %s" % (base.get("fp") or "", base.get("fy") or ""),
            "fy": base.get("fy"), "fp": base.get("fp"),
            "derived": any(v.get("derived") for v in values.values()),
            "derived_series": [k for k in SHOWN if values.get(k, {}).get("derived")],
            "amended_series": [k for k in SHOWN if values.get(k, {}).get("amendment")],
            "revenue": r, "gross_profit": gp, "operating_income": oi, "net_income": ni,
            "eps_diluted": _round(eps, CENT) if eps is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(oi, r),
            "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, (prior["revenue"] or {}).get("value")),
            "net_income_yoy": pct(ni, (prior["net_income"] or {}).get("value")),
            "eps_yoy": pct(eps, (prior["eps_diluted"] or {}).get("value")),
            "filed": max(str(v["filed"]) for v in values.values()),
            "first_filed": max(str(v["first_filed"]) for v in values.values()),
            "form": base.get("form"),
            "values": values,
        })
    ttm, ttm_ends, ttm_derived = {}, [], []
    last4 = all_ends[:4]                               # change 8: the same four quarter ends for every series
    if len(last4) == 4 and (_date(last4[0]) - _date(last4[-1])).days < 300:
        ttm_ends = sorted(last4)
        for key in ("revenue", "net_income", "eps_diluted"):
            if all(e in s[key] for e in ttm_ends):
                ttm[key] = sum((s[key][e]["value"] for e in ttm_ends), Decimal(0))
                if any(s[key][e]["derived"] for e in ttm_ends):
                    ttm_derived.append(key)
    if "eps_diluted" in ttm:
        ttm["eps_diluted"] = _round(ttm["eps_diluted"], CENT)
    return {"entity": name or companyfacts.get("entityName"),
            "cik": str(companyfacts.get("cik", "")).zfill(10),
            "rows": rows, "ttm": ttm, "ttm_ends": ttm_ends, "ttm_derived": ttm_derived,
            "concepts": concepts, "refused_units": refused, "as_of": as_of}


def stale_note(result, filings):
    """A note when SEC's facts lag a report that is already filed."""
    rep = (filings or {}).get("report")
    if not rep or not result["rows"] or not rep.get("period"):
        return None
    latest = result["rows"][0]["end"]
    if rep["period"] > latest and (_date(rep["period"]) - _date(latest)).days > 20:
        return ("SEC's XBRL facts stop at %s, but a %s for the period ending %s was filed %s. "
                "Read it directly: %s" % (latest, rep["form"], rep["period"], rep["filed"],
                                         rep["url"]))
    return None


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    out = []

    def money(v):
        return "n/a" if v is None else "$%.2fB" % (v / 10**9) if abs(v) >= 10**9 else "$%.1fM" % (v / 10**6)

    label = "Quarter ending %s%s" % (cur["end"], " (derived from the 10-K)" if cur["derived"] else "")
    line = "%s: revenue %s" % (label, money(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net income %s" % money(cur["net_income"])
    if cur["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_income_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS $%.2f" % cur["eps_diluted"]
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["net_income"] is not None and cur["operating_income"] is not None \
            and cur["net_income"] > cur["operating_income"]:
        out.append("Net income is above operating income: it includes gains outside the "
                   "business (investments, tax items). Check the 10-Q before reading it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: "
                   "compare with a year earlier first)." % pct(cur["revenue"], prev["revenue"]))
    return out


# ---------------------------------------------------------------- filings

EXHIBIT_RANK = [r"ex(?:hibit)?[-_]?99[-_.]?0?1(?!\d)", r"ex(?:hibit)?[-_]?99",
                r"earnings|release|press", r"ex(?:hibit)?[-_]?\d"]


def exhibit_name(names, primary=None):
    """The press release among a filing's files (EX-99.1 by its usual names)."""
    docs = [n for n in names if n.lower().endswith((".htm", ".html", ".txt", ".pdf"))
            and n != primary and not re.match(r"^(R\d+\.htm|\d{10}-\d{2}-\d{6})", n)]
    for rx in EXHIBIT_RANK:
        hits = sorted(n for n in docs if re.search(rx, n, re.I))
        if hits:
            return hits[0]
    return None


def latest_filings(submissions, cik, client=None, as_of=None):
    """Newest 10-Q/10-K and newest earnings 8-K (item 2.02) with its EX-99 exhibit, filed on or before as_of."""
    rec = submissions.get("filings", {}).get("recent", {})
    out = {"report": None, "earnings_8k": None}
    base_cik = int(cik)
    for i, form in enumerate(rec.get("form", [])):
        try:
            acc, filed = rec["accessionNumber"][i], rec["filingDate"][i]
            if as_of is not None and _date(filed) > as_of:
                continue                               # change 1: not public yet on as_of
        except (IndexError, KeyError, ValueError):
            continue
        folder = ARCHIVE_URL % (base_cik, acc.replace("-", ""))
        if out["report"] is None and form in FORMS:
            out["report"] = {"form": form, "filed": filed,
                             "period": rec["reportDate"][i], "accession": acc,
                             "url": folder + rec["primaryDocument"][i]}
        if out["earnings_8k"] is None and form == "8-K" and "2.02" in (rec.get("items", [""] * (i + 1))[i] or ""):
            e = {"form": form, "filed": filed, "accession": acc,
                 "url": folder + rec["primaryDocument"][i], "exhibit": None,
                 "index": folder + "%s-index.htm" % acc}
            if client is not None:
                try:
                    idx, _ = client.filing_index(cik, acc)
                    names = [it["name"] for it in idx.get("directory", {}).get("item", [])]
                    pick = exhibit_name(names, rec["primaryDocument"][i])
                    if pick:
                        e["exhibit"] = folder + pick
                except (DataError, KeyError, TypeError, AttributeError):
                    pass
            out["earnings_8k"] = e
        if out["report"] and out["earnings_8k"]:
            break
    return out


# ---------------------------------------------------------------- output

def _m(v):
    if v is None:
        return "-"
    return "{:,.0f}".format(v / 10**6)


def _p(v):
    return "-" if v is None else "%+.1f%%" % v if v < 0 or v > 0 else "0.0%"


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def render(ticker, result, filings):
    out = ["%s (%s), CIK %s. US$ millions except EPS. Source: SEC XBRL company facts%s." % (
        result["entity"], ticker, result["cik"],
        ", filed by %s" % result["as_of"] if result.get("as_of") else ""), ""]
    out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s" % (
        "quarter end", "fiscal", "revenue", "rev yoy", "gross", "oper", "net",
        "net inc", "EPS dil", "EPS yoy"))
    for r in result["rows"]:
        out.append("%-11s %-9s %10s %8s %7s %7s %7s %10s %8s %7s%s" % (
            r["end"], r["fiscal"].strip()[:9], _m(r["revenue"]), _p(r["revenue_yoy"]),
            _pm(r["gross_margin"]), _pm(r["operating_margin"]), _pm(r["net_margin"]),
            _m(r["net_income"]), "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"],
            _p(r["eps_yoy"]), "  derived" if r["derived"] else ""))
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters: revenue %s, net income %s, diluted EPS %s." % (
            "$" + _m(t.get("revenue")) + "M" if t.get("revenue") is not None else "-",
            "$" + _m(t.get("net_income")) + "M" if t.get("net_income") is not None else "-",
            "$%.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-"))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    if filings:
        out.append("")
        rep, e8 = filings.get("report"), filings.get("earnings_8k")
        if rep:
            out.append("Latest %s: period %s, filed %s  %s" % (rep["form"], rep["period"],
                                                              rep["filed"], rep["url"]))
        if e8:
            out.append("Latest earnings 8-K (item 2.02): filed %s  %s" % (
                e8["filed"], e8["exhibit"] or e8["index"]))
    note = stale_note(result, filings)
    if note:
        out.append("")
        out.append("NOTE: " + note)
    if any(r["derived"] for r in result["rows"]):
        out.append("")
        out.append("Derived quarters are the 10-K year minus the three 10-Qs; derived EPS is "
                   "approximate.")
    return "\n".join(out) + "\n"


def metrics_row(ticker, result):
    rows = result["rows"]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    derived = [name for key, name in (("revenue", "revenue_ttm"), ("net_income", "net_income_ttm"),
                                      ("eps_diluted", "eps_ttm")) if key in result.get("ttm_derived", ())]
    if cur.get("derived"):
        derived += ["latest quarter: " + ", ".join(cur["derived_series"])]
    return {"ticker": ticker, "name": result["entity"], "cik": result["cik"],
            "period_end": cur.get("end"), "revenue_ttm": t.get("revenue"),
            "net_income_ttm": t.get("net_income"), "eps_ttm": t.get("eps_diluted"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "net_income_growth_yoy": cur.get("net_income_yoy"),
            "gross_margin": cur.get("gross_margin"), "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"), "source": "SEC XBRL company facts",
            "filed": cur.get("filed"), "as_of": result.get("as_of"), "derived": derived}


def clean_title(title):
    return re.sub(r"\s*/[A-Z]{2,5}/?\s*$", "", title or "").strip() or None


# ---------------------------------------------------------------- the record (change 11)

def _mv(value, unit, reason):
    return MetricValue(value=value, unit=unit) if value is not None else MetricValue(value=None, unit=unit,
                                                                                      unavailable_reason=reason)


def _ratio_reason(part, whole, part_name):
    if part is None:
        return f"{part_name} not filed for this quarter"
    if whole is None:
        return "revenue not filed for this quarter"
    return "revenue is not positive"


def _line(row):
    v = row["values"]
    nf = "not filed for this quarter"
    gp_reason = "gross profit not filed for this quarter (nor cost of revenue with revenue)"
    yoy = "no comparable value a year earlier (or it was zero)"
    return QuarterLine(
        fiscal_year=int(row["fy"]) if isinstance(row["fy"], int) else _date(row["end"]).year,
        fiscal_period=str(row["fp"] or ""), period_end=_date(row["end"]),
        period_start=_date(row["start"]) if row["start"] else None,
        revenue=_mv(row["revenue"], "USD", nf), net_income=_mv(row["net_income"], "USD", nf),
        eps_diluted=_mv(row["eps_diluted"], "USD/shares", nf),
        gross_profit=_mv(row["gross_profit"], "USD", gp_reason),
        operating_income=_mv(row["operating_income"], "USD", nf),
        gross_margin=_mv(row["gross_margin"], "percent",
                         _ratio_reason(row["gross_profit"], row["revenue"], "gross profit")),
        operating_margin=_mv(row["operating_margin"], "percent",
                             _ratio_reason(row["operating_income"], row["revenue"], "operating income")),
        net_margin=_mv(row["net_margin"], "percent", _ratio_reason(row["net_income"], row["revenue"], "net income")),
        revenue_yoy=_mv(row["revenue_yoy"], "percent", yoy if row["revenue"] is not None else nf),
        net_income_yoy=_mv(row["net_income_yoy"], "percent", yoy if row["net_income"] is not None else nf),
        eps_yoy=_mv(row["eps_yoy"], "percent", yoy if row["eps_diluted"] is not None else nf),
        filed=_date(row["filed"]), first_filed=_date(row["first_filed"]), form=row["form"],
        derived=row["derived"], derived_series=tuple(row["derived_series"]),
        amended_series=tuple(row["amended_series"]),
        sources=tuple(sorted({a for x in v.values() for a in x["sources"] if a})))


def _fact(key, v):
    return FilingFact(concept="us-gaap:" + v["concept"], value=v["value"], unit=unit_of(key),
                      period_start=_date(v["start"]), period_end=_date(v["end"]),
                      fiscal_year=int(v["fy"]) if isinstance(v["fy"], int) else _date(v["end"]).year,
                      fiscal_period=str(v["fp"] or ""), form=str(v["form"] or ""), filed=_date(v["filed"]),
                      first_filed=_date(v["first_filed"]), accession=str(v["accn"] or ""),
                      derived=v["derived"], amendment=v["amendment"])


def _link(f, exhibit=False):
    if not f:
        return None
    return FilingLink(form=f["form"], filed=_date(f["filed"]), accession=f["accession"], url=f["url"],
                      period=_date(f["period"]) if f.get("period") else None,
                      exhibit_url=f.get("exhibit") if exhibit else None)


def to_report(result, *, ticker, as_of, source_url, retrieved_at, quarters, filings=None,
              filings_retrieved_at=None) -> EarningsReport:
    rows = result["rows"]
    facts = {}
    for row in rows:
        for key, v in row["values"].items():
            facts[(v["concept"], v["start"], v["end"])] = _fact(key, v)
    t = result["ttm"]
    ttm_reason = ("fewer than four consecutive quarters end at the newest quarter" if not result["ttm_ends"]
                  else "not filed for one of the same four quarters")
    ttm = {k: _mv(t.get(k), unit_of(k), ttm_reason) for k in ("revenue", "net_income", "eps_diluted")}
    limits = [Limitation(code="AS_FILED", detail="Values as the company tagged them in its filings (XBRL); not "
                         "audited by TraderOS."),
              Limitation(code="AVAILABLE_ON_FILED_DATE", detail="Each number counts as known from its filing date; "
                         "the time of day is not recorded.")]
    if any(r["derived"] for r in rows) or result["ttm_derived"]:
        limits.append(Limitation(code="DERIVED_Q4", detail="Derived quarters are the 10-K year minus the three "
                                 "10-Qs; derived EPS is approximate."))
    if any(r["amended_series"] for r in rows):
        limits.append(Limitation(code="AMENDED", detail="Some values come from amended filings (10-K/A or 10-Q/A)."))
    for key, units in sorted(result["refused_units"].items()):
        limits.append(Limitation(code="CURRENCY", detail=f"{key.replace('_', ' ')} is reported in "
                                 f"{', '.join(units)}, not USD; TraderOS reads USD figures only."))
    note = stale_note(result, filings)
    if note:
        limits.append(Limitation(code="FACTS_LAG", detail=note))
    if ticker and as_of < retrieved_at.date():
        limits.append(Limitation(code="TICKERS_TODAY", detail=f"{ticker} was matched to CIK {result['cik']} with the "
                                 f"SEC's current ticker list, not the list on {as_of}: a ticker since moved to "
                                 "another company would point to the new one."))
    complete = "revenue" in t and "net_income" in t
    cik = result["cik"]
    report = EarningsReport(
        report_id="ER-" + sha256_bytes(canonical_bytes([cik, ticker, as_of, retrieved_at, quarters,
                                                        filings is not None, filings_retrieved_at]))[:20],
        cik=cik, entity=str(result["entity"] or ""), ticker=ticker, as_of=as_of,
        quarters=tuple(_line(r) for r in reversed(rows)),
        facts=tuple(sorted(facts.values(), key=lambda f: (f.period_end, f.concept))),
        ttm=ttm, ttm_quarter_ends=tuple(_date(e) for e in result["ttm_ends"]),
        ttm_derived=tuple(result["ttm_derived"]),
        concepts={k: tuple(v) for k, v in result["concepts"].items() if v},
        changes=tuple(changes(result)),
        latest_report=_link((filings or {}).get("report")),
        earnings_release=_link((filings or {}).get("earnings_8k"), exhibit=True),
        source_url=source_url, retrieved_at=retrieved_at,
        completeness=Completeness.COMPLETE if complete else Completeness.PARTIAL,
        limitations=tuple(limits), safe_for=(SafeFor.RESEARCH,))
    return with_hash(report)


def fetch(client, ticker=None, *, cik=None, as_of=None, quarters=6, filings=True) -> EarningsReport:
    """One company's earnings/1 record, as of a day (today when not given), fetched through the SEC adapter."""
    as_of = as_of or datetime.now(UTC).date()
    name = None
    if cik is None:
        cik, title = client.cik_for(ticker)
        name = clean_title(title)
    cik = f"{int(cik):010d}"
    cf, fetched = client.companyfacts(cik)
    result = analyse(cf, quarters=quarters, name=name, as_of=as_of)
    found = sub_fetched = None
    if filings:
        sub, sub_fetched = client.submissions(cik)
        found = latest_filings(sub, cik, client, as_of=as_of)
    return to_report(result, ticker=ticker.upper() if ticker else None, as_of=as_of, source_url=FACTS_URL % cik,
                     retrieved_at=fetched, quarters=quarters, filings=found, filings_retrieved_at=sub_fetched)


def describe(r: EarningsReport) -> str:
    """The upstream text report (render), from the record."""
    def mv(m):
        return None if m is None else m.value
    rows = [{"end": q.period_end.isoformat(), "fiscal": f"{q.fiscal_period} {q.fiscal_year}", "derived": q.derived,
             "revenue": mv(q.revenue), "gross_profit": mv(q.gross_profit), "operating_income": mv(q.operating_income),
             "net_income": mv(q.net_income), "eps_diluted": mv(q.eps_diluted), "revenue_yoy": mv(q.revenue_yoy),
             "net_income_yoy": mv(q.net_income_yoy), "eps_yoy": mv(q.eps_yoy), "gross_margin": mv(q.gross_margin),
             "operating_margin": mv(q.operating_margin), "net_margin": mv(q.net_margin)}
            for q in reversed(r.quarters)]
    ttm = {k: v.value for k, v in (r.ttm or {}).items() if v.value is not None}
    filings = {"report": None, "earnings_8k": None}
    if r.latest_report:
        f = r.latest_report
        filings["report"] = {"form": f.form, "filed": f.filed.isoformat(), "accession": f.accession, "url": f.url,
                             "period": f.period.isoformat() if f.period else None}
    if r.earnings_release:
        f = r.earnings_release
        index = ARCHIVE_URL % (int(r.cik), f.accession.replace("-", "")) + "%s-index.htm" % f.accession
        filings["earnings_8k"] = {"form": f.form, "filed": f.filed.isoformat(), "accession": f.accession,
                                  "url": f.url, "exhibit": f.exhibit_url, "index": index}   # upstream's link: the index
    result = {"entity": r.entity, "cik": r.cik, "rows": rows, "ttm": ttm, "as_of": r.as_of}
    text = render(r.ticker or r.cik, result, filings if (r.latest_report or r.earnings_release) else None)
    return text.rstrip("\n")


__all__ = ["CONCEPTS", "analyse", "changes", "clean_title", "describe", "exhibit_name", "facts_for", "fetch",
           "latest_filings", "margin", "metrics_row", "pct", "periods", "quarterly", "render", "series",
           "stale_note", "to_report"]
