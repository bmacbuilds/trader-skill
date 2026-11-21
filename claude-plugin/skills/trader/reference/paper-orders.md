# Paper tickets and paper orders

Needs S17 (Order Executor) usable. The owner's ruling (2026-11-20): paper orders, paper accounts only, live
refused.

This is the only part of TraderOS that can lead to an order, so it is the strictest. A ticket is drafted from a
passing risk check, and the user's explicit yes or no is recorded word for word. Then `ticket place` sends that one
approved ticket to the user's own Alpaca **paper** account, through TraderOS's paper client, which can reach only
Alpaca's paper endpoint. Live trading is refused in TraderOS's code. Drafting, showing, approving, declining,
recording a result and the log need no keys and no network; `place`, `account` and a fill read from the paper
account need the user's own Alpaca paper keys (doctor's `alpaca paper` line). A ticket the user would rather enter
at their own broker is theirs to copy: TraderOS sends only to an Alpaca paper account.

A paper order reaches Alpaca's paper account only after three separate gates, every time, for every ticket:

1. **The risk check passed this exact ticket and has not expired** (READY). See [risk-budget.md](risk-budget.md).
2. **The user typed an explicit yes for this one ticket** in the chat, recorded word for word by TraderOS.
3. **The user allowed that one `traderos ticket place` run** at Claude Code's own permission prompt.

Two yeses from the user, one PASS from the rules. No yes, no order.

```bash
traderos --json ticket RA-...
traderos --json ticket show PT-...
traderos --json ticket approve PT-... --reply "yes"
traderos --json ticket decline PT-... --reply "no"
traderos --json ticket place PT-... --policy policy.json
traderos --json ticket result PT-... --status placed --broker-id ORDER-ID --filled-qty 8 --filled-price 364.20 --account account.json
traderos --json ticket fill PT-... --account account.json
traderos --json ticket flatten TSLA --policy policy.json --account account.json --prices prices.json
traderos --json ticket account
traderos --json ticket log
traderos --json ticket log --all
```

`ticket sign-off` is the older name of `ticket approve`: it records the same yes, and `place` accepts it. `--now`
pins the clock for drafting, show, approve and decline (RFC 3339 with its zone); `place` always uses the real clock.

## What it reads and writes

| record | what |
| --- | --- |
| the assessment (`RA-`) | the risk check a ticket is drafted from; only the latest, unexpired PASS can become a ticket |
| the ticket (`PT-`) | the plain ticket: side, shares, order type, stop-loss, take-profit, max loss, the four risk measures |
| the journal | every draft, yes, no, placed, denied, failed and filled, append-only, with each entry's max loss |
| the account statement | the user's file ([risk-budget.md](risk-budget.md)); `ticket fill` and a `ticket result` with a fill update it |

## Setting up paper orders

1. The user makes an Alpaca account and creates **paper trading** API keys in the Alpaca dashboard. Paper keys
   are separate from live keys.
2. The user puts the keys where TraderOS reads them, themselves, in their own terminal, outside this chat: the
   environment variables `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`, or `traderos config set alpaca_paper_key_id`
   and `traderos config set alpaca_paper_secret_key`, which keep them owner-only in the TraderOS folder. Never run
   those for them. **Keys never go in the chat.** Never ask for them, never read a file that holds them back into the
   conversation, and if the user pastes a key anyway, tell them to delete that message and make new keys in the
   Alpaca dashboard.
3. Their policy says `"paper": true` ([risk-budget.md](risk-budget.md)).
4. Check it: `traderos --json ticket account` reads the paper account's status (read only). TraderOS refuses an
   account, a key or an address that is not Alpaca paper.
5. **Keep the permission prompt.** Never put `traderos ticket place` on an allow list. In `.claude/settings.json`
   keep it on ask:

```json
{"permissions": {"ask": ["Bash(traderos --json ticket place:*)", "Bash(traderos ticket place:*)"]}}
```

   If the user has allowed it, say that one of the three gates is off and ask them to remove it.

## Steps: the entry

1. Reconcile first. The limits only hold if every order is counted, so before the risk check ask what the broker
   shows for today and write it into the account statement ([risk-budget.md](risk-budget.md), step 2). Anything
   TraderOS sent that has no result yet gets one recorded first: an order at Alpaca with no placed record,
   `ticket result PT-... --status placed --broker-id ID --account account.json`; a fill with no record,
   `ticket fill`; a yes Alpaca never got, `--status denied` or `failed`. Until a result is recorded, the risk check
   counts a yes as a placed order and blocks new entries.
2. `risk assess` with the policy that says `"paper": true` ([risk-budget.md](risk-budget.md)). Then `ticket RA-...`
   drafts the ticket from the latest passing assessment, and `ticket show PT-...` shows it with the exact body
   TraderOS would send. Only READY tickets go to the user. A ticket that is NOT READY (expired, or superseded by a
   newer assessment) or BLOCKED goes back to the risk check.
3. Show ONE ticket and ask: "Place this paper order? Reply yes or no." One ticket per question. Never offer "yes to
   all".
4. Record the reply: `ticket approve PT-... --reply "<the user's reply, word for word>"`. If it refuses with
   `INVALID_APPROVAL`, the reply was not an explicit yes for this ticket (for example "sure", "yes to all", a yes
   that names another ticket, or a different amount). Ask again. Never turn a reply into a yes yourself. A no:
   `ticket decline PT-... --reply "<their reply>"`.
5. Only after the yes is recorded: `ticket place PT-... --policy policy.json`, with the same policy file the risk
   check used. Claude Code's permission prompt appears; the user allows that one run, or denies it.
6. Record what happened, every time, before anything else. `place` records the result itself. If it answers that
   the outcome is unknown (a timeout), it never means "not placed": ask the user to look at their Alpaca paper
   account, then record it with `ticket result`. A failed order is not retried without a new check and a new yes.
   The journal keeps the entry's max loss for the daily limit, and a yes with no result counts as placed there until
   one is recorded.

If the user decides not to place an approved ticket, record that: `ticket result PT-... --status denied`. If they
enter it at their own broker instead, their account statement records what happened to it (an order with
`"ticket": "PT-..."`). Until one of these is recorded, the approved ticket counts as placed today.

## Steps: while it is open, and out by the close

- Price alerts watch the stop, the target and the flat-by time ([alerts.md](alerts.md)). The bracket's own orders
  do the stopping.
- When the stop-loss or the take-profit fills, record it: `ticket fill PT-... --account account.json`.
- By `flat_by` (15:55 New York), anything still open gets closed:
  1. `ticket flatten TSLA --policy policy.json --account account.json --prices prices.json` builds the sell ticket
     for the shares held, checked as an exit: a sell that only closes what they hold is never blocked by the daily
     limits or the entry window.
  2. `ticket show`, then the user's yes for that one ticket, then `ticket place`, as above. The ticket says what
     happens to the bracket's open stop-loss and take-profit at Alpaca: read it to them.
  3. Record the result.

## Example: the reel

The reel's plan, sized and checked in [risk-budget.md](risk-budget.md) (8 shares, PASS), with the example
policy and `"paper": true`. `ticket RA-...` drafts it and `ticket show` prints (trimmed):

```
PAPER TICKET PT-8ce3807ec902b82cdbd7, PAPER account, Alpaca
  BUY 8 TSLA, market, good for today
  stop-loss 358.32
  take-profit 370.04
  max loss about $46.88 if the stop fills at 358.32 (it can fill worse in a fast market)
  planned loss 46.88 USD if the stop fills at the stop
  stressed loss 190.21 USD if it gaps through the stop
  theoretical worst case 2913.44 USD (the position to zero)
  or copy it into your own broker yourself: TraderOS sends only to an Alpaca paper account
  READY: risk check PASS, waiting for your yes
  TraderOS will send exactly this to Alpaca's paper endpoint (...):
  { the order's JSON body: symbol, side, qty, type, time_in_force, a bracket's take_profit and stop_loss }
  Reply "yes" to place this one order, or "no" to skip it.
```

Ask "Place this paper order? Reply yes or no." The user replies "yes". `ticket approve` records it word for word;
"sure" is refused, and so is "yes to all". Then, and only then, `ticket place PT-... --policy policy.json`, Claude
Code's prompt, and the user's allow for that one run. Without the user's paper keys `place` is refused before any
request is made, and nothing is sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys or passwords.
- Never sends to a live account. TraderOS refuses live in its code; there is no switch for it here.
- Never cancels, replaces or closes anything except what a ticket the user approved says it will.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

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
