"""The person's risk policy: the upstream config keys, stated by the person, never defaulted (ADR-009).

Provenance: the keys and their meaning are the upstream `trader-budget/config.example.json` and the DEFAULTS tables of
`trader-budget/size.py` and `trader-risk/check.py` (tank/day-trader-skill at 3d0f4ef, MIT; see
the NOTICE file), with upstream PR #2 applied (a limit of 0, missing or junk is a refusal, never
"no limit", for the four limits sizing needs).

What changed, each with its reason:

* **No DEFAULTS.** Upstream filled every missing key from DEFAULTS (a $50 max loss, a $150 day, 3 trades...). Flaw: a
  policy `{"budget": 5000}` was sized by a $50 max loss the person never set (spec 15.6: risk thresholds are the
  customer's policy, never ours; 05.3: unknown data never defaults to permission). Every limit the checks read must
  be stated; a missing one is MISSING_DATA, naming every missing key at once.
* **Each value is checked once, here, as a finite Decimal** (spec 04.3). Upstream read them with float() at each use,
  so "NaN" disabled a check (`NaN > x` is False) and "three" became a traceback at the first comparison.
* **`paper` may only be true, and `broker` only "alpaca"** (owner ruling 2026-11-20, ADR-012: paper accounts only,
  live refused in code). The upstream `"paper": false` meant a live account and `"broker": "ibkr"` the Interactive
  Brokers connector; here either is NOT_AUTHORIZED: live trading is refused in this build, and TraderOS cannot prove
  an IBKR connector routes to a paper account. Upstream defaulted `paper` to true; here a policy that does not state
  `"paper": true` can still be assessed, but `traderos ticket place` refuses it (spec 05.3: unknown is never
  permission). Each key is in the checked policy only when stated, so a policy without them keeps its hash.
  `sec_user_agent` belongs to the SEC adapter, not to a risk policy, and is still refused.
* **`require_stop` may only be true.** An entry without a stop has no bounded loss: it is NO_PROTECTION whatever the
  setting (verdict S16 item 7; the upstream `false` let a stopless entry pass and left it out of the daily loss).

Kept as upstream wrote them: the key names; 0 or null switches off `max_daily_loss_usd`, `max_trades_per_day`,
`max_orders_per_day`, `min_reward_risk` and `min_order_usd`, and null switches off `no_entries_before` /
`no_entries_after` (an explicit choice by the person, not a default); `allow_options`, `allow_margin` and
`allow_short` are false unless the person says true (a default that refuses, never one that permits).
"""
from __future__ import annotations

import functools
import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from traderos.contracts import AuthorizationError, DataError, ErrorCode, ValidationError

POSITIVE = ("budget", "max_loss_per_trade_usd", "max_position_pct", "max_order_usd")   # PR #2: never 0 or junk
AT_LEAST_ZERO = ("cash_buffer_pct", "min_order_usd")
OFF_WHEN_ZERO = ("max_daily_loss_usd", "min_reward_risk")       # Upstream: 0 or null switches the check off
COUNTS = ("max_trades_per_day", "max_orders_per_day")            # Upstream: 0 or null switches the check off
TIMES = ("no_entries_before", "no_entries_after")                # New York "HH:MM"; null switches it off
FLAGS = ("pdt_guard",)
REQUIRED = POSITIVE + AT_LEAST_ZERO + OFF_WHEN_ZERO + COUNTS + TIMES + FLAGS + ("blocked",)
OPTIONAL = {"allow_options": False, "allow_margin": False, "allow_short": False, "require_stop": True,
            "flat_by": None, "fractional": False}
NOT_IN_F1 = {"sec_user_agent": "it configures the SEC adapter, not a risk policy"}
BROKER_KEYS = ("paper", "broker")                 # ADR-012: kept only when stated; "paper" only true, "broker" alpaca
LIVE_REFUSED = "live trading is refused in this build"
IBKR_REFUSED = ("TraderOS cannot prove that an Interactive Brokers connector routes to a paper account, so the "
                "IBKR branch is refused")
MAX = Decimal(10) ** 15                           # no amount, price or count in a plan is this large
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
EXAMPLE = {                                       # The upstream config.example.json, the broker keys taken out
    "budget": "5000", "cash_buffer_pct": "10", "max_position_pct": "75", "max_order_usd": "4000",
    "min_order_usd": "5", "max_loss_per_trade_usd": "50", "max_daily_loss_usd": "150", "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": "1.0", "require_stop": True, "no_entries_before": "09:45",
    "no_entries_after": "15:30", "flat_by": "15:55", "fractional": False, "blocked": [], "allow_options": False,
    "allow_margin": False, "allow_short": False, "pdt_guard": False,
}
PAPER_EXAMPLE = {**EXAMPLE, "broker": "alpaca", "paper": True}   # The upstream example with its broker keys (ADR-012)


def exact(fn):
    """Run fn with enough Decimal digits that no product of bounded inputs (each under MAX) is ever rounded or
    overflows a quantize (the default context keeps 28 digits)."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with localcontext() as ctx:
            ctx.prec = 100
            return fn(*args, **kwargs)
    return run


def number(x: Any) -> Decimal | None:
    """A finite Decimal under 10**15 in size, or None. The upstream `_f` (float or None) with the flaws closed: a bool
    is not a number, NaN or infinity is not a number either (they turned comparisons into silent passes), and a number
    too large to be an amount of money (the upstream float overflowed on 10**400) is not one. A float from a caller is
    taken by its shortest repr, the digits that were written; a file never gets that far (no float literals)."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        if isinstance(x, float):
            d = Decimal(repr(x))
        elif isinstance(x, (int, Decimal)):
            d = Decimal(x)
        elif isinstance(x, str):
            d = Decimal(x.strip())
        else:
            return None
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and abs(d) < MAX else None


def positive_number(key: str, value: Any) -> Decimal:
    """A limit that must be a positive number. 0, negative, missing, not a number, NaN or infinity is a refusal,
    never "no limit" (PR #2, and the review's change 6: NaN and infinity too)."""
    d = number(value)
    if d is None or d <= 0:
        raise ValidationError(f"{key} must be a positive finite number, got {value!r}")
    return d


def policy(cfg: Any) -> dict[str, Any]:
    """The checked policy: every required key present, every value typed. Raises MISSING_DATA or INVALID_INPUT."""
    if not isinstance(cfg, dict):
        raise ValidationError("a policy is a JSON object of your limits")
    refused = sorted(k for k in cfg if k in NOT_IN_F1)
    if refused:
        raise ValidationError("not a risk policy key: " + "; ".join(f"{k} ({NOT_IN_F1[k]})" for k in refused))
    unknown = sorted(set(cfg) - set(REQUIRED) - set(OPTIONAL) - set(BROKER_KEYS))
    if unknown:
        raise ValidationError(f"unknown policy key(s): {', '.join(unknown)}; the keys are: "
                              f"{', '.join(REQUIRED + tuple(OPTIONAL) + BROKER_KEYS)}")
    stated = broker_keys(cfg)                     # live and IBKR are refused before anything else is read
    if cfg.get("budget") is None:
        raise DataError("no budget: set \"budget\" in your policy file (TraderOS has no default)",
                        code=ErrorCode.MISSING_DATA)
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise DataError(f"your policy does not state {', '.join(missing)}. TraderOS has no default for a risk limit: "
                        "write each one, or 0 / null where that check may be off", code=ErrorCode.MISSING_DATA)
    out: dict[str, Any] = dict(OPTIONAL)
    for k in POSITIVE:
        out[k] = positive_number(k, cfg[k])
    for k in AT_LEAST_ZERO + OFF_WHEN_ZERO:
        v = cfg[k]
        if v is None and k in OFF_WHEN_ZERO:
            out[k] = None
            continue
        d = number(v)
        if d is None or d < 0:
            raise ValidationError(f"{k} must be a finite number of at least 0, got {v!r}")
        out[k] = d
    if out["cash_buffer_pct"] >= 100:
        raise ValidationError("cash_buffer_pct must be between 0 and 100")
    for k in COUNTS:
        v = cfg[k]
        if v is None:
            out[k] = None
            continue
        d = number(v)
        if d is None or d < 0 or d != d.to_integral_value():
            raise ValidationError(f"{k} must be a whole number of at least 0, got {v!r}")
        out[k] = int(d)
    for k in (*TIMES, "flat_by"):
        v = cfg.get(k)
        if v is not None and (not isinstance(v, str) or not HHMM.match(v.strip())):
            raise ValidationError(f"{k} must be a New York time like \"09:45\", got {v!r}")
        out[k] = v.strip() if isinstance(v, str) else None
    for k in (*FLAGS, "allow_options", "allow_margin", "allow_short", "require_stop", "fractional"):
        if k in cfg:
            if not isinstance(cfg[k], bool):
                raise ValidationError(f"{k} must be true or false, got {cfg[k]!r}")
            out[k] = cfg[k]
    if out["require_stop"] is not True:
        raise ValidationError("require_stop may only be true: an entry without a stop has no bounded loss "
                              "(NO_PROTECTION), whatever the policy says")
    out.update(stated)
    blocked = cfg["blocked"]
    if not isinstance(blocked, list) or not all(isinstance(b, str) for b in blocked):
        raise ValidationError(f"blocked must be a list of tickers, got {blocked!r}")
    out["blocked"] = [b.strip().upper() for b in blocked]
    return out


def broker_keys(cfg: dict[str, Any]) -> dict[str, Any]:
    """`paper` and `broker` as stated, or AuthorizationError: "paper" must be exactly true (not "true", not 1) and
    "broker" "alpaca". Absent keys stay absent (`traderos ticket place` then refuses: see require_paper)."""
    out: dict[str, Any] = {}
    if "paper" in cfg:
        if cfg["paper"] is not True:
            raise AuthorizationError(f"{LIVE_REFUSED}: \"paper\" must be true, got {cfg['paper']!r}",
                                     code=ErrorCode.NOT_AUTHORIZED)
        out["paper"] = True
    if "broker" in cfg:
        broker = cfg["broker"]
        if isinstance(broker, str) and broker.strip().lower() == "ibkr":
            raise AuthorizationError(IBKR_REFUSED, code=ErrorCode.NOT_AUTHORIZED)
        if broker != "alpaca":
            raise ValidationError(f"broker must be \"alpaca\" (the only paper broker TraderOS sends to), got "
                                  f"{broker!r}")
        out["broker"] = "alpaca"
    return out


def require_paper(checked: dict[str, Any]) -> dict[str, Any]:
    """The gate `traderos ticket place` applies to a checked policy: it must say "paper": true (stated, not assumed)
    and name no broker but Alpaca. Anything else is NOT_AUTHORIZED: live trading is refused in this build."""
    if checked.get("paper") is not True:
        raise AuthorizationError(f"{LIVE_REFUSED}: your policy must say \"paper\": true before TraderOS sends a "
                                 "ticket to Alpaca's paper endpoint (it is never assumed)",
                                 code=ErrorCode.NOT_AUTHORIZED)
    if checked.get("broker", "alpaca") != "alpaca":
        raise AuthorizationError(IBKR_REFUSED if str(checked.get("broker")).lower() == "ibkr" else
                                 f"{LIVE_REFUSED}: the only broker is Alpaca's paper endpoint",
                                 code=ErrorCode.NOT_AUTHORIZED)
    return checked


__all__ = ["BROKER_KEYS", "EXAMPLE", "MAX", "OPTIONAL", "PAPER_EXAMPLE", "REQUIRED", "broker_keys", "exact", "number",
           "policy", "positive_number", "require_paper"]
