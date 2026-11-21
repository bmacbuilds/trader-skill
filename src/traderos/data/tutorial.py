"""The bundled tutorial dataset (spec 06.1 Explore): synthetic, labelled, no login, no network.

Seven made-up instruments (the TUT tickers are fictional on purpose, so no tutorial price can be mistaken for a
real one), daily bars on real XNYS sessions from 2016 to 2025. Prices follow a random walk with drift and two
volatility regimes, from an integer-only generator, so the dataset is byte-identical on every operating system and
Python version. tests/unit/test_data.py pins its hash.

It goes through the same CSV import and quality gate as a customer's file, and its manifest says SYNTHETIC_DATA, so
every result built on it is labelled tutorial-only. It is never evidence about any market.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from traderos.clock import XNYS
from traderos.contracts import SourceKind
from traderos.data import ImportOptions

START, END = date(2016, 1, 4), date(2025, 12, 31)
SEED = 20261123
# symbol: (description, start price, drift per day in basis points x100, calm vol bp, stressed vol bp, volume)
UNIVERSE = {
    "TUTMKT": ("synthetic broad market", 200, 4, 80, 220, 60_000_000),
    "TUTTEC": ("synthetic technology", 110, 6, 120, 300, 40_000_000),
    "TUTSML": ("synthetic small caps", 120, 3, 130, 320, 25_000_000),
    "TUTFIN": ("synthetic financials", 24, 3, 110, 330, 35_000_000),
    "TUTGLD": ("synthetic gold", 105, 2, 90, 160, 8_000_000),
    "TUTSLV": ("synthetic silver", 14, 1, 160, 280, 15_000_000),
    "TUTBND": ("synthetic long bonds", 120, 0, 60, 120, 12_000_000),
}
NAME = "Tutorial: seven synthetic instruments, daily, 2016-2025"
OPTIONS = ImportOptions(name=NAME, provider="traderos-tutorial", rights_label="synthetic-tutorial",
                        rights_stated_by="traderos", source_kind=SourceKind.TUTORIAL, synthetic=True,
                        export_raw_allowed=True, redistribution_allowed=True, venue="TUTORIAL")
CENT = Decimal("0.01")


class _Rng:
    """xorshift64*: integers only, identical everywhere."""

    def __init__(self, seed: int) -> None:
        self.s = (seed or 1) & 0xFFFFFFFFFFFFFFFF

    def next(self) -> int:
        s = self.s
        s ^= (s >> 12)
        s ^= (s << 25) & 0xFFFFFFFFFFFFFFFF
        s ^= (s >> 27)
        self.s = s
        return (s * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF

    def unit(self) -> Decimal:
        """Uniform in [0, 1) with 18 digits."""
        return Decimal(self.next() >> 4) / Decimal(1 << 60)

    def normal(self) -> Decimal:
        """Irwin-Hall: the sum of 12 uniforms minus 6 (mean 0, variance 1), exact in Decimal."""
        return sum((self.unit() for _ in range(12)), Decimal(0)) - 6


def csv_bytes() -> bytes:
    cal = XNYS()
    days = cal.sessions(START, END)
    rng = _Rng(SEED)
    lines = ["symbol,timestamp_utc,open,high,low,close,volume,adjustment"]
    with localcontext() as ctx:
        ctx.prec = 28
        ctx.rounding = ROUND_HALF_EVEN
        # one shared market factor and a regime that switches now and then, so the series move together
        regime_stressed = False
        market = []
        for _ in days:
            if rng.unit() < Decimal("0.01"):
                regime_stressed = not regime_stressed
            market.append((rng.normal(), regime_stressed))
        for symbol, (_desc, start, drift, calm, stressed, volume) in UNIVERSE.items():
            close = Decimal(start)
            beta = Decimal("0.2") if symbol in ("TUTGLD", "TUTSLV", "TUTBND") else Decimal("0.8")
            for day, (m, stress) in zip(days, market, strict=True):
                vol = Decimal(stressed if stress else calm) / 10_000
                shock = (beta * m + (1 - beta * beta).sqrt() * rng.normal()) * vol
                gap = rng.normal() * vol / 4
                open_ = (close * (1 + gap)).quantize(CENT)
                new_close = (close * (1 + Decimal(drift) / 10_000 + shock)).quantize(CENT)
                if new_close <= CENT:
                    new_close = CENT * 10
                wick = abs(rng.normal()) * vol / 2
                high = (max(open_, new_close) * (1 + wick)).quantize(CENT)
                low = (min(open_, new_close) * (1 - abs(rng.normal()) * vol / 2)).quantize(CENT)
                low = max(low, CENT)
                vol_shares = int(volume * (1 + abs(shock) * 20 + rng.unit() / 2))
                closes_at = cal.open_close(day)[1]
                lines.append(f"{symbol},{closes_at:%Y-%m-%dT%H:%M:%SZ},{open_},{high},{low},{new_close},"
                             f"{vol_shares},raw")
                close = new_close
    return ("\n".join(lines) + "\n").encode("utf-8")


RECORDED_AT = datetime(2026, 11, 20, tzinfo=UTC)       # fixed: every profile builds the identical manifest


def load(store):
    """Put the tutorial dataset in the store (idempotent) and return its manifest.

    The manifest's recording time is fixed, so the tutorial dataset (and every Passport built on it) has the same
    hashes on every machine: a bundle that omits the tutorial's bars restores against the copy `traderos init` makes.
    """
    from traderos.data import import_into
    return import_into(store, csv_bytes(), OPTIONS, now=RECORDED_AT)
