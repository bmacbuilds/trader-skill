# Exit Planner

Needs S14 (Exit Planner) usable. Hypothetical only.

What it needs: `--dataset` naming a dataset in the store that holds the symbol (it can be left out only when the
store holds one dataset), because every plan reads the symbol's bars for its scenarios. An opening-range stop
(`or-mid`, `or-low`) and an `atr:K` stop read the session's bars: a 5-minute (or 1-minute) dataset, the person's
own CSV. On the tutorial's daily bars, price and `pct:` stops on a tutorial symbol work (the last line below); an
opening-range or ATR stop is refused there.

The Exit Planner decides how a trade ends before it starts. The stop is where the setup has failed. The target is
where they take what they came for. Both are set BEFORE the entry, written down and handed on, so nobody has to
decide anything in the middle of a fast move. A stop is a plan, not a guarantee: a gap or a fast market can fill
past it. Day trading loses most people money, and a neat plan does not change that.

```bash
traderos --json exit TSLA --direction long --entry 364.18 --stop or-mid --target 2R --dataset DS-...
traderos --json exit TSLA --direction long --entry 364.18 --stop or-mid --target 1R --dataset DS-... \
    --entry-at 2026-10-19T14:15:00Z --as-of 2026-10-19T14:15:00Z
traderos --json exit TSLA --direction long --entry 364.18 --stop or-mid --target 1R --or-high 362.18 --or-low 354.47 \
    --dataset DS-...
traderos --json exit TSLA --direction long --entry 364.18 --stop atr:1.5 --entry-at 2026-10-19T14:15:00Z --dataset DS-...
traderos --json exit TSLA --direction long --entry 371.25 --setup level --stop price:366.00 --target 2R --dataset DS-...
traderos --json exit TSLA --direction long --entry 364.18 --stop 358.32 --target 370.04 --dataset DS-...
traderos --json exit TUTMKT --direction long --entry 100.70 --stop pct:2 --target 2R --dataset DS-...
```

Other flags: `--flat-by 15:55`, `--or-minutes 15`, `--setup orb|level`. A bare number is a price; anything else is
one of the rules below. `--flat-by`, `--or-high`, `--or-low` and `--entry-at` go with a stop or target rule only.
`--entry-at` (when the entry filled) must be on the `--as-of`'s New York day and not after it: a plan is for one
trading day, and it cannot read an entry that has not happened.

## What it reads and writes

| what | where |
| --- | --- |
| the entry | the price from the Entry Timer's trigger ([entry.md](entry.md)), or a price the user gives |
| the session's bars | the stored dataset, for the opening range and an `atr:K` stop, read only up to the entry (`--entry-at`) |
| the result | an `exit-plan/1` record (`XP-`): the entry, and each hypothetical scenario (the stop, a gap through the stop, the target, the time exit at flat-by, an exit at the last close, one ATR against) with its price, its change "vs the entry, per unit" and its assumptions, then the risks and the limitations |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | their price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar average true range (simple mean, not Wilder's smoothing) under the entry, on the session's bars that closed at or before the entry. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop they name.

- **Rounding.** The stop rounds AWAY from the entry to the tick (down, for a long). Risk = entry minus stop.
  Target = entry + R x risk, rounded half up to the tick. `2R` by default; `1R`, `1.5R` or `price:X` also work.
- **Refused, with the reason (exit 3), for every plan.** A long's stop at or above the entry, or its target at or
  below it (a short's the other way round). An entry, stop or target off the tick.
- **Refused, with the reason, for a plan with a stop or target rule** (`or-mid`, `pct:X`, `2R` and the rest). A risk
  under 0.05% of the price: too tight to survive the spread, normal wiggle would stop them out. A short: the rules
  plan longs only. An entry at or after the flat-by time. An `--entry-at` on another day than `--as-of`, or after
  it. With bare prices these rule checks do not run: the planner takes a short or a very tight stop as given. Do
  not call such a plan checked; the risk check refuses a short ([risk-budget.md](risk-budget.md)).
- **Flat by** (with a rule). 15:55 by default. Out by then, win or lose, no overnight hold. On a 1:00 p.m. early
  close it moves up to 12:55.

## Steps

1. Run it right after the Entry Timer triggers. Before that, plan ahead at the level they expect.
2. Use the default stop for an opening-range breakout unless the user names another. A level setup needs one:
   ask.
3. Run it, and read back the entry, then each scenario as printed: its price and its change vs the entry, per unit,
   with the rule or assumption it states (the stop's rule, the target's R, the flat-by time). Those are the
   planner's numbers; the risk a share and reward:risk come from the risk check ([risk-budget.md](risk-budget.md)).
4. If it refuses, say the reason and the fix (a wider stop, a farther target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to the Time Machine to replay the plan ([timemachine.md](timemachine.md): the plan's symbol, date,
   entry, stop, target and entry time as flags), then to the risk check to size it to the max loss per trade
   ([risk-budget.md](risk-budget.md)). Offer price alerts for the stop, the target and the flat-by time
   ([alerts.md](alerts.md): `alerts ring --from-plan` reads this plan's `--json` result).

## Example: the reel

TSLA, Monday 2026-10-19, on the user's own 5-minute bars. The entry is 364.18 at 10:15 AM (the 10:10 bar, bar 8)
and the opening range is 354.47 to 362.18. `--stop or-mid --target 1R --entry-at 2026-10-19T14:15:00Z --as-of
2026-10-19T14:15:00Z` prints (trimmed):

```
Exit Planner [EXPERIMENTAL: research use only]: HYPOTHETICAL long TSLA from 364.18, as of 2026-10-19T14:15:00+00:00
  stop                     price 358.32 USD  vs the entry, per unit -5.86 USD
  gap through stop         price 344.82 USD  vs the entry, per unit -19.36 USD
  target                   price 370.04 USD  vs the entry, per unit 5.86 USD
  time exit                price unavailable (the price at the flat-by time is not known in advance)  ...
  one ATR against          price 362.09 USD  vs the entry, per unit -2.09 USD
  risk: Hypothetical only: nothing here is an order, and no order is sent.
```

The `--json` record says how each was set: the stop is the opening-range midpoint, (362.18 + 354.47) / 2 =
358.325, rounded down; the target is 1R; the time exit is flat by 15:55 New York. The default 2R target is 375.90.
The risk check then sizes it: a $50 max loss per trade buys 8 shares ([risk-budget.md](risk-budget.md)).

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: the entry, and each scenario's price and its change vs the entry, per unit, as the planner
  prints them, labelled hypothetical. Never a P&L in money, a return or a profit for the user.
- Nothing here promises winning trades or money. A 1R target is not a prediction that the price gets there.
- Long side only.

## What it never does

It never places an order, never sizes a position, never widens a stop to make a plan pass, and never calls a stop a
guarantee.

## Safety

- Education and tooling, not financial advice. A plan is a rules check, not a recommendation.
- Paper only. TraderOS refuses live trading in its code, and no setting here changes that.
- Nothing in TraderOS places an order except `traderos ticket place`, to the user's Alpaca paper account, and
  only when all three hold: the risk check passed that exact ticket and has not expired, the user typed an
  explicit yes for that one ticket in the chat, and the user allowed that one run at Claude Code's own permission
  prompt ("Allow once"). Never put `traderos ticket place` on an allow list, and never tell the user to.
- No options, margin, short selling or leverage: options are deferred, and this build plans long stock trades only.
- Broker keys live in the user's own environment or TraderOS's settings, set by them in their own terminal, never
  in the chat.
- The paper ticket and its gates: [paper-orders.md](paper-orders.md).
