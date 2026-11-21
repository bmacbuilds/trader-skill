"""Mutants of the ported risk rules (ADR-009). Each one edits one rule, deletes the bytecode, runs the risk tests
with bytecode off and restores the file; a mutant the tests do not turn red has survived.

    python tools/mutants/risk.py [PYTHON] [-k TEXT]

Run it from the repository root, on a clean tree (it rewrites files under src/ and puts them back). PYTHON is the
interpreter with pytest (default: this one); -k runs only the mutants whose name contains TEXT. The first 76 are the
port's (ADR-009, "Evidence"); the rest are the review of #10 (OSDev6), one or more per fix, and the three mutants
that survived his run, now pinned; then his second round (s1 to s4).
"""
# ruff: noqa: E501  (the mutant table: each row is a name and two exact source texts, kept on their lines)
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

W = pathlib.Path(__file__).resolve().parents[2]
S, C, T, P, R, D = ("src/traderos/risk/size.py", "src/traderos/risk/check.py", "src/traderos/risk/tickets.py",
                    "src/traderos/risk/policy.py", "src/traderos/risk/__init__.py",
                    "src/traderos/commands/decisions.py")
N, A, K = "src/traderos/risk/inputs.py", "src/traderos/commands/paper.py", "src/traderos/contracts/risk.py"
G = "src/traderos/settings.py"

M = [
    # size.py: the upstream sizing
    # a ceiling, not -(-a // b): Decimal // truncates toward zero, so on Decimals that is the floor again (an
    # equivalent mutant; the port's list ran it on the upstream integer cents)
    ("size: shares round up", S, "    return int(usd // per_share)\n",
     "    return int((usd / per_share).to_integral_value(rounding=\"ROUND_CEILING\"))\n"),
    ("size: largest limit wins", S, 'qty = max(0, min(lim["shares"] for lim in limits))',
     'qty = max(0, max(lim["shares"] for lim in limits))'),
    ("size: a fresh higher price ignored", S, "    elif quote is not None and quote > entry_plan:",
     "    elif False:"),
    ("size: plan for another day", S, '    if today and str(plan["date"]) != today:', "    if False:"),
    ("size: undated plan", S, '    if not plan.get("date"):', "    if False:"),
    ("size: short sized", S, '    if side != "buy":', "    if False:"),
    ("size: blocked ignored", S, "    if sym in blocked:\n        raise SizeError", "    if False:\n        raise SizeError"),
    ("size: stop at the entry", S, "    if stop >= ref:", "    if stop > ref:"),
    ("size: target at the entry", S, "    if target <= ref:", "    if target < ref:"),
    ("size: reward:risk ignored", S, "    elif min_rr and reward < min_rr * risk:", "    elif False:"),
    ("size: min order ignored", S, '    elif cost < c["min_order_usd"] - Decimal("0.005"):', "    elif False:"),
    ("size: multiplier out of the max loss", S, '"shares": shares_for(max_loss, risk * mult)',
     '"shares": shares_for(max_loss, risk)'),
    ("size: stated cash ignored", S, '    if cash is not None and cash - cash_buffer < available - Decimal("0.005"):',
     "    if False:"),
    ("size: buffer ignored", S, "    available = budget - cash_buffer\n", "    available = budget\n"),
    # policy.py
    ("policy: NaN is a number", P, "    return d if d.is_finite() and abs(d) < MAX else None", "    return d"),
    ("policy: a bool is a number", P, "    if isinstance(x, bool) or x is None:", "    if x is None:"),
    ("policy: 0 is a limit", P, "    if d is None or d <= 0:\n        raise ValidationError(f\"{key} must",
     "    if d is None or d < 0:\n        raise ValidationError(f\"{key} must"),
    ("policy: a missing limit defaults", P, "    missing = [k for k in REQUIRED if k not in cfg]",
     "    missing = []\n    cfg = {**EXAMPLE, **cfg}"),
    ("policy: require_stop false allowed", P, '    if out["require_stop"] is not True:', "    if False:"),
    ("policy: broker keys accepted", P, "    refused = sorted(k for k in cfg if k in NOT_IN_F1)", "    refused = []"),
    ("policy: negative buffer", P, "        if d is None or d < 0:\n            raise ValidationError(f\"{k} must be a finite",
     "        if d is None:\n            raise ValidationError(f\"{k} must be a finite"),
    # check.py: the upstream rules and the named changes
    ("check: unresolved approval only warns", C,
     '        reasons.append("exposure: existing exposure unknown: the approval', '        (warns or []).append("x: the approval'),
    ("check: unknown loss only with a limit (#55 as is)", C, "            if unknown_losses:\n",
     "            if daily and unknown_losses:\n"),
    ("check: no account is a fresh account", C,
     '    if portfolio is None:\n        reasons.append(', '    if False:\n        reasons.append('),
    ("check: no log is nothing placed", C, '    if state is None:\n        reasons.append(',
     '    if False:\n        reasons.append('),
    ("check: stale price on a market entry only warns", C,
     '            if sym in prices:\n                later.append("price: the %s price',
     '            if sym in prices:\n                warns.append("price: the %s price'),
    ("check: untimed price is fresh", C, "        if t is None:\n            return None\n        age",
     "        if t is None:\n            return prices[sym]\n        age"),
    ("check: an hour is fresh", C, "-60 <= age <= FRESH_MINUTES * 60 else None", "-60 <= age <= 3600 else None"),
    ("check: #48 price through the stop", C, "                if stop and ((long and live <= stop + CENT)",
     "                if False and ((long and live <= stop + CENT)"),
    ("check: #48 price through the target", C, "                if target and ((long and live >= target - CENT)",
     "                if False and ((long and live >= target - CENT)"),
    ("check: max loss per trade", C, "                    if cap and max_loss > cap + CENT:", "                    if False:"),
    ("check: reward:risk", C, "                            if min_rr and rr < min_rr:", "                            if False:"),
    ("check: daily loss", C, "                if used + max_loss > daily + CENT:", "                if False:"),
    ("check: trades per day off by one", C, "            if cap_t and len(entries_today) + batch_entries >= cap_t:",
     "            if cap_t and len(entries_today) + batch_entries > cap_t:"),
    ("check: window not applied", C, "            later.extend(win_reasons)\n", ""),
    ("check: calendar unknown passes", C,
     '    except DataError:\n        return ["window: the exchange calendar', '    except DataError:\n        return [] or ["'),
    ("check: no early-close cutoff", C, "    if close == EARLY and after is not None:", "    if False:"),
    ("check: no stop only warns", C, '                later.append("stop: no stop.', '                warns.append("stop: no stop.'),
    ("check: a sale funds a buy (line 776)", C,
     '                    if value is not None:\n                        moved = min(',
     '                    if value is not None:\n                        acct["cash"] += value\n'
     '                        moved = min('),
    ("check: cash buffer", C, "                elif after_cash < buffer_usd - CENT:", "                elif False:"),
    ("check: margin", C, '                if after_cash < -CENT and not c.get("allow_margin"):', "                if False:"),
    ("check: position cap", C, "                if total > 0 and pos_after > limit + CENT:", "                if False:"),
    ("check: already placed today", C, "        if key in placed_keys:", "        if False:"),
    ("check: exits blocked by day limits", C, '        if kind == "entry":\n            reasons.extend(later)',
     '        if True:\n            reasons.extend(later)'),
    ("check: multiplier out of the value", C, "                value = q * ref * mult", "                value = q * ref"),
    ("check: verdict outlives the cutoff", C, "        entry_until = min(entry_until, cutoff_utc)", "        pass"),
    ("check: verdict lasts an hour", C, "    fresh_until = now_utc + datetime.timedelta(minutes=FRESH_MINUTES)",
     "    fresh_until = now_utc + datetime.timedelta(minutes=60)"),
    ("check: NaN order field passes", C, "                if v is None or v <= 0:\n                    reasons.append(\"format: %s",
     "                if v is not None and v <= 0:\n                    reasons.append(\"format: %s"),
    ("check: pdt", C, '            if c["pdt_guard"] and pdt_n >= 3:', "            if False:"),
    ("check: short selling", C, '            if over and not c.get("allow_short"):', "            if False:"),
    ("check: bracket whole shares", C, "            elif _f(qty) is not None and _f(qty) != _f(qty).to_integral_value():",
     "            elif False:"),
    ("check: blocked list", C, "        if sym in blocked:\n            later.append(", "        if False:\n            later.append("),
    ("check: an exit is an entry", C, '        kind = "exit" if exiting else "entry"', '        kind = "entry"'),
    ("check: fingerprint ignores the plan", C, '    for k in ("entry", "stop", "target"):\n        if plan.get(k) is not None:\n            d = _f',
     '    for k in ():\n        if plan.get(k) is not None:\n            d = _f'),
    # tickets.py: the document half
    ("ticket: no expiry is fine", T, "    if until is None:\n        return None, \"the risk check has no expiry",
     "    if until is None:\n        return assessment, None\n    if False:\n        return None, \"the risk check has no expiry"),
    ("ticket: an expired check is fine", T, "    if now > until:", "    if False:"),
    ("ticket: yes to all", T, "    if BATCH_WORDS & set(words):", "    if False:"),
    ("ticket: a doubt is a yes", T, "    if DOUBT_WORDS & set(words[1:]):", "    if False:"),
    ("ticket: any amount", T, '        if not any(abs(n - w) < Decimal("0.005") for n in nums for w in want):', "        if False:"),
    ("ticket: another ticket's symbol", T, "    if named:", "    if False:"),
    ("ticket: sure is a yes", T, "    if not words or words[0] not in YES_WORDS:", "    if not words:"),
    ("ticket: a superseded check is ready", T, "    if superseded_by:", "    if False:"),
    ("ticket: a decided ticket is ready", T, "    if decision:\n", "    if False:\n"),
    # the adapter and the CLI
    ("adapter: unknown exposure is a policy refusal", R, '    ("exposure:", ErrorCode.UNKNOWN_EXPOSURE),',
     '    ("exposure:", ErrorCode.NOT_AUTHORIZED),'),
    ("adapter: no stop is not NO_PROTECTION", R, '    ("stop: no stop", ErrorCode.NO_PROTECTION),', ""),
    ("adapter: an unknown reason permits", R, "    return ErrorCode.NOT_AUTHORIZED\n\n\n@dataclass",
     "    return ErrorCode.INVALID_INPUT\n\n\n@dataclass"),
    ("adapter: stress ignored", R, '        gapped = sizing["stop"] * (1 - stress_gap)', '        gapped = sizing["stop"]'),
    ("adapter: worst case without the multiplier", R, "        worst = _money(ref * m * qty, cur)",
     "        worst = _money(ref * qty, cur)"),
    ("adapter: a sizing refusal is eligible", R, '        if sizing["refusal"]:\n            details.append(',
     '        if False:\n            details.append('),
    ("adapter: exposure unchecked when sizing refused", R, "        details += [(code_of(r), r) for r in reasons]\n", ""),
    ("adapter: a stated outcome inherits nothing", R,
     "                if appr is not None and rec.get(k) is None and appr.get(k) is not None:", "                if False:"),
    ("adapter: a ticket for any plan", R, "    if assessment.plan_hash != record_hash(plan):", "    if False:"),
    ("cli: a failed attempt supersedes nothing", D, '        _remember(store, "journal.plan.failed"',
     '        _remember(store, "journal.note"'),
    ("cli: sign-offs never count", D, "approvals=_log(store) if account is not None else (),",
     "approvals=(),"),
    ("cli: the batch is this ticket only", D, "[*_open_symbols(store, _ny_date(now), t.ticket_id), t.symbol]",
     "[t.symbol]"),
    ("cli: a newer assessment supersedes nothing", D, "    if last is None or assessment_id in last.subject_refs:",
     "    if True:"),
    # ---- the review of #10 (OSDev6, comment 7045037626) -------------------------------------------------------
    # 1. never round an order price
    ("r1 size: the risk a share rounded to the cent", S, "    risk = ref - stop ",
     "    risk = (ref - stop).quantize(PENNY) "),
    ("r1 size: no tick check", S, "    if price % tick != 0:", "    if False:"),
    ("r1 size: four decimals at any price", S, "    return PENNY if price >= 1 else SUB_PENNY", "    return SUB_PENNY"),
    ("r1 size: whole cents at any price", S, "    return PENNY if price >= 1 else SUB_PENNY", "    return PENNY"),
    ("r1 size: the plan's tick_size ignored", S, "    if tick_size is not None:\n        return tick_size",
     "    if False:\n        return tick_size"),
    ("r1 size: the stop not on the tick", S, '("limit price", lim), ("stop", stop), ("target", target)',
     '("limit price", lim), ("target", target)'),
    ("r1 size: the target not on the tick", S, '("limit price", lim), ("stop", stop), ("target", target)',
     '("limit price", lim), ("stop", stop)'),
    ("r1 size: the limit not on the tick", S, '("limit price", lim), ("stop", stop), ("target", target)',
     '("stop", stop), ("target", target)'),
    ("r1 check: the entry rounded to the cent", C,
     "                ref, ref_from = max(cands, key=lambda x: x[0])\n",
     "                ref, ref_from = max(cands, key=lambda x: x[0])\n                ref = ref.quantize(PENNY)\n"),
    ("r1 check: the risk a share rounded to the cent", C, "                    risk_ps = abs(ref - stop) ",
     "                    risk_ps = abs(ref - stop).quantize(PENNY) "),
    ("r1 check: a price printed to the cent", C, '    return format(d.normalize(), "f")',
     '    return format(d.quantize(PENNY), "f")'),
    ("r1 ticket: the max loss on a cent risk", T, "    return (q * risk * mult).quantize(PENNY, rounding=ROUND_HALF_EVEN), stop",
     "    return (q * risk.quantize(PENNY) * mult).quantize(PENNY, rounding=ROUND_HALF_EVEN), stop"),
    ("r1 adapter: the ticket's stop rounded", R, "    stop = plan.stop_price ",
     "    stop = plan.stop_price.quantize(CENT) if plan.stop_price is not None else None "),
    # 2. a holding carries its multiplier
    ("r2 check: a holding valued without its multiplier", C, "values[t] = shares[t] * prices[t] * (mults[t] or 1)",
     "values[t] = shares[t] * prices[t]"),
    ("r2 check: no multiplier mismatch", C,
     "            if (mult != 1 and stated != mult) or (stated is not None and stated != mult):", "            if False:"),
    ("r2 check: an unstated holding multiplier is fine", C,
     "            if (mult != 1 and stated != mult) or (stated is not None and stated != mult):",
     "            if stated is not None and stated != mult:"),
    ("r2 check: a stated holding multiplier vs a plan at 1 is fine", C,
     "            if (mult != 1 and stated != mult) or (stated is not None and stated != mult):",
     "            if mult != 1 and stated != mult:"),
    ("r2 inputs: no position multiplier", N, 'POSITION_KEYS = {"symbol", "ticker", "shares", "qty", "lots", "name", '
     '"multiplier"}', 'POSITION_KEYS = {"symbol", "ticker", "shares", "qty", "lots", "name"}'),
    ("r2 inputs: a zero multiplier", N, "            if m <= 0:\n", "            if m < 0:\n"),
    ("r2 ticket: a fill costs without the multiplier", T, "    cost = (q * p * m).quantize(", "    cost = (q * p).quantize("),
    ("r2 ticket: a fill at another multiplier", T, "    if pos is not None and (held_m or Decimal(1)) != m:",
     "    if False:"),
    ("r2 ticket: a new holding forgets its multiplier", T, '                pos["multiplier"] = _s(m)', "                pass"),
    ("r2 paper: contracts sent as shares", A, "    if mult is not None:\n        raise DomainError(", "    if False:\n        raise DomainError("),
    # 3. a fractional multiplier: the floor of the exact quotient
    ("r3 size: the upstream integer cents", S, "    return int(usd // per_share)\n",
     "    return int(usd * 100) // int((per_share * 100).to_integral_value(ROUND_HALF_EVEN))\n"),
    # 4. a market entry with no usable price blocks
    ("r4 check: no price only warns", C, '            else:\n                later.append("price: no price for %s: a market',
     '            else:\n                warns.append("price: no price for %s: a market'),
    ("r4 adapter: no price is DATA_STALE", R, '    ("price: no price", ErrorCode.MISSING_DATA),\n', ""),
    ("r4 inputs: a price unbounded", N, '        v = num(p, f"prices.{sym}")',
     '        v = parse_decimal(p, field=f"prices.{sym}")'),
    # 5. bounded inputs; no untyped failure
    ("r5 inputs: no bound", N, "    if abs(v) >= MAX:", "    if False:"),
    ("r5 cli: an arithmetic error is untyped", D, "        except ArithmeticError as e:", "        except KeyError as e:"),
    ("r5 cli: the plan's numbers unbounded", D, 'PLAN_NUMBERS = ("entry", "stop", "target", "multiplier", "tick_size")',
     'PLAN_NUMBERS = ()'),
    # 6. a yes cannot come before its check
    ("r6 ticket: a backdated yes", T, "    if now < assessment.created_at:", "    if False:"),
    # 7. a sale leaves the total unchanged; its proceeds are not spendable
    ("r7 check: a sale shrinks the total", C, '                        acct["unsettled"] += moved\n', ""),
    ("r7 check: unsettled cash out of the total", C, '            total = cash + acct["unsettled"] + sum(',
     '            total = cash + sum('),
    ("r7 adapter: the statement's unsettled cash dropped", R,
     '        portfolio["unsettled_cash"] = account["unsettled_cash"]', "        pass"),
    # LOW a, b, c
    ("la adapter: v1.0 fields ignored", R, "    check_plan_fields(plan)\n", ""),
    ("la adapter: any currency", R, '    if plan.currency != "USD":', "    if False:"),
    ("lb size: the buffer on the budget only", S, "    if cash is not None and account_total is not None:",
     "    if False:"),
    ("lb size: the cap on the budget only", S, "        if room < cap:", "        if False:"),
    ("lb size: the cap forgets what you hold", S, "        room = account_total * cap_pct / 100 - (held_value or 0)",
     "        room = account_total * cap_pct / 100"),
    ("lb adapter: sized without the account", R, "account_total=total, held_value=held)",
     "account_total=None, held_value=None)"),
    ("lc inputs: any symbol", N, "    if not isinstance(raw, str) or not raw.strip():", "    if not str(raw).strip():"),
    # OSDev6's three survivors, pinned (check.py:654, :458 and :607 at 3035201)
    ("pin check: OSDev6's loss tolerance of a cent", C, "                    if cap and max_loss > cap + CENT:",
     "                    if cap and max_loss > cap + 2 * CENT:"),
    ("pin check: the loss tolerance none", C, "                    if cap and max_loss > cap + CENT:",
     "                    if cap and max_loss > cap - CENT:"),
    ("pin check: fresh is under 15 minutes", C, "-60 <= age <= FRESH_MINUTES * 60 else None",
     "-60 <= age < FRESH_MINUTES * 60 else None"),
    ("pin check: fresh is 15 minutes and a second", C, "-60 <= age <= FRESH_MINUTES * 60 else None",
     "-60 <= age <= FRESH_MINUTES * 60 + 1 else None"),
    ("pin check: the buffer tolerance a cent", C, "                elif after_cash < buffer_usd - CENT:",
     "                elif after_cash < buffer_usd - 3 * CENT:"),
    ("pin check: the buffer tolerance none", C, "                elif after_cash < buffer_usd - CENT:",
     "                elif after_cash < buffer_usd + CENT:"),
    ("pin check: OSDev6's fresh window of 16 minutes", C, "-60 <= age <= FRESH_MINUTES * 60 else None",
     "-60 <= age <= (FRESH_MINUTES + 1) * 60 else None"),
    ("pin check: OSDev6's buffer tolerance of 50 cents", C, "                elif after_cash < buffer_usd - CENT:",
     '                elif after_cash < buffer_usd - Decimal("0.50"):'),
    # ---- the second round (OSDev6, comment 7050923565 at c5d95ae) -------------------------------------------------
    # 1. (blocking) a holding with no price and no stated cost is unknown, and the plan's own holding needs its price
    ("s1 check: an unpriced, uncosted holding is $0 again", C, "            elif shares[t]:\n", "            elif False:\n"),
    ("s1 check: an unknown holding only warns", C,
     '            for t in acct["unknown"]:\n                later.append(',
     '            for t in acct["unknown"]:\n                warns.append('),
    ("s1 check: a limit order needs no price for a held symbol", C,
     '            if acct["shares"].get(sym) and sym not in prices and typ != "market":', "            if False:"),
    # 2. a holding in contracts is never flattened
    ("s2 adapter: a contract holding flattens as shares", R, "    if held_m is not None:\n        # an Alpaca paper",
     "    if False:\n        # an Alpaca paper"),
    # 3. a result's fill without --account is typed
    ("s3 paper: a fill without --account crashes", A, '    if not getattr(args, "account", None):', "    if False:"),
    # LOWs
    ("s4 ticket: a yes 1 second early", T, "    if now < assessment.created_at:",
     "    if (assessment.created_at - now).total_seconds() > 1:"),
    ("s4 ticket: OSDev6's yes 23 hours early", T, "    if now < assessment.created_at:",
     "    if (assessment.created_at - now).total_seconds() > 23 * 3600:"),
    ("s4 adapter: a 1E-99 tick crashes the API", R, "    except ArithmeticError as e:\n        raise out_of_range(e)",
     "    except KeyError as e:\n        raise out_of_range(e)"),
    ("s4 ticket: a contract ticket says it is sent", T, "    if contracts is not None:", "    if False:"),
    ("s4 cli: a contract yes says it is sent", D, "    if m is not None:                 # `ticket place`",
     "    if False:                 # `ticket place`"),
    ("s4 settings: an unknown key is kept", G, "    if unknown:\n        # a hand-added key",
     "    if False:\n        # a hand-added key"),
    ("s4 paper: a contract ticket's fill as shares", A,
     "                          multiplier=_multiplier_of(store, t.ticket_id))\n    elif",
     "                          multiplier=None)\n    elif"),
    ("s4 ticket: sub-cent money rounded", T, "    if d != d.quantize(PENNY):                     # a sub-cent",
     "    if False:                     # a sub-cent"),
    ("s4 check: negative unsettled cash", C, "        if unsettled is None or unsettled < 0:",
     "        if unsettled is None:"),
    ("s4 inputs: negative unsettled cash", N,
     '        if v < 0:\n            raise ValidationError(f"account.unsettled_cash',
     '        if False:\n            raise ValidationError(f"account.unsettled_cash'),
    ("s4 inputs: an empty note refused", N,
     '            rec["ticket"] = text(r["ticket"], f"account.orders[{i}].ticket")\n',
     '            rec["ticket"] = text(r["ticket"], f"account.orders[{i}].ticket")\n'
     '        if r.get("note") is not None:\n            rec["note"] = text(r["note"], "note")\n'),
]

TESTS = ["tests/unit/test_risk.py", "tests/unit/test_risk_check.py", "tests/unit/test_risk_size.py",
         "tests/unit/test_risk_tickets.py", "tests/unit/test_risk_review10.py", "tests/unit/test_paper_orders.py"]


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
