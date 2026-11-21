"""A fresh quote for a plan, for tests that assess a market entry.

Since the review of #10 (item 4, ADR-009) a market entry with no usable price BLOCKs, as a stale one does: the plan's
entry cannot show whether the price already ran through the stop. A test about something else gives its plan a quote
at the plan's own entry, stamped at the assessment's time, so the entry is measured exactly as before.
"""
from __future__ import annotations

import json
import pathlib


def quote_file(folder, plan_path: str, now: str, name: str = "prices.json") -> str:
    plan = json.loads(pathlib.Path(plan_path).read_text(encoding="utf-8"))
    sym = str(plan["symbol"]).strip().upper()
    path = pathlib.Path(folder) / name
    path.write_text(json.dumps({"prices": {sym: str(plan["entry"])}, "detail": {sym: {"time": now}}}),
                    encoding="utf-8")
    return str(path)
