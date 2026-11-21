"""Part B, Market Intelligence: S01 Chart Scanner, S03 Market Pulse, S13 Entry Timer, S14 Exit Planner and
S18 Price Alerts (spec 12). Each takes the data store and the exchange calendar it reads as arguments
(BarReader, ExchangeCalendar from traderos.contracts), so it runs the same against the in-memory fakes and the
real store. traderos.api calls these with the defaults once the store lands.

None of these can place an order or send anything anywhere: they read, compute and return a typed record.
"""
from traderos.intelligence.alerts import evaluate_alert
from traderos.intelligence.entry import assess_entry
from traderos.intelligence.exit import check_geometry, plan_exit
from traderos.intelligence.pulse import market_context
from traderos.intelligence.scan import scan

__all__ = ["assess_entry", "check_geometry", "evaluate_alert", "market_context", "plan_exit", "scan"]
