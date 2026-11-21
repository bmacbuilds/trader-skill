# Where this skill's text came from, and every change to it

The upstream SKILL.md files (MIT) are the starting text for each capability it has one for. The owner's rule
(2026-11-20, relayed by OSDev1): nothing is rewritten for the sake of rewriting it; text changes only where it must
fit TraderOS, where it is clearly wrong, or where the spec forbids it. Upstream's "Rules", "What it never does", step
lists and wording are kept otherwise, and its "the user" stays. Never represent the upstream author as an
endorser, sponsor or co-owner of TraderOS.

Its MIT notice is in `NOTICE` beside SKILL.md, so it travels with every copy of the skill (the project
skill and the plugin), and in the repository's `NOTICE`.

Upstream, as archived in the TraderOS tank (`tank/`, traderos-dev commit `92d084f`; `tank/PROVENANCE.csv` holds
each file's blob and SHA-256):

- `tank/day-trader-skill/`: upstream day-trader-skill at `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd`.
- `tank/trader-skill/`: upstream trader-skill at `5189e79b9a480755a3733ac9713f5353981dbe18`.
- `tank/prs/day-trader-skill/pr-NN/files/`: the community PRs at their heads, taken as OSDev9's upstream PR review
  (version 3) approves them: #2 `458e10f`, #21 `f6a95eb`, #22 `6d63b15`, #23 `ddd397d`, #24 `ef390ed`, #25
  `67a1d20`, #53 `1613327`, #55 `ce4cbb3`.

## Changes made to every ported file

| Change | Reason |
| --- | --- |
| `python3 "${CLAUDE_SKILL_DIR}/<tool>.py" ...` lines become the real `traderos --json ...` commands | TraderOS's tools are its CLI; the skill calls it and reads its structured result (spec 13.3). `tests/skill/test_skill.py` parses every command line against the CLI |
| `trader/*.json` files become the local store and the files each command reads | One store (spec 11); the commands' own arguments |
| Yahoo, Stooq, Nasdaq Trader, OpenFIGI and other download wording removed | Spec 06.1 holds Yahoo's unofficial endpoints; TraderOS reads the person's imported data and, for SEC tools, SEC EDGAR only; OpenFIGI is not an approved source (ADR-011) |
| Safety: the upstream five bullets are kept on every page whose upstream file has them (timemachine, scanner, entry, exit, alerts, risk-budget, paper-orders; holdings keeps copycat's own set), each adapted only as the next four rows say. An earlier port had dropped them from timemachine, scanner, entry, exit and alerts, and paper-by-default and gate 3 from holdings; they are restored | The owner's rewrite rule; OSDev6's reviews of #16 (7047949063, 7050691071) |
| Safety: the first bullet's "A PASS is a rules check" names what the page produces: a match (scanner), a trigger (entry), a plan (exit), an alert (alerts), a replay (timemachine) | Only the risk check produces a PASS |
| Safety: upstream's "Paper trading by default" line, which left live trading to the user's own config, becomes "Paper only; TraderOS refuses live trading in its code" | Owner ruling 2026-11-20: keep the upstream paper orders, paper accounts only, live refused |
| Safety: the three gates name `traderos ticket place` and Claude Code's permission prompt (upstream's "Allow once") instead of the MCP order tool's "Needs approval" card; "Never set an order tool to Always allow" becomes "Never put `traderos ticket place` on an allow list, and never tell the user to" | TraderOS sends a paper order itself through a paper-only client (#10, ADR-012); the MCP server chose paper or live from its own environment, outside code TraderOS can test |
| Safety: "No options, margin, short selling or leverage unless the config allows it" becomes "options are deferred, and this build plans long stock trades only" | S12 is deferred (spec 12.1); sizing refuses a short (ADR-009) |
| Safety: "Broker keys live in environment variables or the Claude Desktop config" becomes "in the user's own environment or TraderOS's settings, set by them in their own terminal" | No Claude Desktop MCP setup in TraderOS; the paper client reads `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY` or `traderos config set` (ADR-012) |
| "Paper-orders build", "document-only build" and "until Part B lands" qualifiers removed | Paper orders (#10) and the upstream part B (#7, #13) are in the release this skill ships with |
| Each page says what its commands need: daily or 5-minute bars, the network and the SEC identification, or the paper keys; examples that can run on the tutorial use its symbols | OSDev6's reviews of #16 (7047949063, 7050691071), MEDIUM 3: the film path is the tutorial, offline, with no keys |
| Hand-offs to `/trader-news`, `/trader-pelosi` and `/trader-tax` removed | S02 is deferred, S05 and S20 are on hold: never offered (spec 12.1) |
| Hand-offs to `/trader-x` become links to the matching reference file | One `/trader` skill with reference files (spec 13.1) |
| The upstream budget rule "never say what a trade ... make" and copycat's "copying a fund ... make money", both in the future tense, become "would make" and "makes" | The skill's no-promise test bans that future-tense phrase anywhere in the skill, so a promise cannot slip in as an edit to a prohibition |

## Per file

### `SKILL.md`, `data.md`, `strategy-lab.md`, `journal.md`, `bundles.md`, `market-pulse.md`

TraderOS's own text: upstream has no router, CSV import, typed strategy, Passport, research memory, journal, bundle or
market pulse. Sources: spec 01.1, 12.1, 12.2, 13.1-13.3; `docs/cli-contract-v1.md`; `docs/data-csv-v1.md`; the
docstrings of `traderos.strategies.rulebook`, `traderos.strategies.conditions`, `traderos.analyst`,
`traderos.transfer` and `traderos.commands.demo`. `market-pulse.md` follows OSDev6's PR #13 (`7988e8d`).

Changed after OSDev6's reviews of #16 (7047949063, 7050691071):

| Change | Reason |
| --- | --- |
| SKILL.md routes on the registry's `modes` and `prerequisites`, with a table of what each prerequisite needs and how to tell it is met | MEDIUM 3 |
| SKILL.md: the plan replay and the demo run without `--json`, and their verdict lines are quoted, never worked out | HIGH 2: the plan replay's verdict is only in the text output |
| SKILL.md step 5 and strategy-lab: a number goes in a plain statement, never in a negated or conditional sentence; SYNTHETIC_DATA's words go in a sentence with no number | LOW: check-claims voids every number in a sentence with a negation or a condition (#14's `_NEGATION`, `_CONDITION`) |
| SKILL.md step 5's example number is the tutorial Passport's own ("in validation, net return -1.07%") | It passes check-claims on the film's own Passport |
| Bundles: export, import and backup are "in a later release"; the router lists them under Never offered and offers nothing; `bundles.md` has no command | OSDev1's cut ruling (21:46): not in this release |
| The strategy example trades TUTMKT, TUTTEC and TUTSML, and its approval hash is that draft's (`44376b645159`) | MEDIUM 3: SPY, QQQ and IWM are not in the tutorial (MISSING_DATA) |
| The demo runs as text, and a second run in one store is described both ways: a replay as a new Passport revision (#22) or INTEGRITY_MISMATCH without it | MEDIUM 4 |
| Part B is described as usable, with what it needs, not as `CAPABILITY_DISABLED` | HIGH 3: #7 and #13 land before #16 |

### `timemachine.md`

From `tank/day-trader-skill/trader-timemachine/SKILL.md` (`3d0f4ef`) with PR #25, #53 and the Time Machine part of
#2, as already ported in `docs/timemachine-plan-replay.md` (ADR-010), whose changes carry over: no Yahoo, no fetch;
the method words say what the code does (whole-session replays, next-open fills, costs on each leg, a stationary
bootstrap); #53's unreproduced S&P 500 results and its line "Say this when the user asks whether the setup works"
removed (review of #53, change 2); the reel's printed numbers removed (they came from the old independent draw).
Changed here:

| Change | Reason |
| --- | --- |
| A section for a strategy version's daily Time Machine (observed, resampled, stress) | TraderOS's own daily Time Machine (ADR-006) is the same command |
| The fan chart is shown by a run without `--json` | With `--json` the CLI prints only the record |
| The upstream step 5 "Check `/trader-news`" removed | S02 deferred |
| The upstream step 6 "hand off to `/trader-budget`" kept, as "size it hypothetically" | Sizing is hypothetical (S15); the earlier port had dropped it |
| Safety: S11's status is read from the registry, not stated | The skill never states a status the registry may contradict (spec 01.2) |
| The plan replay runs without `--json`; step 2 says so, and step 3 adds "Too few trades to tell", the trade-minimum line and the record id | OSDev6's reviews of #16 (7047949063, 7050691071), HIGH 2: the verdict, its notes and "Too few trades to tell" are only in the text output (`timemachine/1` carries no verdict) |
| How it works, step 6, and Safety: the trade minimum (100 trades) is signed off by the founder, for the replay and the lab alike; the other thresholds, the interval and the cost stay proposals | OWNER ruling 15:59 (2026-11-20); the CLI prints "Trade minimum: 100 trades, signed off by the founder" (#9, #18); HIGH 1 |
| Step 8: "waits for the thresholds to be signed off" becomes "waits for that threshold" | Only the trade minimum is signed off; "strongly" stays proposed |
| A paragraph on what each run needs: daily bars for a strategy version, 5-minute bars for the plan replay and the ORB study | MEDIUM 3 |
| Safety: the upstream paper, three-gates, no-options and broker-keys bullets restored, adapted as in the table at the top | The earlier port had dropped them |

### `risk-budget.md`

From `tank/day-trader-skill/trader-budget/SKILL.md` and `trader-risk/SKILL.md` (`3d0f4ef`), with PR #2's budget and
risk text, #21's and #55's risk text (`tank/prs/day-trader-skill/pr-02`, `pr-21`, `pr-55`).

| Change | Reason |
| --- | --- |
| `size.py` and `check.py` become one `traderos risk assess` | ADR-009: one adapter sizes and checks |
| `config.json` becomes the policy file; upstream's "(default 10)" values become its example values, every one asked for | Spec 15.6, ADR-009: TraderOS has no default for a risk limit |
| "Keep `paper` true until ..." becomes "`ticket place` needs `"paper": true` in the very policy the risk check used", and the example policy carries `"paper": true` | `place` checks the policy's hash against the assessment's (#10, ADR-012) |
| `portfolio.json` becomes the account statement; "if missing, a fresh account funded with the budget" becomes UNKNOWN exposure and no ticket | ADR-009 (P7); spec 15.2, 12.2 C |
| PR #2's "reconcile first" with `get_orders` and `get_all_positions` becomes "ask the user and write the account statement" | ADR-009: the broker read tools are not carried; TraderOS reads no broker on an F1 build |
| "Refresh the price (`alerts.py quote --write`)" becomes the user's own price, with its time, in a prices file | No quote download (06.1); a price with no time is never fresh (ADR-009, P16) |
| Exit codes 0/1 become `ticket_eligible` with exit 0, and 3 for a bad file | The CLI contract |
| The refusals are split: a plan that cannot be checked (no date, another day, a stop not below or a target not above the entry, a short, a blocked ticker, a bad limit) exits 3 with a code; a checked plan that fails exits 0 with `ticket_eligible` false | OSDev6's reviews of #16 (7047949063, 7050691071), MEDIUM 7: re-run at this head |
| `--prices`: a market entry needs a fresh price, or the check blocks it (MISSING_DATA); a limit order needs none | The CLI's check at this head; the earlier text called the file optional |
| "never places an order" goes back to the upstream budget line "never places, approves or schedules an order", with "a paper order is only `ticket place`, after the user's own yes" | OSDev6's reviews of #16 (7047949063, 7050691071): the earlier port had shortened it; TraderOS's place-after-yes is the one order path |
| Example: "8.16 a share" becomes "8.155 a share" | The CLI prints the risk unrounded: "Risk 8.155 a share" |
| Checks table: rows exposure, price and calendar added; daily loss per #55 and the review (unknown blocks whatever the limit); stop is NO_PROTECTION whatever `require_stop` says; cash uses settled cash | ADR-009 (P4-P13, review changes to #2 and #55, S15/RC17) |
| "An approval with no result counts as placed" (#2) now blocks | Review change 4 to #2 |
| A stale, future-stamped or untimed price blocks a market entry | Review change 4 to #2; ADR-009 |
| The window bullet and `valid_until` from #21; a verdict with no `valid_until` is expired | PR #21 with review change 1 |
| "Warnings ... a LIVE account ..." removed | ADR-009: no LIVE warning in F1; live is refused |
| The four risk measures added | Spec 12.1 S16; ADR-009 keeps them |
| Weights mode becomes "not in this build" | S21 is P2; `weights.py` is not wired (ADR-009) |
| "Free Yahoo prices are unofficial and can lag" removed; "the broker's price is the one that fills" kept | 06.1 |
| Example: the upstream reel kept; its `orders.json` body removed; the output trimmed to lines this build prints | No orders.json (the ticket is the document); every example number was re-run on this branch: 8 shares, $46.88, PASS; stop 300.00 refused; the 10:52 price gives 6 shares and reward:risk 0.44, refused; 9:40 blocked by the window |
| `/trader-exit`, `/trader-risk`, `/trader-broker` hand-offs become reference links | One skill |

### `paper-orders.md`

From `tank/day-trader-skill/trader-broker/SKILL.md` (`3d0f4ef`), with PR #2's and #21's text.

| Change | Reason |
| --- | --- |
| `ticket.py show/approve/result/fill/flatten/log` become `traderos ticket ...`; `approve`, `place`, `result`, `fill`, `flatten` and `account` as #10 defines them; `sign-off` is the older name of `approve`: it records the same yes, and `place` accepts it | ADR-009 and ADR-012 (#10); OSDev6's reviews of #16 (7047949063, 7050691071), MEDIUM 6 |
| `ticket result` names `--account account.json` | Without it the command crashed (exit 4) at #10's head; OSDev6's reviews of #16 (7047949063, 7050691071), MEDIUM 1 (#10 fixes the crash) |
| `--index N` becomes a ticket id (`PT-...`) | The store keeps every ticket by id |
| Gate 3 is Claude Code's permission prompt for the `traderos ticket place` run, kept on "ask" | TraderOS sends the paper order itself (ADR-012); the upstream project's own Claude Code line kept the order tool on ask |
| Setup A (Alpaca's MCP server in Claude Desktop) becomes the TraderOS paper setup: paper keys set by the user outside the chat, `ticket account`, the ask rule | Same reason; keys never in the chat is the upstream rule, kept |
| Setup B (the Interactive Brokers connector) removed | A live brokerage path; owner ruling: paper accounts only, live refused |
| "Bracket orders on Alpaca" (the MCP field mapping) removed | The paper client maps the body (ADR-012); the ticket shows the bracket |
| Reconcile (#2) uses the account statement and `ticket result` / `ticket fill`, not `get_orders` / `get_all_positions` | ADR-009 |
| Only an unexpired PASS is READY (#21), and a superseded one is NOT READY | PR #21; ADR-009 (a newer assessment supersedes) |
| "a yes that names another order in the batch" (#2) becomes "another ticket" | One ticket per id |
| `place_stock_order` with the printed JSON becomes `ticket place` | Same reason as gate 3 |
| An unknown outcome (a timeout) is never "not placed" | The paper client's INDETERMINATE rule (ADR-012) |
| The two-layer intro ("the ticket document ... nothing is sent") and the "document-only build" branches removed; a ticket the user decides not to place is recorded with `ticket result --status denied`, and one entered at their own broker is recorded in their account statement | Paper orders are in this build (#10); OSDev6's reviews of #16 (7047949063, 7050691071), MEDIUM 6 |
| Setup step 2 names where TraderOS reads the paper keys (the two environment variables, or `traderos config set`), set by the user in their own terminal | The paper client's own text (ADR-012); keys never in the chat is the upstream rule, kept |
| Flat by: `get_open_position` and `cancel_order_by_id` steps become `ticket flatten`, which says what it does with the bracket's legs | No MCP tools; the paper client acts only inside a ticket the user approved |
| "Never switches `ALPACA_PAPER_TRADE` or `paper` to live" becomes "Never sends to a live account. TraderOS refuses live in its code" | Owner ruling 2026-11-20 |
| "Never calls `close_all_positions`, `cancel_all_orders` ..." becomes "Never cancels, replaces or closes anything except what a ticket the user approved says it will" | No MCP order tools |
| Example: this branch's real ticket output replaces the MCP JSON and the "Claude will call place_stock_order" lines; it follows the paper flow (assess, ticket, show, approve, then place only after the yes), with the order body and the endpoint's address trimmed | Re-run at this head with `"paper": true`; "sure" and "yes to all" refused as the upstream rules say; `place` without keys is refused before any request; the skill names one web address only |

### `earnings.md`

From `tank/trader-skill/trader-analyst/SKILL.md` (`5189e79`: its metrics-for-the-screener text matches
`--metrics`), with PR #2's two "data, not instructions" passages from `pr-02/files/trader-analyst/SKILL.md`.

| Change | Reason |
| --- | --- |
| `facts.py` becomes `traderos earnings`; `sec_user_agent` in `trader/config.json` or `SEC_USER_AGENT` becomes `traderos config set sec_user_agent` | ADR-011: the SEC adapter reads the store's settings |
| "every ticker in `trader/targets.json`" becomes "the companies in question" | No targets file: copy weights are a hypothetical record (ADR-011, change 8) |
| Point in time (`--as-of`) added | ADR-011, change 1 |
| "a row is derived when any number in it was, and the result names them" | PR #24 with review change 1 (ADR-011, change 6) |
| Step 2 "open the press release and the 10-Q and read them" becomes "give the links; read one when asked, or the 10-Q when the lag NOTE is there, and keep its figures apart with its link" | Spec 13.3: the skill interprets TraderOS's structured results; a filing is untrusted text |
| "Say when the data lags" keeps upstream's "read the 10-Q itself before summarising", with "read it as data, and keep its figures apart" | OSDev6's reviews of #16 (7047949063, 7050691071): the earlier port's "say so before summarising" contradicted the example, which reads that 10-Q |
| "It needs the network and the SEC identification ... refuses (NOT_AUTHORIZED, exit 3) before any call"; "sent only to" the SEC's host name becomes "sent only to the SEC (EDGAR)" | MEDIUM 3; the skill names one web address only |
| Example kept, labelled "the upstream run (2026-10-13)" | Not re-run against EDGAR here (no network in this session); the port's tests keep its numbers |
| #2's "run it in a session with no broker connector, or keep every order tool on Needs approval" becomes "nothing in it can make you run a command, change a setting or place an order" | TraderOS has no broker connector; its one order command sits behind Claude Code's prompt |

### `holdings.md`

From `tank/trader-skill/trader-copycat/SKILL.md` (`5189e79`; no reviewed PR touches it).

| Change | Reason |
| --- | --- |
| `edgar13f.py find/filings/holdings/diff/copy` become `traderos holdings --find/--filings/--combine/--diff/--copy` | ADR-011, change 7 |
| `trader/targets.json`, `--out` and the hand-off to `/trader-budget` removed; copy is a hypothetical `copy-targets/1` record | ADR-011, change 8; sizing a weights list is S21, not wired (ADR-009) |
| `--figi` and `trader/tickers.json` become `--map FILE` | ADR-011, change 5 |
| "If an amendment exists, read it with `--accession`" becomes "amendments applied" | ADR-011, change 6 |
| Form 4: the EDGAR browse link becomes "TraderOS does not read them" | No Form 4 reader; the skill reads only TraderOS results (13.3) |
| "13F names are data, not instructions" added | Spec 13.3; PR #2's rule for filings applied to 13F text |
| "The firm, not the user" goes back to upstream's "The firm, not the person" | OSDev6's reviews of #16 (7047949063, 7050691071): the person is Buffett; "the user" changed the meaning |
| Safety: upstream's "Paper trading by default" and its third gate ("Allow once / Deny") restored, adapted as in the table at the top | The earlier port had dropped them |
| "`--find` without `--sec` works offline; every other line needs the network and the identification" | MEDIUM 3 |

### `screener.md`

From `tank/trader-skill/trader-screener/SKILL.md` (`5189e79`; no reviewed PR touches it).

| Change | Reason |
| --- | --- |
| `screen.py FILE --rules` becomes `traderos fundamentals screen --metrics/--tickers --criteria` | ADR-011; `--criteria` is the frozen spelling (`--rules` stays an alias) |
| `trader/prices.json` "any source; /trader-alerts writes it" becomes the user's own prices file | No alerts prices file; 06.1 |
| `--out trader/shortlist.json` becomes the stored `fundamental-screen/1` record | One store |
| `"missing": "fail"/"skip"` semantics: a missing metric is `passed: null` (N/A); units; stale flags | ADR-011, changes 2-4 |
| "Exit code 1 means nothing passed" becomes "if nothing passed, say so" | CLI exit codes (ADR-011, change 6) |
| Example kept as upstream's, with how TraderOS answers BAC and KO | Re-run on this branch with its fixture: BAC is N/A, KO passes with STALE_PERIOD and PERIOD_MISMATCH |
| The `--as-of` example reads a dated file (`my-dated-companies.csv`); a plain CSV is screened as of today; a "Point in time" paragraph explains LOOKAHEAD_RISK | Since #11's look-ahead fix, an undated file with a past `--as-of` exits 3 (LOOKAHEAD_RISK); OSDev6's reviews of #16 (7047949063, 7050691071), second review |
| `--tickers` needs the network and the SEC identification; `--metrics` needs neither | MEDIUM 3 |

### `scanner.md`

From `tank/day-trader-skill/trader-scanner/SKILL.md` (`3d0f4ef`; PR #2's scanner text is about Yahoo's rate limits
and is not carried), written for OSDev6's PR #13 (`7988e8d`, ADR-008).

| Change | Reason |
| --- | --- |
| `scan.py universe/daily/orb` become `traderos scan --setups daily/orb` and `--criteria` | ADR-008 |
| "The universe" (Nasdaq Trader download) removed; the universe is the dataset's instruments | ADR-008; 06.1 |
| Yahoo spark/v8 stages, timings and rate limits removed | ADR-008: one pass on real highs; Yahoo held |
| `trader/watchlist.json` becomes the stored `scan/1` record; `no_entries_after` from the config becomes `--until` | PR #13's CLI |
| `--offline`, `--no-write`, `--show`, `--max-confirm`, `--now` removed | No such flags; `--as-of` reads the store as of a time |
| `NO_ELIGIBLE_SETUP` and "show why a symbol qualified" added | Spec 12.2 A |
| Example: the reel's numbers kept as the upstream run; its offline command line removed | Not a TraderOS command |
| The daily example and the criteria file use the tutorial's symbols; a paragraph says the ORB scan needs 5-minute bars | MEDIUM 3 |
| Safety: the upstream paper, three-gates, no-options and broker-keys bullets restored, adapted as in the table at the top | The earlier port had dropped them |

### `entry.md`

From `tank/day-trader-skill/trader-entry/SKILL.md` (`3d0f4ef`; PR #23 changes only the code), written for PR #13.

| Change | Reason |
| --- | --- |
| `entry.py check` becomes `traderos entry SYMBOL --setup orb/level`; `watch`, `watchlist`, `--write`, `--interval`, `--offline`, `--now` removed | ADR-008; PR #13's CLI (`--as-of`) |
| Yahoo bar timing becomes "only once the store has made them available" | ADR-008 |
| "A range missing a bar is refused" | PR #23 |
| Step 3 (background `watch` or `/loop`) becomes "re-run after importing newer bars; never from a background task that could lead to an order" | Stored data only; no watch command |
| Example: printed lines condensed to prose | Its lines named Yahoo and `exit.py` |
| "never runs `/trader-broker` from a background watch" becomes "never leads to an order from a background task" | One order path |
| Upstream's "Before 9:30: not open yet. Before 9:45: the range is still forming. Weekends and NYSE holidays: the market is closed." restored, with "each is not eligible, with the time the range is set or the next open" | OSDev6's reviews of #16 (7047949063, 7050691071): the earlier port had dropped them; the CLI answers each that way (#13) |
| The header says the Entry Timer needs 1- or 5-minute bars, which the tutorial does not have | MEDIUM 3 |
| Safety: the upstream paper, three-gates, no-options and broker-keys bullets restored, adapted as in the table at the top | The earlier port had dropped them |

### `exit.md`

From `tank/day-trader-skill/trader-exit/SKILL.md` (`3d0f4ef`), with PR #2's ATR row, written for PR #13.

| Change | Reason |
| --- | --- |
| `exit.py plan/show` become `traderos exit SYMBOL --direction long --entry P --stop RULE --target RULE` | ADR-008 |
| The ATR stop reads stored bars only up to `--entry-at` | ADR-008 (review fix) and PR #2: no look-ahead |
| "to the cent" becomes "to the tick"; an entry off the tick is refused | ADR-008 |
| `trader/plan.json` becomes the `exit-plan/1` record; hand-offs point at the Time Machine (flags), the risk check and `alerts ring --from-plan` | PR #13's records and CLI |
| "Plain numbers only" names the entry and each scenario's price and its change "vs the entry, per unit", as the planner prints them | #13 prints each scenario that way, never as P&L (9d42564) |
| Every command line names `--dataset`; a paragraph says a plan reads the symbol's bars, and an opening-range or ATR stop needs 5-minute bars; a tutorial line (TUTMKT, a `pct:` stop) added | OSDev6's reviews of #16 (7047949063, 7050691071), MEDIUM 2 and 3: without `--dataset` a store with two datasets refuses, and the tutorial has no TSLA |
| "What it reads and writes", step 3 and the reel example describe this CLI's output: the scenarios (stop, gap through the stop, target, time exit, last close, one ATR against), each "vs the entry, per unit"; the upstream risk-a-share, reward:risk and flat-by block is replaced by the planner's own lines | OSDev6's reviews of #16 (7047949063, 7050691071), MEDIUM 5: `exit-plan/1` has no risk-a-share, reward:risk or flat-by field; those come from the risk check |
| "Refused" is split: every plan (the stop and target sides, the tick) and a plan with a rule (under 0.05%, a short, an entry at or after flat-by, an `--entry-at` on another day or after `--as-of`) | OSDev6's reviews of #16 (7047949063, 7050691071), MEDIUM 5: with bare prices the planner takes a short and a 0.02% stop; #13's `--entry-at` rule |
| Safety: the upstream paper, three-gates, no-options and broker-keys bullets restored, adapted as in the table at the top | The earlier port had dropped them |

### `alerts.md`

From `tank/day-trader-skill/trader-alerts/SKILL.md` (`3d0f4ef`), with PR #2's re-arm text (PR #22's fix is in
the code), written for PR #13.

| Change | Reason |
| --- | --- |
| `alerts.py clock/add/check/quote/list/reset` become `traderos alerts ring --rules/--from-plan`, `alerts reset` and `alerts check`; `clock`, `add`, `quote` and `list` have no command | ADR-008; PR #13's CLI |
| "Quotes" (Yahoo, Stooq, broker snapshots, `trader/prices.json`) removed: prices are the newest complete stored close | ADR-008; 06.1 |
| "The NYSE clock" holiday list removed | The XNYS calendar is in the code (ADR-004, ADR-008) |
| PR #2's "`add` clears the rang mark" becomes "a rule that rang stays quiet until `alerts reset ID`" | There is no `add` |
| Step 2: "from a Claude scheduled task or `/loop`" becomes "run it again on fresh data"; Free has no always-on service | Spec 12.1 S18 |
| Example: printed lines become prose | Its lines quoted Yahoo and pointed at `trader-broker flatten`; no message points at an order (ADR-008) |
| Example: "a run at 10:53 on a price of 366.48" becomes "with every bar since the plan between the stop and the target" | 366.48 was the upstream Yahoo quote; TraderOS reads stored regular-session bars since the plan |
| Prices are regular-session bars only; step 2: a price rule reads every bar since its own last check (or its plan), so a touch between two checks rings at the next one; the first check of a rule with no plan reads the price now; MISSED_WINDOW past 5 sessions | OSDev6's reviews of #16 (7047949063, 7050691071): #13 (an after-hours close of 400.5 does not ring a 390 target; `alerts ring` catches a touch from the rule's own last check) |
| `alerts reset` without `--state` acts on the default state file, shown beside the `--state` form; every `ring` line names `--dataset` | #13; OSDev6's reviews of #16 (7047949063, 7050691071), MEDIUM 2 |
| The `alerts check` example is dated inside the tutorial's range, and a paragraph says what alerts need | MEDIUM 3 |
| Safety: the upstream paper, three-gates, no-options and broker-keys bullets restored, adapted as in the table at the top | The earlier port had dropped them |

### `data fetch` (code, not skill text; ADR-014)

`traderos data fetch` ports the upstream free price fetch from `tank/day-trader-skill/` into
`src/traderos/adapters/prices.py`: `YAHOO_HOSTS`, `PAUSE`, `BACKOFF`, `UA`, `_get`, `yahoo_url`, `fetch_yahoo`,
`parse_yahoo`, `stooq_url` and `parse_stooq` from `trader-alerts/alerts.py`; `yahoo_symbol` and `yahoo_get` from
`trader-entry/entry.py`; `series_from_result`, `parse_chart` and `daily_closes`' rules for today's bar and the late
duplicate from `trader-scanner/scan.py`. Every change, with its reason, is in `docs/adr/ADR-014-data-fetch.md`; its
MIT notice covers them in `NOTICE`. The skill's lines about it (SKILL.md's Guided mode, the `bars`
prerequisite, and `data.md`'s "Free bars" section) are TraderOS's own text: its packs fetch inside each tool, so
there was no SKILL.md step to port. Like its README, they call the data "free and unofficial".
