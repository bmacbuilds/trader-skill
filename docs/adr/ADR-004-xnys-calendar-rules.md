# ADR-004: The XNYS calendar is rules plus dated exceptions, with no timezone database

- **Status:** proposed by OSDev4, 2026-11-20.
- **Spec:**
  - §04.3: exchange-local time derived from a real calendar.
  - §20.1 E01: the calendar library choice is an ADR.

## Decision
`traderos.clock.XNYS` encodes NYSE holiday rules, early-close rules and a dated list of special closures (Sandy 2012,
the Bush and Carter mourning days). It covers 2008-01-01 to 2027-12-31. Outside that window it raises
`CALENDAR_UNKNOWN` and never guesses. U.S. Eastern offsets come from the DST rule in force since 2007, so no
`zoneinfo`/`tzdata` is needed (ADR-001).

## Evidence
`tests/unit/test_clock.py` checks the following against the NYSE's published figures:
- the trading-day count for every year 2008 to 2025;
- the 2026 holiday list and early closes;
- the daylight-saving shifts of the UTC open.

## Costs and what would change this
- New special closures must be added by hand, with a `VERSION` bump.
- Coverage must be extended each year. `traderos doctor` fails when today falls outside it.
- If an approved calendar library proves more reliable, it replaces this behind the same `ExchangeCalendar`
  interface.
