# ADR-014: `traderos data fetch`, the upstream free Yahoo and Stooq fetch, through the import path (contracts v1.4, additive)

- **Status:** proposed, 2026-11-21, OSDev4 (branch `osdev4/data-fetch`).
- **Why:** /trader could test only a CSV the person imports, so a person who types NVDA got nowhere, and the
  founder's demo needs real tickers. The founder's instruction, as relayed to OSDev4 by the team lead on 2026-11-21
  (not seen first-hand in this session): model it on the upstream repository, with no artificial limits.
- **Owner rule (11-19, posted by OSDev1 on 2026-11-20):** code is rewritten only for a clear flaw or a clearly better
  method. So the upstream functions are ported, not rewritten, and every change is named below with its reason.
- **What this reverses, and what it does not:** ADR-010 kept Yahoo out of the Time Machine because spec §06.1 holds
  Yahoo's unofficial collection. That hold stays on the *Time Machine*: it still reads only stored bars. What changes
  is that a person may now put Yahoo's (or Stooq's) bars into their own store with one named command, and every
  dataset made that way says so (`UNOFFICIAL_DATA`) and may never be exported or redistributed. Whether §06.1's
  hold is cleared for a public release is a release-gate question for the founder; this ADR does not decide it.
- **Spec:** §04.3 (internet only through a registered adapter), §06.1, §06.3, §06.4, §06.5, §05.3, §11.2.

## What it is

`traderos data fetch SYMBOL [SYMBOL ...] [--interval 1d|5m] [--range R] [--name N]`
(`src/traderos/commands/data.py`, `src/traderos/adapters/prices.py`).

- **Daily** (`1d`, default range `5y`): Yahoo Finance's public chart endpoint; if Yahoo cannot answer, Stooq's daily
  CSV. **5-minute** (`5m`, default `60d`, which is what Yahoo keeps): Yahoo only, since Stooq has no intraday bars.
  The output and the result say which source answered, and why the fallback did when it did.
- One fetch is one dataset, from one source: if Yahoo cannot answer for any symbol, Stooq answers for all of them, so
  a dataset never mixes two vendors' prices (the gate would refuse mixed adjustments anyway).
- The bars go through exactly the path `data import` takes (`traderos.data.import_into`): the CSV contract, the
  quality gate, a content-addressed `DS-` id, the raw CSV kept byte for byte, a memory event.
- The manifest records the source (`source_kind` `yahoo_chart` or `stooq`, `provider` `yahoo-chart` or `stooq`), the
  fetch time (`recorded_at`, and the date in the default name), and the limitation `UNOFFICIAL_DATA`: "Free and
  unofficial" (the upstream README). Its rights label (`free-unofficial-source: ...`, stated by `traderos`) allows neither
  raw export nor redistribution. Bundles are gated off this release anyway.
- **Fetching again** never fails the integrity check: the same bars (the market closed, the same day) build the same
  id and replay the stored dataset; a later day's fetch has a new dated name, so it is a new dataset; new bars on the
  same day are a new file, so a new id.
- It is an explicitly named adapter command, so it may use the network (docs/cli-contract-v1.md). Nothing else does.

## Ported from the tank (tank/day-trader-skill at `3d0f4ef`, MIT)

| Ours (`adapters/prices.py`) | Upstream's | Changed |
| --- | --- | --- |
| `YAHOO_HOSTS`, `PAUSE`, `BACKOFF` | `trader-alerts/alerts.py:60-63` | Unchanged (2.0 s backoff, alerts' value; entry's is 5.0). |
| `UA` | `alerts.py:60` | Names TraderOS and its version, in its `Mozilla/5.0 (compatible; ...)` form. Its user agent names its project and its address; ours must name no person and no address (the brief). |
| `_get` | `alerts.py:_get` | Calls our transport (below) instead of `urllib.request.urlopen`, and asks for gzip. A network failure is a typed `DataError`, not its `AlertError`. |
| `yahoo_symbol` | `trader-entry/entry.py:230` | Unchanged. |
| `yahoo_url` | `alerts.py:yahoo_url` + `entry.py:chart_path` | Takes `rng` and `interval` (its alerts URL asks for `range=1d&interval=1d`, a quote; history needs entry's `chart_path` parameters). |
| `fetch_yahoo` | `alerts.py:fetch_yahoo` + `entry.py:yahoo_get` | (1) No answer from query2 moves on to query1, as `yahoo_get` does (alerts gave up). (2) Any 4xx carrying Yahoo's error JSON is returned as the answer, not only a 404, so its own reason is read (a 5m range past 60 days is a 422). (3) Typed errors. |
| `series_from_result` | `trader-scanner/scan.py:285` | Unchanged. |
| `parse_yahoo` | `alerts.py:parse_yahoo` + `scan.py:parse_chart` | Returns the chart's series (scan's) instead of one quote (alerts'), keeping its "unexpected" and "no data: <Yahoo's description>" refusals, typed. Yahoo's own "no data" is marked so the fetch does not fall back to Stooq for a ticker Yahoo has said does not exist. |
| `stooq_url` | `alerts.py:stooq_url` | Its symbol mapping (`.us`, `.` to `-`); the path is Stooq's daily history (`/q/d/l/?s=...&i=d`) instead of its one-row quote (`/q/l/...`). |
| `parse_stooq` | `alerts.py:parse_stooq` | Every dated row, not the first; its `N/D` rule and its float-or-None `f()` kept. An answer with no `Date` column (Stooq's "No data") is its "Stooq has no data for X (check the ticker)", typed. |
| `_lines` (complete flag) | `scan.py:daily_closes` | Its rule that today's daily bar is not complete until the session closes is kept, but the bar is written `complete=false` instead of dropped: the store keeps an incomplete bar and never uses it (spec 06.4). Its "a late duplicate (the live tick) wins" is kept. |
| `chart_csv`, `_lines` | `tests/unit/tm_fixture.py:yahoo_csv` (ours) | Moved into src; the fixture helper now calls it and its bytes are unchanged (checked against the TSLA fixture). |

## The rest of the changes, each with its reason

1. **An exact-host allowlist** (`query2.finance.yahoo.com`, `query1.finance.yahoo.com`, `stooq.com`; HTTPS, no
   user, no port), checked before every request and again in the transport; a redirect is followed only to an
   allowed host. The upstream `urlopen` follows any redirect anywhere. Spec §04.3 and our adapter pattern (`sec.py`,
   `alpaca_paper.py`).
2. **A timeout (20 s, its value) and a 16 MB size cap that holds after gzip** (`sec._gunzip`, which now takes the cap
   as a parameter). Upstream reads whatever arrives. Spec §11.2: a free endpoint can send anything.
3. **A timeout or a refused connection is `MISSING_DATA`, a typed refusal (exit 3)**, never a traceback.
4. **Yahoo's zero-volume closing-print stub is left out**: on the last day Yahoo appends a bar that starts at 16:00
   New York with no volume. Kept, it would be a bar outside regular hours in every 5-minute dataset. It is counted
   and named in the output ("Left out: 1 Yahoo closing-print stub(s) at 16:00"), as are bars without a price
   (tm_fixture's rule) and bars before the XNYS calendar's 2008 start (`--range max`), which the gate cannot place.
5. **The Stooq fallback is trimmed to the asked range** (Stooq sends its whole history), and Stooq bars are stamped
   with their session's open, since Stooq gives a date only and the CSV contract refuses a zone-less date.
6. **Adjustment:** Yahoo's chart OHLC is split-adjusted, not dividend-adjusted, so Yahoo bars are `split`. Stooq does
   not say, so its bars are `unknown` and carry `ADJUSTMENT_UNKNOWN`.
7. **Symbols are the CSV contract's** (`^[A-Z][A-Z0-9.\-]{0,14}$`), checked before any request.

## Contracts v1.4 (additive)

- `ErrorCode.UNOFFICIAL_DATA` (a limitation code, as `SYNTHETIC_DATA` is) and `SourceKind.YAHOO_CHART`,
  `SourceKind.STOOQ`. No record's shape changes; the golden fixtures are unchanged.
- `ImportOptions.limitations`: a source's own limits, appended to the manifest's.
- docs/contracts-v1.md §5 and docs/cli-contract-v1.md name them.

## Scope: stocks and ETFs. Crypto, forex and futures, tried once (2026-11-21)

Not built: the engine (calendar, sessions) is not widened for them. One real request each showed none flows
through unchanged, and none needs only a small change:

- **BTC-USD**: the ticker passes and Yahoo answers, but Yahoo stamps crypto days at 00:00 UTC, which the CSV contract
  refuses for daily bars (`AMBIGUOUS_TIMESTAMP`), and it trades on weekends (`NOT_A_SESSION`). It needs a 24/7
  calendar.
- **EURUSD=X, ES=F**: refused before any request, since `=` is not in the contract's ticker rule. With it allowed,
  EURUSD=X daily is stamped at midnight UTC (refused) and ES=F daily has bars on XNYS holidays (Thanksgiving:
  refused); their 5-minute bars pass the gate only as `OUTSIDE_SESSION` warnings, read against stock-market hours
  they do not keep. They need their own calendars and sessions.

## Tests and fixtures

`tests/unit/test_prices.py`, offline, on fixtures recorded with one real request each (tests/fixtures/prices/README.md):
Yahoo daily NVDA 5y, Yahoo 5m NVDA 60d, a Yahoo 404 error body. stooq.com could not be reached from the build
environment, so the Stooq fixture is a file in Stooq's daily CSV shape built from the Yahoo daily bars, and says so.
The conftest network guard covers this adapter too.
