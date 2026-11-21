"""The Time Machine (S11): observed, resampled and stressed scenarios, kept apart and stated (spec 08.6 Q09, 12.1)."""
from __future__ import annotations

import dataclasses
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from itertools import pairwise

import pytest

from traderos import timemachine
from traderos.contracts import OwnerAction, ValidationError
from traderos.contracts.examples import all_examples
from traderos.contracts.research import ScenarioKind, TimeMachineReport
from traderos.store import Store
from traderos.strategies import rulebook


def test_resampling_keeps_blocks_of_consecutive_days_it_never_shuffles_single_days():
    daily = [float(i) for i in range(200)]

    def consecutive_share(block):
        paths = timemachine.block_paths(daily, horizon=100, paths=200, block=block, seed=1)
        pairs = [pair for p in paths for pair in pairwise(p)]
        return sum(1 for a, b in pairs if b == (a + 1) % 200) / len(pairs)
    assert consecutive_share(10) > 0.85                  # expected 1 - 1/10
    assert consecutive_share(1) < 0.05                   # block 1 is independent draws: the thing S11 forbids
    assert timemachine.block_paths(daily, horizon=50, paths=10, block=5, seed=3) == \
        timemachine.block_paths(daily, horizon=50, paths=10, block=5, seed=3)


def _series():
    from traderos.contracts.examples import bar
    b0 = bar()
    out = []
    for i, c in enumerate(["100", "110", "99", "105", "120", "118"]):
        d = timedelta(days=i)
        out.append(dataclasses.replace(b0, start=b0.start + d, end=b0.end + d, available_at=b0.available_at + d,
                                       open=D(c), high=D(c) + 2, low=D(c) - 2, close=D(c)))
    return {"SPY": out}


def test_crash_shifts_only_from_its_day_and_keeps_bars_valid():
    bars = _series()
    on = bars["SPY"][3].end.date()
    out = timemachine.crash(bars, on)["SPY"]
    assert out[:3] == bars["SPY"][:3]
    assert [b.close for b in out[3:]] == [b.close * D("0.9") for b in bars["SPY"][3:]]


def test_amplify_doubles_each_move_from_its_start_and_keeps_bars_valid():
    bars = _series()
    start = bars["SPY"][2].end.date()
    out = timemachine.amplify(bars, start)["SPY"]
    assert out[:2] == bars["SPY"][:2]
    for i in range(2, len(out)):
        old = math.log(float(bars["SPY"][i].close / bars["SPY"][i - 1].close))
        new = math.log(float(out[i].close / out[i - 1].close))
        assert abs(new - 2 * old) < 1e-4
        assert out[i].low <= min(out[i].open, out[i].close) and out[i].high >= max(out[i].open, out[i].close)


def test_a_report_must_carry_the_observed_history():
    tm = all_examples()["timemachine/1"]
    only_generated = tuple(s for s in tm.scenarios if s.kind is not ScenarioKind.OBSERVED)
    with pytest.raises(ValidationError):
        dataclasses.replace(tm, scenarios=only_generated)


@pytest.fixture(scope="module")
def tutorial_store(tmp_path_factory):
    from traderos.data import tutorial
    store = Store(tmp_path_factory.mktemp("tm") / "home").init()
    return store, tutorial.load(store)


def _version(store, entry, name="tm"):
    doc = {"schema_version": "strategy/1", "name": name, "hypothesis": "h", "universe": ["TUTMKT"],
           "frequency": "1d", "direction": "long", "entry": entry,
           "exit": {"any": [{"above": ["close", {"sma": 5}]}, {"bars_held": 8}]}}
    d = rulebook.draft(doc)
    v = rulebook.approve(d, OwnerAction(actor="t", method="cli_confirm", at=datetime.now(UTC),
                                        statement=d.semantic_hash[:12]))
    store.put(v)
    return v


def test_report_on_the_tutorial(tutorial_store):
    from traderos.backtest import CostModel, simulate
    from traderos.contracts import AccountAssumptions
    store, m = tutorial_store
    v = _version(store, {"below": [{"rsi": 2}, 15]})
    rep = timemachine.run(store, v, m.dataset_id, start=date(2018, 1, 2), end=date(2020, 12, 31), paths=200)
    kinds = [s.kind for s in rep.scenarios]
    assert kinds == [ScenarioKind.OBSERVED] + [ScenarioKind.RESAMPLED] * 3 + [ScenarioKind.STRESS] * 3
    assert {lim.code for lim in rep.limitations} >= {"SYNTHETIC_DATA", "GENERATED_NOT_FORECAST",
                                                    "RESAMPLING_ASSUMES_PERSISTENCE"}
    crash = next(s for s in rep.scenarios if s.name == "crash gap")
    assert any("held a position" in a for a in crash.assumptions)
    from traderos.clock import XNYS
    sessions = XNYS().sessions(date(2018, 1, 2), date(2020, 12, 31))
    shock_day = date.fromisoformat(crash.assumptions[0].split(" on ")[1].split(" ")[0])
    assert shock_day >= sessions[len(sessions) // 2]
    assert crash.metrics["change_vs_observed"].value != 0
    ids = {i.symbol: i.instrument_id for i in m.instruments}
    bars = {"TUTMKT": list(store.bar_reader().bars(m.dataset_id, ids["TUTMKT"],
                                                   as_of=datetime(2021, 1, 1, tzinfo=UTC)))}
    direct = simulate(rulebook.compile_rules(v.rules), bars, start=date(2018, 1, 2), end=date(2020, 12, 31),
                      account=AccountAssumptions(starting_cash=D("100000")), costs=CostModel())
    observed = rep.scenarios[0].metrics["net_return"].value
    assert observed == (direct.final_equity / direct.starting_cash - 1).quantize(D("0.000001"))
    again = timemachine.run(store, v, m.dataset_id, start=date(2018, 1, 2), end=date(2020, 12, 31), paths=200)
    assert again == store.get(TimeMachineReport, rep.report_id) == rep


def test_a_strategy_that_never_trades_has_nothing_to_resample(tutorial_store):
    store, m = tutorial_store
    v = _version(store, {"below": [{"rsi": 2}, 0]})
    rep = timemachine.run(store, v, m.dataset_id, start=date(2018, 1, 2), end=date(2018, 12, 31), paths=100)
    resampled = [s for s in rep.scenarios if s.kind is ScenarioKind.RESAMPLED]
    assert len(resampled) == 1 and resampled[0].paths == 0 and "nothing to resample" in resampled[0].assumptions[0]


def test_the_default_window_never_reads_the_sealed_holdout(tutorial_store, monkeypatch):
    """Regression (QT02, spec 08.4): the default window ran to the holdout's last day while the lab still counted
    that holdout as unseen. It now stops at the validation window's end, reads no bar after it, and says so."""
    from traderos.clock import XNYS
    from traderos.contracts import SplitName
    from traderos.research import default_splits
    from traderos.strategies.rulebook import compile_rules
    store, m = tutorial_store
    v = _version(store, {"below": [{"rsi": 2}, 20]})
    splits = {s.name: s for s in default_splits(m, compile_rules(v.rules).history_needed(), XNYS())}
    holdout, validation = splits[SplitName.HOLDOUT], splits[SplitName.VALIDATION]
    seen = []
    real = store.bar_reader

    def spying_reader():
        reader = real()
        bars = reader.bars

        def spy(dataset_id, instrument_id, *, as_of, **kw):
            seen.append(as_of)
            return bars(dataset_id, instrument_id, as_of=as_of, **kw)
        reader.bars = spy
        return reader
    monkeypatch.setattr(store, "bar_reader", spying_reader)
    rep = timemachine.run(store, v, m.dataset_id, paths=100, horizon=20)
    assert (rep.window_start, rep.window_end) == (splits[SplitName.DEVELOPMENT].start, validation.end)
    assert rep.window_end < holdout.start
    assert seen and max(seen) == XNYS().open_close(validation.end)[1] < XNYS().open_close(holdout.start)[0]
    observed = rep.scenarios[0]
    assert (f"before the earliest sealed holdout of this strategy family on this dataset ({holdout.start} to "
            f"{holdout.end}), which is not read") in observed.assumptions[0]
    assert "HOLDOUT_READ" not in {lim.code for lim in rep.limitations}
    # a window a person sets into that holdout is run, and says what it read
    into = timemachine.run(store, v, m.dataset_id, start=validation.start, end=holdout.end, paths=100, horizon=20)
    assert "HOLDOUT_READ" in {lim.code for lim in into.limitations}


def test_a_time_machine_run_into_the_holdout_opens_it_for_the_lab(tutorial_store):
    """QT02 / 08.4: once a Time Machine window reaches into a family's holdout, the lab counts that holdout as seen.
    The default window (development + validation) does not open it."""
    from traderos.research import Lab
    store, m = tutorial_store
    v = _version(store, {"below": [{"rsi": 2}, 7]})
    lab = Lab(store)
    spec = lab.register(v, m.dataset_id)
    holdout = next(s for s in spec.splits if s.name.value == "holdout")
    timemachine.run(store, v, m.dataset_id, paths=100, horizon=10)                 # default window
    assert lab._holdout_compromised(spec) is False
    other = _version(store, {"below": [{"rsi": 2}, 8]}, name="tm-other")           # another family: no effect
    assert f"RF-{other.strategy_id}" != spec.research_family_id
    timemachine.run(store, other, m.dataset_id, start=holdout.start, end=holdout.end, paths=100, horizon=10)
    assert lab._holdout_compromised(spec) is False
    timemachine.run(store, v, m.dataset_id, start=holdout.start, end=holdout.end, paths=100, horizon=10)
    assert lab._holdout_compromised(spec) is True


def _revision(store, previous, entry, name="tm-family"):
    doc = {"schema_version": "strategy/1", "name": name, "hypothesis": "h", "universe": ["TUTMKT"],
           "frequency": "1d", "direction": "long", "entry": entry,
           "exit": {"any": [{"above": ["close", {"sma": 5}]}, {"bars_held": 8}]}}
    d = rulebook.draft(doc, parent_version_id=previous.version_id if previous else None)
    v = rulebook.approve(d, OwnerAction(actor="t", method="cli_confirm", at=datetime.now(UTC),
                                        statement=d.semantic_hash[:12]), previous=previous)
    store.put(v)
    return v


@pytest.fixture(scope="module")
def family(tmp_path_factory):
    """OSDev6's repro (review of #9, HIGH 2): v1 has a short warm-up and a registered experiment; v2 is a revision
    with a 200-session warm-up, so its own default splits end later than v1's registered holdout begins."""
    from traderos.data import tutorial
    from traderos.research import Lab
    store = Store(tmp_path_factory.mktemp("family") / "home").init()
    m = tutorial.load(store)
    v1 = _revision(store, None, {"below": [{"rsi": 2}, 15]})
    v2 = _revision(store, v1, {"all": [{"above": ["close", {"sma": 200}]}, {"below": [{"rsi": 2}, 15]}]})
    assert v1.strategy_id == v2.strategy_id and v2.revision == 2
    lab = Lab(store)
    spec1 = lab.register(v1, m.dataset_id)
    h1 = next(s for s in spec1.splits if s.name.value == "holdout")
    return store, m, v1, v2, lab, spec1, h1


def test_the_default_window_stops_before_any_holdout_of_the_family(family):
    """Regression (OSDev6's review of #9, HIGH 2; QT02, spec 08.4): the default window used this version's own
    splits only, so a revision with a longer warm-up read the holdout registered for an earlier revision, said it
    had not, and the lab then counted that holdout as opened."""
    from traderos.clock import XNYS
    from traderos.research import default_splits
    from traderos.strategies.rulebook import compile_rules
    store, m, _v1, v2, lab, spec1, h1 = family
    own = {s.name.value: s for s in default_splits(m, compile_rules(v2.rules).history_needed(), XNYS())}
    assert own["validation"].end >= h1.start                         # the flaw's precondition: v2's own window
    assert lab._holdout_compromised(spec1) is False
    rep = timemachine.run(store, v2, m.dataset_id, paths=100, horizon=20)
    assert rep.window_end == XNYS().previous_session(h1.start) < h1.start
    assert f"({h1.start} to {h1.end}), which is not read" in rep.scenarios[0].assumptions[0]
    assert "HOLDOUT_READ" not in {lim.code for lim in rep.limitations}
    assert lab._holdout_compromised(spec1) is False
    assert timemachine.family_holdouts(store, v2, m.dataset_id) == [h1]


def test_holdout_read_and_the_labs_overlap_are_inclusive_at_the_edges(family):
    """A window that ends on the holdout's first day, or starts on its last day, has read it."""
    import dataclasses

    from traderos.contracts import TimeMachineReport, with_hash
    from traderos.contracts.examples import all_examples
    store, m, _v1, v2, lab, spec1, h1 = family
    rep = timemachine.run(store, v2, m.dataset_id, start=date(2023, 1, 3), end=h1.start, paths=100, horizon=20)
    read = [lim.detail for lim in rep.limitations if lim.code == "HOLDOUT_READ"]
    assert len(read) == 1 and f"{h1.start} to {h1.end}" in read[0]
    assert lab._holdout_compromised(spec1) is True
    # the other edge, on a fresh family record: a stored report whose window starts on the holdout's last day
    v3 = _revision(store, None, {"below": [{"rsi": 2}, 11]}, name="tm-edge")
    spec3 = lab.register(v3, m.dataset_id)
    h3 = next(s for s in spec3.splits if s.name.value == "holdout")
    assert lab._holdout_compromised(spec3) is False
    edge = with_hash(dataclasses.replace(
        all_examples()["timemachine/1"], report_id="TM-edge", strategy_version_id=v3.version_id,
        strategy_semantic_hash=v3.semantic_hash, dataset_id=m.dataset_id, dataset_hash=m.canonical_hash,
        window_start=h3.end, window_end=h3.end, canonical_hash=None))
    store.put(edge)
    assert store.get(TimeMachineReport, "TM-edge").window_start == h3.end
    assert lab._holdout_compromised(spec3) is True


def test_the_report_id_names_the_costs_and_the_cash(tutorial_store):
    """Regression (OSDev6's review of #9, MEDIUM 5): a second run with other costs or cash returned the first."""
    store, m = tutorial_store
    v = _version(store, {"below": [{"rsi": 2}, 13]})
    kw = {"start": date(2018, 1, 2), "end": date(2019, 12, 31), "paths": 100, "horizon": 20}
    base = timemachine.run(store, v, m.dataset_id, **kw)
    slip = timemachine.run(store, v, m.dataset_id, costs={"spread_bps": "2", "slippage_bps": "50",
                                                          "fee_per_share": "0", "fee_bps": "0"}, **kw)
    cash = timemachine.run(store, v, m.dataset_id, starting_cash="5000", **kw)
    assert len({base.report_id, slip.report_id, cash.report_id}) == 3
    assert "'slippage_bps': '50'" in slip.scenarios[0].assumptions[1]
    assert timemachine.run(store, v, m.dataset_id, **kw) == base


def test_holdouts_read_counts_both_edges():
    from traderos.contracts import SplitName, SplitWindow
    h = SplitWindow(name=SplitName.HOLDOUT, start=date(2024, 1, 5), end=date(2025, 12, 31))
    later = SplitWindow(name=SplitName.HOLDOUT, start=date(2025, 3, 3), end=date(2025, 12, 31))
    assert timemachine.holdouts_read([h], date(2020, 1, 2), date(2024, 1, 4)) == []
    assert timemachine.holdouts_read([h], date(2020, 1, 2), date(2024, 1, 5)) == [(h.start, h.end)]
    assert timemachine.holdouts_read([h, h], date(2025, 12, 31), date(2025, 12, 31)) == [(h.start, h.end)]
    assert timemachine.holdouts_read([later, h], date(2025, 6, 2), date(2025, 7, 1)) == [(h.start, h.end),
                                                                                         (later.start, later.end)]
