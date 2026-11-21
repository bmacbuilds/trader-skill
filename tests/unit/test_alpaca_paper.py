"""traderos.adapters.alpaca_paper by itself (ADR-012): the transport's answers, the keys, the calls. No network: the
real transport is driven through a stand-in opener, and the client through FakeAlpaca."""
from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from tests.unit.alpaca_fake import KEY_ID, SECRET, FakeAlpaca
from traderos import settings
from traderos.adapters import alpaca_paper as ap
from traderos.contracts import AuthorizationError, ErrorCode, TraderOSError

REAL_TRANSPORT = ap._urllib_transport                    # captured at import, before conftest's no-network guard
URL = "https://paper-api.alpaca.markets/v2/orders"


def client(fake):
    return ap.PaperClient(KEY_ID, SECRET, transport=fake)


# ---- the real transport, through a stand-in opener ------------------------------------------------------------------
class _Resp(io.BytesIO):
    def __init__(self, status, data):
        super().__init__(data)
        self.status = status


def opener(monkeypatch, outcome):
    seen = []

    def open_(req, timeout=None):
        seen.append((req.get_method(), req.full_url, dict(req.header_items()), req.data, timeout))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(ap._OPENER, "open", open_)
    return seen


def test_the_transport_sends_to_the_paper_url_with_a_timeout(monkeypatch):
    seen = opener(monkeypatch, _Resp(200, b'{"id": "x"}'))
    assert REAL_TRANSPORT("POST", URL, {"A": "b"}, b"{}") == (200, b'{"id": "x"}')
    assert seen == [("POST", URL, {"A": "b"}, b"{}", ap.TIMEOUT_S)]


def test_an_http_error_is_alpacas_answer(monkeypatch):
    err = urllib.error.HTTPError(URL, 403, "Forbidden", {}, io.BytesIO(b'{"message": "no"}'))
    opener(monkeypatch, err)
    assert REAL_TRANSPORT("POST", URL, {}, b"{}") == (403, b'{"message": "no"}')


@pytest.mark.parametrize("reason", [socket.gaierror("dns"), ConnectionRefusedError("refused")])
def test_not_reaching_alpaca_sent_nothing(monkeypatch, reason):
    opener(monkeypatch, urllib.error.URLError(reason))
    with pytest.raises(TraderOSError) as e:
        REAL_TRANSPORT("POST", URL, {}, b"{}")
    assert e.value.code is ErrorCode.MISSING_DATA and "nothing was sent" in e.value.message


@pytest.mark.parametrize("error", [urllib.error.URLError(TimeoutError("timed out")), TimeoutError("read"),
                                   ConnectionResetError("reset")])
def test_a_timeout_or_a_reset_is_indeterminate(monkeypatch, error):
    opener(monkeypatch, error)
    with pytest.raises(ap.Indeterminate) as e:
        REAL_TRANSPORT("POST", URL, {}, b"{}")
    assert e.value.code is ErrorCode.BROKER_INDETERMINATE


def test_a_big_answer_is_capped(monkeypatch):
    opener(monkeypatch, _Resp(200, b"x" * (ap.MAX_BYTES + 10)))
    _, data = REAL_TRANSPORT("GET", URL, {}, None)
    assert len(data) == ap.MAX_BYTES + 1
    fake = FakeAlpaca()
    big = b"[" + b" " * ap.MAX_BYTES + b"]"
    with pytest.raises(TraderOSError) as e:
        ap.PaperClient(KEY_ID, SECRET, transport=lambda *a: (200, big)).open_orders("TSLA")
    assert e.value.code is ErrorCode.RESOURCE_BUDGET_EXCEEDED
    with pytest.raises(ap.Indeterminate):
        ap.PaperClient(KEY_ID, SECRET, transport=lambda *a: (200, big)).submit({"symbol": "TSLA"})
    assert fake.calls == []


# ---- the client's answers -------------------------------------------------------------------------------------------
def test_submit_answers():
    assert client(FakeAlpaca()).submit({"symbol": "KO", "side": "buy", "qty": "1", "type": "market",
                                        "time_in_force": "day"})["status"] == "accepted"
    with pytest.raises(ap.Rejected) as e:
        client(FakeAlpaca(submit=(422, {"message": "qty must be > 0"}))).submit({})
    assert e.value.code is ErrorCode.INVALID_INPUT and "qty must be > 0" in e.value.message
    for answer in ((500, {"message": "boom"}), (200, {"status": "accepted"}), (302, {})):
        with pytest.raises(ap.Indeterminate):
            client(FakeAlpaca(submit=answer)).submit({})
    with pytest.raises(TraderOSError) as e:
        client(lambda *a: (200, b"not json")).submit({})
    assert e.value.code is ErrorCode.INTEGRITY_MISMATCH


def test_get_cancel_and_list():
    fake = FakeAlpaca()
    c = client(fake)
    o = c.submit({"symbol": "TSLA", "side": "buy", "qty": "8", "type": "market", "time_in_force": "day",
                  "order_class": "bracket", "take_profit": {"limit_price": "370.04"},
                  "stop_loss": {"stop_price": "358.32"}})
    assert c.order(o["id"])["id"] == o["id"]
    assert fake.calls[-1][1] == f"{URL}/{o['id']}?nested=true"
    found = c.open_orders("tsla")
    assert [x["side"] for x in found] == ["buy", "sell", "sell"]           # the entry and its two legs
    legs = found[1:]
    assert fake.calls[-1][1] == URL + "?status=open&symbols=TSLA&limit=500"
    assert c.cancel(legs[0]["id"]) is True and c.cancel(legs[0]["id"]) is False         # 204, then 422
    fake.cancel_answer = (500, {})
    with pytest.raises(ap.Indeterminate):
        c.cancel(legs[1]["id"])
    fake.cancel_answer = (403, {"message": "forbidden"})
    with pytest.raises(ap.Rejected):
        c.cancel(legs[1]["id"])
    n = len(fake.calls)
    for bad in ("../account", "x", "a b", "1" * 80):
        with pytest.raises(TraderOSError):
            c.order(bad)
        with pytest.raises(TraderOSError):
            c.cancel(bad)
    with pytest.raises(TraderOSError):
        c.open_orders("TSLA,AAPL")
    assert len(fake.calls) == n                                  # refused before any call
    with pytest.raises(TraderOSError) as e:
        c.order("b7d0e2c1-6a4f-4d3b-9e8a-999999999999")
    assert e.value.code is ErrorCode.NOT_AUTHORIZED and "(404)" in e.value.message


def test_the_keys_go_in_alpacas_two_headers_only():
    fake = FakeAlpaca()
    c = client(fake)
    c.account()
    (_, _, headers, body), = fake.calls
    assert headers == {"APCA-API-KEY-ID": KEY_ID, "APCA-API-SECRET-KEY": SECRET, "Accept": "application/json"}
    assert body is None and KEY_ID not in repr(c) and SECRET not in repr(c)
    fake.submit = (403, {"message": f"bad key {KEY_ID} {SECRET}"})
    with pytest.raises(ap.Rejected) as e:
        c.submit({})
    assert KEY_ID not in e.value.message and SECRET not in e.value.message and "****" in e.value.message


# ---- the keys --------------------------------------------------------------------------------------------------------
def test_keys_from_the_environment_or_the_settings(tmp_path):
    env = {"APCA_API_KEY_ID": KEY_ID, "APCA_API_SECRET_KEY": SECRET}
    assert ap.credentials(None, env) == (KEY_ID, SECRET)
    with pytest.raises(AuthorizationError) as e:
        ap.credentials(tmp_path, {})
    assert e.value.code is ErrorCode.NOT_AUTHORIZED and "traderos config set alpaca_paper_key_id" in e.value.message
    settings.write(tmp_path, "alpaca_paper_key_id", "PKFROMSETTINGS1234")
    with pytest.raises(AuthorizationError):
        ap.credentials(tmp_path, {})                             # half a pair is no pair
    settings.write(tmp_path, "alpaca_paper_secret_key", "settings-secret")
    assert ap.credentials(tmp_path, {}) == ("PKFROMSETTINGS1234", "settings-secret")
    assert ap.credentials(tmp_path, {"APCA_API_KEY_ID": KEY_ID}) == ("PKFROMSETTINGS1234", "settings-secret")
    assert ap.credentials(tmp_path, env) == (KEY_ID, SECRET)       # the environment wins
    assert (tmp_path / "config" / "settings.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(TraderOSError):
        ap.credentials(None, {"APCA_API_KEY_ID": "PK1", "APCA_API_SECRET_KEY": "a\nb"})
    with pytest.raises(TraderOSError):
        settings.write(tmp_path, "alpaca_paper_secret_key", "has space")
    with pytest.raises(TraderOSError):
        settings.write(tmp_path, "sec_user_agent", "x")


def test_the_environment_is_checked_before_the_keys():
    env = {"APCA_API_KEY_ID": KEY_ID, "APCA_API_SECRET_KEY": SECRET, "APCA_API_BASE_URL": "https://api.alpaca.markets"}
    with pytest.raises(ap.LiveRefused):
        ap.credentials(None, env)
    ap.check_environment({"APCA_API_BASE_URL": "paper-api.alpaca.markets/v2", "ALPACA_PAPER_TRADE": "true"})
    ap.check_environment({"ALPACA_PAPER_TRADE": "1"})


def test_masks():
    assert settings.mask("alpaca_paper_key_id", KEY_ID) == "set (...6789)"
    assert settings.mask("alpaca_paper_secret_key", SECRET) == "set (hidden)"
    assert settings.mask("alpaca_paper_key_id", "PKSHORT") == "set (hidden)"
    assert settings.mask("alpaca_paper_key_id", "") == "not set"
    assert ap.mask_account("PA3ABCDEFGH1") == "PA...FGH1" and ap.mask_account("") == "(none)"
    assert json.dumps(ap.order_body({"symbol": "X"})) == '{"symbol": "X"}'


@pytest.mark.parametrize("method", ["POST", "DELETE", "PATCH"])
def test_a_redirect_to_an_order_or_a_cancel_is_indeterminate_never_not_placed(method):
    """OSDev6's #10 review: a redirect is an answer, so the request reached Alpaca. An order or a cancel answered with a
    redirect has an unknown outcome: it must be Indeterminate (the write-ahead claim stands), never "failed"."""
    import urllib.request
    req = urllib.request.Request(ap.BASE_URL + "/v2/orders", data=b"{}", method=method)
    with pytest.raises(ap.Indeterminate) as e:
        ap._NoRedirect().redirect_request(req, None, 307, "Temporary Redirect", {}, "https://elsewhere.example/v2/x")
    assert e.value.code is ErrorCode.BROKER_INDETERMINATE and not isinstance(e.value, ap.LiveRefused)


def test_a_redirect_to_a_read_changes_nothing_and_is_followed_nowhere():
    import urllib.request
    req = urllib.request.Request(ap.BASE_URL + "/v2/account", method="GET")
    with pytest.raises(TraderOSError) as e:
        ap._NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://elsewhere.example/v2/account")
    assert e.value.code is ErrorCode.MISSING_DATA and not isinstance(e.value, (ap.Indeterminate, ap.LiveRefused))
