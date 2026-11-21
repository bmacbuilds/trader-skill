# TraderOS CLI contract v1 (FROZEN)

The `traderos` command's spelling, output and exit codes. Spec §13.2 proposes the commands and this document freezes
them. A change needs an ADR and a note to the skill builders, because the `/trader` skill calls exactly these
commands (spec §13.3).

## Rules

- `pip install` installs the CLI, not the Claude skill (spec §04.3).
- Every command accepts `--json`.
  - Success prints `{"ok": true, "result": <record or records>}`.
  - A refusal prints `{"ok": false, "failure": <failure/1>}`.
  - Human output goes to stdout, and refusal text without `--json` goes to stderr.
- **Exit codes:**
  - `0`: ok.
  - `2`: a usage error.
  - `3`: a typed refusal, such as `CAPABILITY_DISABLED` or `MISSING_DATA`.
  - `4`: an unexpected internal error. It is a bug: it is reported and never disguised as a result.
- **No network:** no command makes a network call unless it is an explicitly named adapter command run with the
  user's own entitlement. `demo` never does. The Alpaca commands (`ticket place`, `ticket fill <ticket-id>`,
  `ticket account`) use the person's own Alpaca PAPER keys and reach only `paper-api.alpaca.markets` (ADR-012).
  `data fetch` reaches only Yahoo's two chart hosts and Stooq, and sends no credential (ADR-014).
- **Paper only, never live (ADR-012, owner ruling 2026-11-20):** every command that produces a plan or ticket says
  it is hypothetical or paper. Only `traderos ticket place` sends an order, only to Alpaca's paper endpoint, and
  only after the person's explicit yes for that one ticket. No command, alias, flag, setting or environment variable
  can send an order anywhere else: a live endpoint, a live key id, a policy whose `paper` is not `true` and the IBKR
  branch are refused with `NOT_AUTHORIZED` before any request.

## Commands

| Command | Behavior in F1 | Owner |
|---|---|---|
| `traderos --version` / `version` | Prints `traderos <version>`. | core |
| `traderos help` | Lists the commands that really exist. | core |
| `traderos doctor` | Checks Python, the skill files, the version, the local store and the permitted adapters. It never prints a credential. | core (A) |
| `traderos init` | Creates `~/.traderos/` (or `$TRADEROS_HOME`) idempotently. Existing research is kept. | core (A) |
| `traderos demo` | Offline tutorial: market → rules → test → challenge → Passport → reopen. Zero network. | integration (A) |
| `traderos data import <csv> [--name N] [--rights-label L]` | Validates and hashes the file and runs the quality gate. Returns a manifest or a typed refusal. | data (A) |
| `traderos data fetch <symbol>... [--interval 1d\|5m] [--range R] [--name N]` | An adapter command (network): free, unofficial bars from Yahoo Finance's public chart endpoint, with Stooq as the daily fallback; says which source answered. The bars take the `data import` path (quality gate, content-addressed dataset) and carry `UNOFFICIAL_DATA`; their rights allow no export (ADR-014). | data (A) |
| `traderos data check <dataset-id>` | Reports rights, completeness, calendar and point-in-time limits. Nothing is repaired silently. | data (A) |
| `traderos data list` | Lists local datasets. | data (A) |
| `traderos market [--dataset D] [--as-of T]` | Market Pulse. | B |
| `traderos scan --criteria FILE \| --setups daily\|orb [--symbols S,…] [--min-price P] [--min-volume V] [--or-minutes N] [--until HH:MM] [--dataset D] [--as-of T]` | Chart Scanner, with per-symbol reasons. `--setups` runs the upstream setups (ADR-008); orb expires after `--until` (default 15:30 New York). | B |
| `traderos entry <symbol> [--setup orb\|level [--level P] [--or-minutes N] [--min-volume-x X]] [--dataset D] [--as-of T]` | Entry Timer; with `--setup`, the upstream trigger on 1- or 5-minute bars. Research only. | B |
| `traderos exit <symbol> --direction long\|short --entry P [--stop P\|RULE] [--target P\|RULE] [--setup orb\|level] [--or-minutes N] [--or-high P --or-low P] [--flat-by HH:MM] [--entry-at T] [--dataset D] [--as-of T]` | Exit Planner. Hypothetical. A RULE is the upstream (long only): stops or-mid, or-low, price:X, pct:X, atr:K; targets 2R, 1R ..., price:X. | B |
| `traderos alerts check --rules FILE [--dataset D] [--at T]` | Evaluates alert rules over stored data. | B |
| `traderos alerts ring --rules FILE\|--from-plan FILE [--state FILE] [--dataset D] [--at T]` | The upstream alerts (stop, target, pct_move, opening_bell, time): one alert-event/1 per rule; `triggered: null` is not checked. Price rules name an instrument_id. Each rings once, remembered in the state file. | B |
| `traderos alerts reset ID... [--state FILE]` | Lets alerts that rang ring again (the upstream reset). | B |
| `traderos strategy draft FILE` / `validate FILE` / `approve <draft-id>` / `show <id>` | The strategy lifecycle. `approve` needs the person's confirmation; `--in-chat` records that they typed `approve <hash>` in the Claude app. | lab (A) |
| `traderos research run <strategy-version-id> --dataset D` | Preregisters, runs and challenges, and stores everything. | lab (A) |
| `traderos timemachine <strategy-version-id> --dataset D` | Historical replay, block resampling and stress. With no window, it reads the development and validation windows only, and stops before the earliest sealed holdout of the strategy's research family on that dataset. | lab (A) |
| `traderos timemachine plan --plan FILE --dataset D [--as-of T] [--until HH:MM] [--replays N] [--seed S] [--cost-bps X] [--chart]` | The upstream plan replay of one planned intraday trade on a stored 5-minute dataset (or the plan as `--symbol --date --entry --stop --target --entry-time`; never both). Stores a `timemachine/1` record with a plan-replay subject. Hypothetical. Added by ADR-010. | lab (A) |
| `traderos timemachine orb-study --dataset D [--as-of T] [--until HH:MM] [--symbols S…] [--draws N] [--seed S]` | The upstream opening-range study across the dataset's symbols: four variants, one research family, recorded in research memory. Added by ADR-010. | lab (A) |
| `traderos passport show\|reproduce <id>` | The Evidence Passport. `passport share` is not in this release (spec 18.3: redacted and opt-in): it refuses with exit 3, CAPABILITY_DISABLED, and help does not list it. | lab (A) |
| `traderos passport explain <id>` / `check-claims <id> --text FILE` | S08 Quant Analyst: the Passport's own numbers in words, each cited, a baseline set only beside the window it was measured on (development and validation together). `check-claims` ties each number in a text to the one field it states (the metric named next to it for the window, baseline or challenge named, an explicit `[holdout.net_return]`, or a verbatim quote of a stored finding) and passes it only when its sign, its unit and its magnitude, at the precision the Passport states, agree; any number that disagrees (`FAIL`) or cannot be tied (`UNVERIFIED`) refuses the text (`INSUFFICIENT_EVIDENCE`, exit 3). Not checked: words comparing two numbers, dates, identifiers. | lab (A) |
| `traderos memory search <query>` | Searches local research memory, with record ids and hashes. | lab (A) |
| `traderos risk assess --plan FILE --policy FILE [--account FILE] [--prices FILE] [--limit [PRICE]] [--now T] [--stress F]` | The upstream sizing and risk check under your policy, and the four risk measures. No broker. A refused plan exits `0` with `ticket_eligible: false` and a NoTradeRecord; a bad file exits `3`. | C |
| `traderos ticket <assessment-id> [--now T]` | Drafts a paper ticket from the latest assessment while its check is valid. Nothing is sent. | C |
| `traderos ticket show\|approve\|decline <ticket-id> [--reply TEXT] [--now T]` / `traderos ticket log [--all]` | Reads a ticket, its status and the exact body TraderOS would send; records your explicit yes for that one ticket (the upstream approve; `sign-off`, the F1 name, is an alias), or a no; lists the day's decisions and how many were placed. A yes sends nothing. | C |
| `traderos ticket place <ticket-id> --policy FILE` | Sends ONE approved ticket to your Alpaca PAPER account (upstream's "Allow once"), after every gate holds: the policy says `"paper": true` and is the one the check used; a yes is recorded for this exact order; the check has not expired and is the latest (measured by the real clock: no `--now`); your paper keys are set; the account is a paper account. A flatten ticket first cancels your open sell orders for the symbol. Added by ADR-012. | C |
| `traderos ticket result <ticket-id> --status placed\|denied\|failed [--broker-id ID] [--note N] [--filled-qty Q --filled-price P --account FILE]` | The upstream result: what happened to an approved ticket when you know better than TraderOS (a send whose outcome is unknown, or one you did not send). `--account` is needed only with a fill; a fill without it is refused (exit 3) and nothing is recorded. Added by ADR-012. | C |
| `traderos ticket fill <ticket-id> --account FILE` / `ticket fill --symbol S --side buy\|sell --qty Q --price P --account FILE [--date D]` | The upstream fill: from the paper endpoint (the placed order and its legs), or by hand; updates your account statement (a sale's proceeds go to `unsettled_cash`) and the journal. Added by ADR-012. | C |
| `traderos ticket flatten <symbol> --policy FILE --account FILE [--qty Q] [--prices FILE] [--now T]` | The upstream flatten: a market sell of what you hold, by `flat_by`, checked at once as an exit; then show, approve and place it. A holding whose account statement states a contract `multiplier` other than 1 is refused (`OUTSIDE_CAPABILITY`): an Alpaca paper order is a number of shares. Added by ADR-012. | C |
| `traderos ticket account` | Your Alpaca paper account's status, read from the paper endpoint; nothing is changed. Added by ADR-012. | C |
| `traderos config set alpaca_paper_key_id\|alpaca_paper_secret_key VALUE` / `config show` | Local settings (owner-only file). Secrets are never printed: `show` and `doctor` mask them. A settings file with any other key is refused, naming the key (`doctor` reports it). Added by ADR-012. | core (A) |
| `traderos journal add\|list` | The local append-only journal. | C |
| `traderos earnings <ticker>... [--cik C] [--as-of D] [--quarters N] [--no-filings] [--metrics FILE]` | Quarterly revenue, margins, net income, diluted EPS and growth from the company's SEC filings, point in time, with derived quarters flagged; several tickers give a list (ADR-011). Needs the person's SEC identification. | C |
| `traderos holdings <cik-or-ticker> [--as-of D] [--top N] [--combine] [--period D] [--accession A] [--map FILE] [--diff \| --copy]`, `holdings <filer> --filings`, `holdings --find NAME [--sec]` | A manager's 13F as reported (amendments applied), period vs filing date; `--diff` compares two quarter ends; `--copy` gives hypothetical weights only (ADR-011). Needs the SEC identification, except `--find` without `--sec`. | C |
| `traderos fundamentals screen (--tickers A,B \| --metrics FILE) --criteria FILE [--prices FILE] [--as-of D] [--top N]` | Screens companies against rules (operators, ranking, prices) on figures from dated filings or your file; a missing value gives `passed: null`; figures or prices from after `--as-of`, or undated ones for a past `--as-of`, are refused (`LOOKAHEAD_RISK`, exit 3) (ADR-011). `--tickers` needs the SEC identification. | C |
| `traderos config set sec_user_agent "Name email"` / `config show` | Local settings (owner-only file). The SEC identification is sent only to sec.gov. | core (A) |
| `traderos export [<passport-id>…] [--profile full_private\|share_redacted] [--out FILE] [--now TIME]` | Writes a portable `.traderos` bundle: no ids means everything (`full_private`), with the experiment runs and their files so a restore reproduces; a shareable copy names its Passports, leaves out records that name a person and reads dataset names and rights labels as "dataset 1" and "rights: see original". Never keys, tokens or settings; raw data only where its rights allow. The same profile exports the same bytes; the bundle holds a time only when `--now` gives one (ADR-013). **Not in this release:** answers `CAPABILITY_DISABLED` (exit 3) before anything is read or written; it comes in the first update (ADR-013). | lab (A) |
| `traderos import <bundle> [--dry-run]` / `--apply` | Previews by default: runs every check apply runs and writes nothing, not even the profile folder. Apply writes all of it in one transaction or none of it: any conflict, failed check or Passport missing what it stands on refuses it ("Apply refused", exit 3). A dataset whose bars the bundle could not carry is listed to re-import, never created empty. Applying again is a no-op. **Not in this release:** answers `CAPABILITY_DISABLED` (exit 3) before anything is read or written; it comes in the first update (ADR-013). | lab (A) |
| `traderos capabilities` | Shows exactly what this build enables, from the same registry the skill reads. | core (A) |

A command that is not built yet exits `3` with `CAPABILITY_DISABLED` and names its owner. It never prints a made-up
result.

## Part C: the files `risk assess` reads (contracts v1.1, ADR-009)

Every file is JSON read strictly: at most 64 KB (prices 256 KB), UTF-8, numbers written as strings or integers (a
float literal, `NaN` or `Infinity` is refused), no duplicate or unknown keys. A refusal names the key.

- **`--plan`**: `symbol`, `direction` (`long`; the upstream sizing is long only, so `short` is `OUTSIDE_CAPABILITY`),
  `date` (the New York trading day the plan is for; another day is `DATA_STALE`), `entry`, `stop`, `target`;
  optional `multiplier`, `tick_size`, `currency` (only `USD`: the policy's limits are dollars), `instrument_id`.
  The v1.0 keys `equity`, `risk_per_trade`, `quantity`, `whole_units`, `settled_cash`, `reserved_cash`,
  `existing_exposure` and `prices_observed_at` are refused with the place each moved to. Prices are used exactly,
  never rounded: an `entry`, `stop`, `target` or `--limit` price off the tick is refused (`INVALID_INPUT`, exit 3).
  The tick is `tick_size` if stated, else the U.S. equity rule: whole cents at $1.00 and above, at most four
  decimals below (ADR-009, review of #10).
- **`--policy`** (required): your limits, the upstream config keys. Each of `budget`, `cash_buffer_pct`,
  `max_position_pct`, `max_order_usd`, `min_order_usd`, `max_loss_per_trade_usd`, `max_daily_loss_usd`,
  `max_trades_per_day`, `max_orders_per_day`, `min_reward_risk`, `no_entries_before`, `no_entries_after`,
  `blocked` and `pdt_guard` must be stated: a missing one is `MISSING_DATA`, because TraderOS has no default for a
  risk limit. `0` or `null` switches off a daily limit, the trade and order counts, the minimum reward:risk or the
  minimum order; `null` switches off an entry-window bound. Optional: `allow_options`, `allow_margin`,
  `allow_short` (false unless you say true), `require_stop` (only `true`), `flat_by`, `fractional` (weights mode,
  not in F1), `paper` (only `true`: anything else, `"true"` included, is `NOT_AUTHORIZED`, live trading is refused
  in this build; `ticket place` needs it stated) and `broker` (only `"alpaca"`; `"ibkr"` is `NOT_AUTHORIZED`: TraderOS
  cannot prove an IBKR connector routes to a paper account). `sec_user_agent` is refused. Example, the upstream
  `config.example.json` without the broker keys (add `"broker": "alpaca", "paper": true` to place):

  ```json
  {"budget": "5000", "cash_buffer_pct": "10", "max_position_pct": "75", "max_order_usd": "4000",
   "min_order_usd": "5", "max_loss_per_trade_usd": "50", "max_daily_loss_usd": "150", "max_trades_per_day": 3,
   "max_orders_per_day": 10, "min_reward_risk": "1.0", "require_stop": true, "no_entries_before": "09:45",
   "no_entries_after": "15:30", "flat_by": "15:55", "fractional": false, "blocked": [], "allow_options": false,
   "allow_margin": false, "allow_short": false, "pdt_guard": false}
  ```
- **`--account`** (optional; without it exposure is `UNKNOWN` and no ticket can be drafted): `settled_cash`,
  `reserved_cash`, `positions` (`[{"symbol", "shares"}]`, optional `lots` and `multiplier`, the contract multiplier
  of what you hold, default 1: a holding is worth shares x price x multiplier, and a plan whose multiplier is not
  the holding's stated one is `UNKNOWN_EXPOSURE`; a holding with no price in `--prices` and no `lots` cost has an
  unknown value, and blocks every entry as `UNKNOWN_EXPOSURE`) and `orders`, the last few days' orders
  you placed yourself (`symbol`, `side`, `status`: placed / filled / denied / failed / declined, `date`; optional
  `kind`, `max_loss`, `qty`, `stop`, `ticket`); optional `unsettled_cash` (where `ticket fill` puts a sale's
  proceeds until they settle; never spent by the check, and counted in the account's total). An order naming an
  approved ticket (`"ticket": "PT-..."`)
  records what happened to it. TraderOS's own paper orders, results and fills are in the journal, and the check
  reads both; `ticket fill` updates this file the way the upstream `fill` updated its portfolio.json. `risk assess` never
  reads a broker.
- **`--prices`**: `{"prices": {"TSLA": "366.475"}, "detail": {"TSLA": {"time": "2026-10-19T14:52:13Z"}},
  "as_of": ...}`. A price at most 15 minutes old is fresh; a price with no time is never fresh. A market entry needs
  a fresh one: with none (no `--prices`, or the symbol not in it) the check BLOCKs (`MISSING_DATA`), and with a
  stale one too (`DATA_STALE`). A limit order is measured from its limit and needs none, unless you hold its
  symbol: then its price values your position, and without it the check BLOCKs (`MISSING_DATA`).
- Every number in these files is under 10^15 in size: a larger one (`1E+99`) is refused (`INVALID_INPUT`, exit 3),
  and so is any arithmetic the assessment cannot do. Such a refusal is recorded and supersedes the older
  assessment, as every failed attempt does.
- **`--now`**: RFC 3339 with its zone; a naive time is refused.

What a ticket answers (the upstream statuses): `READY` (the check passed, has not expired and is the latest assessment),
`NOT READY` (it expired, or a newer assessment or a failed attempt supersedes it), `BLOCKED by the risk check`,
`APPROVED` (waiting for `ticket place`), `SENT, OUTCOME UNKNOWN` (record it with `ticket result`), or `DONE: placed`,
`denied`, `failed` or `declined`. `approve` takes only an explicit yes for that one ticket (the upstream rules: not "sure",
"yes to all", a doubt, another open ticket's symbol or a different amount), and refuses with `INVALID_APPROVAL`. An
approved ticket counts as placed today, and blocks every new entry as `UNKNOWN_EXPOSURE`, until a result is recorded
(by `place`, by `result`, or by your account statement). A ticket is never sent twice: a failed one needs a new check
and a new yes, and one whose send timed out (`BROKER_INDETERMINATE`) waits for `ticket result`.
