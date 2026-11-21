# ADR-007: `earnings/1`, `holdings/1`, `fundamental-screen/1`, additive records in contracts v1.2

- **Status:** proposed by OSDev4, 2026-11-20. **Revised the same day by ADR-011** (the port of the upstream SEC tools):
  decisions 2, 3 and 4, the network client and the fixtures caveat changed, and each change is marked "Revised".
  The records grew additively in contracts v1.3 (ADR-011).
- **Change:** three new record types in `src/traderos/contracts/fundamentals.py`:
  - `EarningsReport` (`earnings/1`), with parts `QuarterLine` and `FilingFact`. This is S04.
  - `HoldingsReport` (`holdings/1`), with part `Holding`. This is S06.
  - `FundamentalScreen` (`fundamental-screen/1`), with part `ScreenRow`. This is S07.
- **Compatibility:** nothing existing changes. No record, field, hash or golden file moved. The golden set gains
  three files.
- **Why typed records and not free text:** spec §12.1 asks each of these for things a sentence can lose:
  - S04 needs the company, fiscal context, units, the filed (available) date of every number, amendments, and a
    derived-quarter flag.
  - S06 needs the reporting period kept apart from the filing date, the delay stated, and no invented trade time.
  - S07 needs a flag on every missing value.
  Each of these is a field. The record refuses construction without it:
  - A `HoldingsReport` whose filing date is before its period end is refused.
  - A `MetricValue` is either a value or a reason, never neither.

## Decisions inside the records

1. **Point in time is the filing date.** A number counts as known from the day it was filed, and only filings on or
   before `as_of` are read. A later restatement never changes an earlier answer. The time of day is not recorded,
   and each report says so (`AVAILABLE_ON_FILED_DATE`).
2. **A quarter is found from its own start and end dates.** A quarter spans 80 to 100 days and a year 350 to 380.
   Year-to-date figures (six and nine months) are never read as quarters. *Revised (ADR-011):* its fiscal label is
   upstream's: the `fy`/`fp` of the FIRST filing that reported the period (a later filing repeating it does not relabel
   it), placed in the period's own fiscal time when that first filing is a comparative, and Q4 when a three-month
   value was first filed in a 10-K.
3. *Revised (ADR-011):* replaced by decision 2's labels. A fiscal year with no 10-K yet still keeps its newest
   quarters: they are labelled by their own 10-Qs.
4. **Q4 is derived only when it can be:** the annual total minus the three filed quarters inside the same year.
   *Revised (ADR-011), per the settled verdict on upstream PR #24:* diluted EPS is derived the same way, and every
   derived series is flagged: a line is `derived` when any series shown in it was, it names them
   (`derived_series`), and a trailing total that includes one says so (`ttm_derived`). Derived EPS is approximate,
   because share counts change during a year, and the report says that (`DERIVED_Q4`). The earlier rule (a derived
   Q4's EPS is unavailable) is retired: it made trailing EPS unavailable for every company that does not file a Q4.
5. **Trailing-twelve-month figures need four consecutive quarters.** If a quarter is missing, the metric is
   unavailable and says why. It is never a sum over a gap.
6. **A screen never drops a company and never guesses:**
   - A company whose filings cannot be read gets a row with every metric unavailable.
   - A criterion that cannot be evaluated makes the row `passed: null`, unless another criterion already failed.
7. **13F values are dollars.** Filings before 2023-01-03 reported thousands; they are converted and labelled
   (`VALUE_UNITS`). Every holdings report carries `REPORTED_WITH_DELAY` and `LONG_ONLY_DISCLOSURE`.

## The network client (spec §06.1 adapter (d))

`src/traderos/adapters/sec.py` is the only code in the Free package that opens a connection. It does so only when
a person runs `earnings`, `holdings` or `fundamentals screen`.

- **Identification is the person's own.** It comes from `SEC_USER_AGENT` or
  `traderos config set sec_user_agent "Name email"`. Without it the command refuses (`NOT_AUTHORIZED`) before any
  request. No name, address or domain of ours appears in the code, and a test checks that.
- **Fair access:**
  - It sends at most 4 requests a second (the SEC's limit is 10).
  - Requests have a 30-second timeout and a 60 MB cap, which *(revised, ADR-011)* also holds after decompression.
  - It talks only to `www.sec.gov`, `data.sec.gov` and *(revised, ADR-011)* `efts.sec.gov` (the filer-name search),
    over HTTPS.
- **Reproducible:** every response is cached under `~/.traderos/cache/sec` with the time it was fetched. That time
  becomes the report's `retrieved_at`, and part of its id. A rerun within 12 hours reads the same bytes.
- **The XML of an information table is untrusted.** Any DOCTYPE or ENTITY is refused before parsing, and its size
  and holding count are capped.

## What this does not claim

- *Revised (ADR-011):* **the fixtures are real SEC filings, but not independent.** The fictional fixtures are
  retired. The tests now run on the upstream project's own fixtures (Apple and Coca-Cola company facts and submissions, Berkshire
  Hathaway's 13F filings for two quarters), real SEC responses captured and trimmed by the upstream author (MIT,
  `tests/fixtures/upstream-trader-skill/`). S04's acceptance asks for an *independent* XBRL reference fixture,
  checked by hand against a real filing; these were captured by the upstream author, not by an independent tester,
  so one is still needed before S04, S06 or S07 can move past `EXPERIMENTAL`.
- The SEC's fair-access terms must be revalidated by the implementer (spec §21). This build uses the limits
  published on the SEC's page on 2026-11-20.
- S04, S06 and S07 stay `EXPERIMENTAL` in the registry until independent review.

- **Dependent builders:** none affected.
