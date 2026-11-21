"""`traderos data import|fetch|check|list` (docs/cli-contract-v1.md, spec 06.3/06.4; fetch: ADR-014)."""
from __future__ import annotations

import argparse
import pathlib

from traderos.adapters import prices
from traderos.cli import Output
from traderos.contracts import Interval
from traderos.data import ImportOptions, describe, import_into, read_file
from traderos.store import Store


def register(sub) -> None:
    p = sub.add_parser("data", help="import, check and list datasets")
    dsub = p.add_subparsers(dest="data_command", required=True)
    imp = dsub.add_parser("import", help="import your own CSV after validation")
    imp.add_argument("path")
    imp.add_argument("--name")
    imp.add_argument("--interval", default="1d", choices=[i.value for i in Interval])
    imp.add_argument("--currency", default="USD")
    imp.add_argument("--rights-label", default="user-provided",
                     help="what you are allowed to do with this data, in your words (stored with the dataset)")
    imp.set_defaults(run=data_import)
    fet = dsub.add_parser("fetch", help="free, unofficial bars from Yahoo Finance's chart (Stooq the daily fallback); "
                                        "network")
    fet.add_argument("symbols", nargs="+", metavar="SYMBOL")
    fet.add_argument("--interval", default="1d", choices=list(prices.INTERVALS),
                     help="1d (default range 5y) or 5m (Yahoo keeps about 60 days; default 60d)")
    fet.add_argument("--range", dest="range", help="a Yahoo range: 5d, 60d, 6mo, 1y, 5y, 10y, ytd, max")
    fet.add_argument("--name", help="the dataset's name (default: the symbols, the source and the fetch date)")
    fet.set_defaults(run=data_fetch)
    chk = dsub.add_parser("check", help="rights, completeness, calendar and point-in-time limits")
    chk.add_argument("dataset_id")
    chk.set_defaults(run=data_check)
    lst = dsub.add_parser("list", help="your local datasets")
    lst.set_defaults(run=data_list)


def _store(args: argparse.Namespace) -> Store:
    return Store(args.home).init()


def data_import(args: argparse.Namespace) -> Output:
    path = pathlib.Path(args.path)
    raw = read_file(path)
    opts = ImportOptions(name=args.name or path.name, interval=Interval(args.interval),
                         currency=args.currency.upper(), rights_label=args.rights_label)
    manifest = import_into(_store(args), raw, opts)
    return Output(manifest, "Imported.\n" + describe(manifest))


def data_fetch(args: argparse.Namespace) -> Output:
    manifest, got = prices.fetch_into(_store(args), args.symbols, interval=args.interval, rng=args.range,
                                      name=args.name)
    words = prices.UNOFFICIAL[got.source][1]
    head = [f"Fetched {', '.join(got.symbols)} ({got.interval.value}, range {got.range}) from {words} at "
            f"{got.fetched_at:%Y-%m-%d %H:%M} UTC. The data is free and unofficial: research only, never shared or "
            "exported."]
    if got.fallback_reason:
        head.append(f"Yahoo could not answer ({got.fallback_reason}), so Stooq, the daily fallback, did.")
    named = {"no_price": "without a price", "closing_print": "Yahoo closing-print stub(s) at 16:00",
             "before_calendar": "before the XNYS calendar's 2008 start", "earlier_duplicate": "earlier duplicate(s)"}
    if got.left_out:
        head.append("Left out: " + "; ".join(f"{n} {named.get(k, k)}" for k, n in sorted(got.left_out.items())) + ".")
    result = {"dataset_id": manifest.dataset_id, "source": got.source.value, "unofficial": True,
              "fetched_at": got.fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "symbols": list(got.symbols),
              "interval": got.interval.value,
              "range": got.range, "fallback_reason": got.fallback_reason, "left_out": dict(got.left_out),
              "limitations": [lim.code for lim in manifest.limitations], "dataset": manifest}
    return Output(result, "\n".join(head) + "\n" + describe(manifest))


def data_check(args: argparse.Namespace) -> Output:
    manifest = _store(args).bar_reader().manifest(args.dataset_id)
    return Output(manifest, describe(manifest))


def data_list(args: argparse.Namespace) -> Output:
    datasets = _store(args).datasets()
    if not datasets:
        return Output([], "No datasets yet. `traderos init` loads the tutorial; `traderos data import <csv>` loads "
                          "yours.")
    lines = [f"{m.dataset_id}  {m.name}  ({m.row_count} bars, {len(m.instruments)} instruments"
             + (", synthetic tutorial" if m.synthetic else "") + ")" for m in datasets]
    return Output(datasets, "\n".join(lines))
