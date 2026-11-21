# Strategy Laboratory: draft, approve, research, Passport, memory

Needs S09 (Strategy Builder) and S10 (Backtester) usable for the lifecycle and the run, and S08 (Quant Analyst) for
`passport explain` and `passport check-claims`.

The flow: the person's idea → a safe typed draft → the person approves it → a preregistered experiment with costs,
baselines and ten falsification challenges → an Evidence Passport → research memory.

## 0. The offline demo

```bash
traderos demo
```

It runs the whole Strategy Laboratory offline on the synthetic tutorial dataset: market context, a sample strategy
shown before it is approved, a preregistered experiment, a backtest after costs, ten falsification challenges, an
Evidence Passport reopened from the store, and research memory. Zero network, no keys. Run it without `--json`:
the text prints every step, the verdict and the Passport id, and says SYNTHETIC_DATA. Its verdict is at best
`insufficient_evidence`: say so. The demo approves its own sample strategy; the person's own strategies need the
person's approval (step 2).

In a store where the demo has already run, a second run either says "Already approved by an earlier demo run" and
replays the same frozen experiment as a new Passport revision, or refuses with INTEGRITY_MISMATCH (exit 3) on a
build without that fix. Either way the first run's records are all still in the store: reopen them by id
(`passport show`, `memory search pullback`) rather than running it again.

## 1. From words to a draft

A strategy is data: a `strategy/1` JSON document, never code. Write it from the person's words, and ask whenever a
threshold is vague ("a pullback" means what RSI?):

```json
{"schema_version": "strategy/1", "name": "daily-etf-pullback",
 "hypothesis": "Short pullbacks inside an uptrend tend to revert within days.",
 "universe": ["TUTMKT", "TUTTEC", "TUTSML"], "frequency": "1d", "direction": "long",
 "entry": {"all": [{"above": ["close", {"sma": 200}]}, {"below": [{"rsi": 2}, 10]}]},
 "exit": {"any": [{"above": ["close", {"sma": 5}]}, {"bars_held": 10}]},
 "hypothetical_stop": {"atr_multiple": "2.5", "atr_period": 14}}
```

- Combinators `all` and `any`; comparisons `above` and `below` of two operands; an operand is a bar field
  (`close`), a feature (`{"sma": 200}`, `ema`, `rsi`, `atr`) or a number (an integer, or a decimal as a string).
  `{"bars_held": N}` is for exits only. Unknown keys and floats are refused.
- Long only and daily bars in this build. A short is refused: say so, do not rewrite it as something else.
- The universe names symbols in a dataset the person has: the tutorial's are TUTBND, TUTFIN, TUTGLD, TUTMKT,
  TUTSLV, TUTSML and TUTTEC (`traderos --json data list`). A symbol no dataset holds is refused at the run
  (MISSING_DATA).

```bash
traderos --json strategy validate my-strategy.json
traderos --json strategy draft my-strategy.json --author ai
traderos --json strategy show SD-...
```

`validate` saves nothing. `draft` saves a draft that is not approved (`--author ai` when you wrote it, `human`
when the person wrote the file). Show the person the rules exactly as the result renders them, and the rules hash.

## 2. The person approves it, never you

```bash
traderos strategy approve SD-... --confirm 44376b645159
```

That command is the person's act. Give it to them with the rules hash from the draft (`44376b645159` is the example
above's), and let them run it themselves: in
their own terminal, or in Claude Code by typing it after `!`. Never run it for them, even when asked to: an AI
cannot generate an approval. An approved version (`SV-...`) never changes; a changed rule is a new draft, and its
approval makes a new revision.

## 3. Run the research

```bash
traderos --json research run SV-... --dataset DS-...
traderos --json research run SV-... --dataset DS-... --seed 7 --cash 100000
```

It preregisters the experiment before any calculation (development, validation and a sealed holdout window), runs
the backtest after costs against its baselines, runs the ten challenges, and stores a Passport. The verdict is one
of `rejected`, `insufficient_evidence` or `candidate_for_paper`. None of them is advice, a permission to trade or a
promise of returns: `candidate_for_paper` means only that the evidence cleared the bar for a paper test.

Say every challenge that failed, with its finding as printed. Never re-run with tweaked rules to get past the
holdout: each change is a new trial, recorded in memory, and the Passport counts the trials.

## 4. Explain it, and check every number before you say it

```bash
traderos --json passport explain PP-...
traderos --json passport check-claims PP-... --text answer.txt
traderos --json passport show PP-...
traderos --json passport show PP-... --revision 1 --out passport.md
traderos --json passport reproduce PP-...
```

1. `explain` gives the Passport's own numbers in plain words, each cited to where it is stored. Build your answer
   from those findings only. Never subtract, average or annualise them yourself.
2. Write your answer to a file (`answer.txt`) and run `check-claims` on it. Each number must sit next to its
   metric and its window, baseline or challenge, written exactly as `explain` gives it, or inside a word-for-word
   quote of a finding. A number it cannot tie to one stored field, or whose sign, unit or size differs, is
   refused (`INSUFFICIENT_EVIDENCE`) with the reason and the stored value: fix it or take it out, and check
   again. Send only an answer that passed.
3. Give the four separate answers the Passport gives (data, tests, merit, freshness), never one badge.
4. `show` reopens and verifies the Passport from the store; `--out` also writes its Markdown. `reproduce` reruns
   its frozen experiment as a verification replay and says whether it reproduced exactly.

Sharing or exporting a Passport is not in this release: `passport share` and export come in a later release
([bundles.md](bundles.md)). Offer nothing in their place.

## 5. Research memory

```bash
traderos --json memory search pullback
traderos --json memory search rsi holdout --limit 10
```

Memory holds what was really researched, with record ids and hashes. Quote what it returns; never describe a past
result it did not return.

## Rules

- Natural-language rules stay drafts until the person approves them, by their own command.
- Every number comes from a result or a Passport, cited by its id. Explain first, check-claims before you answer.
- Tutorial data is never evidence. Say SYNTHETIC_DATA whenever it comes back, in a sentence of its own with no
  number in it: check-claims refuses every number in a sentence that says "never" or "not".
- No hidden tuning: never search for parameters that make the holdout look good.

## What it never does

It never approves a strategy for the person, never edits an approved version, never states a number the Passport
does not hold, and never calls a verdict a recommendation.
