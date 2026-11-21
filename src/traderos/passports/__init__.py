"""The Strategy Evidence Passport (spec 10 I01, I08): one portable, versioned, immutable dossier per strategy.

Each completed experiment appends a revision that names the one before it (previous_revision_hash). Nothing is
rewritten. The Passport carries its rules, data rights and limits, every assumption, every trial, every challenge
(including those not performed, with why), baselines, uncertainty, and the research verdict. Four things are
reported separately and never merged into one green badge (I08): data quality, test execution, strategy merit, and
operational freshness.

`reopen` proves after a restart that every reference still matches what is stored: the strategy, the experiment,
the dataset, the trials and the challenges, by their hashes (a dataset recorded on another machine is the same dataset
when its content is, ADR-013). The Markdown rendering names the Passport's canonical
hash; its own file hash differs, as it should (spec 05.4).
"""
from __future__ import annotations

from datetime import UTC, datetime

from traderos.contracts import (
    ChallengeOutcome,
    ChallengeResult,
    ErrorCode,
    EvidencePassport,
    ExperimentOutcome,
    ExperimentSpec,
    IntegrityError,
    Limitation,
    PassportState,
    Severity,
    StrategyVersion,
    Trial,
    UncertaintyReport,
    record_hash,
    verify_hash,
    with_hash,
)

STATE = {ExperimentOutcome.REJECTED: PassportState.REJECTED,
         ExperimentOutcome.INSUFFICIENT_EVIDENCE: PassportState.INSUFFICIENT_EVIDENCE,
         ExperimentOutcome.CANDIDATE_FOR_PAPER: PassportState.CANDIDATE_FOR_PAPER}
DISCLAIMER = ("This is a research record, not investment advice and not a recommendation. A verdict describes the "
              "evidence in these tests on this data; it never means profitable, and TraderOS places no orders.")


def latest(store, strategy_id: str) -> EvidencePassport | None:
    passport_id = f"PP-{strategy_id}"
    best = None
    for p in store.all(EvidencePassport, limit=None):
        if p.passport_id == passport_id and (best is None or p.revision > best.revision):
            best = p
    return best


def build(store, outcome) -> EvidencePassport:
    """A new Passport revision from a completed RunOutcome (traderos.research)."""
    spec: ExperimentSpec = outcome.spec
    version = store.get(StrategyVersion, spec.strategy_version_id)
    manifest = store.bar_reader().manifest(spec.dataset_id)
    previous = latest(store, version.strategy_id)
    metrics = {}
    for window, summary in sorted(outcome.summaries.items()):
        for name, value in summary.metrics.items():
            metrics[f"{window}.{name}"] = value
    counts = {o: sum(1 for c in outcome.challenges if c.outcome is o) for o in ChallengeOutcome}
    ran = counts[ChallengeOutcome.PASS] + counts[ChallengeOutcome.FAIL]
    test_text = (f"{ran} of {len(outcome.challenges)} challenges ran: {counts[ChallengeOutcome.PASS]} passed, "
                 f"{counts[ChallengeOutcome.FAIL]} failed; "
                 f"{counts[ChallengeOutcome.NOT_PERFORMED_DUE_TO_DATA]} not performed (data), "
                 f"{counts[ChallengeOutcome.NOT_APPLICABLE]} not applicable, {counts[ChallengeOutcome.ERROR]} error. "
                 "Author-run; not independently verified.")
    data_bits = [manifest.usability.value]
    data_bits += [lim.code for lim in manifest.limitations]
    if manifest.synthetic:
        data_bits.insert(0, "SYNTHETIC tutorial data")
    limitations = list(manifest.limitations)
    for name, run in sorted(outcome.runs.items()):
        for code, n in sorted(run.limitations.items()):
            limitations.append(Limitation(code=code, detail=f"{n} occurrence(s) in the {name} run",
                                          severity=Severity.INFO))
    for reason in outcome.reasons:
        limitations.append(Limitation(code="NOT_A_CANDIDATE_BECAUSE", detail=reason))
    if outcome.verification_replay:
        limitations.append(Limitation(code="VERIFICATION_REPLAY", detail="An exact rerun of a frozen experiment; it "
                                      "makes no new statistical claim (spec 08.4)."))
    assumptions = {
        "fill_model": spec.fill_model_id, "cost_model": spec.cost_model_id,
        **{f"cost.{k}": v for k, v in sorted(spec.cost_params.items())},
        "starting_cash": f"{spec.account.starting_cash} {spec.account.currency}",
        "account": f"{spec.account.account_type}, settles T+{spec.account.settlement_days}, "
                   f"{'fractional' if spec.account.fractional_shares else 'whole'} shares",
        **{f"window.{s.name.value}": f"{s.start} to {s.end}" for s in spec.splits},
        "calendar": f"{spec.calendar_id} {spec.calendar_version}",
        "annualization": "252 sessions per year (daily bars)",
        "uncertainty": "stationary bootstrap of daily excess returns, 1000 resamples, seeded",
        "matched_baseline": "8 random-entry controls with the strategy's own entry rate, exits, stop and costs",
        "verdict_policy": (f"{spec.verdict_policy_id} (ADR-005: trade minimum signed off by the founder 2026-11-20; "
                           "the other numbers proposed; independent quant review pending)"),
        "challenge_registry": spec.challenge_policy_id,
    }
    passport = EvidencePassport(
        passport_id=f"PP-{version.strategy_id}", revision=(previous.revision + 1) if previous else 1,
        previous_revision_hash=previous.canonical_hash if previous else None, state=STATE[outcome.verdict],
        strategy_version_id=version.version_id, strategy_semantic_hash=version.semantic_hash,
        hypothesis=version.hypothesis, rules=version.rules, experiment_ids=(spec.experiment_id,),
        experiment_spec_hashes=(spec.canonical_hash,), dataset_ids=(manifest.dataset_id,),
        dataset_hashes=(manifest.canonical_hash,),
        data_rights=(f"{manifest.rights.label} (stated by {manifest.rights.stated_by})",), assumptions=assumptions,
        trial_ids=tuple(t.trial_id for t in outcome.trials), trials_attempted=len(outcome.trials), metrics=metrics,
        baselines=outcome.baselines, challenge_result_ids=tuple(c.result_id for c in outcome.challenges),
        uncertainty=UncertaintyReport(data_quality="; ".join(data_bits), test_execution=test_text,
                                      strategy_merit=outcome.verdict,
                                      operational_freshness="not applicable: historical research"),
        limitations=tuple(limitations),
        reproduce_command=f"traderos research run {version.version_id} --dataset {manifest.dataset_id}",
        generated_at=datetime.now(UTC))
    passport = with_hash(passport)
    store.put(passport)
    return passport


def reopen(store, passport_id: str, revision: int | None = None) -> EvidencePassport:
    """Load a Passport and prove every reference it makes still matches the store (I01 acceptance)."""
    candidates = [p for p in store.all(EvidencePassport, limit=None) if p.passport_id == passport_id]
    if not candidates:
        from traderos.contracts import DataError
        raise DataError(f"no passport {passport_id!r}", code=ErrorCode.MISSING_DATA)
    p = max(candidates, key=lambda x: x.revision) if revision is None else \
        next((x for x in candidates if x.revision == revision), None)
    if p is None:
        from traderos.contracts import DataError
        raise DataError(f"{passport_id} has no revision {revision}", code=ErrorCode.MISSING_DATA)
    verify_hash(p)
    version = store.get(StrategyVersion, p.strategy_version_id)
    if version.semantic_hash != p.strategy_semantic_hash:
        raise IntegrityError(f"{p.passport_id}: the strategy no longer matches")
    for eid, h in zip(p.experiment_ids, p.experiment_spec_hashes, strict=True):
        if record_hash(store.get(ExperimentSpec, eid)) != h:
            raise IntegrityError(f"{p.passport_id}: experiment {eid} no longer matches")
    for did, h in zip(p.dataset_ids, p.dataset_hashes, strict=True):
        try:                                     # the same dataset recorded elsewhere is the same (ADR-013)
            store.dataset_for(did, h)
        except IntegrityError:
            raise IntegrityError(f"{p.passport_id}: dataset {did} no longer matches") from None
    for tid in p.trial_ids:
        store.get(Trial, tid)
    for cid in p.challenge_result_ids:
        verify_hash(store.get(ChallengeResult, cid))
    if p.revision > 1:
        prev = next((x for x in candidates if x.revision == p.revision - 1), None)
        if prev is None or prev.canonical_hash != p.previous_revision_hash:
            raise IntegrityError(f"{p.passport_id}: revision {p.revision} does not chain to {p.revision - 1}")
    return p


def _v(m) -> str:
    if m is None:
        return "-"
    if m.value is None:
        return f"n/a ({m.unavailable_reason})"
    if m.unit == "fraction":
        return f"{m.value * 100:.2f}%"
    return f"{m.value}"


def render(store, p: EvidencePassport) -> str:
    """The Passport in Markdown, for a person. Every number comes from the stored record."""
    from traderos.strategies.rulebook import render as render_rules
    challenges = [store.get(ChallengeResult, cid) for cid in p.challenge_result_ids]
    lines = [f"# Evidence Passport {p.passport_id}, revision {p.revision}", "",
             f"**Research verdict: {p.state.value.replace('_', ' ')}.** {DISCLAIMER}", "",
             "| Dimension | Status |", "|---|---|",
             f"| Data quality and rights | {p.uncertainty.data_quality} |",
             f"| Test execution | {p.uncertainty.test_execution} |",
             f"| Strategy merit | {p.uncertainty.strategy_merit.value} |",
             f"| Operational freshness | {p.uncertainty.operational_freshness} |", "",
             "## The rules", "", "```", render_rules(p.rules), "```", "",
             "## Results (after costs)", "",
             "| Window | Net return | Trades | Max drawdown | Sharpe | Daily excess over cash, 95% interval |",
             "|---|---|---|---|---|---|"]
    for window in ("development", "validation", "holdout", "in_sample"):
        get = p.metrics.get
        if get(f"{window}.net_return") is None:
            continue
        lo, hi = get(f"{window}.excess_mean_daily_ci95_low"), get(f"{window}.excess_mean_daily_ci95_high")
        interval = (f"{lo.value} to {hi.value} per day" if lo and hi and lo.value is not None and hi.value is not None
                    else "n/a")
        lines.append(f"| {window} | {_v(get(f'{window}.net_return'))} | {_v(get(f'{window}.trades'))} | "
                     f"{_v(get(f'{window}.max_drawdown'))} | {_v(get(f'{window}.sharpe'))} | {interval} |")
    lines += ["", "The matched-baseline test (random-entry controls) is FS08 below.", "",
              "## Baselines (in-sample, same costs)", ""]
    for name, ms in p.baselines.items():
        lines.append(f"- **{name.replace('_', ' ')}**: " + ", ".join(f"{k.replace('_', ' ')} {_v(v)}"
                                                                      for k, v in ms.items()))
    lines += ["", "## Falsification challenges (fs/1)", "", "| Id | Outcome | Finding |", "|---|---|---|"]
    lines += [f"| {c.challenge_id} | {c.outcome.value} | {c.finding.replace('|', '/')} |" for c in challenges]
    lines += ["", "## Limitations", ""]
    lines += [f"- `{lim.code}`: {lim.detail}" for lim in p.limitations] or ["- none recorded"]
    lines += ["", "## Assumptions", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(p.assumptions.items())]
    lines += ["", "## Reproduce", "", f"`{p.reproduce_command}`", "",
              f"Strategy {p.strategy_version_id} (rules hash {p.strategy_semantic_hash[:12]}), experiment "
              f"{', '.join(p.experiment_ids)}, dataset {', '.join(p.dataset_ids)}. "
              f"Passport canonical hash `{p.canonical_hash}`."
              + (f" Previous revision `{p.previous_revision_hash}`." if p.previous_revision_hash else "")]
    return "\n".join(lines) + "\n"


__all__ = ["DISCLAIMER", "build", "latest", "render", "reopen"]
