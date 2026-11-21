# `data fetch` test fixtures: TEST-ONLY, UNOFFICIAL, NEVER SHIPPED

Recorded for `tests/unit/test_prices.py` (ADR-014). The tests read these files through a fake transport; no test
reaches the network (`tests/conftest.py` keeps the guard).

| File | What it is | Recorded |
| --- | --- | --- |
| `yahoo-NVDA-5y1d.json.gz` | Yahoo Finance's public chart endpoint, `NVDA?range=5y&interval=1d`, one real request, HTTP 200: 1,255 daily bars, 2021-11-22 to 2026-11-20. Gzipped, byte for byte (141,045 bytes before gzip). | 2026-11-21 18:27 UTC |
| `yahoo-NVDA-60d5m.json.gz` | The same endpoint, `NVDA?range=60d&interval=5m`, one real request, HTTP 200: 4,681 five-minute bars, ending with Yahoo's zero-volume closing-print stub at 16:00 New York on 2026-11-20. Gzipped, byte for byte (432,413 bytes before gzip). | 2026-11-21 18:27 UTC |
| `yahoo-NOSUCHTICKERZZ-404.json` | The same endpoint for a ticker that does not exist: HTTP 404 with Yahoo's own error JSON. Byte for byte. | 2026-11-21 18:27 UTC |
| `stooq-NVDA-1d-SHAPE-ONLY.csv.gz` | **Not a Stooq answer.** stooq.com could not be reached from the build environment (the agent proxy closed the tunnel: `ws_closed_mid_exchange`, 2026-11-21 18:27 UTC). The upstream tank has only Stooq's one-row *quote* answers (`stooq-AAPL.csv`, `stooq-ZZZZ.csv`), not its daily history. So this file is written in Stooq's daily-history CSV shape (`Date,Open,High,Low,Close,Volume`) from the Yahoo daily fixture's own bars, rounded to 4 decimals. It tests the Stooq parser and the fallback path only; its numbers are Yahoo's. | built 2026-11-21 |

The responses are kept whole, gzipped, rather than trimmed: about 165 KB in all, and the full 60 days are what the
plan replay and the ORB study read.

**Rights: unofficial, test-only.** Both sources are free and unofficial (the upstream README): neither licenses its data.
The prices are used only to check TraderOS's arithmetic and plumbing, never as evidence about NVDA or any strategy,
and never packaged: setuptools ships only `src/`, and the public release copies from an allowlist that names no test
fixture. Before any public repository includes the tests, clear the rights or replace these files with synthetic
ones (the same release blocker as `tests/fixtures/timemachine/`).

SHA-256:

```
81575f083a2d1dc2a16ddd4e80654274c0a4b64905fcb92d0fa98123338b57de  yahoo-NVDA-5y1d.json.gz
92f53b045f3bf6b4be8ffd463fc5328c469219a038a6645d76acd890c29e9e31  yahoo-NVDA-60d5m.json.gz
f0b5f9feeac9f1fc93c31b1edc03fbc1fda4acb3ec5e233ebb39d6f8b66e1769  yahoo-NOSUCHTICKERZZ-404.json
a8f0f47bc1e7282623eb8d6a00fa9050ebc9f1434b9e89aa8d5bf85e2b3b098e  stooq-NVDA-1d-SHAPE-ONLY.csv.gz
```
