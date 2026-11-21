"""The upstream Time Machine tests, ported with the plan replay (ADR-010).

Source: tank/day-trader-skill/tests/test_timemachine.py at 3d0f4ef with PRs #25 and #53 applied and #2's Time
Machine tests merged by hand (MIT; see NOTICE). Its class and test names are kept. What
changed, and why:

* The data is its Yahoo TSLA fixture, imported as a stored 5-minute dataset (tests/unit/tm_fixture.py); no network.
* `Reel` became `LegacyReel`: the reel's bit-for-bit numbers come from its independent-bar draw, which S11 forbids
  as a result. They are kept as a test of the data path (the port reads exactly the 59 sessions the reel read) and
  of its arithmetic, nothing more. `Reel` now checks the session replays on the same plan.
* `Network` is gone (no network); `Cli` drives `traderos timemachine plan` against a store.
* Numbers that changed by a named change say so: per-leg costs, the edge's stationary-bootstrap interval, and the
  "strongly" label waiting for ADR-010's sign-off. The trade minimum (100) is signed off by the founder (2026-11-20,
  OWNER post on the wall); the other thresholds stay proposed and the independent quant review is pending.

Below its tests: the review's listed cases (#25, #53, #2) and a regression test for every flaw ADR-010 names.
"""
from __future__ import annotations

import hashlib
import inspect
import io
import json
import random
import statistics
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, time, timedelta

import pytest

from tests.unit import tm_fixture as F
from traderos import cli
from traderos.clock import XNYS, eastern_to_utc
from traderos.contracts import ErrorCode, ScenarioKind, TimeMachineReport, ValidationError, from_json, to_json
from traderos.research import DEFAULT_COSTS
from traderos.research.statistics import stationary_bootstrap_mean
from traderos.store import Store
from traderos.timemachine import plan as tm

READER, DS = F.reader()
NOW = "2026-10-19 10:50"
NOW_UTC = tm.parse_now(NOW)

# the reel's plan, as trader-exit writes it (CONTRACT.md)
PLAN = {"created_by": "trader-exit", "symbol": "TSLA", "side": "buy", "date": "2026-10-19", "setup": "orb",
        "entry": 364.18, "entry_time": "10:15 AM", "entry_bar": 8, "interval": "5m",
        "opening_range": {"high": 362.18, "low": 354.47, "minutes": 15},
        "stop": 358.32, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 370.04, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 5.86, "reward_risk": 1.0, "flat_by": "15:55"}

# fingerprints of the reel's refs/sim-out.json (sim.py mid 1, seed 19): proves bit-for-bit equality
SIM_BANDS = "b9b7c006a733c68b"
SIM_PATHS = "90f313ef40c576a0"
SIM_TRADES = "0058bef72b0dcdee"
SIM_HIST = {"lo": 351.75, "hi": 377.39,
            "bins": [107, 109, 128, 199, 252, 331, 465, 571, 689, 694, 757, 811, 761, 732, 686, 558, 503, 452,
                     327, 244, 160, 125, 102, 78]}
LEGACY = {"method": "independent", "fill": "close", "cost_bps": 0, "until": "16:00"}   # the reel's accounting


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=None, now=NOW_UTC, **kw):
    reader, ds = (READER, DS) if text is None else F.reader(text)
    return tm.time_machine(reader, ds, p or plan(), now, **kw)


def et(ts):
    return tm.new_york(datetime.fromtimestamp(ts, UTC))


def code_of(cm):
    return cm.exception.code


class LegacyReel(unittest.TestCase):
    """The upstream reel, bit for bit, through the store: the port reads the bars upstream read. LEGACY: not a
    result."""

    @classmethod
    def setUpClass(cls):
        cls.doc = machine(**LEGACY)

    def test_the_reel_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "16:00"))
        self.assertEqual(m["pool_sessions"], 59)
        self.assertEqual(m["bars_left"], 69)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-07-27", "2026-10-16"))
        self.assertEqual(m["p_target"], 0.2938)
        self.assertEqual(m["p_stop"], 0.2548)
        self.assertEqual(m["p_neither"], 0.4514)
        self.assertEqual(m["p_green"], 0.5168)
        self.assertEqual(m["median_close"], 364.36)
        self.assertEqual(m["p5_close"], 355.54)
        self.assertEqual(m["p95_close"], 373.54)
        self.assertEqual(m["avg_R"], 0.034)
        self.assertEqual(m["r_at_target"], 1.0)

    def test_bit_for_bit_with_the_reel_sim(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), SIM_BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), SIM_PATHS)
        self.assertEqual(m["hist"], SIM_HIST)
        self.assertEqual(m["bands"][-1], [355.42, 360.91, 364.46, 368.12, 373.65])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (59, 33))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (13, 15, 5, 15))
        self.assertEqual(round(h["avg_R"], 3), -0.062)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": round(t["R"], 2)}
                for t in h["trades"]]
        self.assertEqual(sha(slim), SIM_TRADES)
        first = h["trades"][0]
        self.assertEqual((first["date"], first["entry"], first["stop"], first["target"], first["outcome"]),
                         ("2026-07-28", 307.97, 304.89, 311.05, "target"))

    def test_the_verdict_says_coin_flip(self):
        self.assertEqual(self.doc["verdict"], "Close to a coin flip: target first 29.4 % vs stop first 25.5 %.")
        _gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), even), (0.536, 0.5))
        self.assertEqual(self.doc["verdict_notes"],
                         ["45.1 % of the replays hit neither by 4:00 PM; the price at 4:00 PM decides those."])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [364.18] * 5)
        self.assertEqual(len(m["bands"]), 70)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("10:15", "16:00", 70))
        self.assertTrue(all(p[0] == 364.18 and len(p) == 70 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("TSLA", 364.18, 358.32, 370.04, 8, "10:15 AM"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"]), (5.86, 1.0, "15:55"))
        # the fixture's data runs to 10:49 ET, 6 closed bars past the 10:15 entry: real but
        # unresolved (neither the stop 358.32 nor the target 370.04 was touched by 10:45)
        self.assertEqual(self.doc["warnings"],
                         ["6 bar(s) after the entry are already in the data and were not used: the odds "
                          "are from the entry bar only"])

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("TSLA buy, Monday 2026-10-19: entry 364.18 at 10:15 AM (bar 8), stop 358.32, target 370.04",
                     "10,000 replays", "69 five-minute bars", "seed 19", "59 full sessions",
                     "target first   29.4 %", "stop first     25.5 %", "neither        45.1 %   still open at 4:00 PM",
                     "green          51.7 %", "median 364.36, 90 % of replays between 355.54 and 373.54",
                     "Average R: +0.034", "fired 33 times: 13 target, 15 stop, 5 neither. Average R -0.062",
                     "Close to a coin flip: target first 29.4 % vs stop first 25.5 %.\n45.1 % of the replays "
                     "hit neither by 4:00 PM; the price at 4:00 PM decides those.", "not a prediction", "--seed 19",
                     "LEGACY"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick"):
            self.assertNotIn(word, text)

    def test_a_legacy_run_is_never_recorded(self):
        with self.assertRaises(tm.TMError):
            tm.to_record(self.doc, plan())


class Reel(unittest.TestCase):
    """The reel's plan under the port's replays: whole sessions, next-open fills, costs, flat_by (#2)."""

    @classmethod
    def setUpClass(cls):
        cls.doc = machine(replays=4000)

    def test_the_session_replays(self):
        m = self.doc["mc"]
        self.assertEqual((m["method"], m["sessions"], m["replays"], m["seed"]), ("session", 59, 4000, 19))
        self.assertEqual((m["until"], m["until_from"], m["bars_left"]), ("15:55", "flat_by", 68))   # #2's default
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"] + m["p_rejected"], 1.0, places=6)
        self.assertEqual((m["fill"], m["cost_bps"]), ("next_open", tm.DEFAULT_COST_BPS))
        # a regression pin of this port's first run (measured, not an independent check)
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"]), (0.2572, 0.136, 0.6068))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"]), (364.05, 358.87, 372.09))

    def test_the_session_replays_agree_with_the_exact_count_over_sessions(self):
        # each replay is one of 59 sessions, equally likely: the exact answer is a count over the sessions, so the
        # Monte Carlo must sit within sampling noise of it (an oracle that does not share the sampler)
        sessions, _ = tm.full_sessions(tm.split_sessions(tm.read_bars(READER, DS, "TSLA", NOW_UTC, XNYS())[2]),
                                       "2026-10-19")
        p = plan()
        k, last = p["entry_bar"], 76
        hits = []
        for _, D in sessions:
            price, bars = p["entry"], []
            for i in range(k + 1, last + 1):
                pc = D[i - 1]["c"]
                bars.append({x: price * D[i][x] / pc for x in "ohlc"})
                price = bars[-1]["c"]
            fill = round(bars[0]["o"], 2)
            if not p["stop"] < fill < p["target"]:
                hits.append("rejected")
                continue
            hits.append(tm.walk(bars, p["stop"], p["target"], True, True)[0] or "close")
        exact = hits.count("target") / len(hits)
        m = self.doc["mc"]
        self.assertLess(abs(m["p_target"] - exact), 4 * (exact * (1 - exact) / m["replays"]) ** 0.5)

    def test_the_report_says_how_it_was_built(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("4,000 replays of the rest of the session, 10:15 AM to 3:55 PM, the plan's flat_by",
                     "Each replay is one of TSLA's last 59 full sessions (2026-07-27 to 2026-10-16)",
                     "59 distinct paths", "Fills at the open of the bar after the entry bar",
                     "costs 2 bps a side, each leg on its own price", "not filled", "AMBIGUOUS_BAR_ASSUMPTION",
                     "Blocks of 6 bars instead of whole sessions", "Blocks of 12 bars instead of whole sessions",
                     "Stress, crash gap (the first bar after the fill opens 10 % lower", "Stress, doubled moves",
                     "Stress, five times the costs (costs 10 bps a side instead of 2",
                     "Built from the same 59 sessions as the history: the two are not independent evidence.",
                     "Trade minimum: 100 trades, signed off by the founder 2026-11-20 (the lab's is the same, "
                     "ADR-005)", "the independent quant review is pending (spec 09.4)",
                     "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "Yahoo", "Strongly"):
            self.assertNotIn(word, text)

    def test_the_record(self):
        rec = tm.to_record(self.doc, plan(), READER.manifest(DS).limitations)
        self.assertEqual(rec.schema_version, "timemachine/1")
        self.assertIsNone(rec.strategy_version_id)
        s = rec.plan_replay
        self.assertEqual((s.symbol, s.direction.value, str(s.entry), str(s.stop), str(s.target), s.session),
                         ("TSLA", "long", "364.18", "358.32", "370.04", date(2026, 10, 19)))
        self.assertEqual((s.entry_bar_end, s.replay_until, s.as_of),
                         (datetime(2026, 10, 19, 14, 15, tzinfo=UTC), datetime(2026, 10, 19, 19, 55, tzinfo=UTC),
                          NOW_UTC))
        self.assertEqual((s.fill_model_id, str(s.cost_bps_per_side)), ("next_open_bracket/1", "2"))
        self.assertTrue(s.code_version.startswith("traderos ") and "plan_replay/1 sha256" in s.code_version)
        self.assertEqual(rec.dataset_hash, READER.manifest(DS).canonical_hash)
        kinds = [x.kind for x in rec.scenarios]
        self.assertEqual(kinds, [ScenarioKind.OBSERVED] * 2 + [ScenarioKind.RESAMPLED] * 3 + [ScenarioKind.STRESS] * 3)
        base = rec.scenarios[1].metrics
        self.assertEqual(base["min_trades"].value, 100)
        self.assertIn("minimum trades: 100, signed off by the founder 2026-11-20", rec.scenarios[1].assumptions[2])
        signed = next(lim for lim in rec.limitations if lim.code == "THRESHOLDS_NOT_SIGNED_OFF").detail
        for words in ("The trade minimum (100) is signed off by the founder 2026-11-20",
                      "coin flip, strongly, many neither", "interval", "still proposals",
                      "the independent quant review is pending"):
            self.assertIn(words, signed)
        codes = {lim.code for lim in rec.limitations}
        self.assertTrue({"PIT_LIMITED", "GENERATED_NOT_FORECAST", "SAME_SESSIONS", "THRESHOLDS_NOT_SIGNED_OFF",
                         "PLAN_REPLAY_NOTE"} <= codes)
        self.assertEqual(from_json(TimeMachineReport, to_json(rec)), rec)
        self.assertEqual(tm.to_record(self.doc, plan(), READER.manifest(DS).limitations).report_id, rec.report_id)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(replays=2000, seed=7)["mc"]
        n = machine(replays=2000)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]),
                            (n["p_target"], n["p_stop"], n["median_close"]))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"] + m["p_rejected"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), sha(n["bands"]))

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint, choice and randrange are stable since Python 3.2; the reel numbers above
        # are the real proof, this pins the first draws so a failure points at the RNG
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)
        self.assertEqual([random.Random(19).randrange(59) for _ in range(1)], [43])   # the first session draw

    def test_window_and_replays(self):
        a = machine(replays=500, window=0, **LEGACY)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500, **LEGACY)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def test_the_in_progress_tick_is_dropped(self):
        _, _, bars, dropped, incomplete = tm.read_bars(READER, DS, "TSLA", NOW_UTC, XNYS())
        # at 10:50 the 10:45 bar is in the store, marked incomplete; the live tick is not available yet
        self.assertEqual(dropped, {"off_grid": 0, "not_complete": 1, "outside_hours": 0})
        self.assertEqual(incomplete, {("2026-10-19", 15)})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-19", "10:40", 14))
        # any time later the tick (10:49:45, off the grid) is there too, and both are still dropped
        _, _, later, dropped, _ = tm.read_bars(READER, DS, "TSLA", tm.parse_now("2026-10-19 16:00"), XNYS())
        self.assertEqual(dropped, {"off_grid": 1, "not_complete": 1, "outside_hours": 0})
        self.assertEqual(later[-1]["start"], "10:40")
        entry = [b for b in bars if b["date"] == "2026-10-19" and b["slot"] == 8][0]
        self.assertEqual((entry["start"], entry["c"]), ("10:10", 364.1759948730469))

    def test_now_pins_the_clock(self):
        bars = tm.read_bars(READER, DS, "TSLA", tm.parse_now("2026-10-19 10:12"), XNYS())[2]
        today = [b for b in bars if b["date"] == "2026-10-19"]
        self.assertEqual(len(today), 8)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-19 10:12"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("10:05 bar", str(cm.exception))
        self.assertEqual(code_of(cm), ErrorCode.MISSING_DATA)
        self.assertEqual(machine(now=tm.parse_now("2026-10-19 10:15"), replays=200)["mc"]["pool_sessions"], 59)

    def test_daylight_saving(self):
        stamps = [1793367000,   # 2026-10-30 13:30Z = 9:30 EDT
                  1793629800,   # 2026-11-02 14:30Z = 9:30 EST
                  1793626200,   # 2026-11-02 13:30Z = 8:30 EST, before the open
                  1793649600]   # 2026-11-02 20:00Z = 15:00 EST
        reader, ds = F.rows_reader([(datetime.fromtimestamp(s, UTC), 1.0, 1.0, 1.0, 1.0) for s in stamps])
        _, _, bars, dropped, _ = tm.read_bars(reader, ds, "TSLA", tm.parse_now("2026-11-03T00:00Z"), XNYS())
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-10-30", "09:30", 0), ("2026-11-02", "09:30", 0), ("2026-11-02", "15:00", 66)])
        self.assertEqual(dropped["outside_hours"], 1)
        self.assertEqual(tm.new_york(datetime.fromtimestamp(1793367000, UTC)).hour, 9)
        self.assertEqual(tm.new_york(datetime.fromtimestamp(1793629800, UTC)).hour, 9)

    def test_a_half_day_is_left_out(self):
        def cut(res):  # 2026-09-04 ends at 1:00 PM, like a real early close: 42 bars
            F.keep_bars(res, lambda ts: not (et(ts).date().isoformat() == "2026-09-04" and et(ts).hour >= 13))
        doc = machine(text=F.edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 58)
        self.assertEqual(doc["data"]["short_sessions_left_out"], [{"date": "2026-09-04", "bars": 42}])
        self.assertEqual(doc["history"]["sessions"], 58)
        self.assertNotIn("2026-09-04", [t["date"] for t in doc["history"]["trades"]])
        self.assertIn("Left out 1 short session", "\n".join(tm.report(doc)))

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-16")
        self.assertNotIn("2026-10-19", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_bar_leaves_its_session_out(self):
        # Upstream's "missing price" case: a stored bar cannot lack a price, so the row is not in the dataset at all
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        self.assertNotIn(F.utc(json.loads(F.TEXT)["chart"]["result"][0]["timestamp"][5]),
                         F.yahoo_csv(F.edited(hole)).decode())
        doc = machine(text=F.edited(hole), replays=200)
        self.assertEqual(doc["mc"]["pool_sessions"], 58)   # 2026-07-27 now has a gap

    def test_empty_and_broken_data(self):
        with self.assertRaises(tm.TMError) as cm:            # nothing available yet as of that time
            machine(now=tm.parse_now("2026-07-01 10:00"))
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        self.assertEqual(code_of(cm), ErrorCode.MISSING_DATA)
        from traderos.contracts import DataError
        with self.assertRaises(DataError) as cm:
            tm.time_machine(READER, "DS-nope", plan(), NOW_UTC)
        self.assertEqual(code_of(cm), ErrorCode.MISSING_DATA)
        import dataclasses

        from traderos.contracts import Interval
        from traderos.testing import MemoryBarReader
        manifest, bars = F.dataset()
        daily = MemoryBarReader()
        daily.add(dataclasses.replace(manifest, interval=Interval.D1), bars)
        with self.assertRaises(tm.TMError) as cm:
            tm.time_machine(daily, DS, plan(), NOW_UTC)
        self.assertIn("needs 5-minute bars", str(cm.exception))
        self.assertEqual(code_of(cm), ErrorCode.OUTSIDE_CAPABILITY)

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):
            F.keep_bars(res, lambda ts: et(ts).date().isoformat() >= "2026-09-22")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=F.edited(trim))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))
        self.assertEqual(code_of(cm), ErrorCode.INSUFFICIENT_EVIDENCE)

        def twenty(res):
            F.keep_bars(res, lambda ts: et(ts).date().isoformat() >= "2026-09-21")
        self.assertEqual(machine(text=F.edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_an_early_close_day_ends_the_replay_at_1_pm(self):
        # the fixture's bars moved onto the 30 trading days up to Friday 2026-11-27 (an NYSE 1:00 PM close,
        # from the XNYS calendar, not a table), across the Nov 1 clock change, with real New York times
        sessions = tm.full_sessions(tm.split_sessions(tm.read_bars(READER, DS, "TSLA", NOW_UTC, XNYS())[2]),
                                    "2026-10-19")[0][-30:]
        days = XNYS().sessions(date(2026, 10, 1), date(2026, 11, 27))[-30:]
        rows = []
        for day, (_, bars) in zip(days, sessions, strict=True):
            for b in bars[:42] if day == date(2026, 11, 27) else bars:
                start = eastern_to_utc(day, time(9, 30)) + timedelta(minutes=5 * b["slot"])
                rows.append((start, b["o"], b["h"], b["l"], b["c"]))
        reader, ds = F.rows_reader(rows)
        last = sessions[-1][1]
        p = plan(date="2026-11-27", entry=round(last[8]["c"], 2), stop=round(last[8]["c"], 2) - 5,
                 target=round(last[8]["c"], 2) + 5, stop_rule="pct")
        # right after the entry bar closes: nothing past it is in the data yet
        later = tm.parse_now("2026-11-27 10:16")
        doc = tm.time_machine(reader, ds, p, later, replays=300)
        self.assertEqual((doc["mc"]["until"], doc["mc"]["bars_left"], doc["mc"]["pool_sessions"]), ("13:00", 33, 29))
        self.assertIn("to 1:00 PM", "\n".join(tm.report(doc)))
        self.assertIn("after the 1:00 PM early close", doc["warnings"][0])   # #2: flat_by 15:55 moved to 1:00 PM
        with self.assertRaises(tm.TMError) as cm:
            tm.time_machine(reader, ds, p, later, until="15:55", replays=10)
        self.assertIn("early close", str(cm.exception))

    def test_the_data_must_be_the_plans_ticker(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="AAPL"))
        self.assertIn("AAPL is not in dataset", str(cm.exception))
        self.assertEqual(code_of(cm), ErrorCode.MISSING_DATA)


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-20"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 10:50 has not happened in this data yet
            machine(plan(entry_time="10:55 AM", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="10:17 AM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="4:00 PM", entry_bar=None), "between 9:35 AM and 3:55 PM"),
            (dict(entry_bar=9), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "5-minute bars only"),
            (dict(stop=365.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "ticker"),
            (dict(entry="abc"), "number for entry"),
            (dict(entry=True), "number for entry"),
            (dict(entry="NaN"), "positive price"),
            (dict(entry="Infinity"), "positive price"),
            (dict(date="Oct 19"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)
            self.assertEqual(code_of(cm), ErrorCode.INVALID_INPUT)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "10:15 AM")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 8)
        self.assertEqual(plan(entry_time="10:15", entry_bar=None)["entry_bar"], 8)
        self.assertEqual(plan(entry_time="10:15am", entry_bar=None)["entry_bar"], 8)

    def test_until_cuts_the_replay(self):
        doc = machine(until="15:50", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:50", 67, "15:50"))
        self.assertIn("still open at 3:50 PM", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="3:50 PM", replays=500)["mc"], m)
        for bad, words in (("10:15 AM", "nothing to replay"), ("16:05", "no later than 4:00 PM"),
                           ("15:57", "5-minute grid"), ("noon", "New York time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_the_default_until_is_the_plans_flat_by(self):   # #2
        self.assertEqual(machine(replays=10)["mc"]["until"], "15:55")
        self.assertEqual(machine(plan(flat_by=None), replays=10)["mc"]["until"], "16:00")
        doc = machine(plan(flat_by="15:53"), replays=10)          # off the grid: the bar before it, said
        self.assertEqual(doc["mc"]["until"], "15:50")
        self.assertIn("off the 5-minute grid", doc["warnings"][0])

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=368.0, stop=362.0, target=374.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_refuses_odds_once_the_real_bars_already_resolved_it(self):
        # the fixture's 10:20 bar (high 365.88) already hits this target before the sim starts
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(stop=360.0, target=365.50), replays=10)
        msg = str(cm.exception)
        self.assertIn("target was hit at 10:25 (bar 10) on 2026-10-19", msg)
        self.assertIn("hindsight, not a forecast", msg)
        self.assertEqual(code_of(cm), ErrorCode.LOOKAHEAD_RISK)

    def test_stop_rounds_away_from_the_entry(self):
        self.assertEqual(tm.cents_away(358.325, True), 358.32)
        self.assertEqual(tm.cents_away(358.325, False), 358.33)
        self.assertEqual(tm.cents_away(354.4700012207031, True), 354.47)
        self.assertEqual(tm.cents_away(354.46999999999997, True), 354.47)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=354.47, target=373.89), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=360.0, target=372.54), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 1.1478, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 59)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))
        rec = tm.to_record(doc, plan(setup="flag"))
        self.assertEqual((rec.scenarios[0].kind, rec.scenarios[0].method), (ScenarioKind.OBSERVED, "not run"))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=370.04, target=358.32, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"] + m["p_rejected"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the fill", "\n".join(tm.report(doc)))
        rec = tm.to_record(doc, plan(side="sell", stop=370.04, target=358.32, stop_rule="or_high"))
        self.assertEqual(rec.plan_replay.direction.value, "short")

    def test_the_verdict_thresholds_at_1r(self):
        # 1R: the target's share of the replays that hit one side against 50 %
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        # past STRONG: "Leans" until ADR-010 is signed off, and a note says why
        mc = {"p_target": 0.60, "p_stop": 0.30}
        self.assertTrue(tm.verdict(mc).startswith("Leans toward the target"))
        self.assertIn("waits for ADR-010's sign-off", tm.verdict_notes(mc)[-1])
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Leans toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:55"}),
                         "No replay reached the target or the stop by 3:55 PM: the price at 3:55 PM decides every one.")

    def test_after_sign_off_the_strong_label_is_upstream(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(tm, "SIGNED_OFF", True)
            self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
            self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                             "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
            self.assertEqual(tm.verdict_notes({"p_target": 0.10, "p_stop": 0.60}), [])

    def test_a_2r_target_is_judged_against_its_break_even(self):
        # the live TSLA run of 2026-10-20 (entry 370.58, stop 367.06, 2R target 377.62): it needs 33 %, got 32 %
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_2r_on_the_fixture_near_break_even(self):
        # LEGACY draws, to keep the upstream numbers: stop 362.00, 2R target 368.54: 33.5 % vs 62.4 % looks lopsided,
        # but 2R only needs a third
        doc = machine(plan(stop=362.0, target=368.54, stop_rule="pct"), **LEGACY)
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["avg_R"]), (2.0, 0.3345, 0.6238, 0.068))
        self.assertEqual(doc["verdict"], "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 35 % (target first 33.5 % vs stop "
                                         "first 62.4 %).")
        self.assertEqual(doc["verdict_notes"], [])

    def test_2r_on_the_fixture_a_clear_lean(self):
        # LEGACY draws: the reel's entry and stop with a 2R target 375.90: target first only 4.0 % vs 25.6 %
        doc = machine(plan(target=375.90), **LEGACY)
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 0.0397, 0.2558, 0.7045, 0.042))
        self.assertEqual(doc["verdict"], "Leans toward the stop: at 2R the target has to come first in 33 % of "
                                         "the replays that hit one side; it did in 13 % (target first 4.0 % vs stop "
                                         "first 25.6 %).")
        self.assertEqual(doc["verdict_notes"], [
            "70.5 % of the replays hit neither by 4:00 PM; the price at 4:00 PM decides those.",
            "Average R is +0.042 all the same: the replays that hit neither pull it the other way.",
            "That is past the 15-point line for \"strongly\"; the word waits for ADR-010's sign-off (that "
            "threshold is still proposed, and the independent quant review is pending (spec 09.4))."])
        text = "\n".join(tm.report(doc))
        self.assertIn(doc["verdict"] + "\n" + "\n".join(doc["verdict_notes"]), text)


def bar(o, h=None, l=None, c=None):  # noqa: E741
    return {"o": o, "h": o if h is None else h, "l": o if l is None else l, "c": o if c is None else c}


def session(date, overrides=None):
    """A made-up session: a 99 to 101 opening range, bar 3 closes at 101.50 (the signal),
    then flat at 101.60. Stop at the midpoint 100.00, 1R target 103.00, risk 1.50."""
    D = [bar(100, 101, 99, 100) for _ in range(3)] + [bar(100.5, 101.6, 100.4, 101.5)] + \
        [bar(101.6) for _ in range(tm.SESSION_BARS - 4)]
    for i, b in (overrides or {}).items():
        D[i] = b
    return (date, D)


def quiet(date):
    """A session that never breaks its 99 to 101 range: flat at 100."""
    return (date, [bar(100, 101, 99, 100) for _ in range(3)] + [bar(100) for _ in range(tm.SESSION_BARS - 3)])


RULE = {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0}
LAST = tm.SESSION_BARS - 1


def one(overrides=None, fill="next_open", cost_bps=0, last=LAST):
    h = tm.history_replay([session("2026-01-02", overrides)], RULE, last, "buy", fill, cost_bps)
    return h, (h["trades"][0] if h["trades"] else None)


class Fills(unittest.TestCase):
    def test_the_fill_is_the_next_open_not_the_signal_close(self):
        h, t = one()
        self.assertEqual((t["signal"], t["entry"], t["stop"], t["target"], t["risk"]),
                         (101.5, 101.6, 100.0, 103.0, 1.5))
        self.assertEqual((t["outcome"], t["exit"], t["R"]), ("close", 101.6, 0.0))
        self.assertEqual((h["fill"], h["cost_bps"], h["skipped"], h["signals"]),
                         ("next_open", 0, {"no_bar": 0, "rejected": 0}, 1))

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        _, t = one({5: bar(99.5)})
        self.assertEqual((t["outcome"], t["exit"], round(t["R"], 4)), ("stop", 99.5, -1.4))   # worse than -1R
        _, t = one({5: bar(99.5)}, fill="close")
        self.assertEqual((t["outcome"], t["exit"], t["R"]), ("stop", 100.0, -1.0))  # the old accounting

    def test_a_gap_past_the_target_fills_at_the_open(self):
        _, t = one({5: bar(104)})
        self.assertEqual((t["outcome"], t["exit"], round(t["R"], 4)), ("target", 104.0, 1.6))

    def test_a_stop_inside_a_bar_fills_at_the_stop(self):
        _, t = one({5: bar(101.0, 101.1, 99.9, 100.2)})
        self.assertEqual((t["outcome"], t["exit"], round(t["R"], 2)), ("stop", 100.0, -1.07))  # 1.60 on a 1.50 risk
        _, t = one({5: bar(101.0, 101.1, 99.9, 100.2)}, fill="close")
        self.assertEqual(t["R"], -1.0)

    def test_a_next_open_past_the_target_is_a_rejected_bracket(self):
        h, t = one({4: bar(103.2)})
        self.assertIsNone(t)
        self.assertEqual((h["triggered"], h["skipped"], h["signals"]), (0, {"no_bar": 0, "rejected": 1}, 1))
        _, t = one({4: bar(103.2)}, fill="close")
        self.assertEqual((t["outcome"], t["R"]), ("target", 1.0))

    def test_a_signal_on_the_last_bar_has_nothing_to_fill_on(self):
        h, t = one(last=3)
        self.assertIsNone(t)
        self.assertEqual(h["skipped"], {"no_bar": 1, "rejected": 0})

    def test_costs_come_off_every_r(self):
        # each leg on its own price (ADR-010): 0.10 % x 101.60 + 0.10 % x 99.50 = 0.2011 a share = 0.1341R
        _, t = one({5: bar(99.5)}, cost_bps=10)
        self.assertAlmostEqual(t["cost_R"], 0.13406667, places=7)
        self.assertAlmostEqual(t["R"], -1.53406667, places=7)
        self.assertAlmostEqual(tm.cost_r(100.0, 100.0, 1.0, 2.0), 0.04)


class Baseline(unittest.TestCase):
    def test_a_blind_entry_on_the_other_sessions(self):
        sessions = [session("2026-01-02", {5: bar(104)})] + [quiet("2026-01-0%d" % d) for d in (5, 6, 7)]
        h = tm.history_replay(sessions, RULE, LAST, "buy", "next_open", 0)
        self.assertEqual((h["triggered"], round(h["trades"][0]["R"], 4)), (1, 1.6))
        b = tm.baseline(sessions, h, LAST, "buy", "next_open", 0, seed=19, draws=500)
        # the quiet days: entry 100 at bar 4's open, stop 98.52 (the same 1.48 % width), nothing moves: 0R each
        self.assertEqual((b["trades"], b["avg_R"], round(b["setup_avg_R"], 4), round(b["edge_R"], 4)),
                         (3, 0.0, 1.6, 1.6))
        self.assertEqual([round(x, 4) for x in b["setup_ci"] + b["edge_ci"]], [1.6, 1.6, 1.6, 1.6])
        self.assertTrue(tm.baseline_verdict(b).startswith("Too few trades to tell: 1 trades, and at least 100"))
        self.assertEqual(b["min_trades"], 100)
        self.assertNotIn("min_trades_lab", b)

    def test_the_verdict_needs_enough_trades(self):
        b = {"setup_avg_R": 0.2, "setup_trades": 150, "min_trades": 100, "edge_R": 0.15, "edge_ci": [0.05, 0.25],
             "draws": 10000}
        self.assertEqual(tm.baseline_verdict(b), "The setup beat a blind entry: +0.150 R a trade, 95 % range +0.050 "
                                                 "to +0.250 (stationary bootstrap, 10,000 draws), all above zero. Not "
                                                 "a qualification: the interval and the verdict's other "
                                                 "thresholds are proposals (ADR-010), and the independent quant "
                                                 "review is pending (spec 09.4).")
        self.assertTrue(tm.baseline_verdict(dict(b, edge_R=-0.2, edge_ci=[-0.3, -0.1]))
                        .startswith("The setup did worse than a blind entry"))
        self.assertTrue(tm.baseline_verdict(dict(b, edge_ci=[-0.05, 0.3])).startswith("No clear difference"))
        self.assertTrue(tm.baseline_verdict(dict(b, setup_trades=33)).startswith("Too few trades to tell: 33 trades"))
        self.assertTrue(tm.baseline_verdict(dict(b, setup_trades=99)).startswith("Too few trades to tell: 99 trades"))
        self.assertTrue(tm.baseline_verdict(dict(b, setup_trades=100)).startswith("The setup beat"))
        self.assertIsNone(tm.baseline_verdict(None))

    def test_the_fixture_by_default(self):
        doc = machine(replays=300)
        h, b = doc["history"], doc["baseline"]
        self.assertEqual((h["fill"], h["cost_bps"]), ("next_open", 2.0))
        # the same 33 signals as the old accounting: each one is either a trade or left out with a reason
        self.assertEqual(h["triggered"] + h["skipped"]["rejected"] + h["skipped"]["no_bar"], 33)
        self.assertEqual(h["signals"], 33)
        for t in h["trades"]:
            self.assertTrue(t["stop"] < t["entry"] < t["target"], t)
            self.assertGreater(t["cost_R"], 0)
        self.assertEqual(b["setup_trades"], h["triggered"])
        self.assertGreater(b["trades"], 10 * h["triggered"])
        self.assertLessEqual(b["setup_ci"][0], b["setup_avg_R"])
        self.assertLessEqual(b["setup_avg_R"], b["setup_ci"][1])
        self.assertLessEqual(b["edge_ci"][0], b["edge_R"])
        self.assertLessEqual(b["edge_R"], b["edge_ci"][1])
        self.assertAlmostEqual(b["edge_R"], b["setup_avg_R"] - b["control_R"], places=9)
        self.assertTrue(b["verdict"].startswith("Too few trades to tell: %d trades" % h["triggered"]))
        text = "\n".join(tm.report(doc))
        for line in ("Fills: at the open of the bar after the signal close", "costs 2 bps a side",
                     "Baseline: a blind entry at the same bars", "Setup minus its own blind entries:", b["verdict"]):
            self.assertIn(line, text)
        self.assertEqual(machine(replays=300)["baseline"], b)   # same seed, same numbers

    def test_the_reel_with_real_fills(self):
        doc = machine(replays=100, until="16:00")
        h, b = doc["history"], doc["baseline"]
        self.assertEqual((h["triggered"], h["target"], h["stop"], h["close"], round(h["avg_R"], 3)),
                         (33, 13, 15, 5, -0.113))
        # #53 had -0.041: with each cost leg on its own price the blind entries' average moves to -0.042 (ADR-010)
        self.assertEqual((b["trades"], round(b["avg_R"], 3), round(b["edge_R"], 3)), (1914, -0.042, -0.071))
        old = machine(replays=100, **LEGACY)["baseline"]
        self.assertEqual((round(old["avg_R"], 3), round(old["edge_R"], 3)), (0.005, -0.068))

    def test_costs_only_make_the_old_accounting_worse(self):
        h = machine(replays=100, fill="close", cost_bps=2, until="16:00")["history"]
        self.assertEqual(h["triggered"], 33)
        self.assertLess(h["avg_R"], -0.062)

    def test_bad_fill_and_costs_are_refused(self):
        with self.assertRaises(tm.TMError):
            machine(replays=10, fill="vwap")
        with self.assertRaises(tm.TMError):
            machine(replays=10, cost_bps=-1)
        self.assertEqual(machine(replays=10, fill="next-open")["history"]["fill"], "next_open")


# ---------------------------------------------------------------- the CLI (`traderos timemachine plan`)

@pytest.fixture(scope="module")
def home(tmp_path_factory):
    d = tmp_path_factory.mktemp("tmplan")
    (d / "tsla.csv").write_bytes(F.yahoo_csv())
    (d / "plan.json").write_text(json.dumps(PLAN), encoding="utf-8")
    h = str(d / "home")
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["--home", h, "init"]) == 0
        assert cli.main(["--json", "--home", h, "data", "import", str(d / "tsla.csv"), "--name", "tsla5",
                         "--interval", "5m", "--rights-label", "test-only"]) == 0
    ds = json.loads(out.getvalue().splitlines()[-1])["result"]["dataset_id"]
    return {"dir": d, "home": h, "ds": ds}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_run_with_the_plan_end_to_end(home):
    code, text, err = run(["--home", home["home"], "timemachine", "plan", "--plan", str(home["dir"] / "plan.json"),
                           "--dataset", home["ds"], "--as-of", NOW, "--replays", "500"])
    assert code == 0, err
    for line in ("target first", "Leans toward the target", "Recorded TM-", "nothing here is an order",
                 "Trade minimum: 100 trades, signed off by the founder"):
        assert line in text
    rid = text.strip().splitlines()[-1].split()[1]
    rec = Store(home["home"]).get(TimeMachineReport, rid)
    assert rec.plan_replay.symbol == "TSLA" and rec.dataset_id == home["ds"]


def test_cli_json_output_is_the_record(home):
    code, text, _ = run(["--json", "--home", home["home"], "timemachine", "plan", "--plan",
                         str(home["dir"] / "plan.json"), "--dataset", home["ds"], "--as-of", NOW, "--replays", "300"])
    assert code == 0
    doc = json.loads(text)
    assert doc["ok"] is True and doc["result"]["schema_version"] == "timemachine/1"
    assert doc["result"]["plan_replay"]["fill_model_id"] == "next_open_bracket/1"
    assert "sample_paths" not in text


def test_cli_the_plan_as_flags_matches_the_file(home):
    argv = ["--json", "--home", home["home"], "timemachine", "plan", "--dataset", home["ds"], "--as-of", NOW,
            "--replays", "300"]
    _, from_file, _ = run([*argv, "--plan", str(home["dir"] / "plan.json")])
    code, from_flags, err = run([*argv, "--symbol", "tsla", "--date", "2026-10-19", "--entry", "364.18", "--stop",
                                 "358.32", "--target", "370.04", "--entry-time", "10:15 AM",
                                 "--stop-rule", "or_mid", "--until", "15:55"])
    assert code == 0, err
    a, b = json.loads(from_file)["result"], json.loads(from_flags)["result"]
    assert a["plan_replay"]["entry"] == b["plan_replay"]["entry"] == "364.18"
    assert a["scenarios"][0] == b["scenarios"][0]                        # the same history
    code, _, err = run(["--home", home["home"], "timemachine", "plan", "--symbol", "TSLA", "--entry", "364.18",
                        "--dataset", home["ds"]])
    assert code == 3 and "--stop" in err


def test_cli_errors_are_one_plain_line(home, monkeypatch):
    base = ["--home", home["home"], "timemachine"]
    for argv, want in (([*base, "plan", "--plan", str(home["dir"] / "nope.json"), "--dataset", home["ds"]], 3),
                       ([*base, "plan", "--plan", str(home["dir"] / "plan.json"), "--dataset", home["ds"],
                         "--as-of", NOW, "--until", "9:00"], 3),
                       ([*base, "plan", "--plan", str(home["dir"] / "plan.json"), "--dataset", "DS-nope"], 3),
                       ([*base, "plan", "--dataset", home["ds"]], 3),
                       ([*base, "plan", "--plan", str(home["dir"] / "plan.json"), "--dataset", home["ds"],
                         "--paths", "5"], 3),
                       ([*base, "SV-x", "--dataset", home["ds"], "--replays", "5"], 3),
                       ([*base, "plan", "--plan", str(home["dir"] / "plan.json"), "--dataset", home["ds"],
                         "--as-of", NOW, "--replays", "0"], 3)):
        code, _, err = run(argv)
        assert code == want, (argv, err)
        assert len(err.strip().splitlines()) == 1 and err.startswith("refused [") and "Traceback" not in err

    def boom(*a, **k):
        raise ZeroDivisionError("float division by zero")
    monkeypatch.setattr(tm, "time_machine", boom)        # #2's catch-all: an unexpected error is one line, exit 4
    code, _, err = run([*base, "plan", "--plan", str(home["dir"] / "plan.json"), "--dataset", home["ds"]])
    assert code == 4 and len(err.strip().splitlines()) == 1 and "Traceback" not in err


def test_cli_chart(home):
    code, text, _ = run(["--home", home["home"], "timemachine", "plan", "--plan", str(home["dir"] / "plan.json"),
                         "--dataset", home["ds"], "--as-of", NOW, "--replays", "300", "--chart"])
    assert code == 0
    lines = text.splitlines()
    assert any("2,000 replay paths from the 10:15 AM entry (seed 19)" in line for line in lines)
    assert any(line.startswith("   370.04 |") and line.endswith("  target") for line in lines)
    assert any(line.startswith("   358.32 |") and line.endswith("  stop") for line in lines)
    assert any(line.startswith("   364.18 |") and line.endswith("  entry") for line in lines)
    assert "10:15" in text and "3:55" in text


def test_a_plan_file_is_bounded(tmp_path):
    big = tmp_path / "big.json"
    big.write_text('{"symbol": "TSLA", "pad": "' + "x" * tm.MAX_PLAN_BYTES + '"}')
    with pytest.raises(tm.TMError) as e:
        tm.read_plan(big)
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED
    (tmp_path / "list.json").write_text("[1, 2]")
    with pytest.raises(tm.TMError, match="is not a plan"):
        tm.read_plan(tmp_path / "list.json")


# ---------------------------------------------------------------- the review's cases and the named flaws

def two_sessions_pool():
    """A pool of two past sessions: one rising 1 % a bar, one falling 1 % a bar (the review's #53 case)."""
    up = [{"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0}]
    down = [dict(up[0])]
    for _ in range(tm.SESSION_BARS - 1):
        u, d = up[-1]["c"] * 1.01, down[-1]["c"] * 0.99
        up.append({"o": up[-1]["c"], "h": u, "l": up[-1]["c"], "c": u})
        down.append({"o": down[-1]["c"], "h": down[-1]["c"], "l": d, "c": d})
    return tm.build_pool([("2026-01-02", up), ("2026-01-05", down)])


def test_session_replays_never_mix_sessions_and_independent_draws_do():
    # #53 review: three bars replayed from 100.00; every session path ends at 103.0301 or 97.0299, never a mix
    pool = two_sessions_pool()
    k, last = 74, 77
    mc = tm.simulate(pool, 100.0, 50.0, 150.0, k, last, replays=400, seed=3, cost_bps=0)
    ends = {round(p[-1], 4) for p in mc["sample_paths"]}
    assert ends <= {103.03, 97.03} and len(ends) == 2
    assert all(abs(p[-1] - 103.0301) < 0.006 or abs(p[-1] - 97.0299) < 0.006 for p in mc["sample_paths"])
    assert mc["sessions"] == 2
    # the flaw S11 names: the upstream independent draw builds paths no session produced
    legacy = tm.simulate_legacy(pool, 100.0, 50.0, 150.0, k, last, replays=400, seed=3, window=0)
    mixes = [p for p in legacy["sample_paths"] if round(p[-1], 2) not in (103.03, 97.03)]
    assert len(mixes) > 50
    # the block sensitivity mixes on purpose, at its stated rate
    block = tm.simulate(pool, 100.0, 50.0, 150.0, k, last, replays=400, seed=3, method="block", block=1,
                        cost_bps=0)
    assert any(round(p[-1], 2) not in (103.03, 97.03) for p in block["sample_paths"])
    with pytest.raises(tm.TMError):
        tm.simulate(pool, 100.0, 50.0, 150.0, k, last, method="block", block=0)
    with pytest.raises(tm.TMError):
        tm.simulate(pool, 100.0, 50.0, 150.0, k, last, method="independent")


def one_path_pool(opens):
    """A one-session pool whose bars from slot k+1 open at the given prices (o=h=l=c), the bar before closing at
    100: each replay is that one path."""
    D = [{"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0} for _ in range(tm.SESSION_BARS)]
    for j, o in enumerate(opens):
        D[71 + j] = {"o": o, "h": o, "l": o, "c": o}
    return tm.build_pool([("2026-01-02", D)])


def test_a_gap_through_the_stop_exits_at_the_open_in_the_replays():
    # #2 review: filled at 100.00, stop 95.00 (risk 5.00), the next bar opens at 90.00: exit 90.00, R -2.00
    mc = tm.simulate(one_path_pool([100.0, 90.0]), 100.0, 95.0, 110.0, 70, 77, replays=20, cost_bps=0)
    assert (mc["p_stop"], mc["avg_R"], mc["p_rejected"]) == (1.0, -2.0, 0.0)
    legacy = tm.simulate_legacy(one_path_pool([100.0, 90.0]), 100.0, 95.0, 110.0, 70, 77, replays=20, window=0)
    assert legacy["avg_R"] == -1.0                                   # the flaw: the reel booked -1R


def test_a_next_open_through_the_stop_is_not_filled_in_the_replays():
    # #2 review: signal close 100.00, stop 95.00, next open 94.00: no trade. Never +0.20 (#2's next-open mode)
    mc = tm.simulate(one_path_pool([94.0]), 100.0, 95.0, 110.0, 70, 77, replays=20, cost_bps=0)
    assert (mc["p_rejected"], mc["avg_R"], mc["p_stop"], mc["median_fill"]) == (1.0, None, 0.0, None)


def test_the_review_fills_and_costs_case_by_case():
    # #53 review fixture: range 99 to 101, signal close 101.50, stop 100.00 (risk 1.50), 1R target 103.00, next
    # open 101.60. TraderOS takes costs in bps a side, so the per-share cases are checked gross, the bps one net.
    _, t = one()
    assert (t["entry"], t["R"]) == (101.6, 0.0)                                 # not 101.50
    per_share = 0.02 / 101.6 * 1e4                                              # 0.02 a share at the fill
    _, t = one(cost_bps=per_share)
    assert abs(t["R"] - (-0.04 / 1.5)) < 1e-9                                    # -0.0267R
    _, t = one({5: bar(99.5)})
    assert (t["exit"], round(t["R"], 4)) == (99.5, -1.4)                        # gross -1.40R
    h, t = one({4: bar(103.2)})
    assert t is None and h["skipped"]["rejected"] == 1                          # never +1.00R
    _, t = one({5: bar(101.0, 103.5, 99.9, 101.0)})
    assert (t["outcome"], t["exit"], t["ambiguous"], round(t["R"], 4)) == ("stop", 100.0, True, -1.0667)
    _, t = one({5: bar(99.5)}, cost_bps=10)
    legs = (0.001 * 101.6, 0.001 * 99.5)
    assert [round(x, 4) for x in legs] == [0.1016, 0.0995]
    assert (round(t["cost_R"], 4), round(t["R"], 4)) == (0.1341, -1.5341)       # no double subtraction


def test_settled_trades_and_the_tie_rule():
    # #25 review: a later bar touching both 360.00 and 365.50 is a stop (stop first); run at 10:16, nothing after
    day = {8: {"o": 364.0, "h": 364.5, "l": 363.0, "c": 364.18}, 9: {"o": 364.2, "h": 366.0, "l": 359.9, "c": 362.0}}
    assert tm.real_outcome(day, 8, 77, 364.18, 360.0, 365.5, True)[:2] == ("stop", 9)
    doc = machine(plan(stop=360.0, target=365.50), now=tm.parse_now("2026-10-19 10:16"), replays=50)
    assert doc["warnings"] == []


def test_a_bar_not_yet_available_never_changes_the_answer():
    # QT01: the same plan as of 10:16, with and without every bar after it in the dataset
    def cut(res):
        F.keep_bars(res, lambda ts: et(ts) < datetime(2026, 10, 19, 10, 15))
    at = tm.parse_now("2026-10-19 10:16")
    a, b = machine(now=at, replays=300), machine(text=F.edited(cut), now=at, replays=300)
    for key in ("mc", "history", "baseline", "sensitivity", "stress", "verdict"):
        assert a[key] == b[key], key


def test_an_incomplete_entry_bar_is_its_own_refusal():
    with pytest.raises(tm.TMError) as e:
        machine(plan(entry_time="10:50 AM", entry_bar=None, entry=366.0, stop=360.0, target=372.0),
                now=tm.parse_now("2026-10-19 11:30"))
    assert e.value.code is ErrorCode.INCOMPLETE_BAR and "marked incomplete" in e.value.message


def test_the_trade_minimum_is_the_labs_and_signed_off():
    # OWNER post (OSDev1 on the wall, 2026-11-20 15:59 UTC): both minimums are 100, the founder's sign-off
    from traderos.research import MIN_TRADES as LAB
    assert tm.MIN_TRADES == LAB == 100
    assert tm.MIN_TRADES_SIGNED_OFF == "signed off by the founder 2026-11-20"
    assert tm.SIGNED_OFF is False                     # every other threshold is still proposed


def test_one_cost_default_everywhere_and_it_is_the_labs():
    for fn in (tm.history_replay, tm.baseline, tm.time_machine, tm.simulate, tm.replay):
        assert inspect.signature(fn).parameters["cost_bps"].default == tm.DEFAULT_COST_BPS, fn.__name__
    lab_per_side = float(DEFAULT_COSTS["spread_bps"]) / 2 + float(DEFAULT_COSTS["slippage_bps"])
    assert tm.DEFAULT_COST_BPS == lab_per_side == 2.0


def test_r_is_averaged_unrounded():
    h = machine(replays=50)["history"]
    exact = sum(t["R"] for t in h["trades"]) / len(h["trades"])
    assert h["avg_R"] == pytest.approx(exact, abs=1e-12)
    assert any(t["R"] != round(t["R"], 2) for t in h["trades"])


def test_the_intervals_are_the_stationary_bootstrap():
    doc = machine(replays=50)
    h, b = doc["history"], doc["baseline"]
    lo, hi, block = stationary_bootstrap_mean([t["R"] for t in h["trades"]], seed=19, resamples=tm.BASELINE_DRAWS)
    assert (b["setup_ci"], b["block_length"], b["method"]) == ([lo, hi], block, "stationary_bootstrap/1")
    assert "blind_as_good" not in b


def test_ambiguous_bars_are_counted_and_labelled():
    # a pool whose only bar after the fill touches both the stop and the target: the stop, labelled
    D = [{"o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0} for _ in range(tm.SESSION_BARS)]
    D[72] = {"o": 100.0, "h": 112.0, "l": 94.0, "c": 100.0}
    mc = tm.simulate(tm.build_pool([("2026-01-02", D)]), 100.0, 95.0, 110.0, 70, 77, replays=10, cost_bps=0)
    assert (mc["ambiguous"], mc["p_ambiguous"], mc["p_stop"], mc["avg_R"]) == (10, 1.0, 1.0, -1.0)


def test_the_stresses_do_what_they_say():
    pool = one_path_pool([100.0] * 7)
    crash = tm.simulate(pool, 100.0, 95.0, 110.0, 70, 77, replays=10, cost_bps=0, shock=tm.crash_shock(True))
    assert (crash["p_stop"], round(crash["avg_R"], 6)) == (1.0, -2.0)        # opens 90.00 after a 100.00 fill
    short = tm.simulate(pool, 100.0, 105.0, 90.0, 70, 77, replays=10, side="sell", cost_bps=0,
                        shock=tm.crash_shock(False))
    assert (short["p_stop"], round(short["avg_R"], 6)) == (1.0, -2.0)        # against a short: 10 % higher
    r = (1.002, 1.01, 0.995, 1.004)
    doubled = tm.amplify_shock()(0, r)
    assert [round(x, 9) for x in doubled] == [round(x * x, 9) for x in r]
    assert doubled[1] >= max(doubled[0], doubled[3]) and doubled[2] <= min(doubled[0], doubled[3])
    doc = machine(replays=300)
    base, five = doc["mc"]["avg_R"], next(s for s in doc["stress"] if s["name"] == "five times the costs")["mc"]
    plain = machine(replays=300, cost_bps=0)["mc"]["avg_R"]
    assert five["p_target"] == doc["mc"]["p_target"]                           # costs never change an outcome
    assert five["avg_R"] - plain == pytest.approx(5 * (base - plain), rel=1e-9)


def test_no_network_and_no_order_words_in_the_port():
    import pathlib
    for mod in (tm,):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "urllib" not in src and "http" not in src.lower().replace("https://", "")
        assert not hasattr(mod, "fetch_chart")


def test_the_record_refuses_two_subjects_and_none():
    from traderos.contracts.examples import variants
    rec = variants()["timemachine_v1_plan_replay"]
    import dataclasses
    with pytest.raises(ValidationError):
        dataclasses.replace(rec, strategy_version_id="SV-1", strategy_semantic_hash="2" * 64)
    with pytest.raises(ValidationError):
        dataclasses.replace(rec, plan_replay=None)
    with pytest.raises(ValidationError):
        dataclasses.replace(rec.plan_replay, stop=rec.plan_replay.target)


def test_the_edge_is_each_trade_against_its_own_blind_entries():
    # the edge series is R minus the average of that trade's own blind entries (the upstream backtest.py definition),
    # derived here by hand from its baseline loop; the interval is the stationary bootstrap of that series
    doc = machine(replays=50)
    h, b = doc["history"], doc["baseline"]
    sessions, _ = tm.full_sessions(tm.split_sessions(tm.read_bars(READER, DS, "TSLA", NOW_UTC, XNYS())[2]),
                                   "2026-10-19")
    edges = []
    for t in h["trades"]:
        mine = []
        for d, D in sessions:
            if d == t["date"]:
                continue
            p = round(D[t["bar"]]["c"], 2)
            s = tm.cents_away(p * (1 - t["risk"] / t["signal"]), True)
            ct = tm.one_trade(D, t["bar"], p, s, round(p + (p - s), 2), 76, True, "next_open", 2.0)
            if not isinstance(ct, str):
                mine.append(ct["R"])
        edges.append(t["R"] - statistics.mean(mine))      # as the product averages them: exact on every Python
    lo, hi, _ = stationary_bootstrap_mean(edges, seed=20, resamples=tm.BASELINE_DRAWS)
    assert b["edge_ci"] == [lo, hi]
    assert b["edge_R"] == pytest.approx(sum(edges) / len(edges), abs=1e-12)


def test_green_is_measured_from_the_fill():
    # #2: filled at 101.00 on a 100.00 signal; the day ends at 100.50, above the entry but below the fill: not green
    mc = tm.simulate(one_path_pool([101.0] + [100.5] * 6), 100.0, 95.0, 110.0, 70, 77, replays=10, cost_bps=0)
    assert (mc["p_neither"], mc["p_green"], mc["median_fill"]) == (1.0, 0.0, 101.0)
    assert mc["avg_R"] == pytest.approx(-0.1)


# ---------------------------------------------------------------- OSDev6's review of #9

def test_an_orb_plan_cannot_enter_inside_its_opening_range():
    # HIGH 1 (QT01): entry 10:15 with a 60-minute range made `auto` read range bars closing after the entry, so the
    # stop rule and the history changed with the as-of time. The plan is refused, the same at both times.
    for at in ("2026-10-19 10:16", "2026-10-19 10:50"):
        with pytest.raises(tm.TMError) as e:
            p = plan(opening_range={"minutes": 60}, stop=360.60, stop_rule="auto")
            machine(p, now=tm.parse_now(at), replays=50)
        assert e.value.code is ErrorCode.LOOKAHEAD_RISK and "before the 60-minute opening range ends" in str(e.value)
    with pytest.raises(tm.TMError):
        plan(entry_time="9:45 AM", entry_bar=None)              # the range's own last bar
    assert plan(entry_time="9:50 AM", entry_bar=None)["entry_bar"] == 3   # the first bar after it
    assert plan(setup="flag", opening_range={"minutes": 60})["setup"] == "flag"   # no range rule to read
    # and resolve_rule reads only bars closed by the entry, whatever reaches it
    _, _, bars, _, _ = tm.read_bars(READER, DS, "TSLA", NOW_UTC, XNYS())
    day = tm.split_sessions(bars)["2026-10-19"]
    early = dict(plan(), opening_range={"minutes": 60}, stop=360.60, stop_rule="auto")
    assert tm.resolve_rule(early, day)["stop_rule"] == "pct"


def test_a_plan_gives_the_same_rule_and_history_at_any_later_as_of_time():
    p = plan(stop_rule=None)
    a = machine(p, now=tm.parse_now("2026-10-19 10:16"), replays=100)
    b = machine(p, now=tm.parse_now("2026-10-19 10:50"), replays=100)
    assert a["history"] == b["history"] and a["baseline"] == b["baseline"] and a["mc"] == b["mc"]
    assert a["plan"]["stop_rule"] == "or_mid"


def test_a_bars_open_decides_before_any_touch():
    # MEDIUM 3: opened past the target, then traded through the stop: the open decides, not an ambiguous stop
    assert tm.walk([{"o": 111, "h": 112, "l": 94, "c": 100}], 95, 110, True, True) == ("target", 111, False)
    assert tm.walk([{"o": 89, "h": 106, "l": 88, "c": 100}], 105, 90, False, True) == ("target", 89, False)
    assert tm.walk([{"o": 94, "h": 112, "l": 93, "c": 100}], 95, 110, True, True) == ("stop", 94, False)
    assert tm.walk([{"o": 111, "h": 112, "l": 94, "c": 100}], 95, 110, True, False) == ("stop", 95, True)


def test_touches_are_inclusive_and_the_tie_is_flagged_at_the_edge():
    # OSDev6's survivors: a low exactly at the stop is the stop; a high exactly at the target in that bar is a tie
    assert tm.walk([{"o": 100, "h": 101, "l": 95, "c": 100}], 95, 110, True, True) == ("stop", 95, False)
    assert tm.walk([{"o": 100, "h": 105, "l": 99, "c": 100}], 105, 90, False, True) == ("stop", 105, False)
    assert tm.walk([{"o": 100, "h": 110, "l": 95, "c": 100}], 95, 110, True, True) == ("stop", 95, True)
    assert tm.walk([{"o": 100, "h": 105, "l": 90, "c": 100}], 105, 90, False, True) == ("stop", 105, True)
    assert tm.walk([{"o": 100, "h": 110, "l": 96, "c": 100}], 95, 110, True, True) == ("target", 110, False)


def test_a_next_open_exactly_at_the_stop_or_the_target_is_a_rejected_bracket():
    for opens in (100.0, 103.0):                       # stop 100.00, target 103.00
        h, t = one({4: bar(opens)})
        assert t is None and h["skipped"]["rejected"] == 1, opens


def test_the_real_bars_settle_on_the_last_bar_too():
    # #25 survivor: the last replay bar counts
    day = {9: {"o": 364.2, "h": 364.9, "l": 363.5, "c": 364.0}, 10: {"o": 364.0, "h": 365.6, "l": 363.9, "c": 365.0}}
    assert tm.real_outcome(day, 8, 10, 364.18, 360.0, 365.5, True)[:2] == ("target", 10)
    assert tm.real_outcome(day, 8, 9, 364.18, 360.0, 365.5, True)[:2] == (None, None)


def test_a_close_exactly_at_the_range_high_is_not_a_breakout():
    # OSDev6's survivor: the signal is the first close ABOVE the range (the upstream rule): equal is not above
    _h, t = one({3: bar(100.5, 101.0, 100.4, 101.0)})
    assert t["bar"] == 4 and t["signal"] == 101.6
    (_, D) = session("2026-01-02")
    D = [dict(b) for b in D]
    D[3] = bar(99.5, 99.6, 99.0, 99.0)                 # a short's mirror: a close at the range low is not a signal
    for i in range(4, tm.SESSION_BARS):
        D[i] = bar(98.5)
    hs = tm.history_replay([("2026-01-02", D)], RULE, LAST, "sell", "next_open", 0)
    assert hs["trades"][0]["bar"] == 4


def test_the_crash_stress_says_when_it_cannot_apply():
    # MEDIUM 4: the gap lands on the first bar after the fill; a 3:55 PM entry has none before the close
    _, _, bars, _, _ = tm.read_bars(READER, DS, "TSLA", NOW_UTC, XNYS())
    day = tm.split_sessions(bars)["2026-10-16"]
    for close, applied in (("3:55 PM", False), ("3:50 PM", True)):
        k = (tm.parse_clock(close, "t") - tm.OPEN_MIN) // 5 - 1
        e = round(day[k]["c"], 2)
        p = plan(date="2026-10-16", entry=e, stop=e - 5, target=e + 5, stop_rule="pct", entry_time=close,
                 entry_bar=None, flat_by=None)
        doc = machine(p, now=tm.parse_now("2026-10-16 " + tm.clock24(tm.parse_clock(close, "t") + 1)), replays=50)
        crash = next(s for s in doc["stress"] if s["name"] == "crash gap")
        assert (crash["mc"] is not None) is applied, close
        rec = tm.to_record(doc, p)
        scenario = next(s for s in rec.scenarios if s.name == "crash gap")
        text = "\n".join(tm.report(doc))
        if applied:
            assert scenario.paths == 50 and "Stress, crash gap (" in text
        else:
            assert (scenario.method, scenario.paths) == ("not applied", 0)
            assert "Stress, crash gap: not applied (no bar after the fill bar before 4:00 PM" in text
            assert crash["mc"] is None and "nowhere to happen" in scenario.assumptions[-1]


def test_a_plan_file_names_each_key_once(tmp_path):
    # LOW 6: duplicate keys are refused; floats stay accepted (the Exit Planner's plan shape uses them)
    f = tmp_path / "dup.json"
    f.write_text('{"symbol": "TSLA", "entry": 364.18, "entry": 1.0}')
    with pytest.raises(tm.TMError, match="names 'entry' twice"):
        tm.read_plan(f)
    f.write_text(json.dumps(PLAN))
    assert tm.read_plan(f)["entry"] == 364.18


def test_cli_a_plan_file_beside_plan_flags_is_refused(home):
    # LOW 7: --plan FILE ignored the plan's flags silently
    code, _, err = run(["--home", home["home"], "timemachine", "plan", "--plan", str(home["dir"] / "plan.json"),
                        "--dataset", home["ds"], "--entry", "1.00", "--stop-rule", "pct"])
    assert code == 3 and "--entry, --stop-rule" in err and "not both" in err


def test_a_value_the_record_cannot_hold_is_refused_before_the_replay():
    # LOW 11: a float in a list under opening_range was refused only after the replay, in canonical words
    p = plan(opening_range={"minutes": 15, "levels": [362.18, 354.47]})
    assert p["opening_range"]["levels"] == [362.18, 354.47] and len(tm.plan_hash(p)) == 64
    with pytest.raises(tm.TMError, match=r"opening_range\.levels\[1\] is nan"):
        plan(opening_range={"minutes": 15, "levels": [362.18, float("nan")]})
    with pytest.raises(tm.TMError, match="cannot be recorded"):
        plan(opening_range={"minutes": 15, "volume": 2 ** 60})
