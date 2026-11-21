"""Mutants of the ported SEC tools (ADR-011): facts.py (S04), edgar13f.py (S06), screen.py (S07) and the SEC adapter.
Each one edits one rule, deletes the bytecode, runs the SEC tests with bytecode off and restores the file; a mutant
the tests do not turn red has survived.

    python tools/mutants/sec.py [PYTHON] [-k TEXT]

Run it from the repository root, on a clean tree (it rewrites files under src/ and puts them back). PYTHON is the
interpreter with pytest (default: this one); -k runs only the mutants whose name contains TEXT. The first group is
the port's named changes and the upstream kept rules (the PR body's first list of 54 was not committed, so this one is
written again from ADR-011's tables); the rest are OSDev6's review of #11, one or more per fix, and the three
mutants that survived his run, now pinned.
"""
# ruff: noqa: E501  (the mutant table: each row is a name and two exact source texts, kept on their lines)
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

W = pathlib.Path(__file__).resolve().parents[2]
F, E, S = ("src/traderos/fundamentals/facts.py", "src/traderos/fundamentals/edgar13f.py",
           "src/traderos/fundamentals/screen.py")
A, C = "src/traderos/adapters/sec.py", "src/traderos/commands/fundamentals.py"

M = [
    # ---- the port: facts.py
    ("facts: a fact filed after as_of read", F, "    return as_of is None or filed <= as_of", "    return True"),
    ("facts: a fact filed on as_of not read", F, "    return as_of is None or filed <= as_of", "    return as_of is None or filed < as_of"),
    ("facts: another currency read as USD", F, '    return node.get("units", {}).get(unit_hint, []) if unit_hint else []',
     '    units = node.get("units", {})\n    return units.get(unit_hint) or next(iter(units.values()), [])'),
    ("facts: margin over a negative revenue", F, "    if part is None or whole is None or whole <= 0:", "    if part is None or not whole:"),
    ("facts: TTM on each series' own quarters", F, '            if all(e in s[key] for e in ttm_ends):\n                ttm[key] = sum((s[key][e]["value"] for e in ttm_ends), Decimal(0))',
     '            if len(s[key]) >= 4:\n                ttm[key] = sum((s[key][e]["value"] for e in sorted(s[key])[-4:]), Decimal(0))'),
    ("facts: a row dated by its revenue only", F, '            "filed": max(str(v["filed"]) for v in values.values()),',
     '            "filed": str((rev.get(end) or s["net_income"].get(end))["filed"]),'),
    ("facts: the upstream 80-100 day quarter widened", F, "        if 80 <= days <= 100:\n            q[end]", "        if 80 <= days <= 120:\n            q[end]"),
    ("facts: Q4 from two quarters", F, "            if len(inside) != 3:", "            if len(inside) < 2:"),
    ("facts: TTM over more than a year", F, "(_date(last4[0]) - _date(last4[-1])).days < 300:", "(_date(last4[0]) - _date(last4[-1])).days < 400:"),
    ("facts: a NaN value read", F, "        if _dec(f.get(\"val\")) is None:\n            continue", "        if False:\n            continue"),
    # ---- the port: edgar13f.py
    ("13f: filings after as_of read", E, "            if as_of is not None and date.fromisoformat(filed) > as_of:", "            if False:"),
    ("13f: dollars from 2023-01-04", E, "    return 1 if d >= DOLLARS_FROM else 1000", "    return 1 if d > DOLLARS_FROM else 1000"),
    ("13f: a missing value read as 0", E, '            value = Decimal(f["value"].replace(",", "")) * multiplier',
     '            value = Decimal(f.get("value", "0").replace(",", "")) * multiplier'),
    ("13f: a restatement only noted", E, '        if kind == "RESTATEMENT":\n            rows = a_rows', '        if kind == "RESTATEMENT":\n            pass'),
    ("13f: new holdings only noted", E, "            rows = rows + a_rows", "            pass"),
    ("13f: a filing asked by accession amended", E, '    if accession or chosen["form"] != "13F-HR":', '    if chosen["form"] != "13F-HR":'),
    ("13f: options copied", E, "        if not is_stock(p):\n            why = ", "        if False:\n            why = "),
    ("13f: --min-pct ignored", E, '        elif p["pct_of_13f"] < min_pct:', "        elif False:"),
    ("13f: the byte check for a DOCTYPE dropped", E, '    if re.search(rb"<!\\s*(DOCTYPE|ENTITY)", data, re.I) or _declares(data):', "    if _declares(data):"),
    # ---- the port: screen.py
    ("screen: inf is a number", S, "        if not d.is_finite():\n            raise ScreenError(\"%s is not a number\" % v)", "        if False:\n            raise ScreenError(\"%s is not a number\" % v)"),
    ("screen: true is a number", S, "    if isinstance(v, bool):\n        raise ScreenError", "    if False:\n        raise ScreenError"),
    ("screen: a missing metric fails the row", S, "                unknown = True                                         # change 2", "                failed = True"),
    ("screen: a column mixing units read", S, "        if len(stated) > 1 or (stated and None in hints):", "        if len(stated) > 1:"),
    ("screen: a rule in another unit read", S, "        if want and have and want != have:", "        if False:"),
    ("screen: a file dated after as_of read", S, "    if file_as_of and file_as_of > as_of:", "    if False:"),
    ("screen: prices dated after as_of read", S, "    if as_of is not None and stated is not None and stated > as_of:", "    if False:"),
    ("screen: STALE_PERIOD never flagged", S, "        if as_of and (as_of - end).days > STALE_DAYS:", "        if False:"),
    ("screen: a duplicate key read", S, "        if len(keys) != len(set(keys)):", "        if False:"),
    ("screen: no company cap", S, "    if len(raw) > MAX_COMPANIES:", "    if False:"),
    # ---- the port: the adapter
    ("adapter: any host", A, '    return parts.scheme == "https" and parts.hostname in HOSTS and not parts.username and not parts.port',
     '    return parts.scheme == "https" and not parts.username and not parts.port'),
    ("adapter: the cap only before decompression", A, "            if len(out) > MAX_BYTES or d.unconsumed_tail:", "            if False:"),
    ("adapter: the placeholder identification accepted", A, '    if not ua or "@" not in ua or len(ua) > 200 or is_placeholder(ua):', '    if not ua or "@" not in ua or len(ua) > 200:'),
    ("adapter: BRK-B is not BRK.B", A, '    return str(ticker).strip().upper().replace("-", ".").replace("/", ".")', "    return str(ticker).strip().upper()"),

    # ---- OSDev6's review of #11: H1, the screener's look-ahead
    ("h1 screen: a quarter ending after as_of used", S, '(("filed", "were filed"), ("as_of", "are as of"), ("period_end", "cover a quarter ending"))',
     '(("filed", "were filed"), ("as_of", "are as of"))'),
    ("h1 screen: a row's own as_of ignored", S, '(("filed", "were filed"), ("as_of", "are as of"), ("period_end", "cover a quarter ending"))',
     '(("filed", "were filed"), ("period_end", "cover a quarter ending"))'),
    ("h1 screen: a row filed after as_of used", S, '(("filed", "were filed"), ("as_of", "are as of"), ("period_end", "cover a quarter ending"))',
     '(("as_of", "are as of"), ("period_end", "cover a quarter ending"))'),
    ("h1 screen: a row on the day itself refused", S, "            if c.get(key) and c[key] > as_of:", "            if c.get(key) and c[key] >= as_of:"),
    ("h1 screen: undated figures guessed", S, '        if not (c.get("filed") or c.get("as_of") or file_as_of) and as_of < today:', "        if False:"),
    ("h1 screen: undated figures refused today", S, '        if not (c.get("filed") or c.get("as_of") or file_as_of) and as_of < today:',
     '        if not (c.get("filed") or c.get("as_of") or file_as_of) and as_of <= today:'),
    ("h1 screen: the file's as_of does not vouch", S, '        if not (c.get("filed") or c.get("as_of") or file_as_of) and as_of < today:',
     '        if not (c.get("filed") or c.get("as_of")) and as_of < today:'),
    ("h1 write_metrics: the file's as_of lowered", S, '    body = {"as_of": None if undated else max([*days, *([file_as_of] if file_as_of else [])], default=None),',
     '    body = {"as_of": None if undated else max(days, default=None),'),
    ("h1 write_metrics: old rows not dated by their file", S, '                if not r.get("as_of") and file_as_of:\n                    r["as_of"] = file_as_of',
     "                if False:\n                    pass"),
    ("h1 write_metrics: vouches for an undated row", S, '    body = {"as_of": None if undated else max(', '    body = {"as_of": max('),
    ("h1 metrics rows: no as_of", S, '           "as_of": report.as_of, "derived": [], "_reasons": {}}', '           "derived": [], "_reasons": {}}'),
    ("h1 prices: undated prices guessed", S, "    if as_of is not None and stated is None and as_of < (today or datetime.now(UTC).date()):", "    if False:"),
    ("h1 prices: undated prices refused today", S, "    if as_of is not None and stated is None and as_of < (today or datetime.now(UTC).date()):",
     "    if as_of is not None and stated is None and as_of <= (today or datetime.now(UTC).date()):"),
    # M3: one tiny position never fails the copy
    ("m3 copy: a zero weight kept", E, "        if weight <= 0:                                                # change 9", "        if False:                                                # change 9"),
    ("m3 copy: weights rounded in the record", E, '        weight = 100 * p["pct_of_13f"] / base                          # change 9',
     '        weight = _round(100 * p["pct_of_13f"] / base, PCT)                          # change 9'),
    ("m3 copy: the upstream scaling replaced by value shares", E, '        weight = 100 * p["pct_of_13f"] / base                          # change 9',
     '        weight = 100 * p["value"] / value                          # change 9'),
    ("m3 copy: the true weight is the table's", E, '            true = 100 * p["value"] / value if value else Decimal(0)', "            true = weight"),
    ("m3 copy: the left-out line not printed", E, '        if s["why"].startswith("true weight "):', "        if False:"),
    # M1: an amendment dates the holdings
    ("m1 13f: filed stays the original's", E, '        meta["filed"] = max(meta["filed"], a["filed"])                 # change 6', "        pass"),
    ("m1 13f: made public on the original's day", E, "detail=f\"Holdings as of {meta['period']}, made public \"\n                      f\"{meta['filed']}.",
     "detail=f\"Holdings as of {meta['period']}, made public \"\n                      f\"{meta['original_filed']}."),
    ("m1 13f: the holdings header names the amendment's day", E, '        meta.get("filer", "?"), meta.get("period", "?"), meta.get("original_filed", meta.get("filed", "?")),',
     '        meta.get("filer", "?"), meta.get("period", "?"), meta.get("filed", "?"),'),
    ("m1 13f: the copy header names the amendment's day", E, '        meta.get("filer"), meta.get("period"), meta.get("original_filed", meta.get("filed")), meta.get("accession"),',
     '        meta.get("filer"), meta.get("period"), meta.get("filed"), meta.get("accession"),'),
    # M2: the DOCTYPE refusal in any encoding
    ("m2 13f: the parser does not refuse a DOCTYPE", E, '    if re.search(rb"<!\\s*(DOCTYPE|ENTITY)", data, re.I) or _declares(data):', '    if re.search(rb"<!\\s*(DOCTYPE|ENTITY)", data, re.I):'),
    ("m2 13f: only an entity refused, not a DOCTYPE", E, "    p.StartDoctypeDeclHandler = p.EntityDeclHandler = p.UnparsedEntityDeclHandler = stop",
     "    p.EntityDeclHandler = p.UnparsedEntityDeclHandler = stop"),
    ("m2 13f: broken XML reported as a DOCTYPE", E, "    except xml.parsers.expat.ExpatError:\n        return False", "    except xml.parsers.expat.ExpatError:\n        return True"),
    # LOW 1-8
    ("low1 13f: a refused amendment blocks the quarter", E, "        except (DataError, ValidationError) as exc:", "        except DataError as exc:"),
    ("low2 screen: text in a numeric metric decides", S, "    if m in numeric and not isinstance(have, Decimal):", "    if False:"),
    ("low2 screen: the file's numeric columns ignored", S, '    numeric = set(UNITS) | set(getattr(companies, "units", None) or ())', "    numeric = set(UNITS)"),
    ("low3 holdings: the text capped at 20 rows", C, "        text = e13.render_holdings(meta, positions, total, top=args.top, combined=args.combine)",
     "        text = e13.render_holdings(meta, positions, total, top=min(args.top or 20, 200), combined=args.combine)"),
    ("low4 facts: the 8-K links its primary document", F, '"url": f.url, "exhibit": f.exhibit_url, "index": index}', '"url": f.url, "exhibit": f.exhibit_url, "index": f.url}'),
    ("low5 adapter: a redirect followed anywhere", A, "        if not sec_address(newurl):", "        if False:"),
    ("low5 adapter: urlopen", A, "        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:", "        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:"),
    ("low5 adapter: the opener without the handler", A, "_OPENER = urllib.request.build_opener(_SecRedirectsOnly)", "_OPENER = urllib.request.build_opener()"),
    ("low5 adapter: plain http", A, '    return parts.scheme == "https" and parts.hostname in HOSTS', "    return parts.hostname in HOSTS"),
    ("low7 facts: TICKERS_TODAY never said", F, "    if ticker and as_of < retrieved_at.date():", "    if False:"),
    ("low7 facts: TICKERS_TODAY with --cik", F, "    if ticker and as_of < retrieved_at.date():", "    if as_of < retrieved_at.date():"),
    ("low7 facts: TICKERS_TODAY for today", F, "    if ticker and as_of < retrieved_at.date():", "    if ticker and as_of <= retrieved_at.date():"),
    ("low7 13f: TICKERS_TODAY never said", E, '           Limitation(code="TICKERS_TODAY", detail="Tickers come', '           Limitation(code="TICKERS", detail="Tickers come'),
    ("low7 screen: TICKERS_TODAY never said", S, "    if from_filings and as_of < today:", "    if False:"),
    # named changes with a behaviour
    ("map: a number as a ticker", E, "                    if not isinstance(v, str):\n                        raise", "                    if False:\n                        raise"),
    ("13f: the 20,000-row cap back", E, '    if not rows:\n        raise EdgarError("no infoTable rows found.', '    if not rows or len(rows) > 20_000:\n        raise EdgarError("no infoTable rows found.'),
    # OSDev6's three survivors, pinned (edgar13f.py:142 and :145, facts.py:116 at 6375c1b)
    ("pin 13f: a 13F filed on the as_of day is not public", E, "            if as_of is not None and date.fromisoformat(filed) > as_of:",
     "            if as_of is not None and date.fromisoformat(filed) >= as_of:"),
    ("pin 13f: an unreadable filing date listed", E, "            continue                                   # an unreadable filing date is never known to be public",
     "            pass"),
    ("pin facts: an unreadable filing date read", F, "        return False                                   # no readable filing date: never known to be public",
     "        return True"),
]

TESTS = ["tests/unit/test_analyst.py", "tests/unit/test_copycat.py", "tests/unit/test_screener.py",
         "tests/unit/test_sec.py", "tests/unit/test_sec_review11.py"]


def main(argv: list[str]) -> int:
    args = list(argv)
    only = None
    if "-k" in args:
        i = args.index("-k")
        only = args[i + 1]
        del args[i:i + 2]
    py = args[0] if args else sys.executable
    chosen = [m for m in M if only is None or only in m[0]]
    env = dict(os.environ, PYTHONPATH=str(W / "src"), PYTHONDONTWRITEBYTECODE="1")
    red = alive = 0
    survivors = []
    for name, f, a, b in chosen:
        p = W / f
        orig = p.read_text(encoding="utf-8")
        if orig.count(a) != 1:
            print(f"  ?? {name}: {orig.count(a)} matches", flush=True)
            survivors.append(name + " (no match)")
            alive += 1
            continue
        p.write_text(orig.replace(a, b), encoding="utf-8")
        for cache in W.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        try:
            r = subprocess.run([py, "-B", "-m", "pytest", "-x", "-q", "-o", "addopts=", "-p", "no:cacheprovider",
                                *TESTS], cwd=W, env=env, capture_output=True, text=True, timeout=900)
        finally:
            p.write_text(orig, encoding="utf-8")
        if r.returncode:
            red += 1
        else:
            alive += 1
            survivors.append(name)
        print(f"  {'red  ' if r.returncode else 'ALIVE'} {name}", flush=True)
    for cache in W.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    print(f"{red} red, {alive} survived, of {len(chosen)}")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
