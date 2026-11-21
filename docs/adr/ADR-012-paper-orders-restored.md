# ADR-012: the upstream paper-order half is restored; TraderOS sends only to Alpaca's paper endpoint, live refused in code

- **Status:** proposed, 2026-11-20, for part C (OSDev4). Not yet reviewed. Built on `claude/traderos-decisions`
  (a82c978).
- **The owner's decision.** OWNER post by OSDev1 on the wall, 2026-11-20 14:50 UTC (Brian's own answer was "Y"):
  > "OSDev1 asked Brian: 'should the free product keep [the upstream] paper orders, paper accounts only and live refused?' His
  > answer: 'Y'. OSDev4: restore [the upstream] paper-order half of ticket.py in #10, unchanged except what must fit. It
  > sends only to the paper endpoint, and live is refused in code with a test that proves it. Name any change per the
  > rewrite rule."
- **The rewrite rule** (owner, relayed by OSDev1 as OWNER posts; quoted in ADR-009): "Python files and all code only
  gets rewritten if there is clearly a flaw or if there is clearly a better method." Every change below names its
  reason.
- **What it supersedes.** Spec v2.0 makes S17 a paper ticket *document only* in F1 (spec 15, 00.2, 21.1). The spec
  still says so; this ADR does not claim otherwise. The owner's ruling above supersedes it for S17, and that ruling is
  the authority for everything here. ADR-009's removal of the order half ("ticket.py (S17 in F1: a document)") is
  superseded; the rest of ADR-009 stands.
- **Sources:** `tank/day-trader-skill` at `3d0f4ef` (MIT; see NOTICE): `trader-broker/ticket.py` and its
  SKILL.md with the approved PRs applied (#49 as is, #2 and #21 with the review's changes), `tests/test_broker.py`.
  For the REST body: `alpaca-mcp-server` 2.4.1 (MIT, Alpaca; the server the upstream SKILL.md set up, checked upstream at
  2.4.0), its `overrides.py` (`place_stock_order`) and the OpenAPI spec it ships (`specs/trading-api.json`), read
  from the PyPI wheel. No code was copied from it; the three-field mapping below is written fresh and cited.

## Decision

### The one design change (OSDev4's decision, in the brief for this build)

The upstream ticket.py never sends: it prints the exact arguments and Claude calls a separately installed Alpaca MCP
server's `place_stock_order`, whose paper/live choice lives in that server's environment (ALPACA_PAPER_TRADE),
outside any code TraderOS can test. The ruling requires "live refused in code with a test that proves it", so
TraderOS sends the SAME order body (the upstream `tool_input`, its ALPACA_FIELDS, unchanged) itself, through a new adapter
that can only reach Alpaca's paper endpoint. **Named reason:** "the endpoint was set in a separate MCP server's
environment, outside code TraderOS can test; the owner's 11-20 ruling requires paper only, refused live, proven by
a test."

### What "the same body" is on the wire (a finding of this build; proposal, not yet owner- or OSDev4-approved)

`tool_input` is the MCP tool's *arguments*, not Alpaca's REST body. Alpaca's `POST /v2/orders` has no
`take_profit_limit_price`, `stop_loss_stop_price` or `stop_loss_limit_price`; its body takes `take_profit:
{limit_price}` and `stop_loss: {stop_price, limit_price}` (trading-api.json, `postOrder` request schema). The MCP
server did that mapping (`overrides.py`, `place_stock_order`):

```python
if take_profit_limit_price is not None:
    body["take_profit"] = {"limit_price": take_profit_limit_price}
if stop_loss_stop_price is not None:
    sl: dict = {"stop_price": stop_loss_stop_price}
    if stop_loss_limit_price is not None:
        sl["limit_price"] = stop_loss_limit_price
    body["stop_loss"] = sl
```

So sending `tool_input` verbatim would not reproduce Claude's call: every TraderOS entry ticket is a bracket, and its
stop-loss and take-profit would not reach Alpaca as legs. TraderOS sends `tool_input` with exactly that mapping
(`alpaca_paper.order_body`) and nothing else changed: the upstream field order, its strings, `order_class` set to bracket
when legs are present and no class is given (as the server did). Proven by tests: for an order without legs (the
flatten, a market or limit order, its AAPL notional fixture) the bytes sent are `json.dumps(tool_input(order))`,
byte for byte; for its TSLA bracket the body equals what the server POSTed for its `TSLA_CALL`, and
`tool_args(body)` gives back its `tool_input` byte for byte. **Not verified against Alpaca itself:** this session had
no network to Alpaca, so no order was sent to a real paper account; the evidence is the server's code and the
OpenAPI spec it ships.

## The adapter: `src/traderos/adapters/alpaca_paper.py`

Network only inside `adapters/`, in the style of `adapters/sec.py` (on `claude/traderos-sec`): an injected transport
for tests, a timeout (15 s), a size cap (1 MB), HTTPS only.

| Guard | How |
|---|---|
| One host | `PAPER_HOST = "paper-api.alpaca.markets"`, `BASE_URL = "https://" + PAPER_HOST`: constants. `PaperClient` and `client()` take no URL, host or base argument (a `TypeError`). |
| Every URL checked before the transport is called | `check_url`: scheme https, host exactly the paper host, no port, no user info, path `/v2/...`. The live host `api.alpaca.markets` is refused by name (`LIVE_HOSTS`); any other host is refused too. The client checks, and the real transport checks again. `LiveRefused` is `NOT_AUTHORIZED`: "live trading is refused in this build". |
| No setting or environment variable can change the host | The adapter never reads an address. A stray `APCA_API_BASE_URL` (and five other spellings) pointing anywhere but the paper host, or `ALPACA_PAPER_TRADE` set to anything but true, is refused before any request, so nobody believes it is in use. |
| No redirect | The opener has a redirect handler that refuses: a redirect would carry the keys to another host. |
| Paper keys only | The person's own keys: `APCA_API_KEY_ID` + `APCA_API_SECRET_KEY`, else `traderos config set alpaca_paper_key_id ...` / `alpaca_paper_secret_key ...` (`<home>/config/settings.json`, created 0600, written atomically). Missing: `NOT_AUTHORIZED` with how to set them. A key id starting with `AK` is refused as a live key (Alpaca's live key ids start with AK and paper ones with PK: observed, not documented, so it can only refuse). |
| The account is checked | See below. |
| A send that times out | `BROKER_INDETERMINATE`, never "not placed" (the MCP server's own words: "The order MAY have been placed"). A 5xx answer to a send is indeterminate too; a 4xx is `Rejected` (nothing placed). A read that times out changed nothing. |
| Methods | Only what the upstream flow needs: the account, submit (`POST /v2/orders`), get one order (with legs), list the open orders for one symbol, cancel one order (for flatten). |

**The account check, and which field.** Alpaca's account object has no documented paper flag: the OpenAPI `Account`
schema in trading-api.json has `account_number`, `status`, `cash` and the rest, and nothing that says paper or live.
So **the guarantee is the host**: the paper endpoint serves only paper accounts. On top of it, `account()` refuses an
account whose `account_number` does not start with `PA`, Alpaca's paper numbering (the spec's own account example is
`PALPACA_123`). That prefix is observed, not documented: it can only stop an order, never send one, and if Alpaca
changes its numbering the refusal is the failure mode, not a live order.

## The restored half, function by function

`src/traderos/risk/tickets.py` (the functions), `src/traderos/commands/decisions.py` (show, approve, decline, log,
and the journal as the upstream log), `src/traderos/commands/paper.py` (place, result, fill, flatten, account).

| Upstream's | Here | Changed, and why |
|---|---|---|
| `tool_input`, `ALPACA_FIELDS`, `STRING_FIELDS` | `tickets.tool_input` (ALPACA_FIELDS is check.py's one copy, ADR-009) | Unchanged. |
| `render`: "Claude will call place_stock_order with exactly:" + the JSON | "TraderOS will send exactly this to Alpaca's paper endpoint (paper-api.alpaca.markets):" + the body actually sent | The sender changed; the block shows the bytes' JSON (the legs nested, above). |
| `render`: "ORDER n of N, PAPER account / LIVE ACCOUNT, REAL MONEY, Alpaca / Interactive Brokers" | "PAPER TICKET PT-..., PAPER account, Alpaca" (`ACCOUNT_LINE`, a constant) | Live is refused in this build, so the live line cannot render; tickets have ids, not indexes (ADR-009). |
| `render`: the IBKR lines; `"broker": "ibkr"` | Refused, `NOT_AUTHORIZED` (`policy.broker_keys`, `policy.require_paper`) | TraderOS cannot prove an Interactive Brokers connector routes to a paper account. |
| `render`: "Claude's own card then asks again: Allow once places it, Deny stops it." | "Then `traderos ticket place PT-...` sends it; nothing is sent before that." | The sender changed: `ticket place` is the Allow once. |
| `ticket_json` | `tickets.ticket_json` (`ticket show --json`) | Same fields; `"tool": "place_stock_order"` becomes `"endpoint"`, plus the `body` sent and `kind`. Amounts as strings (JSON). |
| `status_of` labels | Kept, with "APPROVED by you at ..., waiting for `traderos ticket place PT-...`" | Upstream's "waiting for the place_stock_order call": the sender changed. "SENT, OUTCOME UNKNOWN" is new (below). |
| `cmd_approve`, `parse_yes` | `ticket approve` (the F1 name `sign-off` is an alias) | `parse_yes` unchanged. The yes now records the upstream approve record and the order's `fingerprint` (below); its message says what TraderOS will send and that nothing is sent before `place`. |
| Claude's `place_stock_order` call + Allow once | `ticket place <id> --policy FILE` | New: the sender. Its gates are below. |
| `cmd_decline` | `ticket decline` | Unchanged. |
| `cmd_result` | `ticket result <id> --status placed\|denied\|failed` | Unchanged statuses and rule ("a result can only follow an approval"); a fill goes into `--account`. `place` records `placed` (with Alpaca's order id and status) or `failed` (Alpaca's 4xx) itself: TraderOS saw the answer. |
| `cmd_fill`, `apply_fill`, `log_fill` | `ticket fill <id> --account FILE` (the placed order's and its legs' fills, read from the paper endpoint: upstream's "the fill from the response") or `ticket fill --symbol --side --qty --price --account FILE` (by hand, as its) | The account statement (the file `risk assess --account` reads) instead of trader/portfolio.json; a buy is paid from settled cash and a sale's proceeds go to the new optional `unsettled_cash` (S15 / RC17, the same reason as check.py's batch rule); no account is started from the budget (P7); Decimal (04.3). The file is rewritten atomically, and not at all if the result would be a statement the check cannot read. Each Alpaca fill is recorded once. |
| `cmd_flatten` | `ticket flatten <symbol> --policy FILE --account FILE [--qty Q]` | One step with the check: in TraderOS a ticket exists only for a checked order (ADR-009), so its two steps (flatten writes orders.json, then check.py) are one, and a refusal is raised, never ticketed. The qty comes from the statement or `--qty`, as its; `flat_by` from the policy or 15:55 (its fallbacks: the port's plan file has no `flat_by`). It supersedes earlier proposals (its "replaced N earlier proposals"). |
| Flatten step 5: "get_orders (status open), cancel_order_by_id each one (Needs approval), then place_stock_order" | `place` of a flatten ticket lists the open sell orders for the symbol, cancels each, waits until each is canceled, then sends | The cancels are on the ticket the person says yes to ("TraderOS first cancels your open TSLA sell orders ..."), so that yes covers them; upstream asked for a card per cancel. If a leg FILLED meanwhile (the stop hit), nothing is sent (`UNKNOWN_EXPOSURE`): a sell then would be a short. |
| `cmd_log` | `ticket log` | Its "N placed today." line; every approval, result, cancel and fill. |
| `--now` | Kept on every command but `place` | `place` measures the check's expiry by the real clock: a pinned `--now` could otherwise move it. |
| broker.json | Journal events, each with its record kept content-addressed in the store (`payload_hash` is the artifact's digest) | S19 stays ours (ADR-009). |
| config `"paper"` defaulted to true | `"paper": true` must be stated to place; anything but `true` (`false`, `"true"`, `1`) is refused when the policy is read | Spec 05.3: unknown is never permission; live is refused. `paper` and `broker` enter the checked policy only when stated, so every earlier policy keeps its hash. |

### `ticket place`: the gates, in order, each before any request

1. The policy names no broker but Alpaca and says `"paper": true` (stated), and it is the very policy the ticket's
   check used (`policy_hash`). *Reason (proposal of this build):* the yes and the check are for that policy; another
   one could have other limits.
2. A yes is recorded for this ticket (`approve`, word for word) and nothing has happened to it since: a declined,
   placed, denied or failed ticket is never sent again; one whose send was indeterminate waits for `ticket result`.
3. The yes is for this exact order: the upstream `fingerprint` (the order's fields and the plan's entry, stop and target)
   recorded with the yes equals the order's now. *Reason:* the upstream fingerprint binding; the ticket id already binds
   the plan hash, the fingerprint binds what is sent.
4. The check passed, has not expired, is not dated after the real time, and is the latest attempt. *Reasons:* #21
   (expiry; a missing `valid_until` is expired, ADR-009); a pinned `--now` cannot move an expiry; PR #2's drop_stale.
5. The paper keys are set and the environment points nowhere else (`credentials`).
6. The account the paper endpoint answers with looks like a paper account (`require_paper_account`).

Then, before the body leaves, `place` records the send as `indeterminate` (a write-ahead claim) and checks it is the
only sender of that ticket. Alpaca's answer replaces it with `placed` (its order id and status) or `failed` (its 4xx,
or a send that never left). A timeout, a 5xx or a crash between the send and the answer leaves the ticket "SENT,
OUTCOME UNKNOWN", never "approved", so it is never sent twice; the person records what happened with `ticket result`.
*Reason (proposal of this build):* the MCP server answered a timeout with "The order MAY have been placed"; with
nobody in between, TraderOS must record that itself, and a record written only after the answer would let a crash
leave a sent ticket looking unsent. `indeterminate` is the one status upstream did not have. An approved or indeterminate
ticket counts as placed today, and blocks new entries, until a result is recorded (PR #2 with the review's change 4,
as in ADR-009).

## State: where account truth lives

- **What check.py reads.** The portfolio is the person's account statement file (`--account`), as in ADR-009. The
  log (the upstream broker.json) is the journal (`decisions._log`: approvals with the approved ticket's qty, kind, max loss
  and stop; results; fills) followed by the statement's own `orders`. A stated outcome the journal already holds (the
  same ticket and status) is not counted twice.
- **Fills** update that statement file the way the upstream `apply_fill` updated portfolio.json (positions, cost-basis lots,
  settled cash; a sale's proceeds to `unsettled_cash`), and add a journal fill (its `log_fill`) that check.py's
  day-trade count reads.
- **Every placement, broker response (Alpaca's order id and status), failure, indeterminate send, cancel and fill** is
  a journal event (`journal.ticket.placed`, `.failed`, `.indeterminate`, `.canceled`, `.filled`; and `.approved`,
  `.denied`, `.declined`). A flatten is a `journal.plan` attempt and a `journal.ticket`.

## Other changes, each with its reason

| Change | Reason |
|---|---|
| `PaperTicket.kind` (contracts v1.2, additive, omitted when None): `"exit"` for a flatten ticket, which sells the long its `direction` names; it has no RiskPlan (its plan is the order, `FL-` id and hash) | A ticket must say whether it buys or sells; ADR-009's tickets were entries only. Every earlier ticket keeps its bytes and hash. |
| `PaperTicket.validate`: `kind` is `"exit"` or absent; an exit has no legs; quantity is positive | A ticket that can be sent must be well formed. |
| `ExecutionMode.PAPER` (contracts v1.2, additive); S17 gains it, stays EXPERIMENTAL, prerequisite "your own Alpaca paper keys", next gate "independent review of the paper order path" | The brief for this build; the registry says what a build enables. |
| Account statement: optional `unsettled_cash` | Where a recorded sale's proceeds wait (T+1); the check never spends it. |
| `traderos config set\|show` and `traderos/settings.py`; `doctor` reports whether paper keys are set (masked) and whether anything would make `place` refuse, with no request | Where the keys live; never printed. |
| `ticket account` | Upstream's "Check it: ask Claude for the Alpaca account info (read only)". |
| The ticket's disclaimer and note, `describe`, check.py's closing line, the CLI help, README and the package description | Wording only where the sender changed: "a document; nothing was or can be submitted" is no longer true. |
| `tests/security/test_no_order_path.py` becomes "no LIVE order path" | Network libraries only in adapters/ (kept); order endpoints only in the adapter; the live host only in the adapter's refusal list, every other Alpaca host the paper host; the host a constant nobody can pass; only `ticket place` submits; no module named for trading or brokers (kept). |

`claude_desktop_config.example.json` and SKILL.md's MCP set-up are not carried: TraderOS sends itself, so no MCP
server is set up for orders.

## Secrets

The keys go to Alpaca in its two headers and nowhere else. They are never in a message (Alpaca's own error text is
scrubbed), a record, the journal, the store, the account statement or any output; `config show` and `doctor` mask
them; `repr(client)` hides them. A test runs the whole flow (config, doctor, account, show, approve, place, fill, log,
journal, memory search, and a refused send whose answer echoes both keys) and greps every byte printed, every file
under the test's folder but settings.json, and a dump of the store's database for the key id, the secret and
fragments of each. TraderOS has no export yet (`export` is not built), so there is no bundle to grep; nothing that
could be exported holds a key.

## Live is refused in code: the proof

`tests/unit/test_paper_orders.py`, `tests/unit/test_alpaca_paper.py`, `tests/security/test_no_order_path.py`; no
network (a FakeAlpaca records every call; conftest fails any test that reaches the real transport):

- the live host, and ten other wrong addresses, refused before any call; the real transport refuses the live host
  before opening anything; no argument can pass another address;
- every endpoint variable pointing elsewhere, and `ALPACA_PAPER_TRADE` asking for live, refused before any call; the
  paper URL in the environment ignored;
- a live key id refused; an account not numbered like a paper account refused; no redirect followed;
- policy `paper: false`, missing, `"true"` (string), `1`: refused, through the CLI with the transport never called;
  the IBKR branch refused; a policy other than the one the check used refused;
- no yes, a declined ticket, a yes for another order, an expired check, a check dated after the real time, `--now`
  on place, a superseded ticket, no keys: each refused, transport never called;
- the body sent equals the upstream `tool_input` byte for byte for its fixtures without legs, and for its TSLA bracket equals
  the server's body and maps back to its `tool_input` byte for byte;
- a refused order is failed and never resent; an unknown one (a 5xx, a timeout, a crash mid-send) is never resent
  until its result is recorded; only one `place` can send a ticket; a send that never left is failed;
- what is sent carries exactly the fields of the order the upstream check passed (market and limit);
- secrets never appear in any output, file or the store.

**Mutation.** 44 mutants, one per live guard (the address checks, the environment, the key id, the account check, the
redirect handler, indeterminate vs rejected, the policy's `paper` and `broker`, `require_paper`, each gate in
`place`, the real clock, the account call, the filled-leg stop, the write-ahead claim and its single-sender checks,
the fingerprint recorded with the yes, the account line), each run against the order-path, risk and security tests
with `-B`, `PYTHONDONTWRITEBYTECODE=1`, every `__pycache__` cleared and `-p no:cacheprovider`: **44 red, 0
survived.** One of them (the client calling `check_url`) is caught only by the security test's reading of the
source, because with a constant base URL no run can show it.

The upstream tests ported for the restored half: TicketShape's two tool_input tests (unchanged); Flow's two-yes flow,
flatten by the close, flatten with the broker qty, result needs an approval, the IBKR test (now: refused) and fill
sell relieves the oldest lot. Not ported: DesktopConfig (the MCP server's config is not carried) and the malformed
broker.json test (broker.json is the journal; a refusal is typed and one line by the CLI contract).

**The suite:** 717 passed on Python 3.12.3 and on 3.13.18 (611 before this change), run one after the other;
`ruff check src tests tools` clean.

## Still deferred (gate P items)

- **Review change 5 to #2:** bind the fingerprint to the broker *account*. Not done: the yes is bound to the order,
  the policy and (by construction) the paper endpoint, not to an Alpaca account id. Normalizing Alpaca's numeric
  fields: not done; TraderOS sends strings, as upstream did.
- **Review change 2 to #21:** revalidate immediately before submission. Partly: `place` refuses an expired,
  superseded or future-dated check, but does not re-run the risk check against the paper account's own positions and
  buying power.
- **Reconciling with the broker** (the upstream step 1: get_orders and get_all_positions before a check): not built. The
  person's account statement stays the account truth; `ticket fill <id>` reads only the fills of TraderOS's own
  orders. No positions endpoint is used.
- **"Never from a scheduled or background task"** (the upstream SKILL.md): TraderOS cannot tell a person from a script
  running `ticket place`; it is a rule for the skill, which does not exist yet (spec 13.1).
- **Independent review** of this order path (S17's next gate), and a first run against a real Alpaca paper account
  by a person with paper keys: not done here (no network to Alpaca in this session).
- Live trading is not deferred: it is refused.

## Merge notes

`claude/traderos-sec` adds the same one-line `src/traderos/adapters/__init__.py`, its own `config set|show` (with
`sec_user_agent`, in commands/fundamentals.py) and a conftest no-network guard for the SEC transport. On merge, one
`config` command should hold both keys (`settings.KEYS` and `settings.SECRETS` here; the SEC key is not a secret),
and conftest should keep both guards.
