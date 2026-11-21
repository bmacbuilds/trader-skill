# Time Machine test fixture: TEST-ONLY, NEVER SHIPPED, DATA RIGHTS UNVERIFIED

`TSLA-60d5m.json.gz` is the upstream test fixture from `tank/day-trader-skill/tests/fixtures/trader-timemachine/` at
`3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd` (git blob `791c3cfd58e9e046df5ef072224949fd22b7b2a0`, SHA-256
`b326efd64aa807e93325cff52770976e0820610a9cb9442f17dcfc5cffc9ef9f`, 92,941 bytes). Copied byte for byte.

- **What it is:** one response of Yahoo Finance's unofficial chart endpoint (`range=60d&interval=5m`) for TSLA,
  fetched upstream on 2026-10-19 at about 10:49 New York time. It holds 4,619 five-minute bars, 2026-07-27 to
  2026-10-19, including the bar in progress at the fetch and one live tick off the 5-minute grid.
- **Rights: unverified.** The upstream repository is MIT (`NOTICE`, kept here as its notice requires), but the
  prices inside the file are Yahoo's data, and nobody has checked Yahoo's terms for them. Spec 06.1 holds Yahoo's
  unofficial collection pending clearance, and spec 06.5 says vendor data never flows anywhere it is not cleared to
  go. So:
  - it is used **only by the tests** (`tests/unit/tm_fixture.py` turns it into a stored 5-minute dataset in a
    throwaway home);
  - it is **never** packaged: setuptools ships only `src/` and the package data named in `pyproject.toml`, and the
    public release copies from an allowlist that names no test fixture;
  - it is **never** used as evidence about TSLA or about any setup. Results from it only check arithmetic;
  - before any public package or repository includes the tests, either clear the rights or replace this file
    with a licensed or synthetic one. That is a release blocker, not a style note.
- **Why it is kept:** it is the only fixture whose numbers upstream published (the reel). The legacy test proves the
  port reads the same 59 sessions upstream read, bit for bit.
