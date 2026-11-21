# Upstream fixtures for the SEC port (ADR-011)

These files are the upstream project's own test fixtures, copied byte for byte from the tank (`tank/day-trader-skill/` for
the analyst, `tank/trader-skill/` for the copycat and the screener). They are MIT licensed: the notice is
`NOTICE`, next to them. `PROVENANCE.csv` names each file's upstream repository, commit, path and SHA-256,
and `tests/unit/test_sec_port.py` re-hashes every file against it.

What they are:

- `trader-analyst/`: SEC responses for Apple (CIK 320193) and Coca-Cola (CIK 21344), captured by the upstream author
  and trimmed to fiscal 2024 to 2026. Real filings, not made up. Coca-Cola's company facts stop at 2026-04-03 while
  its submissions list a 10-Q for 2026-07-03: the "facts lag" case.
- `trader-copycat/`: Berkshire Hathaway's 13F-HR filings for the quarters ending 2026-03-31 and 2026-06-30 (both
  information tables, the newest cover page), its submissions list, an excerpt of the SEC ticker list, an EDGAR
  filer-name search, and OpenFIGI's answers for some share-class CUSIPs (used only to cross-check the bundled
  `share_classes.json`; TraderOS does not call OpenFIGI).
- `trader-screener/`: a metrics file, a CSV and a prices file in the upstream format, and the upstream example rules.

They were captured by the upstream author, not independently by TraderOS. S04, S06 and S07 stay `EXPERIMENTAL` until
an independent tester captures and checks fixtures against the filings themselves (ADR-007, ADR-011).
