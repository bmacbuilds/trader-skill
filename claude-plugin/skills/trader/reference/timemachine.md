# Time Machine

Needs S11 (Time Machine) usable.

Three runs, one command:

```bash
traderos --json timemachine SV-... --dataset DS-...
traderos timemachine plan --plan replay-plan.json --dataset DS-...
traderos --json timemachine orb-study --dataset DS-...
```

What each needs: a strategy version's run reads daily bars, so it works on the tutorial dataset (an approved
strategy on its symbols, [strategy-lab.md](strategy-lab.md)). The plan replay and the ORB study read 5-minute bars,
which the tutorial does not have: they need the person's own 5-minute CSV for the symbol
([data.md](data.md), `--interval 5m`). On any other dataset they refuse (OUTSIDE_CAPABILITY): say so.

## A strategy version: history, resampled paths and stress

```bash
traderos --json timemachine SV-... --dataset DS-...
traderos --json timemachine SV-... --dataset DS-... --horizon 252 --paths 1000 --seed 7
```

It reads the development and validation windows only, never the sealed holdout. Each section is labelled:
OBSERVED (what the strategy did in history), GENERATED (block-resampled paths of the strategy's own observed
returns, at three block lengths) and STRESS (a crash gap, doubled volatility, five times the slippage), each with
the assumptions it states. Repeat the labels and the assumptions. Resampled paths are plausible variation of what
was observed, not a forecast.

## A planned day trade: the plan replay

The Time Machine sits between the plan and any money. The Exit Planner has set the entry, the stop and the target.
Before anything is sized, this asks one question: if today plays out like one of the stock's own recent sessions,
how often does this plan hit the target first, the stop first, or neither? It answers with replays of whole past
sessions and with the real record of the same rule on every one of those sessions. It is a simulation built from
the stock's own recent bars, not a prediction. Day trading loses most people money, and a good-looking replay does
not change that.

```bash
traderos timemachine plan --plan replay-plan.json --dataset DS-...
traderos timemachine plan --plan replay-plan.json --dataset DS-... --as-of "2026-10-19 10:15"
traderos timemachine plan --plan replay-plan.json --dataset DS-... --until 15:55 --replays 10000 --seed 19
traderos timemachine plan --symbol TSLA --date 2026-10-19 --entry 364.18 --stop 358.32 --target 370.04 \
    --entry-time "10:15 AM" --dataset DS-...
traderos timemachine plan --plan replay-plan.json --dataset DS-... --chart
```

**Run the plan replay without `--json`.** The verdict, the lines under it, "Too few trades to tell" and the
"Trade minimum" line are printed only in the text output: the `--json` record (`timemachine/1`) carries the numbers
and the limitations but no verdict. One run in the text form prints every number the steps below name, the
verdict, the seed and the record id (`TM-`), so run it once, that way. Quote the verdict line and every line under
it exactly as printed, and never work out a verdict yourself from the odds. The dataset's limits are in
`traderos --json data check DS-...`.

Other flags: `--replays N` (default 10,000), `--seed N` (default 19), `--cost-bps N` (a side, each leg, default 2).
The fan chart is text too: add `--chart`, and show its lines as printed.

### What it reads and writes

| what | where |
| --- | --- |
| the plan | as flags (`--symbol --date --entry --stop --target --entry-time`), or a JSON file in the plan shape: `symbol`, `date`, `entry`, `entry_time` (or `entry_bar`), `stop`, `target`; optional `stop_rule`, the opening range, `flat_by`. It is not the risk check's plan file, which refuses `entry_time`, and not the Exit Planner's `exit-plan/1` record: take that record's entry, stop and target, with the day and the entry time, as flags |
| 5-minute bars | a dataset the user imported into the local store (`traderos data import FILE --interval 5m`), read as of `--as-of`; the tutorial has none |
| the result | a `timemachine/1` record in the local store: the history and the blind baseline (observed), the replays (generated), the stresses (generated) |

### How it works

1. **The pool.** Only FULL sessions before the plan's date: all 78 five-minute bars, 9:30 to 4:00 New York time.
   Half days, gaps and the plan's own day are left out.
2. **One replay.** Pick one past session at random. From the entry bar on, walk that session's own bars, scaled to
   start at the entry price. A replay is something one real session did, never a mix of bars from different days.
   Beside it, blocks of 6 and 12 bars on average show how much the choice matters.
3. **The fill.** The entry bar's close is the signal, so nobody can buy at it: the fill is the next bar's open. A
   next open already past the stop or the target is a bracket the broker rejects: no trade, counted. A bar that
   opens past the stop exits at that open, worse than -1R. Costs (2 bps a side by default, each leg on its own
   price) come off every R.
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
   history trades the line says "Too few trades to tell". That trade minimum, 100 trades, is signed off by the
   founder, and the lab's is the same. The verdict's other thresholds (the coin flip, "strongly", many neither),
   the interval and the cost are still proposals, and the independent quant review is pending. The report prints
   both on its "Trade minimum" line: repeat it as printed.
7. **The stresses.** A 10 % gap against the position at the first bar after the fill; every move doubled; five
   times the costs. Each says exactly what it changed.
8. **The verdict** is one line from the numbers. It looks only at the replays that hit one side, and asks how often
   the target came first. A bigger target needs to win less often to break even: 1 in (1 + R). That is 50 % at 1R,
   33 % at 2R, 25 % at 3R. Less than 5 points from break-even: "Close to a coin flip" at 1R, "Close to break-even"
   at any other R. 5 points or more: "Leans toward the target" or "Leans toward the stop". "Strongly toward"
   at 15 points waits for that threshold to be signed off, and a line under the verdict says so.
9. **Below the verdict**, when it applies: a line when more than 40 % of the replays hit neither, and the average R
   when its sign goes against the lean.

**The seed.** Same plan, same data, same as-of time, same seed: the same numbers, on any machine. Say the seed with
every result so the user can repeat it.

**It refuses, in one line with a code,** when there are fewer than 20 full sessions (INSUFFICIENT_EVIDENCE), when
the entry bar is not in the data (MISSING_DATA) or is marked incomplete (INCOMPLETE_BAR), when the data is not
5-minute bars (OUTSIDE_CAPABILITY), when the plan does not make sense (INVALID_INPUT), or when the real bars after
the entry are already in the data and already hit the stop or the target (LOOKAHEAD_RISK): odds on a settled trade
would be hindsight, not a forecast. If later real bars are in the data but did not resolve it, it still runs, with a
note that those bars were not used.

### Steps

1. Take today's plan from the Exit Planner's result ([exit.md](exit.md)) or from the user, as flags or a plan
   file (above), and make sure a 5-minute dataset for the symbol is in the store (`traderos --json data list`). If
   there is none, say the replay needs the person's own 5-minute bars, and stop.
2. Run `traderos timemachine plan --plan FILE --dataset D` (or the flags), without `--json`. Add `--until 15:55`
   when the plan has a flat-by time, so "neither" means still open at the time they would get out.
3. Tell the user, in this order: the odds, the median and the 5 to 95 range of the price at `--until`, the average
   R, the history line and its fills, the baseline lines, "Too few trades to tell" and the trade minimum line
   when they are printed, the block and stress lines, then the verdict line and every line under it exactly as
   printed. Say the seed and the record id.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do not dress it up. If the history and the
   replays disagree, say that too.
5. Say once that this is built from the stock's own recent sessions and is not a prediction. Earnings, news and
   halts can do things those sessions never did.
6. If the user still wants the trade, size it hypothetically: [risk-budget.md](risk-budget.md).

## The bigger test: the ORB study

```bash
traderos --json timemachine orb-study --dataset DS-...
traderos --json timemachine orb-study --dataset DS-... --symbols TSLA NVDA AMD --until 15:55 --draws 2000 --seed 19
```

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

## Safety

- Say the Time Machine's (S11) status from `traderos --json capabilities`. While it is `experimental` it is built
  and tested by its author, not independently verified and research use only. Its trade minimum (100 trades) is
  signed off by the founder; its other thresholds are proposals.
- Education and tooling, not financial advice. A replay is a simulation, not a recommendation.
- Paper only. TraderOS refuses live trading in its code, and no setting here changes that.
- Nothing in TraderOS places an order except `traderos ticket place`, to the user's Alpaca paper account, and
  only when all three hold: the risk check passed that exact ticket and has not expired, the user typed an
  explicit yes for that one ticket in the chat, and the user allowed that one run at Claude Code's own permission
  prompt ("Allow once"). Never put `traderos ticket place` on an allow list, and never tell the user to.
- No options, margin, short selling or leverage: options are deferred, and this build plans long stock trades only.
- Broker keys live in the user's own environment or TraderOS's settings, set by them in their own terminal, never
  in the chat.
