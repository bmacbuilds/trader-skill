# Entry Timer

Needs S13 (Entry Timer) usable, and the symbol's 1-minute or 5-minute bars in the store: the person's
own CSV ([data.md](data.md), `--interval 5m`). The tutorial has daily bars only, so on it the Entry Timer has
nothing to time: say so. Research only.

The Entry Timer turns a setup into one moment: the bar that confirms it. It reads the user's stored bars,
1-minute or 5-minute, and only bars that have finished. A price that pokes above the high in the middle of a bar is
not an entry. A bar that CLOSES above it is. Then it says the time and the price, and stops. It never buys anything.
Day trading loses most people money, and a clean signal does not change that.

```bash
traderos --json entry TSLA --setup orb --dataset DS-...
traderos --json entry TSLA --setup orb --dataset DS-... --as-of 2026-10-19T14:15:00Z
traderos --json entry TSLA --setup level --level 371.20 --dataset DS-...
traderos --json entry TSLA --setup orb --min-volume-x 1.5 --or-minutes 15 --dataset DS-...
```

Without `--setup` it answers whether the symbol is session-eligible now. `--as-of` (RFC 3339) replays a moment of
a past day on the stored bars. `--dataset` can be left out only when the store holds one dataset.

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes: the 9:30, 9:35 and 9:40 five-minute bars, or
  fifteen 1-minute bars. The level to beat is the range high. A range missing a bar is refused.
- **Level.** A price the user gives, like a 20-day high from the scanner.
- **The trigger** is the first completed bar after the range that CLOSES above the level. A wick above is not a
  trigger. A close exactly at the level is not above it.
- **The time** is that bar's close time (the 10:10 bar closes at 10:15 AM). The price is its close, to the cent.
- **Only closed bars count**, and only once the store has made them available.
- **Volume, if they want it.** `--min-volume-x 1.5` also needs the trigger bar's volume at 1.5 times the range
  bars' average. The 9:30 bar holds the opening auction, so 1.0 is already strict.
- **Time limits.** No entries before 9:45 or after 15:30. On a 1:00 p.m. early close the cutoff moves up with the
  close, to 12:30.
- **What it says.** Triggered (eligible at the trigger bar's close), waiting or expired (not eligible, with the
  reason, and how far the last close is from the level), or unavailable (no bars, stale bars, or a broken range).
  Before 9:30: not open yet. Before 9:45: the range is still forming. Weekends and NYSE holidays: the market is
  closed. Each of those is not eligible, with the time the range is set or the next open.

## Steps

1. Get the symbol from the user, or from a scan ([scanner.md](scanner.md)).
2. Run `traderos --json entry SYMBOL --setup orb --dataset D`. Tell the user the status in one line: waiting and
   how far, triggered at what time and price, or expired.
3. It reads the stored data, so it is as fresh as the user's last import. To check again later, they import the
   newer bars and you run it again. Never run it from a scheduled or background task that could lead to an order.
4. When it triggers, say the time and the price, and that nothing was placed. If the trigger is more than a bar
   old, say that too: a late entry is a different trade.
5. Hand off to the Exit Planner to set the stop and the target BEFORE anything is bought: [exit.md](exit.md).

## Example: the reel

TSLA, Monday 2026-10-19, 5-minute bars. The opening range is 354.47 to 362.18. At 10:12 the 10:10 bar is still
open, so it waits: the last close is $361.68 (10:05 bar), $0.50 under the range high. At 10:15 the bar has closed:
triggered at 10:15 AM at $364.18, the 10:10 bar (bar 8) closed above the range high $362.18 (+0.55%). Nothing was
placed. With `--min-volume-x 1` the 10:10 bar is too thin (0.60x the range's volume) and the trigger moves to
10:35 AM at 365.75 (the 10:30 bar, 7.98x).

## Rules

- Bar by bar, on the user's stored 1-minute or 5-minute bars, never tick by tick and never real time.
- A signal is information, not an order, and not a promise. Nothing here promises winning trades or money.
- Long side only. If the user wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that is the Exit Planner), never moves the
level after the fact, and never counts a bar that has not closed. It never leads to an order from a background
task: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. A trigger is a rules check, not a recommendation.
- Paper only. TraderOS refuses live trading in its code, and no setting here changes that.
- Nothing in TraderOS places an order except `traderos ticket place`, to the user's Alpaca paper account, and
  only when all three hold: the risk check passed that exact ticket and has not expired, the user typed an
  explicit yes for that one ticket in the chat, and the user allowed that one run at Claude Code's own permission
  prompt ("Allow once"). Never put `traderos ticket place` on an allow list, and never tell the user to.
- No options, margin, short selling or leverage: options are deferred, and this build plans long stock trades only.
- Broker keys live in the user's own environment or TraderOS's settings, set by them in their own terminal, never
  in the chat.
- The paper ticket and its gates: [paper-orders.md](paper-orders.md).
