"""Alpaca's paper endpoint, in memory, for the order-path tests (ADR-012). It records every call the client makes and
never touches the network. The order and account shapes are the ones in Alpaca's OpenAPI spec (the trading-api.json
shipped with alpaca-mcp-server 2.4.1: the BracketOrderResponse and the Account examples)."""
from __future__ import annotations

import json
import urllib.parse

KEY_ID = "PKTESTKEYID0123456789"
SECRET = "s3cret-PAPER-only-zZ9x8w7v6u5t4s3r2q1p"
ACCOUNT = {"account_blocked": False, "account_number": "PALPACA_123", "buying_power": "245432.61",
           "cash": "122086.5", "currency": "USD", "equity": "123346.11", "id": "1d9eed04-be39-4e01-9b84-a48ac5bbafcf",
           "pattern_day_trader": False, "status": "ACTIVE", "trading_blocked": False, "daytrade_count": 0}


class FakeAlpaca:
    def __init__(self, *, account=None, submit=None):
        self.calls = []                      # (method, url, headers, body bytes or None)
        self.account = dict(ACCOUNT if account is None else account)
        self.orders = {}
        self.submit = submit                 # (status, body) to answer a POST with, instead of accepting it
        self.cancel_answer = None            # (status, body) to answer a DELETE with
        self.cancel_to = "canceled"          # the status a canceled order ends in
        self.n = 0

    # ---- what the client sees ----
    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        parts = urllib.parse.urlsplit(url)
        path, query = parts.path, dict(urllib.parse.parse_qsl(parts.query))
        if method == "GET" and path == "/v2/account":
            return 200, json.dumps(self.account).encode()
        if method == "POST" and path == "/v2/orders":
            if self.submit is not None:
                status, answer = self.submit
                return status, json.dumps(answer).encode()
            return 200, json.dumps(self._accept(json.loads(body))).encode()
        if method == "GET" and path == "/v2/orders":
            sym = query.get("symbols", "")
            return 200, json.dumps([o for o in self._all() if o["symbol"] == sym and o["status"] in
                                    ("new", "accepted", "held", "partially_filled")]).encode()
        if path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            order = self._find(oid)
            if order is None:
                return 404, b'{"code": 40410000, "message": "order not found"}'
            if method == "GET":
                return 200, json.dumps(order).encode()
            if method == "DELETE":
                if self.cancel_answer is not None:
                    status, answer = self.cancel_answer
                    return status, json.dumps(answer).encode()
                if order["status"] not in ("new", "accepted", "held"):
                    return 422, b'{"message": "order is not cancelable"}'
                order["status"] = self.cancel_to
                return 204, b""
        return 404, b'{"message": "not found"}'

    # ---- helpers for the tests ----
    def _accept(self, req):
        self.n += 1
        oid = f"b7d0e2c1-6a4f-4d3b-9e8a-{self.n:012d}"
        order = {"id": oid, "client_order_id": f"c-{self.n}", "symbol": req["symbol"], "side": req["side"],
                 "qty": req.get("qty"), "type": req["type"], "order_type": req["type"],
                 "time_in_force": req["time_in_force"], "order_class": req.get("order_class", "simple"),
                 "status": "accepted", "filled_qty": "0", "filled_avg_price": None, "legs": None}
        if req.get("order_class") == "bracket":
            other = "sell" if req["side"] == "buy" else "buy"
            order["legs"] = [
                {"id": f"{oid[:-4]}{self.n:02d}01", "symbol": req["symbol"], "side": other, "qty": req.get("qty"),
                 "type": "limit", "limit_price": req["take_profit"]["limit_price"], "status": "held",
                 "filled_qty": "0", "filled_avg_price": None, "group_role": "take_profit"},
                {"id": f"{oid[:-4]}{self.n:02d}02", "symbol": req["symbol"], "side": other, "qty": req.get("qty"),
                 "type": "stop", "stop_price": req["stop_loss"]["stop_price"], "status": "held", "filled_qty": "0",
                 "filled_avg_price": None, "group_role": "stop_loss"}]
        self.orders[oid] = order
        return order

    def _all(self):
        for o in self.orders.values():
            yield o
            yield from (o.get("legs") or [])

    def _find(self, oid):
        return next((o for o in self._all() if o["id"] == oid), None)

    def fill(self, oid, qty, price):
        order = self._find(oid)
        order.update(status="filled", filled_qty=str(qty), filled_avg_price=str(price))
        if order.get("legs"):
            for leg in order["legs"]:
                leg["status"] = "new"
        return order

    def sent(self, method="POST"):
        return [c for c in self.calls if c[0] == method]


def install(monkeypatch, fake=None, *, now=None, keys=True):
    """Route the client to `fake`, set paper keys in the environment and pin place's real clock."""
    from traderos.adapters import alpaca_paper
    from traderos.commands import paper
    fake = fake or FakeAlpaca()
    monkeypatch.setattr(alpaca_paper, "_urllib_transport", fake)
    if keys:
        monkeypatch.setenv("APCA_API_KEY_ID", KEY_ID)
        monkeypatch.setenv("APCA_API_SECRET_KEY", SECRET)
    if now is not None:
        monkeypatch.setattr(paper, "_real_now", lambda: now)
    return fake
