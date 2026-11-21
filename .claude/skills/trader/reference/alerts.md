# Price Alerts

Needs S18 (Price Alerts) usable.

What it needs: `--dataset` naming a dataset in the store that holds the symbol (a plan's own dataset is used when
it is left out). Price alerts on a day trade (`alerts ring`, a plan's stop and target) need the symbol's 5-minute
bars, the person's own CSV: the tutorial has daily bars only. `alerts check` runs condition rules on any bars,
the tutorial's included (the last line below, on a tutorial symbol).

Price Alerts watches the numbers so the user does not have to stare at a chart. An alert is information, never an
order: when a stop alert rings, nothing is sold. TraderOS checks the user's stored data each time it is run; Free
has no always-on service, so the user runs it, or schedules it on their own machine.

```bash
traderos --json alerts ring --from-plan exit-plan.json --dataset DS-...
traderos --json alerts ring --from-plan exit-plan.json --at 2026-10-19T19:55:00Z --dataset DS-...
traderos --json alerts ring --rules alerts.json --state ring-state.json --dataset DS-...
traderos --json alerts reset tsla-stop
traderos --json alerts reset tsla-stop --state ring-state.json
traderos --json alerts check --rules alert-rules.json --dataset DS-... --at 2025-12-31T18:00:00Z
```

`alerts reset` without `--state` acts on the default state file; a rule that rang under its own `--state` file is
reset with the same `--state`.

## What it reads and writes

| what | where |
| --- | --- |
| an exit plan | `--from-plan`: the Exit Planner's `--json` result, saved to a file: its stop, target and flat-by alert for its day |
| alert rules | `--rules`: a JSON array of the rule types (below); a price rule names its `instrument_id`; prices as integers or strings |
| the state | which rules rang and when, so each rings once (default: `alerts/ring-state.json` in the TraderOS folder) |
| prices | the newest complete regular-session close in the stored dataset (9:30 to 4:00 New York): an after-hours bar is never the price, the previous close or a touch. A symbol whose newest bar is stale gets no price, so nothing rings on old data |
| condition rules | `alerts check --rules`: `alert-rule/1` records in the condition grammar, with their sampling and missed-window policy |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the New York clock passed `at` ("15:55") on a trading day | once per trading day |
| `opening_bell` | the NYSE is open | once per trading day |

A rule with `"date"` is watched on that New York date only; plan rules carry the plan's date, so yesterday's stop
never rings today. A `time` rule with `"before_close": true` moves up with a 1:00 p.m. early close, so "flat by
close" at 15:55 rings at 12:55 on those days. A rule that rang stays quiet until `alerts reset ID` re-arms it.

## Steps

1. Right after the Exit Planner makes the plan, save its `--json` result to a file and run
   `alerts ring --from-plan` on it. Confirm the three alerts back to the user: the stop, the target and the
   flat-by time.
2. Run it again while the trade is open, on fresh data: each run reads the newest stored bar and rings each alert
   once. It is not a live feed. A price rule reads every regular-session bar since its own last check (or, for a
   plan's rules, since the plan was made), so a level touched between two checks rings at the next one: it says
   the price it traded and the price now. A rule's first check with no plan behind it reads only the price now.
   A rule last checked more than 5 sessions ago reads only those 5 (MISSED_WINDOW: say so).
3. Tell the user what rang in one line each, and what could not be checked (UNKNOWN). A stop or target alert means
   a bracket order has probably filled; "flat by close" means it is time to close anything still open. Both go
   through [paper-orders.md](paper-orders.md) (record the fill; flatten, the risk check and their yes). Never act
   for them.

## Example: the reel

TSLA on Monday 2026-10-19, on the user's own 5-minute bars: entry 364.18 at 10:15 AM, stop 358.32, target 370.04,
flat by 15:55. `--from-plan` gives a stop, a target and a flat-by alert for that day. A run at 10:53, with every
bar since the plan between the stop and the target: nothing rang. At 15:55 the flat-by alert rings once. The next
morning the plan's three alerts are no longer watched.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a stored price as live.
- Confirm every new rule back to the user: ticker, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a price is live, and never predicts where a price is
going.

## Safety

- Education and tooling, not financial advice. An alert is a rules check, not a recommendation.
- Paper only. TraderOS refuses live trading in its code, and no setting here changes that.
- Nothing in TraderOS places an order except `traderos ticket place`, to the user's Alpaca paper account, and
  only when all three hold: the risk check passed that exact ticket and has not expired, the user typed an
  explicit yes for that one ticket in the chat, and the user allowed that one run at Claude Code's own permission
  prompt ("Allow once"). Never put `traderos ticket place` on an allow list, and never tell the user to.
- No options, margin, short selling or leverage: options are deferred, and this build plans long stock trades only.
- Broker keys live in the user's own environment or TraderOS's settings, set by them in their own terminal, never
  in the chat.
- The paper ticket and its gates: [paper-orders.md](paper-orders.md).
