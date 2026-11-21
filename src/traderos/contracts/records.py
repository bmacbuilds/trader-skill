"""The record machinery every contract uses: frozen typed dataclasses, strict parsing, canonical dicts and hashes.

ADR-001: contracts are standard-library frozen dataclasses with explicit validation, so the core installs with no
runtime dependency. Two kinds:
  * `Part`: a value object nested inside a record (no schema_version of its own);
  * `Record`: anything stored or sent on its own. It carries `schema_version` (its class's `SCHEMA`, e.g. "bar/1").

PARSING IS STRICT (spec 04.2, untrusted input): `from_dict` refuses unknown keys, missing required keys, a float
where a Decimal belongs, a naive timestamp, a wrong type and a schema_version this build does not know
(SCHEMA_UNSUPPORTED). Construction also refuses a non-finite or float Decimal and a naive datetime, so no code path
can build a record that would not survive its own round trip.
"""
from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, TypeVar

from traderos.contracts import canonical
from traderos.contracts.errors import ErrorCode, IntegrityError, ValidationError

T = TypeVar("T")
_HINTS: dict[type, dict[str, Any]] = {}


def _hints(cls: type) -> dict[str, Any]:
    got = _HINTS.get(cls)
    if got is None:
        got = typing.get_type_hints(cls)      # each class in the MRO resolves names in its own module
        got = {f.name: got[f.name] for f in fields(cls)}
        _HINTS[cls] = got
    return got


def _is_optional(tp: Any) -> tuple[bool, Any]:
    if typing.get_origin(tp) in (typing.Union, types.UnionType):
        args = typing.get_args(tp)
        rest = tuple(a for a in args if a is not type(None))
        if len(rest) < len(args):
            return True, rest[0] if len(rest) == 1 else typing.Union[rest]  # noqa: UP007
    return False, tp


def _check_value(name: str, tp: Any, value: Any) -> None:
    """Construction-time guard for the invariants no record may break (finite Decimal, aware datetime)."""
    optional, inner = _is_optional(tp)
    if value is None:
        if not optional and inner in (Decimal, datetime, date):
            raise ValidationError(f"{name}: required")
        return
    if inner is Decimal and (not isinstance(value, Decimal) or not value.is_finite()):
        raise ValidationError(f"{name}: must be a finite Decimal, got {value!r}")
    if inner is datetime and (not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None):
        raise ValidationError(f"{name}: must be a timezone-aware datetime, got {value!r}")


def contract(cls: type[T]) -> type[T]:
    """The one decorator every contract class carries: a frozen, slotted, keyword-only dataclass."""
    return dataclass(frozen=True, slots=True, kw_only=True)(cls)


@dataclass(frozen=True, slots=True, kw_only=True)
class Part:
    """A value object inside a record."""

    SCHEMA: ClassVar[str | None] = None
    # Optional fields added in a minor contract revision. When None they are left out of the canonical form, so a
    # record written before they existed keeps its exact bytes and its hash (ADR-009; docs/contracts-v1.md section 8).
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset()

    def __post_init__(self) -> None:
        for name, tp in _hints(type(self)).items():
            _check_value(f"{type(self).__name__}.{name}", tp, getattr(self, name))
        self.validate()

    def validate(self) -> None:
        """Cross-field rules; subclasses override and raise ValidationError."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Record(Part):
    """Anything stored or transmitted on its own. Subclasses set SCHEMA = "<name>/<major>"."""

    schema_version: str = ""

    def __post_init__(self) -> None:
        schema = type(self).SCHEMA
        if not schema:
            raise TypeError(f"{type(self).__name__} must set SCHEMA")
        if not self.schema_version:
            object.__setattr__(self, "schema_version", schema)
        elif self.schema_version != schema:
            raise ValidationError(f"{type(self).__name__}: schema_version {self.schema_version!r} is not {schema!r}",
                                  code=ErrorCode.SCHEMA_UNSUPPORTED)
        Part.__post_init__(self)


def is_record(value: Any) -> bool:
    return isinstance(value, Part)


def to_dict(value: Part) -> dict[str, Any]:
    """The canonical plain-JSON form of a record (every field, None as null; a field the class lists in
    OMIT_IF_NONE is left out when None)."""
    omit = type(value).OMIT_IF_NONE
    return {f.name: canonical.to_jsonable(getattr(value, f.name)) for f in fields(value)
            if not (f.name in omit and getattr(value, f.name) is None)}


def to_json(value: Part) -> str:
    return canonical.canonical_bytes(value).decode("utf-8")


def _convert(path: str, tp: Any, raw: Any) -> Any:
    if tp is Any:
        canonical.to_jsonable(raw)            # still refuses floats, NaN and non-string keys inside free-form JSON
        return raw
    optional, inner = _is_optional(tp)
    if raw is None:
        if optional:
            return None
        raise ValidationError(f"{path}: required, got null")
    tp = inner
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        problems = []
        for option in typing.get_args(tp):
            try:
                return _convert(path, option, raw)
            except ValidationError as e:
                problems.append(e.message)
        raise ValidationError(f"{path}: matches none of {typing.get_args(tp)}: {'; '.join(problems)}")
    if origin is tuple:
        args = typing.get_args(tp)
        if not isinstance(raw, list):
            raise ValidationError(f"{path}: expected an array")
        item = args[0] if args else Any
        return tuple(_convert(f"{path}[{i}]", item, v) for i, v in enumerate(raw))
    if origin is dict:
        key_t, val_t = typing.get_args(tp) or (str, Any)
        if not isinstance(raw, dict):
            raise ValidationError(f"{path}: expected an object")
        return {_convert(f"{path}.<key>", key_t, k): _convert(f"{path}.{k}", val_t, v) for k, v in raw.items()}
    if isinstance(tp, type) and issubclass(tp, Part):
        return from_dict(tp, raw, _path=path)
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(raw)
        except ValueError:
            allowed = ", ".join(str(m.value) for m in tp)
            raise ValidationError(f"{path}: {raw!r} is not one of: {allowed}") from None
    if tp is Decimal:
        return canonical.parse_decimal(raw, field=path)
    if tp is datetime:
        return canonical.parse_utc(raw, field=path)
    if tp is date:
        return canonical.parse_date(raw, field=path)
    if tp is bool:
        if not isinstance(raw, bool):
            raise ValidationError(f"{path}: expected true or false")
        return raw
    if tp is int:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValidationError(f"{path}: expected an integer")
        return raw
    if tp is str:
        if not isinstance(raw, str):
            raise ValidationError(f"{path}: expected a string")
        return raw
    raise ValidationError(f"{path}: unsupported contract type {tp!r}")


def from_dict(cls: type[T], data: Any, *, _path: str = "") -> T:
    """Parse untrusted plain JSON into a record of `cls`, strictly."""
    path = _path or cls.__name__
    if not isinstance(data, dict):
        raise ValidationError(f"{path}: expected an object")
    hints = _hints(cls)
    unknown = sorted(set(data) - set(hints))
    if unknown:
        raise ValidationError(f"{path}: unknown key(s): {', '.join(unknown)}")
    schema = getattr(cls, "SCHEMA", None)
    if schema and data.get("schema_version") != schema:
        raise ValidationError(f"{path}: schema_version {data.get('schema_version')!r} is not supported "
                              f"(this build reads {schema!r})", code=ErrorCode.SCHEMA_UNSUPPORTED)
    kwargs = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _convert(f"{path}.{f.name}", hints[f.name], data[f.name])
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise ValidationError(f"{path}: missing required key {f.name!r}")
    return cls(**kwargs)


def from_json(cls: type[T], text: str | bytes) -> T:
    import json

    def _no_float(s: str):
        raise ValidationError(f"float literal {s} refused; write decimals as strings")

    def _no_constant(s: str):
        raise ValidationError(f"{s} refused")

    def _no_duplicates(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValidationError(f"duplicate key {k!r}")
            out[k] = v
        return out

    try:
        data = json.loads(text, parse_float=_no_float, parse_constant=_no_constant, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as e:
        raise ValidationError(f"not valid JSON: {e.msg} at line {e.lineno}") from None
    except RecursionError:
        raise ValidationError("JSON nested too deeply") from None
    except ValueError as e:                      # e.g. an integer longer than Python will convert
        raise ValidationError(f"JSON value refused: {str(e)[:120]}") from None
    return from_dict(cls, data)


def record_hash(value: Part) -> str:
    """SHA-256 of the canonical form, the `canonical_hash` field omitted."""
    return canonical.record_hash(value)


def with_hash(value: T) -> T:
    """The same record with `canonical_hash` filled in."""
    return replace(value, canonical_hash=record_hash(value))  # type: ignore[type-var]


def verify_hash(value: Part) -> None:
    stated = getattr(value, "canonical_hash", None)
    if not stated:
        raise IntegrityError(f"{type(value).__name__} carries no canonical_hash")
    actual = record_hash(value)
    if stated != actual:
        raise IntegrityError(f"{type(value).__name__}: canonical_hash {stated[:12]}... does not match {actual[:12]}...")


__all__ = ["Part", "Record", "contract", "field", "from_dict", "from_json", "is_record", "record_hash", "to_dict",
           "to_json", "verify_hash", "with_hash"]
