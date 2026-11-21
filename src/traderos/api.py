"""The public API (spec 04.4), frozen as signatures in contracts v1. Exact contract: docs/contracts-v1.md section 6.

Each function names its owner. Until a part lands, its function raises CapabilityDisabled, so a caller (the CLI,
the /trader skill, a test) always gets a typed refusal instead of a fabricated result. None of these functions can
place an order or send telemetry (spec 04.4).

Parts land by replacing the body of their own functions here with a call into their package; the signature and the
returned record type do not change without an ADR.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from traderos.contracts import (
    CapabilityDisabled,
    DatasetManifest,
    Direction,
    EntryAssessment,
    EvidencePassport,
    ExitPlan,
    HypotheticalRiskAssessment,
    ImportReport,
    MarketContext,
    OwnerAction,
    PortableBundleManifest,
    ResearchMemoryEvent,
    RiskPlan,
    ScanCriteria,
    ScanResult,
    StrategyDraft,
    StrategyVersion,
    Trial,
    ValidationError,
    WorkflowResult,
)
from traderos.contracts.intel import AlertEvent, AlertRule
from traderos.contracts.market import Interval
from traderos.contracts.research import ChallengeResult
from traderos.store import Store


def _not_yet(name: str, owner: str) -> CapabilityDisabled:
    return CapabilityDisabled(f"{name} is not built yet ({owner})", details={"function": name, "owner": owner})


# ---- part A: data, lab (OSDev4) ----------------------------------------------------------------------------------
def load_dataset(source: str, *, name: str | None = None, rights_label: str | None = None,
                 interval: Interval = Interval.D1, store: Store | None = None) -> DatasetManifest:
    """Import a CSV path or "tutorial" into the local store; validated, hashed, quality-gated (spec 06)."""
    import pathlib

    from traderos import data
    from traderos.data import tutorial
    store = (store or Store()).init()
    if source == "tutorial":
        return tutorial.load(store)
    path = pathlib.Path(source)
    opts = data.ImportOptions(name=name or path.name, interval=interval,
                              rights_label=rights_label or "user-provided")
    return data.import_into(store, data.read_file(path), opts)


def check_dataset(dataset_id: str, *, store: Store | None = None) -> DatasetManifest:
    return (store or Store()).bar_reader().manifest(dataset_id)


def parse_strategy(draft: str | Mapping[str, Any], *, author_type: str = "human") -> StrategyDraft:
    """strategy/1 JSON text or a mapping -> a validated draft. Never evaluates code (spec 07.2)."""
    from traderos.contracts import AuthorType
    from traderos.strategies import rulebook
    return rulebook.draft(draft, author_type=AuthorType(author_type))


def approve_strategy_version(draft: StrategyDraft, owner_action: OwnerAction, *,
                             store: Store | None = None) -> StrategyVersion:
    from traderos.strategies import rulebook
    store = (store or Store()).init()
    previous = store.get(StrategyVersion, draft.parent_version_id) if draft.parent_version_id else None
    store.put(draft)
    version = rulebook.approve(draft, owner_action, previous=previous)
    store.put(version)
    return version


def register_experiment(version_id: str, dataset_id: str, *, seed: int = 7, store: Store | None = None) -> str:
    """Freeze and store the spec before any calculation; returns experiment_id. (The frozen v1 signature took a
    prepared ExperimentSpec; the lab builds the spec itself from a version and a dataset, so nothing a caller
    passes can skip the freeze.)"""
    from traderos.research import Lab
    store = (store or Store()).init()
    return Lab(store).register(store.get(StrategyVersion, version_id), dataset_id, seed=seed).experiment_id


def _run(experiment_id: str, store: Store | None):
    from traderos.research import Lab
    return Lab((store or Store()).init()).run(experiment_id)


def run_experiment(experiment_id: str, *, store: Store | None = None) -> tuple[Trial, ...]:
    return tuple(_run(experiment_id, store).trials)


def falsify_experiment(experiment_id: str, challenge_policy: str = "fs/1", *,
                       store: Store | None = None) -> tuple[ChallengeResult, ...]:
    if challenge_policy != "fs/1":
        raise CapabilityDisabled(f"challenge policy {challenge_policy!r} is not in this build")
    return tuple(_run(experiment_id, store).challenges)


def build_passport(experiment_id: str, *, store: Store | None = None) -> EvidencePassport:
    """The newest Passport revision that records this experiment (the lab writes one per completed run)."""
    from traderos.contracts import IncompleteEvidence
    store = (store or Store()).init()
    found = [p for p in store.all(EvidencePassport, limit=None) if experiment_id in p.experiment_ids]
    if not found:
        raise IncompleteEvidence(f"{experiment_id} has not completed a run; run it first")
    return max(found, key=lambda p: (p.passport_id, p.revision))


def search_memory(query: str, *, kinds: tuple[str, ...] = (), limit: int = 20,
                  store: Store | None = None) -> tuple[ResearchMemoryEvent, ...]:
    return tuple((store or Store()).init().search_memory(query, kinds=kinds, limit=limit))


def export_portable_bundle(refs: tuple[str, ...], *, profile: str = "full_private", out: str | None = None,
                           now: datetime | None = None, store=None) -> PortableBundleManifest:
    """I07: write a .traderos bundle of these Passports (none: everything this profile's rights allow). It holds no
    time unless `now` is given, so the same profile always exports the same bytes."""
    import pathlib

    from traderos import transfer
    transfer.require_released("export")
    from traderos.store import Store
    manifest, _ = transfer.export(store or Store().init(), tuple(refs), profile=profile,
                                  out=pathlib.Path(out) if out else None, now=now)
    return manifest


def import_portable_bundle(path: str, *, mode: str = "preview", store=None) -> ImportReport:
    """I07: preview (writes nothing) or apply (adds what is new; refuses any conflict or hash failure)."""
    import pathlib

    from traderos import transfer
    transfer.require_released("import")
    from traderos.store import Store
    if mode not in ("preview", "apply"):
        raise ValidationError("mode is preview or apply")
    return transfer.import_bundle(store or Store(), pathlib.Path(path), apply=mode == "apply")


def run_workflow(intent: str, context: Mapping[str, Any]) -> WorkflowResult:
    raise _not_yet("run_workflow", "integration, OSDev4")


# ---- part B: market intelligence (OSDev6) ------------------------------------------------------------------------
# Each reads the local store (or the one given) through its as-of BarReader, and the exchange calendar the dataset
# names. The work is in traderos.intelligence, which takes both as arguments (contracts v1 section 6).

def _market(dataset_id: str, store: Store | None):
    from traderos import clock
    reader = (store or Store()).bar_reader()
    return reader, clock.calendar(reader.manifest(dataset_id).calendar_id)


def scan_market(dataset_id: str, criteria: ScanCriteria, *, as_of: datetime | None = None,
                store: Store | None = None) -> ScanResult:
    """S01. `as_of` None means the newest bar available in the dataset."""
    from traderos import intelligence
    return intelligence.scan(*_market(dataset_id, store), dataset_id, criteria, as_of=as_of)


def scan_setups(dataset_id: str, setups: str, *, symbols: tuple[str, ...] | None = None,
                as_of: datetime | None = None, min_price: Decimal | None = None, min_volume: Decimal | None = None,
                or_minutes: int = 15, until: str | None = "15:30", store: Store | None = None) -> ScanResult:
    """S01, the upstream setups (ADR-008): "daily" (breakout_20d, bull_flag, reclaim_ma20, oversold, oversold_bounce on
    daily bars) or "orb" (the opening-range breakout on 5-minute bars, expired after `until` New York)."""
    from traderos.intelligence import setups as setup_scan
    return setup_scan.scan_setups(*_market(dataset_id, store), dataset_id, setups, symbols=symbols, as_of=as_of,
                                  min_price=min_price, min_volume=min_volume, or_minutes=or_minutes, until=until)


def market_context(dataset_id: str, *, as_of: datetime | None = None, store: Store | None = None) -> MarketContext:
    """S03 Market Pulse."""
    from traderos import intelligence
    return intelligence.market_context(*_market(dataset_id, store), dataset_id, as_of=as_of)


def assess_entry(dataset_id: str, symbol: str, *, as_of: datetime | None = None,
                 setup_ref: str | None = None, setup: str | None = None, level: Decimal | None = None,
                 or_minutes: int = 15, min_volume_x: Decimal | None = None,
                 store: Store | None = None) -> EntryAssessment:
    """S13 Entry Timer. Research only. With setup "orb" or "level", the upstream trigger (ADR-008)."""
    from traderos import intelligence
    return intelligence.assess_entry(*_market(dataset_id, store), dataset_id, symbol, as_of=as_of,
                                     setup_ref=setup_ref, setup=setup, level=level, or_minutes=or_minutes,
                                     min_volume_x=min_volume_x)


def plan_exit(dataset_id: str, symbol: str, *, direction: Direction, entry_price: Decimal,
              stop_price: Decimal | None = None, target_price: Decimal | None = None,
              as_of: datetime | None = None, stop_rule: str | None = None, target_rule: str | None = None,
              setup: str = "orb", or_minutes: int = 15, or_high: Decimal | None = None,
              or_low: Decimal | None = None, flat_by: str | None = None, entry_at: datetime | None = None,
              store: Store | None = None) -> ExitPlan:
    """S14 Exit Planner. Hypothetical scenarios only. With a stop or target rule, the upstream rules (ADR-008)."""
    from traderos import intelligence
    return intelligence.plan_exit(*_market(dataset_id, store), dataset_id, symbol, direction=direction,
                                  entry_price=entry_price, stop_price=stop_price, target_price=target_price,
                                  as_of=as_of, stop_rule=stop_rule, target_rule=target_rule, setup=setup,
                                  or_minutes=or_minutes, or_high=or_high, or_low=or_low, flat_by=flat_by,
                                  entry_at=entry_at)


def evaluate_alert(dataset_id: str, rule: AlertRule, *, at: datetime,
                   previous_at: datetime | None = None, store: Store | None = None) -> AlertEvent:
    """S18 Price Alerts: one evaluation of one rule at one instant. `previous_at` is the last evaluation, so a
    window that went unevaluated is reported as missed rather than hidden."""
    from traderos import intelligence
    return intelligence.evaluate_alert(*_market(dataset_id, store), dataset_id, rule, at=at, previous_at=previous_at)


def ring_alerts(rules: list[dict], *, at: datetime, state: dict | None = None, dataset_id: str | None = None,
                store: Store | None = None) -> tuple[list[AlertEvent], dict]:
    """S18, the upstream rule types (ADR-008): one alert-event/1 per rule, and the new state. Price rules read
    `dataset_id` by instrument_id; the opening bell and time rules need no dataset. triggered None means the rule
    could not be checked, which is never reported as nothing hitting."""
    from traderos import clock
    from traderos.intelligence import alert_rules
    from traderos.intelligence._common import aware
    if dataset_id is not None:
        reader, calendar = _market(dataset_id, store)
    else:
        reader, calendar = None, clock.calendar("XNYS")
    return alert_rules.check(rules, at=aware(at, "at"), state=state, calendar=calendar, reader=reader,
                             dataset_id=dataset_id)


# ---- part C: decisions (OSDev4) ----------------------------------------------------------------------------------
def assess_hypothetical_plan(plan: RiskPlan, policy: Mapping[str, Any], *, account: Mapping[str, Any] | None = None,
                             prices: Mapping[str, Any] | None = None, now: datetime | None = None,
                             stress_gap: str = "0.05") -> HypotheticalRiskAssessment:
    """S15/S16: the upstream sizing and check under the person's policy, and the four risk measures. No broker, ever.
    `policy` states every limit (traderos.risk.policy: none has a default); without `account` (settled cash,
    reservations, positions and the log of recent orders) exposure is UNKNOWN and no ticket can be drafted. The
    signature gained `policy` and the keyword arguments in contracts v1.1 (ADR-009; spec 04.4 names `policy`)."""
    from traderos import risk
    from traderos.risk import inputs
    return risk.assess(plan, dict(policy), account=inputs.account(dict(account)) if account is not None else None,
                       prices=inputs.prices(dict(prices)) if prices is not None else None, now=now,
                       stress_gap=Decimal(stress_gap))
