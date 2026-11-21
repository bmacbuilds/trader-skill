"""The lab: rulebook, simulator reference fixtures, statistics, verdict, experiments, Passport (spec 07.2, 08, 09, 10).

Simulator fixtures are small hand-built series whose fills are worked by hand in the comments (spec 09.5): costs are
2 bps spread (1 bp per side) + 1 bp slippage, so a buy fills at reference x 1.0002 and a sell at reference x 0.9998.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from traderos import challenges as fs
from traderos.backtest import CostModel, simulate
from traderos.clock import XNYS
from traderos.contracts import (
    AccountAssumptions,
    Adjustment,
    Bar,
    ChallengeOutcome,
    ChallengeResult,
    DatasetUsability,
    ErrorCode,
    ExperimentOutcome,
    IntegrityError,
    Interval,
    OwnerAction,
    StrategyVersion,
    Trial,
    ValidationError,
    with_hash,
)
from traderos.contracts.examples import all_examples
from traderos.research import Lab, decide
from traderos.research import statistics as stats
from traderos.store import Store
from traderos.strategies import rulebook

CAL = XNYS()
ACCOUNT = AccountAssumptions(starting_cash=D("10000"))
COSTS = CostModel()


def bars(symbol: str, days: list[date], ohlc: list[tuple[str, str, str, str]]) -> list[Bar]:
    out = []
    for day, (o, h, lo, c) in zip(days, ohlc, strict=True):
        op, cl = CAL.open_close(day)
        out.append(Bar(instrument_id=f"INS-{symbol}-X-USD", interval=Interval.D1, start=op, end=cl, open=D(o),
                       high=D(h), low=D(lo), close=D(c), volume=D(1000), available_at=cl, provider="test",
                       adjustment=Adjustment.RAW))
    return out


def book(entry, exit_=None, *, stop=None, universe=("AAA",), fraction=None):
    doc = {"schema_version": "strategy/1", "name": "t", "hypothesis": "test", "universe": list(universe),
           "frequency": "1d", "direction": "long", "entry": entry,
           "exit": exit_ or {"bars_held": 100}}
    if stop:
        doc["hypothetical_stop"] = stop
    if fraction:
        doc["sizing"] = {"fraction_of_equity": fraction}
    return rulebook.compile_rules(doc)


DAYS = CAL.sessions(date(2026, 11, 2), date(2026, 11, 13))          # 10 sessions, Mon 2 .. Fri 13 November


# ---- rulebook ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("change,code", [
    ({"extra": 1}, ErrorCode.INVALID_STRATEGY),
    ({"direction": "short"}, ErrorCode.OUTSIDE_CAPABILITY),
    ({"frequency": "5m"}, ErrorCode.OUTSIDE_CAPABILITY),
    ({"universe": ["AAA", "AAA"]}, ErrorCode.INVALID_STRATEGY),
    ({"universe": ["=cmd"]}, ErrorCode.INVALID_STRATEGY),
    ({"sizing": {"fraction_of_equity": "1.5"}}, ErrorCode.INVALID_STRATEGY),
    ({"schema_version": "strategy/2"}, ErrorCode.SCHEMA_UNSUPPORTED),
    ({"entry": {"below": ["close", {"rsi": 2}]}}, ErrorCode.INVALID_STRATEGY),        # price vs RSI: units
])
def test_rulebook_refuses(change, code):
    doc = dict(book({"above": ["close", "1"]}).document) | change
    with pytest.raises(ValidationError) as e:
        rulebook.compile_rules(doc)
    assert e.value.code is code


@pytest.mark.parametrize("text", ['{"a": 1, "a": 2}', '{"x": 1.5}', "schema_version: strategy/1\nname: x", "nope"])
def test_rulebook_text_is_strict_json(text):
    with pytest.raises(ValidationError):
        rulebook.compile_rules(text)


def test_semantic_hash_ignores_notes_and_draft_ids_are_stable():
    doc = dict(book({"above": ["close", "1"]}).document)
    assert rulebook.semantic_hash(doc) == rulebook.semantic_hash(doc | {"notes": "words"})
    assert rulebook.draft(doc).draft_id == rulebook.draft(doc).draft_id


def test_approval_needs_the_exact_hash_and_a_person_method():
    d = rulebook.draft(book({"above": ["close", "1"]}).document)
    now = datetime.now(UTC)
    for action in (OwnerAction(actor="local", method="cli_confirm", at=now, statement="looks fine"),
                   OwnerAction(actor="local", method="ai_said_so", at=now, statement=d.semantic_hash[:12])):
        with pytest.raises(ValidationError) as e:
            rulebook.approve(d, action)
        assert e.value.code is ErrorCode.INVALID_APPROVAL
    ok = OwnerAction(actor="local", method="cli_confirm", at=now, statement=f"rules {d.semantic_hash[:12]}")
    v1 = rulebook.approve(d, ok)
    assert rulebook.approve(d, ok, previous=v1) == v1                      # unchanged rules: the version stands
    d2 = rulebook.draft(dict(d.rules) | {"entry": {"above": ["close", "2"]}}, parent_version_id=v1.version_id)
    v2 = rulebook.approve(d2, dataclasses.replace(ok, statement=f"rules {d2.semantic_hash[:12]}"), previous=v1)
    assert (v2.revision, v2.parent_version_id, v2.strategy_id) == (2, v1.version_id, v1.strategy_id)


# ---- simulator reference fixtures (spec 09.5) ---------------------------------------------------------------------
def test_signal_at_a_close_fills_at_the_next_open_with_costs():
    # close crosses above 101 on day 2 (index 1) -> buy at day 3's open 102 x 1.0002 = 102.0204
    series = bars("AAA", DAYS, [("100", "101", "99", "100"), ("100", "102", "99", "101.5"),
                                ("102", "103", "101", "102")] + [("102", "103", "101", "102")] * 7)
    r = simulate(book({"above": ["close", "101"]}), {"AAA": series}, start=DAYS[0], end=DAYS[-1], account=ACCOUNT,
                 costs=COSTS)
    t = r.trades[0]
    assert t.entry_signal_at == series[1].end and t.entry_at == series[2].start
    assert t.entry_price == D("102.0204")
    assert t.quantity == D(98)                            # floor(10000 / (101.5 x 1.0002)) = 98
    assert t.exit_reason == "window_end" and t.exit_price == D("101.9796")    # 102 x 0.9998 at the last close


GAP = [("100", "101", "99", "100"),     # d0: a signal, but ATR(1) needs a previous close: no stop, no entry
       ("100", "101", "99.5", "100"),   # d1: signal; ATR(1) = true range = max(1.5, |101-100|, |99.5-100|) = 1.5
       ("100", "101", "99", "100"),     # d2: buy at the open 100 x 1.0002 = 100.02; stop 100.02 - 1.5 = 98.52
       ("95", "96", "94", "95"),        # d3: opens at 95, below the stop: sell at 95 x 0.9998 = 94.981 at the OPEN
       ("95", "96", "94", "95")]


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    series = bars("AAA", DAYS[:5], GAP)
    b = book({"above": ["close", "99"]}, stop={"atr_multiple": "1", "atr_period": 1})
    r = simulate(b, {"AAA": series}, start=DAYS[0], end=DAYS[4], account=ACCOUNT, costs=COSTS)
    t = r.trades[0]
    assert r.skipped_entries["stop_unavailable"] == 1                     # d0: fail closed without a stop
    assert t.entry_at == series[2].start and t.entry_price == D("100.0200") and t.stop_price == D("98.5200")
    assert t.exit_reason == "stop_gap" and t.exit_price == D("94.9810") and t.exit_at == series[3].start


def test_a_stop_touched_on_the_entry_bar_is_assumed_hit_and_labelled():
    # as GAP to d2's open (entry 100.02, stop 98.52), but d2's own low is 97: entry and stop on one bar, order
    # unknown: assumed hit (conservative) at 98.52 x 0.9998 = 98.500296 -> 98.5003, and labelled
    series = bars("AAA", DAYS[:5], [*GAP[:2], ("100", "100.5", "97", "100"), *GAP[2:4]])
    b = book({"above": ["close", "99"]}, stop={"atr_multiple": "1", "atr_period": 1})
    r = simulate(b, {"AAA": series}, start=DAYS[0], end=DAYS[4], account=ACCOUNT, costs=COSTS)
    t = r.trades[0]
    assert t.ambiguous and t.exit_reason == "stop" and t.exit_price == D("98.5003")
    assert r.limitations["AMBIGUOUS_BAR_ASSUMPTION"] == 1


def test_a_cash_account_never_spends_unsettled_money():
    # two instruments, 100% sizing each: the second entry cannot use the first's sale proceeds before T+1
    a = bars("AAA", DAYS[:6], [("100", "101", "99", "100")] * 6)
    b2 = bars("BBB", DAYS[:6], [("50", "51", "49", "50")] * 6)
    b = book({"above": ["close", "1"]}, {"bars_held": 1}, universe=("AAA", "BBB"), fraction="1")
    r = simulate(b, {"AAA": a, "BBB": b2}, start=DAYS[0], end=DAYS[5], account=ACCOUNT, costs=COSTS)
    assert r.skipped_entries.get("insufficient_settled_cash", 0) + r.skipped_entries.get(
        "reduced_to_settled_cash", 0) >= 1
    settled = D("10000")
    for e in r.ledger:
        settled += e.settled_cash_delta
        assert settled >= -D("0.01"), "settled cash went negative"


def test_sale_proceeds_settle_t_plus_1_before_they_can_buy():
    # d0: AAA closes above 100.5 -> buy AAA at d1's open with all the cash; d1: AAA exits (bars_held 1) and BBB
    # signals. At d2's open AAA is sold, but its proceeds settle at d3: BBB cannot be bought with them at d2.
    a = bars("AAA", DAYS[:5], [("100", "101", "99", "101")] + [("100", "101", "99", "100")] * 4)
    b2 = bars("BBB", DAYS[:5], [("100", "101", "99", "100")] + [("100", "102", "99", "101")] * 4)
    b = book({"above": ["close", "100.5"]}, {"bars_held": 1}, universe=("AAA", "BBB"), fraction="1")
    r = simulate(b, {"AAA": a, "BBB": b2}, start=DAYS[0], end=DAYS[4], account=ACCOUNT, costs=COSTS)
    # AAA used 98 x 100.02 = 9801.96 of 10000: only 198.04 is settled at d2, so BBB is cut to 1 share at 101.0202
    assert r.trades[0].symbol == "AAA" and r.trades[0].exit_at == a[2].start
    (bbb_d2,) = [t for t in r.trades if t.symbol == "BBB" and t.entry_at == b2[2].start]
    assert bbb_d2.quantity == 1 and r.skipped_entries == {"reduced_to_settled_cash": 1}


def test_zero_trade_strategy_reports_unavailable_never_zero():
    series = bars("AAA", DAYS, [("100", "101", "99", "100")] * 10)
    r = simulate(book({"above": ["close", "1000"]}), {"AAA": series}, start=DAYS[0], end=DAYS[-1], account=ACCOUNT,
                 costs=COSTS)
    s = stats.summarize(r, seed=1)
    assert r.trades == [] and s.metrics["net_return"].value == 0
    for k in ("win_rate", "profit_factor", "avg_trade_return", "sharpe", "excess_mean_daily_ci95_low"):
        assert s.metrics[k].value is None and s.metrics[k].unavailable_reason


def test_later_bars_never_change_earlier_trades():
    """QT01 at the simulator level: cut the series, and every trade that ended before the cut is identical."""
    m = all_examples()["dataset/1"]
    del m
    from traderos.data import build, tutorial
    _, tut = build(tutorial.csv_bytes(), tutorial.OPTIONS, now=datetime(2026, 11, 20, tzinfo=UTC))
    series = {"TUTMKT": [x for x in tut if "TUTMKT" in x.instrument_id]}
    b = book({"below": [{"rsi": 2}, 15]}, {"any": [{"above": ["close", {"sma": 5}]}, {"bars_held": 5}]},
             universe=("TUTMKT",))
    full = simulate(b, series, start=date(2019, 1, 2), end=date(2019, 12, 31), account=ACCOUNT, costs=COSTS)
    cut = date(2019, 7, 1)
    poisoned = {"TUTMKT": [x if x.end.date() < cut else dataclasses.replace(x, open=x.open * 2, high=x.high * 2,
                                                                             low=x.low * 2, close=x.close * 2)
                           for x in series["TUTMKT"]]}
    other = simulate(b, poisoned, start=date(2019, 1, 2), end=date(2019, 12, 31), account=ACCOUNT, costs=COSTS)
    before = [t.as_dict() for t in full.trades if t.exit_at and t.exit_at.date() < cut]
    assert before and before == [t.as_dict() for t in other.trades if t.exit_at and t.exit_at.date() < cut]


# ---- statistics --------------------------------------------------------------------------------------------------
def test_drawdown_and_bootstrap_are_deterministic():
    from traderos.backtest import SimResult
    curve = [(DAYS[i], D(v)) for i, v in enumerate(["100", "110", "99", "105", "120"])]
    r = SimResult(trades=[], equity=curve, ledger=[], exposure_sessions=5, starting_cash=D("100"))
    s = stats.summarize(r, seed=3, bootstrap=False)
    assert s.metrics["max_drawdown"].value == D("-0.100000")              # 110 -> 99
    assert s.metrics["max_drawdown_sessions"].value == 2
    xs = [0.01, -0.02, 0.015, 0.0, 0.005] * 20
    assert stats.stationary_bootstrap_mean(xs, seed=5) == stats.stationary_bootstrap_mean(xs, seed=5)


# ---- verdict/1 ---------------------------------------------------------------------------------------------------
def _challenge(cid, outcome, **metrics):
    from traderos.contracts import MetricValue
    return ChallengeResult(result_id=f"CR-{cid}", challenge_id=cid, registry_version="fs/1", experiment_id="EX",
                           hypothesis="h", scope="s", outcome=outcome, finding="f",
                           metrics={k: MetricValue(value=D(v), unit="fraction") for k, v in metrics.items()},
                           created_at=datetime.now(UTC))


def test_verdict_policy():
    from traderos.backtest import SimResult, Trade
    real = dataclasses.replace(all_examples()["dataset/1"], synthetic=False, source_kind="csv",
                               usability=DatasetUsability.LIMITED)
    trade = Trade(symbol="A", instrument_id="A", entry_signal_at=datetime.now(UTC), entry_at=datetime.now(UTC),
                  entry_price=D(1), quantity=D(1), entry_fee=D(0), stop_price=None, exit_price=D(2))
    from traderos.research import MIN_TRADES
    assert MIN_TRADES == 100                                 # founder sign-off 2026-11-20 (ADR-005)
    many = SimResult(trades=[trade] * 100, equity=[], ledger=[], exposure_sessions=1)
    passing = [_challenge(c, ChallengeOutcome.PASS) for c in ("FS01", "FS02", "FS04", "FS08", "FS09", "FS10")]
    assert decide(passing, many, real, False)[0] is ExperimentOutcome.CANDIDATE_FOR_PAPER
    assert decide(passing, many, all_examples()["dataset/1"], False)[0] is ExperimentOutcome.INSUFFICIENT_EVIDENCE
    assert decide(passing, many, real, True)[1] == ["HOLDOUT_COMPROMISED"]
    few = decide(passing, SimResult(trades=[trade] * 99, equity=[], ledger=[], exposure_sessions=1), real, False)
    assert few[0] is ExperimentOutcome.INSUFFICIENT_EVIDENCE and "99 in-sample trades; 100 needed" in few[1][0]
    assert "quant review is still pending" in few[1][0]
    leak = [c if c.challenge_id != "FS09" else _challenge("FS09", ChallengeOutcome.FAIL) for c in passing]
    assert decide(leak, many, real, False)[0] is ExperimentOutcome.REJECTED
    worse = [c if c.challenge_id != "FS08" else _challenge("FS08", ChallengeOutcome.FAIL, ci95_high="-0.001")
             for c in passing]
    assert decide(worse, many, real, False)[0] is ExperimentOutcome.REJECTED
    missing = [c for c in passing if c.challenge_id != "FS04"]
    assert "FS04 missing" in decide(missing, many, real, False)[1]
    not_run = [c if c.challenge_id != "FS02" else _challenge("FS02", ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA)
               for c in passing]
    assert decide(not_run, many, real, False)[0] is ExperimentOutcome.INSUFFICIENT_EVIDENCE


# ---- the whole lifecycle on the tutorial ---------------------------------------------------------------------------
DOC = {"schema_version": "strategy/1", "name": "life", "hypothesis": "pullbacks revert",
       "universe": ["TUTMKT"], "frequency": "1d", "direction": "long",
       "entry": {"all": [{"above": ["close", {"sma": 50}]}, {"below": [{"rsi": 2}, 15]}]},
       "exit": {"any": [{"above": ["close", {"sma": 5}]}, {"bars_held": 8}]}}


@pytest.fixture(scope="module")
def lab_run(tmp_path_factory):
    from traderos.data import tutorial
    mp = pytest.MonkeyPatch()
    mp.setattr(fs, "CONTROLS", 3)
    store = Store(tmp_path_factory.mktemp("lab") / "home").init()
    m = tutorial.load(store)
    d = rulebook.draft(DOC)
    v = rulebook.approve(d, OwnerAction(actor="t", method="cli_confirm", at=datetime.now(UTC),
                                        statement=d.semantic_hash[:12]))
    store.put(d)
    store.put(v)
    lab = Lab(store)
    spec = lab.register(v, m.dataset_id)
    first = lab.run(spec.experiment_id)
    yield store, lab, m, v, spec, first
    mp.undo()


def test_experiment_is_frozen_registered_once_and_fully_recorded(lab_run):
    store, lab, m, v, spec, out = lab_run
    assert lab.register(v, m.dataset_id).experiment_id == spec.experiment_id
    assert [t.split.value for t in out.trials] == ["development", "validation", "holdout"]
    assert sorted(c.challenge_id for c in out.challenges) == sorted(fs.CHALLENGES)
    assert all(store.get(Trial, t.trial_id) == t for t in out.trials)
    assert out.verdict is ExperimentOutcome.INSUFFICIENT_EVIDENCE
    assert any("SYNTHETIC_DATA" in r for r in out.reasons)
    with store.connect() as db:
        assert db.execute("SELECT status FROM runs WHERE experiment_id = ?", (spec.experiment_id,)).fetchall() == \
            [("completed",)]


def test_passport_reopens_and_detects_tampering(lab_run):
    from traderos import passports
    store, _, _, _, _, out = lab_run
    p = passports.reopen(Store(store.home), out.passport.passport_id)
    assert p == out.passport and p.state.value == "insufficient_evidence"
    text = passports.render(store, p)
    assert "insufficient evidence" in text and p.canonical_hash in text and "not investment advice" in text
    with store.connect() as db:
        db.execute("UPDATE records SET body = replace(body, 'the approved rules only', 'THE approved rules only') "
                   "WHERE record_type = 'trial/1'")
    with pytest.raises(IntegrityError):
        passports.reopen(store, p.passport_id, 1)


def test_replay_reproduces_and_a_second_family_experiment_has_a_compromised_holdout(tmp_path):
    from traderos import passports
    from traderos.data import tutorial
    mp = pytest.MonkeyPatch()
    mp.setattr(fs, "CONTROLS", 2)
    try:
        store = Store(tmp_path / "home").init()
        m = tutorial.load(store)
        d = rulebook.draft(DOC)
        v = rulebook.approve(d, OwnerAction(actor="t", method="cli_confirm", at=datetime.now(UTC),
                                            statement=d.semantic_hash[:12]))
        store.put(v)
        lab = Lab(store)
        spec = lab.register(v, m.dataset_id)
        first = lab.run(spec.experiment_id)
        again = lab.run(spec.experiment_id)                                 # QT07: exact replay
        assert again.verification_replay and not any(r.startswith("NONREPRODUCIBLE") for r in again.reasons)
        assert again.passport.revision == first.passport.revision + 1
        assert passports.reopen(store, again.passport.passport_id).previous_revision_hash == \
            first.passport.canonical_hash
        mp.setattr(fs, "CONTROLS", 3)                                       # the platform changed under a replay
        drifted = lab.run(spec.experiment_id)
        assert any(r.startswith("NONREPRODUCIBLE") for r in drifted.reasons)
        mp.setattr(fs, "CONTROLS", 2)
        other = lab.register(v, m.dataset_id, seed=99)                      # QT02: same family, same holdout
        assert other.experiment_id != spec.experiment_id
        out = lab.run(other.experiment_id)
        assert out.holdout_compromised and "HOLDOUT_COMPROMISED" in out.reasons
        fs04 = next(c for c in out.challenges if c.challenge_id == "FS04")
        assert fs04.outcome is ChallengeOutcome.FAIL
    finally:
        mp.undo()


def test_a_changed_strategy_or_dataset_cannot_run_a_frozen_experiment(lab_run):
    store, lab, _, v, spec, _ = lab_run
    tampered = with_hash(dataclasses.replace(v, version_id="SV-other", semantic_hash="0" * 64))
    store.put(tampered)
    bad = dataclasses.replace(spec, experiment_id="EX-bad", strategy_version_id="SV-other", canonical_hash=None)
    store.put(with_hash(bad))
    from traderos.contracts import ResearchError
    with pytest.raises(ResearchError) as e:
        lab.run("EX-bad")
    assert e.value.code is ErrorCode.NONREPRODUCIBLE
    assert isinstance(store.get(StrategyVersion, v.version_id), StrategyVersion)


def test_a_crashing_challenge_is_an_error_never_a_pass(monkeypatch):
    import types

    def boom(ctx):
        raise ZeroDivisionError("broken challenge")
    monkeypatch.setattr(fs, "CHALLENGES", {"FS01": boom})
    (result,) = fs.run_all(types.SimpleNamespace(experiment_id="EX-x", id_suffix=""))
    assert result.outcome is ChallengeOutcome.ERROR and "broken challenge" in result.finding


def test_a_daily_strategy_never_runs_on_intraday_bars(lab_run):
    """OSDev6's #8 review: sma(5) on 5-minute bars is 25 minutes, and the result must not read as daily."""
    from traderos.contracts import ResearchError
    store, lab, m, v, spec, _ = lab_run
    m5 = with_hash(dataclasses.replace(m, dataset_id="DS-m5", interval=Interval.M5, canonical_hash=None))
    store.put_dataset(m5, [])
    with pytest.raises(ResearchError) as e:
        lab.register(v, "DS-m5")
    assert e.value.code is ErrorCode.OUTSIDE_CAPABILITY and "5m" in str(e.value)
    old = with_hash(dataclasses.replace(spec, experiment_id="EX-m5", dataset_id="DS-m5", dataset_hash=m5.canonical_hash,
                                        canonical_hash=None))
    store.put(old)                                      # a spec frozen before the check existed
    with pytest.raises(ResearchError) as e:
        lab.run("EX-m5")
    assert e.value.code is ErrorCode.OUTSIDE_CAPABILITY
    intraday = [dataclasses.replace(b, interval=Interval.M5) for b in bars("AAA", DAYS, [("1", "1", "1", "1")] * 10)]
    with pytest.raises(ResearchError):
        simulate(book({"above": ["close", "0"]}), {"AAA": intraday}, start=DAYS[0], end=DAYS[-1], account=ACCOUNT,
                 costs=COSTS)


def test_the_ledger_reconciles_to_the_exact_cent():
    from traderos import backtest
    from traderos.contracts import IntegrityError
    series = bars("AAA", DAYS, [("100", "101", "99", "100"), ("100", "102", "99", "101.5"),
                                ("102", "103", "101", "102")] + [("102.33", "103", "101", "102.17")] * 7)
    r = simulate(book({"above": ["close", "101"]}), {"AAA": series}, start=DAYS[0], end=DAYS[-1], account=ACCOUNT,
                 costs=COSTS)
    t = r.trades[0]
    assert t.entry_cost == (t.entry_price * t.quantity).quantize(D("0.01"))
    assert sum(e.cash_delta for e in r.ledger) == ACCOUNT.starting_cash + t.booked_pnl
    assert abs(t.booked_pnl - t.net_pnl) <= D("0.01")
    final = ACCOUNT.starting_cash + t.booked_pnl
    backtest._reconcile(r, final)
    with pytest.raises(IntegrityError):
        backtest._reconcile(r, final + D("0.01"))           # one cent off is caught, whatever the trade count
    leak = dataclasses.replace(r.ledger[-1], cash_delta=r.ledger[-1].cash_delta + D("0.01"))
    with pytest.raises(IntegrityError):
        backtest._reconcile(dataclasses.replace(r, ledger=[*r.ledger[:-1], leak]), final + D("0.01"))
