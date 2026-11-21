"""The strategy/1 rulebook (spec 07.2, S09): untrusted text in, a validated draft out, an approved version kept.

A strategy is DATA: a JSON document in the strategy/1 grammar. It is parsed into a typed `Rulebook`, never
evaluated as code (no eval, imports, templates, SQL or shell). Unknown keys are refused, sizes are bounded, and the
entry and exit conditions go through the one condition grammar (`traderos.strategies.conditions`).

    {
      "schema_version": "strategy/1",
      "name": "daily-etf-pullback",
      "hypothesis": "Short pullbacks inside an uptrend tend to revert within days.",
      "universe": ["SPY", "QQQ", "IWM"],
      "frequency": "1d",
      "direction": "long",
      "entry": {"all": [{"above": ["close", {"sma": 200}]}, {"below": [{"rsi": 2}, 10]}]},
      "exit": {"any": [{"above": ["close", {"sma": 5}]}, {"bars_held": 10}]},
      "hypothetical_stop": {"atr_multiple": "2.5", "atr_period": 14},
      "sizing": {"fraction_of_equity": "0.33"}
    }

Draft and approval are different operations (spec 04.4): `parse_strategy` makes an editable draft; only
`approve_strategy_version`, with a person's explicit OwnerAction, makes an immutable StrategyVersion. A changed rule
is a new revision whose parent is the old one; an approved version never changes.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from traderos.contracts import (
    AuthorType,
    ErrorCode,
    IntegrityError,
    Interval,
    OwnerAction,
    StrategyDraft,
    StrategyVersion,
    ValidationError,
    canonical_bytes,
    parse_decimal,
    sha256_bytes,
    with_hash,
)
from traderos.strategies import conditions as cond

SCHEMA = "strategy/1"
MAX_BYTES = 64 * 1024
MAX_UNIVERSE = 50
MAX_TEXT = 2000
KEYS = {"schema_version", "name", "hypothesis", "universe", "frequency", "direction", "entry", "exit",
        "hypothetical_stop", "sizing", "notes"}
REQUIRED = {"schema_version", "name", "hypothesis", "universe", "frequency", "direction", "entry", "exit"}
NAME = re.compile(r"^[a-z0-9][a-z0-9\-_.]{0,63}$")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
SUPPORTED_DIRECTIONS = ("long",)                 # short selling needs borrow and margin modelling: deferred


def _refuse(message: str, code: ErrorCode = ErrorCode.INVALID_STRATEGY) -> ValidationError:
    return ValidationError(message, code=code)


@dataclass(frozen=True)
class Stop:
    atr_multiple: Decimal
    atr_period: int


@dataclass(frozen=True)
class Rulebook:
    """A validated strategy/1 document."""
    name: str
    hypothesis: str
    universe: tuple[str, ...]
    frequency: Interval
    direction: str
    entry: cond.Condition
    exit: cond.Condition
    stop: Stop | None
    fraction_of_equity: Decimal
    document: dict[str, Any]                     # the canonical document (what is hashed, stored and shown)

    @property
    def semantic_hash(self) -> str:
        return semantic_hash(self.document)

    def history_needed(self) -> int:
        need = max(cond.history_needed(self.entry), cond.history_needed(self.exit))
        if self.stop:
            need = max(need, self.stop.atr_period * 10 + 1)
        return need


def semantic_hash(document: Mapping[str, Any]) -> str:
    """The hash of the rules alone: no names of people, no timestamps (spec 05.4). `notes` are not semantics."""
    return sha256_bytes(canonical_bytes({k: v for k, v in document.items() if k != "notes"}))


def _loads(text: str | bytes) -> Any:
    if isinstance(text, bytes):
        if len(text) > MAX_BYTES:
            raise _refuse(f"a strategy is at most {MAX_BYTES} bytes", ErrorCode.RESOURCE_BUDGET_EXCEEDED)
        text = text.decode("utf-8")
    if len(text) > MAX_BYTES:
        raise _refuse(f"a strategy is at most {MAX_BYTES} bytes", ErrorCode.RESOURCE_BUDGET_EXCEEDED)

    def no_dupes(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise _refuse(f"duplicate key {k!r}")
            out[k] = v
        return out

    def no_float(s):
        raise _refuse(f"{s} is a float; write decimal numbers as strings, e.g. \"2.5\"")

    def no_constant(s):
        raise _refuse(f"{s} is not allowed")

    if text.lstrip().startswith(("schema_version:", "name:", "---")):
        raise _refuse("YAML strategies are not supported in this build; write the same keys as JSON")
    try:
        return json.loads(text, object_pairs_hook=no_dupes, parse_float=no_float, parse_constant=no_constant)
    except json.JSONDecodeError as e:
        raise _refuse(f"not valid JSON: {e.msg} at line {e.lineno} column {e.colno}") from None


def _text(raw: Any, key: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _refuse(f"{key} must be non-empty text")
    if len(raw) > limit:
        raise _refuse(f"{key} is longer than {limit} characters")
    return raw.strip()


def compile_rules(raw: Any) -> Rulebook:
    """A strategy/1 document (text or a mapping) -> a validated Rulebook, or ValidationError(INVALID_STRATEGY)."""
    doc = _loads(raw) if isinstance(raw, (str, bytes)) else raw
    if not isinstance(doc, Mapping):
        raise _refuse("a strategy is a JSON object")
    unknown = sorted(set(doc) - KEYS)
    if unknown:
        raise _refuse(f"unknown key(s): {', '.join(unknown)}")
    missing = sorted(REQUIRED - set(doc))
    if missing:
        raise _refuse(f"missing key(s): {', '.join(missing)}")
    if doc["schema_version"] != SCHEMA:
        raise _refuse(f"schema_version must be {SCHEMA!r}", ErrorCode.SCHEMA_UNSUPPORTED)
    name = _text(doc["name"], "name", 64)
    if not NAME.match(name):
        raise _refuse("name is lower case letters, digits, '-', '_' or '.', starting with a letter or digit")
    hypothesis = _text(doc["hypothesis"], "hypothesis")
    universe = doc["universe"]
    if not isinstance(universe, list) or not universe or len(universe) > MAX_UNIVERSE:
        raise _refuse(f"universe is a list of 1 to {MAX_UNIVERSE} symbols")
    for s in universe:
        if not isinstance(s, str) or not SYMBOL.match(s):
            raise _refuse(f"{s!r} is not a ticker symbol")
    if len(set(universe)) != len(universe):
        raise _refuse("universe lists a symbol twice")
    try:
        frequency = Interval(doc["frequency"])
    except ValueError:
        raise _refuse(f"frequency must be one of {', '.join(i.value for i in Interval)}") from None
    if frequency is not Interval.D1:
        raise _refuse("this build researches daily (1d) strategies; intraday research is deferred",
                      ErrorCode.OUTSIDE_CAPABILITY)
    direction = doc["direction"]
    if direction not in ("long", "short"):
        raise _refuse("direction is long or short")
    if direction not in SUPPORTED_DIRECTIONS:
        raise _refuse("short strategies need borrow and margin modelling, which this build does not have",
                      ErrorCode.OUTSIDE_CAPABILITY)
    entry = cond.parse(doc["entry"], context=cond.Context.STRATEGY_ENTRY)
    exit_ = cond.parse(doc["exit"], context=cond.Context.STRATEGY_EXIT)
    stop = None
    if doc.get("hypothetical_stop") is not None:
        s = doc["hypothetical_stop"]
        if not isinstance(s, Mapping) or set(s) != {"atr_multiple", "atr_period"}:
            raise _refuse("hypothetical_stop is {\"atr_multiple\": \"2.5\", \"atr_period\": 14}")
        mult = parse_decimal(s["atr_multiple"], field="hypothetical_stop.atr_multiple")
        period = s["atr_period"]
        if not (Decimal("0.1") <= mult <= 20) or isinstance(period, bool) or not isinstance(period, int) \
                or not 1 <= period <= 200:
            raise _refuse("hypothetical_stop: atr_multiple from 0.1 to 20, atr_period from 1 to 200")
        stop = Stop(atr_multiple=mult, atr_period=period)
    fraction = Decimal(1) / len(universe)
    if doc.get("sizing") is not None:
        s = doc["sizing"]
        if not isinstance(s, Mapping) or set(s) != {"fraction_of_equity"}:
            raise _refuse("sizing is {\"fraction_of_equity\": \"0.25\"}")
        fraction = parse_decimal(s["fraction_of_equity"], field="sizing.fraction_of_equity")
        if not (0 < fraction <= 1):
            raise _refuse("sizing.fraction_of_equity is more than 0 and at most 1 (no leverage)")
    if "notes" in doc:
        _text(doc["notes"], "notes")
    document = json.loads(canonical_bytes(dict(doc)))
    return Rulebook(name=name, hypothesis=hypothesis, universe=tuple(universe), frequency=frequency,
                    direction=direction, entry=entry, exit=exit_, stop=stop, fraction_of_equity=fraction,
                    document=document)


def draft(raw: Any, *, author_type: AuthorType = AuthorType.HUMAN, parent_version_id: str | None = None,
          source_pack: str | None = None, now: datetime | None = None) -> StrategyDraft:
    """parse_strategy (spec 04.4): validated, still a draft. An AI's draft is marked so until a person approves."""
    book = compile_rules(raw)
    digest = book.semantic_hash
    return StrategyDraft(draft_id=f"SD-{digest[:20]}", name=book.name, hypothesis=book.hypothesis,
                         rules=book.document, author_type=author_type, source_pack=source_pack,
                         parent_version_id=parent_version_id, semantic_hash=digest,
                         created_at=now or datetime.now(UTC))


def approve(d: StrategyDraft, owner_action: OwnerAction, *, previous: StrategyVersion | None = None) -> StrategyVersion:
    """approve_strategy_version (spec 04.4): a person's explicit act turns a draft into an immutable revision."""
    # Re-validate and re-hash: a stored draft is still untrusted input, and the approval names exactly its rules.
    if compile_rules(d.rules).semantic_hash != d.semantic_hash:
        raise IntegrityError(f"draft {d.draft_id}: its rules do not match its rules hash; nothing was approved")
    if not owner_action.actor or not owner_action.statement:
        raise ValidationError("an approval names who approved and what they were shown",
                              code=ErrorCode.INVALID_APPROVAL)
    if owner_action.method not in ("cli_confirm", "skill_confirm", "demo_confirm"):
        raise ValidationError(f"{owner_action.method!r} is not an approval method", code=ErrorCode.INVALID_APPROVAL)
    if d.semantic_hash[:12] not in owner_action.statement:
        raise ValidationError("the approval must name the exact rules it approves (their hash)",
                              code=ErrorCode.INVALID_APPROVAL)
    if previous is not None and previous.semantic_hash == d.semantic_hash:
        return previous                          # nothing changed: the approved version stands
    strategy_id = previous.strategy_id if previous else f"ST-{d.semantic_hash[:16]}"
    revision = previous.revision + 1 if previous else 1
    return with_hash(StrategyVersion(
        version_id=f"SV-{d.semantic_hash[:20]}", strategy_id=strategy_id, revision=revision, draft_id=d.draft_id,
        name=d.name, hypothesis=d.hypothesis, rules=d.rules, semantic_hash=d.semantic_hash,
        parent_version_id=previous.version_id if previous else None, approved_by=owner_action))


def _stop_text(stop: Stop | None) -> str:
    if stop is None:
        return "none"
    return f"{stop.atr_multiple} x atr({stop.atr_period}) below the entry fill"


def render(book_or_rules: Rulebook | Mapping[str, Any]) -> str:
    """The rules in words, for the person who must approve them (spec 07.2: rendered before approval)."""
    book = book_or_rules if isinstance(book_or_rules, Rulebook) else compile_rules(book_or_rules)
    lines = [f"Strategy {book.name} ({book.direction}, {book.frequency.value} bars)",
             f"  Hypothesis: {book.hypothesis}",
             f"  Universe:   {', '.join(book.universe)}",
             f"  Enter when: {cond.text(book.entry)}",
             f"  Exit when:  {cond.text(book.exit)}",
             f"  Stop:       {_stop_text(book.stop)}",
             f"  Size:       {book.fraction_of_equity:.2%} of equity per position, whole shares, no leverage",
             f"  Rules hash: {book.semantic_hash[:12]}",
             "  This is a research hypothesis, not a recommendation. Nothing here places an order."]
    return "\n".join(lines)


__all__ = ["SCHEMA", "Rulebook", "Stop", "approve", "compile_rules", "draft", "render", "semantic_hash"]
