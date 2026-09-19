"""Contract tests of the realtime foundation (work package wp1).

Covers, offline and deterministically:

* the twelve new error classes of the realtime branch and their exact edges;
* the layer-1 rule (``trading_platform.core.errors`` imports no project module);
* the frozen realtime vocabulary: frozen dataclasses, JSON-native ``to_dict()``
  payloads (no ``NaN``/``Inf``, no pandas object) and lossless ``from_dict()``;
* :func:`trading_platform.realtime.models.new_client_order_id`;
* the ``Clock`` seam (``SystemClock``, ``ManualClock``);
* the typed profile / realtime / monitoring configuration;
* ``load_profiles`` / ``load_realtime_config`` / ``load_monitoring_config``;
* ``config/profiles.example.json`` (no credential-ish key, valid as-is);
* the lazy packaging of ``trading_platform.realtime``.

No network, no wall-clock dependency, no fixed TCP port, no shared fixture: every
helper is local to this module.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from trading_platform.config import (
    AppConfig,
    MonitoringConfig,
    ProfileConfig,
    RealtimeConfig,
    RiskLimitsConfig,
    default_monitoring_config,
    default_realtime_config,
    load_monitoring_config,
    load_profiles,
    load_realtime_config,
)
from trading_platform.core import errors as core_errors
from trading_platform.core.errors import (
    BrokerError,
    BrokerUnavailableError,
    ConfigError,
    GatewayError,
    KillSwitchActiveError,
    LiveTradingForbiddenError,
    MarketStreamError,
    MonitoringError,
    OrderRejectedError,
    ProfileError,
    RealtimeError,
    RiskLimitExceededError,
    StateStoreError,
    TradingBacktestError,
)
from trading_platform.core.models import Direction
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

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
EXAMPLE_PROFILES = REPO_ROOT / "config" / "profiles.example.json"

TS = pd.Timestamp("2024-01-01T00:00:00Z")
TS_LATER = pd.Timestamp("2024-01-01T01:00:00Z")
CLIENT_ID = "btc-paper-BTC_USDT-20240101T000000Z-0000"

REALTIME_ERRORS = [
    RealtimeError,
    ProfileError,
    MarketStreamError,
    StateStoreError,
    BrokerError,
    BrokerUnavailableError,
    OrderRejectedError,
    GatewayError,
    LiveTradingForbiddenError,
    RiskLimitExceededError,
    KillSwitchActiveError,
    MonitoringError,
]

ERROR_PARENTS = [
    (RealtimeError, TradingBacktestError),
    (ProfileError, RealtimeError),
    (MarketStreamError, RealtimeError),
    (StateStoreError, RealtimeError),
    (BrokerError, RealtimeError),
    (BrokerUnavailableError, BrokerError),
    (OrderRejectedError, BrokerError),
    (GatewayError, RealtimeError),
    (LiveTradingForbiddenError, RealtimeError),
    (RiskLimitExceededError, RealtimeError),
    (KillSwitchActiveError, RealtimeError),
    (MonitoringError, RealtimeError),
]


# ---------------------------------------------------------------------------
# local helpers
# ---------------------------------------------------------------------------


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter that sees ``src`` on ``PYTHONPATH``."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) if not existing else f"{SRC_DIR}{os.pathsep}{existing}"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


def write_profiles(tmp_path: Path, payload: Any, *, name: str = "profiles.json") -> Path:
    """Write ``payload`` as JSON under ``tmp_path`` and return the path."""
    target = tmp_path / name
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def minimal_profile(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid profile payload, with ``overrides`` applied."""
    payload: dict[str, Any] = {"id": "prob-1", "symbol": "BTC/USDT"}
    payload.update(overrides)
    return payload


def travel(payload: Any) -> Iterator[Any]:
    """Yield every scalar/container node of a ``to_dict()`` payload."""
    yield payload
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            yield from travel(key)
            yield from travel(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from travel(item)


def assert_json_native(payload: Any) -> None:
    """Assert a payload can travel as JSON: native types only, finite floats."""
    for node in travel(payload):
        if isinstance(node, bool) or node is None:
            continue
        if isinstance(node, float):
            assert math.isfinite(node), f"non-finite float in payload: {node!r}"
            continue
        assert not isinstance(node, (pd.Timestamp, pd.Series, pd.DataFrame, Enum)), (
            f"non-JSON-native value in payload: {node!r}"
        )
        assert isinstance(node, (str, int, list, dict)), f"unexpected value in payload: {node!r}"
    json.dumps(payload, allow_nan=False)


# ---------------------------------------------------------------------------
# 1. the realtime error branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error_class", REALTIME_ERRORS)
def test_realtime_errors_derive_from_the_base_class(error_class: type[Exception]) -> None:
    assert issubclass(error_class, TradingBacktestError)
    error = error_class("boom")
    assert isinstance(error, TradingBacktestError)
    assert str(error) == "boom"
    assert error.issues == ()
    assert error.message == "boom"


@pytest.mark.parametrize(("error_class", "parent"), ERROR_PARENTS)
def test_realtime_error_edges_are_exact(
    error_class: type[Exception], parent: type[Exception]
) -> None:
    assert issubclass(error_class, parent)
    assert error_class.__bases__ == (parent,)


def test_realtime_error_branch_is_closed() -> None:
    """Every realtime error derives from RealtimeError; the parent has one parent."""
    assert RealtimeError.__bases__ == (TradingBacktestError,)
    for error_class in REALTIME_ERRORS:
        assert issubclass(error_class, RealtimeError)
    assert not issubclass(ConfigError, RealtimeError)


def test_issues_are_rendered_with_the_shared_constructor() -> None:
    error = RiskLimitExceededError("risk limit hit", ["max_order_notional", "max_open_positions"])
    assert error.issues == ("max_order_notional", "max_open_positions")
    assert str(error) == "risk limit hit: max_order_notional; max_open_positions"
    assert core_errors.TradingBacktestError("message", ["a", "b"]).__str__() == "message: a; b"


def test_error_all_exports_are_sorted_and_complete() -> None:
    exported = core_errors.__all__
    assert exported == sorted(exported)
    expected = {
        "BrokerError",
        "BrokerUnavailableError",
        "GatewayError",
        "KillSwitchActiveError",
        "LiveTradingForbiddenError",
        "MarketStreamError",
        "MonitoringError",
        "OrderRejectedError",
        "ProfileError",
        "RealtimeError",
        "RiskLimitExceededError",
        "StateStoreError",
    }
    assert expected <= set(exported)
    for name in expected:
        assert getattr(core_errors, name) is not None


# ---------------------------------------------------------------------------
# 2. layer-1 rule: core.errors imports no project module
# ---------------------------------------------------------------------------


def test_core_errors_is_importable_without_any_project_import() -> None:
    code = (
        "import json, sys\n"
        "import trading_platform.core.errors\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('trading_platform'))))\n"
    )
    result = run_python(code)
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert loaded, "the subprocess must at least see the package under test"
    for module in loaded:
        assert module == "trading_platform" or module.startswith("trading_platform.core"), (
            f"importing trading_platform.core.errors must not pull in {module}"
        )
    for layer in ("config", "data", "realtime", "strategy", "metrics", "reporting", "web"):
        assert f"trading_platform.{layer}" not in loaded


# ---------------------------------------------------------------------------
# 3. frozen vocabulary: freeze, JSON-native to_dict, lossless from_dict
# ---------------------------------------------------------------------------


def vocabulary_samples() -> list[Any]:
    """Return one populated instance of every round-trippable dataclass."""
    order = Order(
        client_order_id=CLIENT_ID,
        profile_id="btc-paper",
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        type=OrderType.LIMIT,
        quantity=0.5,
        state=OrderState.PARTIALLY_FILLED,
        mode=RunMode.PAPER,
        created_at=TS,
        updated_at=TS_LATER,
        filled_quantity=0.2,
        price=30_000.0,
        average_fill_price=30_010.0,
        broker_order_id="venue-1",
        reject_reason="",
    )
    fill = Fill(
        fill_id="f-1",
        client_order_id=CLIENT_ID,
        profile_id="btc-paper",
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        quantity=0.2,
        price=30_010.0,
        fee=6.002,
        timestamp=TS_LATER,
        mode=RunMode.PAPER,
    )
    return [
        CandleEvent(
            symbol="BTC/USDT",
            timeframe="1h",
            timestamp=TS,
            open=30_000.0,
            high=30_500.0,
            low=29_500.0,
            close=30_200.0,
            volume=12.5,
            closed=True,
        ),
        CandleEvent(
            symbol="ETH/USDT",
            timeframe="1h",
            timestamp=TS,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=0.0,
            closed=False,
        ),
        OrderRequest(
            profile_id="btc-paper",
            client_order_id=CLIENT_ID,
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            quantity=0.5,
            price=None,
            stop_price=29_000.0,
            mode=RunMode.LIVE,
            reason="exit_long",
            created_at=TS,
        ),
        OrderRequest(
            profile_id="btc-paper",
            client_order_id=CLIENT_ID,
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            quantity=0.1,
        ),
        order,
        Fill(
            fill_id="f-1",
            client_order_id=CLIENT_ID,
            profile_id="btc-paper",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=0.2,
            price=30_010.0,
            fee=6.002,
            timestamp=TS_LATER,
            mode=RunMode.PAPER,
        ),
        BrokerAck(
            client_order_id=CLIENT_ID,
            accepted=True,
            state=OrderState.SUBMITTED,
            broker_order_id="venue-1",
            reason="",
            submitted_at=TS,
        ),
        BrokerAck(
            client_order_id=CLIENT_ID, accepted=False, state=OrderState.REJECTED, reason="nope"
        ),
        BrokerEvent(
            event_type=BrokerEventType.ORDER_PARTIALLY_FILLED,
            client_order_id=CLIENT_ID,
            profile_id="btc-paper",
            order=order,
            fill=fill,
            message="partial",
            timestamp=TS_LATER,
        ),
        BrokerEvent(
            event_type=BrokerEventType.ORDER_CANCELLED,
            client_order_id=CLIENT_ID,
            profile_id="btc-paper",
        ),
        ReconciliationReport(
            profile_id="btc-paper",
            ok=False,
            checked_at=TS_LATER,
            matched=3,
            only_at_venue=("a",),
            only_locally=("b", "c"),
            mismatched=("d",),
            details={"reason": "quantity mismatch", "delta": 0.1},
        ),
        ReconciliationReport(profile_id="btc-paper", ok=True, checked_at=TS),
        Position(
            profile_id="btc-paper",
            symbol="BTC/USDT",
            quantity=0.5,
            average_price=30_000.0,
            direction=Direction.LONG,
            opened_at=TS,
            updated_at=TS_LATER,
            realized_pnl=-1.5,
            unrealized_pnl=25.0,
            stop_price=29_000.0,
        ),
        Position(
            profile_id="btc-paper",
            symbol="BTC/USDT",
            quantity=0.0,
            average_price=0.0,
            direction=Direction.SHORT,
            opened_at=TS,
            updated_at=TS,
        ),
        EquityPoint(
            profile_id="btc-paper",
            timestamp=TS_LATER,
            equity=10_025.0,
            cash=9_000.0,
            position_value=1_025.0,
        ),
        EngineCounters(
            candles_processed=12,
            orders_submitted=3,
            orders_filled=2,
            orders_rejected=1,
            stream_reconnects=1,
            risk_rejections=1,
            errors=0,
        ),
        EngineCounters(),
        ProfileState(
            profile_id="btc-paper",
            status=ProfileStatus.DEGRADED,
            mode=RunMode.PAPER,
            last_candle_at=TS_LATER,
            lag_seconds=12.5,
            last_error="reconciliation mismatch",
            reconnect_count=2,
            updated_at=TS_LATER,
        ),
        ProfileState(profile_id="btc-paper", status=ProfileStatus.STARTING, mode=RunMode.LIVE),
    ]


def to_dict_only_samples() -> list[Any]:
    """Return one populated instance of every ``to_dict``-only dataclass."""
    health = ProfileHealth(
        profile_id="btc-paper",
        status=ProfileStatus.RUNNING,
        last_candle_at=TS_LATER,
        lag_seconds=3.0,
        last_error=None,
        reconnect_count=0,
        counters=EngineCounters(candles_processed=5),
    )
    snapshot = ProfileSnapshot(
        profile_id="btc-paper",
        symbol="BTC/USDT",
        timeframe="1h",
        strategy="basic",
        mode=RunMode.PAPER,
        status=ProfileStatus.RUNNING,
        initial_balance=10_000.0,
        equity=10_150.0,
        cash=9_000.0,
        position_value=1_150.0,
        total_return=0.015,
        n_trades=4,
        open_positions=1,
        health=health,
        started_at=TS,
        updated_at=TS_LATER,
    )
    return [
        health,
        snapshot,
        ProfileSnapshot(
            profile_id="eth-paper",
            symbol="ETH/USDT",
            timeframe="4h",
            strategy="basic",
            mode=RunMode.PAPER,
            status=ProfileStatus.STOPPED,
            initial_balance=1_000.0,
            equity=0.0,
            cash=0.0,
            position_value=0.0,
            total_return=-1.0,
            n_trades=0,
            open_positions=0,
            health=ProfileHealth(profile_id="eth-paper", status=ProfileStatus.STOPPED),
        ),
        PlatformSnapshot(
            profiles=(snapshot,),
            generated_at=TS_LATER,
            kill_switch=True,
            kill_switch_reason="operator",
            kill_switch_changed_at=TS_LATER,
            version="0.1.0",
            started_at=TS,
            uptime_seconds=3600.0,
        ),
        PlatformSnapshot(profiles=(), generated_at=TS_LATER, kill_switch=False),
        TradeSignalDecision(
            profile_id="btc-paper",
            timestamp=TS,
            action=SignalAction.ENTER_LONG,
            direction=Direction.LONG,
            stop_price=29_000.0,
            quantity=0.5,
            reference_price=30_000.0,
            client_order_id=CLIENT_ID,
            blocked=False,
            block_reason="",
            reason="ema cross",
        ),
        TradeSignalDecision(
            profile_id="btc-paper",
            timestamp=TS,
            action=SignalAction.HOLD,
            blocked=True,
            block_reason="max_open_positions",
        ),
    ]


@pytest.mark.parametrize("sample", vocabulary_samples(), ids=lambda item: type(item).__name__)
def test_frozen_vocabulary_round_trips(sample: Any) -> None:
    assert dataclasses.is_dataclass(sample)
    assert sample.__dataclass_params__.frozen  # type: ignore[attr-defined]
    payload = sample.to_dict()
    assert_json_native(payload)
    restored = type(sample).from_dict(payload)
    assert restored == sample
    assert restored.to_dict() == payload


@pytest.mark.parametrize("sample", vocabulary_samples(), ids=lambda item: type(item).__name__)
def test_every_frozen_field_is_really_frozen(sample: Any) -> None:
    assert dataclasses.fields(sample), "a frozen sample must declare at least one field"
    for field in dataclasses.fields(sample):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(sample, field.name, None)


@pytest.mark.parametrize("sample", to_dict_only_samples(), ids=lambda item: type(item).__name__)
def test_to_dict_only_samples_are_frozen_and_json_native(sample: Any) -> None:
    assert dataclasses.is_dataclass(sample)
    assert sample.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert not hasattr(sample, "from_dict")
    payload = sample.to_dict()
    assert_json_native(payload)
    assert payload == json.loads(json.dumps(payload, allow_nan=False))
    for field in dataclasses.fields(sample):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(sample, field.name, None)


def test_non_finite_floats_never_reach_a_payload() -> None:
    candle = CandleEvent(
        symbol="BTC/USDT",
        timeframe="1h",
        timestamp=TS,
        open=float("nan"),
        high=float("inf"),
        low=float("-inf"),
        close=30_000.0,
        volume=1.0,
    )
    payload = candle.to_dict()
    assert payload["open"] is None
    assert payload["high"] is None
    assert payload["low"] is None
    assert payload["close"] == 30_000.0
    assert_json_native(payload)

    point = EquityPoint(
        profile_id="btc-paper",
        timestamp=TS,
        equity=float("nan"),
        cash=1.0,
        position_value=float("inf"),
    )
    assert point.to_dict()["equity"] is None
    assert point.to_dict()["position_value"] is None
    assert_json_native(point.to_dict())

    # A payload whose floats had to be dropped stays decodable (never raises).
    assert CandleEvent.from_dict(payload) == CandleEvent(
        symbol="BTC/USDT",
        timeframe="1h",
        timestamp=TS,
        open=0.0,
        high=0.0,
        low=0.0,
        close=30_000.0,
        volume=1.0,
    )


def test_nested_payloads_delegate_to_their_own_to_dict() -> None:
    health = ProfileHealth(profile_id="btc-paper", status=ProfileStatus.RUNNING)
    snapshot = ProfileSnapshot(
        profile_id="btc-paper",
        symbol="BTC/USDT",
        timeframe="1h",
        strategy="basic",
        mode=RunMode.PAPER,
        status=ProfileStatus.RUNNING,
        initial_balance=10_000.0,
        equity=10_000.0,
        cash=10_000.0,
        position_value=0.0,
        total_return=0.0,
        n_trades=0,
        open_positions=0,
        health=health,
    )
    health_payload = snapshot.to_dict()["health"]
    assert health_payload == health.to_dict()
    assert health_payload["counters"] == health.counters.to_dict()
    platform = PlatformSnapshot(profiles=(snapshot,), generated_at=TS, kill_switch=False)
    assert platform.to_dict()["profiles"] == [snapshot.to_dict()]

    order = Order(
        client_order_id=CLIENT_ID,
        profile_id="btc-paper",
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        quantity=1.0,
        state=OrderState.FILLED,
        mode=RunMode.PAPER,
        created_at=TS,
        updated_at=TS,
    )
    event = BrokerEvent(
        event_type=BrokerEventType.ORDER_FILLED,
        client_order_id=CLIENT_ID,
        profile_id="btc-paper",
        order=order,
    )
    assert event.to_dict()["order"] == order.to_dict()
    assert event.to_dict()["fill"] is None


#: The keys a :class:`ProfileSnapshot` published before the shared platform wallet.
LEGACY_PROFILE_SNAPSHOT_KEYS = frozenset(
    {
        "profile_id",
        "symbol",
        "timeframe",
        "strategy",
        "mode",
        "status",
        "initial_balance",
        "equity",
        "cash",
        "position_value",
        "total_return",
        "n_trades",
        "open_positions",
        "health",
        "started_at",
        "updated_at",
    }
)

#: The attributed figures appended after ``updated_at`` (all with defaults).
ATTRIBUTED_PROFILE_SNAPSHOT_KEYS = frozenset(
    {"allocation", "deployed", "realized_pnl", "unrealized_pnl", "last_block_reason"}
)


def attributed_snapshot(**overrides: Any) -> ProfileSnapshot:
    """Return a profile snapshot carrying the attributed platform figures."""
    payload: dict[str, Any] = {
        "profile_id": "btc-paper",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "strategy": "basic",
        "mode": RunMode.PAPER,
        "status": ProfileStatus.RUNNING,
        "initial_balance": 10_000.0,
        "equity": 10_025.0,
        "cash": 9_750.0,
        "position_value": 275.0,
        "total_return": 0.0025,
        "n_trades": 2,
        "open_positions": 1,
        "health": ProfileHealth(profile_id="btc-paper", status=ProfileStatus.RUNNING),
        "allocation": 10_000.0,
        "deployed": 250.0,
        "realized_pnl": 25.0,
        "unrealized_pnl": 0.0,
        "last_block_reason": "platform wallet cannot fund order",
    }
    payload.update(overrides)
    return ProfileSnapshot(**payload)


def test_profile_snapshot_appends_the_attributed_figures() -> None:
    """The five new keys are additive: the legacy ones keep their name and type."""
    payload = attributed_snapshot().to_dict()

    assert set(payload) == LEGACY_PROFILE_SNAPSHOT_KEYS | ATTRIBUTED_PROFILE_SNAPSHOT_KEYS
    assert payload["allocation"] == 10_000.0
    assert payload["deployed"] == 250.0
    assert payload["realized_pnl"] == 25.0
    assert payload["unrealized_pnl"] == 0.0
    assert payload["last_block_reason"] == "platform wallet cannot fund order"
    assert payload["cash"] == 9_750.0
    assert payload["initial_balance"] == 10_000.0
    assert_json_native(payload)


def test_profile_snapshot_defaults_keep_every_existing_construction_working() -> None:
    """Without the new fields the payload is exactly the historical one, plus nulls."""
    snapshot = ProfileSnapshot(
        profile_id="btc-paper",
        symbol="BTC/USDT",
        timeframe="1h",
        strategy="basic",
        mode=RunMode.PAPER,
        status=ProfileStatus.RUNNING,
        initial_balance=10_000.0,
        equity=10_000.0,
        cash=10_000.0,
        position_value=0.0,
        total_return=0.0,
        n_trades=0,
        open_positions=0,
        health=ProfileHealth(profile_id="btc-paper", status=ProfileStatus.RUNNING),
    )
    payload = snapshot.to_dict()

    assert set(payload) == LEGACY_PROFILE_SNAPSHOT_KEYS | ATTRIBUTED_PROFILE_SNAPSHOT_KEYS
    assert payload["allocation"] == 0.0
    assert payload["deployed"] == 0.0
    assert payload["realized_pnl"] == 0.0
    assert payload["unrealized_pnl"] == 0.0
    assert payload["last_block_reason"] is None
    assert_json_native(payload)


def test_profile_snapshot_never_publishes_a_non_finite_attributed_float() -> None:
    """``NaN``/``Infinity`` collapse to ``None``, exactly like every other float."""
    payload = attributed_snapshot(
        allocation=float("nan"),
        deployed=float("inf"),
        realized_pnl=float("-inf"),
        unrealized_pnl=float("nan"),
    ).to_dict()

    assert payload["allocation"] is None
    assert payload["deployed"] is None
    assert payload["realized_pnl"] is None
    assert payload["unrealized_pnl"] is None
    assert_json_native(payload)


def test_platform_snapshot_carries_the_shared_wallet_view() -> None:
    """The wallet view is the platform's single ledger, JSON-native in every field."""
    from trading_platform.realtime.wallet import PlatformWallet

    wallet = PlatformWallet(initial_balance=15_000.0, name="platform")
    view = wallet.snapshot(
        positions_value=275.0,
        deployed=250.0,
        realized_pnl=25.0,
        unrealized_pnl=25.0,
        total_exposure=275.0,
        profiles=2,
    )
    platform = PlatformSnapshot(
        profiles=(attributed_snapshot(),),
        generated_at=TS,
        kill_switch=False,
        wallet=view,
    )
    payload = platform.to_dict()

    assert payload["wallet"] == view.to_dict()
    assert payload["wallet"]["name"] == "platform"
    assert payload["wallet"]["cash"] == 15_000.0
    assert payload["wallet"]["initial_balance"] == 15_000.0
    assert payload["wallet"]["equity"] == 15_275.0
    assert payload["wallet"]["total_exposure"] == 275.0
    assert payload["wallet"]["profiles"] == 2
    assert_json_native(payload)


def test_platform_snapshot_wallet_is_null_when_absent() -> None:
    """A platform read model built without a wallet serialises ``null``, never a gap."""
    payload = PlatformSnapshot(profiles=(), generated_at=TS, kill_switch=True).to_dict()

    assert "wallet" in payload
    assert payload["wallet"] is None
    assert payload["profiles"] == []
    assert_json_native(payload)


def test_platform_snapshot_wallet_field_is_optional_and_frozen() -> None:
    """The field is the last one and it is optional: positional use keeps working."""
    bare = PlatformSnapshot((), TS, False, "", None, "0.1.0", TS, 1.0)
    assert bare.wallet is None

    from trading_platform.realtime.wallet import PlatformWallet

    wallet = PlatformWallet(initial_balance=1.0).snapshot()
    with_wallet = PlatformSnapshot((), TS, False, "", None, "0.1.0", TS, 1.0, wallet)
    assert with_wallet.wallet == wallet
    with pytest.raises(dataclasses.FrozenInstanceError):
        with_wallet.wallet = None  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 4. new_client_order_id
# ---------------------------------------------------------------------------


def test_new_client_order_id_matches_the_documented_example() -> None:
    assert (
        new_client_order_id("btc-paper", "BTC/USDT", "2024-01-01T00:00:00Z", 0)
        == "btc-paper-BTC_USDT-20240101T000000Z-0000"
    )


def test_new_client_order_id_is_deterministic_and_timezone_agnostic() -> None:
    aware = new_client_order_id("btc-paper", "BTC/USDT", pd.Timestamp("2024-01-01T00:00:00Z"), 0)
    naive = new_client_order_id("btc-paper", "BTC/USDT", pd.Timestamp("2024-01-01T00:00:00"), 0)
    shifted = new_client_order_id(
        "btc-paper", "BTC/USDT", pd.Timestamp("2024-01-01T01:00:00+01:00"), 0
    )
    assert aware == naive == shifted == "btc-paper-BTC_USDT-20240101T000000Z-0000"
    assert new_client_order_id("btc-paper", "BTC/USDT", TS, 0) == aware
    assert new_client_order_id("btc-paper", "BTC/USDT", TS, 7).endswith("-0007")
    assert new_client_order_id("eth-paper", "ETH/USDT", TS, 0) == (
        "eth-paper-ETH_USDT-20240101T000000Z-0000"
    )
    # Two calls, two identical ids (the id is a pure function of its arguments).
    assert new_client_order_id("btc-paper", "BTC/USDT", TS, 3) == new_client_order_id(
        "btc-paper", "BTC/USDT", TS, 3
    )


def test_new_client_order_id_does_not_read_the_wall_clock() -> None:
    code = (
        "import pandas as pd\n"
        "from trading_platform.realtime.models import new_client_order_id\n"
        "print(new_client_order_id('btc-paper', 'BTC/USDT', pd.Timestamp('2024-01-01T00:00:00Z'), 0))\n"
    )
    result = run_python(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "btc-paper-BTC_USDT-20240101T000000Z-0000"


def test_new_client_order_id_rejects_a_negative_sequence() -> None:
    with pytest.raises(ValueError, match="sequence must be >= 0"):
        new_client_order_id("btc-paper", "BTC/USDT", TS, -1)


# ---------------------------------------------------------------------------
# 5. the Clock seam
# ---------------------------------------------------------------------------


def test_system_clock_is_utc_aware_and_monotonic() -> None:
    clock = SystemClock()
    moment = clock.now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)
    first = clock.monotonic()
    second = clock.monotonic()
    assert isinstance(first, float)
    assert second >= first
    assert isinstance(clock, Clock)


def test_system_clock_sleep_is_a_real_asyncio_sleep() -> None:
    clock = SystemClock()

    async def scenario() -> float:
        started = clock.monotonic()
        await clock.sleep(0.01)
        return clock.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert elapsed >= 0.005


def test_manual_clock_starts_on_a_fixed_instant() -> None:
    clock = ManualClock()
    assert clock.now() == pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime()
    assert clock.monotonic() == 0.0
    other = ManualClock(pd.Timestamp("2025-06-01T12:00:00Z").to_pydatetime(), monotonic_start=100.0)
    assert other.now() == pd.Timestamp("2025-06-01T12:00:00Z").to_pydatetime()
    assert other.monotonic() == 100.0


def test_manual_clock_sleep_advances_virtual_time_only() -> None:
    clock = ManualClock()

    async def scenario() -> None:
        await clock.sleep(90.0)

    started = time.monotonic()
    asyncio.run(scenario())
    elapsed = time.monotonic() - started
    assert elapsed < 0.05, f"ManualClock.sleep must not block, took {elapsed:.4f}s"
    assert clock.monotonic() == 90.0
    assert clock.now() == pd.Timestamp("2024-01-01T00:01:30Z").to_pydatetime()
    assert isinstance(clock, Clock)


def test_manual_clock_advance_set_and_advance_to() -> None:
    clock = ManualClock()
    clock.advance(60.5)
    assert clock.monotonic() == 60.5
    assert clock.now() == pd.Timestamp("2024-01-01T00:01:00.500000Z").to_pydatetime()

    clock.set(pd.Timestamp("2024-03-01T00:00:00Z").to_pydatetime())
    assert clock.now() == pd.Timestamp("2024-03-01T00:00:00Z").to_pydatetime()
    assert clock.monotonic() == 60.5, "set() only moves the wall-clock reading"

    clock.advance_to(pd.Timestamp("2024-03-01T00:10:00Z").to_pydatetime())
    assert clock.now() == pd.Timestamp("2024-03-01T00:10:00Z").to_pydatetime()
    assert clock.monotonic() == 60.5 + 600.0

    clock.advance_to(pd.Timestamp("2024-03-01T00:10:00Z").to_pydatetime())
    assert clock.monotonic() == 660.5, "advance_to an identical instant is a no-op"


def test_manual_clock_rejects_naive_datetimes_and_negative_moves() -> None:
    clock = ManualClock()
    naive = pd.Timestamp("2024-01-01T00:00:00").to_pydatetime()
    with pytest.raises(ValueError, match="naive datetime"):
        ManualClock(naive)
    with pytest.raises(ValueError, match="naive datetime"):
        clock.set(naive)
    with pytest.raises(ValueError, match="naive datetime"):
        clock.advance_to(naive)
    with pytest.raises(ValueError, match="can only advance"):
        clock.advance(-1.0)
    with pytest.raises(ValueError, match="cannot move the clock backwards"):
        clock.advance_to(pd.Timestamp("2023-12-31T23:59:59Z").to_pydatetime())


# ---------------------------------------------------------------------------
# 6. the typed profile configuration
# ---------------------------------------------------------------------------


def test_profile_config_round_trips_and_defaults() -> None:
    payload = {
        "id": "btc-paper",
        "symbol": " BTC/USDT ",
        "timeframe": "4h",
        "strategy": "basic",
        "params": {"ema_fast": 9, "allow_short": False},
        "mode": "live",
        "initial_balance": 5_000.0,
        "stake_amount": 250.0,
        "exchange": "binance",
        "enabled": False,
        "warmup_candles": 50,
        "poll_interval_seconds": 2.5,
        "risk": {"max_open_positions": 2, "max_daily_trades": 0},
    }
    profile = ProfileConfig.model_validate(payload)
    assert profile.symbol == "BTC/USDT"
    assert profile.mode == "live"
    assert profile.risk.max_open_positions == 2
    assert profile.risk.max_daily_trades == 0
    assert profile.risk.max_position_notional is None
    dumped = profile.model_dump(mode="json")
    assert ProfileConfig.model_validate(dumped) == profile

    default = ProfileConfig(id="x", symbol="BTC/USDT")
    assert default.timeframe == "1h"
    assert default.strategy == "basic"
    assert default.mode == "paper"
    assert default.enabled is True
    assert default.warmup_candles == 200
    assert default.risk == RiskLimitsConfig()
    assert default.initial_balance > 0


def test_profile_config_forbids_a_credential_field() -> None:
    for key in ("api_key", "api_secret", "password", "token"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ProfileConfig.model_validate(minimal_profile(**{key: "s3cr3t"}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": ""},
        {"id": "has space"},
        {"id": "has/slash"},
        {"id": "-leading-dash"},
        {"id": "x" * 65},
        {"symbol": "   "},
        {"timeframe": "7m"},
        {"mode": "other"},
        {"initial_balance": 0},
        {"initial_balance": -1},
        {"stake_amount": 0},
        {"warmup_candles": 0},
        {"poll_interval_seconds": 0},
        {"exchange": 3.5},
    ],
)
def test_profile_config_rejects_invalid_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProfileConfig.model_validate(minimal_profile(**overrides))


def test_profile_config_accepts_the_live_mode_and_supported_timeframes() -> None:
    for mode in ("paper", "live"):
        for timeframe in ("1m", "5m", "15m", "30m", "1h", "4h", "1d"):
            profile = ProfileConfig(
                id=f"p-{timeframe}", symbol="BTC/USDT", timeframe=timeframe, mode=mode
            )
            assert profile.mode == mode
            assert profile.timeframe == timeframe


def test_risk_limits_bounds() -> None:
    assert RiskLimitsConfig(max_open_positions=0).max_open_positions == 0
    assert RiskLimitsConfig(max_drawdown_pct=1).max_drawdown_pct == 1
    assert RiskLimitsConfig(max_daily_trades=0).max_daily_trades == 0
    with pytest.raises(ValidationError):
        RiskLimitsConfig(max_open_positions=-1)
    with pytest.raises(ValidationError):
        RiskLimitsConfig(max_drawdown_pct=1.5)
    with pytest.raises(ValidationError):
        RiskLimitsConfig(max_order_notional=0)
    with pytest.raises(ValidationError):
        RiskLimitsConfig(max_daily_loss=0)
    with pytest.raises(ValidationError):
        RiskLimitsConfig(unknown_limit=1)


def test_realtime_and_monitoring_config_defaults() -> None:
    realtime = RealtimeConfig()
    assert realtime.state_db == Path("data/realtime/state.db")
    assert realtime.logs_dir == Path("data/realtime/logs")
    assert realtime.history_candles == 300
    assert realtime.benchmark_variant == "buy_and_hold"
    assert realtime.kill_switch_file is None
    assert realtime.start_at is None
    assert realtime.csv_dir is None
    assert default_realtime_config() == realtime

    monitoring = MonitoringConfig()
    assert monitoring.host == "127.0.0.1"
    assert monitoring.port == 8080
    assert monitoring.refresh_seconds == 2.0
    assert monitoring.max_request_bytes == 65536
    assert default_monitoring_config() == monitoring

    for model, payload in ((RealtimeConfig, {"port": 1}), (MonitoringConfig, {"state_db": "x"})):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_app_config_gains_the_two_new_sections() -> None:
    config = AppConfig()
    assert config.realtime == RealtimeConfig()
    assert config.monitoring == MonitoringConfig()
    fields = list(AppConfig.model_fields)
    assert fields[-3:] == ["realtime", "monitoring", "forecast"]
    assert "realtime" in AppConfig.model_validate({}).model_dump()
    assert "monitoring" in AppConfig.model_validate({}).model_dump()


# ---------------------------------------------------------------------------
# 7. load_profiles / load_realtime_config / load_monitoring_config
# ---------------------------------------------------------------------------


def test_load_profiles_reads_every_profile_in_order(tmp_path: Path) -> None:
    path = write_profiles(
        tmp_path,
        {
            "profiles": [
                minimal_profile(id="btc-paper", symbol="BTC/USDT"),
                minimal_profile(id="eth-paper", symbol="ETH/USDT", enabled=False),
            ],
            "realtime": {"poll_interval_seconds": 1.5},
            "monitoring": {"port": 9001},
        },
    )
    profiles = load_profiles(path)
    assert [profile.id for profile in profiles] == ["btc-paper", "eth-paper"]
    assert profiles[1].enabled is False
    assert all(isinstance(profile, ProfileConfig) for profile in profiles)
    # load_profiles ignores the two other root keys.
    assert load_realtime_config(path).poll_interval_seconds == 1.5
    assert load_monitoring_config(path).port == 9001


def test_load_profiles_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration file not found"):
        load_profiles(tmp_path / "nope.json")


def test_load_profiles_rejects_a_non_json_suffix(tmp_path: Path) -> None:
    target = tmp_path / "profiles.yaml"
    target.write_text("profiles: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported configuration file format"):
        load_profiles(target)


def test_load_profiles_rejects_invalid_json(tmp_path: Path) -> None:
    target = tmp_path / "profiles.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_profiles(target)


def test_load_profiles_rejects_a_non_object_root(tmp_path: Path) -> None:
    path = write_profiles(tmp_path, [{"id": "btc-paper", "symbol": "BTC/USDT"}])
    with pytest.raises(ConfigError, match="must contain a JSON object, got list"):
        load_profiles(path)


def test_load_profiles_rejects_an_unknown_root_key(tmp_path: Path) -> None:
    path = write_profiles(
        tmp_path, {"profiles": [minimal_profile()], "backtest": {"initial_balance": 1}}
    )
    with pytest.raises(
        ConfigError,
        match=r"unknown key at the root of the profiles file .*: 'backtest' "
        r"\(allowed: monitoring, profiles, realtime\)",
    ):
        load_profiles(path)


def test_load_profiles_requires_the_profiles_key(tmp_path: Path) -> None:
    path = write_profiles(tmp_path, {"realtime": {"poll_interval_seconds": 1}})
    with pytest.raises(ConfigError, match="declares no 'profiles' key"):
        load_profiles(path)


def test_load_profiles_rejects_a_non_list_profiles_key(tmp_path: Path) -> None:
    path = write_profiles(tmp_path, {"profiles": {"id": "btc-paper"}})
    with pytest.raises(ConfigError, match="must be a JSON list, got dict"):
        load_profiles(path)


def test_load_profiles_rejects_an_empty_list(tmp_path: Path) -> None:
    path = write_profiles(tmp_path, {"profiles": []})
    with pytest.raises(ConfigError, match="the profiles file declares no profile"):
        load_profiles(path)


def test_load_profiles_rejects_a_duplicate_id(tmp_path: Path) -> None:
    path = write_profiles(
        tmp_path,
        {"profiles": [minimal_profile(id="dup"), minimal_profile(id="dup")]},
    )
    with pytest.raises(ConfigError, match="duplicate profile id: 'dup'"):
        load_profiles(path)


def test_load_profiles_rejects_an_entry_that_is_not_an_object(tmp_path: Path) -> None:
    path = write_profiles(tmp_path, {"profiles": ["btc-paper"]})
    with pytest.raises(ConfigError, match=r"invalid profile at index 0: expected a JSON object"):
        load_profiles(path)


def test_load_profiles_rejects_an_entry_carrying_a_credential(tmp_path: Path) -> None:
    path = write_profiles(
        tmp_path,
        {"profiles": [minimal_profile(api_key="s3cr3t")]},
    )
    with pytest.raises(ConfigError) as excinfo:
        load_profiles(path)
    message = str(excinfo.value)
    assert "invalid profile at index 0" in message
    assert "api_key" in message
    assert "s3cr3t" not in message, "an error message must never echo a credential"


def test_load_profiles_rejects_an_invalid_profile(tmp_path: Path) -> None:
    path = write_profiles(tmp_path, {"profiles": [minimal_profile(mode="other")]})
    with pytest.raises(ConfigError, match="invalid profile at index 0: mode:"):
        load_profiles(path)


def test_load_realtime_config_defaults_and_overrides() -> None:
    assert load_realtime_config() == RealtimeConfig()
    assert load_realtime_config(None) == default_realtime_config()
    overridden = load_realtime_config(None, {"poll_interval_seconds": 2.5})
    assert overridden.poll_interval_seconds == 2.5
    prefixed = load_realtime_config(None, {"realtime.history_candles": 42})
    assert prefixed.history_candles == 42
    assert load_realtime_config(None).history_candles == 300


def test_load_monitoring_config_defaults_and_overrides(tmp_path: Path) -> None:
    assert load_monitoring_config() == MonitoringConfig()
    path = write_profiles(
        tmp_path,
        {"profiles": [minimal_profile()], "monitoring": {"host": "0.0.0.0", "port": 0}},
    )
    config = load_monitoring_config(path)
    assert config.host == "0.0.0.0"
    assert config.port == 0
    assert config.refresh_seconds == 2.0
    assert load_monitoring_config(None, {"monitoring.refresh_seconds": 5.0}).refresh_seconds == 5.0
    assert load_monitoring_config(None, {"port": 9999}).port == 9999


def test_load_section_rejects_bad_input(tmp_path: Path) -> None:
    bad_section = write_profiles(tmp_path, {"profiles": [minimal_profile()], "realtime": []})
    with pytest.raises(
        ConfigError, match=r"the 'realtime' key of .* must be a JSON object, got list"
    ):
        load_realtime_config(bad_section)

    bad_value = write_profiles(
        tmp_path, {"profiles": [minimal_profile()], "monitoring": {"port": 70_000}}, name="bad.json"
    )
    with pytest.raises(ConfigError, match="invalid monitoring configuration: port:"):
        load_monitoring_config(bad_value)

    unknown_key = write_profiles(
        tmp_path,
        {"profiles": [minimal_profile()], "realtime": {"unknown_setting": 1}},
        name="unknown.json",
    )
    with pytest.raises(ConfigError, match="unknown_setting"):
        load_realtime_config(unknown_key)

    other_section = write_profiles(tmp_path, {"profiles": [minimal_profile()]}, name="plain.json")
    with pytest.raises(ConfigError, match="monitoring"):
        load_realtime_config(other_section, {"monitoring.port": 1234})


# ---------------------------------------------------------------------------
# 8. config/profiles.example.json
# ---------------------------------------------------------------------------

CREDENTIAL_KEYS = re.compile(r"api_key|api_secret|password|secret|token", re.IGNORECASE)


def test_example_profiles_file_is_credential_free() -> None:
    raw = EXAMPLE_PROFILES.read_text(encoding="utf-8")
    assert CREDENTIAL_KEYS.search(raw) is None, "the example file must contain no credential key"
    payload = json.loads(raw)
    assert set(payload) == {"profiles", "realtime", "monitoring"}
    assert all(
        CREDENTIAL_KEYS.search(key) is None for profile in payload["profiles"] for key in profile
    )


def test_example_profiles_file_loads_through_every_loader() -> None:
    profiles = load_profiles(EXAMPLE_PROFILES)
    assert [profile.id for profile in profiles] == ["btc-paper", "eth-paper"]
    assert all(profile.enabled for profile in profiles)
    assert all(profile.mode == "paper" for profile in profiles)
    assert {profile.symbol for profile in profiles} == {"BTC/USDT", "ETH/USDT"}

    realtime = load_realtime_config(EXAMPLE_PROFILES)
    assert realtime.state_db == Path("data/realtime/state.db")
    assert realtime.kill_switch_file == Path("data/realtime/KILL_SWITCH")
    assert realtime.allow_network is True
    assert realtime.start_at is not None

    monitoring = load_monitoring_config(EXAMPLE_PROFILES)
    assert monitoring.host == "127.0.0.1"
    assert monitoring.port == 8080
    assert monitoring.refresh_seconds == 2.0


def test_example_profiles_declare_every_documented_field() -> None:
    payload = json.loads(EXAMPLE_PROFILES.read_text(encoding="utf-8"))
    profile_keys = set(ProfileConfig.model_fields)
    risk_keys = set(RiskLimitsConfig.model_fields)
    for profile in payload["profiles"]:
        assert set(profile) == profile_keys
        assert set(profile["risk"]) == risk_keys
    assert set(payload["realtime"]) == set(RealtimeConfig.model_fields)
    assert set(payload["monitoring"]) == set(MonitoringConfig.model_fields)


def test_gitignore_covers_the_realtime_runtime_state() -> None:
    lines = {
        line.strip() for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert "data/realtime/" in lines
    for pattern in ("*.db", "*.db-wal", "*.db-shm", "*.db.lock", "*.sqlite3", "realtime-*.log"):
        assert pattern in lines


# ---------------------------------------------------------------------------
# 9. lazy packaging
# ---------------------------------------------------------------------------


def test_importing_the_package_is_lazy_and_side_effect_free() -> None:
    code = (
        "import json, sys\n"
        "import trading_platform.realtime as rt\n"
        "payload = {\n"
        "    'clock': rt.Clock.__name__,\n"
        "    'manual': rt.ManualClock.__name__,\n"
        "    'mode': rt.RunMode.PAPER.value,\n"
        "    'candle': rt.CandleEvent.__name__,\n"
        "    'gateway_exported': 'ExecutionGateway' in rt.__all__,\n"
        "    'names': sorted(m for m in sys.modules if m.startswith('trading_platform.realtime')),\n"
        "    'optional': sorted(m for m in ('ccxt', 'freqtrade') if m in sys.modules),\n"
        "}\n"
        "print(json.dumps(payload))\n"
    )
    result = run_python(code)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["clock"] == "Clock"
    assert payload["manual"] == "ManualClock"
    assert payload["mode"] == "paper"
    assert payload["candle"] == "CandleEvent"
    assert payload["gateway_exported"] is True
    assert "trading_platform.realtime.stream" not in payload["names"]
    assert "trading_platform.realtime.store" not in payload["names"]
    assert "trading_platform.realtime.broker" not in payload["names"]
    assert "trading_platform.realtime.gateway" not in payload["names"]
    assert payload["optional"] == []


def test_lazy_map_targets_only_realtime_sibling_modules() -> None:
    """Every lazily exported name points at a submodule of the realtime package."""
    from trading_platform.realtime import _LAZY

    assert _LAZY, "the lazy export map must not be empty"
    for name, module in _LAZY.items():
        assert isinstance(name, str) and name
        assert module in {
            "broker",
            "credentials",
            "gateway",
            "monitor",
            "observability",
            "orchestrator",
            "risk",
            "runner",
            "store",
            "strategies",
            "stream",
        }, f"{name} points at the unexpected module {module!r}"
    for name in ("MarketStream", "SqliteStateStore", "PaperBroker", "ExecutionGateway", "Monitor"):
        assert name in _LAZY


def test_lazy_getattr_resolves_a_sibling_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lazy path (import + getattr) is exercised without needing the siblings."""
    import types

    import trading_platform.realtime as realtime

    fake = types.ModuleType("trading_platform.realtime._fake_sibling")
    fake.MarketStream = "resolved"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "trading_platform.realtime._fake_sibling", fake)
    monkeypatch.setitem(realtime._LAZY, "MarketStream", "_fake_sibling")
    assert realtime.MarketStream == "resolved"
    assert "MarketStream" in dir(realtime)
    assert "RunMode" in dir(realtime)


def test_unknown_attribute_raises_attribute_error() -> None:
    import trading_platform.realtime as realtime

    with pytest.raises(AttributeError, match="has no attribute 'nope'"):
        _ = realtime.nope


def test_realtime_models_module_exports_are_sorted() -> None:
    from trading_platform.realtime import models as realtime_models

    assert realtime_models.__all__ == sorted(realtime_models.__all__)
    for name in realtime_models.__all__:
        assert getattr(realtime_models, name) is not None


def test_realtime_models_and_clock_are_exported_eagerly() -> None:
    import trading_platform.realtime as realtime

    for name in ("Clock", "ManualClock", "SystemClock", "RunMode", "CandleEvent", "Order"):
        assert name in realtime.__all__
    assert realtime.__all__ == sorted(realtime.__all__)
    assert realtime.RunMode is RunMode
    assert realtime.new_client_order_id is new_client_order_id
    assert realtime.ManualClock is ManualClock
