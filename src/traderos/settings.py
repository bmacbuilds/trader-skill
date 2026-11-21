"""Local settings: `<home>/config/settings.json`, readable by its owner only (0600), written by `traderos config set`.

A secret setting (the Alpaca PAPER key id and secret, ADR-012) is never printed: `config show` and `traderos doctor`
show it masked, and no record, journal entry or output carries it. The file is written atomically and created
owner-only, so there is no moment when another user could read it. A key other than the settings is refused on read,
named but never shown with its value (review of #10, second round): a hand-added one was kept and silently ignored.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import tempfile

from traderos.contracts import DataError, ErrorCode, ValidationError

SECRETS = ("alpaca_paper_key_id", "alpaca_paper_secret_key")
SEC_USER_AGENT = "sec_user_agent"            # the person's own "Name email" for SEC EDGAR (ADR-011); not secret
KEYS = (*SECRETS, SEC_USER_AGENT)
MAX_VALUE = 256


def path(home: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(home) / "config" / "settings.json"


def read(home: pathlib.Path) -> dict:
    p = path(home)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError, OSError):
        data = None
    if not isinstance(data, dict):
        raise DataError(f"{p} is not a settings file TraderOS wrote; fix or delete it",
                        code=ErrorCode.INTEGRITY_MISMATCH)
    unknown = sorted(str(k)[:40] for k in data if k not in KEYS)
    if unknown:
        # a hand-added key (a base_url, say) was kept and silently ignored; it is refused, named, never its value
        # (review of #10, second round)
        raise DataError(f"{p} has unknown setting(s) {', '.join(repr(k) for k in unknown)}: the settings are "
                        f"{', '.join(KEYS)}. Remove the others; TraderOS reads nothing else from this file",
                        code=ErrorCode.INTEGRITY_MISMATCH)
    return data


def write(home: pathlib.Path, key: str, value: str) -> pathlib.Path:
    if key not in KEYS:
        raise ValidationError(f"unknown setting {key!r}; the settings are: {', '.join(KEYS)}")
    value = str(value).strip()
    if key == SEC_USER_AGENT:                 # a name and an email: spaces allowed, never the help text's example
        from traderos.adapters.sec import is_placeholder
        if "@" not in value or len(value) > 200 or is_placeholder(value) or any(ch in value for ch in "\r\n\t\x00"):
            raise ValidationError('sec_user_agent is your name and email, e.g. "Jane Doe jane@example.com"')
    elif not value or len(value) > MAX_VALUE or any(ch in value for ch in "\r\n\t\x00 "):
        raise ValidationError(f"{key} must be 1 to {MAX_VALUE} characters with no spaces or line breaks")
    p = path(home)
    data = read(home)
    data[key] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".settings-")     # mkstemp creates it 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, p)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    with contextlib.suppress(OSError):
        p.chmod(0o600)
    return p


def mask(key: str, value: object) -> str:
    """A secret as "set (...1234)" for a key id and "set (hidden)" for a secret key; anything else as it is."""
    if key not in SECRETS:
        return str(value)
    text = str(value or "")
    if not text:
        return "not set"
    if key.endswith("secret_key") or len(text) < 12:
        return "set (hidden)"
    return f"set (...{text[-4:]})"


__all__ = ["KEYS", "SECRETS", "SEC_USER_AGENT", "mask", "path", "read", "write"]
