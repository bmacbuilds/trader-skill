"""The Free package has no LIVE order path and no hidden network (spec 04.1, 04.3, 21.1; ADR-012).

Since the owner's ruling of 2026-11-20 (ADR-012) there is one order path: traderos.adapters.alpaca_paper, which can
reach only Alpaca's paper endpoint. A feature flag is not isolation, so this reads the source itself:
  * no module, package or file under src/traderos is named for trading, brokers or orders;
  * nothing outside src/traderos/adapters/ imports a network library;
  * order endpoints (/v2/orders and the like) are named only in adapters/alpaca_paper.py;
  * no source line names Alpaca's live host, except the adapter's explicit refusal list (LIVE_HOSTS), and every
    other Alpaca host named anywhere is the paper host;
  * the adapter's host is a constant, the paper host, and nothing can pass it another one;
  * only `traderos ticket place` (commands/paper.py) submits an order.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re

from traderos.adapters import alpaca_paper

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "traderos"
ADAPTER = "adapters/alpaca_paper.py"
NETWORK = {"socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "websocket", "websockets", "ftplib",
           "smtplib", "alpaca", "alpaca_trade_api"}
FORBIDDEN_NAMES = re.compile(r"(^|/)(trading|broker|brokers|orders?|execution)(/|\.py$)")
ORDER_ENDPOINT = re.compile(r"/v2/orders|/v2/positions|submit_order|place_order|create_order|replace_order",
                            re.IGNORECASE)
ALPACA_HOST = re.compile(r"[A-Za-z0-9.-]*alpaca\.markets", re.IGNORECASE)
LIVE_HOST = re.compile(r"(?<![A-Za-z0-9.-])api\.alpaca\.markets", re.IGNORECASE)


def _sources():
    return sorted(SRC.rglob("*.py"))


def _lines():
    for p in _sources():
        rel = p.relative_to(SRC).as_posix()
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            yield rel, i, line


def test_no_trading_or_broker_module():
    bad = [str(p.relative_to(SRC)) for p in SRC.rglob("*") if FORBIDDEN_NAMES.search(str(p.relative_to(SRC)))]
    assert not bad, f"order-path modules are not allowed in the Free package: {bad}"


def test_network_libraries_only_inside_adapters():
    offenders = []
    for path in _sources():
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("adapters/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            offenders += [f"{rel}: {n}" for n in names if n.split(".")[0] in NETWORK]
    assert not offenders, f"network imports outside traderos/adapters: {offenders}"


def test_order_endpoints_only_in_the_paper_adapter():
    hits = [f"{rel}:{i}" for rel, i, line in _lines() if ORDER_ENDPOINT.search(line) and rel != ADAPTER]
    assert not hits, f"order endpoints named outside {ADAPTER}: {hits}"


def test_the_live_host_is_named_only_in_the_refusal_list():
    hits = [f"{rel}:{i}" for rel, i, line in _lines() if LIVE_HOST.search(line)
            and not (rel == ADAPTER and line.startswith("LIVE_HOSTS = "))]
    assert not hits, f"Alpaca's live host named outside the adapter's refusal list: {hits}"
    others = sorted({m.lower() for rel, _, line in _lines() for m in ALPACA_HOST.findall(line)
                     if not (rel == ADAPTER and line.startswith("LIVE_HOSTS = "))})
    assert others == ["paper-api.alpaca.markets"], others


def test_the_host_is_a_constant_and_the_paper_host():
    assert alpaca_paper.PAPER_HOST == "paper-api.alpaca.markets"
    assert alpaca_paper.BASE_URL == "https://paper-api.alpaca.markets"
    assert alpaca_paper.LIVE_HOSTS == ("api.alpaca.markets",)
    tree = ast.parse((SRC / ADAPTER).read_text(encoding="utf-8"))
    assigned = {t.id: node.value for node in tree.body if isinstance(node, ast.Assign) for t in node.targets
                if isinstance(t, ast.Name)}
    assert isinstance(assigned["PAPER_HOST"], ast.Constant)
    base = assigned["BASE_URL"]
    assert isinstance(base, ast.BinOp) and isinstance(base.left, ast.Constant) and base.left.value == "https://"
    assert isinstance(base.right, ast.Name) and base.right.id == "PAPER_HOST"
    # nothing reassigns them, and no request URL is built from anything but BASE_URL
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and node not in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assert not any(isinstance(t, ast.Name) and t.id in ("PAPER_HOST", "BASE_URL") for t in targets)
        if isinstance(node, ast.Global):
            assert not {"PAPER_HOST", "BASE_URL"} & set(node.names)
    call = inspect.getsource(alpaca_paper.PaperClient._call)
    assert "check_url(BASE_URL + path" in call


def test_nothing_can_pass_the_client_another_address():
    for fn in (alpaca_paper.PaperClient.__init__, alpaca_paper.client):
        params = set(inspect.signature(fn).parameters)
        assert not {p for p in params if re.search(r"url|host|base|endpoint|paper|live", p)}, (fn, params)
    source = (SRC / ADAPTER).read_text(encoding="utf-8")
    # the environment is read for the keys and for refusals only (check_environment): never for an address
    reads = re.findall(r"env\.get\(([^)]*)\)", source)
    assert sorted(set(reads)) == ["KEY_ENV[0]", "KEY_ENV[1]", "PAPER_SWITCH_ENV", "name"], reads


def test_only_ticket_place_submits_an_order():
    callers = []
    for path in _sources():
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "submit":
                callers.append(rel)
    assert callers == ["commands/paper.py"], callers
