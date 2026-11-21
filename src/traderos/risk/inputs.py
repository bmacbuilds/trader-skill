"""Strict readers for the files a person writes for `traderos risk assess`: the policy, the account statement and a
prices document (ADR-009). Each is JSON read as strictly as the plan file: at most 64 KB (prices 256 KB), UTF-8, no
float literals (write numbers as strings), no NaN or Infinity, no duplicate or unknown keys.

The shapes are the upstream (tank/day-trader-skill at 3d0f4ef): the policy is its config.json keys; the account
statement is its portfolio.json (positions with optional lots) plus its broker.json log ("orders"); the prices document
is its prices.json (prices, detail.time / detail.fetched, as_of). Three changes, each with its reason:
* the account states `settled_cash` and `reserved_cash` instead of `cash` (S15 / RC17: only settled cash less
  reservations may fund a buy; upstream's "cash" also held a sale's unsettled proceeds);
* the log here is what the person says they did themselves; TraderOS's own paper orders, results and fills are in
  the journal (ADR-012), and the risk check reads both. A record may name the paper ticket it is about
  (`"ticket": "PT-..."`): then it resolves that ticket's approval.
* optional `unsettled_cash` (ADR-012): where a recorded sale's proceeds go until they settle (T+1). The risk check
  never spends it, and counts it in the account's total (review of #10, item 7); move it into settled_cash when it
  settles.

The review of #10 (ADR-009) added: every number is bounded (`num`: under 10^15 in size, as policy.number bounds
the policy), so 1E+99 is refused here, typed, rather than crashing a later quantize or being dropped as "no price"
(items 4 and 5); a position may state its contract `multiplier` (item 2); an order's symbol and ticket id must be
strings (LOW c: a number was turned into one, and a list crashed). Its `at`, `plain` and `note` are upstream's, as it
read them (the first round's check on them refused an empty note, and had no reason to).
"""
from __future__ import annotations

import json
import pathlib
import re
from decimal import Decimal
from typing import Any

from traderos.contracts import DataError, ErrorCode, ValidationError
from traderos.contracts.canonical import parse_decimal, parse_utc
from traderos.risk.policy import MAX, policy

MAX_BYTES = 64 * 1024
MAX_PRICES_BYTES = 256 * 1024
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ACCOUNT_KEYS = {"settled_cash", "reserved_cash", "positions", "orders"}
ACCOUNT_OPTIONAL = {"as_of", "note", "unsettled_cash"}
POSITION_KEYS = {"symbol", "ticker", "shares", "qty", "lots", "name", "multiplier"}
LOT_KEYS = {"id", "date", "shares", "cost"}
ORDER_KEYS = {"symbol", "side", "status", "date", "at", "kind", "max_loss", "ticket", "qty", "price", "stop",
              "plain", "note"}
STATUSES = {"placed", "filled", "denied", "failed", "declined"}       # upstream's log, less "approved" (TraderOS's own)


def read_json(path: str, what: str, *, max_bytes: int = MAX_BYTES) -> Any:
    p = pathlib.Path(path)
    if not p.is_file():
        raise DataError(f"{p} is not a file ({what})", code=ErrorCode.MISSING_DATA)
    raw = p.read_bytes()
    if len(raw) > max_bytes:
        raise ValidationError(f"a {what} file is at most {max_bytes // 1024} KB",
                              code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)

    def no_dupes(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValidationError(f"{what}: duplicate key {k!r}")
            out[k] = v
        return out

    def no_float(s):
        raise ValidationError(f"{what}: {s} is a float; write numbers as strings, e.g. \"603.25\"")

    def no_constant(s):
        raise ValidationError(f"{what}: {s} is not allowed")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=no_dupes, parse_float=no_float,
                          parse_constant=no_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValidationError(f"the {what} is not valid JSON: {e}") from None


def num(raw: Any, field: str) -> Decimal:
    """A Decimal a person wrote, bounded: under 10**15 in size, the bound policy.number puts on every amount. A
    larger one (1E+99) is refused here, typed, instead of overflowing a later quantize into an untyped crash or being
    dropped as "no price" (review items 4 and 5)."""
    v = parse_decimal(raw, field=field)
    if abs(v) >= MAX:
        raise ValidationError(f"{field}: {raw!r} is out of range (under 10^15 in size)")
    return v


def text(raw: Any, field: str) -> str:
    """A string field: a number or a list where a symbol or an id belongs is refused, not turned into a string."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"{field} must be a non-empty string, got {raw!r}")
    return raw.strip()


def read_policy(path: str) -> dict[str, Any]:
    return policy(read_json(path, "policy"))


def _keys(obj: Any, allowed: set[str], where: str, required: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValidationError(f"{where} is a JSON object")
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ValidationError(f"{where}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}")
    missing = sorted(required - set(obj))
    if missing:
        raise DataError(f"{where}: missing {', '.join(missing)} (nothing about an account is assumed)",
                        code=ErrorCode.MISSING_DATA)
    return obj


def account(data: Any) -> dict[str, Any]:
    """A checked account statement: {"settled_cash", "reserved_cash", "positions", "orders"}, numbers as Decimal."""
    d = _keys(data, ACCOUNT_KEYS | ACCOUNT_OPTIONAL, "the account", ACCOUNT_KEYS)
    out: dict[str, Any] = {}
    if "unsettled_cash" in d:
        v = num(d["unsettled_cash"], "account.unsettled_cash")
        if v < 0:
            raise ValidationError(f"account.unsettled_cash must be at least 0, got {d['unsettled_cash']!r}")
        out["unsettled_cash"] = v
    for k in ("settled_cash", "reserved_cash"):
        v = num(d[k], f"account.{k}")
        if v < 0:
            raise ValidationError(f"account.{k} must be at least 0, got {d[k]!r}")
        out[k] = v
    if not isinstance(d["positions"], list) or not isinstance(d["orders"], list):
        raise ValidationError("account.positions and account.orders are lists ([] when there are none)")
    positions = []
    for i, p in enumerate(d["positions"]):
        p = _keys(p, POSITION_KEYS, f"account.positions[{i}]")
        sym = p.get("symbol") or p.get("ticker")
        if not isinstance(sym, str) or not sym.strip():
            raise ValidationError(f"account.positions[{i}] needs a symbol")
        pos: dict[str, Any] = {"symbol": sym.strip().upper()}
        for k in ("shares", "qty"):
            if k in p:
                pos["shares"] = num(p[k], f"account.positions[{i}].{k}")
        if p.get("multiplier") is not None:          # a holding's contract multiplier (review item 2); default 1
            m = num(p["multiplier"], f"account.positions[{i}].multiplier")
            if m <= 0:
                raise ValidationError(f"account.positions[{i}].multiplier must be positive, got {p['multiplier']!r}")
            pos["multiplier"] = m
        lots = p.get("lots", [])
        if not isinstance(lots, list):
            raise ValidationError(f"account.positions[{i}].lots is a list")
        pos["lots"] = []
        for j, lot in enumerate(lots):
            lot = _keys(lot, LOT_KEYS, f"account.positions[{i}].lots[{j}]", {"shares", "cost"})
            pos["lots"].append({"shares": num(lot["shares"], f"account.positions[{i}].lots[{j}].shares"),
                                "cost": num(lot["cost"], f"account.positions[{i}].lots[{j}].cost"),
                                **({"date": lot["date"]} if "date" in lot else {})})
        if "shares" not in pos and not pos["lots"]:
            raise DataError(f"account.positions[{i}] ({pos['symbol']}) states no shares", code=ErrorCode.MISSING_DATA)
        positions.append(pos)
    out["positions"] = positions
    orders = []
    for i, r in enumerate(d["orders"]):
        r = _keys(r, ORDER_KEYS, f"account.orders[{i}]", {"symbol", "side", "status", "date"})
        if r["side"] not in ("buy", "sell"):
            raise ValidationError(f"account.orders[{i}].side is buy or sell, got {r['side']!r}")
        if r["status"] not in STATUSES:
            raise ValidationError(f"account.orders[{i}].status is one of {', '.join(sorted(STATUSES))}, got "
                                  f"{r['status']!r}")
        if not isinstance(r["date"], str) or not DATE.match(r["date"]):
            raise ValidationError(f"account.orders[{i}].date is YYYY-MM-DD, got {r['date']!r}")
        if r.get("kind") not in (None, "entry", "exit"):
            raise ValidationError(f"account.orders[{i}].kind is entry or exit, got {r['kind']!r}")
        rec = dict(r)
        rec["symbol"] = text(r["symbol"], f"account.orders[{i}].symbol").upper()
        if r.get("ticket") is not None:
            rec["ticket"] = text(r["ticket"], f"account.orders[{i}].ticket")
        for k in ("max_loss", "qty", "price", "stop"):
            if r.get(k) is not None:
                rec[k] = num(r[k], f"account.orders[{i}].{k}")
        orders.append(rec)
    out["orders"] = orders
    return out


def read_account(path: str) -> dict[str, Any]:
    return account(read_json(path, "account statement"))


def prices(data: Any) -> dict[str, Any]:
    """A checked prices document: {"prices": {SYM: "price"}, "detail": {SYM: {"time"|"fetched": RFC 3339}}, "as_of"}.
    Every time must carry its timezone; a price with no time is kept but is never fresh."""
    d = _keys(data, {"prices", "detail", "as_of", "source"}, "the prices document", {"prices"})
    if not isinstance(d["prices"], dict):
        raise ValidationError("prices is an object of SYMBOL: \"price\"")
    out: dict[str, Any] = {"prices": {}, "detail": {}}
    for sym, p in d["prices"].items():
        v = num(p, f"prices.{sym}")
        if v <= 0:
            raise ValidationError(f"prices.{sym} must be positive, got {p!r}")
        out["prices"][str(sym).strip().upper()] = v
    detail = d.get("detail") or {}
    if not isinstance(detail, dict):
        raise ValidationError("detail is an object of SYMBOL: {\"time\": ...}")
    for sym, det in detail.items():
        det = _keys(det, {"time", "fetched", "source", "prev_close"}, f"detail.{sym}")
        out["detail"][str(sym).strip().upper()] = {k: parse_utc(det[k], field=f"detail.{sym}.{k}").isoformat()
                                                   for k in ("time", "fetched") if det.get(k) is not None}
    if d.get("as_of") is not None:
        out["as_of"] = parse_utc(d["as_of"], field="as_of").isoformat()
    return out


def read_prices(path: str) -> dict[str, Any]:
    return prices(read_json(path, "prices", max_bytes=MAX_PRICES_BYTES))


__all__ = ["account", "prices", "read_account", "read_json", "read_policy", "read_prices"]
