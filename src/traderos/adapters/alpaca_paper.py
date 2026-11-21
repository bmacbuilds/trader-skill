"""Alpaca PAPER trading: the one order path in TraderOS, and it can reach only Alpaca's paper endpoint (ADR-012).

The owner's ruling (OWNER post by OSDev1 on the wall, 2026-11-20 14:50 UTC; Brian's answer "Y"): the free product
keeps the upstream paper orders, paper accounts only, live refused, "live is refused in code with a test that proves
it".

The upstream trader-broker never sent anything itself: Claude called a separately installed Alpaca MCP server's
`place_stock_order`, and that server chose paper or live from ITS environment (ALPACA_PAPER_TRADE), outside any code
TraderOS can test. So TraderOS sends the same order itself, through this client, which:

  * contacts ONE host, paper-api.alpaca.markets, over HTTPS. The address is a constant: no argument, setting or
    environment variable can change it. Every URL is checked before the transport is called; any other host, the
    live host by name included, is refused (LiveRefused, NOT_AUTHORIZED) and nothing is sent;
  * refuses to start when the environment points Alpaca anywhere else (APCA_API_BASE_URL and the like) or asks for
    live (ALPACA_PAPER_TRADE other than true), and when the key id is a live key id (Alpaca's live key ids start
    with AK, its paper ones with PK: observed, not documented, so it is a second refusal, never the guarantee);
  * follows no redirect: a redirect would carry the keys to another host;
  * checks the account it reached. Alpaca's account object has no documented paper flag (its OpenAPI `Account` has
    none), so the guarantee is the host; on top of it, an account number that does not start with "PA" (Alpaca's
    paper numbering, observed, not documented) is refused, which can only stop an order, never send one;
  * uses the person's own PAPER keys (APCA_API_KEY_ID and APCA_API_SECRET_KEY, else `traderos config set
    alpaca_paper_key_id ...` / `alpaca_paper_secret_key ...`), sends them only in Alpaca's two headers, and never
    puts them in a message, a record, the journal or any output;
  * has a timeout and a size cap; a send that times out is INDETERMINATE (BROKER_INDETERMINATE), never "not placed".

The order body is the upstream `tool_input` (its ALPACA_FIELDS, unchanged) as alpaca-mcp-server's `place_stock_order`
turned it into Alpaca's REST body (alpaca-mcp-server 2.4.1, overrides.py, place_stock_order; upstream checked 2.4.0):
the bracket legs `take_profit_limit_price`, `stop_loss_stop_price` and `stop_loss_limit_price` are not REST fields
and become the `take_profit` and `stop_loss` objects; every other field goes as it is, in the upstream order.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from traderos.contracts import AuthorizationError, DataError, ErrorCode, ValidationError

PAPER_HOST = "paper-api.alpaca.markets"
BASE_URL = "https://" + PAPER_HOST
LIVE_HOSTS = ("api.alpaca.markets",)              # Alpaca's live trading host: refused by name (and like any host)
ENDPOINT_ENV = ("APCA_API_BASE_URL", "ALPACA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_TRADING_API_URL",
                "ALPACA_ENDPOINT", "APCA_ENDPOINT")
PAPER_SWITCH_ENV = "ALPACA_PAPER_TRADE"           # the MCP server's switch; anything but true is a request for live
KEY_ENV = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
KEY_SETTINGS = ("alpaca_paper_key_id", "alpaca_paper_secret_key")
LIVE_KEY_PREFIX = "AK"
PAPER_ACCOUNT_PREFIX = "PA"
LEG_FIELDS = ("take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
TIMEOUT_S = 15
MAX_BYTES = 1024 * 1024
ORDER_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")
SYMBOL = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")
DONE = ("filled", "canceled", "expired", "rejected", "done_for_day", "replaced")      # no further fills
HOW_TO_SET = ("Alpaca paper keys are not set. Make PAPER trading keys in your Alpaca dashboard (paper keys are "
              "separate from live keys), then either set APCA_API_KEY_ID and APCA_API_SECRET_KEY in your "
              "environment, or run `traderos config set alpaca_paper_key_id <key id>` and `traderos config set "
              "alpaca_paper_secret_key <secret>` (kept owner-only in your TraderOS folder). Never paste a key into a "
              "chat.")
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]   # method, url, headers, body


class LiveRefused(AuthorizationError):
    """Anything that is not Alpaca's paper endpoint. Always raised before a request is made."""

    def __init__(self, why: str) -> None:
        super().__init__("live trading is refused in this build: " + why, code=ErrorCode.NOT_AUTHORIZED)


class Indeterminate(DataError):
    """The request may have reached Alpaca: never assume it did not."""
    default_code = ErrorCode.BROKER_INDETERMINATE


class Rejected(DataError):
    """Alpaca answered and refused: nothing was placed or changed."""
    default_code = ErrorCode.NOT_AUTHORIZED


# ---- the one address ---------------------------------------------------------------------------------------------
def check_url(url: str) -> str:
    """`url` if it is https://paper-api.alpaca.markets/v2/..., else LiveRefused. Called before every request."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host in LIVE_HOSTS:
        raise LiveRefused(f"{host} is Alpaca's live endpoint; TraderOS sends only to {PAPER_HOST}. Nothing was sent")
    if (parts.scheme != "https" or host != PAPER_HOST or parts.port is not None or parts.username is not None
            or parts.password is not None or not parts.path.startswith("/v2/")):
        raise LiveRefused(f"{url!r} is not Alpaca's paper endpoint ({BASE_URL}/v2/...). Nothing was sent")
    return url


def check_environment(env: Mapping[str, str] | None = None) -> None:
    """Refuse when the environment points Alpaca elsewhere or asks for live. TraderOS never READS an address from the
    environment; a stray one is refused so nobody believes it is in use."""
    env = os.environ if env is None else env
    for name in ENDPOINT_ENV:
        value = str(env.get(name) or "").strip()
        if not value:
            continue
        host = (urllib.parse.urlsplit(value if "://" in value else "https://" + value).hostname or "").lower()
        if host != PAPER_HOST:
            raise LiveRefused(f"{name} points Alpaca at {host or value!r}. TraderOS sends only to {PAPER_HOST} and "
                              f"will not run while another endpoint is configured: unset {name}")
    switch = env.get(PAPER_SWITCH_ENV)
    if switch is not None and switch.strip().lower() not in ("true", "1", "yes"):
        raise LiveRefused(f"{PAPER_SWITCH_ENV} is {switch!r}, which asks for live. Unset it or set it to true")


def credentials(home: pathlib.Path | None = None, env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """The person's own PAPER key id and secret, or AuthorizationError saying how to set them. Never printed."""
    env = os.environ if env is None else env
    check_environment(env)
    key, secret = str(env.get(KEY_ENV[0]) or "").strip(), str(env.get(KEY_ENV[1]) or "").strip()
    if not (key and secret) and home is not None:
        from traderos import settings
        saved = settings.read(home)
        key = str(saved.get(KEY_SETTINGS[0]) or "").strip()
        secret = str(saved.get(KEY_SETTINGS[1]) or "").strip()
    if not key or not secret:
        raise AuthorizationError(HOW_TO_SET, code=ErrorCode.NOT_AUTHORIZED)
    if key.upper().startswith(LIVE_KEY_PREFIX):
        raise LiveRefused("that key id is a LIVE key id (Alpaca's live key ids start with AK, its paper ones with "
                          "PK). Use your paper keys")
    if len(key) > 128 or len(secret) > 256 or any(not ch.isprintable() or ch.isspace() for ch in key + secret):
        raise ValidationError("the Alpaca key id or secret has spaces, line breaks or is too long; set it again")
    return key, secret


def require_paper_account(account: Any) -> dict:
    """The account the paper endpoint answered with, if it looks like a paper account; else LiveRefused."""
    if not isinstance(account, dict):
        raise DataError("Alpaca's paper endpoint did not answer with an account", code=ErrorCode.INTEGRITY_MISMATCH)
    number = str(account.get("account_number") or "")
    if not number.upper().startswith(PAPER_ACCOUNT_PREFIX):
        raise LiveRefused(f"the account at the paper endpoint is numbered {mask_account(number)}, not like a paper "
                          f"account ({PAPER_ACCOUNT_PREFIX}...). Nothing was sent")
    return account


def mask_account(number: str) -> str:
    text = str(number or "")
    return "(none)" if not text else (text[:2] + "..." + text[-4:] if len(text) > 8 else text[:2] + "...")


# ---- the body: the upstream tool_input, as the MCP server posted it
# --------------------------------------------------------
def order_body(args: Mapping[str, Any]) -> dict[str, Any]:
    """The upstream tool_input as Alpaca's REST body, the way alpaca-mcp-server's place_stock_order built it: the three
    leg arguments become `take_profit` / `stop_loss`, an order with legs and no class is a bracket, and every other
    field is sent as it is."""
    if args.get("stop_loss_limit_price") is not None and args.get("stop_loss_stop_price") is None:
        raise ValidationError("stop_loss_limit_price requires stop_loss_stop_price")       # the server's own refusal
    legs = args.get("take_profit_limit_price") is not None or args.get("stop_loss_stop_price") is not None
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k == "take_profit_limit_price" and v is not None:
            out["take_profit"] = {"limit_price": v}
        elif k == "stop_loss_stop_price" and v is not None:
            out["stop_loss"] = {"stop_price": v}
            if args.get("stop_loss_limit_price") is not None:
                out["stop_loss"]["limit_price"] = args["stop_loss_limit_price"]
        elif k not in LEG_FIELDS:
            out[k] = v
    if legs and out.get("order_class") is None:
        out["order_class"] = "bracket"
    return out


def tool_args(body: Mapping[str, Any]) -> dict[str, Any]:
    """The inverse of order_body: the REST body back as place_stock_order's arguments (for tests and the log)."""
    out: dict[str, Any] = {}
    for k, v in body.items():
        if k == "take_profit":
            out["take_profit_limit_price"] = v["limit_price"]
        elif k == "stop_loss":
            out["stop_loss_stop_price"] = v["stop_price"]
            if "limit_price" in v:
                out["stop_loss_limit_price"] = v["limit_price"]
        else:
            out[k] = v
    return out


def body_bytes(body: Mapping[str, Any]) -> bytes:
    """The exact bytes sent: compact JSON, keys in the order given (the upstream ALPACA_FIELDS order)."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")


# ---- the transport -----------------------------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed (it would carry the keys to another host). It is an ANSWER, so the request reached
    Alpaca: for an order or a cancel the outcome is unknown (Indeterminate, never "not placed"); a read changed
    nothing (OSDev6's #10 review)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        why = (f"the paper endpoint answered with a redirect ({code}); TraderOS follows none, so your keys go "
               "nowhere else")
        if req.get_method() not in ("GET", "HEAD"):
            raise Indeterminate(f"{why}. The request reached Alpaca, so its outcome is unknown: check your paper "
                                "account before doing anything else")
        raise DataError(f"{why}. Nothing was changed", code=ErrorCode.MISSING_DATA)


_OPENER = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(
    context=ssl.create_default_context()))


def _read(resp) -> bytes:
    return resp.read(MAX_BYTES + 1)


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    check_url(url)                                # again, here: the transport itself never reaches another host
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            return resp.status, _read(resp)
    except urllib.error.HTTPError as e:           # Alpaca answered: its status and its (capped) words
        try:
            data = _read(e)
        except (OSError, AttributeError, ValueError):
            data = b""
        finally:
            e.close()
        return e.code, data
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError)):
            raise DataError(f"could not reach Alpaca's paper endpoint ({type(e.reason).__name__}); nothing was sent",
                            code=ErrorCode.MISSING_DATA) from None
        raise Indeterminate(f"no answer from Alpaca's paper endpoint ({type(e.reason).__name__})") from None
    except (TimeoutError, OSError) as e:
        raise Indeterminate(f"no answer from Alpaca's paper endpoint ({type(e).__name__})") from None


# ---- the client --------------------------------------------------------------------------------------------------
class PaperClient:
    """Alpaca's paper trading API, the few calls the upstream flow needs: the account, submit, get, list open,
    cancel."""

    def __init__(self, key_id: str, secret: str, *, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._key = key_id
        self._secret = secret
        self.transport = transport or _urllib_transport
        self.sleep = sleep

    def __repr__(self) -> str:
        return f"PaperClient({BASE_URL}, keys hidden)"

    def _scrub(self, text: str) -> str:
        for s in (self._secret, self._key):
            if s:
                text = text.replace(s, "****")
        return text

    def _call(self, method: str, path: str, *, query: dict[str, str] | None = None,
              body: Mapping[str, Any] | None = None) -> tuple[int, bytes]:
        url = check_url(BASE_URL + path + ("?" + urllib.parse.urlencode(query) if query else ""))
        headers = {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret, "Accept": "application/json"}
        data = None
        if body is not None:
            data = body_bytes(body)
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self.transport(method, url, headers, data)
        except Indeterminate as e:
            if method == "GET":
                raise DataError(self._scrub(e.message) + "; nothing was changed", code=ErrorCode.MISSING_DATA) from None
            raise
        if len(raw) > MAX_BYTES:
            if method == "POST":
                raise Indeterminate("Alpaca's paper endpoint answered with more than "
                                    f"{MAX_BYTES} bytes: the order may or may not have been placed")
            raise DataError(f"Alpaca's paper endpoint answered with more than {MAX_BYTES} bytes",
                            code=ErrorCode.RESOURCE_BUDGET_EXCEEDED)
        return status, raw

    def _json(self, raw: bytes, what: str):
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise DataError(f"Alpaca's paper endpoint answered {what} with something that is not JSON",
                            code=ErrorCode.INTEGRITY_MISMATCH) from None

    def _refused(self, status: int, raw: bytes, what: str) -> Rejected:
        try:
            detail = json.loads(raw.decode("utf-8")).get("message", "")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, RecursionError):
            detail = ""
        code = ErrorCode.INVALID_INPUT if status == 422 else ErrorCode.NOT_AUTHORIZED
        text = f"Alpaca's paper endpoint refused {what} ({status})" + (f": {str(detail)[:200]}" if detail else "")
        return Rejected(self._scrub(text), code=code)

    def _ok(self, status: int, raw: bytes, what: str):
        if 200 <= status < 300:
            return self._json(raw, what)
        if 400 <= status < 500:
            raise self._refused(status, raw, what)
        raise DataError(f"Alpaca's paper endpoint answered {status} for {what}; nothing was changed",
                        code=ErrorCode.MISSING_DATA)

    # ---- the calls ----
    def account(self) -> dict:
        """GET the paper account, checked to be one (require_paper_account)."""
        return require_paper_account(self._ok(*self._call("GET", "/v2/account"), "the account"))

    def submit(self, body: Mapping[str, Any]) -> dict:
        """POST one order. 2xx: Alpaca's order. 4xx: Rejected (nothing was placed). Anything else: Indeterminate."""
        status, raw = self._call("POST", "/v2/orders", body=body)
        if 200 <= status < 300:
            order = self._json(raw, "the order")
            if not isinstance(order, dict) or not ORDER_ID.match(str(order.get("id", ""))):
                raise Indeterminate("Alpaca's paper endpoint accepted the order without an order id: it may or may "
                                    "not have been placed")
            return order
        if 400 <= status < 500:
            raise self._refused(status, raw, "the order")
        raise Indeterminate(f"Alpaca's paper endpoint answered {status}: the order may or may not have been placed")

    def order(self, order_id: str) -> dict:
        """GET one order with its legs."""
        order = self._ok(*self._call("GET", "/v2/orders/" + _order_id(order_id), query={"nested": "true"}),
                         "the order")
        if not isinstance(order, dict):
            raise DataError("Alpaca's paper endpoint did not answer with an order", code=ErrorCode.INTEGRITY_MISMATCH)
        return order

    def open_orders(self, symbol: str) -> list[dict]:
        """GET the open orders for one symbol (each bracket leg is its own order)."""
        sym = str(symbol).strip().upper()
        if not SYMBOL.match(sym):
            raise ValidationError(f"{symbol!r} is not a stock ticker")
        orders = self._ok(*self._call("GET", "/v2/orders", query={"status": "open", "symbols": sym, "limit": "500"}),
                          "the open orders")
        if not isinstance(orders, list) or not all(isinstance(o, dict) for o in orders):
            raise DataError("Alpaca's paper endpoint did not answer with a list of orders",
                            code=ErrorCode.INTEGRITY_MISMATCH)
        return [o for o in orders if str(o.get("symbol", "")).upper() == sym]

    def cancel(self, order_id: str) -> bool:
        """DELETE one order: True when Alpaca accepted the cancel, False when the order is no longer cancelable."""
        status, raw = self._call("DELETE", "/v2/orders/" + _order_id(order_id))
        if 200 <= status < 300:
            return True
        if status == 422:
            return False
        if 400 <= status < 500:
            raise self._refused(status, raw, "the cancel")
        raise Indeterminate(f"Alpaca's paper endpoint answered {status} to a cancel: it may or may not be canceled")


def _order_id(order_id: str) -> str:
    text = str(order_id).strip()
    if not ORDER_ID.match(text):
        raise ValidationError(f"{order_id!r} is not an Alpaca order id")
    return text


def client(home: pathlib.Path | None = None, *, transport: Transport | None = None,
           env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep) -> PaperClient:
    """A client with the person's own paper keys (credentials() refuses first, before any request)."""
    key, secret = credentials(home, env)
    return PaperClient(key, secret, transport=transport, sleep=sleep)


__all__ = ["BASE_URL", "DONE", "KEY_ENV", "KEY_SETTINGS", "LIVE_HOSTS", "PAPER_HOST", "Indeterminate", "LiveRefused",
           "PaperClient", "Rejected", "body_bytes", "check_environment", "check_url", "client", "credentials",
           "mask_account", "order_body", "require_paper_account", "tool_args"]
