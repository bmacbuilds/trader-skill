# Chart Scanner

Needs S01 (Chart Scanner) usable.

What it needs: daily bars for `--setups daily` and for criteria on `1d` bars, so those run on the tutorial dataset
(its symbols are in `traderos --json data list`). `--setups orb` needs 5-minute bars, the person's own CSV: on the
tutorial it is refused (INVALID_INPUT): say so.

The Chart Scanner is the first step of a day trade. It reads the charts in the user's stored data and lists the
ones that match a rule. That is all it does. A scan is a list of charts that match a rule, not a list of stocks to
buy, and a setup on a chart is not a prediction. Most people who day trade lose money; the scanner does not change
that. It only saves them from flipping through charts by hand.

```bash
traderos --json scan --setups daily --dataset DS-...
traderos --json scan --setups daily --symbols TUTMKT,TUTTEC,TUTSML --dataset DS-...
traderos --json scan --setups orb --dataset DS-... --as-of 2026-10-19T14:15:00Z
traderos --json scan --setups orb --dataset DS-... --until 15:30 --or-minutes 15 --min-price 5
traderos --json scan --criteria criteria.json --dataset DS-...
```

Flags: `--min-price` and `--min-volume` (daily 5 and 1,000,000; orb 5 and 0), `--or-minutes 15`, `--until 15:30`
(orb: the last New York time for new entries; later is expired), `--symbols` (default: every instrument in the
dataset), `--as-of` (RFC 3339) to read the data as it was then. `--dataset` can be left out when the store holds
one dataset.

## What it reads and writes

| what | where |
| --- | --- |
| bars | a dataset the user imported: daily bars for `--setups daily`, 5-minute bars for `--setups orb` |
| criteria | `--criteria FILE`: their own `scan-criteria/1` rules, in the same condition grammar as a strategy |
| the result | a `scan/1` record (`SC-`) in the local store: each symbol, eligible or not, with the reason and every condition's numbers |

A criteria file:

```json
{"schema_version": "scan-criteria/1", "criteria_id": "CRIT-1", "name": "pullback in uptrend",
 "universe": ["TUTMKT", "TUTTEC", "TUTSML"], "interval": "1d", "max_staleness_bars": 1,
 "conditions": {"all": [{"above": ["close", {"sma": 200}]}, {"below": [{"rsi": 2}, 10]}]}}
```

## The daily setups

The liquidity floor comes first (last price at least `--min-price`, last session's volume at least `--min-volume`);
a name under it is not scanned. Then, on daily closes:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one. Hits are listed most traded first (price times
volume).

## The opening-range breakout scan

The opening range is the first `--or-minutes` (15) of the session: the 9:30, 9:35 and 9:40 five-minute bars. Range
high = the highest high of the range bars, range low = the lowest low. A hit is the first completed 5-minute bar to
CLOSE above the range high, at that bar's close time. A chart is usable only when every 5-minute bar from 9:30 to
the cutoff is there; a missing bar means a halt or a thin stock. A bar counts only once the store has made it
available. After `--until` the setup has expired.

## Steps

1. Before the open: `scan --setups daily` on their daily dataset. Tell the user the counts (scanned, liquid,
   hits) and the top few by setup.
2. After the opening range (10:15 is the classic): `scan --setups orb` on their 5-minute dataset as of that time.
   Tell the user how many usable charts broke their range and when, and that a later hit is closer to the price
   now.
3. The user picks one chart. Hand it on: [entry.md](entry.md) (the entry bar by bar). Never pick for them.

If nothing qualified, the result says `NO_ELIGIBLE_SETUP`: say exactly that. Never offer a near miss as a candidate.

## Example: the reel

Monday Oct 19 2026, a scan at 10:15 AM over the reel's 234-ticker wall found 42 of 231 usable charts closed
above their opening-range high by 10:15 AM (41 with the $5 floor). TSLA's range is the 9:30 to 9:40 bars, high
362.18, low 354.47; the 10:10 bar closed at 364.18, 0.55 % over the high, at 10:15 AM. The user picks TSLA, and
Claude hands it to the entry timer.

## Rules

- Say what was scanned: how many symbols, how many were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run on newer data before acting on it.
- The user can see why each symbol qualified: give the conditions and their numbers when asked.

## What it never does

It never places, sizes or checks an order, never says which stock to buy, never calls data real time, and never
promises that a setup works.

## Safety

- Education and tooling, not financial advice. A match is a rules check, not a recommendation.
- Paper only. TraderOS refuses live trading in its code, and no setting here changes that.
- Nothing in TraderOS places an order except `traderos ticket place`, to the user's Alpaca paper account, and
  only when all three hold: the risk check passed that exact ticket and has not expired, the user typed an
  explicit yes for that one ticket in the chat, and the user allowed that one run at Claude Code's own permission
  prompt ("Allow once"). Never put `traderos ticket place` on an allow list, and never tell the user to.
- No options, margin, short selling or leverage: options are deferred, and this build plans long stock trades only.
- Broker keys live in the user's own environment or TraderOS's settings, set by them in their own terminal, never
  in the chat.
- The paper ticket and its gates: [paper-orders.md](paper-orders.md).
