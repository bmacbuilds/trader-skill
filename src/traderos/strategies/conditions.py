"""The one condition grammar (spec 07.2), shared by a scan, an alert and a strategy's entry and exit.

    {"all": [ ... ]}  {"any": [ ... ]}             combinators, non-empty lists
    {"above": [a, b]}  {"below": [a, b]}           a > b, a < b
    a, b: "close" (a bar field), {"sma": 200} or {"sma": {"period": 200, "field": "close"}} (a feature),
          10 or "10.5" (a number: an int, or a decimal string; a float is refused)
    {"bars_held": 10}                              strategy exits only: it needs a position

Customer content is untrusted (spec 04.2). It is parsed into a small typed tree and nothing in it is ever executed:
no eval, no imports, no templates. Size is bounded, unknown keys are refused, and a comparison between two different
units (a price against an RSI) is refused rather than evaluated. Every refusal is ValidationError(INVALID_STRATEGY).

Evaluation is three-valued. A comparison whose sides cannot both be computed is None (unknown), never False:
`all` is False if any part is False, else None if any is None; `any` is True if any part is True, else None if any
is None. Each comparison leaves a ConditionTrace, so a person can see why a symbol did or did not qualify (spec 12.2 A).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from traderos.contracts import Bar, ErrorCode, MetricValue, ValidationError
from traderos.contracts.canonical import decimal_str
from traderos.contracts.intel import ConditionTrace
from traderos.features import FIELDS, MAX_PERIOD, FeatureImpl, feature, field_units

MAX_DEPTH = 8
MAX_NODES = 64
MAX_LIST = 32
NUMBER = "number"


class Context(StrEnum):
    SCAN = "scan"
    ALERT = "alert"
    STRATEGY_ENTRY = "strategy_entry"
    STRATEGY_EXIT = "strategy_exit"


def _refuse(message: str) -> ValidationError:
    return ValidationError(message, code=ErrorCode.INVALID_STRATEGY)


# ---- the tree ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    name: str

    @property
    def units(self) -> str:
        return field_units(self.name)

    @property
    def text(self) -> str:
        return self.name


@dataclass(frozen=True)
class FeatureCall:
    impl: FeatureImpl

    @property
    def units(self) -> str:
        return self.impl.definition.units

    @property
    def text(self) -> str:
        return self.impl.label


@dataclass(frozen=True)
class Number:
    value: Decimal
    units: str = NUMBER

    @property
    def text(self) -> str:
        return decimal_str(self.value)


Operand = Field | FeatureCall | Number


@dataclass(frozen=True)
class Compare:
    op: str                                      # "above" | "below"
    left: Operand
    right: Operand

    @property
    def text(self) -> str:
        return f"{self.left.text} {'>' if self.op == 'above' else '<'} {self.right.text}"


@dataclass(frozen=True)
class BarsHeld:
    bars: int

    @property
    def text(self) -> str:
        return f"bars_held >= {self.bars}"


@dataclass(frozen=True)
class Group:
    kind: str                                    # "all" | "any"
    parts: tuple[Condition, ...]


Condition = Group | Compare | BarsHeld


# ---- parsing ---------------------------------------------------------------------------------------------------

def parse(raw: Any, *, context: Context | str) -> Condition:
    """Untrusted JSON-shaped data → a validated Condition, or ValidationError(INVALID_STRATEGY)."""
    ctx = Context(context)
    count = [0]
    return _node(raw, ctx, 1, count)


def _node(raw: Any, ctx: Context, depth: int, count: list[int]) -> Condition:
    if depth > MAX_DEPTH:
        raise _refuse(f"conditions nest deeper than {MAX_DEPTH} levels")
    count[0] += 1
    if count[0] > MAX_NODES:
        raise _refuse(f"more than {MAX_NODES} conditions")
    if not isinstance(raw, Mapping) or len(raw) != 1:
        raise _refuse("each condition is an object with exactly one key: all, any, above, below"
                      + (" or bars_held" if ctx is Context.STRATEGY_EXIT else ""))
    (key, body), = raw.items()
    if key in ("all", "any"):
        if not isinstance(body, list) or not body:
            raise _refuse(f"{key} takes a non-empty list of conditions")
        if len(body) > MAX_LIST:
            raise _refuse(f"{key} lists more than {MAX_LIST} conditions")
        return Group(kind=key, parts=tuple(_node(x, ctx, depth + 1, count) for x in body))
    if key in ("above", "below"):
        if not isinstance(body, list) or len(body) != 2:
            raise _refuse(f"{key} takes a list of exactly two values")
        left, right = (_operand(x) for x in body)
        if isinstance(left, Number) and isinstance(right, Number):
            raise _refuse(f"{key} compares two numbers; one side must be a bar field or a feature")
        units = {o.units for o in (left, right) if not isinstance(o, Number)}
        if len(units) > 1:
            raise _refuse(f"{key} compares {left.text} ({left.units}) with {right.text} ({right.units}); "
                          "the units differ")
        return Compare(op=key, left=left, right=right)
    if key == "bars_held":
        if ctx is not Context.STRATEGY_EXIT:
            raise _refuse("bars_held is only allowed in a strategy's exit: it needs a position")
        if isinstance(body, bool) or not isinstance(body, int) or not 1 <= body <= MAX_PERIOD:
            raise _refuse(f"bars_held takes a whole number from 1 to {MAX_PERIOD}")
        return BarsHeld(bars=body)
    raise _refuse(f"unknown condition {key!r}")


def _operand(raw: Any) -> Operand:
    if isinstance(raw, bool):
        raise _refuse("true/false is not a value")
    if isinstance(raw, float):
        raise _refuse(f"{raw!r} is a float; write a decimal number as a string, e.g. \"10.5\"")
    if isinstance(raw, int):
        return Number(Decimal(raw))
    if isinstance(raw, str):
        if raw in FIELDS:
            return Field(raw)
        try:
            d = Decimal(raw)
        except InvalidOperation:
            raise _refuse(f"{raw!r} is neither a bar field ({', '.join(FIELDS)}) nor a number") from None
        if not d.is_finite():
            raise _refuse(f"{raw!r} is not a finite number")
        return Number(d)
    if isinstance(raw, Mapping) and len(raw) == 1:
        (name, params), = raw.items()
        return FeatureCall(feature(name, params))
    raise _refuse(f"{raw!r} is not a bar field, a feature or a number")


def features(cond: Condition) -> tuple[FeatureImpl, ...]:
    """Every feature the condition reads, once each, in the order they appear."""
    seen: dict[Any, FeatureImpl] = {}

    def walk(c: Condition) -> None:
        if isinstance(c, Group):
            for p in c.parts:
                walk(p)
        elif isinstance(c, Compare):
            for o in (c.left, c.right):
                if isinstance(o, FeatureCall):
                    seen.setdefault((o.impl.definition.formula_version, o.impl._params), o.impl)
    walk(cond)
    return tuple(seen.values())


def history_needed(cond: Condition) -> int:
    """How many bars to read, newest last, so every feature in the condition is computed the same way each time."""
    return max((f.definition.lookback for f in features(cond)), default=1)


# ---- evaluation ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Evaluation:
    passed: bool | None
    traces: tuple[ConditionTrace, ...]


class _Values:
    """Every operand's value at one bar index, computed once per feature over the whole series."""

    def __init__(self, bars: Sequence[Bar], cond: Condition, price_unit: str = "price") -> None:
        self.bars = bars
        self.price_unit = price_unit
        self.cache = {(f.definition.formula_version, f._params): f.compute(bars) for f in features(cond)}

    def at(self, o: Operand, i: int) -> MetricValue:
        if isinstance(o, Number):
            return MetricValue(value=o.value, unit=NUMBER)
        unit = self.price_unit if o.units == "price" else o.units
        if i < 0:
            return MetricValue(value=None, unit=unit, unavailable_reason="no bars available")
        if isinstance(o, Field):
            b = self.bars[i]
            if not b.complete:
                return MetricValue(value=None, unit=unit, unavailable_reason="the bar is not complete")
            return MetricValue(value=getattr(b, o.name), unit=unit)
        d = o.impl.definition
        v = self.cache[(d.formula_version, o.impl._params)][i]
        if v is None:
            usable = sum(1 for b in self.bars[:i + 1] if b.complete)
            why = (f"needs {d.min_history} complete bars, has {usable}" if usable < d.min_history
                   else "not defined at this bar (an incomplete bar or an undefined value)")
            return MetricValue(value=None, unit=unit, unavailable_reason=f"{o.text}: {why}")
        return MetricValue(value=v, unit=unit)


def _kleene(kind: str, results: Sequence[bool | None]) -> bool | None:
    if kind == "all":
        return False if False in results else (None if None in results else True)
    return True if True in results else (None if None in results else False)


def _eval(c: Condition, vals: _Values, i: int, held: int | None, traces: list[ConditionTrace] | None) -> bool | None:
    if isinstance(c, Group):
        return _kleene(c.kind, [_eval(p, vals, i, held, traces) for p in c.parts])
    if isinstance(c, BarsHeld):
        left = (MetricValue(value=Decimal(held), unit="bars") if held is not None
                else MetricValue(value=None, unit="bars", unavailable_reason="no position"))
        passed = None if held is None else held >= c.bars
        if traces is not None:
            traces.append(ConditionTrace(condition=c.text, passed=passed, left=left,
                                         right=MetricValue(value=Decimal(c.bars), unit="bars"),
                                         reason="" if passed is not None else "no position"))
        return passed
    left, right = vals.at(c.left, i), vals.at(c.right, i)
    if left.value is None or right.value is None:
        passed = None
    else:
        passed = left.value > right.value if c.op == "above" else left.value < right.value
    if traces is not None:
        refs = tuple(o.impl.definition.formula_version for o in (c.left, c.right) if isinstance(o, FeatureCall))
        reason = "; ".join(x.unavailable_reason for x in (left, right) if x.unavailable_reason)
        traces.append(ConditionTrace(condition=c.text, passed=passed, left=left, right=right, feature_refs=refs,
                                     reason=reason))
    return passed


def evaluate(cond: Condition, bars: Sequence[Bar], *, bars_held: int | None = None,
             price_unit: str = "price") -> Evaluation:
    """The condition at the LAST bar given, with one trace per comparison. The caller has already applied as-of:
    only bars available at the decision instant are passed (BarReader.bars). No bars: every comparison is None.
    `price_unit` labels prices in the traces, e.g. the instrument's currency "USD" (contracts v1 section 2)."""
    vals = _Values(bars, cond, price_unit)
    traces: list[ConditionTrace] = []
    passed = _eval(cond, vals, len(bars) - 1, bars_held, traces)
    return Evaluation(passed=passed, traces=tuple(traces))


def evaluate_series(cond: Condition, bars: Sequence[Bar],
                    *, bars_held: Sequence[int | None] | None = None) -> tuple[bool | None, ...]:
    """The condition at every bar, for the lab's simulator. Causal: the value at i reads bars 0..i only.
    `bars_held`, aligned with `bars`, is the position's age at each bar (None when flat), for exit conditions."""
    if bars_held is not None and len(bars_held) != len(bars):
        raise ValidationError("bars_held must be aligned with bars", code=ErrorCode.INVALID_INPUT)
    vals = _Values(bars, cond)
    return tuple(_eval(cond, vals, i, None if bars_held is None else bars_held[i], None) for i in range(len(bars)))


def text(cond: Condition) -> str:
    """The whole condition in words, e.g. "all(close > sma(200), rsi(2) < 10)"."""
    if isinstance(cond, Group):
        return f"{cond.kind}({', '.join(text(p) for p in cond.parts)})"
    return cond.text


__all__ = ["MAX_DEPTH", "MAX_LIST", "MAX_NODES", "BarsHeld", "Compare", "Condition", "Context", "Evaluation",
           "FeatureCall", "Field", "Group", "Number", "evaluate", "evaluate_series", "features", "history_needed",
           "parse", "text"]
