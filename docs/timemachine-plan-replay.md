# Time Machine: replaying a planned day trade

Ported from the upstream `trader-timemachine/SKILL.md` at `3d0f4ef` (upstream day-trader-skill, MIT) with upstream
PRs #25 and #53 applied and #2's Time Machine text merged by hand. It is the text the `/trader` skill carries for
`traderos timemachine plan` and `traderos timemachine orb-study` (ADR-010). Changes from the upstream file, each with its
reason:

- **No Yahoo, no fetch:** the bars come from a dataset in the local store (spec 06.1 holds Yahoo's unofficial
  endpoint).
- **The method words say what the code now does:** whole-session replays, next-open fills, costs on each leg, a
  stationary-bootstrap interval. Upstream's "each bar is drawn on its own" is the flaw S11 names.
- **Removed:** the results nobody reproduced (#53's S&P 500 table) and the line "Say this when the user asks
  whether the setup works" (OSDev9's review of #53, change 2). The reel's printed numbers are gone too: they came
  from the old independent draw.
- **Safety:** the paper, live and broker lines are replaced by the capability's status. TraderOS has no order path.

---

The Time Machine sits between the plan and any money. The Exit Planner has set the entry, the stop and the target.
Before anything is sized, this asks one question: if today plays out like one of the stock's own recent sessions,
how often does this plan hit the target first, the stop first, or neither? It answers with replays of whole past
sessions and with the real record of the same rule on every one of those sessions. It is a simulation built from the
stock's own recent bars, not a prediction. Day trading loses most people money, and a good-looking replay does not
change that.

```bash
traderos timemachine plan --plan plan.json --dataset DS-…                    # 10,000 replays, seed 19
traderos timemachine plan --plan plan.json --dataset DS-… --as-of "2026-10-19 10:15"
traderos timemachine plan --plan plan.json --dataset DS-… --until 15:55 --chart
traderos timemachine plan --symbol TSLA --date 2026-10-19 --entry 364.18 --stop 358.32 --target 370.04 \
    --entry-time "10:15 AM" --dataset DS-…
traderos --json timemachine plan --plan plan.json --dataset DS-…             # the timemachine/1 record
traderos timemachine orb-study --dataset DS-…                                # the same rule across the dataset
```

Other flags: `--replays N`, `--seed N` (default 19), `--cost-bps N` (a side, each leg, default 2).

## What it reads and writes

| what | where |
| --- | --- |
| the plan | the Exit Planner's plan (symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, opening range, flat_by), as a file or as flags |
| 5-minute bars | a dataset you imported into the local store (`traderos data import FILE --interval 5m`), read as of `--as-of` |
| the result | a `timemachine/1` record in the local store: the history and the blind baseline (observed), the replays (generated), the stresses (generated) |

## How it works

1. **The pool.** Only FULL sessions before the plan's date: all 78 five-minute bars, 9:30 to 4:00 New York time.
   Half days, gaps and the plan's own day are left out. Every such session the dataset holds is in the pool, and
   the report says how many and from when to when.
2. **One replay.** Pick one past session at random. From the entry bar on, walk that session's own bars, scaled to
   start at the entry price. A replay is something one real session did, never a mix of bars from different days.
   Beside it, blocks of 6 and 12 bars on average show how much the choice matters.
3. **The fill.** The entry bar's close is the signal, so nobody can buy at it: the fill is the next bar's open. A
   next open at or past the stop or the target is a bracket the broker rejects: no trade, counted. In every later
   bar the open decides first: a bar that opens past the stop exits at that open, worse than -1R, and one that opens
   past the target exits at that open. Costs (2 bps a side by default, each leg on its own price) come off every R.
4. **The odds.** Target first, stop first, neither (still open at `--until`: the plan's flat_by when it has one,
   else the close, or 1:00 PM on an early-close day), not filled, and green (target first, or above the fill at
   `--until`). A bar that touches both the stop and the target counts as the stop, and the count is printed. R is
   (exit - fill) divided by the planned risk.
5. **The history.** The plan's own rule (the same opening range, stop rule and target R) runs again on every one
   of those past sessions, with the same fills and costs. That is what really happened, not a simulation. Signals,
   fills and rejected brackets are counted apart.
6. **The baseline.** The same trades with the setup taken out: for each history trade, a blind entry at the same
   bar on every other past session, with the same stop width, target R, fills and costs. It prints the setup's
   average R and its edge over its own blind entries, each with a 95 % range from a stationary bootstrap. Under 100
   history trades the line says "Too few trades to tell" and claims nothing. 100 is the trade minimum the founder
   signed off on 2026-11-20, the same as the lab's. At 100 or more the line says how the edge's range sits against
   zero and ends "Not a qualification": the interval and the other thresholds are still proposals, and the
   independent quant review is pending.
7. **The stresses.** A 10 % gap against the position at the first bar after the fill; every move doubled; five
   times the costs. Each says exactly what it changed. When no bar follows the fill bar before `--until` (an entry
   at 3:55 PM), the gap has nowhere to happen and the crash line says "not applied".
8. **The verdict** is one line from the numbers. It looks only at the replays that hit one side, and asks how often
   the target came first. A bigger target needs to win less often to break even: 1 in (1 + R). That is 50 % at 1R,
   33 % at 2R, 25 % at 3R. Less than 5 points from break-even: "Close to a coin flip" at 1R, "Close to break-even"
   at any other R. 5 points or more: "Leans toward the target" or "Leans toward the stop". Upstream's "Strongly toward"
   at 15 points waits for the thresholds to be signed off, and a line under the verdict says so.
9. **Below the verdict**, when it applies: a line when more than 40 % of the replays hit neither, and the average R
   when its sign goes against the lean.

**The seed.** Same plan, same data, same as-of time, same seed: the same numbers, on any machine. Say the seed with
every result so the user can repeat it.

**It refuses, in one line with a code,** when there are fewer than 20 full sessions (INSUFFICIENT_EVIDENCE), when
the entry bar is not in the data (MISSING_DATA) or is marked incomplete (INCOMPLETE_BAR), when the data is not
5-minute bars (OUTSIDE_CAPABILITY), when the plan does not make sense (INVALID_INPUT), when an opening-range plan
enters before its range ends (LOOKAHEAD_RISK: the rest of the range is not known yet at the entry), or when the real
bars after the entry are already in the data and already hit the stop or the target (LOOKAHEAD_RISK): odds on a
settled trade would be hindsight, not a forecast. If later real bars are in the data but did not resolve it, it still runs, with a
note that those bars were not used.

## Steps

1. Make sure the plan is today's plan from the Exit Planner, and that a 5-minute dataset for the symbol is in the
   store (`traderos data list`).
2. Run `traderos timemachine plan --plan FILE --dataset D`.
3. Tell the user, in this order: the odds, the median and the 5 to 95 range of the price at `--until`, the average
   R, the history line and its fills, the baseline lines, the block and stress lines, then the verdict line and
   every line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do not dress it up. If the history and the
   replays disagree, say that too.
5. Say once that this is built from the stock's own recent sessions and is not a prediction. Earnings, news and
   halts can do things those sessions never did.

## The bigger test: `traderos timemachine orb-study`

One stock's 60 days give about 30 trades, too few to tell. The study runs the same rule, fills and costs on every
symbol in a dataset and compares each trade with its blind entry, with 95 % ranges that resample whole days (trades
on the same day move together). Its four variants (1R and 2R, with and without costs, and the old signal-close
accounting) are one research family: read any one of them with the other three in mind. A dataset of today's index
members tested on past days carries survivor bias; the result says which limits its dataset has.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat them, do not rewrite them. Never judge a
  2R or 3R plan by target first vs stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Bars are 5-minute bars, checked bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan, and never calls a result a sure thing. It
never prints a P&L or a profit. It never uses a bar that the store has not made available by the as-of time or that
is marked incomplete, and never mixes the plan's own day into the pool.

## Status

- Time Machine (S11) is EXPERIMENTAL in this build (`traderos capabilities`): built and tested by its author, not
  independently verified, research use only. Its trade minimum (100) is signed off by the founder (2026-11-20);
  its other thresholds are proposals, and the independent quant review is pending (ADR-010).
- TraderOS has no order path: no broker, no order tool, nothing that submits. A plan here is hypothetical.
- Education and tooling, not financial advice.
