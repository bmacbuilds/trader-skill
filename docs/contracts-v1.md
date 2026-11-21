# TraderOS contracts v1 (FROZEN)

**Status:** frozen 2026-11-20 by the integration owner (OSDev4) under spec v2.0 §02.3 and §05. Proposed
engineering choices recorded in ADR-001 to ADR-003. Not yet independently reviewed by a human; the reviewer named by
OSDev1 signs below when they have read it.

Every part builds against what is written here. A change to anything in this file needs an ADR, a migration or
compatibility fixture, a regenerated golden set and a note to every builder (spec §02.2). `tests/contract/` fails on
any accidental drift.

## 1. Where it lives

| What | Path |
|---|---|
| Records (the DTOs) | `src/traderos/contracts/` — import everything from `traderos.contracts` |
| Error codes and exceptions | `src/traderos/contracts/errors.py` |
| Canonical JSON and hashes | `src/traderos/contracts/canonical.py` |
| Core interfaces (Protocols) | `src/traderos/contracts/interfaces.py` |
| Public API signatures and owners | `src/traderos/api.py` |
| One valid example of every record | `src/traderos/contracts/examples.py` (`all_examples()`) |
| In-memory `BarReader` for testing now | `src/traderos/testing.py` (`MemoryBarReader`) |
| Golden fixtures (bytes + hashes) | `tests/fixtures/contracts/`; second shapes after an additive change in `variants/` (`examples.variants()`) |
| CLI spelling and exit codes | `docs/cli-contract-v1.md` |

## 2. Record rules (spec §05.1)

- Records are frozen standard-library dataclasses (ADR-001). Construct with keywords only.
- A **Record** is stored or sent on its own and carries `schema_version`, its class's `SCHEMA` (`"bar/1"`).
  A **Part** is a value nested inside a record.
- `from_dict`/`from_json` parse **untrusted** input strictly. They refuse unknown keys, missing keys,
  duplicate JSON keys, a float literal, NaN or Infinity, a naive timestamp, a wrong type, an unknown enum value and an
  unknown `schema_version` (`SCHEMA_UNSUPPORTED`).
- Construction refuses a float or non-finite value in a `Decimal` field and a naive `datetime`. No code path can build a
  record that would not survive its own round trip.
- Money, prices, quantities and limits are `Decimal`. Floats are allowed only inside computation and enter a record
  through `decimal_from_float(x, places)`. A non-finite result becomes `MetricValue(value=None, unavailable_reason=…)`.
- A number that may be missing is a `MetricValue`: either a value or a reason, never both, never zero for
  "unknown" (spec §09.4).
- **Units** on a `MetricValue` (proposed by OSDev6, adopted):
  - `"fraction"` for returns and changes, where `0.012` means 1.2%;
  - `"percent"` where `16.4` means 16.4%: only the SEC records' margins and growth (S04, S07), the convention of the
    upstream tools they are ported from (v1.3, ADR-011); a screen refuses a rule in another unit;
  - `"ratio"` for dimensionless ratios such as Sharpe or profit factor;
  - an ISO currency code (`"USD"`) for money and prices;
  - `"shares"`, `"contracts"` and `"bars"` for counts;
  - a feature's own unit (for example `"rsi"`) where it is a score.
- IDs are opaque strings. Use a readable prefix (`DS-`, `SV-`, `EX-`, `TR-`, `PP-`…); nothing parses them.
- **Four times:** `observed_at` (for a bar, `end`), `received_at`, `available_at`, `recorded_at`. A bar's
  `available_at` is never before its `end` (`LOOKAHEAD_RISK`).

## 3. Canonical form and hashes (spec §05.4, ADR-002)

- **Objects:** keys are sorted by Unicode code point, with no whitespace (`","` and `":"`). Output is UTF-8, and
  non-ASCII characters are written as themselves.
- **Decimal:** written as a string in plain notation, normalized. There is no exponent and no trailing zero after
  the point, and `-0` becomes `"0"`. So `1.50` → `"1.5"` and `1E+2` → `"100"`.
- **datetime:** written in UTC as `YYYY-MM-DDTHH:MM:SSZ`, with `.ffffff` added when microseconds are present.
  **date:** `YYYY-MM-DD`.
- **Other values:**
  - An int must be within ±(2⁵³−1).
  - bool, null, arrays (order kept) and enums (written as their value) are allowed.
  - A float is refused.
- **Artifact hash:** SHA-256 of the exact bytes. **Record hash:** SHA-256 of the canonical form, with
  `canonical_hash` itself left out. Both are written as 64 lowercase hex characters.
- **Helpers:** `with_hash(r)` fills `canonical_hash` in and `verify_hash(r)` checks it (`INTEGRITY_MISMATCH`).
  Renderings such as Markdown, the redacted share copy or a CLI table reference the record hash. Their own file
  hashes differ from it, and that is correct.

## 4. The records

| Schema | Class | Spec | Produced by |
|---|---|---|---|
| `instrument/1` | `Instrument` | 05.2 | data (A) |
| `bar/1` | `Bar` | 05.2, 06 | data (A) |
| `quote/1` | `Quote` | 05.2, 09.2 | data (A), only where rights allow |
| `dataset/1` | `DatasetManifest` (+ `QualityIssue`, `RightsLabel`, `Coverage`) | 05.2 Q01, 06 | data (A) |
| `provider-capabilities/1`, `data-batch/1` | `ProviderCapabilities`, `DataBatch` | 06.2 | adapters (A) |
| `feature/1` | `FeatureDefinition` | 07.1 | lab (A) |
| `strategy-draft/1`, `strategy-version/1` | `StrategyDraft`, `StrategyVersion` (+ `OwnerAction`) | 07.2 | lab (A) |
| `experiment-spec/1` | `ExperimentSpec` (+ `SplitWindow`, `AccountAssumptions`) | 08.3 | lab (A) |
| `trial/1` | `Trial` | 05.2, 08.3 | lab (A) |
| `challenge/1` | `ChallengeResult` | 10 I03 | lab (A) |
| `passport/1` | `EvidencePassport` (+ `UncertaintyReport`) | 10 I01, I08 | lab (A) |
| `ledger/1` | `LedgerEntry` | 09.3 | lab backtester (A) |
| `memory-event/1` | `ResearchMemoryEvent` | 10 I04 | lab (A) |
| `no-trade/1` | `NoTradeRecord` | 10 I05 | C, and B where a candidate is refused |
| `workflow/1` | `WorkflowResult` (+ `WorkflowStep`) | 10 I06 | integration (A) |
| `bundle/1`, `import-report/1` | `PortableBundleManifest`, `ImportReport`. A manifest's `created_at` is optional (additive, ADR-013): null unless the exporter is given a time, so the same profile exports the same bytes | 10 I07, 11.3 | lab (A) |
| `scan-criteria/1`, `scan/1` | `ScanCriteria`, `ScanResult` (+ `ScanCandidate`, `ConditionTrace`) | 12 S01 | **B** |
| `market-context/1` | `MarketContext` (+ `InstrumentMove`, `RegimeReading`) | 12 S03 | **B** |
| `entry/1` | `EntryAssessment` | 12 S13 | **B** |
| `exit-plan/1` | `ExitPlan` (+ `ExitScenario`) | 12 S14 | **B** |
| `alert-rule/1`, `alert-event/1` | `AlertRule`, `AlertEvent` | 12 S18 | **B** |
| `risk-plan/1`, `risk-assessment/1` | `RiskPlan` (v1.1 adds `plan_date`, `limit_price`; v1.3 adds `tick_size`, the instrument's tick an order price must sit on, ADR-009 review of #10), `HypotheticalRiskAssessment` (v1.1 adds `valid_until`, `policy_hash`, `account_hash`); ADR-009 | 12 S15/S16 | C |
| `paper-ticket/1` | `PaperTicket` (no broker, account, credential or order-id field exists: sending it to an Alpaca paper account is the adapter's, and what happened is a journal event; v1.1 adds `target_price`, ADR-009; v1.2 adds `kind`, `"exit"` for the upstream flatten ticket, which sells the long it names, ADR-012) | 12 S17 | C |
| `timemachine/1` | `TimeMachineReport` (+ `Scenario`, `ScenarioKind`); added in v1.1, ADR-006. Its subject is a strategy version or (additive, ADR-010) a `PlanReplaySubject` | 08.6 Q09, 12 S11 | lab (A) |
| `earnings/1` | `EarningsReport` (+ `QuarterLine`, `FilingFact`, `FilingLink`); added in v1.2, ADR-007; fields added in v1.3, ADR-011 | 12 S04 | C |
| `holdings/1` | `HoldingsReport` (+ `Holding`); added in v1.2, ADR-007; fields added in v1.3, ADR-011 | 12 S06 | C |
| `holdings-diff/1` | `HoldingsDiff` (+ `HoldingChange`): share counts at two quarter ends; added in v1.3, ADR-011 | 12 S06 | C |
| `copy-targets/1` | `CopyTargets` (+ `TargetWeight`, `SkippedPosition`): hypothetical weights only; added in v1.3, ADR-011 | 12 S06 | C |
| `fundamental-screen/1` | `FundamentalScreen` (+ `ScreenRow`); added in v1.2, ADR-007; fields added in v1.3, ADR-011 | 12 S07 | C |
| `capabilities/1` | `CapabilityRegistry` (+ `CapabilityEntry`; v1.2 adds the `ExecutionMode` value `paper`, ADR-012) | 01.2 | core (A) |
| `failure/1`, `receipt/1` | `Failure`, `ToolReceipt` | 05.3, 13.3 | everyone |

Status vocabularies are one enum per axis, never collapsed (spec §00.3): `CapabilityImplementation`, `UserCapability`,
`DatasetUsability`, `ExperimentOutcome`, `SourceDisposition`, `TestExecution`. Partial results carry
`completeness` (`Completeness`), `limitations` (`Limitation`) and `safe_for` (`SafeFor`). `SafeFor` has no "trade"
value. Results computed on tutorial data are `SafeFor.TUTORIAL_ONLY` and carry the `SYNTHETIC_DATA` limitation.

## 5. Errors (spec §05.3)

- **Raising:** a public function returns its record or raises a `TraderOSError` subclass. Each exception carries
  one `ErrorCode`, words for a person, and optional `limitations` and `details`.
- **Exception classes:** `ValidationError`, `DataError`, `DomainError`, `AuthorizationError`, `ResearchError`,
  `IncompleteEvidence`, `IntegrityError` and `CapabilityDisabled`.
- **Codes:** every code in spec §05.3, plus six that the spec names elsewhere: `CAPABILITY_DISABLED` (§01.2),
  `SCHEMA_UNSUPPORTED` (§11.3), `INVALID_INPUT` (§04.2), `PIT_LIMITED` (§06.3), `AVAILABILITY_UNVERIFIED` (§05.1) and
  `SYNTHETIC_DATA` (§06.1). v1.4 adds `UNOFFICIAL_DATA` (ADR-014): a limitation on every dataset `data fetch` makes
  from a free, unofficial source (Yahoo Finance's chart, or Stooq), whose rights allow no raw export and no
  redistribution. The same version adds the `SourceKind` values `yahoo_chart` and `stooq`. Both are additive.
- **On the wire** (CLI `--json`, the skill, the Machine): `err.to_failure()` gives a `failure/1` record.
- **Unknown codes:** a consumer treats a code it does not know as a refusal.
- **Unknown data** never becomes zero or permission.

## 6. The public API (spec §04.4)

The signatures are in `src/traderos/api.py`. Until its part lands, a function raises `CapabilityDisabled` naming its
owner. When a part lands, it replaces only its own function bodies.

| Function | Returns | Owner |
|---|---|---|
| `load_dataset`, `check_dataset` | `DatasetManifest` | A (OSDev4) |
| `parse_strategy`, `approve_strategy_version` | `StrategyDraft`, `StrategyVersion` | A |
| `register_experiment`, `run_experiment`, `falsify_experiment` | id, `tuple[Trial]`, `tuple[ChallengeResult]` | A |
| `build_passport`, `search_memory` | `EvidencePassport`, `tuple[ResearchMemoryEvent]` | A |
| `export_portable_bundle`, `import_portable_bundle` | `PortableBundleManifest`, `ImportReport` | A |
| `run_workflow` | `WorkflowResult` | integration (OSDev4) |
| `scan_market`, `market_context` | `ScanResult`, `MarketContext` | **B (OSDev6)** |
| `assess_entry`, `plan_exit`, `evaluate_alert` | `EntryAssessment`, `ExitPlan`, `AlertEvent` | **B (OSDev6)** |
| `assess_hypothetical_plan(plan, policy, *, account=None, prices=None, now=None, stress_gap="0.05")` (v1.1, ADR-009: spec §04.4 names `policy`) | `HypotheticalRiskAssessment` | C (OSDev4) |

None of them can place an order or send telemetry.

**Injected dependencies (agreed with OSDev6 on #1):** each part's implementation takes its `BarReader` and
`ExchangeCalendar` as arguments, for example `traderos.intelligence.scan(reader, calendar, dataset_id, criteria,
as_of)`. The functions in `api.py` resolve the defaults (the local store's reader and XNYS) and call them.
`evaluate_alert` also takes `previous_at`, the last evaluation, so a window that went unevaluated is reported as
missed rather than hidden.

## 7. Core interfaces and the as-of rule

`src/traderos/contracts/interfaces.py`:

- `ExchangeCalendar`: sessions, open/close in UTC, early closes, next and previous session, and the session at an
  instant. Outside its coverage it raises `CALENDAR_UNKNOWN` and never guesses. The real XNYS calendar is part of the
  core PR.
- `BarReader`: `manifest`, `instruments` and `bars(dataset_id, instrument_id, *, as_of, start=None, limit=None)`.
  - **It never returns a bar whose `available_at` is after `as_of`.** That one rule is how QT01 (no look-ahead)
    holds everywhere.
  - Bars come oldest first, and `limit` keeps the newest.
  - `tests/contract/test_bar_reader.py` is the conformance suite: the SQLite store must pass the same tests the
    in-memory fake passes.
- `Feature`: a versioned pure function from bars to aligned values. `None` means not yet usable (warm-up or a
  missing input), never zero.
- `DataAdapter`: the spec §06.2 provider protocol. An unsupported call is `OUTSIDE_CAPABILITY` and never falls back
  to lower-grade data on its own.
- `Capabilities`: the release registry. `require()` raises `CAPABILITY_DISABLED` with the next gate.

**Condition grammar:** a scan, an alert and a strategy's entry/exit share it (`ScanCriteria.conditions`,
`AlertRule.conditions` and strategy/1 `entry`/`exit`). It is spec §07.2's grammar:

- combinators `all` and `any`;
- comparisons `above` and `below`, as two-element lists of operands;
- operands are a bar field (`close`), a feature call (`{"sma": 200}`) or a decimal string.

There is one parser and evaluator, `traderos.strategies.conditions`, and one feature library, `traderos.features`
(spec §07.1, §07.3). Both are built by part B (OSDev6, #3, moved from A to B to keep B off A's critical path) and
imported by the lab for strategy/1 entry and exit. No part writes a second one.

## 8. Changing a contract

1. Write `docs/adr/ADR-###-….md` with the change, the reason, the migration and what each dependent part must do.
2. Bump the record's major version (`"bar/2"`) for any incompatible change, and keep a reader for `bar/1`.
   An **additive** change (a minor revision, e.g. v1.1) adds only optional fields that default to `None` and lists
   them in the class's `OMIT_IF_NONE`: they are left out of the canonical form while `None`, so every record written
   before them keeps its exact bytes and its hash (a store re-hashes what it reads). Keep the old golden file under
   `tests/fixtures/contracts/v1.0/` as the compatibility fixture (spec §05.1); `tests/contract/` reads it back
   byte for byte. A reader of the older revision still refuses a newer record that carries one of the new keys
   (strict parsing), so a newer record never passes silently for an older one.
3. Run `python tools/contracts/regen_golden.py` and commit the new golden files with the ADR.
4. Post the change on the wall to every builder before it merges.

## 9. Sign-off

| Role | Who | Date |
|---|---|---|
| Integration owner | OSDev4 | 2026-11-20 |
| Independent reviewer (human, spec §02) | _named by OSDev1_ | |
