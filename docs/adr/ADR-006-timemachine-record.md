# ADR-006: `timemachine/1`, an additive record in contracts v1.1

- **Status:** proposed by OSDev4, 2026-11-20.
- **Change:** a new record type, `TimeMachineReport` (`timemachine/1`), with its parts `Scenario` and
  `ScenarioKind`.
- **Compatibility:** nothing existing changes. No existing record, field, hash or golden file moved, and the golden
  set gains one file (`timemachine_v1.json`). A reader built on v1.0 never meets this record unless it asks for it.
- **Why:** S11 needs a typed result that keeps OBSERVED history, RESAMPLED (generated) paths and STRESS (generated)
  shocks apart, each with its method and assumptions (spec §08.6 Q09, §12.1 S11). A report without the observed
  history is refused at construction.
- **Dependent builders:** none affected. OSDev6 was told on the wall.
