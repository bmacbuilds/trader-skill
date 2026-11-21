# Importing your own bar data: the CSV contract v1

`traderos data import my-bars.csv` loads a file you are allowed to use (spec §06.3). Nothing is downloaded, nothing
is sent anywhere, and the original file is kept byte for byte in your local store.

## Columns

Required, in any order: `symbol,timestamp_utc,open,high,low,close,volume`.

Optional: `available_at_utc,interval,source,feed,adjustment,currency,complete,rights_label`.

Any other column is refused, so a misspelled column never disappears quietly.

## Timestamps

- **Format:** RFC 3339 with `Z` or an offset, such as `2026-11-20T21:00:00Z` or `2026-11-20T16:00:00-05:00`.
- **Dates on their own and times without a zone are refused.** Reading them would mean guessing a timezone.
- **`timestamp_utc` is the bar's start.** An intraday bar runs from that instant for one interval. It is cut short
  if the session closes first, for example at 13:00 on a half day.
- **A daily bar names a New York session.** The session's open, its close and New York midnight (with its offset)
  all name the same session. The stored bar runs from that session's open to its close.
- **Midnight UTC is refused for daily bars.** It is the evening before in New York, so reading it would be a guess.
- **`available_at_utc` is when the bar became usable**, and it must not be before the bar ended.
  - Without it, each bar is taken as usable when it ended.
  - The dataset is then marked `PIT_LIMITED`, which means research only. It never certifies when a vendor really
    published the bar.

## What is refused (the whole file, nothing repaired)

| Code | Meaning |
|---|---|
| `UNREADABLE_VALUE` | A value that is not a number, a timestamp without a zone, a text cell that starts like a spreadsheet formula (`= + - @`), or a cell over 64 characters |
| `AMBIGUOUS_TIMESTAMP` | A daily bar stamped at midnight UTC |
| `NOT_A_SESSION` | A bar on a day the exchange was closed |
| `IMPOSSIBLE_BAR` | High below the open or close, low above them, a non-positive price, or negative volume |
| `DUPLICATE_BAR` | Two bars for one symbol at one time |
| `LOOKAHEAD_AVAILABILITY` | `available_at_utc` before the bar ended |
| `CALENDAR_UNKNOWN` | A date outside the calendar's coverage (2008 to 2027) |
| `MIXED_*` | One file mixing intervals, sources, feeds, adjustments or rights labels |
| `EXTRA_CELLS` | A row with more cells than the header |

## What is recorded and kept (warnings)

These issues are kept with the dataset and travel with every result built on it:

- `MISSING_SESSION`: a session between the first and last bar has no bar.
- `ZERO_VOLUME`: a bar has zero volume.
- `LARGE_MOVE_INVESTIGATE`: an open more than 25% from the previous close. It may be a split, a data error or a
  real event.
- `OUTSIDE_SESSION`: an intraday bar outside regular hours.
- `INCOMPLETE_BAR`: a bar marked `complete=false`. It is never used as an input.

## Limits every CSV dataset carries

- `IDENTITY_FROM_TICKER`: instruments are identified by ticker.
- `SELECTION_BIAS`: the universe is the symbols in your file, chosen today.
- `TICK_SIZE_ASSUMED`: the tick size is taken as 0.01.
- `ADJUSTMENT_UNKNOWN`: shown when the file does not say whether prices are adjusted.

## Size limits

256 MB per file, 5,000,000 rows, 64 characters per cell.

## The tutorial dataset

`traderos init` loads a synthetic tutorial dataset, so you can try everything with no account and no download.

- It has seven fictional instruments (`TUT…`) with daily bars on real NYSE sessions, 2016 to 2025.
- It goes through this same gate.
- It is labelled `SYNTHETIC_DATA` everywhere. Its prices are made up and are never evidence about any market.
