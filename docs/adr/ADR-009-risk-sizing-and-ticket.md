# ADR-009: the upstream sizing, risk check and ticket document are kept; TraderOS wraps them in its records

- **Status:** proposed, 2026-11-20, for part C (OSDev4). Not yet reviewed. **Superseded in part the same day by
  ADR-012:** the owner ruled that the free product keeps the upstream paper orders (paper accounts only, live refused), so
  the order half of ticket.py that this ADR removes ("ticket.py (S17 in F1: a document)" below) is restored there,
  and the gate P items it defers are re-sorted there. Everything else here stands.
- **Revised 2026-11-20** after OSDev6's review of #10 (comment 7045037626, at 3035201): two blocking flaws, five
  medium and three low, each fixed and named below in "Review of #10", file by file. Where a row of the earlier
  tables changed, it points there. **Revised again** after his second round (comment 7050923565, at c5d95ae):
  one blocking item, one medium and six low, in "Review of #10, second round".
- **Owner rules (relayed by OSDev1 as OWNER posts, 2026-11-20):** "we're not going to rewrite any of the code just for
  the sake of rewriting it. If the code is perfect and it works, leave it as it is." and "Python files and all code
  only gets rewritten if there is clearly a flaw or if there is clearly a better method."
- **Spec:** §12.1 S15, S16, S17, S19, S21; §12.2 C; §13.2; §15.2, §15.6; §05.3; §04.3; §04.4; §00.2.
- **Sources:** `tank/day-trader-skill` at `3d0f4ef` (MIT; see NOTICE): `trader-budget/size.py` (+
  `config.example.json`, `targets.example.json`), `trader-risk/check.py`, `trader-broker/ticket.py`, their SKILL.md
  files, `tests/test_budget.py`, `tests/test_risk.py`, `tests/test_broker.py` and the fixtures they read. The
  community PRs applied in the review's order, each as the approved review (upstream PR review v3) says: #49 (take as
  is), #2 (with changes), #21 (with changes), #48 (as is), #55 (with changes). Two hunks needed a hand merge: #55's
  JSON `today` hunk next to #2's `unresolved_approvals` line, and both SKILL.md tables #2 and #21 edit at one line.
  With those merged, the upstream 118 tests (and 12 subtests) pass on the upstream code.

## Decision

Part C keeps the upstream rules and TraderOS keeps its records. `src/traderos/risk/` now holds:

| Module | What it is |
|---|---|
| `size.py` | The upstream plan-mode sizing: the four limits, the smallest wins, whole shares rounded down, the entry it sizes at, its refusals |
| `check.py` | The upstream `check()`: format, asset, short, blocked, size, position, cash, stop, loss, reward:risk, daily loss, trades, window, pdt, duplicates, the daily order limit, entries vs exits, the batch, `valid_until` |
| `tickets.py` | The document half of the upstream ticket: the ticket in plain words, an explicit yes for one ticket, its status |
| `policy.py` | The upstream config keys as the person's policy file, with no defaults |
| `inputs.py` | Strict readers for the policy, the account statement and a prices document |
| `weights.py` | The upstream weights mode, carried unchanged and not wired (S21 is P2) |
| `__init__.py` | The adapter: a `RiskPlan` in, the upstream sizing and check, the four measures, a `HypotheticalRiskAssessment`, a `PaperTicket` or a `NoTradeRecord` out |

Kept from the earlier TraderOS module, because the verdict keeps them: the four measures (planned loss at the stop is
the upstream max loss; stressed loss and the theoretical worst case are added to its), UNKNOWN_EXPOSURE and NO_PROTECTION,
typed codes, the NoTradeRecord (I05), strict file parsing, the journal (S19, below) and capability gating. Retired:
our sizing block (risk budget from equity x a fraction) and our two flaws it carried, O1 (a stated quantity bypassed
every limit) and O2 (no stated equity or cash meant no cap). The upstream rules fix both: the size always comes from its
four limits, and the budget's limits still cap a plan with no account.

## What changed from the upstream files, each with its reason

### size.py (S15)
| Change | Reason |
|---|---|
| `positive_number` refuses NaN and infinity; the message says "positive finite number" | Review change 6 to #2. A NaN max loss raised a traceback and kept the stale orders.json; Infinity raised OverflowError |
| "drop_stale on every failure" becomes "only the latest attempt can become a ticket", and every attempt, refused or failed, is recorded first | Review change 6 to #2. TraderOS has no orders.json to delete; without this, an eligible plan from ten minutes ago could still be ticketed after a newer one was refused or failed |
| Decimal money; exact prices, and the floor of the exact quotient (this row first read "shares in exact integer cents (the upstream `shares_for` already was)") | Spec 04.3. The integer cents were a flaw: see "Review of #10", items 1 and 3 |
| The contract multiplier: one contract risks (entry - stop) x multiplier and costs entry x multiplier | S15 "whole-contract sizing". With a multiplier of 1 every number is upstream's |
| The cash row is settled cash less reservations | S15 / RC17. Upstream read portfolio "cash", which its own fills credited with a sale's unsettled proceeds |
| No DEFAULTS: every limit comes from the person's policy; a missing one is MISSING_DATA | Spec 15.6. Demonstrated: `{"budget": 5000}` was sized by a $50 max loss nobody set |
| Typed refusals: short OUTSIDE_CAPABILITY, another day DATA_STALE, blocked NOT_AUTHORIZED, a missing limit MISSING_DATA, the rest INVALID_INPUT | Spec 05.3 |
| F1 wording; `bracket_order` is the order a paper ticket documents; the report no longer says "place_stock_order arguments" or hands over to trader-broker | Spec 00.2, 12.1 S17: no order path |
| Not carried: `main`, `run_plan`, `write_orders`, `drop_stale`, `read_json`, `load_config`, the clock helpers and `--budget` | The traderos CLI, the store and the policy file replace them; `--budget` would be a second source of a limit |

### check.py (S16)
| Change | Reason |
|---|---|
| An unresolved approval BLOCKs new entries (in F1: a signed-off ticket with no outcome in the account statement) | Review change 4 to #2 (P4) |
| A stale (or future-stamped, or untimed) price BLOCKs a market entry, even with a plan | Review change 4 to #2 (P5). It supersedes the review's third #48 case (PASS with a warning): the reuse plan's S16 item 1 is the later, settled call |
| Unknown recorded risk BLOCKs new entries whatever the daily limit, 0 and null included; exits are allowed | Review changes 1 and 2 to #55 (P6b); spec 05.3 |
| No account statement, or no log of the recent orders, is UNKNOWN_EXPOSURE | P7; spec 15.2, 12.2 C. Upstream assumed a fresh account funded with the budget and nothing placed |
| No stop is NO_PROTECTION whatever `require_stop` says; the policy refuses `require_stop: false` | P10. The upstream `false` let a stopless entry pass and left it out of the daily loss |
| Non-finite order fields and account numbers are refused (`_f` returns a finite Decimal under 10^15, or None) | P8 (`qty: "NaN"` passed: "BUY NaN shares"), P9 (a NaN cash turned the cash checks off). PR #55's 10^400 case stays unknown |
| A day outside the exchange calendar is CALENDAR_UNKNOWN, a refusal | P13. Upstream warned and let the entry through |
| An untimed price is not fresh | P16. The upstream `price_times=None` counted every price as fresh |
| Settled cash in the batch: a sale's proceeds do not fund a later buy; they are unsettled cash, still in the account's total | The upstream line 776 and its test at test_risk.py:547-555 (S15 / RC17). The first port took them out of the total too: see "Review of #10", item 7 |
| traderos.clock XNYS, not a 2026-2027 holiday table | The same flaw OSDev6 named in ADR-006: a hand-kept copy, blind outside two years. The upstream New York conversion rule is kept |
| Decimal; timezone-aware instants | Spec 04.3; contracts v1 (QT01) |
| The multiplier scales an order's value and loss | S15; without it a 100x contract passed 100x under its real size |
| F1 wording: no LIVE warning, no "paper" flag, no "check get_orders at the broker", no ticket.py / trader-broker hand-offs | Spec 00.2, 15 (verdict S16 item 12) |
| One `fingerprint`, imported by tickets.py | Upstream kept two byte-identical copies (one per skill folder) and a test that they stay identical |
| Not carried: `main`, `read_json`, `parse_now`, the risk.json writer | The CLI and the store |

### ticket.py (S17 in F1: a document)
Removed, every part of the order half: `tool_input` and its three uses (`render`, `ticket_json`, `cmd_approve`), the
IBKR lines and the account line of `render`, the "waiting for the place_stock_order call" label, `cmd_result`,
`cmd_fill`, `apply_fill`, `log_fill` and `cmd_flatten`; `claude_desktop_config.example.json` is not carried. Kept:
`plain_parts`, `plain`, `max_loss`, `amount_numbers`, `parse_yes` (now the sign-off of a document), `status_of`,
`risk_verdict`, `latest`, show, decline, log, `--now` and `ny_iso`.

| Change | Reason |
|---|---|
| A verdict with no readable `valid_until` counts as expired | Review change 1 to #21 (P11). #21 skipped the check when it was missing |
| The verdict is the stored assessment, bound by the plan hash | The store keeps every record (S19); the plan hash binds the whole plan, not only entry, stop and target |
| A newer assessment supersedes an older one | The upstream single orders.json and PR #2's drop_stale |
| No separate "from another day" rule | An entry's `valid_until` ends at the latest at the day's close (PR #21) |
| `write_json` and broker.json are the store's job; a decision is an append-only journal event | S19 stays ours (below) |
| A sign-off counts as placed today until the account statement records its outcome; an outcome naming the ticket inherits its qty, kind, max loss and stop | The upstream `approve` / `result` pair, with the review's change 4 to #2. In F1 the person, not a broker, says what happened |

`DOUBT_WORDS` is unchanged: the verdict marks tightening it as optional (N10) and names no reply it lets through.

### config.example.json and the SKILL.md files
The policy file takes the upstream config keys (`policy.EXAMPLE` is its example without `broker`, `paper` and
`sec_user_agent`). Strict like every input: numbers as strings or integers, no float literals. The SKILL.md files are
not carried as files: TraderOS has no skill folder yet (spec 13.1). Their rules are in the module docstrings and in
`docs/cli-contract-v1.md` ("Part C"). Not carried, per the verdict: trader-broker's set-up (Alpaca's MCP server, the
Interactive Brokers connector, tool permissions), its three gates and its order steps, and the "reconcile first with
get_orders and get_all_positions" steps of trader-risk and trader-broker. The originals stay in the tank.

### Weights mode (S21)
`weights.py` carries the upstream weights-mode functions unchanged (floats, its place_stock_order-shaped `orders_from` and
its report wording), with only the plumbing edits (SizeError's import; the CLI glue and its file reader are not
carried). It is **not wired**: no command, API function or skill calls it, and `traderos capabilities` lists S21 as
planned and unavailable (spec 12.1: S21 is P2). Before S21 is built, those parts get their review against S21
("non-executing target drift and constraint arithmetic") and 04.3. The upstream 15 WeightsMode tests run against it.

## Review of #10 (OSDev6, 2026-11-20, comment 7045037626)

The review measured the port against the upstream code rebuilt from the tank with the five PRs: outside the named changes,
the check and the sizing agreed with its on 17,000 and 17,483 random cases. It found two blocking flaws (1, 2), five
medium (3 to 7), three low (a to c), and three mutants that survived for want of a boundary test. Each fix below
changes the upstream code, or the first port's, only where it has a flaw the review demonstrated (the owner's rule,
above). The case in each row is OSDev6's, and `tests/unit/test_risk_review10.py` runs it.

### size.py
| Change | Flaw (OSDev6's case) |
|---|---|
| Prices are used exactly: no `round_cent` on the entry, limit, stop or target; the risk a share, the reward, the cost and the loss are exact Decimals, and only the money shown is rounded (item 1) | Upstream rounded every price to the cent, half up, before measuring. Entry 0.5049, stop 0.4851 at a $50 max loss: 0.50 - 0.49 = 0.01 a share, 5000 shares, eligible, a real loss of **$99.00** at the plan's stop. At multiplier 125000 (ours), 1.0849 / 1.0651 sized 4 contracts, a real loss of $9,900 on a $5,000 limit. Exact: 2525 shares, $49.995 |
| An order price off the tick is refused by `on_tick`, a typed `DataError` (INVALID_INPUT, exit 3). The tick is the plan's `tick_size` if stated, else whole cents at $1.00 and above and at most four decimals below (item 1) | The rounding also moved the person's stop on the ticket (0.4851 to 0.49, 1.0651 to 1.07): RISK-002, a repaired stop. Refused, never rounded: the person writes the price they mean. 1.0849 with no tick_size is refused; with `tick_size` 0.0001 it sizes 2 contracts, $4,950 |
| `shares_for` floors the exact quotient, `int(usd // per_share)` (items 1 and 3) | The upstream integer cents rounded the divisor half-even. Multiplier 12.5, entry 10.01, stop 10.00, $50: 0.125 a contract became 0.12 and sized 416 contracts ($52.00, over the cap; the check then refused a plan that fits 400). Multiplier 2.5: 2500 contracts, $62.50, where 2000 fit |
| Given the account (`account_total`, `held_value`), the cash row also keeps the buffer on the account's total, and the position cap is also measured on the account less what you hold (LOW b; named as a flaw, since the fix is small) | Upstream sized the cap and the buffer on the budget, while its check measures them on the account, so a sized quantity could fail its own check. Settled 3,413.44: 8 sized, then BLOCKed at 85.4%; now 7, which pass. With $20,000 of other holdings the buffer is $2,500, not $500 (12 sized and blocked; now 6). Holding 5 TSLA: 9, not 10 (80.1%, blocked). With an account that is only settled cash, at least the budget, every number is upstream's |

### check.py
| Change | Flaw (OSDev6's case) |
|---|---|
| The entry it measures from is not rounded (`_cent` removed); the risk a share and reward:risk are exact; `entry_ref` is reported as measured; a price prints exactly (`price_text`, at least two decimals) (item 1) | The same rounding as size.py: 0.5049 / 0.4851 measured 0.01 a share, so the check passed what the sizing mis-sized. The half-cent tolerances on the loss, the buffer and the cap are upstream's and unchanged |
| A position may state `multiplier` (default 1) and is worth shares x price x multiplier. A plan whose multiplier is not the holding's stated one, or a plan multiplier other than 1 on a holding that states none, is `exposure:` UNKNOWN_EXPOSURE (item 2) | Holdings were valued at shares x price. Multiplier 100, price 50, 10 contracts held, $20,000 settled, a 75% cap: eligible with 2 more, the holding valued at $500 instead of $50,000. Now 60,000 of 70,000 = 85.7%: BLOCK |
| A market entry with no usable price BLOCKs: no prices file, the symbol not in it, or a value that is not a positive number under 10^15. The reason is `price: no price for ...`, MISSING_DATA (spec 05.3: a missing quote is UNKNOWN) (item 4) | Only a stale or untimed price blocked (`sym in prices`). With no `--prices`, or a file without the symbol, a market entry was eligible, though "the plan's entry cannot show whether the price already ran through the stop" holds for no price too. A design gap in the first port; upstream passed both |
| A sale in the batch moves its value from the position to unsettled cash: the account's total is unchanged, and the proceeds are never spendable. A stated `unsettled_cash` is in the total the same way (item 7) | The first port took the sale's value off the position without adding it anywhere, so the total shrank, and the buffer with it. $5,000 settled and 10 TSLA: buy 48 KO alone BLOCKs ("leaves $674.72 cash, under your 10% buffer of $864.18"); sell the TSLA first and the same buy PASSed. Now it BLOCKs with the same words |

### tickets.py
| Change | Flaw (OSDev6's case) |
|---|---|
| `_p` prints a price exactly; `_money` shows a sub-cent amount as it is; `max_loss` uses the exact risk a share (item 1) | Upstream's "%.2f" and its cents moved a 0.4851 stop to 0.49 on the ticket and halved its max loss |
| `status_of`: a time before the ticket's risk check is NOT READY ("a yes cannot come before its check"), and nothing is recorded (item 6) | Assess at 2026-10-19T14:15Z, draft, then sign off with `--now 2026-10-18T14:15:00Z`: it was recorded, dated the 18th. The today-only unresolved-approval rule ignored it, so the next entry that day was eligible. Upstream's "from another day" rule refused it; this ADR dropped that rule because of `now > valid_until`, which does not cover `now < created_at` |
| `apply_fill(..., multiplier=None)`: a fill costs qty x price x multiplier, a new holding keeps `multiplier` (when not 1), and a fill at another multiplier than the holding's is refused (item 2) | Consistency: `ticket fill` writes the account statement (ADR-012), and the statement must state a holding's multiplier for the check to value it |

### inputs.py
| Change | Flaw (OSDev6's case) |
|---|---|
| `num` bounds every number: under 10^15 in size, the bound `policy.number` already puts on the policy. A larger one is INVALID_INPUT, exit 3 (items 4 and 5) | "1E+101" as a price was accepted, then dropped as unusable, so the market entry had no price and passed. `reserved_cash` "1E+98" crashed later in a quantize (exit 4) |
| A position may state `multiplier` (positive) (item 2) | See check.py |
| An order's symbol and ticket id in the account statement must be non-empty strings (`text`) (LOW c). Its `at`, `plain` and `note` are read as upstream read them (second round: this row first applied `text` to them too) | A symbol or ticket of 5 was turned into "5", and a list crashed. It failed safe, but a refusal names the key |

### `__init__.py` (the adapter)
| Change | Flaw (OSDev6's case) |
|---|---|
| The ticket carries the plan's exact limit, stop and target (item 1) | `round_cent` moved them, so the ticket's stop was not the stop the risk was measured at |
| The sizing is given the account's total and what you hold, taken from `check.account` so that both value a holding the same way (LOW b) | See size.py |
| `check_plan_fields` refuses a RiskPlan that states a v1.0 field the assessment no longer reads (`quantity`, `account_equity`, `settled_cash`, `reserved_cash`, `risk_per_trade`, `existing_exposure`, `whole_units=False`, `prices_observed_at`). It also refuses a currency other than USD (OUTSIDE_CAPABILITY) (LOW a) | `api.assess_hypothetical_plan` ignored them silently, while the CLI refused them as keys. It also labelled USD limits in the plan's currency ("46.88 EUR") |
| `split_account` passes a stated `unsettled_cash` to the check (item 7) | See check.py |

### commands/decisions.py
| Change | Flaw (OSDev6's case) |
|---|---|
| The plan's numbers and `--limit` go through `inputs.num`, and the symbol through `inputs.text`; `tick_size` is a plan key (items 1 and 5, LOW c) | `--limit 1E+99` and a plan entry "1E+99" exited 4 (`decimal.InvalidOperation`); a plan symbol of 5 became "5" |
| An `ArithmeticError` during the assessment is a typed refusal (INVALID_INPUT, exit 3), recorded as `journal.plan.failed`, so it supersedes the older assessment (item 5) | Only `TraderOSError` was caught. The untyped failure was not recorded, and the earlier eligible assessment could still be drafted (`ticket` exited 0) |

### commands/paper.py
| Change | Flaw |
|---|---|
| `ticket place` refuses a ticket sized at a multiplier other than 1 (OUTSIDE_CAPABILITY, nothing sent); `ticket result` and `ticket fill` pass the ticket's multiplier to `apply_fill` (item 2) | Consistency with item 2; not one of its cases. An Alpaca paper order is a number of shares, so 2 contracts of 100 would have been sent as 2 shares, a hundredth of what was approved |

### contracts/risk.py
| Change | Reason |
|---|---|
| `RiskPlan.tick_size` (contracts v1.3: additive, optional, positive, in `OMIT_IF_NONE`) (item 1) | The plan states the instrument's tick, so an order price off it is refused rather than rounded. Every earlier RiskPlan keeps its bytes and hash; the golden example does not set it |

### What the review found that this does not change
- The half-cent tolerances (`CENT = 0.005`) on the loss, the buffer and the cap, and the 15-minute fresh window, are
  Upstream's and stay. His three surviving mutants are now pinned by boundary tests:
  - a $50.01 loss over a $50 max loss blocks, and $50.00 passes;
  - a quote 15:00 old is fresh, and 15:01 is stale;
  - a buy leaving $499.99 against a $500 buffer blocks, and $500.00 passes.
- A hand-written orders file given to `check.check` directly is not tick-checked. Its prices are measured exactly,
  so the risk is right, and the order the sizing writes is always on the tick.
- The upstream 366.475 example quote is now measured as quoted: 8.155 a share, not 8.16. The quote is not an order price,
  so it is used exactly and never rounded. The size is unchanged (6 shares), at $48.93 to the stop instead of
  $48.96; the check's 8 shares read $65.24, not $65.28.

## Review of #10, second round (OSDev6, 2026-11-20, comment 7050923565, at c5d95ae)

He re-ran items 1 and 3 to 7, LOW a to c and the three pinned survivors, and found them fixed. One blocking item
reopened item 2 through a door older than the port; the rest are a medium and six lows. The tests are in
`tests/unit/test_risk_review10.py` ("the second round") and `tests/unit/test_paper_orders.py` (the paper side).

### check.py
| Change | Flaw (OSDev6's case) |
|---|---|
| For an entry, a holding with no price and no stated cost (no lots) is `exposure:` UNKNOWN_EXPOSURE: its value, and the account's, is unknown. `account()` lists it in `unknown`; a holding with lots is still valued at their cost, as upstream did, and a holding of 0 shares is worth 0. An exit only warns (BLOCKING) | The upstream `account()` valued such a holding at its cost basis, the sum of no lots: **$0**, and a limit order needs no prices file. 10 XYZ × 100 held, $20,000 settled, `--limit 50.00` buy 2: exit 0, eligible, "valued at its cost basis $0", at a true $60,000 of $70,000 = 85.7% over a 75% cap. At multiplier 1, 10 TSLA held and a limit buy sized 8: 75.9%. It contradicts this ADR's own rule that an empty value is never zero |
| When the account holds the plan's own symbol, an entry needs its price even for a limit order: `price: no price for X: you hold it ...`, MISSING_DATA (BLOCKING) | The position cap and the buffer are measured on what you hold; a limit order's own price does not value the holding. With the price, the XYZ case leaves no room for a contract (75% of $70,000 less $50,000), and the TSLA case sizes 7, which pass at 71.6% |

### `__init__.py` (the adapter)
| Change | Flaw (OSDev6's case) |
|---|---|
| `flatten` refuses a holding whose account statement states a multiplier other than 1 (OUTSIDE_CAPABILITY), and says to close it at the broker and record the fill by hand (MEDIUM) | `paper.py:67`'s gate reads only a plan's multiplier, and a flatten ticket has no plan: 10 contracts of 100 became a paper sell of 10 shares, and its fill would have booked $500 of unsettled cash for a $50,000 holding |
| `assess` turns an `ArithmeticError` into the typed refusal the CLI gives (`out_of_range`, INVALID_INPUT) (LOW) | `api.assess_hypothetical_plan` with a `tick_size` or multiplier of 1E-99 raised `decimal.InvalidOperation` (DivisionImpossible); only the CLI caught it |

### tickets.py and commands/decisions.py
| Change | Flaw (OSDev6's case) |
|---|---|
| `contract_multiplier(order)`: one definition of a ticket sized in contracts, used by the place gate, the ticket and the yes. Such a ticket says "TraderOS will not send this ... `traderos ticket place` refuses it", shows no body, and asks for a yes to approve, not to place (LOW) | A contract ticket said "TraderOS will send exactly this", though `place` always refuses it |

### commands/paper.py
| Change | Flaw (OSDev6's case) |
|---|---|
| `ticket place` cannot meet a flatten ticket for a contract holding: `flatten` refuses to make one (above), so the gate's multiplier check stays the entry ticket's | See the adapter. A second check in `place` would have nothing to read: a flatten ticket records no multiplier, and no ticket can carry one other than 1 |
| A fill without `--account` is a typed refusal (INVALID_INPUT, exit 3) before anything is recorded; `ticket result` with no fill needs no `--account` (from his review of #16) | `ticket result <id> --status placed --filled-qty --filled-price` with no `--account` crashed with exit 4 (`TypeError` on None) |

### settings.py
| Change | Flaw (OSDev6's case) |
|---|---|
| `settings.read` refuses a key other than the two settings (INTEGRITY_MISMATCH), naming it, never its value. `doctor` still reports: its "alpaca paper" check fails with that message, and `ticket place` refuses before any request (LOW, from before this diff; ADR-012's file) | `settings.json` kept unknown keys silently: a paper key and a hand-added `base_url` reached the paper account endpoint in his guarded run. It went to paper only and the stray key was ignored, but an ignored setting is a silent one |

### inputs.py
| Change | Reason |
|---|---|
| An order's `at`, `plain` and `note` are upstream's again: an empty note is accepted (LOW) | The first round applied `text` (a non-empty string) to them along with the symbol and the ticket. LOW c named only the symbol and the ticket, and nothing needed the others, so the change had no reason |

### Tests the first round's fixes lacked (LOW)
A contract ticket's fill keeps its multiplier (`ticket result --filled-qty 2 --filled-price 1.00` books $200 and
`multiplier: "100"`); a sub-cent amount shows as written ($0.4851, a limit order at $0.5049); a negative
`unsettled_cash` is refused by the reader and by the check. Item 6 is pinned at the second: a yes 1 second before its
check is refused and one at the check is recorded (its weaker 23-hour check is one of the mutants).

## S19: the journal stays ours
S19 has no upstream file: the upstream packs carry no trade journal; ticket.py's broker.json records broker decisions and
actual fills, which F1 does not have (spec 12.1 S19, 15.7). Plans, assessments, tickets, sign-offs, declines and
failed attempts are append-only memory events linked by id.

## Contracts v1.1 and v1.3 (additive)
- `RiskPlan` + `plan_date` (the upstream plan "date") and `limit_price` (its `--limit`); `HypotheticalRiskAssessment` +
  `valid_until` (#21), `policy_hash` and `account_hash`; `PaperTicket` + `target_price` (the take-profit leg).
- Each new field is optional, defaults to None and is listed in the class's new `OMIT_IF_NONE`, so it is left out of
  the canonical form while None. Every v1.0 record therefore keeps its bytes and its hash, which matters because the
  store re-hashes what it reads. The v1.0 golden files are kept in `tests/fixtures/contracts/v1.0/` and read back byte
  for byte (spec 05.1). The three golden files and HASHES.txt are regenerated.
- `api.assess_hypothetical_plan(plan, policy, *, account=None, prices=None, now=None, stress_gap="0.05")`: spec 04.4
  names `policy`. The v1.0 call without a policy is gone, on purpose: there is no default policy.
- Since v1.1, `traderos risk assess` leaves the RiskPlan's v1.0 account fields empty: the limits are in the policy,
  the account in the account statement, each bound by hash. Since the review of #10 the API refuses a plan that
  states them (LOW a).
- v1.3: `RiskPlan.tick_size`, optional and in `OMIT_IF_NONE` (review of #10, item 1). No golden file changes.

## CLI (docs/cli-contract-v1.md)
`risk assess --plan FILE` gains `--policy FILE` (required), `--account FILE`, `--prices FILE` (a market entry needs
it since the review of #10), `--limit [PRICE]` and `--now T`; the plan file gains `tick_size` and a position
`multiplier`; `ticket <assessment-id>` stays and gains `ticket show|sign-off|decline <ticket-id>` and `ticket log`.
The plan file drops the keys that moved (`equity`, `risk_per_trade`, `quantity`, `whole_units`, `settled_cash`,
`reserved_cash`, `existing_exposure`, `prices_observed_at`) and refuses each with where it went. `journal add|list`
is unchanged. PR #49 (as is) lives in `traderos.cli.main`, the one main() the scripts became.

## Deferred to gate P (paper trading), not F1
Review change 5 to #2 (bind the fingerprint to the broker account and paper or live; normalize Alpaca's numeric
fields) and review change 2 to #21 (revalidate immediately before submission). F1 has no account, no environment and
no submission (spec 15.1-15.3).

## Evidence
- The upstream tests are ported with its numbers (Decimals now): `tests/unit/test_risk_size.py` (budget, plan mode),
  `test_risk_check.py` (risk, and PR #55's ledger tests), `test_risk_tickets.py` (broker, the document half),
  `test_risk_weights.py` (WeightsMode). Where the port changes its answer on purpose, the test says so and names the
  change. Each test file names what it did not port and why (the scripts' own CLIs, the order half, the desktop
  config). The review's listed cases are there, and a regression test for each flaw above.
- `tests/unit/test_risk.py`: the adapter by hand (the reel: 8 shares, $46.88 planned, $190.21 stressed, $2,913.44
  worst case), the strict files, the CLI, and PR #49's cases in the traderos CLI (a cp1252 console, an error path, an
  in-process call).
- `tests/unit/test_risk_review10.py`: OSDev6's review of #10, a test (or more) per item with his numbers, and his
  three surviving mutants pinned at their boundaries. A market entry in the other tests now gets a fresh quote at
  the plan's entry (`tests/unit/quotes.py`), so it is measured as before (item 4).
- Mutation: `tools/mutants/risk.py`, run from the repository root (`python tools/mutants/risk.py [PYTHON]`), so a
  reviewer can rerun it. It holds the port's 76 mutants of the rules (sizing, policy, every check, the ticket rules,
  the adapter's codes and measures, the CLI's supersession and sign-off accounting) and, since the review of #10,
  49 more: one or more per fix, and OSDev6's three survivors with a neighbour each; and 16 for his second round, one
  or more per fix. Each is run with bytecode off against the risk and paper-order tests: 141 red, 0 survived. The port's "shares round up" (`-(-a // b)`) became an
  equivalent mutant on exact Decimals, since Decimal `//` truncates toward zero; it is now a ceiling.
- License: `src/traderos/risk/NOTICE` and `tests/fixtures/risk/NOTICE` carry the upstream MIT notice
  next to the code and the fixtures; the wheel ships the first.
