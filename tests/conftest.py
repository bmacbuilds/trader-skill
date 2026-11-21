"""Every test runs against a throwaway TraderOS home, never the real ~/.traderos, and never reaches the network."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEROS_HOME", str(tmp_path / "traderos-home"))
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    def no_network(url, headers):
        raise AssertionError(f"a test tried to reach the network: {url}")
    from traderos.adapters import sec
    monkeypatch.setattr(sec, "_urllib_transport", no_network)

    def no_price_network(url, headers):
        raise AssertionError(f"a test tried to reach the network: {url}")
    from traderos.adapters import prices
    monkeypatch.setattr(prices, "_urllib_transport", no_price_network)

    from traderos.adapters import alpaca_paper
    for name in (*alpaca_paper.KEY_ENV, *alpaca_paper.ENDPOINT_ENV, alpaca_paper.PAPER_SWITCH_ENV):
        monkeypatch.delenv(name, raising=False)

    def no_network(method, url, headers, body):
        raise AssertionError(f"a test tried to reach the network: {method} {url}")
    monkeypatch.setattr(alpaca_paper, "_urllib_transport", no_network)
