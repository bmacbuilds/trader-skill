"""SEC-backed commands: `earnings`, `holdings`, `fundamentals screen` (S04, S06, S07).

These are the only commands that use the network, and only when run. They need the person's own SEC identification
(`traderos config set sec_user_agent "Name email"`), which is sent only to sec.gov. The work is the upstream SEC tools,
ported (traderos.fundamentals.facts, .edgar13f, .screen; ADR-011); its command lines become these:

    traderos earnings AAPL KO [--quarters 8] [--as-of D] [--no-filings]       facts.py AAPL KO --quarters 8
    traderos holdings --find buffett [--sec]                                    edgar13f.py find buffett [--sec]
    traderos holdings 1067983 --filings                                         edgar13f.py filings 1067983
    traderos holdings 1067983 [--combine] [--top 15]                            edgar13f.py holdings ...
    traderos holdings 1067983 --diff                                            edgar13f.py diff 1067983
    traderos holdings 1067983 --copy --top 5                                    edgar13f.py copy ... (hypothetical)
    traderos fundamentals screen --tickers A,B --criteria RULES [--prices P]    screen.py ... --rules RULES
    traderos fundamentals screen --metrics FILE --criteria RULES                screen.py FILE --rules RULES
"""
from __future__ import annotations

import hashlib
import pathlib
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from traderos.capabilities import Capabilities
from traderos.cli import Output
from traderos.contracts import CapabilityId, DataError, ErrorCode, ValidationError
from traderos.store import Store

MAX_QUARTERS = 40
MAX_TICKERS = 50


def register(sub) -> None:
    p = sub.add_parser("earnings", help="quarterly results from SEC filings: revenue, margins, net income, EPS")
    p.add_argument("tickers", nargs="*", help="tickers, e.g. AAPL KO (at most 50)")
    p.add_argument("--cik", help="use this CIK instead of a ticker lookup (one company)")
    p.add_argument("--as-of", help="only filings public by this day (YYYY-MM-DD)")
    p.add_argument("--quarters", type=int, default=6, help="quarters to show (default 6)")
    p.add_argument("--no-filings", action="store_true", help="skip the 8-K and 10-Q links")
    p.add_argument("--metrics", metavar="FILE", help="merge a row per ticker into this JSON (the screener reads it)")
    p.set_defaults(run=earnings)
    p = sub.add_parser("holdings", help="a manager's 13F holdings, as reported; --diff, --copy (hypothetical)")
    p.add_argument("filer", nargs="?", help="the filer's CIK (digits) or ticker")
    p.add_argument("--as-of", help="only filings public by this day (YYYY-MM-DD)")
    p.add_argument("--top", type=int, help="only the N biggest positions")
    p.add_argument("--combine", action="store_true", help="sum share classes of one company (Alphabet A + C)")
    p.add_argument("--period", help="quarter end, YYYY-MM-DD (default: the newest)")
    p.add_argument("--accession", help="a specific filing, read as filed, e.g. 0001193125-26-352200")
    p.add_argument("--map", help="your own CUSIP to ticker fixes, a JSON file {\"650111107\": \"NYT\"}")
    p.add_argument("--find", metavar="NAME", help="find a filer's CIK by name or person")
    p.add_argument("--sec", action="store_true", help="with --find: also search every EDGAR filer")
    p.add_argument("--filings", action="store_true", help="list the filer's 13F filings, newest first")
    p.add_argument("--limit", type=int, default=8, help="with --filings: how many (default 8)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--diff", action="store_true", help="what changed since the quarter before")
    mode.add_argument("--copy", action="store_true", help="hypothetical weights from the top positions")
    p.add_argument("--min-pct", default="0", help="with --copy: skip positions under this %% of the 13F")
    p.add_argument("--exclude", nargs="*", default=[], help="with --copy: tickers to leave out")
    p.add_argument("--separate", action="store_true", help="with --copy: keep share classes apart")
    p.set_defaults(run=holdings)
    p = sub.add_parser("fundamentals", help="fundamental screens")
    fs = p.add_subparsers(dest="fundamentals_command", required=True)
    x = fs.add_parser("screen", help="screen companies against your rules, with the reason for each")
    src = x.add_mutually_exclusive_group(required=True)
    src.add_argument("--tickers", help="comma-separated, at most 50: figures from their SEC filings")
    src.add_argument("--metrics", help="a metrics file instead: JSON ({\"companies\": [...]}) or CSV")
    x.add_argument("--criteria", "--rules", dest="criteria", required=True,
                   help='a rules file: {"rules": [{"metric", "op", "value"}], "rank": [...], "missing", "top"}')
    x.add_argument("--prices", help='your prices file {"as_of": "YYYY-MM-DD", "prices": {"AAPL": "341.27"}}')
    x.add_argument("--as-of", help="only filings public by this day (YYYY-MM-DD)")
    x.add_argument("--top", type=int, help="show only the N best that pass")
    x.set_defaults(run=screen)


def _store(args) -> Store:
    return Store(args.home).init()


def _as_of(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"--as-of {text!r} is not YYYY-MM-DD") from None


def _client(store: Store):
    from traderos.adapters import sec
    return sec.client(store.home)


def _put_all(store: Store, records) -> None:
    with store.transaction() as tx:
        for r in records:
            store.put(r, db=tx)


def earnings(args) -> Output:
    from traderos.fundamentals import facts
    Capabilities().require(CapabilityId.S04)
    store = _store(args)
    as_of = _as_of(args.as_of) or datetime.now(UTC).date()      # every input is checked before any request
    if not 1 <= args.quarters <= MAX_QUARTERS:
        raise ValidationError(f"--quarters is 1 to {MAX_QUARTERS}")
    tickers = [t.strip().upper() for t in args.tickers if t.strip()]
    if args.cik and tickers:
        raise ValidationError("give tickers or --cik, not both")
    if not tickers and not args.cik:
        raise ValidationError("give a ticker (e.g. AAPL), or --cik")
    if len(tickers) > MAX_TICKERS or len(set(tickers)) != len(tickers):
        raise ValidationError(f"give 1 to {MAX_TICKERS} different tickers")
    if args.cik and not (str(args.cik).isdigit() and len(str(args.cik)) <= 10):
        raise ValidationError(f"--cik {args.cik!r} is not a CIK (up to 10 digits)")
    client = _client(store)
    if args.cik:
        reports = [facts.fetch(client, cik=args.cik, as_of=as_of, quarters=args.quarters,
                               filings=not args.no_filings)]
    else:
        for t in tickers:
            client.cik_for(t)                                   # an unknown ticker stops the command, by name
        reports = [facts.fetch(client, t, as_of=as_of, quarters=args.quarters, filings=not args.no_filings)
                   for t in tickers]
    _put_all(store, reports)
    text = "\n\n".join(facts.describe(r) for r in reports)
    if args.metrics:
        from traderos.fundamentals import screen as scr
        n = scr.write_metrics(args.metrics, reports)
        text += f"\n\nwrote {args.metrics} ({n} companies)"
    return Output(reports[0] if len(reports) == 1 else reports, text)


def _map_digest(path) -> str:
    if not path:
        return ""
    p = pathlib.Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def holdings(args) -> Output:
    from traderos.fundamentals import edgar13f as e13
    Capabilities().require(CapabilityId.S06)
    store = _store(args)
    as_of = _as_of(args.as_of) or datetime.now(UTC).date()
    period = _as_of(args.period)
    try:
        min_pct = Decimal(str(args.min_pct))
        if not min_pct.is_finite() or min_pct < 0:
            raise InvalidOperation
    except InvalidOperation:
        raise ValidationError(f"--min-pct {args.min_pct!r} is a percent, 0 or more") from None
    if args.top is not None and args.top < 1:
        raise ValidationError("--top is 1 or more")
    if args.find:
        hits = [dict(f, source="bundled") for f in e13.find_bundled(args.find)]
        if args.sec:
            known = {h["cik"] for h in hits}
            hits += [dict(h, source="sec") for h in e13.search_sec(_client(store), args.find)
                     if h["cik"] not in known]
        lines = [h["cik"] + "  " + h["name"] + (f"  ({h['person']})" if h.get("person") else "")
                 + (f"  [{h['status']}]" if h.get("status") else "") for h in hits]
        text = "\n".join(lines) or (f"No filer matched {args.find!r}. Try --sec to search every EDGAR filer, or a "
                                    "shorter name.")
        return Output(hits, text)
    if not args.filer:
        raise ValidationError("give a filer's CIK or ticker (find one with: traderos holdings --find NAME)")
    client = _client(store)
    cik = e13.resolve_cik(client, args.filer)
    if args.filings:
        sub, _ = client.submissions(cik)
        rows = e13.list_filings(sub, as_of=as_of)[:max(1, min(args.limit, 200))]
        text = "\n".join([f"{sub.get('name')} (CIK {cik})"]
                         + [f"  {f['form']:<9} quarter {f['period']}  filed {f['filed']}  {f['accession']}"
                            for f in rows] + ([] if rows else ["  no 13F filings"]))
        return Output({"filer": sub.get("name"), "cik": cik, "as_of": as_of.isoformat(), "filings": rows}, text)
    overrides = e13.load_overrides([e13.CLASSES, args.map] if args.map else [e13.CLASSES])
    meta, rows, _ = e13.load_quarter(client, cik, period=period.isoformat() if period else None,
                                     accession=args.accession, as_of=as_of)
    try:
        tickers, tickers_at = client.tickers()
        index = e13.TickerIndex(e13.load_company_tickers(tickers))
    except DataError as exc:
        index, tickers_at = None, None
        meta["notes"].append(f"Tickers by name skipped: {exc.message}")
    options = [_map_digest(args.map), tickers_at, meta["retrieved_at"]]
    if args.diff:
        pmeta, prows, _ = e13.load_quarter(client, cik, before=meta["period"], as_of=as_of)
        meta["prior_period"] = pmeta["period"]
        cur, _ = e13.aggregate(rows)
        pri, _ = e13.aggregate(prows)
        e13.resolve_tickers(cur, index, overrides)
        e13.resolve_tickers(pri, index, overrides)
        changes = e13.diff(cur, pri)
        record = e13.diff_record(meta, pmeta, changes, options=[*options, pmeta["retrieved_at"]])
        text = e13.render_diff(meta, changes)
    elif args.copy:
        combine = not args.separate
        positions, _ = e13.build(meta, rows, index, overrides, combine=combine)
        weights, skipped = e13.copy_weights(positions, top=args.top, min_pct=min_pct, exclude=args.exclude)
        record = e13.copy_record(meta, weights, skipped, top=args.top, combined=combine,
                                 options=[*options, str(min_pct), sorted(e.upper() for e in args.exclude)])
        text = e13.render_copy(meta, weights, combine, args.top, skipped)
    else:
        positions, total = e13.build(meta, rows, index, overrides, combine=args.combine)
        record = e13.holdings_report(meta, positions, total, combined=args.combine, options=options)
        text = e13.render_holdings(meta, positions, total, top=args.top, combined=args.combine)
    _put_all(store, [record])
    return Output(record, text.rstrip("\n"))


def screen(args) -> Output:
    from traderos.fundamentals import facts
    from traderos.fundamentals import screen as scr
    Capabilities().require(CapabilityId.S07)
    store = _store(args)
    as_of = _as_of(args.as_of)
    spec = scr.load_rules(args.criteria)                       # refuse bad rules before any network call
    if isinstance(spec, dict) and set(spec) & {"min", "max"}:
        raise ValidationError('the {"min": ..., "max": ...} criteria of the pre-port build are retired (ADR-011); '
                              'write rules: {"rules": [{"metric": "net_margin", "op": ">=", "value": 15}]}')
    if args.top is not None and args.top < 1:
        raise ValidationError("--top is 1 or more")
    reports = []
    if args.metrics:
        companies = scr.load_metrics(args.metrics)
    else:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if not tickers or len(tickers) > MAX_TICKERS:
            raise ValidationError(f"give 1 to {MAX_TICKERS} tickers")
        if len(set(tickers)) != len(tickers):
            raise ValidationError("a ticker is listed twice")
        scr.check_units(spec, scr.UNITS)
        as_of = as_of or datetime.now(UTC).date()
        client = _client(store)
        unavailable = []
        for t in tickers:
            cik, entity = client.cik_for(t)                    # an unknown ticker stops the screen, by name
            try:
                reports.append(facts.fetch(client, t, as_of=as_of, quarters=8, filings=False))
            except DataError as e:                             # no XBRL facts, none by as_of, not USD: a row says so
                if e.code not in (ErrorCode.MISSING_DATA, ErrorCode.OUTSIDE_CAPABILITY):
                    raise
                unavailable.append((t, cik, entity, e.message))
        companies = scr.from_reports(reports, unavailable)
    prices_as_of = None
    if args.prices:
        when = as_of or getattr(companies, "as_of", None) or datetime.now(UTC).date()
        prices_as_of = scr.add_prices(companies, args.prices, as_of=when)
    result, text = scr.run(companies, spec, as_of=as_of, top=args.top, prices_as_of=prices_as_of,
                           from_filings=not args.metrics)
    _put_all(store, [*reports, result])
    return Output(result, text.rstrip("\n"))
