"""The research lifecycle (spec 08, S10, I01-I04): register, run, challenge, decide, record.

    rules -> approved StrategyVersion -> ExperimentSpec frozen (hash stored BEFORE any calculation)
          -> trials: development, validation, holdout (every attempt recorded, failures included)
          -> challenges fs/1 -> verdict/1 -> Evidence Passport -> research memory

THE HOLDOUT (spec 08.4): the last window is sealed until the experiment runs with its rules and costs frozen. Once
an experiment in a research family has opened a holdout, a later experiment of that family whose holdout overlaps
it is HOLDOUT_COMPROMISED: it can be run and reported, never qualify. An exact rerun of the same frozen experiment is
a `verification_replay` and makes no new claim (QT07).

VERDICT verdict/1 (ADR-005, proposed; its numbers need the quant reviewer's and the founder's sign-off before any
user-facing qualification claim):
  * rejected: the rules leak the future (FS09 fails), or the in-sample edge over the matched baseline is robustly
    negative (its whole 95% interval is below zero);
  * candidate_for_paper: FS01, FS02, FS04, FS08, FS09 and FS10 all PASS; at least 100 in-sample trades; the holdout
    is not compromised; the data is not synthetic. A research state, never permission to trade;
  * insufficient_evidence: everything else, including every result on tutorial data.
"""
from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from traderos.backtest import CostModel, SimResult, simulate
from traderos.clock import XNYS
from traderos.contracts import (
    AccountAssumptions,
    ActorRole,
    Bar,
    ChallengeOutcome,
    ChallengeResult,
    DatasetManifest,
    ErrorCode,
    ExperimentOutcome,
    ExperimentSpec,
    IntegrityError,
    MetricValue,
    ResearchError,
    ResearchMemoryEvent,
    SplitName,
    SplitWindow,
    StrategyVersion,
    Trial,
    TrialStatus,
    canonical_bytes,
    record_hash,
    sha256_bytes,
    verify_hash,
    with_hash,
)
from traderos.research import statistics as stats
from traderos.strategies.rulebook import compile_rules

VERDICT_POLICY = "verdict/1"
MIN_TRADES = 100                                 # ADR-005: founder sign-off 2026-11-20 (OWNER, 15:59)
DEFAULT_COSTS = {"spread_bps": "2", "slippage_bps": "1", "fee_per_share": "0", "fee_bps": "0"}


@dataclass
class RunOutcome:
    spec: ExperimentSpec
    trials: list[Trial]
    challenges: list[ChallengeResult]
    verdict: ExperimentOutcome
    reasons: list[str]
    runs: dict[str, SimResult]
    summaries: dict[str, stats.Summary]
    baselines: dict[str, dict[str, MetricValue]]
    holdout_compromised: bool
    verification_replay: bool
    passport: object = None                      # the EvidencePassport revision this run produced


def _symbol_bars(reader, manifest: DatasetManifest, universe, end_close: datetime) -> dict[str, list[Bar]]:
    by_symbol = {i.symbol: i.instrument_id for i in manifest.instruments}
    missing = [s for s in universe if s not in by_symbol]
    if missing:
        raise ResearchError(f"{', '.join(missing)} not in dataset {manifest.dataset_id}", code=ErrorCode.MISSING_DATA)
    return {s: list(reader.bars(manifest.dataset_id, by_symbol[s], as_of=end_close)) for s in universe}


def default_splits(manifest: DatasetManifest, warmup_bars: int, cal: XNYS) -> tuple[SplitWindow, ...]:
    """Chronological development / validation / holdout over the sessions after warm-up: 60/20/20 by session count.
    An example proportion only (spec 08.4); a person may set their own windows."""
    first = min(c.first_bar_end for c in manifest.coverage if c.first_bar_end).date()
    last = max(c.last_bar_end for c in manifest.coverage if c.last_bar_end).date()
    sessions = cal.sessions(max(first, cal.coverage[0]), min(last, cal.coverage[1]))
    usable = sessions[warmup_bars:]
    if len(usable) < 3 * stats.MIN_OBSERVATIONS:
        raise ResearchError(f"after {warmup_bars} sessions of warm-up only {len(usable)} sessions remain; at least "
                            f"{3 * stats.MIN_OBSERVATIONS} are needed for three windows",
                            code=ErrorCode.INSUFFICIENT_EVIDENCE)
    a, b = int(len(usable) * 0.6), int(len(usable) * 0.8)
    return (SplitWindow(name=SplitName.DEVELOPMENT, start=usable[0], end=usable[a - 1]),
            SplitWindow(name=SplitName.VALIDATION, start=usable[a], end=usable[b - 1]),
            SplitWindow(name=SplitName.HOLDOUT, start=usable[b], end=usable[-1]))



def _same_interval(book, manifest) -> None:
    """A strategy's bars are the dataset's bars: sma(5) on 5-minute bars is 25 minutes, not five days."""
    if manifest.interval is not book.frequency:
        raise ResearchError(f"the strategy is written for {book.frequency.value} bars but dataset "
                            f"{manifest.dataset_id} holds {manifest.interval.value} bars; nothing was run",
                            code=ErrorCode.OUTSIDE_CAPABILITY)

class Lab:
    """The research lab over one local store."""

    def __init__(self, store, calendar: XNYS | None = None) -> None:
        self.store = store
        self.cal = calendar or XNYS()

    # ---- register ----------------------------------------------------------------------------------------------
    def register(self, version: StrategyVersion, dataset_id: str, *, created_by: str = "local", seed: int = 7,
                 starting_cash: str = "100000", costs: dict[str, str] | None = None,
                 splits: tuple[SplitWindow, ...] | None = None, now: datetime | None = None) -> ExperimentSpec:
        """register_experiment (spec 08.3): freeze everything before any calculation and store its hash."""
        verify_hash(version)
        manifest = self.store.bar_reader().manifest(dataset_id)
        book = compile_rules(version.rules)
        _same_interval(book, manifest)
        ids = {i.symbol: i.instrument_id for i in manifest.instruments}
        missing = [s for s in book.universe if s not in ids]
        if missing:
            raise ResearchError(f"the strategy trades {', '.join(missing)}, which dataset {dataset_id} does not have",
                                code=ErrorCode.MISSING_DATA)
        splits = splits or default_splits(manifest, book.history_needed(), self.cal)
        cost_model = CostModel.from_params(costs or DEFAULT_COSTS)
        body = dict(
            research_family_id=f"RF-{version.strategy_id}", hypothesis=version.hypothesis,
            strategy_version_id=version.version_id, strategy_semantic_hash=version.semantic_hash,
            dataset_id=dataset_id, dataset_hash=manifest.canonical_hash, universe=tuple(ids[s] for s in book.universe),
            universe_as_of=manifest.universe_as_of, interval=manifest.interval, calendar_id=self.cal.calendar_id,
            calendar_version=self.cal.version, splits=splits, embargo_bars=0,
            account=AccountAssumptions(starting_cash=Decimal(starting_cash)), fill_model_id="next_open/1",
            cost_model_id=cost_model.model_id, cost_params=cost_model.params(), search_space={}, trial_budget=3,
            selection_rule="the approved rules only; no parameter search", test_family=f"RF-{version.strategy_id}",
            seed=seed, challenge_policy_id="fs/1", verdict_policy_id=VERDICT_POLICY, created_by=created_by)
        identity = sha256_bytes(canonical_bytes({k: v for k, v in body.items()}))[:20]
        spec = with_hash(ExperimentSpec(experiment_id=f"EX-{identity}", created_at=now or datetime.now(UTC), **body))
        existing = self.store.find(ExperimentSpec, spec.experiment_id)
        if existing is not None:
            return existing                       # the same frozen experiment: a replay will be labelled as one
        self.store.put(spec)
        self._remember("experiment.registered", (spec.experiment_id, version.version_id, dataset_id),
                       f"Registered {spec.experiment_id}: {version.name} on {manifest.name}", spec)
        return spec

    # ---- run ---------------------------------------------------------------------------------------------------
    def run(self, experiment_id: str) -> RunOutcome:
        spec = self.store.get(ExperimentSpec, experiment_id)
        verify_hash(spec)
        version = self.store.get(StrategyVersion, spec.strategy_version_id)
        if version.semantic_hash != spec.strategy_semantic_hash:
            raise ResearchError("the strategy no longer matches the frozen experiment", code=ErrorCode.NONREPRODUCIBLE)
        reader = self.store.bar_reader()
        try:                                     # the same dataset recorded elsewhere is the same (ADR-013)
            manifest = self.store.dataset_for(spec.dataset_id, spec.dataset_hash)
        except IntegrityError:
            raise ResearchError("the dataset no longer matches the frozen experiment",
                                code=ErrorCode.INTEGRITY_MISMATCH) from None
        book = compile_rules(version.rules)
        _same_interval(book, manifest)                       # again: a spec frozen before this check existed
        windows = {s.name: (s.start, s.end) for s in spec.splits}
        last = max(s.end for s in spec.splits)
        end_close = self.cal.open_close(last if self.cal.is_session(last) else self.cal.previous_session(last))[1]
        bars = _symbol_bars(reader, manifest, book.universe, end_close)
        costs = CostModel.from_params(spec.cost_params)
        replay = self._completed_before(spec.experiment_id)
        compromised = self._holdout_compromised(spec)
        number = len([r for r in self._runs(spec.experiment_id) if r[1] == "completed"]) + 1
        suffix = "" if number == 1 else f"-r{number}"
        run_id = f"RUN-{spec.experiment_id}-{len(self._runs(spec.experiment_id)) + 1}"
        self._run_state(run_id, spec, "running")
        try:                                     # a run that cannot record its results fails; it is never left running
            outcome = self._run(spec, book, bars, windows, costs, manifest, replay, compromised, suffix)
            artifacts = self._store_artifacts(outcome)
            for t in outcome.trials:
                self.store.put(t)
            for c in outcome.challenges:
                self.store.put(c)
        except Exception as e:
            self._run_state(run_id, spec, "failed", error=f"{type(e).__name__}: {e}")
            raise
        self._run_state(run_id, spec, "completed", artifacts=artifacts)
        from traderos import passports
        outcome.passport = passports.build(self.store, outcome)
        self._remember("passport.revised", (outcome.passport.passport_id, spec.experiment_id),
                       f"{outcome.passport.passport_id} revision {outcome.passport.revision}: "
                       f"{outcome.passport.state.value}", outcome.passport, codes=(outcome.passport.state.value,))
        self._remember("experiment.completed", (spec.experiment_id, spec.strategy_version_id),
                       f"{spec.experiment_id} {'replayed' if replay else 'completed'}: {outcome.verdict.value}"
                       + (f" ({'; '.join(outcome.reasons[:3])})" if outcome.reasons else ""), spec,
                       codes=(outcome.verdict.value,))
        return outcome

    def _run(self, spec, book, bars, windows, costs, manifest, replay, compromised, suffix="") -> RunOutcome:
        from traderos import challenges as fs

        runs: dict[str, SimResult] = {}
        summaries: dict[str, stats.Summary] = {}
        trials: list[Trial] = []
        names = (SplitName.DEVELOPMENT, SplitName.VALIDATION, SplitName.HOLDOUT)
        for ordinal, name in enumerate(names, 1):
            if name not in windows:
                continue
            started = datetime.now(UTC)
            trial_id = f"TR-{spec.experiment_id}-{name.value}{suffix}"
            try:
                run = simulate(book, bars, start=windows[name][0], end=windows[name][1], account=spec.account,
                               costs=costs, run_id=trial_id)
                summary = stats.summarize(run, seed=spec.seed + ordinal)
                runs[name.value], summaries[name.value] = run, summary
                trials.append(Trial(
                    trial_id=trial_id, experiment_id=spec.experiment_id, ordinal=ordinal, parameters={},
                    split=name, status=TrialStatus.COMPLETED, metrics=summary.metrics, started_at=started,
                    ended_at=datetime.now(UTC), selected=True, selection_trace=spec.selection_rule,
                    verification_replay=replay))
            except Exception as e:               # a failed run is evidence of a failure, never a zero return
                code = getattr(e, "code", None)
                trials.append(Trial(trial_id=trial_id, experiment_id=spec.experiment_id, ordinal=ordinal,
                                    parameters={}, split=name, status=TrialStatus.FAILED,
                                    error_code=code.value if isinstance(code, ErrorCode) else "ERROR",
                                    started_at=started, ended_at=datetime.now(UTC), selection_trace=str(e)[:500],
                                    verification_replay=replay))
        if "development" not in runs or "validation" not in runs:
            raise ResearchError("the in-sample windows could not be simulated: " + "; ".join(
                t.selection_trace for t in trials if t.status is TrialStatus.FAILED), code=ErrorCode.NONREPRODUCIBLE)
        in_sample = (windows[SplitName.DEVELOPMENT][0], windows[SplitName.VALIDATION][1])
        main = simulate(book, bars, start=in_sample[0], end=in_sample[1], account=spec.account, costs=costs,
                        run_id=f"{spec.experiment_id}-in-sample")
        main_summary = stats.summarize(main, seed=spec.seed)
        ctx = fs.Context(book=book, bars=bars, in_sample=in_sample, holdout=windows.get(SplitName.HOLDOUT),
                         account=spec.account, costs=costs, seed=spec.seed, experiment_id=spec.experiment_id,
                         main=main, main_summary=main_summary, holdout_run=runs.get("holdout"),
                         holdout_compromised=compromised, trials_recorded=len(trials), trials_attempted=len(trials),
                         trial_budget=spec.trial_budget, id_suffix=suffix)
        challenges = fs.run_all(ctx)
        controls = fs.control_daily(fs.ensure_controls(ctx))
        baselines = self._baselines(book, bars, in_sample, spec, costs, controls, main)
        verdict, reasons = decide(challenges, main, manifest, compromised)
        if replay:
            drift = self._replay_drift(spec, trials, challenges)
            if drift:                             # QT07: a replay must reproduce the documented output
                verdict = ExperimentOutcome.INSUFFICIENT_EVIDENCE
                reasons = [f"NONREPRODUCIBLE: {d}" for d in drift] + reasons
        runs["in_sample"], summaries["in_sample"] = main, main_summary
        return RunOutcome(spec=spec, trials=trials, challenges=challenges, verdict=verdict, reasons=reasons,
                          runs=runs, summaries=summaries, baselines=baselines, holdout_compromised=compromised,
                          verification_replay=replay)

    def _baselines(self, book, bars, window, spec, costs, controls_daily, main,
                   controls: int = 8) -> dict[str, dict[str, MetricValue]]:
        """Cash, buy-and-hold of the universe (equal weight, same costs) and the random-entry controls."""
        from traderos.strategies.rulebook import compile_rules as _compile
        hold_doc = dict(book.document)
        hold_doc.update({"entry": {"above": ["close", "0"]}, "exit": {"below": ["close", "0"]}, "name": "buy-and-hold"})
        hold_doc.pop("hypothetical_stop", None)
        hold = simulate(_compile(hold_doc), bars, start=window[0], end=window[1], account=spec.account, costs=costs,
                        run_id=f"{spec.experiment_id}-hold")
        hold_summary = stats.summarize(hold, seed=spec.seed, bootstrap=False).metrics
        ctrl_return = Decimal(1)
        for d in controls_daily:
            ctrl_return *= Decimal(1) + Decimal(repr(d))
        return {
            "cash": {"net_return": MetricValue(value=Decimal(0), unit="fraction")},
            "buy_and_hold": {k: hold_summary[k] for k in ("net_return", "max_drawdown", "sharpe", "exposure")},
            "random_entry_controls": {
                "net_return": MetricValue(value=(ctrl_return - 1).quantize(Decimal("0.000001")), unit="fraction"),
                "controls": MetricValue(value=Decimal(controls), unit="runs")},
        }

    def _replay_drift(self, spec, trials, challenges) -> list[str]:
        """What differs between this replay and the first run of the same frozen experiment (QT07)."""
        drift = []
        for t in trials:
            first = self.store.find(Trial, f"TR-{spec.experiment_id}-{t.split.value}")
            if first is None or first.status is not t.status or first.metrics != t.metrics:
                drift.append(f"trial {t.split.value} differs from the first run")
        for c in challenges:
            first = self.store.find(ChallengeResult, f"CR-{spec.experiment_id}-{c.challenge_id}")
            if first is None or first.outcome is not c.outcome or first.metrics != c.metrics:
                drift.append(f"{c.challenge_id} differs from the first run")
        return drift

    # ---- bookkeeping -------------------------------------------------------------------------------------------
    def _runs(self, experiment_id: str) -> list[tuple]:
        with self.store.connect() as db:
            return db.execute("SELECT run_id, status FROM runs WHERE experiment_id = ?", (experiment_id,)).fetchall()

    def _completed_before(self, experiment_id: str) -> bool:
        return any(status == "completed" for _, status in self._runs(experiment_id))

    def _holdout_compromised(self, spec: ExperimentSpec) -> bool:
        """An EARLIER experiment of the same family already opened an overlapping holdout (spec 08.4, QT02)."""
        mine = next((s for s in spec.splits if s.name is SplitName.HOLDOUT), None)
        if mine is None:
            return False
        for other in self.store.all(ExperimentSpec, limit=None):
            if other.experiment_id == spec.experiment_id or other.research_family_id != spec.research_family_id:
                continue
            if not self._completed_before(other.experiment_id) or other.dataset_id != spec.dataset_id:
                continue
            theirs = next((s for s in other.splits if s.name is SplitName.HOLDOUT), None)
            if theirs and theirs.start <= mine.end and mine.start <= theirs.end:
                return True
        return self._holdout_seen_by_timemachine(spec, mine)

    def _holdout_seen_by_timemachine(self, spec: ExperimentSpec, mine) -> bool:
        """A Time Machine run of a strategy in this family, on this dataset, whose window reached into this holdout has
        shown its results there: the holdout is no longer unseen (spec 08.4, QT02)."""
        from traderos.contracts import TimeMachineReport
        for report in self.store.all(TimeMachineReport, limit=None):
            if report.strategy_version_id is None or report.dataset_id != spec.dataset_id:
                continue
            if not (report.window_start <= mine.end and mine.start <= report.window_end):
                continue
            version = self.store.find(StrategyVersion, report.strategy_version_id)
            if version is not None and f"RF-{version.strategy_id}" == spec.research_family_id:
                return True
        return False

    def _run_state(self, run_id: str, spec: ExperimentSpec, status: str, *, artifacts: list[str] | None = None,
                   error: str | None = None) -> None:
        from traderos.store import us
        now = us(datetime.now(UTC))
        with self.store.transaction() as db:
            row = db.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO runs(run_id, experiment_id, spec_hash, status, started_us) VALUES (?,?,?,?,?)",
                           (run_id, spec.experiment_id, spec.canonical_hash, status, now))
            else:
                db.execute("UPDATE runs SET status = ?, ended_us = ?, artifacts = ?, error = ? WHERE run_id = ?",
                           (status, now, json.dumps(artifacts or []), error, run_id))

    def _store_artifacts(self, outcome: RunOutcome) -> list[str]:
        """Trades and equity curves as immutable files, written before the run is marked completed (spec 11.2)."""
        digests = []
        for name, run in sorted(outcome.runs.items()):
            payload = {"experiment_id": outcome.spec.experiment_id, "window": name,
                       "trades": [t.as_dict() for t in run.trades],
                       "equity": [[d.isoformat(), v] for d, v in run.equity],
                       "limitations": run.limitations, "skipped_entries": run.skipped_entries}
            digests.append(self.store.put_artifact(canonical_bytes(payload), "application/json"))
        return digests

    def _remember(self, kind: str, subjects: tuple[str, ...], summary: str, payload, codes: tuple[str, ...] = ()):
        event = ResearchMemoryEvent(
            event_id=f"ME-{kind}-{sha256_bytes(canonical_bytes([kind, list(subjects), summary]))[:16]}",
            actor_role=ActorRole.SYSTEM, kind=kind, subject_refs=subjects, outcome_codes=codes, summary=summary,
            payload_hash=record_hash(payload), previous_event_hash=self.store.last_memory_hash(),
            at=datetime.now(UTC))
        with contextlib.suppress(IntegrityError):  # the same event again (a replay): the first one stands
            self.store.remember(with_hash(event))


def decide(challenges: list[ChallengeResult], main: SimResult, manifest: DatasetManifest,
           compromised: bool) -> tuple[ExperimentOutcome, list[str]]:
    """verdict/1 (ADR-005). Returns the outcome and every reason it is not better."""
    by = {c.challenge_id: c for c in challenges}
    if by.get("FS09") and by["FS09"].outcome is ChallengeOutcome.FAIL:
        return ExperimentOutcome.REJECTED, ["LOOKAHEAD_RISK: later data changed earlier decisions (FS09)"]
    fs08 = by.get("FS08")
    hi = fs08.metrics.get("ci95_high") if fs08 else None
    if hi is not None and hi.value is not None and hi.value < 0:
        return ExperimentOutcome.REJECTED, ["the in-sample edge over the matched baseline is robustly negative (FS08)"]
    reasons = []
    for cid in ("FS01", "FS02", "FS04", "FS08", "FS09", "FS10"):
        c = by.get(cid)
        if c is None or c.outcome is not ChallengeOutcome.PASS:
            reasons.append(f"{cid} {c.outcome.value if c else 'missing'}")
    if len(main.trades) < MIN_TRADES:
        reasons.append(f"{len(main.trades)} in-sample trades; {MIN_TRADES} needed (ADR-005: the founder signed off "
                       "this minimum; the independent quant review is still pending)")
    if compromised:
        reasons.append("HOLDOUT_COMPROMISED")
    if manifest.synthetic:
        reasons.append("SYNTHETIC_DATA: tutorial data is never evidence")
    return (ExperimentOutcome.INSUFFICIENT_EVIDENCE if reasons else ExperimentOutcome.CANDIDATE_FOR_PAPER), reasons


__all__ = ["DEFAULT_COSTS", "MIN_TRADES", "VERDICT_POLICY", "Lab", "RunOutcome", "decide", "default_splits"]
