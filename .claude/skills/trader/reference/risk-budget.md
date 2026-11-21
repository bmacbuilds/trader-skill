# Budget and risk: size a plan, check it against the user's limits

In TraderOS the budget and the risk check run as one command, `traderos risk assess`. Needs S15 (Budget Manager)
and S16 (Risk Manager) usable.

The Budget Manager turns a plan ("buy TSLA at 364.18, stop 358.32") into an exact size ("8 shares"), inside rules
the user sets once. The stop decides the size: the further away the stop, the fewer shares. The Risk Manager is
the gate after it: every plan is checked against the user's limits, and a BLOCK is final for that plan: it does
not go to the user for a yes. Most people who day trade lose money. Sizing to a max loss and these limits keep
each loss and each day's losses small. They do not make a trade win.

```bash
traderos --json risk assess --plan plan.json --policy policy.json --account account.json
traderos --json risk assess --plan plan.json --policy policy.json --account account.json --prices prices.json
traderos --json risk assess --plan plan.json --policy policy.json --account account.json --limit
traderos --json risk assess --plan plan.json --policy policy.json --account account.json --limit 364.00
traderos --json risk assess --plan plan.json --policy policy.json --account account.json --now 2026-10-19T10:15:00-04:00
```

`--limit` makes it a limit order at the plan's entry (or at the price given). `--now` pins the clock (RFC 3339 with
its zone; a time without a zone is refused). `--stress 0.05` is the gap beyond the stop the stressed loss assumes.
A plan for another day is refused: make a new one with the Exit Planner. It needs no market data and no keys: it
reads only the files named here, so it runs on a fresh store.

## What it reads and writes

Every file is JSON read strictly: numbers as strings or integers (a float literal, `NaN` or `Infinity` is refused),
no duplicate or unknown keys, small files only. A refusal names the key.

| file | what |
| --- | --- |
| `--plan` | the trade: `symbol`, `direction` (`long`), `date` (the New York trading day it is for), `entry`, `stop`, `target`; optional `multiplier`, `currency`, `instrument_id` |
| `--policy` | the user's limits, in the keys below. Required: TraderOS has no default for any risk limit |
| `--account` | what they hold and the orders of the last few days as they placed them at their broker: `settled_cash`, `reserved_cash`, `positions`, `orders`. Without it, exposure is UNKNOWN and no ticket can be drafted |
| `--prices` | the user's latest price, with the time they read it: `{"prices": {"TSLA": "366.475"}, "detail": {"TSLA": {"time": "2026-10-19T14:52:13Z"}}}`. A price under 15 minutes old is fresh; a price with no time is never fresh. A market entry needs a fresh one: without it the check blocks the entry (MISSING_DATA). A limit order (`--limit`) needs none |
| the result | a plan (`RP-`), an assessment (`RA-`) with the four risk measures, and a no-trade record (`NT-`) when it is refused, all in the local store |

## Step 1: set the rules once

If the user has no policy file, ask them for each value in plain words and write it for them. Example
values are shown; they are examples, not defaults, and every one must be the user's own answer:

- **budget**: how much money this is for. Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (example: 10).
- **max_loss_per_trade_usd**: the most one trade may lose if the stop fills at the stop (50).
- **max_position_pct**, **max_order_usd**, **min_order_usd**: the biggest position (75% of the budget), the
  biggest order ($4,000) and the smallest order ($5).
- The day limits the risk check enforces: **max_daily_loss_usd** (150), **max_trades_per_day** (3),
  **max_orders_per_day** (10), **min_reward_risk** (1.0), **no_entries_before** / **no_entries_after**
  ("09:45" / "15:30") and **flat_by** ("15:55"), all New York time. `require_stop` can only be `true`.
- **blocked**: tickers they never want to trade. **pdt_guard**: true if their broker still applies the old
  pattern-day-trader rule.
- `allow_options`, `allow_margin`, `allow_short` stay false unless they say otherwise.

```json
{"budget": "5000", "cash_buffer_pct": "10", "max_position_pct": "75", "max_order_usd": "4000",
 "min_order_usd": "5", "max_loss_per_trade_usd": "50", "max_daily_loss_usd": "150", "max_trades_per_day": 3,
 "max_orders_per_day": 10, "min_reward_risk": "1.0", "require_stop": true, "no_entries_before": "09:45",
 "no_entries_after": "15:30", "flat_by": "15:55", "fractional": false, "blocked": [], "allow_options": false,
 "allow_margin": false, "allow_short": false, "pdt_guard": false, "paper": true}
```

`0` or `null` switches off a daily limit, the trade and order counts, the minimum reward:risk or the minimum order;
`null` switches off an entry-window bound. A missing key is `MISSING_DATA`. `paper` (only `true`) and `broker` (only
`"alpaca"`) are optional. `ticket place` needs `"paper": true` in the very policy file the risk check used, so put
it in before the check when the user wants paper orders ([paper-orders.md](paper-orders.md)).

## Step 2: reconcile first

The limits only hold if every order placed is counted. Before the check, ask the user what their broker shows
for today and the last few days, and write it into the account statement as they placed it (`symbol`, `side`,
`status`: placed, filled, denied, failed or declined, `date`; optional `kind`, `max_loss`, `qty`, `stop`, and
`ticket` when it was a TraderOS ticket). Never assume a fresh account, and never fill in an order they did not
tell you about. A signed-off ticket with no outcome counts as placed today, and so does an unknown loss: both
block new entries until the statement says what happened.

## Step 3: size and check

```bash
traderos --json risk assess --plan plan.json --policy policy.json --account account.json --prices prices.json
```

For a market entry, put the user's latest price in the prices file with the time they read it; without a fresh
one the check blocks the entry. Only a price they give you: never one you looked up or remember.

Whole shares only. The size is the smallest of four limits:

| limit | shares |
| --- | --- |
| max loss | `max_loss_per_trade_usd` / risk a share (entry minus stop) |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_usd` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the settled cash less reservations in the account statement |

The entry it sizes at: the limit price with `--limit`. For a market order, the plan's entry, or the price in the
prices file if it is fresh (under 15 minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

Two kinds of refusal, and neither drafts a ticket:

- **The plan or the policy cannot be checked**: exit 3, with a code, recorded in the journal as a refused attempt.
  The plan has no date (INVALID_INPUT), the plan is for another day (DATA_STALE: make a new one), the stop is not
  below the entry or the target is not above it (INVALID_INPUT: the move already happened), the plan is a short
  (OUTSIDE_CAPABILITY), the ticker is on the blocked list (NOT_AUTHORIZED), or a limit is 0, negative or not a
  number (INVALID_INPUT: a bad limit is a refusal, never "no limit").
- **The plan is checked and fails**: exit 0, with `ticket_eligible` false and a no-trade record. The size is zero
  shares, the order is under the minimum, reward:risk is under `min_reward_risk`, or a check below BLOCKs.

Say the reason in one line, either way.

### Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go short. An **exit** only reduces what you
already hold. Getting out must always be possible, so an exit is checked for format, asset and short sales
only; every other rule turns into a warning for an exit, never a BLOCK. `risk assess` checks an entry plan; an exit
is a flatten ticket ([paper-orders.md](paper-orders.md)).

### The checks

Orders are checked as if each passed order had filled, so a batch that is fine order by order but breaks a limit
together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol, side not buy or sell, both or neither of qty and notional, a notional order that is not market + day (Alpaca's rule), a limit order with no limit price; a bracket that is not whole shares, not `day` or `gtc`, or misses its stop-loss or take-profit |
| asset | an option symbol while `allow_options` is false; crypto or anything that is not a US stock or ETF ticker |
| short | a sell of more than they hold (`allow_short` false) |
| blocked | (entries) the ticker is on the `blocked` list |
| size | (entries) under `min_order_usd` or over `max_order_usd` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need margin; a sale's unsettled proceeds do not fund a buy |
| stop | (entries) no stop (`NO_PROTECTION`), or a stop on the wrong side of the entry |
| loss | (entries) qty x (entry minus stop) over `max_loss_per_trade_usd` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_usd`; or a placed entry today has an unknown max loss, whatever the daily limit |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` or after `no_entries_after`, or the market is closed |
| pdt | (entries, only with `pdt_guard` true) 3 day trades already in the last 5 business days |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |
| exposure | (entries) no account statement, or a signed-off ticket with no outcome: `UNKNOWN_EXPOSURE` |
| price | (entries) a market entry whose latest price is stale, future-stamped or untimed |
| calendar | a day outside the exchange calendar's coverage: `CALENDAR_UNKNOWN` |

How the numbers work:

- **The entry it measures from**: the limit price for a limit order. For a market order, the HIGHER of the plan's
  entry and a fresh price from the prices file (under 15 minutes old). A plan written at 10:15 cannot understate
  the risk at 10:55 after the price ran up.
- **Daily loss** counts the full max loss of every entry placed today, win or lose, and of every signed-off ticket
  with no outcome yet. Three full-size trades use up a $150 day. An unknown max loss makes the total unknown, not
  zero: new entries are blocked until the account statement has the real order and its stop. Never invent a risk
  amount to unblock it.
- **The window** is New York time, worked out by TraderOS, never from the computer's clock zone. Weekends, NYSE
  holidays and outside the regular session always block an entry, even with both `no_entries_*` set to null. On a
  1:00 p.m. early close, a `no_entries_after` past the early close moves up with it.
- **A verdict expires**: `valid_until` is the earlier of 15 minutes from the check and, for an entry, the
  (early-close-adjusted) `no_entries_after` cutoff or the close. A ticket cannot be drafted from an expired or
  superseded assessment: run the check again.
- **pdt_guard** (off unless they say so) counts a day trade as a symbol bought and sold on the same day, plus any
  entry placed today. Note: FINRA removed the $25,000 pattern-day-trader minimum (Regulatory Notice 26-10,
  effective June 4, 2026; firms may phase in until October 20, 2027), so some brokers still apply the old rule:
  turn `pdt_guard` on if theirs does.

The four risk measures: **planned loss** (the max loss if the stop fills at the stop), **stressed loss** (if it
gaps through the stop by `--stress`), **theoretical worst case** (the position to zero) and **existing exposure**
(known, or UNKNOWN). Say all four.

## Step 4: tell the user

Show the four limits, which one decided the size, the shares, the dollars and the max loss at the stop. Say plainly
that a stop is an order, not a promise: in a fast market it can fill below the stop. Then the check, in plain
words: the max loss at the stop, reward:risk, how much of today's limits it uses, the four measures and until when
the check is valid. For a BLOCK, give the reason and the fix (fewer shares, a closer stop from the Exit Planner,
wait for the window, or skip the trade). Never change a limit yourself.

If the plan changes in any way, its entry, stop or target included, run the check again: a ticket is tied to the
exact plan and assessment, so an edited plan is not ready until it passes again, and a check from yesterday does
not count today. When it passes and the user wants the paper ticket: [paper-orders.md](paper-orders.md).

## Example: the reel

TSLA on Monday 2026-10-19 (a plan file needs no market data, so this runs on any store, with
`--now 2026-10-19T10:15:00-04:00` and a prices file). Opening-range breakout: entry 364.18 at 10:15 AM, stop 358.32
(the opening-range midpoint), target 370.04 (1R). Risk 5.86 a share. The example policy ($5,000 budget, 10% cash
buffer, $50 max loss) and an account statement with $5,000 settled cash and no orders:

```
Limits (whole shares; the smallest one wins):
  max loss      $50 max loss / 5.86 risk a share                             8 shares  <- binds
  position cap  75% of $5,000 = $3,750 / 364.18                             10 shares
  max order     $4,000 / 364.18                                             10 shares
  cash          budget $5,000 minus the $500 cash buffer = $4,500 / 364.18  12 shares

Size: 8 shares of TSLA = $2,913.44 at 364.18. Max loss at the stop: 8 x 5.86 = $46.88.

PASS  #1 buy TSLA 8 shares, entry
          max loss $46.88 (limit $50), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, $0 of $150 daily loss
```

With the stop at 300.00 instead, one share would risk 64.18, more than the $50 max loss: refused. At 10:52 the real
quote was 366.48, so the same plan sizes to 6 shares at 8.155 a share, and reward:risk falls to 0.44: refused,
because the move it was waiting for has mostly happened. At 9:40 it is blocked by the window (no new entries before
9:45).

## Rules

- Proposals and checks only. Never size money the user has not said is for this: ask for the budget and the max
  loss.
- A PASS only means the plan may be shown to the user as a ticket. It is not a yes.
- Every number comes from the policy, the plan, the account statement or the prices file. Nothing is guessed.
- No projections: never say what a trade would make. A target is a plan, not a forecast.
- The broker's price is the one that fills, and a stop can fill worse than its price in a fast market.
- Sizing a list of weights (weights mode) is the Portfolio Rebalancer (S21), not in this build.

## What it never does

It never places, approves or schedules an order (a paper order is only `ticket place`, after the user's own yes
for that ticket: [paper-orders.md](paper-orders.md)), never edits a plan to make it pass, never raises or loosens a
limit on its own, never shrinks a stop to fit more shares, never blocks a sell that only gets the user out, never
sizes options, margin or short positions, and never treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper only. TraderOS refuses live trading in its code, and no setting here changes that.
- Nothing in TraderOS places an order except `traderos ticket place`, to the user's Alpaca paper account, and
  only when all three hold: the risk check passed that exact ticket and has not expired, the user typed an
  explicit yes for that one ticket in the chat, and the user allowed that one run at Claude Code's own permission
  prompt ("Allow once"). Never put `traderos ticket place` on an allow list, and never tell the user to.
- No options, margin, short selling or leverage: options are deferred, and this build plans long stock trades only.
- Broker keys live in the user's own environment or TraderOS's settings, set by them in their own terminal, never
  in the chat.
