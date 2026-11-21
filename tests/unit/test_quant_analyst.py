"""S08 Quant Analyst: findings cite the engine's own numbers; any other number is refused (spec 12.1 S08).

check-claims is the guard the /trader skill runs on its own answer before it states a number, so a number passes only
when its sign, magnitude (at the precision the Passport states) and unit agree with the one field it is tied to.
"""
from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP
from decimal import Decimal as D

import pytest

from traderos import analyst, cli
from traderos import challenges as fs
from traderos.contracts import (
    ChallengeResult,
    DataError,
    ErrorCode,
    IntegrityError,
    MetricValue,
    OwnerAction,
    ResearchError,
)
from traderos.research import Lab
from traderos.store import Store
from traderos.strategies import rulebook

DOC = {"schema_version": "strategy/1", "name": "dip-buyer", "hypothesis": "short dips in an uptrend revert",
       "universe": ["TUTMKT"], "frequency": "1d", "direction": "long",
       "entry": {"all": [{"above": ["close", {"sma": 50}]}, {"below": [{"rsi": 2}, 15]}]},
       "exit": {"any": [{"above": ["close", {"sma": 5}]}, {"bars_held": 8}]}}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    from traderos.data import tutorial
    mp = pytest.MonkeyPatch()
    mp.setattr(fs, "CONTROLS", 3)
    home = tmp_path_factory.mktemp("an") / "home"
    store = Store(home).init()
    m = tutorial.load(store)
    d = rulebook.draft(DOC)
    v = rulebook.approve(d, OwnerAction(actor="t", method="cli_confirm", at=datetime.now(UTC),
                                        statement=d.semantic_hash[:12]))
    store.put(d)
    store.put(v)
    lab = Lab(store)
    out = lab.run(lab.register(v, m.dataset_id).experiment_id)
    yield store, out.passport, home
    mp.undo()


@pytest.fixture(scope="module")
def ev(run):
    store, p, _ = run
    return analyst.evidence(*analyst._load(store, p.passport_id, None))


def _ev(window: str = "development", **fields: tuple[str, str]) -> analyst.Evidence:
    return analyst.Evidence([analyst.Field("window", window, k, f"{window}.{k}", f"T {window}.{k}", D(v), u)
                             for k, (v, u) in fields.items()])


def _one(ev: analyst.Evidence, text: str) -> dict:
    rows = analyst.judge(ev, text)
    assert len(rows) == 1, rows
    return rows[0]


def _passes(ev: analyst.Evidence, text: str) -> bool:
    return all(r["status"] == analyst.PASS for r in analyst.judge(ev, text))


# ---- reading numbers ----------------------------------------------------------------------------------------------
def test_numbers_read_sign_unit_and_precision_and_skip_dates_and_identifiers():
    got = [(n.magnitude, n.sign, n.unit, n.step) for n in analyst._numbers(
        "FS08 failed on 2025-03-01T21:00:00Z; PP-x r2 lost 1.25% and $1,234.50, Sharpe \u22120.4, rsi(2) 15, "
        "ADR-005, xnys/2026.11.1, 778a67aab02a38311dd7")]
    assert got == [(D("1.25"), None, "percent", D("0.01")), (D("1234.50"), None, "usd", D("0.01")),
                   (D("0.4"), -1, "plain", D("0.1")), (D("15"), None, "plain", D(1))]


@pytest.mark.parametrize(("written", "magnitude", "unit", "step"), [
    ("10x", D(10), "x", D(1)), ("3.93x", D("3.93"), "x", D("0.01")), ("3.93\u00d7", D("3.93"), "x", D("0.01")),
    ("50bps", D(50), "bps", D(1)), ("50 bps", D(50), "bps", D(1)), ("50 basis points", D(50), "bps", D(1)),
    ("$5k", D(5000), "usd", D(1000)), ("$5.2k", D(5200), "usd", D(100)), ("5M", D(5000000), "plain", D(1000000)),
    ("$2bn", D(2000000000), "usd", D(1000000000)), ("7 percent", D(7), "percent", D(1)),
    ("9.5 USD", D("9.5"), "usd", D("0.1")), ("13 trades", D(13), "count:trades", D(1)),
])
def test_a_unit_glued_to_a_number_is_read(written, magnitude, unit, step):
    (n,) = analyst._numbers(f"it was {written} then")
    assert (n.magnitude, n.unit, n.step, n.problem) == (magnitude, unit, step, "")


@pytest.mark.parametrize("written", ["250pct", "2nd", "1e5", "5k%", "$5%", "4.6E-7"])
def test_an_unknown_or_mixed_unit_is_never_read_as_a_plain_number(written):
    n = analyst._numbers(f"it was {written} then")[0]
    assert n.unit == "unknown" and n.problem


@pytest.mark.parametrize(("written", "sign"), [
    ("-21.98%", -1), ("(21.98%)", -1), ("(50 bps)", -1), ("($5k)", -1), ("(13 trades)", None),
    ("\u221221.98%", -1), ("\u201321.98%", -1), ("\uff0d21.98%", -1),
    ("+21.98%", 1), ("21.98%", None), ("T+1", None), ("5-10%", None),
])
def test_each_written_sign_is_read(written, sign):
    assert analyst._numbers(f"x {written} y")[-1].sign == sign


# ---- the sign (HIGH 1) ----------------------------------------------------------------------------------------------
LOSS = _ev(net_return=("-0.219833", "fraction"), cagr=("-0.041443", "fraction"),
           max_drawdown=("-0.251014", "fraction"))
GAIN = _ev(net_return=("0.219833", "fraction"), cagr=("0.0792", "fraction"))


@pytest.mark.parametrize("written", ["-21.98%", "(21.98%)", "\u221221.98%", "down 21.98%", "\u201321.98%",
                                     "lost 21.98%", "down -21.98%", "-2198bps"])
def test_every_negative_form_states_a_loss_and_never_a_gain(written):
    assert _one(LOSS, f"In development, net return {written}.")["status"] == analyst.PASS
    row = _one(GAIN, f"In development, net return {written}.")
    assert row["status"] == analyst.FAIL and "sign" in row["reason"]


@pytest.mark.parametrize("written", ["+21.98%", "21.98%", "gained 21.98%", "up 21.98%"])
def test_a_sign_flip_fails(written):
    row = _one(LOSS, f"In development, net return {written}.")
    assert row["status"] == analyst.FAIL and "sign" in row["reason"], row
    assert _one(GAIN, f"In development, net return {written}.")["status"] == analyst.PASS


def test_an_explicit_minus_on_a_gain_fails_and_a_drawdown_is_a_depth():
    assert _one(GAIN, "In development, cagr -7.92%.")["status"] == analyst.FAIL
    assert _one(LOSS, "In development, cagr +4.14%.")["status"] == analyst.FAIL
    assert _one(LOSS, "In development, max drawdown 25.10%.")["status"] == analyst.PASS
    assert _one(LOSS, "In development, max drawdown -25.10%.")["status"] == analyst.PASS
    assert _one(LOSS, "In development, max drawdown +25.10%.")["status"] == analyst.FAIL
    row = _one(LOSS, "In development, net return down +21.98%.")
    assert row["status"] == analyst.UNVERIFIED and "disagree" in row["reason"]


def test_a_value_that_rounds_to_zero_has_no_sign():
    tiny = _ev(net_return=("-0.00001", "fraction"))
    assert _passes(tiny, "In development, net return 0.00%.") and _passes(tiny, "In development, net return -0.00%.")
    assert analyst._show(MetricValue(value=D("-0.00001"), unit="fraction")) == "0.00%"


# ---- the unit (HIGH 2) -----------------------------------------------------------------------------------------------
def test_a_unit_must_be_the_fields_own():
    ev = _ev(net_return=("0.005", "fraction"), sharpe=("3.93", "ratio"), expectancy_per_trade=("5000", "USD"),
             trades=("13", "trades"), sessions=("493", "sessions"))
    ok = ["net return 50bps", "net return 0.50%", "sharpe 3.93", "sharpe 3.93x", "expectancy per trade $5k",
          "expectancy per trade 5000.00 USD", "trades 13", "13 trades", "sessions 493"]
    bad = ["net return 50", "net return 0.005", "net return 50x", "sharpe 3.93%", "sharpe 393bps", "sharpe $3.93",
           "expectancy per trade 5000", "expectancy per trade 5000%", "trades 13%", "sessions 493 days",
           "trades 13 sessions", "expectancy per trade $5.2k", "sharpe 393%", "expectancy per trade 500000%",
           "trades 1300%", "net return 0.005x"]
    for t in ok:
        assert _one(ev, f"In development, {t}.")["status"] == analyst.PASS, t
    for t in bad:
        assert _one(ev, f"In development, {t}.")["status"] != analyst.PASS, t


def test_osdev6s_unit_cases_are_not_supported(ev):
    for t in ("It returns 10x the market.", "It costs 50bps.", "It made $5k.", "It was up 250pct.",
              "In the holdout, sharpe 3.93x.", "In the holdout, sharpe 3.93."):
        assert not _passes(ev, t), t
    assert _one(ev, "It was up 250pct.")["reason"].startswith("its unit 'pct'")


# ---- tied to one field (MEDIUM 4) ------------------------------------------------------------------------------------
def test_a_whole_number_no_longer_matches_any_cite(ev):
    """OSDev6: 36 of 101 'made N% a year' and 37 of 101 'Sharpe N' passed at 1f8e520. Now none does, even with the
    window named, because each is tied to its own field and judged at the Passport's own precision."""
    forms = ("It made {n}% a year.", "Sharpe {n}", "Sharpe 22 on the -21.98% net return",
             "In the holdout, net return {n}%.", "In the holdout, net return -{n}%.", "In the holdout, sharpe {n}.",
             "In the holdout, sharpe -{n}.", "In development, win rate {n}%.")
    for form in forms:
        assert [n for n in range(101) if _passes(ev, form.format(n=n))] == [], form


def test_osdev6s_sign_cases_are_refused_on_the_tutorial_passport(ev):
    for t in ("In development, net return +21.98%.", "In development, net return gained 21.98%.",
              "In development, cagr +4.14%.", "development net_return +21.98%", "It gained 21.98%."):
        assert not _passes(ev, t), t
    assert _passes(ev, "In development, net return -21.98%.") and _passes(ev, "development net_return -21.98%")


@pytest.mark.parametrize(("text", "why"), [
    ("It would have made 41.7% a year with a Sharpe of 3.93.", "'would'"),
    ("It made 41.7% a year.", "not a quote"),
    ("Sharpe 22", "but not the window"),
    ("In the holdout and in validation, net return -4.97%.", "could be"),
    ("In the holdout, net return was not -4.97%.", "'not'"),
    ("If costs doubled, in the holdout net return -4.97%.", "'if'"),
    ("Before costs, in the holdout net return -4.97%.", "'before'"),
    ("In the holdout, net return above -4.97%.", "no metric"),
    ("In the holdout, net return about -4.97%.", "no metric"),
    ("In the holdout, net return -4.97% [holdout.sharpe]", "cites [holdout.sharpe] but names"),
    ("Net return -4.97% [nope.x]", "does not hold"),
    ("In validation, -4.97% [holdout.net_return]", "its sentence is about validation"),
    ("See [gain_50].", "follows no number"),
    ("The baseline's net return -44.04%.", "not which one"),
    ("In the holdout, sharpe fewer than 30 sessions", "holds 493"),
    ("In the holdout, net return -4.97% a year.", "'year' beside it"),
    ("In the holdout, annual net return -4.97%.", "'annual' beside it"),
    ("In the holdout, gross net return -4.97%.", "'gross' beside it"),
    ("In the holdout, net return -4.97% per month.", "'per' beside it"),
    ("In the holdout, net return of minus five percent.", "in words"),
    ("In the holdout there were thirteen trades.", "in words"),
    ("In the holdout, the buy and hold baseline's net return -44.04%.", "not on the untouched holdout"),
    ("The buy and hold baseline in the holdout had net return -44.04%.", "not on the untouched holdout"),
    ("The baseline's net return in the holdout -4.97%.", "not which one"),
    ("The buy and hold baseline in the holdout had trades 13.", "names trades but not"),
    ("The strategy's net return -28.20% beat the buy and hold baseline's -44.04%.", "holds -44.04%"),
])
def test_a_number_that_cannot_be_tied_to_its_field_is_never_a_pass(ev, text, why):
    rows = analyst.judge(ev, text)
    assert rows and all(r["status"] != analyst.PASS for r in rows)
    assert any(why in r["reason"] for r in rows), rows


def test_a_number_ties_to_the_field_named_next_to_it(ev, run):
    _, p, _ = run
    tag = f"{p.passport_id} r{p.revision}"
    cases = {"In the holdout, net return -4.97%.": "metrics.holdout.net_return",
             "In validation net return was -7.96%.": "metrics.validation.net_return",
             "Net return -7.96% in validation.": "metrics.validation.net_return",
             "In the holdout, validation net return -7.96%.": "metrics.validation.net_return",
             "In the holdout there were 13 trades.": "metrics.holdout.trades",
             "The buy and hold baseline's net return -44.04%.": "baselines.buy_and_hold.net_return",
             "In development and validation together, net return -28.20%.": "metrics.in_sample.net_return",
             "In-sample sharpe -0.447745": "metrics.in_sample.sharpe",
             "Net return -4.97% [holdout.net_return]": "metrics.holdout.net_return",
             "3 trials were attempted.": "trials_attempted",
             "The starting cash was $100k.": "assumptions.starting_cash",
             "Costs: spread 2 bps.": "assumptions.cost.spread_bps",
             "Below 15 [rules.entry.all.1.below.1]": "rules.entry.all.1.below.1"}
    for text, ref in cases.items():
        row = _one(ev, text)
        assert (row["status"], row["ref"]) == (analyst.PASS, f"{tag} {ref}"), (text, row)
    for text in ("In development, cagr -4.14% a year.", "In the holdout, total trades 13.",
                 "In the holdout, average expectancy per trade -382.05 USD per trade.",
                 "In development and validation together, the buy and hold baseline's net return -44.04%."):
        assert _passes(ev, text), text
    row = _one(ev, "FS08 excess mean daily 0.0018%")
    assert row["status"] == analyst.PASS and row["ref"].endswith("(FS08) metrics.excess_mean_daily")


def test_a_number_is_judged_at_the_precision_the_passport_states():
    ev = _ev(net_return=("-0.049666", "fraction"), sharpe=("-0.17725", "ratio"), starting_cash=("100000", "USD"))
    assert _passes(ev, "In development, net return -4.97%.") and _passes(ev, "In development, net return -4.967%.")
    assert _passes(ev, "In development, net return -4.9666%.") and _passes(ev, "In development, net return -4.96660%.")
    for t in ("net return -5%", "net return -4.966%", "net return -4.9667%", "sharpe -0.18", "sharpe 0",
              "sharpe -0.177", "net return -4.9%"):
        assert _one(ev, f"In development, {t}.")["status"] == analyst.FAIL, t
    assert "less precisely" in _one(ev, "In development, net return -5%.")["reason"]
    assert _passes(ev, "In development, starting cash $100k.")           # exactly the stored value


def test_a_verbatim_quote_of_a_stored_finding_passes_and_a_rephrase_does_not(ev, run):
    store, p, _ = run
    fs06 = next(c for c in (store.get(ChallengeResult, i) for i in p.challenge_result_ids) if c.challenge_id == "FS06")
    first = fs06.finding.split(". ")[0]
    assert _passes(ev, f"FS06 says: {first}.")
    changed = first.replace("-42471.25", "-42471.35")
    assert not _passes(ev, changed)
    assert not _passes(ev, "Without its best 4 trades the P&L is -42471.25.")
    assert not analyst._quotable("fewer than 30 sessions") and not analyst._quotable("100000 USD")
    assert not _passes(ev, "In the holdout the strategy had fewer than 30 sessions.")


# ---- the property (decision 1) --------------------------------------------------------------------------------------
KINDS = (("net_return", "fraction"), ("max_drawdown", "fraction"), ("sharpe", "ratio"),
         ("expectancy_per_trade", "USD"), ("trades", "trades"), ("sessions", "sessions"))
SWAPS = {"fraction": ("", "x", "$"), "ratio": ("%", " bps", "$"), "USD": ("%", "", "x"),
         "trades": ("%", "$", " sessions"), "sessions": ("%", "$", " days")}


def _value(rng: random.Random, key: str, unit: str) -> D:
    if unit == "fraction":
        v = D(rng.randint(-999999, 999999)).scaleb(-6)
        return -abs(v) if key == "max_drawdown" else v
    if unit == "ratio":
        return D(rng.randint(-999999, 999999)).scaleb(-rng.randint(0, 6))
    if unit == "USD":
        return D(rng.randint(-10 ** 8, 10 ** 8)).scaleb(-2)
    return D(rng.randint(1, 10 ** 5))


def _restate(rng: random.Random, unit: str, v: D) -> tuple[D, str]:
    """A true restatement's magnitude and unit: at the precision the Passport states, or finer."""
    if unit == "fraction" and rng.random() < 0.5:
        return abs(v).scaleb(2).quantize(D(1).scaleb(-rng.randint(2, 4)), ROUND_HALF_UP), "%"
    if unit == "fraction":
        return abs(v).scaleb(4).quantize(D(1).scaleb(-rng.randint(0, 2)), ROUND_HALF_UP), rng.choice(("bps", " bps"))
    if unit == "ratio":
        return abs(v).quantize(D(1).scaleb(min(v.as_tuple().exponent, 0) - rng.randint(0, 2))), rng.choice(("", "x"))
    if unit == "USD":
        return abs(v).quantize(D(1).scaleb(-rng.randint(2, 3)), ROUND_HALF_UP), rng.choice(("$", " USD"))
    return abs(v), rng.choice(("", f" {unit}"))


def _write(key: str, magnitude: D, unit: str, sign: str) -> str:
    money = unit == "$"
    body = f"{'$' if money else ''}{magnitude}{'' if money else unit}"
    if sign == "(":
        body = f"({body})"
    elif sign in ("down ", "up "):
        body = sign + body
    else:
        body = sign + body
    return f"In the holdout, {key.replace('_', ' ')} {body}."


def test_property_every_true_restatement_passes_and_a_flip_a_slip_or_a_swap_fails():
    rng = random.Random(20261120)
    checked = 0
    for _ in range(600):
        key, unit = rng.choice(KINDS)
        v = _value(rng, key, unit)
        ev = _ev("holdout", **{key: (str(v), unit)})
        magnitude, written = _restate(rng, unit, v)
        negative = ("-", "\u2212", "\u2013", "(", "down ") + (("",) if key == "max_drawdown" else ())
        sign = rng.choice(negative) if v < 0 else rng.choice(("", "+", "up "))
        true = _write(key, magnitude, written, sign)
        assert _one(ev, true)["status"] == analyst.PASS, (true, v)
        if magnitude != 0:
            flip = ("+" if key == "max_drawdown" else rng.choice(("", "+"))) if v < 0 else "-"
            for wrong in (_write(key, magnitude, written, flip), _write(key, magnitude * 10, written, sign),
                          _write(key, magnitude.scaleb(-1), written, sign)):
                row = _one(ev, wrong)
                assert row["status"] == analyst.FAIL, (wrong, v, row)
        swap = rng.choice(SWAPS[unit] + (("bps" if written == "%" else "%",) if unit == "fraction" else ()))
        row = _one(ev, _write(key, magnitude, swap, sign if swap != "$" or sign != "(" else "-"))
        assert row["status"] == analyst.FAIL, (swap, v, row)
        checked += 1
    assert checked == 600


# ---- explain (HIGH 3) ---------------------------------------------------------------------------------------------
def test_every_cite_is_the_stored_value_and_the_explanation_passes_its_own_check(run, ev):
    store, p, _ = run
    out = analyst.explain(store, p.passport_id)
    assert out["passport_hash"] == p.canonical_hash and out["findings"][0]["topic"] == "verdict"
    stored = {f"{p.passport_id} r{p.revision} metrics.{k}": m.value for k, m in p.metrics.items()}
    stored |= {f"{p.passport_id} r{p.revision} baselines.{n}.{k}": m.value
               for n, b in p.baselines.items() for k, m in b.items()}
    for c in (c for f in out["findings"] for c in f["cites"]):
        if c["ref"] in stored:
            assert stored[c["ref"]] == D(c["value"])
    quotes = {ref for ref, _ in ev.quotes}
    for f in out["findings"]:
        for row in analyst.judge(ev, f["text"]):        # every number it says is tied to a field it cites, or quoted
            assert row["status"] == analyst.PASS, (f["topic"], row)
            assert row["ref"] in {c["ref"] for c in f["cites"]} | quotes, (f["topic"], row)
    analyst.check_claims(store, p.passport_id, "\n".join(f["text"] for f in out["findings"]))
    analyst.check_claims(store, p.passport_id, analyst.describe(out))
    for f in out["findings"]:
        if f["topic"].startswith("results."):
            w = f["topic"].split(".", 1)[1]
            lo = p.metrics[f"{w}.excess_mean_daily_ci95_low"].value
            hi = p.metrics[f"{w}.excess_mean_daily_ci95_high"].value
            want = "above zero" if lo > 0 else "below zero" if hi < 0 else "across zero"
            assert want in f["text"], (w, lo, hi)
    hold = next(f for f in out["findings"] if f["topic"] == "results.holdout")
    assert "net return" in hold["text"] and "-0.00%" not in "\n".join(f["text"] for f in out["findings"])


def _tenfold(digits: str) -> str:
    if "." not in digits:
        return digits + "0"
    i = digits.index(".")
    return digits[:i] + digits[i + 1] + ("." + digits[i + 2:] if digits[i + 2:] else "")


def test_every_number_explain_states_is_refused_once_altered(run, ev):
    """A last digit bumped, a tenfold slip or a sign flip on any number explain writes (its quotes included) makes the
    text fail its check."""
    store, p, _ = run
    altered = 0
    for f in analyst.explain(store, p.passport_id)["findings"]:
        text = f["text"]
        for n in analyst._numbers(text):
            core = next(t for t in analyst._lex(text) if t.kind == "num" and n.start <= t.start < n.end)
            wrong = [text[:core.start] + core.text[:-1] + str((int(core.text[-1]) + 1) % 10) + text[core.end:]]
            if n.magnitude != 0:
                wrong.append(text[:core.start] + _tenfold(core.text) + text[core.end:])
                wrong.append(text[:n.start] + "+" + text[n.start + 1:] if n.sign == -1 and text[n.start] == "-"
                             else text[:core.start] + "-" + text[core.start:])
            for w in wrong:
                assert not _passes(ev, w), (f["topic"], w[max(0, core.start - 30): core.end + 30])
                altered += 1
    assert altered > 200


def test_the_baselines_are_measured_on_development_and_validation_together(run):
    """The stored numbers prove the window: buy and hold is in the market every session but the first, so its
    exposure is 1 - 1/sessions of the window it ran on, and that is in_sample's."""
    _, p, _ = run
    exposure = p.baselines["buy_and_hold"]["exposure"].value
    sessions = {w: p.metrics[f"{w}.sessions"].value for w in ("in_sample", "holdout", "development", "validation")}
    on = [w for w, n in sessions.items() if (1 - D(1) / n).quantize(D("0.000001")) == exposure]
    assert on == [analyst.BASELINE_WINDOW] == ["in_sample"]


def test_explain_sets_a_baseline_only_beside_the_window_it_was_measured_on(run):
    store, p, _ = run
    out = analyst.explain(store, p.passport_id)
    tag = f"{p.passport_id} r{p.revision}"
    base = [f for f in out["findings"] if f["topic"].startswith("baseline.") and f["topic"] != "baseline.none"]
    assert {f["topic"] for f in base} == {f"baseline.{n}" for n in p.baselines}
    holdout = analyst._show(p.metrics["holdout.net_return"])
    for f in base:
        assert "development and validation together" in f["text"] and holdout not in f["text"]
        for c in f["cites"]:
            assert c["ref"].startswith((f"{tag} metrics.in_sample.", f"{tag} baselines.")), c
    hold = analyst._show(p.metrics["in_sample.net_return"])
    bh = analyst._show(p.baselines["buy_and_hold"]["net_return"])
    buy = next(f for f in base if f["topic"] == "baseline.buy_and_hold")
    assert f"net return {hold}, above the baseline's net return {bh}" in buy["text"]
    none = next(f for f in out["findings"] if f["topic"] == "baseline.none")
    assert "No baseline is measured on the untouched holdout, validation or development" in none["text"]
    assert "no comparison across windows" in " ".join(out["not_claimed"])


def test_without_its_window_a_baseline_is_set_beside_nothing(run, monkeypatch):
    store, p, _ = run
    monkeypatch.setattr(analyst, "BASELINE_WINDOW", "walk_forward")
    out = analyst.explain(store, p.passport_id)
    topics = [f["topic"] for f in out["findings"]]
    assert "baseline.window" in topics and not any(t.startswith("baseline.buy") for t in topics)
    assert "no results for" in next(f["text"] for f in out["findings"] if f["topic"] == "baseline.window")


# ---- the Passport, the commands --------------------------------------------------------------------------------------
def test_a_tampered_passport_cannot_be_explained(run):
    store, p, _ = run
    challenge = store.get(ChallengeResult, p.challenge_result_ids[0])
    with store.connect() as db:
        db.execute("UPDATE records SET body = replace(body, ?, ?) WHERE record_type = 'challenge/1' AND record_id = ?",
                   (challenge.finding, challenge.finding + " edited", challenge.result_id))
    try:
        with pytest.raises(IntegrityError):
            analyst.explain(store, p.passport_id)
    finally:
        with store.connect() as db:
            db.execute("UPDATE records SET body = replace(body, ?, ?) WHERE record_type = 'challenge/1' "
                       "AND record_id = ?", (challenge.finding + " edited", challenge.finding, challenge.result_id))


def test_an_unsupported_claim_is_refused_naming_each_number_and_why(run):
    store, p, _ = run
    with pytest.raises(ResearchError) as e:
        analyst.check_claims(store, p.passport_id, "It would have made 41.7% a year. In development, net return "
                                                   "+21.98%.")
    assert e.value.code is ErrorCode.INSUFFICIENT_EVIDENCE
    assert "41.7% UNVERIFIED" in str(e.value) and "+21.98% FAIL" in str(e.value)
    assert (e.value.details["failed"], e.value.details["unverified"]) == ("1", "1")
    assert [c["number"] for c in json.loads(e.value.details["claims"])] == ["41.7%", "+21.98%"]
    res = analyst.check_claims(store, p.passport_id, f"In the holdout, net return "
                                                     f"{analyst._show(p.metrics['holdout.net_return'])}.")
    assert [c["status"] for c in res["claims"]] == [analyst.PASS] and res["not_checked"] and res["how_checked"]


def test_the_commands(run, tmp_path, capsys, monkeypatch):
    _, p, home = run
    assert cli.main(["--json", "--home", str(home), "passport", "explain", p.passport_id]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["passport_id"] == p.passport_id
    good, bad = tmp_path / "good.txt", tmp_path / "bad.txt"
    good.write_text(f"{p.trials_attempted} trials were attempted.")
    bad.write_text("It returned 12.34% in 2024.")
    assert cli.main(["--home", str(home), "passport", "check-claims", p.passport_id, "--text", str(good)]) == 0
    assert "trials_attempted" in capsys.readouterr().out
    assert cli.main(["--json", "--home", str(home), "passport", "check-claims", p.passport_id, "--text",
                     str(bad)]) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == ErrorCode.INSUFFICIENT_EVIDENCE.value and "12.34%" in failure["message"]
    (tmp_path / "bin.txt").write_bytes(b"\xff\xfe")
    assert cli.main(["--home", str(home), "passport", "check-claims", p.passport_id, "--text",
                     str(tmp_path / "bin.txt")]) == 3
    from traderos.capabilities import Capabilities
    from traderos.contracts import CapabilityDisabled

    def off(self, cid):
        raise CapabilityDisabled(f"{cid.value} is off", code=ErrorCode.CAPABILITY_DISABLED)
    monkeypatch.setattr(Capabilities, "require", off)
    assert cli.main(["--home", str(home), "passport", "explain", p.passport_id]) == 3


def test_a_later_revision_is_read_by_default_and_an_earlier_one_on_request(run):
    store, p, _ = run
    assert analyst.explain(store, p.passport_id, revision=p.revision)["revision"] == p.revision
    with pytest.raises(DataError) as e:
        analyst.explain(store, p.passport_id, revision=p.revision + 5)
    assert e.value.code is ErrorCode.MISSING_DATA


def test_shown_values_never_read_minus_zero_and_counts_carry_no_unit():
    assert analyst._show(MetricValue(value=D("-0.00001"), unit="fraction")) == "0.00%"
    assert analyst._show(MetricValue(value=D("-0.0123"), unit="fraction")) == "-1.23%"
    assert analyst._show(MetricValue(value=D("-0.00125"), unit="fraction")) == "-0.13%"      # half up, as judged
    assert analyst._show(MetricValue(value=D("38"), unit="trades")) == "38"
    assert analyst._show(MetricValue(value=D("4.6E-7"), unit="ratio")) == "0.00000046"     # never E notation
    assert analyst._show(MetricValue(value=D("-0.004"), unit="USD")) == "0.00 USD"
    assert analyst._show(MetricValue(value=None, unit="fraction", unavailable_reason="no trades")).startswith("unavail")
