"""Versioned pure features (spec 07.1). Every value is computed in Decimal from the bars it is given, and nothing else.

Rules every feature here keeps (and tests/unit/test_features.py holds them to):

* **Causal (QT01).** The value at bar i reads bars 0..i only. Changing a later bar never changes an earlier value.
* **Unavailable is None, never zero.** A value is None during warm-up, when an input is missing or not usable, and when
  the formula is undefined there (a division by zero, the log of a non-positive price).
* **Incomplete bars are not inputs.** A bar with `complete=False` (a session still running) has the value None, a
  window that includes one is None, and a recursive feature (EMA, RSI, ATR) carries its state past it unchanged.
* **Same store, same answer.** EMA, RSI and ATR depend on where the series starts (they never forget). Their
  `lookback` is ten periods of warm-up, so a caller that reads `lookback` bars as of an instant gets the same value
  every time it recomputes from the same store.
* **Deterministic.** Decimal arithmetic at 28 digits, values rounded half-even to 10 places on the way out.

Reference values are independent hand calculations (tests/unit/test_features.py), never an upstream tool's output.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

from traderos.contracts import Bar, ErrorCode, ValidationError
from traderos.contracts.research import FeatureDefinition

PLACES = 10
_Q = Decimal(1).scaleb(-PLACES)
FIELDS = ("open", "high", "low", "close", "volume")
MAX_PERIOD = 1000                                # a resource bound (spec 07.2): no feature reads more than this
WARMUP = 10                                      # recursive features read this many periods of history
_ZERO, _ONE, _HUNDRED = Decimal(0), Decimal(1), Decimal(100)

Values = tuple[Decimal | None, ...]


def _out(x: Decimal | None) -> Decimal | None:
    return None if x is None else x.quantize(_Q, rounding=ROUND_HALF_EVEN)


def _field(bars: Sequence[Bar], name: str) -> list[Decimal | None]:
    return [getattr(b, name) if b.complete else None for b in bars]


def _windows(xs: list[Decimal | None], n: int):
    """(i, window) for every i with a full window of n usable values ending at i; other positions are skipped."""
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        if all(v is not None for v in w):
            yield i, w


def _mean(w: Sequence[Decimal]) -> Decimal:
    return sum(w, _ZERO) / len(w)


# ---- the formulas ------------------------------------------------------------------------------------------------
# Each takes the bars and its parameters and returns one value (or None) per bar, unrounded.

def _sma(bars, period, field):
    out: list[Decimal | None] = [None] * len(bars)
    for i, w in _windows(_field(bars, field), period):
        out[i] = _mean(w)
    return out


def _ema(bars, period, field):
    """Seeded with the SMA of the first `period` usable values, then x*k + prev*(1-k), k = 2/(period+1)."""
    xs, out = _field(bars, field), [None] * len(bars)
    k = Decimal(2) / (period + 1)
    seed: list[Decimal] = []
    prev: Decimal | None = None
    for i, x in enumerate(xs):
        if x is None:
            continue                             # an incomplete bar: no value, state unchanged
        if prev is None:
            seed.append(x)
            if len(seed) == period:
                prev = _mean(seed)
                out[i] = prev
            continue
        prev = x * k + prev * (_ONE - k)
        out[i] = prev
    return out


def _wilder(values: list[Decimal | None], period: int) -> list[Decimal | None]:
    """Wilder's smoothing: the first value is the mean of the first `period` inputs, then (prev*(n-1) + x)/n."""
    out: list[Decimal | None] = [None] * len(values)
    seed: list[Decimal] = []
    prev: Decimal | None = None
    for i, x in enumerate(values):
        if x is None:
            continue
        if prev is None:
            seed.append(x)
            if len(seed) == period:
                prev = _mean(seed)
                out[i] = prev
            continue
        prev = (prev * (period - 1) + x) / period
        out[i] = prev
    return out


def _changes(bars) -> list[Decimal | None]:
    """close[i] - close[i-1], for the previous USABLE close (an incomplete bar in between is passed over)."""
    out: list[Decimal | None] = [None] * len(bars)
    prev: Decimal | None = None
    for i, b in enumerate(bars):
        if not b.complete:
            continue
        if prev is not None:
            out[i] = b.close - prev
        prev = b.close
    return out


def _rsi(bars, period):
    """Wilder's RSI on closes. No gains and no losses in the smoothing (a flat series) is 50, by convention."""
    ch = _changes(bars)
    gains = _wilder([None if c is None else max(c, _ZERO) for c in ch], period)
    losses = _wilder([None if c is None else max(-c, _ZERO) for c in ch], period)
    out: list[Decimal | None] = [None] * len(bars)
    for i, (g, l_) in enumerate(zip(gains, losses, strict=True)):
        if g is None or l_ is None:
            continue
        if l_ == 0:
            out[i] = _HUNDRED if g > 0 else Decimal(50)
        else:
            out[i] = _HUNDRED - _HUNDRED / (_ONE + g / l_)
    return out


def _atr(bars, period):
    """Wilder's average true range. True range needs the previous usable close, so the first bar has none."""
    tr: list[Decimal | None] = [None] * len(bars)
    prev_close: Decimal | None = None
    for i, b in enumerate(bars):
        if not b.complete:
            continue
        if prev_close is not None:
            tr[i] = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        prev_close = b.close
    return _wilder(tr, period)


def _ret(bars, period, field):
    """x[i] / x[i-period] - 1, as a fraction (0.012 is 1.2%). Both bars must be usable; a zero base is None."""
    xs, out = _field(bars, field), [None] * len(bars)
    for i in range(period, len(xs)):
        a, b = xs[i], xs[i - period]
        if a is not None and b is not None and b != 0:
            out[i] = a / b - _ONE
    return out


def _rvol(bars, period):
    """Realized volatility: the sample standard deviation of `period` log returns of closes, per bar, not
    annualized. Needs period+1 usable closes; a non-positive close makes the window None."""
    closes, out = _field(bars, "close"), [None] * len(bars)
    if period < 2:
        return out
    for i, w in _windows(closes, period + 1):
        if any(x <= 0 for x in w):
            continue
        logs = [(w[j] / w[j - 1]).ln() for j in range(1, len(w))]
        m = _mean(logs)
        var = sum(((x - m) ** 2 for x in logs), _ZERO) / (len(logs) - 1)
        out[i] = var.sqrt()
    return out


def _relvol(bars, period):
    """volume[i] divided by the mean volume of the `period` bars before it (bar i itself is not in the mean)."""
    vols, out = _field(bars, "volume"), [None] * len(bars)
    for i in range(period, len(vols)):
        prior, now = vols[i - period:i], vols[i]
        if now is None or any(v is None for v in prior):
            continue
        base = _mean(prior)
        if base != 0:
            out[i] = now / base
    return out


def _gap(bars):
    """open[i] / close[i-1] - 1, as a fraction. The previous bar must be usable and directly before it."""
    out: list[Decimal | None] = [None] * len(bars)
    for i in range(1, len(bars)):
        a, b = bars[i], bars[i - 1]
        if a.complete and b.complete and b.close != 0:
            out[i] = a.open / b.close - _ONE
    return out


def _range(bars):
    return [b.high - b.low if b.complete else None for b in bars]


def _extreme(pick):
    def f(bars, period, field):
        out: list[Decimal | None] = [None] * len(bars)
        for i, w in _windows(_field(bars, field), period):
            out[i] = pick(w)
        return out
    return f


# ---- the registry ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Spec:
    fn: Callable[..., list[Decimal | None]]
    units: str                                   # "price" means the unit of `field`; resolved in definition()
    params: tuple[str, ...]                      # in order; "period" is always first when present
    defaults: Mapping[str, Any]
    inputs: Callable[[Mapping[str, Any]], tuple[str, ...]]
    lookback: Callable[[Mapping[str, Any]], int]
    min_history: Callable[[Mapping[str, Any]], int]


def _p(params):
    return params["period"]


_SPECS: dict[str, _Spec] = {
    "sma": _Spec(_sma, "field", ("period", "field"), {"field": "close"}, lambda p: (p["field"],), _p, _p),
    "ema": _Spec(_ema, "field", ("period", "field"), {"field": "close"}, lambda p: (p["field"],),
                 lambda p: p["period"] * WARMUP, _p),
    "rsi": _Spec(_rsi, "index_0_100", ("period",), {}, lambda p: ("close",),
                 lambda p: p["period"] * WARMUP + 1, lambda p: p["period"] + 1),
    "atr": _Spec(_atr, "price", ("period",), {}, lambda p: ("high", "low", "close"),
                 lambda p: p["period"] * WARMUP + 1, lambda p: p["period"] + 1),
    "ret": _Spec(_ret, "fraction", ("period", "field"), {"period": 1, "field": "close"}, lambda p: (p["field"],),
                 lambda p: p["period"] + 1, lambda p: p["period"] + 1),
    "rvol": _Spec(_rvol, "fraction_per_bar", ("period",), {}, lambda p: ("close",),
                  lambda p: p["period"] + 1, lambda p: p["period"] + 1),
    "relvol": _Spec(_relvol, "ratio", ("period",), {}, lambda p: ("volume",),
                    lambda p: p["period"] + 1, lambda p: p["period"] + 1),
    "gap": _Spec(_gap, "fraction", (), {}, lambda p: ("open", "close"), lambda p: 2, lambda p: 2),
    "range": _Spec(_range, "price", (), {}, lambda p: ("high", "low"), lambda p: 1, lambda p: 1),
    "highest": _Spec(_extreme(max), "field", ("period", "field"), {"field": "high"}, lambda p: (p["field"],), _p, _p),
    "lowest": _Spec(_extreme(min), "field", ("period", "field"), {"field": "low"}, lambda p: (p["field"],), _p, _p),
}
NAMES = tuple(sorted(_SPECS))


def field_units(field: str) -> str:
    """The unit of a bar field: prices are "price", volume is "shares"."""
    if field not in FIELDS:
        raise ValidationError(f"{field!r} is not a bar field ({', '.join(FIELDS)})", code=ErrorCode.INVALID_STRATEGY)
    return "shares" if field == "volume" else "price"


def _params(name: str, raw: Any) -> dict[str, Any]:
    """{"sma": 200} or {"sma": {"period": 200, "field": "close"}} → validated params. Unknown keys are refused."""
    spec = _SPECS.get(name) if isinstance(name, str) else None
    if spec is None:
        raise ValidationError(f"unknown feature {name!r}; known: {', '.join(NAMES)}", code=ErrorCode.INVALID_STRATEGY)
    if isinstance(raw, Mapping):
        given = dict(raw)
    elif raw is None or (isinstance(raw, dict) and not raw):
        given = {}
    elif spec.params and spec.params[0] == "period":
        given = {"period": raw}
    else:
        raise ValidationError(f"{name} takes no number; write {{\"{name}\": {{}}}}", code=ErrorCode.INVALID_STRATEGY)
    unknown = set(given) - set(spec.params)
    if unknown:
        raise ValidationError(f"{name}: unknown parameter(s) {sorted(unknown)}", code=ErrorCode.INVALID_STRATEGY)
    params = {**spec.defaults, **given}
    missing = [k for k in spec.params if k not in params]
    if missing:
        raise ValidationError(f"{name}: missing {missing}", code=ErrorCode.INVALID_STRATEGY)
    if "period" in params:
        n = params["period"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_PERIOD:
            raise ValidationError(f"{name}: period must be a whole number from 1 to {MAX_PERIOD}, not {n!r}",
                                  code=ErrorCode.INVALID_STRATEGY)
        if name == "rvol" and n < 2:
            raise ValidationError("rvol: period must be at least 2", code=ErrorCode.INVALID_STRATEGY)
    if "field" in params:
        field_units(params["field"])             # refuses an unknown field
    return params


@dataclass(frozen=True)
class FeatureImpl:
    """A Feature (traderos.contracts.interfaces.Feature): its definition, and a pure compute over bars."""
    definition: FeatureDefinition
    _spec: _Spec
    _params: tuple[tuple[str, Any], ...]

    def compute(self, bars: Sequence[Bar]) -> Values:
        params = dict(self._params)
        args = [params[k] for k in self._spec.params]
        with localcontext() as ctx:
            ctx.prec = 28
            ctx.rounding = ROUND_HALF_EVEN
            raw = self._spec.fn(list(bars), *args)
            return tuple(_out(v) for v in raw)

    @property
    def label(self) -> str:
        """How a person reads it: sma(200), ema(20, high), gap()."""
        params = dict(self._params)
        spec_defaults = self._spec.defaults
        shown = [str(params[k]) for k in self._spec.params
                 if k == "period" or params[k] != spec_defaults.get(k)]
        return f"{self.definition.feature_id}({', '.join(shown)})"


def feature(name: str, raw_params: Any = None) -> FeatureImpl:
    """The feature called `name`, e.g. feature("sma", 200) or feature("ema", {"period": 20, "field": "high"}).
    Refuses an unknown name, an unknown parameter, a period outside 1..MAX_PERIOD and an unknown field
    (INVALID_STRATEGY)."""
    params = _params(name, raw_params)
    spec = _SPECS[name]
    units = field_units(params["field"]) if spec.units == "field" else spec.units
    definition = FeatureDefinition(
        feature_id=name, formula_version=f"{name}/1",
        params={k: params[k] for k in spec.params}, inputs=spec.inputs(params), units=units,
        lookback=spec.lookback(params), min_history=spec.min_history(params), missing_policy="unavailable")
    return FeatureImpl(definition=definition, _spec=spec, _params=tuple((k, params[k]) for k in spec.params))


__all__ = ["FIELDS", "MAX_PERIOD", "NAMES", "PLACES", "WARMUP", "FeatureImpl", "feature", "field_units"]
