"""Realtime layer (layer 6): streaming engine, venue adapters, persistence.

Public surface
--------------
The **vocabulary** (:mod:`trading_backtest.realtime.models`) and the **time seam**
(:mod:`trading_backtest.realtime.clock`) are exported eagerly: they are pure
declarations with no optional dependency, and every other layer imports them.

Every **engine class** is exported lazily through PEP 562 ``__getattr__``.  The
decision is deliberate and has two consequences that the rest of the project
relies on:

* ``import trading_backtest.realtime`` never imports an optional extra (``ccxt``
  or ``freqtrade`` stay lazy), so the whole suite stays green with the dev extra
  only;
* the package can be imported while the other realtime modules are still being
  written, and a missing sibling surfaces as an ``AttributeError`` on the exact
  attribute that was asked for instead of an ``ImportError`` at import time.
"""

from __future__ import annotations

import importlib
from typing import Any

from trading_backtest.realtime.clock import Clock, ManualClock, SystemClock
from trading_backtest.realtime.models import (
    BrokerAck,
    BrokerEvent,
    BrokerEventType,
    CandleEvent,
    EngineCounters,
    EquityPoint,
    Fill,
    Order,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
    PlatformSnapshot,
    Position,
    ProfileHealth,
    ProfileSnapshot,
    ProfileState,
    ProfileStatus,
    ReconciliationReport,
    RunMode,
    SignalAction,
    TradeSignalDecision,
    new_client_order_id,
)

#: Public name -> submodule that defines it, resolved on first attribute access.
_LAZY: dict[str, str] = {
    # market data
    "MarketStream": "stream",
    "ReplayMarketStream": "stream",
    "PollingMarketStream": "stream",
    "CcxtProMarketStream": "stream",
    "CompositeMarketStream": "stream",
    # persistence
    "StateStore": "store",
    "SqliteStateStore": "store",
    # venue adapters
    "Broker": "broker",
    "PaperBroker": "broker",
    "CcxtBroker": "broker",
    # credentials
    "ExchangeCredentials": "credentials",
    "credentials_from_env": "credentials",
    "known_secrets": "credentials",
    # risk and safety
    "RiskLimits": "risk",
    "RiskDecision": "risk",
    "RiskManager": "risk",
    "KillSwitch": "risk",
    "KillSwitchState": "risk",
    "LiveTradingGate": "risk",
    "MetaStore": "risk",
    # order lifecycle
    "ExecutionGateway": "gateway",
    # strategies
    "resolve_strategy": "strategies",
    "freqtrade_strategy_for": "strategies",
    # runtime
    "ProfileRunner": "runner",
    "RealtimeOrchestrator": "orchestrator",
    # read model
    "Monitor": "monitor",
    "ProfileReport": "monitor",
    # observability
    "configure_logging": "observability",
    "log_event": "observability",
    "JsonLogFormatter": "observability",
    "RedactionFilter": "observability",
    "Counters": "observability",
}

__all__ = [
    "Broker",
    "BrokerAck",
    "BrokerEvent",
    "BrokerEventType",
    "CandleEvent",
    "CcxtBroker",
    "CcxtProMarketStream",
    "Clock",
    "CompositeMarketStream",
    "Counters",
    "EngineCounters",
    "EquityPoint",
    "ExchangeCredentials",
    "ExecutionGateway",
    "Fill",
    "JsonLogFormatter",
    "KillSwitch",
    "KillSwitchState",
    "LiveTradingGate",
    "ManualClock",
    "MarketStream",
    "MetaStore",
    "Monitor",
    "Order",
    "OrderRequest",
    "OrderSide",
    "OrderState",
    "OrderType",
    "PaperBroker",
    "PlatformSnapshot",
    "PollingMarketStream",
    "Position",
    "ProfileHealth",
    "ProfileReport",
    "ProfileRunner",
    "ProfileSnapshot",
    "ProfileState",
    "ProfileStatus",
    "RealtimeOrchestrator",
    "ReconciliationReport",
    "RedactionFilter",
    "ReplayMarketStream",
    "RiskDecision",
    "RiskLimits",
    "RiskManager",
    "RunMode",
    "SignalAction",
    "SqliteStateStore",
    "StateStore",
    "SystemClock",
    "TradeSignalDecision",
    "configure_logging",
    "credentials_from_env",
    "freqtrade_strategy_for",
    "known_secrets",
    "log_event",
    "new_client_order_id",
    "resolve_strategy",
]


def __getattr__(name: str) -> Any:
    """Resolve a lazily exported engine symbol (PEP 562).

    Raises
    ------
    AttributeError
        If ``name`` is not part of the public surface of the layer.
    """
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module 'trading_backtest.realtime' has no attribute {name!r}")
    module = importlib.import_module(f"trading_backtest.realtime.{module_name}")
    return getattr(module, name)


def __dir__() -> list[str]:
    """Return the eagerly and lazily available public names."""
    return sorted(set(globals()) | set(__all__))
