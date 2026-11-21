"""Canonical JSON and hashes (spec 05.1, 05.4). FROZEN in contracts v1: changing any rule here changes every hash.

CANONICAL FORM (docs/contracts-v1.md section 3, ADR-002):
  * a JSON object's keys are sorted by Unicode code point; no insignificant whitespace (separators "," and ":");
    UTF-8 bytes, non-ASCII characters written as themselves;
  * Decimal is a JSON string in plain notation, normalized: no exponent, no trailing fractional zeros, "-0" is "0"
    ("1.50" and "1.5" are the same value and the same string);
  * an aware datetime is a JSON string in UTC: "YYYY-MM-DDTHH:MM:SSZ", or with ".ffffff" when it has microseconds;
    a naive datetime is refused (spec 04.3: no timezone-naive interpretation);
  * a date is "YYYY-MM-DD";
  * int is a JSON number and must lie within +/-(2**53 - 1); bool is true/false; None is null;
  * float is REFUSED everywhere a record is hashed or stored (spec 04.3: no float serialization in financial
    contracts). Statistical code converts a float through `decimal_from_float` at a declared precision first;
  * Enum serializes as its value; tuple and list as an array, order preserved.

HASHES: an artifact hash is SHA-256 of exact bytes (`sha256_bytes`); a record's canonical hash is SHA-256 of its
canonical form with the hash field itself omitted (`record_hash`). Both are 64 lowercase hex characters.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum
from typing import Any

from traderos.contracts.errors import ErrorCode, ValidationError

MAX_SAFE_INT = 2**53 - 1
HASH_FIELD = "canonical_hash"


def decimal_str(value: Decimal) -> str:
    """The one canonical spelling of a finite Decimal."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationError(f"not a finite Decimal: {value!r}", code=ErrorCode.INVALID_INPUT)
    if value.is_zero():
        return "0"
    with localcontext() as ctx:
        ctx.prec = max(ctx.prec, len(value.as_tuple().digits) + 2)
        text = format(value.normalize(), "f")
    return text


def parse_decimal(text: Any, *, field: str = "value") -> Decimal:
    """A Decimal from a canonical-form string (or int). Floats are refused: they have already lost precision."""
    if isinstance(text, (bool, float)):
        raise ValidationError(f"{field}: a decimal must be written as a string, not {type(text).__name__}")
    if isinstance(text, int):
        return Decimal(text)
    if not isinstance(text, str):
        raise ValidationError(f"{field}: a decimal must be a string, got {type(text).__name__}")
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise ValidationError(f"{field}: {text!r} is not a decimal number") from None
    if not value.is_finite():
        raise ValidationError(f"{field}: {text!r} is not finite")
    return value


def decimal_from_float(value: float, places: int = 12) -> Decimal | None:
    """A float from statistical code, rounded to `places` decimal places; None when it is not finite (the caller
    records the metric as unavailable with a reason instead of storing NaN or infinity)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-places))


def iso_utc(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise ValidationError(f"not a datetime: {value!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"naive datetime refused: {value.isoformat()}")
    value = value.astimezone(UTC)
    text = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        text += f".{value.microsecond:06d}"
    return text + "Z"


def parse_utc(text: Any, *, field: str = "timestamp") -> datetime:
    """An aware UTC datetime from RFC 3339 text with an explicit offset or 'Z'. Naive text is refused."""
    if isinstance(text, datetime):
        if text.tzinfo is None or text.utcoffset() is None:
            raise ValidationError(f"{field}: naive datetime refused")
        return text.astimezone(UTC)
    if not isinstance(text, str):
        raise ValidationError(f"{field}: a timestamp must be a string, got {type(text).__name__}")
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise ValidationError(f"{field}: {text!r} is not an RFC 3339 timestamp") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field}: {text!r} has no timezone; naive timestamps are refused")
    return value.astimezone(UTC)


def parse_date(text: Any, *, field: str = "date") -> date:
    if isinstance(text, date) and not isinstance(text, datetime):
        return text
    if not isinstance(text, str):
        raise ValidationError(f"{field}: a date must be a string, got {type(text).__name__}")
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise ValidationError(f"{field}: {text!r} is not a YYYY-MM-DD date") from None


def to_jsonable(value: Any) -> Any:
    """Plain JSON values in canonical form. Dataclass records go through `traderos.contracts.records.to_dict`."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise ValidationError(f"integer out of the safe range: {value}")
        return value
    if isinstance(value, float):
        raise ValidationError("float refused in a canonical record; use Decimal (decimal_from_float)")
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValidationError(f"object keys must be strings, got {type(k).__name__}")
            out[k] = to_jsonable(v)
        return out
    from traderos.contracts.records import is_record, to_dict
    if is_record(value):
        return to_dict(value)
    raise ValidationError(f"cannot serialize {type(value).__name__} canonically")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_hash(value: Any, *, omit: str = HASH_FIELD) -> str:
    """Canonical hash of one record, the hash field itself omitted (spec 05.4)."""
    plain = to_jsonable(value)
    if isinstance(plain, dict):
        plain = {k: v for k, v in plain.items() if k != omit}
    return sha256_bytes(json.dumps(plain, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                   allow_nan=False).encode("utf-8"))
