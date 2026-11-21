# ADR-005: verdict/1, the matched baseline, and the numbers that need sign-off

- **Status:** PROPOSED by OSDev4, 2026-11-20, except the minimum trade count, which the founder signed off (below).
  The other numbers need the independent quant reviewer's and the founder's sign-off before any user-facing
  qualification claim (spec §09.4). Until then every Passport says "verdict/1 (ADR-005: trade minimum signed off by
  the founder 2026-11-20; the other numbers proposed; independent quant review pending)".
- **Founder sign-off, minimum trade count (2026-11-20).** OSDev1 asked Brian "should TraderOS require 100 trades
  before it calls a strategy worth paper trading?" and he answered "Y" (OWNER post on the wall, 15:59 UTC). So
  `research.MIN_TRADES` is **100**, the same as the Time Machine's (ADR-010). His yes covers only the trade minimum;
  every other number in the table stays proposed, and the independent quant review (spec §09.4) is still pending.
- **Spec:**
  - §09.4: verdict semantics, and no silent adoption of V1's 30/100 trades or 0.95.
  - §10 I03: FS01 to FS10.
  - §08.4: the holdout.

## The matched baseline
- A strategy that is long a rising market beats cash without any edge. So the edge is measured against random-entry
  controls.
- Each control enters at random, at the strategy's own entry rate on each instrument, and exits by the strategy's own
  exits and stop. It uses the same costs, cash, sizing and window.
- Edge is the strategy's daily return minus the mean daily return of 8 controls. Its 95% interval comes from a
  stationary bootstrap: 1000 resamples, expected block length max(5, n^(1/3)), seeded.

## verdict/1
- **rejected**:
  - FS09 fails: later data changed earlier decisions; or
  - the in-sample edge's whole 95% interval is below zero.
- **candidate_for_paper**, only when all of these hold:
  - FS01, FS02, FS04, FS08, FS09 and FS10 all **pass**. A challenge that was not performed never counts.
  - There are at least **100** in-sample trades (founder sign-off 2026-11-20; it was V1's 30 while proposed).
  - The holdout is not compromised.
  - The dataset is not synthetic.
  - The state is research only; it never means permission to trade.
- **insufficient_evidence**: everything else. That includes every result on tutorial data.
- **Diagnostics only:** FS03, FS05, FS06 and FS07 are reported and never gate the verdict. A positively skewed
  strategy can legitimately fail FS06.

## Numbers to sign off
| Number | Value | Where |
|---|---|---|
| Minimum in-sample trades | **100, signed off by the founder 2026-11-20** (was 30, proposed) | `research.MIN_TRADES` |
| Controls | 8 | `challenges.CONTROLS` |
| Interval | 95%, stationary bootstrap, 1000 resamples | `research.statistics` |
| FS02 | edge must stay positive at 2x costs (3x reported) | `challenges.fs02` |
| FS03 | 75% of ±20% neighbors keep a positive edge | `challenges.fs03` |
| Default windows | 60/20/20 by session count after warm-up | `research.default_splits` |
| Minimum observations for Sharpe and bootstrap | 30 sessions | `research.statistics.MIN_OBSERVATIONS` |

## Calibration evidence so far
- On the tutorial dataset, FS01's fresh random control does not qualify against the matched baseline.
- The bundled example strategy's interval crosses zero, so FS08 fails.
- Both are synthetic-data observations, not calibration on real markets. Calibration on licensed real data is a
  reviewer task before sign-off.
