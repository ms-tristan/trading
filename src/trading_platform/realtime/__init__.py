"""Realtime layer (layer 6): streaming engine, venue adapters, persistence.

Public surface
--------------
The **vocabulary** (:mod:`trading_platform.realtime.models`), the **time seam**
(:mod:`trading_platform.realtime.clock`) and the **shared wallet**
(:mod:`trading_platform.realtime.wallet`) are exported eagerly: the wallet is the
single source of truth for the USDT cash of the whole platform, every layer
injects it, and importing it pulls in no optional dependency.

Every **engine class** is exported lazily through PEP 562 ``__getattr__``.  The
decision is deliberate and has two consequences that the rest of the project
relies on:

* ``import trading_platform.realtime`` never imports an optional extra (``ccxt``
  or ``freqtrade`` stay lazy), so the whole suite stays green with the dev extra
  only;
* the package can be imported while the other realtime modules are still being
  written, and a missing sibling surfaces as an ``AttributeError`` on the exact
  attribute that was asked for instead of an ``ImportError`` at import time.
"""

from __future__ import annotations

import importlib
from typing import Any

from trading_platform.realtime.clock import Clock, ManualClock, SystemClock
from trading_platform.realtime.models import (
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
from trading_platform.realtime.wallet import PlatformWallet, WalletSnapshot

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
    # platform settings (the state store is their single source of truth)
    "PlatformSettings": "settings",
    "SETTINGS_META_KEY": "settings",
    "SETTINGS_VERSION": "settings",
    "BOOTSTRAP_FIELDS": "settings",
    "SettingsError": "settings",
    "settings_from_store": "settings",
    "save_settings": "settings",
    "update_settings": "settings",
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
    "SETTINGS_META_KEY",
    "SETTINGS_VERSION",
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
    "PlatformSettings",
    "PlatformSnapshot",
    "PlatformWallet",
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
    "SettingsError",
    "SignalAction",
    "SqliteStateStore",
    "StateStore",
    "SystemClock",
    "TradeSignalDecision",
    "WalletSnapshot",
    "configure_logging",
    "credentials_from_env",
    "freqtrade_strategy_for",
    "known_secrets",
    "log_event",
    "new_client_order_id",
    "resolve_strategy",
    "save_settings",
    "settings_from_store",
    "update_settings",
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
        raise AttributeError(f"module 'trading_platform.realtime' has no attribute {name!r}")
    module = importlib.import_module(f"trading_platform.realtime.{module_name}")
    return getattr(module, name)


def __dir__() -> list[str]:
    """Return the eagerly and lazily available public names."""
    return sorted(set(globals()) | set(__all__))
