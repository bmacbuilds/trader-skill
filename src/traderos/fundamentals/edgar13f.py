"""13F reader (S06 Institutional Tracker), ported from the upstream trader-copycat.

Provenance: tank/trader-skill/trader-copycat/edgar13f.py, with filers.json and share_classes.json beside it
(upstream trader-skill at 5189e79; tank commit 92d084f), MIT (notice:
the NOTICE file). It exists only in v1; none of the ten reviewed upstream PRs touches it.

The upstream text, kept: reads an investor's public 13F-HR filings on SEC EDGAR and rebuilds the portfolio as weights.

What a 13F is, and is not:
  - It is filed once a quarter, up to 45 days after the quarter ends. The holdings are a snapshot of the last day
    of the quarter, so they can be four and a half months old by the time you read them.
  - It lists long positions in US-listed stocks (and some options and convertible bonds). It does not show shorts,
    cash, bonds, foreign stocks, private companies or anything bought and sold inside the quarter.
  - It is filed by the firm, not by the person the firm is known for.

Values: filings made from 2023-01-03 report value in dollars; older ones in thousands. The tool converts older
filings to dollars.

Weights: pct_of_13f is each position's value divided by the total value of the whole information table. "copy"
keeps the stock positions you pick and scales their pct_of_13f (rounded to 2 decimals, the numbers you see in the
holdings table) so they sum to 100. Options rows (PUT/CALL) and bond rows (PRN) are never copied: they are listed as
excluded.

What changed from the upstream file, each with its reason (ADR-011):
  1. POINT IN TIME: only filings made on or before `as_of` are read, amendments included (QT01). Upstream always read
     the newest, so there was no way to ask what was public on a given day.
  2. The XML is refused when it declares a DOCTYPE or an ENTITY (04.2, untrusted input), however it is encoded:
     the bytes are checked before parsing, and the parser itself refuses one (a UTF-16 document hides it from a
     byte search). The upstream parser accepted an internal entity and put its text into an issuer's name.
  3. Decimal, not float (04.3): 0.1 + 0.2 was 0.30000000000000004 dollars. A row with no value or share count is
     refused rather than read as zero (09.4: unknown is never zero).
  4. Fetching goes through traderos.adapters.sec, with its cache and size caps (04.3, 06.1(d)); EDGAR's filer
     search is the adapter's efts.sec.gov endpoint.
  5. OpenFIGI (--figi) is not ported: it is not an approved source (06.1). The bundled share_classes.json and your
     own CUSIP map (--map FILE) are kept, and the messages that pointed at --figi now point at the map.
  6. Amendments are APPLIED, not only noted: a 13F-HR/A whose cover says RESTATEMENT replaces the table, and one
     that says NEW HOLDINGS adds its rows. Upstream listed them and asked the reader to open each with --accession.
     The holdings are then dated by the latest amendment applied (`filed`, "made public"), the first day they
     could have been known (QT01); the text still names the original filing's own date beside its accession. An
     amendment that cannot be read or is refused (a DOCTYPE in it) is not applied, and a note says why: it does
     not block the quarter.
  7. Typed records: holdings/1 (each Holding with ticker, pct_of_13f and share classes), holdings-diff/1 and
     copy-targets/1. The command line is traderos's (holdings --find, --filings, --diff, --copy).
  8. "copy" produces HYPOTHETICAL weights for research and planning (copy-targets/1). It writes no targets file for
     another tool, sizes nothing and places no order; the hand-off line to a budget skill is gone.
  9. "copy" carries each weight unrounded (its scaling of the 2-decimal pct_of_13f, kept); only the text rounds it,
     so the weights sum to 100, not about 100. A position under 0.005% of the 13F shows as 0.00 and scales to a
     weight of 0, which a copy-targets record cannot hold (a weight is above zero): it is left out of the copy
     with a line naming it and its true weight (its value's share of the copy). Upstream gave it weight 0.00, and the
     port refused the whole copy (`holdings 1067983 --copy` on Berkshire's Q2 2026: D.R. Horton, 0.00019%).
 10. Input limits (04.2): a CIK has at most 10 digits (upstream padded an 11-digit one into a wrong URL); your CUSIP
     map is at most 1 MB and must be JSON whose tickers are text (upstream stopped with a traceback on either).
 11. Your CUSIP map is only the file you name (--map FILE). Upstream also read trader/tickers.json from the current
     folder by default, so the same command gave other tickers depending on where it was run.
 12. Records say that tickers come from the SEC's current company list (TICKERS_TODAY), not the quarter's.
"""
from __future__ import annotations

import json
import pathlib
import re
import xml.etree.ElementTree as ET
import xml.parsers.expat
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from traderos.contracts import (
    DataError,
    ErrorCode,
    Limitation,
    SafeFor,
    ValidationError,
    canonical_bytes,
    sha256_bytes,
    with_hash,
)
from traderos.contracts.fundamentals import (
    CopyTargets,
    Holding,
    HoldingChange,
    HoldingsDiff,
    HoldingsReport,
    SkippedPosition,
    TargetWeight,
)

HERE = pathlib.Path(__file__).resolve().parent
FILERS = HERE / "filers.json"
CLASSES = HERE / "share_classes.json"

ARCHIVE = "https://www.sec.gov/Archives/edgar/data/%s/%s/"
DOLLARS_FROM = date(2023, 1, 3)
FORMS = ("13F-HR", "13F-HR/A")
MAX_MAP_BYTES = 1024 * 1024
PCT, TENTH = Decimal("0.01"), Decimal("0.1")


class EdgarError(DataError):
    """The upstream error type, now a typed TraderOS refusal (MISSING_DATA unless said otherwise)."""
    default_code = ErrorCode.MISSING_DATA


def _round(x, step):
    return x.quantize(step, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------- filers

def cik10(value):
    digits = re.sub(r"\D", "", str(value))
    if not digits or len(digits) > 10:
        raise ValidationError("not a CIK: %r" % value)
    return digits.zfill(10)


def load_filers(path=FILERS):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["filers"]


def find_bundled(query, filers=None):
    """Bundled filers whose name, person or alias contains every word of query."""
    filers = load_filers() if filers is None else filers
    q = query.lower().strip()
    if re.fullmatch(r"\d{1,10}", q):
        return [f for f in filers if f["cik"] == cik10(q)]
    words = q.split()
    hits = []
    for f in filers:
        hay = " ".join([f["name"], f.get("person", "")] + f.get("aliases", [])).lower()
        if all(w in hay for w in words):
            hits.append(f)
    return hits


def search_sec(client, query):
    """Every EDGAR filer whose name matches (the EDGAR company search box)."""
    data, _ = client.search(query)
    out = []
    for hit in data.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        out.append({"cik": cik10(hit.get("_id", "0")), "name": src.get("entity", "")})
    return out


# ---------------------------------------------------------------- filings

def list_filings(submissions, forms=FORMS, as_of=None):
    """13F filings from a submissions JSON filed on or before as_of, newest filing first."""
    rec = submissions.get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(rec.get("form", [])):
        if form not in forms:
            continue
        filed = rec["filingDate"][i]
        try:
            if as_of is not None and date.fromisoformat(filed) > as_of:
                continue                               # change 1: not public yet on as_of
        except (TypeError, ValueError):
            continue                                   # an unreadable filing date is never known to be public
        out.append({
            "form": form,
            "accession": rec["accessionNumber"][i],
            "filed": filed,
            "period": rec.get("reportDate", [""] * (i + 1))[i],
            "primary": rec.get("primaryDocument", [""] * (i + 1))[i],
        })
    out.sort(key=lambda f: (f["filed"], f["accession"]), reverse=True)
    return out


def pick_filing(filings, period=None, accession=None, before=None):
    """The original 13F-HR for a period (default: the newest), plus any amendments."""
    if accession:
        hit = [f for f in filings if f["accession"] == accession]
        if not hit:
            raise EdgarError("accession %s is not in this filer's 13F list" % accession)
        chosen = hit[0]
    else:
        originals = [f for f in filings if f["form"] == "13F-HR"]
        if period:
            originals = [f for f in originals if f["period"] == period]
        if before:
            originals = [f for f in originals if f["period"] < before]
        if not originals:
            raise EdgarError("no 13F-HR found%s" % (" for period %s" % period if period else ""))
        originals.sort(key=lambda f: (f["period"], f["filed"]), reverse=True)
        chosen = originals[0]
    amendments = [f for f in filings if f["form"] == "13F-HR/A"
                  and f["period"] == chosen["period"] and f is not chosen]
    return chosen, amendments


def archive_url(cik, accession):
    return ARCHIVE % (int(cik10(cik)), accession.replace("-", ""))


def info_table_name(index_json):
    """The information table XML inside a filing folder (index.json)."""
    names = [it["name"] for it in index_json.get("directory", {}).get("item", [])]
    xmls = [n for n in names if n.lower().endswith(".xml")
            and n.lower() not in ("primary_doc.xml", "filingsummary.xml")]
    if not xmls:
        raise EdgarError("no information table XML in this filing (files: %s). Filings "
                         "before mid 2013 are plain text and are not supported."
                         % ", ".join(names))
    for n in xmls:
        if re.search(r"info|table|13f", n, re.I):
            return n
    return xmls[0]


def value_multiplier(filed):
    """1 for dollars (filed on or after 2023-01-03), 1000 for older filings."""
    try:
        d = datetime.strptime(filed[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 1
    return 1 if d >= DOLLARS_FROM else 1000


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


class _Declared(Exception):
    """A DOCTYPE or ENTITY declaration met by the parser."""


def _declares(data):
    """True when the XML parser meets a DOCTYPE or an entity declaration, in whatever encoding the document is in."""
    def stop(*_):
        raise _Declared
    p = xml.parsers.expat.ParserCreate()
    p.StartDoctypeDeclHandler = p.EntityDeclHandler = p.UnparsedEntityDeclHandler = stop
    try:
        p.Parse(data, True)
    except _Declared:
        return True
    except xml.parsers.expat.ExpatError:
        return False                                   # not XML: ET.fromstring says so below
    return False


def _xml(data, what):
    """Parsed XML from the internet, refused if it declares a DOCTYPE or ENTITY (change 2)."""
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", data, re.I) or _declares(data):
        raise ValidationError("%s declares a DOCTYPE or ENTITY; refused before parsing" % what,
                              code=ErrorCode.INVALID_INPUT)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise EdgarError("%s is not valid XML: %s" % (what, exc), code=ErrorCode.INTEGRITY_MISMATCH) from None


def parse_info_table(data, multiplier=1):
    """Rows of a 13F information table XML, as dicts. Values in dollars."""
    root = _xml(data, "information table")
    rows = []
    for el in root.iter():
        if _local(el.tag) != "infoTable":
            continue
        f = {}
        for child in el.iter():
            name = _local(child.tag)
            if name not in f and child.text and child.text.strip():
                f[name] = child.text.strip()
        try:
            value = Decimal(f["value"].replace(",", "")) * multiplier
            shares = Decimal(f["sshPrnamt"].replace(",", ""))
            if not (value.is_finite() and shares.is_finite()):
                raise InvalidOperation
        except (KeyError, InvalidOperation):
            raise EdgarError("bad or missing number in row for %s" % f.get("nameOfIssuer"),
                             code=ErrorCode.INTEGRITY_MISMATCH) from None
        rows.append({
            "issuer": f.get("nameOfIssuer", ""),
            "title": f.get("titleOfClass", ""),
            "cusip": f.get("cusip", "").upper(),
            "figi": f.get("figi", ""),
            "value": value,
            "shares": shares,
            "share_type": f.get("sshPrnamtType", "SH").upper(),
            "put_call": f.get("putCall", "").upper(),
            "manager": f.get("otherManager", ""),
        })
    if not rows:
        raise EdgarError("no infoTable rows found. Is this a 13F information table?")
    return rows


def parse_cover(data):
    """tableValueTotal, tableEntryTotal and the cover facts from primary_doc.xml."""
    try:
        root = _xml(data, "cover page")
    except EdgarError:
        return {}
    want = {"tableValueTotal", "tableEntryTotal", "periodOfReport", "isAmendment",
            "amendmentType", "reportType", "otherIncludedManagersCount", "name"}
    out = {}
    for el in root.iter():
        name = _local(el.tag)
        if name in want and name not in out and el.text and el.text.strip():
            out[name] = el.text.strip()
    return out


def is_stock(p):
    return p["share_type"] == "SH" and not p["put_call"]


def aggregate(rows):
    """One position per CUSIP (and per PUT/CALL), summing the manager split rows."""
    total = sum((r["value"] for r in rows), Decimal(0))
    groups = {}
    order = []
    for r in rows:
        key = (r["cusip"], r["put_call"], r["share_type"])
        if key not in groups:
            groups[key] = {"issuer": r["issuer"], "title": r["title"], "cusip": r["cusip"],
                           "figi": r["figi"], "put_call": r["put_call"],
                           "share_type": r["share_type"], "value": Decimal(0), "shares": Decimal(0),
                           "rows": 0}
            order.append(key)
        g = groups[key]
        g["value"] += r["value"]
        g["shares"] += r["shares"]
        g["rows"] += 1
    positions = [groups[k] for k in order]
    for p in positions:
        p["pct_of_13f"] = _round(100 * p["value"] / total, PCT) if total else Decimal(0)
        p["cusips"] = [p["cusip"]]
    positions.sort(key=lambda p: (-p["value"], p["cusip"]))
    return positions, total


def combine_classes(positions, total):
    """Sum the share classes of one issuer (same 6-character CUSIP issuer code).

    Only stock rows are combined. The combined position keeps the ticker and
    name of its biggest class and lists every CUSIP and ticker it holds."""
    out, by_issuer = [], {}
    for p in positions:
        if not is_stock(p):
            out.append(dict(p))
            continue
        key = p["cusip"][:6]
        if key not in by_issuer:
            q = dict(p)
            q["cusips"] = [p["cusip"]]
            q["tickers"] = [p.get("ticker")] if p.get("ticker") else []
            q["classes"] = [p["title"]]
            by_issuer[key] = q
            out.append(q)
            continue
        q = by_issuer[key]
        q["value"] += p["value"]
        q["shares"] += p["shares"]
        q["rows"] += p["rows"]
        q["cusips"].append(p["cusip"])
        q["classes"].append(p["title"])
        if p.get("ticker"):
            q["tickers"].append(p["ticker"])
    for q in out:
        q["pct_of_13f"] = _round(100 * q["value"] / total, PCT) if total else Decimal(0)
    out.sort(key=lambda p: (-p["value"], p["cusip"]))
    return out


# ---------------------------------------------------------------- tickers

SUFFIX_STATE = re.compile(r"\s*/\s*[A-Z]{2,5}\s*/?\s*$")
ABBREV = {
    "AMER": "AMERICA", "AMERN": "AMERICAN", "FINL": "FINANCIAL", "FIN": "FINANCIAL",
    "PETE": "PETROLEUM", "HLDGS": "HOLDINGS", "HLDG": "HOLDING", "HOLDG": "HOLDING",
    "INTL": "INTERNATIONAL", "GRP": "GROUP", "SVCS": "SERVICES", "SVC": "SERVICE",
    "SYS": "SYSTEMS", "MGMT": "MANAGEMENT", "INDS": "INDUSTRIES", "NATL": "NATIONAL",
    "ENTMT": "ENTERTAINMENT", "PAC": "PACIFIC", "LABS": "LABORATORIES",
    "PPTYS": "PROPERTIES", "RLTY": "REALTY", "TECHS": "TECHNOLOGIES", "TECH": "TECHNOLOGY",
    "COMMNS": "COMMUNICATIONS", "COMMUNICATN": "COMMUNICATIONS", "MFG": "MANUFACTURING",
    "ELEC": "ELECTRIC", "PHARMACEUTICALS": "PHARMACEUTICALS", "PHARMS": "PHARMACEUTICALS",
    "BANCORPORATION": "BANCORP", "RES": "RESOURCES", "SOLUTIONS": "SOLUTIONS",
    "WTR": "WATER", "MTRS": "MOTORS", "AIRLS": "AIRLINES", "CHEM": "CHEMICAL",
}
DROP = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED",
        "PLC", "LLC", "LP", "SA", "NV", "AG", "SE", "THE", "AND", "OF"}
NONCOMMON = re.compile(r"-P[A-Z]*$|-WT[A-Z]*$|-WS$|-U$|-UN$|-R$|-RT$")


def norm_name(name):
    s = (name or "").upper().replace("&", " AND ")
    for _ in range(2):
        s = SUFFIX_STATE.sub("", s)
    s = s.replace("'", "").replace("’", "")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    words = [ABBREV.get(w, w) for w in s.split()]
    return [w for w in words if w not in DROP]


def name_keys(name):
    words = norm_name(name)
    return "".join(words), " ".join(sorted(words))


def norm_ticker(t):
    t = (t or "").strip().upper()
    return re.sub(r"[-/ ]([A-Z]{1,2})$", r".\1", t) if not NONCOMMON.search(t) else t


def class_letter(title):
    m = re.search(r"\b(?:CL|CLASS|SER|SERIES)\s*([A-Z])\b", (title or "").upper())
    return m.group(1) if m else None


def load_company_tickers(data):
    """company_tickers.json -> ordered list of (cik10, ticker, title)."""
    rows = []
    items = data.values() if isinstance(data, dict) else data
    for v in items:
        rows.append((cik10(v["cik_str"]), v["ticker"], v["title"]))
    return rows


class TickerIndex:
    """Name lookups over SEC's company_tickers.json."""

    def __init__(self, rows):
        self.by_cik = {}
        self.titles = {}
        self.spaceless = {}
        self.sorted_words = {}
        for cik, ticker, title in rows:
            self.by_cik.setdefault(cik, []).append(ticker)
            if cik in self.titles:
                continue
            self.titles[cik] = title
            a, b = name_keys(title)
            if a:
                self.spaceless.setdefault(a, set()).add(cik)
                self.sorted_words.setdefault(b, set()).add(cik)

    def match_cik(self, issuer):
        a, b = name_keys(issuer)
        if not a:
            return None, None
        for hits, how in ((self.spaceless.get(a), "name"), (self.sorted_words.get(b), "name")):
            if hits and len(hits) == 1:
                return next(iter(hits)), how
        # The 13F truncates or decorates names ("JEFFERIES FINANCIAL GROUP IN").
        best = []
        for key, ciks in self.spaceless.items():
            if len(key) >= 8 and len(ciks) == 1 and (a.startswith(key) or key.startswith(a)):
                best.append((len(key), next(iter(ciks))))
        if best:
            best.sort(reverse=True)
            if len(best) == 1 or best[0][0] > best[1][0]:
                return best[0][1], "name-prefix"
        return None, None

    def ticker_for(self, cik, title_of_class):
        """(ticker, needs_check) for a company and a 13F titleOfClass."""
        tickers = self.by_cik.get(cik, [])
        cands = [t for t in tickers if not NONCOMMON.search(t)] or tickers
        if not cands:
            return None, True
        if len(cands) == 1:
            return norm_ticker(cands[0]), False
        if all(t.startswith(cands[0]) for t in cands[1:]):
            return norm_ticker(cands[0]), False
        letter = class_letter(title_of_class)
        if letter:
            for t in cands:
                if t.upper().endswith(("-" + letter, "." + letter, "/" + letter)):
                    return norm_ticker(t), False
            for t in cands:
                if len(t) >= 4 and t.upper().endswith(letter):
                    return norm_ticker(t), True
        return norm_ticker(cands[0]), True


def load_overrides(paths):
    out = {}
    for path in paths:
        path = pathlib.Path(path) if path else None
        if path and path.is_file():
            if path.stat().st_size > MAX_MAP_BYTES:
                raise ValidationError("%s is larger than %d bytes" % (path, MAX_MAP_BYTES))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise ValidationError("%s is not a JSON CUSIP map" % path) from None
            table = data.get("cusips", data) if isinstance(data, dict) else {}
            for k, v in table.items():
                if re.fullmatch(r"[0-9A-Z]{9}", str(k).upper()):
                    if not isinstance(v, str):
                        raise ValidationError("%s: the ticker for %s is not text: %r" % (path, k, v))
                    out[str(k).upper()] = norm_ticker(v)
    return out


def pretty_name(issuer, sec_title=None):
    name = sec_title if sec_title and re.search(r"[a-z]", sec_title) else (sec_title or issuer)
    name = SUFFIX_STATE.sub("", name or "")
    name = re.sub(r",?\s+(Inc\.?|Incorporated|Corp\.?|Corporation|Co\.?|Company|Ltd\.?|"
                  r"Limited|plc|PLC|LLC|L\.P\.|N\.V\.|S\.A\.)$", "", name.strip(), flags=re.I)
    name = re.sub(r",?\s+(INC|CORP|CO|LTD|PLC)$", "", name.strip())
    if name.isupper():
        small = {"OF", "AND", "THE", "FOR", "IN"}
        words = name.lower().split()
        name = " ".join(w if (w.upper() in small and i) else w.capitalize()
                        for i, w in enumerate(words))
    return name.strip()


def resolve_tickers(positions, index=None, overrides=None):
    """Give every position a ticker, where it came from and whether to check it."""
    overrides = overrides or {}
    for p in positions:
        c = p["cusip"]
        sec_title = None
        cik = None
        if index is not None:
            cik, how = index.match_cik(p["issuer"])
            if cik:
                sec_title = index.titles.get(cik)
                p["issuer_cik"] = cik
        if c in overrides:
            p["ticker"], p["ticker_source"], p["ticker_check"] = overrides[c], "override", False
        elif cik:
            t, check = index.ticker_for(cik, p["title"])
            p["ticker"], p["ticker_source"] = t, how
            p["ticker_check"] = bool(check or how == "name-prefix")
        else:
            p["ticker"], p["ticker_source"], p["ticker_check"] = None, "none", True
        p["name"] = pretty_name(p["issuer"], sec_title)
    return positions


# ---------------------------------------------------------------- analysis

def diff(current, prior):
    """Compare two quarters by CUSIP: NEW, SOLD OUT, ADDED, TRIMMED, UNCHANGED."""
    before = {(p["cusip"], p["put_call"]): p for p in prior}
    now = {(p["cusip"], p["put_call"]): p for p in current}
    out = []
    zero = Decimal(0)
    for key, p in now.items():
        q = before.get(key)
        if q is None:
            status, change = "NEW", p["shares"]
        elif p["shares"] > q["shares"]:
            status, change = "ADDED", p["shares"] - q["shares"]
        elif p["shares"] < q["shares"]:
            status, change = "TRIMMED", p["shares"] - q["shares"]
        else:
            status, change = "UNCHANGED", zero
        out.append({"status": status, "issuer": p["issuer"], "title": p["title"],
                    "cusip": p["cusip"], "ticker": p.get("ticker"), "put_call": p["put_call"],
                    "shares_before": q["shares"] if q else zero, "shares_after": p["shares"],
                    "change": change,
                    "change_pct": (_round(100 * change / q["shares"], TENTH)
                                   if q and q["shares"] else None),
                    "value_after": p["value"], "value_before": q["value"] if q else zero})
    for key, q in before.items():
        if key not in now:
            out.append({"status": "SOLD OUT", "issuer": q["issuer"], "title": q["title"],
                        "cusip": q["cusip"], "ticker": q.get("ticker"),
                        "put_call": q["put_call"], "shares_before": q["shares"],
                        "shares_after": zero, "change": -q["shares"], "change_pct": Decimal(-100),
                        "value_after": zero, "value_before": q["value"]})
    rank = {"NEW": 0, "SOLD OUT": 1, "ADDED": 2, "TRIMMED": 3, "UNCHANGED": 4}
    out.sort(key=lambda d: (rank[d["status"]], -max(d["value_after"], d["value_before"])))
    return out


def copy_weights(positions, top=None, min_pct=Decimal(0), exclude=(), need_ticker=True):
    """Pick the stock positions to copy and scale them to 100.

    Returns (weights, skipped). Weights are pct_of_13f scaled to sum to 100, unrounded (change 9: the text
    rounds them to 2 decimals). A position whose weight is zero is skipped, with its true weight."""
    exclude = {e.upper() for e in exclude}
    chosen, skipped = [], []
    for p in positions:
        why = None
        if not is_stock(p):
            why = "%s row, not a stock you can copy" % (p["put_call"] or p["share_type"])
        elif need_ticker and not p.get("ticker"):
            why = "no ticker found (add it to your CUSIP map: --map FILE)"
        elif p.get("ticker") and p["ticker"].upper() in exclude:
            why = "excluded by you"
        elif p["pct_of_13f"] < min_pct:
            why = "below --min-pct %s" % _round(Decimal(min_pct), PCT)
        if why:
            skipped.append({"issuer": p["issuer"], "ticker": p.get("ticker"), "why": why})
            continue
        chosen.append(p)
    if top:
        for p in chosen[top:]:
            skipped.append({"issuer": p["issuer"], "ticker": p.get("ticker"),
                            "why": "outside the top %d" % top})
        chosen = chosen[:top]
    base = sum((p["pct_of_13f"] for p in chosen), Decimal(0))
    if not chosen or base <= 0:
        raise EdgarError("nothing left to copy")
    value = sum((p["value"] for p in chosen), Decimal(0))
    weights = []
    for p in chosen:
        weight = 100 * p["pct_of_13f"] / base                          # change 9: unrounded; the text rounds it
        if weight <= 0:                                                # change 9: left out by name, not a refusal
            true = 100 * p["value"] / value if value else Decimal(0)
            skipped.append({"issuer": p["issuer"], "ticker": p.get("ticker"),
                            "why": "true weight %s%% of the copy: its share of the 13F rounds to 0.00%%, so it "
                                   "scales to 0, and a copy weight is above zero" % _fmt_pct(true)})
            continue
        w = {"ticker": p["ticker"], "name": p.get("name") or p["issuer"],
             "weight": weight,
             "pct_of_13f": p["pct_of_13f"], "value": p["value"], "shares": p["shares"],
             "cusips": p.get("cusips", [p["cusip"]])}
        if p.get("tickers") and len(p["tickers"]) > 1:
            w["classes"] = p["tickers"]
        if p.get("ticker_check"):
            w["check_ticker"] = True
        weights.append(w)
    return weights, skipped


CAVEATS = [
    "A 13F is a snapshot of the last day of the quarter, filed up to 45 days later. "
    "The investor may have sold or added since.",
    "It shows long positions in US-listed stocks only: no shorts, cash, bonds, foreign "
    "stocks, private companies or trades made inside the quarter.",
    "It is filed by the firm, not by the person it is known for.",
    "Education and tooling, not financial advice. Nothing here places an order.",
]


# ---------------------------------------------------------------- output

def _money(v):
    return "${:,.0f}".format(v)


def _fmt_pct(v):
    """A percent for the text: 2 decimals, or two significant digits when that would hide a weight (change 9)."""
    shown = _round(v, PCT)
    if not shown and v:
        shown = v.quantize(Decimal(1).scaleb(v.adjusted() - 1), rounding=ROUND_HALF_EVEN)
    return format(shown, "f")


def _shares(v):
    return "{:,.0f}".format(v) if v == v.to_integral_value() else "{:,.2f}".format(v)


def render_holdings(meta, positions, total, top=None, combined=False):
    out = []
    out.append("%s  13F-HR for quarter ending %s, filed %s (accession %s)" % (
        meta.get("filer", "?"), meta.get("period", "?"), meta.get("original_filed", meta.get("filed", "?")),
        meta.get("accession", "?")))
    out.append("Total %s across %d positions%s. Weights are %% of that total." % (
        _money(total), len(positions), ", share classes combined" if combined else ""))
    if meta.get("total_check"):
        out.append(meta["total_check"])
    out.append("")
    out.append("%3s  %-11s %-32s %16s %18s %7s" % ("#", "ticker", "issuer", "shares", "value", "%"))
    shown = positions[:top] if top else positions
    for i, p in enumerate(shown, 1):
        tick = p.get("ticker") or "?"
        if p.get("tickers") and len(p["tickers"]) > 1:
            tick = "+".join(p["tickers"])
        flag = " *" if p.get("ticker_check") else ""
        kind = "" if is_stock(p) else " [%s]" % (p["put_call"] or p["share_type"])
        out.append("%3d  %-11s %-32s %16s %18s %7.2f%s" % (
            i, tick, (p["issuer"] + kind)[:32], _shares(p["shares"]), _money(p["value"]),
            p["pct_of_13f"], flag))
    if top and len(positions) > top:
        out.append("     ... %d more" % (len(positions) - top))
    if any(p.get("ticker_check") for p in shown):
        out.append("")
        out.append("* ticker matched by name or share class guess: confirm it before you "
                   "use it, or fix it in your CUSIP map (--map FILE).")
    for note in meta.get("notes", []):
        out.append("")
        out.append(note)
    out.append("")
    out.append("Holdings as of %s. A 13F is quarterly and up to 45 days late, long US "
               "equity positions only." % meta.get("period", "the quarter end"))
    return "\n".join(out) + "\n"


def render_diff(meta, rows):
    out = ["%s: %s vs %s" % (meta.get("filer", "?"), meta.get("period", "?"),
                             meta.get("prior_period", "?")), ""]
    for status in ("NEW", "SOLD OUT", "ADDED", "TRIMMED", "UNCHANGED"):
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        out.append("%s (%d)" % (status, len(group)))
        for r in group:
            tick = r.get("ticker") or "?"
            label = "%s %s" % (r["issuer"], r["title"])
            if status == "UNCHANGED":
                out.append("  %-7s %-40s %s shares" % (tick, label[:40], _shares(r["shares_after"])))
            elif status == "SOLD OUT":
                out.append("  %-7s %-40s %s shares (%s at the prior quarter end)" % (
                    tick, label[:40], _shares(r["shares_before"]), _money(r["value_before"])))
            else:
                pct = "" if r["change_pct"] is None else " (%+.1f%%)" % r["change_pct"]
                out.append("  %-7s %-40s %s -> %s%s" % (
                    tick, label[:40], _shares(r["shares_before"]), _shares(r["shares_after"]), pct))
        out.append("")
    out.append("Share counts at two quarter ends. Trades inside the quarter, and their "
               "prices, are not in a 13F.")
    return "\n".join(out) + "\n"


def render_copy(meta, weights, combined, top, skipped=()):
    out = ["Hypothetical weights from %s 13F-HR, quarter ending %s, filed %s (accession %s), %s." % (
        meta.get("filer"), meta.get("period"), meta.get("original_filed", meta.get("filed")), meta.get("accession"),
        _method(top, combined))]
    for w in weights:
        out.append("  %-7s %-28s %6.2f%%  (%.2f%% of the 13F)%s" % (
            w["ticker"], w["name"][:28], _round(w["weight"], PCT), w["pct_of_13f"],
            "  check ticker" if w.get("check_ticker") else ""))
    for s in skipped:
        if s["why"].startswith("true weight "):                        # change 9: left out, by name
            out.append("  left out: %s (%s), %s" % (s.get("ticker") or "?", s["issuer"], s["why"]))
    out.append("Holdings as of %s, filed %s. For research and hypothetical planning only: TraderOS sizes "
               "nothing from these weights and places no order." % (meta.get("period"), meta.get("filed")))
    return "\n".join(out) + "\n"


def _method(top, combined):
    return "%s stock positions by value%s, pct_of_13f scaled to 100" % (
        "top %d" % top if top else "all", ", share classes combined" if combined else "")


# ---------------------------------------------------------------- the flow

def _table(client, cik, filing):
    """(rows, cover, info-table url, fetched) of one filing."""
    index, _ = client.filing_index(cik10(cik), filing["accession"])
    table = info_table_name(index)
    mult = value_multiplier(filing["filed"])
    data, fetched, url = client.filing_file(cik10(cik), filing["accession"], table)
    rows = parse_info_table(data, multiplier=mult)
    try:
        cover = parse_cover(client.filing_file(cik10(cik), filing["accession"], "primary_doc.xml")[0])
    except DataError:
        cover = {}
    total = Decimal(cover["tableValueTotal"].replace(",", "")) * mult if cover.get("tableValueTotal") else None
    return rows, cover, total, url, fetched


def load_quarter(client, cik, period=None, accession=None, before=None, as_of=None):
    """Download and parse one quarter, its amendments applied. Returns (meta, rows, filings)."""
    sub, _ = client.submissions(cik10(cik))
    filings = list_filings(sub, as_of=as_of)
    if not filings:
        raise EdgarError("%s (CIK %s) has no 13F-HR filings in its recent list%s. Is it a "
                         "13F filer? Try: traderos holdings --find NAME --sec"
                         % (sub.get("name"), cik10(cik), " filed by %s" % as_of if as_of else ""))
    chosen, amendments = pick_filing(filings, period=period, accession=accession, before=before)
    rows, _cover, total, url, fetched = _table(client, cik, chosen)
    meta = {"filer": sub.get("name"), "cik": cik10(cik), "period": chosen["period"],
            "filed": chosen["filed"], "original_filed": chosen["filed"], "accession": chosen["accession"],
            "form": chosen["form"],
            "info_table_url": url, "retrieved_at": fetched, "notes": [], "applied": [],
            "thousands": value_multiplier(chosen["filed"]) == 1000}
    if total is not None:
        meta["cover_total"] = total
    if accession or chosen["form"] != "13F-HR":
        amendments = []                                # a filing asked for by accession is read as filed
    for a in sorted(amendments, key=lambda f: (f["filed"], f["accession"])):
        try:
            a_rows, a_cover, a_total, _, _ = _table(client, cik, a)
        except (DataError, ValidationError) as exc:                    # one bad amendment never blocks the quarter
            meta["notes"].append("Amendment %s filed %s was not applied: %s" % (a["accession"], a["filed"],
                                                                               exc.message))
            continue
        kind = a_cover.get("amendmentType", "").upper()
        if kind == "RESTATEMENT":
            rows = a_rows
            if a_total is not None:
                meta["cover_total"] = a_total
            what = "restates the whole table"
        elif kind == "NEW HOLDINGS":
            rows = rows + a_rows
            if a_total is not None and meta.get("cover_total") is not None:
                meta["cover_total"] = meta["cover_total"] + a_total
            what = "adds holdings"
        else:
            meta["notes"].append("Amendment %s filed %s does not say whether it restates or adds holdings; "
                                 "not applied. Read it with --accession." % (a["accession"], a["filed"]))
            continue
        meta["applied"].append(a["accession"])
        meta["filed"] = max(meta["filed"], a["filed"])                 # change 6: public only once amended (QT01)
        meta["thousands"] = meta["thousands"] or value_multiplier(a["filed"]) == 1000
        meta["notes"].append("Amendment %s filed %s %s; applied." % (a["accession"], a["filed"], what))
    return meta, rows, filings


def build(meta, rows, index=None, overrides=None, combine=False):
    positions, total = aggregate(rows)
    resolve_tickers(positions, index, overrides)
    if meta.get("cover_total") is not None:
        gap = abs(meta["cover_total"] - total)
        meta["total_check"] = ("Matches the cover page total." if gap < 1000 else
                               "Cover page says %s; the rows add up to %s." % (
                                   _money(meta["cover_total"]), _money(total)))
    if combine:
        positions = combine_classes(positions, total)
    return positions, total


def ticker_index(client):
    return TickerIndex(load_company_tickers(client.tickers()[0]))


def resolve_cik(client, filer):
    """A filer given as a CIK (digits, or CIK0001067983) or as a ticker."""
    key = str(filer).strip().upper().removeprefix("CIK")
    return cik10(key) if key.isdigit() else client.cik_for(filer)[0]


# ---------------------------------------------------------------- records (change 7)

def _limits(meta):
    out = [Limitation(code="REPORTED_WITH_DELAY", detail=f"Holdings as of {meta['period']}, made public "
                      f"{meta['filed']}. {CAVEATS[0]}"),
           Limitation(code="LONG_ONLY_DISCLOSURE", detail=CAVEATS[1]),
           Limitation(code="FILED_BY_THE_FIRM", detail=CAVEATS[2]),
           Limitation(code="TICKERS_TODAY", detail="Tickers come from the SEC's current company list and your CUSIP "
                      "map, not the list at the quarter end: a company renamed, merged or delisted since may show "
                      "today's ticker, another company's, or none.")]
    if meta.get("thousands"):
        out.append(Limitation(code="VALUE_UNITS", detail="Filed before 2023-01-03 in thousands of dollars; "
                              "converted to dollars."))
    if meta.get("applied") or meta.get("form", "").endswith("/A"):
        out.append(Limitation(code="AMENDED", detail="An amendment (13F-HR/A) is part of these holdings."))
    if meta.get("total_check", "").startswith("Cover page says"):
        out.append(Limitation(code="COVER_TOTAL_MISMATCH", detail=meta["total_check"]))
    return out


def _holding(p):
    return Holding(issuer=p["issuer"], title_of_class=p["title"], cusip=p["cusip"], value_usd=p["value"],
                   shares=p["shares"], share_type=p["share_type"], put_call=p["put_call"] or None,
                   name=p.get("name"), ticker=p.get("ticker"), ticker_source=p.get("ticker_source"),
                   ticker_check=bool(p.get("ticker_check")), issuer_cik=p.get("issuer_cik"),
                   pct_of_13f=p["pct_of_13f"], cusips=tuple(p.get("cusips", [p["cusip"]])),
                   tickers=tuple(p.get("tickers", ())), classes=tuple(p.get("classes", ())), rows=p.get("rows"))


def holdings_report(meta, positions, total, *, combined, options=()) -> HoldingsReport:
    limits = _limits(meta)
    if any(p.get("ticker_check") for p in positions):
        limits.append(Limitation(code="TICKER_GUESS", detail="Some tickers were matched by name or share class "
                                 "guess (ticker_check): confirm them, or fix them in your CUSIP map."))
    key = [meta["cik"], meta["accession"], meta["applied"], combined, list(options)]
    return with_hash(HoldingsReport(
        report_id="HR-" + sha256_bytes(canonical_bytes(key))[:20], filer_cik=meta["cik"],
        filer_name=str(meta.get("filer") or ""), accession=meta["accession"], form=meta["form"],
        period_of_report=date.fromisoformat(meta["period"]), filed=date.fromisoformat(meta["filed"]),
        holdings=tuple(_holding(p) for p in positions), total_value_usd=total,
        source_url=meta["info_table_url"], retrieved_at=meta["retrieved_at"], limitations=tuple(limits),
        safe_for=(SafeFor.RESEARCH,), combined=combined, cover_total_usd=meta.get("cover_total"),
        amendments_applied=tuple(meta["applied"]), notes=tuple(meta["notes"])))


def diff_record(meta, pmeta, changes, *, options=()) -> HoldingsDiff:
    key = [meta["cik"], meta["accession"], meta["applied"], pmeta["accession"], pmeta["applied"], list(options)]
    limits = [Limitation(code="SHARE_COUNTS_NOT_TRADES", detail="Share counts at two quarter ends. Trades inside "
                         "the quarter, and their prices, are not in a 13F."), *_limits(meta)[1:4]]
    return with_hash(HoldingsDiff(
        diff_id="HD-" + sha256_bytes(canonical_bytes(key))[:20], filer_cik=meta["cik"],
        filer_name=str(meta.get("filer") or ""), accession=meta["accession"],
        period_of_report=date.fromisoformat(meta["period"]), filed=date.fromisoformat(meta["filed"]),
        prior_accession=pmeta["accession"], prior_period_of_report=date.fromisoformat(pmeta["period"]),
        prior_filed=date.fromisoformat(pmeta["filed"]),
        changes=tuple(HoldingChange(status=c["status"], issuer=c["issuer"], title_of_class=c["title"],
                                    cusip=c["cusip"], ticker=c.get("ticker"), put_call=c["put_call"] or None,
                                    shares_before=c["shares_before"], shares_after=c["shares_after"],
                                    change=c["change"], change_pct=c["change_pct"],
                                    value_before=c["value_before"], value_after=c["value_after"])
                      for c in changes),
        source_url=meta["info_table_url"], prior_source_url=pmeta["info_table_url"],
        retrieved_at=max(meta["retrieved_at"], pmeta["retrieved_at"]), limitations=tuple(limits),
        safe_for=(SafeFor.RESEARCH,)))


def copy_record(meta, weights, skipped, *, top, combined, options=()) -> CopyTargets:
    key = [meta["cik"], meta["accession"], meta["applied"], top, combined, list(options)]
    limits = [*_limits(meta), Limitation(code="HYPOTHETICAL", detail="Hypothetical weights for research and "
                                         "planning. TraderOS sizes nothing from them and places no order.")]
    return with_hash(CopyTargets(
        targets_id="CT-" + sha256_bytes(canonical_bytes(key))[:20], filer_cik=meta["cik"],
        filer_name=str(meta.get("filer") or ""), accession=meta["accession"],
        period_of_report=date.fromisoformat(meta["period"]), filed=date.fromisoformat(meta["filed"]),
        method=_method(top, combined),
        weights=tuple(TargetWeight(ticker=w["ticker"], name=w["name"], weight=w["weight"],
                                   pct_of_13f=w["pct_of_13f"], value_usd=w["value"], shares=w["shares"],
                                   cusips=tuple(w["cusips"]), classes=tuple(w.get("classes", ())),
                                   check_ticker=bool(w.get("check_ticker"))) for w in weights),
        skipped=tuple(SkippedPosition(issuer=s["issuer"], ticker=s["ticker"], why=s["why"]) for s in skipped),
        caveats=tuple(CAVEATS), source_url=meta["info_table_url"], retrieved_at=meta["retrieved_at"],
        limitations=tuple(limits), safe_for=(SafeFor.RESEARCH, SafeFor.HYPOTHETICAL_PLANNING)))


def as_of_today(as_of):
    return as_of or datetime.now(UTC).date()


__all__ = ["CAVEATS", "CLASSES", "FILERS", "EdgarError", "TickerIndex", "aggregate", "build", "cik10",
           "combine_classes", "copy_record", "copy_weights", "diff", "diff_record", "find_bundled",
           "holdings_report", "info_table_name", "list_filings", "load_company_tickers", "load_filers",
           "load_overrides", "load_quarter", "name_keys", "norm_ticker", "parse_cover", "parse_info_table",
           "pick_filing", "render_copy", "render_diff", "render_holdings", "resolve_cik", "resolve_tickers",
           "search_sec", "ticker_index", "value_multiplier"]
