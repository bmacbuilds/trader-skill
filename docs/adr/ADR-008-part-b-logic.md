# ADR-008: the upstream scanner, entry, exit and alert logic is kept, and runs on the stored data

- **Status:** proposed by OSDev6, 2026-11-20.
- **Owner rule (Brian, 11-19, relayed by OSDev1 on the wall at 07:22 UTC):** "Python files and all code only gets
  rewritten if there is clearly a flaw or if there is clearly a better method."
- **Spec:** §12.1 S01, §03.3 (reuse workflow), §06.1 (approved sources).

## Decision
Part B keeps the upstream five daily setups (`breakout_20d`, `bull_flag`, `reclaim_ma20`, `oversold`, `oversold_bounce`),
its liquidity floors and minimum history, and its opening-range breakout rules, exactly as written in
`tank/day-trader-skill/trader-scanner/scan.py` at `3d0f4ef`, in `src/traderos/intelligence/setups.py`.

They are offered beside the condition grammar, not instead of it:
- `api.scan_setups(dataset_id, "daily" | "orb", ...)` returns the same `scan/1` record as `api.scan_market`;
- `traderos scan --setups daily|orb` sits beside `traderos scan --criteria FILE`. That one CLI line is additive and
  changes nothing that already exists.

## What changed from the upstream file, each with its reason
| Change | Reason |
|---|---|
| Stored bars through the as-of `BarReader`; the dataset's instruments are the universe | Flaw: Yahoo's unofficial endpoints and the Nasdaq Trader list download are not approved sources (spec §06.1 holds Yahoo), and network reads had no point-in-time guarantee |
| `traderos.clock.XNYS` | Flaw: a hand-kept 2026-2027 holiday table, copied into four files |
| Timezone-aware instants | Better method: no DST guess at the edges (QT01) |
| Decimal prices | Flaw: contracts v1 refuse float money |
| One ORB pass on real highs | The upstream two stages existed to stay under Yahoo's request limits; its own docstring calls stage 1 a necessary condition of stage 2, so the hits are the same |
| RSI 14 from `traderos.features` (`rsi/1`) | Better method: a fixed warm-up, so the same store gives the same value. One named flaw fixed: a flat window reads 50, not 100 |
| Liquidity from the newest complete bar | Stored data has no live quote; upstream fell back to the last close itself |

## Evidence
`tests/unit/test_setups.py` pins every rule at its edge by hand. All 22 mutants of a threshold, a window, a
comparison, a floor, the ORB rules or the ranking fail it (measured with bytecode off: see the note at the end).

## The Entry Timer's triggers (`trader-entry/entry.py`, `evaluate`)
Kept as upstream wrote them, in `src/traderos/intelligence/triggers.py`: `orb` (the first completed bar after the
opening range that closes above its high; a wick is not a trigger) and `level` (the first close above a given price),
an optional volume multiple against the range's average bar, the entry window 09:45 to 15:30 New York, its
`early_cutoff` (15:30 becomes 12:30 before a 1:00 p.m. close), and its statuses: triggered, waiting, expired, no-data.
From upstream PR #23 it also keeps the refusal of an opening range missing a bar.

`api.assess_entry(..., setup="orb" | "level")` and `traderos entry SYMBOL --setup orb|level [--level P]
[--or-minutes N] [--min-volume-x X]` are additive: with no setup, the Entry Timer answers as before. In `entry/1`,
triggered is ELIGIBLE at the trigger bar's close, waiting and expired are NOT_ELIGIBLE with its reason, and no bars or
a broken range is UNAVAILABLE.

| Change | Reason |
|---|---|
| Stored bars; its `SETTLE`, `data_time` and `pending` lag handling dropped | Flaw: Yahoo is held (§06.1). That code existed because Yahoo posts a bar's last trade late; the store withholds a bar until its `available_at`, so a bar it returns is final |
| XNYS sessions | Flaw: the same hand-kept holiday table |
| New York times and the New York date placed from the session's 09:30 open | Better method: no timezone table and no DST guess; 8 p.m. New York on a Monday is still Monday, as in its `to_eastern(now).date()` |
| A thin bar's volume multiple shown rounded down | Flaw: `%.2f` printed 1.998x as "2.00x ... under your 2.00x" |

`tests/unit/test_triggers.py` pins each rule at its edge. Of 16 mutants of the rules, 15 fail it; the survivor is
equivalent (no bar inside the range can close above the range's own high, so `first_slot` never decides an ORB).

## The Exit Planner's rules (`trader-exit/exit.py`, `compute` and `resolve_flat_by`)
Kept as upstream wrote them, in `src/traderos/intelligence/exit_rules.py`: stops `or-mid` (the default for an orb entry),
`or-low`, `price:X`, `pct:X`, `atr:K` (its session ATR: the last 14 of the session's bars to the entry, at least 3, the
first bar its own range), rounded DOWN; targets `2R` (the default), `1R`, `1.5R` ... or `price:X`, rounded half up; its
refusals (a stop at or above the entry, a target at or below it, a risk under 0.05% of the price, a short); `flat_by`
15:55 New York, moved up with an early close (12:55), refused when given at or after the close; and its warning under
`min_reward_risk` 1.0. The upstream project's own numbers are the tests: the reel's or-mid 1R plan is stop 358.32, target 370.04.

`api.plan_exit(..., stop_rule=..., target_rule=...)` and `traderos exit --stop or-mid|pct:X|... --target 2R|...` are
additive: a bare number is a price, as before. A rule adds a "time exit" scenario at flat_by.

| Change | Reason |
|---|---|
| The instrument's tick, not the cent | Flaw: a cent assumed every instrument trades in cents; contracts v1 carry the tick. On a $0.01 tick the prices are identical |
| An entry off the tick is refused, not rounded | Flaw: rounding a typed entry silently plans a different trade (RC08: refuse, never repair) |
| The opening range and ATR from the stored session bars; `or_high`/`or_low` still accepted | Flaw: Yahoo is held (§06.1). A range missing a bar is refused (upstream PR #23) |
| XNYS and the 09:30 open place 15:55 and the early close | Flaw: the same hand-kept holiday table |

`tests/unit/test_exit_rules.py`: all 20 mutants of the rules fail it.

## The alerts (`trader-alerts/alerts.py`, `evaluate` and `rules_from_plan`)
Kept as upstream wrote them, in `src/traderos/intelligence/alert_rules.py`: `above`/`target` and `below`/`stop` ring once
and stay quiet until reset, or re-arm with `"rearm": true` when the price crosses back; `pct_move` from the previous
close (or `ref`), up or down, once per New York day; `opening_bell` and `time` once per trading day, `before_close`
moving a time up with an early close; `date` scoping; its state file's `rung` map (`{"rung": {id: UTC stamp}}`),
which gains two maps of ours: `checked` (each rule's own last check, its touch window and its
`previous_evaluated_at`) and `watching` (a plain rule's first check: when watching began). A plan's price rules carry
`watch_from`, the plan's own time. An exit plan becomes a stop, a target and a flat-by alert for its day.

`api.ring_alerts(rules, at=..., state=...)` and `traderos alerts ring --rules FILE | --from-plan FILE [--state FILE]`
sit beside `alerts check` (alert-rule/1 conditions), which is unchanged.

| Change | Reason |
|---|---|
| The price is the newest complete stored close; the previous close is the last close of the session before it; a stale symbol gets no price | Flaw: Yahoo and Stooq are held (§06.1). No level rings on old data |
| XNYS, and `traderos.clock` for the New York date and time | Flaw: the same hand-kept holiday table |
| A plan's flat-by alert is its own flat-by instant, shifted once | Flaw: `rules_from_plan` copied a flat_by already moved to 12:55 and set `before_close`, so it moved three hours again: on the day after Thanksgiving it rang at 9:55 a.m. |
| The early-close shift is the session's own (regular close minus the early close) | Better method: equal to the upstream three hours on every NYSE early close, and it cannot drift |
| No message points at an order | The upstream flat-by text named its broker skill's sell ticket; TraderOS free never builds, sends or recommends one (§12.1) |
| Prices as ints or decimal strings; a float literal is refused | Contracts v1 |

`tests/unit/test_alert_rules.py`: all 20 mutants of the rules fail it.

## After OSDev4's review of #13
Each change is either restored to the upstream code or kept with its reason. Nothing below is silent.

**Restored to the upstream code:**
- The ORB scan's $5 price floor (`orb --min-price 5`); the port had used $0.
- The ORB scan's expiry after 15:30 New York (`--until`, default 15:30, `None` for none). A read exactly at the
  close is allowed again, as its `--at 16:00` replay was.
- The daily scan checks the liquidity floor before the history length, and a name under the floor is not scanned:
  it gets no setup tags.
- `min_volume_x=0` means off, as in its entry trigger. Only a negative number or NaN is refused.
- ATR is rounded to 6 places before it is multiplied (its `D(round(atr, 6))`).
- `pct_move` falls back to the session's open when there is no previous close.
- A price-only plan still gets the 15:55 flat-by alert, moved up on an early close (its `rules_from_plan` always
  added one).

**Fixed, each a flaw in the port:**
- **Plan alerts never rang again.** Every plan's alerts shared the id `<sym>-plan-stop`, and the state kept the mark
  across days. Plan alert ids now carry the plan's id. A dated rule's mark is dropped once its day has passed, and
  `traderos alerts reset ID` restores the upstream `reset`.
- **The ATR stop could read past the entry** (QT01; upstream #2 fixed the same thing in the upstream planner). It now reads
  only up to `entry_at` (`--entry-at`), and the note names the instant it read to.
- **The plan day came from the UTC date.** It is now the New York day; on a weekend or holiday evening it is the next
  session after it.
- **After-hours bars could push the opening range out of the read window.** Entry and exit now read the trading day
  from its open.
- **An alert that could not be checked looked like a quiet market.** `alerts ring` now returns one `alert-event/1`
  per rule. `triggered: null` means not checked, with the reason; the output says UNKNOWN and never "Nothing rang"
  for it. The dataset's limitations and `safe_for` (TUTORIAL_ONLY on the tutorial) carry through.
- **A stop crossed and recovered between two checks never rang.** Each price rule now reads the regular-session bars
  that closed since ITS OWN last check, so their low or high counts. A rule's first check reads only the price now,
  unless it comes from a plan: then it watches from the plan's own time (`watch_from`). A bar that BEGAN before a
  rule was watched (the plan's time, or the rule's first check) is never a touch, even one that closed after: its low
  may have come first (OSDev4's re-reviews, N1 and R1: without that bound, an or-mid stop "rang" on the plan's own
  range, and a plan made at 10:03 rang on the 10:00-10:05 bar's earlier low). The rest of that one bar is covered by
  the price at each check, as the upstream quote was. Within that bound this is a better method than the upstream single quote. A
  check that had no price never moves a rule's window. A rule last checked more than 5 sessions ago is marked
  MISSED_WINDOW.
- **After-hours bars are never the price** (re-review, N3): only regular-session bars set the price, the previous
  close, the open and a touch, as the upstream regular-market quote did.
- **The previous close is the previous session's closing bar, or none** (re-review, R2): never an older session's (a
  missing Friday must not make Thursday's close Monday's reference), and never a bar before that session's close.
  Without it `pct_move` falls back to the session's open, as the upstream one did.
- **Alert identity was a ticker.** A price rule names its `instrument_id` (contracts v1 §5.2); `symbol` is only
  shown. A plan's rules carry its `instrument_id` and `dataset_id`, and a rule naming another dataset is refused.
  An `instrument_id` the dataset does not hold refuses the whole check (MISSING_DATA, exit 3), where upstream skipped
  that symbol: a ticker typed where an identity belongs is a mistake to say, not a rule to drop silently.
- **A name under a floor was UNAVAILABLE.** It was evaluated, so it is NOT_ELIGIBLE with the reason ILLIQUID.
- **RSI was computed over 200 bars.** It now uses rsi/1's declared lookback, the window `scan --criteria` reads.
- **Off-tick inputs were accepted.** A given opening range off the tick is refused, as an off-tick entry is. A NaN,
  zero or negative `level`, and a non-numeric `min_reward_risk`, are refused (INVALID_INPUT), not raised as raw errors.
- **The state file wasn't checked.** A malformed state is INVALID_INPUT, and the state is saved only after the
  output is printed.
- **An edited plan was accepted.** `--from-plan` checks the plan's `canonical_hash`; a mismatch is
  INTEGRITY_MISMATCH.

**Reason codes:**
- Short history and a missing ORB bar are now `MISSING_DATA`. A trace shows what was found against what was needed.
- `ILLIQUID` stays: it is a filter's verdict on a name that was evaluated, like `setup:<tag>`, not a data error.

**Kept, with the reason:**
- When the exit planner has no range given, `or-mid` reads the opening range from the stored bars. This is a
  better method than typing it in, and the upstream `--or-high`/`--or-low` still work.
- **A range read from the bars is rounded to the tick, half up**, as the upstream `cents()` rounded it (re-review, item
  12; restored). A range typed in must already be on the tick.
- **`entry_at`** must not be after `as_of`, and must be on the same New York day: the upstream plan is for one trading
  day and is flat by the close.
- **"It never prints a P&L or a return" (the upstream Rules) is kept in the text.** Contract v1's `ExitScenario` carries
  `pnl_per_unit` (frozen, so the field name stays): each scenario's difference from the entry, per unit. For the stop
  and the target it is upstream's "risk a share" and the reward. The text labels it "vs the entry, per unit", never P&L,
  and prints no return and no money total (flagged by #16's author).
- **No new exit code for "not checked".** The CLI contract freezes exit codes at 0/2/3/4. An unchecked rule is
  exit 0 with `triggered: null`, as `alerts check` already does. A separate exit code would change the frozen contract
  and needs its own ADR. Settled by OSDev4, who froze the CLI contract (review of #13).

## Note on the mutation counts
Each mutant is run with `python -B`. Without it, two mutants of the same size written in the same second reuse the
first one's cached bytecode, so a mutant can look killed (or alive) because the previous one was. The first count in
this ADR (11 of 11) was taken that way; every count above is re-measured with bytecode off.
