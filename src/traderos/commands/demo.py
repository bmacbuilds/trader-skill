"""`traderos demo`: the offline tutorial, end to end (spec 13.2, 18.1, I06).

market context -> a sample strategy -> its rules shown -> approval -> preregistered experiment -> backtest with
costs -> ten falsification challenges -> Evidence Passport -> reopened and verified from the store -> memory.

Zero network. Everything runs on the labelled synthetic tutorial dataset, so every result says TUTORIAL ONLY and the
verdict can never be better than insufficient_evidence. Steps that are not in this build are reported as
unavailable, never skipped silently, and the whole run is one WorkflowResult (I06).

It runs any number of times in one store. A second run reuses the sample's stored draft and approved version (records
are never overwritten) and runs the same frozen experiment again, which the lab records as a replay: a new Passport
revision.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime

from traderos.cli import Output
from traderos.contracts import (
    Completeness,
    IntegrityError,
    OwnerAction,
    StepStatus,
    StrategyDraft,
    StrategyVersion,
    TraderOSError,
    WorkflowResult,
    WorkflowStep,
    with_hash,
)
from traderos.store import Store

DEMO_RULES = {
    "schema_version": "strategy/1",
    "name": "tutorial-pullback",
    "hypothesis": "In an uptrend, a sharp two-day pullback tends to recover within two weeks.",
    "universe": ["TUTMKT", "TUTTEC", "TUTSML"],
    "frequency": "1d",
    "direction": "long",
    "entry": {"all": [{"above": ["close", {"sma": 200}]}, {"below": [{"rsi": 2}, 10]}]},
    "exit": {"any": [{"above": ["close", {"sma": 5}]}, {"bars_held": 10}]},
    "hypothetical_stop": {"atr_multiple": "2.5", "atr_period": 14},
    "notes": "The bundled tutorial example. Not a recommendation.",
}


def register(sub) -> None:
    p = sub.add_parser("demo", help="the offline tutorial, end to end")
    p.set_defaults(run=demo)


def _say(lines: list[str], text: str) -> None:
    lines.append(text)


def demo(args: argparse.Namespace) -> Output:
    from traderos import passports
    from traderos.data import tutorial
    from traderos.research import Lab
    from traderos.strategies import rulebook

    started = datetime.now(UTC)
    store = Store(args.home).init()
    lines: list[str] = ["TraderOS demo: offline, on SYNTHETIC tutorial data. Nothing here is market evidence, and "
                        "nothing places an order.", ""]
    steps: list[WorkflowStep] = []
    manifest = tutorial.load(store)
    _say(lines, f"1. Data: {manifest.name} ({manifest.dataset_id}), {manifest.row_count} bars, labelled "
         f"{', '.join(lim.code for lim in manifest.limitations)}.")
    steps.append(WorkflowStep(name="data", status=StepStatus.COMPLETED, output_refs=(manifest.dataset_id,),
                              input_hashes=(manifest.canonical_hash,)))
    try:
        from traderos import api
        ctx = api.market_context(manifest.dataset_id, store=store)        # this run's store, not the default
        _say(lines, f"2. Market Pulse as of {ctx.as_of:%Y-%m-%d}: regime {ctx.regime.label}.")
        steps.append(WorkflowStep(name="market_pulse", status=StepStatus.COMPLETED, output_refs=(ctx.context_id,)))
    except TraderOSError as e:                   # a typed refusal (no data, stale): say so; a bug is a bug
        _say(lines, f"2. Market Pulse: unavailable in this build ({getattr(e, 'code', type(e).__name__)}).")
        steps.append(WorkflowStep(name="market_pulse", status=StepStatus.UNAVAILABLE,
                                  reason=f"{getattr(e, 'message', str(e))}"[:300]))
    d = rulebook.draft(DEMO_RULES)
    earlier = store.find(StrategyDraft, d.draft_id)                  # a second run: the same rules, already stored
    if earlier is not None:                      # find() verified its hash; the rules must also be the sample's own
        if earlier.semantic_hash != d.semantic_hash or earlier.rules != d.rules:
            raise IntegrityError(f"the stored demo draft {d.draft_id} no longer holds the demo's sample rules")
        d = earlier
    else:
        store.put(d)
    _say(lines, "3. A sample strategy, shown before anyone approves it:")
    lines += ["   " + x for x in rulebook.render(d.rules).splitlines()]
    steps.append(WorkflowStep(name="strategy_draft", status=StepStatus.COMPLETED, output_refs=(d.draft_id,),
                              input_hashes=(d.semantic_hash,)))
    v = rulebook.approve(d, OwnerAction(actor="local", method="demo_confirm", at=datetime.now(UTC),
                                        statement=f"demo approves its own sample rules {d.semantic_hash[:12]}"))
    approved = store.find(StrategyVersion, v.version_id)
    if approved is not None:
        if approved.semantic_hash != d.semantic_hash or approved.rules != d.rules:
            raise IntegrityError(f"the stored demo version {v.version_id} no longer holds the demo's sample rules")
        v = approved
    else:
        store.put(v)
    _say(lines, f"4. {'Approved' if approved is None else 'Already approved by an earlier demo run'} as "
         f"{v.version_id} (the demo approves its own sample; your strategies need your approval: "
         "`traderos strategy approve`).")
    steps.append(WorkflowStep(name="approval", status=StepStatus.COMPLETED, output_refs=(v.version_id,),
                              input_hashes=(d.semantic_hash,)))
    lab = Lab(store)
    spec = lab.register(v, manifest.dataset_id)
    _say(lines, f"5. Preregistered {spec.experiment_id} (spec hash {spec.canonical_hash[:12]}) before any "
         "calculation: " + ", ".join(f"{s.name.value} {s.start}..{s.end}" for s in spec.splits) + ".")
    steps.append(WorkflowStep(name="experiment", status=StepStatus.COMPLETED, output_refs=(spec.experiment_id,),
                              input_hashes=(v.semantic_hash, manifest.canonical_hash)))
    out = lab.run(spec.experiment_id)
    ins = out.summaries["in_sample"].metrics
    _say(lines, f"6. Backtest after costs, in-sample: net {ins['net_return'].value * 100:.2f}%, "
         f"{ins['trades'].value} trades, max drawdown {ins['max_drawdown'].value * 100:.2f}%.")
    _say(lines, "7. Falsification challenges:")
    lines += [f"   {c.challenge_id} {c.outcome.value:<26} {c.finding[:100]}" for c in out.challenges]
    steps.append(WorkflowStep(name="falsification", status=StepStatus.COMPLETED,
                              output_refs=tuple(c.result_id for c in out.challenges)))
    p = passports.reopen(Store(args.home), out.passport.passport_id)      # a fresh handle: as after a restart
    path = store.home / "exports" / f"{p.passport_id}-r{p.revision}.md"
    path.write_text(passports.render(store, p), encoding="utf-8")
    _say(lines, f"8. Verdict: {out.verdict.value.replace('_', ' ')}. Why not better: {'; '.join(out.reasons)}.")
    _say(lines, f"9. Evidence Passport {p.passport_id} revision {p.revision} reopened and verified from the store "
         f"(hash {p.canonical_hash[:12]}). Written to {path}.")
    steps.append(WorkflowStep(name="passport", status=StepStatus.COMPLETED, output_refs=(p.passport_id,),
                              input_hashes=(p.canonical_hash,)))
    found = store.search_memory("tutorial-pullback") or store.search_memory(spec.experiment_id)
    _say(lines, f"10. Research memory: {len(store.search_memory(''))} events; try `traderos memory search pullback`.")
    steps.append(WorkflowStep(name="memory", status=StepStatus.COMPLETED,
                              output_refs=tuple(e.event_id for e in found[:3])))
    partial = any(s.status is not StepStatus.COMPLETED for s in steps)
    wf = with_hash(WorkflowResult(workflow_id=f"WF-demo-{spec.experiment_id}-r{p.revision}",
                                  intent="offline tutorial: market to Passport", steps=tuple(steps),
                                  evidence_refs=(spec.experiment_id, p.passport_id), final_ref=p.passport_id,
                                  completeness=Completeness.PARTIAL if partial else Completeness.COMPLETE,
                                  limitations=tuple(manifest.limitations),   # SYNTHETIC_DATA: in --json too
                                  started_at=started, ended_at=datetime.now(UTC)))
    store.put(wf)
    lines += ["", f"Workflow {wf.workflow_id}: {wf.completeness.value}. Next: import your own data with "
              "`traderos data import <csv>`."]
    return Output(wf, "\n".join(lines))
