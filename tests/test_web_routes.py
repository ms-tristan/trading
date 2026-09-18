"""Tests of the pure monitoring router (work package wp9).

Everything here is offline, deterministic and socket-free: the router is a pure
function, so the whole JSON contract of section 4 of the delivery brief is
exercised without a server.  Time comes from an injected ``ManualClock``, the
data comes from a **local** ``FakeStore``/``FakeProvider`` (never from SQLite,
never from the orchestrator) and the metrics block is compared against a direct
call of ``Monitor.metrics``, so a payload invented by the router instead of read
from the read model would fail these tests.

Layer 7 is a **pure JSON API**: the HTML document and the two static assets it
used to serve are gone, and ``GET /`` as well as every ``/static/...`` path is
pinned here as the documented JSON ``404``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest

from trading_platform.config.models import MonitoringConfig, ProfileConfig
from trading_platform.core.errors import (
    ConfigError,
    MonitoringError,
    ProfileError,
    StateStoreError,
)
from trading_platform.core.models import Direction, ExitReason, TradeRecord
from trading_platform.realtime.catalog import default_catalog_body
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import (
    EngineCounters,
    EquityPoint,
    Order,
    OrderSide,
    OrderState,
    OrderType,
    PlatformSnapshot,
    Position,
    ProfileHealth,
    ProfileSnapshot,
    ProfileState,
    ProfileStatus,
    RunMode,
)
from trading_platform.realtime.monitor import Monitor
from trading_platform.realtime.store import CandleRow
from trading_platform.web import routes as web_routes
from trading_platform.web.routes import (
    CANDLE_ROUTE_DEFAULT_LIMIT,
    CANDLE_ROUTE_MAX_LIMIT,
    CatalogProvider,
    HttpResponse,
    ProfileController,
    Router,
    SnapshotProvider,
    operator_token_from_env,
)

# ---------------------------------------------------------------------------
# deterministic reference values
# ---------------------------------------------------------------------------

PROFILE_A = "btc-paper"
PROFILE_B = "eth-live"
UNKNOWN_PROFILE = "ghost"
START = datetime(2024, 1, 1, tzinfo=UTC)

#: Repository root, used to pin the absence of the removed asset directory.
REPO_ROOT = Path(__file__).resolve().parents[1]

HEALTH_KEYS = [
    "checked_at",
    "kill_switch",
    "profiles_running",
    "profiles_total",
    "status",
    "uptime_seconds",
    "version",
]

EQUITY_POINT_KEYS = ["cash", "equity", "position_value", "timestamp"]

TRADES_KEYS = ["count", "trades"]
ORDERS_KEYS = ["orders"]
POSITIONS_KEYS = ["positions"]
METRICS_KEYS = ["benchmark", "generated_at", "metrics"]
PROFILES_KEYS = ["generated_at", "profiles"]
PROFILE_KEYS = sorted(
    [
        "cash",
        "equity",
        "health",
        "initial_balance",
        "mode",
        "n_trades",
        "open_positions",
        "position_value",
        "profile_id",
        "started_at",
        "status",
        "strategy",
        "symbol",
        "timeframe",
        "total_return",
        "updated_at",
    ]
)
KILL_SWITCH_KEYS = ["changed_at", "kill_switch", "reason"]

#: Exact keys of ``GET /api/catalog`` and of one symbol entry.
CATALOG_KEYS = ["modes", "strategies", "symbols", "timeframes"]
CATALOG_SYMBOL_KEYS = ["base", "quote", "symbol"]

#: Exact keys of ``GET /api/control`` and of one profile entry.
CONTROL_KEYS = ["engine_running", "mutable", "profiles", "read_only"]
CONTROL_PROFILE_KEYS = ["paused", "profile_id", "running"]

#: Exact keys of ``GET /api/profiles/{id}/candles`` and of one candle.
CANDLES_KEYS = ["candles", "count"]
CANDLE_KEYS = ["close", "closed", "high", "low", "open", "profile_id", "timestamp", "volume"]

#: Malformed values of the ``limit`` query parameter (all answered with a 400).
MALFORMED_LIMITS = ["limit=0", "limit=-1", "limit=abc", "limit=", "limit=1.5", "limit=%20"]


def payload_of(response: HttpResponse) -> dict[str, Any]:
    """Decode the JSON body of ``response`` (every body of this layer is JSON)."""
    decoded = json.loads(response.body.decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def header_value(response: HttpResponse, name: str) -> str | None:
    """Return the header ``name`` of ``response``, case-insensitively."""
    for key, value in response.headers:
        if key.lower() == name.lower():
            return value
    return None


def finite(value: float) -> float | None:
    """Return ``value`` when it is finite, else ``None`` (the wire rule)."""
    return value if math.isfinite(value) else None


# ---------------------------------------------------------------------------
# local fakes: a StateStore for the Monitor, a SnapshotProvider for the router
# ---------------------------------------------------------------------------


class FakeStore:
    """Minimal in-memory ``StateStore`` covering what the read model reads."""

    def __init__(
        self,
        specs: Sequence[ProfileConfig] = (),
        equity: Mapping[str, Sequence[EquityPoint]] | None = None,
        trades: Mapping[str, Sequence[TradeRecord]] | None = None,
        positions: Mapping[str, Sequence[Position]] | None = None,
        orders: Mapping[str, Sequence[Order]] | None = None,
        states: Mapping[str, ProfileState] | None = None,
        *,
        failures: type[Exception] | None = None,
    ) -> None:
        self._specs = list(specs)
        self._equity = dict(equity or {})
        self._trades = dict(trades or {})
        self._positions = dict(positions or {})
        self._orders = dict(orders or {})
        self._states = dict(states or {})
        self._failures = failures

    def _maybe_fail(self) -> None:
        if self._failures is not None:
            raise self._failures("cannot read the persistent state")

    def load_profiles(self) -> list[ProfileConfig]:
        self._maybe_fail()
        return list(self._specs)

    def equity_curve(self, profile_id: str) -> list[EquityPoint]:
        self._maybe_fail()
        return list(self._equity.get(profile_id, ()))

    def list_trades(self, profile_id: str) -> list[TradeRecord]:
        self._maybe_fail()
        return list(self._trades.get(profile_id, ()))

    def list_positions(self, profile_id: str) -> list[Position]:
        self._maybe_fail()
        return list(self._positions.get(profile_id, ()))

    def list_orders(self, profile_id: str, *, limit: int = 100) -> list[Order]:
        self._maybe_fail()
        return list(self._orders.get(profile_id, ()))[:limit]

    def profile_state(self, profile_id: str) -> ProfileState:
        self._maybe_fail()
        known = self._states.get(profile_id)
        if known is not None:
            return known
        return ProfileState(
            profile_id=profile_id,
            status=ProfileStatus.STOPPED,
            mode=RunMode.PAPER,
        )


@dataclass(frozen=True)
class FakeKillSwitchState:
    """Local stand-in of the risk layer's ``KillSwitchState`` dataclass."""

    engaged: bool
    reason: str = ""
    changed_at: Any = None
    source: str = "api"


@dataclass(frozen=True)
class FakeCandle:
    """Local stand-in of a persisted candle row (duck-typed by the route)."""

    profile_id: str
    timestamp: Any
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool


def make_candles(profile_id: str, count: int) -> list[FakeCandle]:
    """Build ``count`` deterministic candles, oldest first, the last one open."""
    return [
        FakeCandle(
            profile_id=profile_id,
            timestamp=pd.Timestamp(START + timedelta(hours=step)),
            open=100.0 + step,
            high=105.0 + step,
            low=95.0 + step,
            close=101.0 + step,
            volume=10.0 + step,
            closed=step < count - 1,
        )
        for step in range(count)
    ]


@dataclass
class FakeProvider:
    """Local implementation of :class:`SnapshotProvider` (no orchestrator)."""

    profiles: tuple[ProfileSnapshot, ...] = ()
    kill_switch: bool = False
    kill_switch_reason: str = ""
    kill_switch_changed_at: Any = None
    version: str = "0.1.0"
    uptime_seconds: float = 42.5
    health_body: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "ok",
            "profiles_total": 2,
            "profiles_running": 1,
            "uptime_seconds": 42.5,
        }
    )
    snapshot_error: Exception | None = None
    kill_state_form: str = "dataclass"
    engaged_calls: list[str] = field(default_factory=list)
    released_calls: int = 0
    candles: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    candle_error: Exception | None = None
    candle_calls: list[tuple[str, int]] = field(default_factory=list)

    def snapshot(self) -> PlatformSnapshot:
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return PlatformSnapshot(
            profiles=self.profiles,
            generated_at=pd.Timestamp(START + timedelta(seconds=10)),
            kill_switch=self.kill_switch,
            kill_switch_reason=self.kill_switch_reason,
            version=self.version,
            uptime_seconds=self.uptime_seconds,
        )

    def health(self) -> dict[str, Any]:
        return dict(self.health_body)

    def profile_snapshot(self, profile_id: str) -> ProfileSnapshot | None:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        return None

    def candle_series(self, profile_id: str, limit: int) -> Sequence[Any]:
        self.candle_calls.append((profile_id, limit))
        if self.candle_error is not None:
            raise self.candle_error
        return list(self.candles.get(profile_id, ()))[:limit]

    def engage_kill_switch(self, reason: str) -> Any:
        self.engaged_calls.append(reason)
        self.kill_switch = True
        self.kill_switch_reason = reason
        self.kill_switch_changed_at = pd.Timestamp(START + timedelta(seconds=99))
        return self.kill_switch_state()

    def release_kill_switch(self) -> Any:
        self.released_calls += 1
        self.kill_switch = False
        self.kill_switch_reason = ""
        self.kill_switch_changed_at = None
        return self.kill_switch_state()

    def kill_switch_state(self) -> Any:
        if self.kill_state_form == "bool":
            return self.kill_switch
        if self.kill_state_form == "mapping":
            return {
                "engaged": self.kill_switch,
                "reason": self.kill_switch_reason,
                "changed_at": self.kill_switch_changed_at,
            }
        return FakeKillSwitchState(
            engaged=self.kill_switch,
            reason=self.kill_switch_reason,
            changed_at=self.kill_switch_changed_at,
        )


@dataclass
class FakeCatalog:
    """Local implementation of :class:`CatalogProvider` (never a network call)."""

    body: dict[str, Any] = field(
        default_factory=lambda: {
            "symbols": [{"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"}],
            "strategies": ["basic"],
            "timeframes": ["1m", "5m"],
            "modes": ["paper", "live"],
        }
    )
    error: Exception | None = None
    calls: int = 0

    def catalog(self) -> Mapping[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.body)


@dataclass
class FakeController:
    """Local implementation of :class:`ProfileController`, recording every call."""

    profiles: tuple[ProfileSnapshot, ...] = ()
    paused: bool = False
    failure: Exception | None = None
    control_failure: Exception | None = None
    pause_calls: list[str] = field(default_factory=list)
    resume_calls: list[str] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)
    create_calls: list[dict[str, Any]] = field(default_factory=list)

    def _maybe_fail(self) -> None:
        if self.failure is not None:
            raise self.failure

    def _snapshot(self, profile_id: str) -> ProfileSnapshot:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        return make_profile_snapshot(profile_id, "BTC/USDT")

    def pause_profile(self, profile_id: str) -> Any:
        self.pause_calls.append(profile_id)
        self._maybe_fail()
        self.paused = True
        return self._snapshot(profile_id)

    def resume_profile(self, profile_id: str) -> Any:
        self.resume_calls.append(profile_id)
        self._maybe_fail()
        self.paused = False
        return self._snapshot(profile_id)

    def delete_profile(self, profile_id: str) -> Any:
        self.delete_calls.append(profile_id)
        self._maybe_fail()
        return profile_id

    def create_profile(self, payload: Mapping[str, Any]) -> Any:
        self.create_calls.append(dict(payload))
        self._maybe_fail()
        return self._snapshot(str(payload.get("profile_id", "")))

    def control_state(self) -> Mapping[str, Any]:
        if self.control_failure is not None:
            raise self.control_failure
        return {
            "profiles": [
                {"profile_id": profile.profile_id, "paused": self.paused, "running": True}
                for profile in self.profiles
            ]
        }


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def make_health(profile_id: str, *, status: ProfileStatus = ProfileStatus.RUNNING) -> ProfileHealth:
    """Build a deterministic health block for one profile."""
    return ProfileHealth(
        profile_id=profile_id,
        status=status,
        last_candle_at=pd.Timestamp(START + timedelta(hours=3)),
        lag_seconds=12.5,
        last_error="",
        reconnect_count=2,
        counters=EngineCounters(
            candles_processed=17,
            orders_submitted=4,
            orders_filled=3,
            orders_rejected=1,
            stream_reconnects=2,
            risk_rejections=1,
            errors=0,
        ),
    )


def make_profile_snapshot(
    profile_id: str,
    symbol: str,
    *,
    mode: RunMode = RunMode.PAPER,
    status: ProfileStatus = ProfileStatus.RUNNING,
    equity: float = 10150.0,
) -> ProfileSnapshot:
    """Build a deterministic profile snapshot for one profile."""
    return ProfileSnapshot(
        profile_id=profile_id,
        symbol=symbol,
        timeframe="1h",
        strategy="basic",
        mode=mode,
        status=status,
        initial_balance=10000.0,
        equity=equity,
        cash=5000.0,
        position_value=5150.0,
        total_return=0.015,
        n_trades=3,
        open_positions=1,
        health=make_health(profile_id, status=status),
        started_at=pd.Timestamp(START),
        updated_at=pd.Timestamp(START + timedelta(hours=3)),
    )


def make_trade(entry: datetime) -> TradeRecord:
    """Build one deterministic closed round-trip."""
    return TradeRecord(
        entry_time=pd.Timestamp(entry),
        exit_time=pd.Timestamp(entry + timedelta(hours=1)),
        entry_price=100.0,
        exit_price=103.0,
        size=1.0,
        direction=Direction.LONG,
        pnl=3.0,
        pnl_pct=0.03,
        fees=0.1,
        exit_reason=ExitReason.SIGNAL,
        duration_minutes=60.0,
        params_id="params-1",
    )


def make_position(profile_id: str) -> Position:
    """Build one deterministic open position."""
    return Position(
        profile_id=profile_id,
        symbol="BTC/USDT",
        quantity=0.5,
        average_price=100.0,
        direction=Direction.LONG,
        opened_at=pd.Timestamp(START + timedelta(hours=1)),
        updated_at=pd.Timestamp(START + timedelta(hours=2)),
        unrealized_pnl=25.0,
    )


def make_order(profile_id: str) -> Order:
    """Build one deterministic order row."""
    return Order(
        client_order_id=f"{profile_id}-1",
        profile_id=profile_id,
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        quantity=0.5,
        state=OrderState.FILLED,
        mode=RunMode.PAPER,
        created_at=pd.Timestamp(START + timedelta(hours=1)),
        updated_at=pd.Timestamp(START + timedelta(hours=1)),
        filled_quantity=0.5,
        average_fill_price=100.0,
    )


def make_store(*, failures: type[Exception] | None = None) -> FakeStore:
    """Build a store holding two deterministic profiles."""
    specs = [
        ProfileConfig(id=PROFILE_A, symbol="BTC/USDT", strategy="basic", timeframe="1h"),
        ProfileConfig(
            id=PROFILE_B,
            symbol="ETH/USDT",
            strategy="basic",
            timeframe="1h",
            mode="live",
            initial_balance=20000.0,
        ),
    ]
    equity = {
        PROFILE_A: [
            EquityPoint(
                profile_id=PROFILE_A,
                timestamp=pd.Timestamp(START + timedelta(hours=step)),
                equity=10000.0 + 50.0 * step,
                cash=9000.0,
                position_value=1000.0 + 50.0 * step,
            )
            for step in range(3)
        ]
    }
    trades = {PROFILE_A: [make_trade(START)]}
    positions = {PROFILE_A: [make_position(PROFILE_A)]}
    orders = {PROFILE_A: [make_order(PROFILE_A)]}
    states = {
        PROFILE_A: ProfileState(
            profile_id=PROFILE_A,
            status=ProfileStatus.RUNNING,
            mode=RunMode.PAPER,
            last_candle_at=pd.Timestamp(START + timedelta(hours=3)),
            lag_seconds=12.5,
            updated_at=pd.Timestamp(START + timedelta(hours=3)),
        ),
        PROFILE_B: ProfileState(
            profile_id=PROFILE_B,
            status=ProfileStatus.DEGRADED,
            mode=RunMode.LIVE,
            last_candle_at=pd.Timestamp(START + timedelta(hours=2)),
            lag_seconds=3600.0,
            last_error="reconcile mismatch",
            reconnect_count=1,
        ),
    }
    return FakeStore(specs, equity, trades, positions, orders, states, failures=failures)


@pytest.fixture
def manual_clock() -> ManualClock:
    """Deterministic clock anchored on a fixed instant."""
    return ManualClock(START)


@pytest.fixture
def monitor(manual_clock: ManualClock) -> Monitor:
    """Read model over the local store (never SQLite)."""
    return Monitor(make_store(), clock=manual_clock)


@pytest.fixture
def provider() -> FakeProvider:
    """Snapshot provider holding the two deterministic profiles."""
    return FakeProvider(
        profiles=(
            make_profile_snapshot(PROFILE_A, "BTC/USDT"),
            make_profile_snapshot(
                PROFILE_B,
                "ETH/USDT",
                mode=RunMode.LIVE,
                status=ProfileStatus.DEGRADED,
            ),
        ),
        candles={PROFILE_A: make_candles(PROFILE_A, 4)},
    )


@pytest.fixture
def controller(provider: FakeProvider) -> FakeController:
    """Runtime controller exposing the two deterministic profiles."""
    return FakeController(profiles=provider.profiles)


def build_router(
    provider: SnapshotProvider,
    monitor: Monitor,
    clock: ManualClock,
    *,
    read_only: bool = True,
    operator_token: str | None = None,
    version: str = "0.1.0",
    controller: ProfileController | None = None,
    catalog: CatalogProvider | None = None,
) -> Router:
    """Build a router over the local fakes."""
    return Router(
        provider,
        monitor=monitor,
        config=MonitoringConfig(),
        read_only=read_only,
        operator_token=operator_token,
        version=version,
        clock=clock,
        controller=controller,
        catalog=catalog,
    )


@pytest.fixture
def router(provider: FakeProvider, monitor: Monitor, manual_clock: ManualClock) -> Router:
    """Read-only router (the default, and the mode of ``realtime serve``)."""
    return build_router(provider, monitor, manual_clock)


# ---------------------------------------------------------------------------
# layer 7 is a pure JSON API: no HTML document, no static asset
# ---------------------------------------------------------------------------

#: The removed static surface, as the contract spells it: the requested path.
STATIC_PATHS = [
    "/static/app.js",
    "/static/styles.css",
    "/static/index.html",
    "/static/server.py",
    "/static/unknown.js",
    "/static/",
    "/static/app.js/extra",
    "/static/../routes.py",
    "/static/%2e%2e/routes.py",
    "/static/..%2Froutes.py",
]


def test_the_root_path_is_the_documented_json_404(router: Router) -> None:
    """``GET /`` serves no HTML document any more: it is an unknown route."""
    response = router.handle("GET", "/")
    assert response.status == 404
    assert response.content_type == "application/json; charset=utf-8"
    assert payload_of(response) == {"error": "not found: /"}
    assert header_value(response, "Allow") is None


@pytest.mark.parametrize("path", STATIC_PATHS)
def test_every_static_path_is_the_documented_json_404(router: Router, path: str) -> None:
    """Every ``/static/...`` path echoes itself in the documented 404 body."""
    response = router.handle("GET", path)
    assert response.status == 404
    assert response.content_type == "application/json; charset=utf-8"
    assert payload_of(response) == {"error": f"not found: {path}"}
    assert header_value(response, "Allow") is None


def test_a_404_carries_no_allowed_method(router: Router) -> None:
    """The two removed surfaces advertise no method at all (a 404 has no ``Allow``)."""
    assert router.allowed_methods("/") == ()
    assert router.allowed_methods("/static/app.js") == ()


def test_the_static_asset_directory_is_gone() -> None:
    """The hand-written dashboard is deleted, not left orphaned in the package."""
    static = REPO_ROOT / "src" / "trading_platform" / "web" / "static"
    assert not static.exists()


def test_the_router_module_exposes_no_html_or_static_surface() -> None:
    """The removed constants must not come back through a stale definition."""
    assert not hasattr(web_routes, "STATIC_ASSETS")
    assert not hasattr(web_routes, "HTML_CONTENT_TYPE")


# ---------------------------------------------------------------------------
# platform routes: exact key sets, empty provider
# ---------------------------------------------------------------------------


def test_health_payload_has_exactly_the_documented_keys(
    router: Router, manual_clock: ManualClock
) -> None:
    response = router.handle("GET", "/api/health")
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == HEALTH_KEYS
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"
    assert body["uptime_seconds"] == 42.5
    assert body["profiles_total"] == 2
    assert body["profiles_running"] == 1
    assert body["kill_switch"] is False
    assert body["checked_at"] == manual_clock.now().isoformat()


def test_health_is_degraded_when_the_kill_switch_is_engaged(
    monitor: Monitor, manual_clock: ManualClock
) -> None:
    provider = FakeProvider(
        profiles=(make_profile_snapshot(PROFILE_A, "BTC/USDT"),),
        kill_switch=True,
        kill_switch_reason="operator stopped everything",
    )
    router = build_router(provider, monitor, manual_clock)
    body = payload_of(router.handle("GET", "/api/health"))
    assert body["status"] == "degraded"
    assert body["kill_switch"] is True


def test_health_accepts_a_boolean_kill_switch_state(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    provider.kill_state_form = "bool"
    provider.kill_switch = True
    router = build_router(provider, monitor, manual_clock)
    assert payload_of(router.handle("GET", "/api/health"))["kill_switch"] is True


def test_health_falls_back_to_the_snapshot_for_a_partial_health_body(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    provider.health_body = {"status": "ok"}
    router = build_router(provider, monitor, manual_clock)
    body = payload_of(router.handle("GET", "/api/health"))
    assert body["profiles_total"] == 2
    assert body["profiles_running"] == 1
    assert body["uptime_seconds"] == 42.5


def test_profiles_payload_has_exactly_the_documented_keys(router: Router) -> None:
    response = router.handle("GET", "/api/profiles")
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == PROFILES_KEYS
    assert [item["profile_id"] for item in body["profiles"]] == [PROFILE_A, PROFILE_B]
    assert body["profiles"][0] == make_profile_snapshot(PROFILE_A, "BTC/USDT").to_dict()
    assert body["generated_at"] == pd.Timestamp(START + timedelta(seconds=10)).isoformat()


def test_empty_provider_still_answers_every_platform_route(
    monitor: Monitor, manual_clock: ManualClock
) -> None:
    provider = FakeProvider(
        profiles=(),
        health_body={
            "status": "ok",
            "profiles_total": 0,
            "profiles_running": 0,
            "uptime_seconds": 0.0,
        },
    )
    router = build_router(provider, monitor, manual_clock)

    assert payload_of(router.handle("GET", "/api/profiles")) == {
        "profiles": [],
        "generated_at": pd.Timestamp(START + timedelta(seconds=10)).isoformat(),
    }
    health = payload_of(router.handle("GET", "/api/health"))
    assert sorted(health) == HEALTH_KEYS
    assert health["profiles_total"] == 0
    assert health["profiles_running"] == 0
    assert payload_of(router.handle("GET", "/")) == {"error": "not found: /"}
    assert router.handle("GET", "/api/profiles/" + PROFILE_A).status == 404


# ---------------------------------------------------------------------------
# per-profile routes
# ---------------------------------------------------------------------------


def test_profile_detail_matches_the_snapshot_payload(router: Router) -> None:
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}")
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == PROFILE_KEYS
    assert body == make_profile_snapshot(PROFILE_A, "BTC/USDT").to_dict()


def test_unknown_profile_is_a_documented_404(router: Router) -> None:
    response = router.handle("GET", f"/api/profiles/{UNKNOWN_PROFILE}")
    assert response.status == 404
    assert payload_of(response) == {"error": f"unknown profile: {UNKNOWN_PROFILE!r}"}


@pytest.mark.parametrize("leaf", ["equity", "trades", "orders", "positions", "metrics"])
def test_unknown_profile_subroutes_are_a_documented_404(router: Router, leaf: str) -> None:
    response = router.handle("GET", f"/api/profiles/{UNKNOWN_PROFILE}/{leaf}")
    assert response.status == 404
    assert payload_of(response) == {"error": f"unknown profile: {UNKNOWN_PROFILE!r}"}


def test_equity_points_carry_exactly_the_documented_keys(router: Router) -> None:
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/equity")
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == ["points"]
    assert len(body["points"]) == 3
    for point in body["points"]:
        assert sorted(point) == EQUITY_POINT_KEYS
    assert body["points"][0]["equity"] == 10000.0
    assert body["points"][0]["timestamp"] == pd.Timestamp(START).isoformat()


def test_trades_orders_and_positions_payloads(router: Router) -> None:
    trades = payload_of(router.handle("GET", f"/api/profiles/{PROFILE_A}/trades"))
    assert sorted(trades) == TRADES_KEYS
    assert trades["count"] == 1
    assert trades["trades"] == [make_trade(START).to_dict()]

    orders = payload_of(router.handle("GET", f"/api/profiles/{PROFILE_A}/orders"))
    assert sorted(orders) == ORDERS_KEYS
    assert orders["orders"] == [make_order(PROFILE_A).to_dict()]

    positions = payload_of(router.handle("GET", f"/api/profiles/{PROFILE_A}/positions"))
    assert sorted(positions) == POSITIONS_KEYS
    assert positions["positions"] == [make_position(PROFILE_A).to_dict()]


def test_empty_profile_detail_is_200_with_empty_collections(router: Router) -> None:
    assert payload_of(router.handle("GET", f"/api/profiles/{PROFILE_B}/equity")) == {"points": []}
    assert payload_of(router.handle("GET", f"/api/profiles/{PROFILE_B}/trades")) == {
        "trades": [],
        "count": 0,
    }
    assert payload_of(router.handle("GET", f"/api/profiles/{PROFILE_B}/orders")) == {"orders": []}
    assert payload_of(router.handle("GET", f"/api/profiles/{PROFILE_B}/positions")) == {
        "positions": []
    }


def test_metrics_payload_delegates_to_the_read_model(
    router: Router, monitor: Monitor, manual_clock: ManualClock
) -> None:
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/metrics")
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == METRICS_KEYS
    assert body["generated_at"] == manual_clock.now().isoformat()

    reference = monitor.metrics(PROFILE_A)
    assert sorted(body["metrics"]) == sorted(reference)
    for name, value in reference.items():
        assert body["metrics"][name] == finite(value)

    benchmark = body["benchmark"]
    assert benchmark is None or isinstance(benchmark, Mapping)


# ---------------------------------------------------------------------------
# routing errors: 404, 405 and the Allow header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api",
        "/api/unknown",
        "/api/health/extra",
        "/api/kill-switch/extra",
        "/api/profiles/",
        "/api/profiles//equity",
        f"/api/profiles/{PROFILE_A}/",
        f"/api/profiles/{PROFILE_A}/equity/",
        f"/api/profiles/{PROFILE_A}/unknown",
        "/nope",
    ],
)
def test_unknown_routes_are_a_documented_404(router: Router, path: str) -> None:
    response = router.handle("GET", path)
    assert response.status == 404
    assert payload_of(response) == {"error": f"not found: {path}"}


@pytest.mark.parametrize(
    "path,allow",
    [
        ("/api/health", "GET, HEAD"),
        (f"/api/profiles/{PROFILE_A}", "GET, HEAD, DELETE"),
    ],
)
def test_post_on_a_get_route_is_405_with_an_allow_header(
    router: Router, path: str, allow: str
) -> None:
    """``POST`` is refused wherever it is not a route, with the documented ``Allow``."""
    response = router.handle("POST", path, body=b"{}")
    assert response.status == 405
    assert payload_of(response) == {"error": "method not allowed"}
    assert header_value(response, "Allow") == allow


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_unsupported_methods_are_405(router: Router, method: str) -> None:
    """The collection answers ``GET``/``HEAD`` and -- since the creation route -- ``POST``."""
    response = router.handle(method, "/api/profiles")
    assert response.status == 405
    assert header_value(response, "Allow") == "GET, HEAD, POST"


def test_unsupported_method_on_an_unknown_path_is_404(router: Router) -> None:
    response = router.handle("PUT", "/api/nowhere")
    assert response.status == 404
    assert header_value(response, "Allow") is None


def test_allowed_methods_advertises_the_documented_tuples(router: Router) -> None:
    assert router.allowed_methods("/") == ()
    assert router.allowed_methods("/static/app.js") == ()
    assert router.allowed_methods("/api/health") == ("GET", "HEAD")
    assert router.allowed_methods(f"/api/profiles/{PROFILE_A}/equity") == ("GET", "HEAD")
    assert router.allowed_methods("/api/kill-switch") == ("GET", "HEAD", "POST")
    assert router.allowed_methods("/api/nowhere") == ()


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/health"),
        ("GET", "/api/profiles"),
        ("GET", f"/api/profiles/{PROFILE_A}"),
        ("GET", f"/api/profiles/{PROFILE_A}/equity"),
        ("GET", f"/api/profiles/{PROFILE_A}/trades"),
        ("GET", f"/api/profiles/{PROFILE_A}/orders"),
        ("GET", f"/api/profiles/{PROFILE_A}/positions"),
        ("GET", f"/api/profiles/{PROFILE_A}/metrics"),
        ("GET", "/api/kill-switch"),
        ("GET", "/"),
        ("GET", "/static/app.js"),
    ],
)
def test_head_answers_like_get_with_an_empty_body(router: Router, method: str, path: str) -> None:
    reference = router.handle("GET", path)
    response = router.handle("HEAD", path)
    assert response.status == reference.status
    assert response.content_type == reference.content_type
    assert response.headers == reference.headers
    assert response.body == b""


def test_query_parameters_do_not_change_the_routing(router: Router) -> None:
    response = router.handle("GET", "/api/health", query="foo=bar")
    assert response.status == 200
    assert sorted(payload_of(response)) == HEALTH_KEYS


# ---------------------------------------------------------------------------
# kill switch: authentication, body validation, and the read counterpart
# ---------------------------------------------------------------------------


def test_get_kill_switch_returns_the_current_state(router: Router, provider: FakeProvider) -> None:
    body = payload_of(router.handle("GET", "/api/kill-switch"))
    assert sorted(body) == KILL_SWITCH_KEYS
    assert body["kill_switch"] is False
    assert body["reason"] == ""
    assert body["changed_at"] is None

    provider.kill_switch = True
    provider.kill_switch_reason = "manual"
    provider.kill_switch_changed_at = pd.Timestamp(START)
    body = payload_of(router.handle("GET", "/api/kill-switch"))
    assert body["kill_switch"] is True
    assert body["reason"] == "manual"
    assert body["changed_at"] == pd.Timestamp(START).isoformat()


def test_kill_switch_post_engages_and_persists_through_the_provider(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    router = build_router(provider, monitor, manual_clock, read_only=False, operator_token="s3cret")
    response = router.handle(
        "POST",
        "/api/kill-switch",
        body=json.dumps({"engage": True, "reason": "operator"}).encode(),
        headers={"X-Operator-Token": "s3cret"},
    )
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == KILL_SWITCH_KEYS
    assert body["kill_switch"] is True
    assert body["reason"] == "operator"
    assert body["changed_at"] == pd.Timestamp(START + timedelta(seconds=99)).isoformat()
    assert provider.engaged_calls == ["operator"]


def test_kill_switch_post_releases(monitor: Monitor, manual_clock: ManualClock) -> None:
    provider = FakeProvider(
        profiles=(make_profile_snapshot(PROFILE_A, "BTC/USDT"),),
        kill_switch=True,
        kill_switch_reason="earlier",
    )
    router = build_router(provider, monitor, manual_clock, read_only=False, operator_token="tok")
    response = router.handle(
        "POST",
        "/api/kill-switch",
        body=b'{"engage": false, "reason": "resume"}',
        headers={"x-operator-token": "tok"},
    )
    assert response.status == 200
    body = payload_of(response)
    assert body["kill_switch"] is False
    assert body["reason"] == ""
    assert body["changed_at"] == manual_clock.now().isoformat()
    assert provider.released_calls == 1


def test_kill_switch_post_without_a_reason_is_accepted(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    router = build_router(provider, monitor, manual_clock, read_only=False, operator_token="tok")
    response = router.handle(
        "POST",
        "/api/kill-switch",
        body=b'{"engage": true}',
        headers={"X-Operator-Token": "tok"},
    )
    assert response.status == 200
    assert payload_of(response)["reason"] == ""


def test_kill_switch_post_is_403_on_a_read_only_server(
    router: Router, provider: FakeProvider
) -> None:
    response = router.handle("POST", "/api/kill-switch", body=b'{"engage": true, "reason": "x"}')
    assert response.status == 403
    assert payload_of(response) == {"error": "mutations are disabled on this server"}
    assert provider.engaged_calls == []


def test_kill_switch_post_is_403_without_a_configured_token(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    router = build_router(provider, monitor, manual_clock, read_only=False, operator_token="")
    response = router.handle(
        "POST",
        "/api/kill-switch",
        body=b'{"engage": true, "reason": "x"}',
        headers={"X-Operator-Token": "anything"},
    )
    assert response.status == 403
    assert payload_of(response) == {"error": "mutations are disabled on this server"}
    assert provider.engaged_calls == []


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"X-Operator-Token": "wrong"},
        {"X-Operator-Token": ""},
        {"Authorization": "Bearer s3cret"},
    ],
)
def test_kill_switch_post_is_403_without_the_right_token(
    monitor: Monitor,
    manual_clock: ManualClock,
    provider: FakeProvider,
    headers: dict[str, str] | None,
) -> None:
    router = build_router(provider, monitor, manual_clock, read_only=False, operator_token="s3cret")
    response = router.handle(
        "POST",
        "/api/kill-switch",
        body=b'{"engage": true, "reason": "x"}',
        headers=headers,
    )
    assert response.status == 403
    assert payload_of(response) == {"error": "missing or invalid operator token"}
    assert "s3cret" not in response.body.decode()
    assert provider.engaged_calls == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        b"[]",
        b'"engage"',
        b"null",
        b"{}",
        b'{"engage": "yes"}',
        b'{"engage": 1}',
        b'{"engage": null}',
        b'{"engage": true, "reason": 3}',
        b'{"engage": true, "reason": {"nested": 1}}',
    ],
)
def test_kill_switch_post_malformed_bodies_are_400(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider, body: bytes
) -> None:
    router = build_router(provider, monitor, manual_clock, read_only=False, operator_token="tok")
    response = router.handle(
        "POST", "/api/kill-switch", body=body, headers={"X-Operator-Token": "tok"}
    )
    assert response.status == 400
    assert sorted(payload_of(response)) == ["error"]
    assert provider.engaged_calls == []


def test_kill_switch_post_without_a_body_is_400(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    router = build_router(provider, monitor, manual_clock, read_only=False, operator_token="tok")
    response = router.handle(
        "POST", "/api/kill-switch", body=b"", headers={"X-Operator-Token": "tok"}
    )
    assert response.status == 400


# ---------------------------------------------------------------------------
# failure surface: 500 with a message, never a stack trace
# ---------------------------------------------------------------------------


def test_a_crashing_provider_is_a_500_without_a_traceback(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    provider.snapshot_error = RuntimeError("boom")
    router = build_router(provider, monitor, manual_clock)
    response = router.handle("GET", "/api/profiles")
    assert response.status == 500
    assert payload_of(response) == {"error": "RuntimeError: boom"}
    text = response.body.decode()
    assert "Traceback" not in text
    assert 'File "' not in text


def test_a_monitoring_error_is_a_500_with_its_message(
    manual_clock: ManualClock, provider: FakeProvider
) -> None:
    monitor = Monitor(make_store(failures=StateStoreError), clock=manual_clock)
    router = build_router(provider, monitor, manual_clock)
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/positions")
    assert response.status == 500
    message = payload_of(response)["error"]
    assert message.startswith("MonitoringError: ")
    assert "cannot read the persistent state" in message
    assert "Traceback" not in response.body.decode()


def test_an_explicit_monitoring_error_is_forwarded(
    manual_clock: ManualClock, provider: FakeProvider
) -> None:
    class ExplodingProvider(FakeProvider):
        def snapshot(self) -> PlatformSnapshot:
            raise MonitoringError("state database is locked")

    router = build_router(
        ExplodingProvider(), Monitor(make_store(), clock=manual_clock), manual_clock
    )
    response = router.handle("GET", "/api/profiles")
    assert response.status == 500
    assert payload_of(response) == {"error": "MonitoringError: state database is locked"}


# ---------------------------------------------------------------------------
# JSON hygiene on the wire
# ---------------------------------------------------------------------------


def test_no_payload_contains_nan_or_infinity() -> None:
    """A non-finite float is nulled, never written as ``NaN``/``Infinity``."""
    clock = ManualClock(START)
    store = FakeStore(
        [ProfileConfig(id=PROFILE_A, symbol="BTC/USDT", timeframe="1h")],
        equity={
            PROFILE_A: [
                EquityPoint(
                    profile_id=PROFILE_A,
                    timestamp=pd.Timestamp(START),
                    equity=float("nan"),
                    cash=float("inf"),
                    position_value=float("-inf"),
                )
            ]
        },
        states={
            PROFILE_A: ProfileState(
                profile_id=PROFILE_A,
                status=ProfileStatus.RUNNING,
                mode=RunMode.PAPER,
                lag_seconds=float("nan"),
            )
        },
    )
    provider = FakeProvider(
        profiles=(make_profile_snapshot(PROFILE_A, "BTC/USDT", equity=float("inf")),),
        uptime_seconds=float("nan"),
        health_body={"status": "ok", "profiles_total": 1, "profiles_running": 1},
    )
    router = build_router(provider, Monitor(store, clock=clock), clock)

    for path in (
        "/api/health",
        "/api/profiles",
        f"/api/profiles/{PROFILE_A}",
        f"/api/profiles/{PROFILE_A}/equity",
        f"/api/profiles/{PROFILE_A}/trades",
        f"/api/profiles/{PROFILE_A}/orders",
        f"/api/profiles/{PROFILE_A}/positions",
        f"/api/profiles/{PROFILE_A}/metrics",
        "/api/kill-switch",
    ):
        response = router.handle("GET", path)
        text = response.body.decode()
        assert "NaN" not in text, path
        assert "Infinity" not in text, path
        assert "datetime.datetime" not in text, path
        assert isinstance(json.loads(text), dict)

    points = payload_of(router.handle("GET", f"/api/profiles/{PROFILE_A}/equity"))["points"]
    assert points[0]["equity"] is None
    assert points[0]["cash"] is None
    assert points[0]["position_value"] is None
    assert payload_of(router.handle("GET", "/api/health"))["uptime_seconds"] is None


def test_every_error_body_is_valid_json(router: Router) -> None:
    for path, method in (
        ("/api/nowhere", "GET"),
        ("/api/health", "POST"),
        (f"/api/profiles/{UNKNOWN_PROFILE}", "GET"),
        ("/", "GET"),
        ("/static/app.js", "GET"),
    ):
        body = router.handle(method, path).body
        assert isinstance(json.loads(body.decode()), dict)


class WeirdProfile:
    """Profile-snapshot stand-in whose payload would break a naive ``json.dumps``."""

    def __init__(self, profile_id: str) -> None:
        self.profile_id = profile_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "unknown_object": object(),
            "when": datetime(2024, 1, 1, tzinfo=UTC),
            "amount": float("nan"),
        }


class WeirdProvider(FakeProvider):
    """Provider handing the router JSON-hostile values on purpose."""

    def snapshot(self) -> PlatformSnapshot:
        return PlatformSnapshot(
            profiles=(cast("Any", WeirdProfile(PROFILE_A)),),
            generated_at=pd.Timestamp(START),
            kill_switch=False,
        )


def test_a_json_hostile_payload_is_always_encodable(
    monitor: Monitor, manual_clock: ManualClock
) -> None:
    """The wire safety net: NaN becomes null, an unknown object becomes a string."""
    router = build_router(WeirdProvider(), monitor, manual_clock)
    response = router.handle("GET", "/api/profiles")
    assert response.status == 200
    assert "NaN" not in response.body.decode()
    entry = payload_of(response)["profiles"][0]
    assert entry["profile_id"] == PROFILE_A
    assert entry["amount"] is None
    assert entry["unknown_object"].startswith("<object object at")
    assert entry["when"] == str(datetime(2024, 1, 1, tzinfo=UTC))


def test_non_numeric_values_and_odd_timestamps_never_break_a_payload(
    manual_clock: ManualClock, provider: FakeProvider
) -> None:
    store = FakeStore(
        [ProfileConfig(id=PROFILE_A, symbol="BTC/USDT", timeframe="1h")],
        equity={
            PROFILE_A: [
                EquityPoint(
                    profile_id=PROFILE_A,
                    timestamp=pd.Timestamp(START),
                    equity=None,  # type: ignore[arg-type] - deliberately hostile
                    cash="not-a-number",  # type: ignore[arg-type] - deliberately hostile
                    position_value=1.0,
                )
            ]
        },
    )
    router = build_router(provider, Monitor(store, clock=manual_clock), manual_clock)

    point = payload_of(router.handle("GET", f"/api/profiles/{PROFILE_A}/equity"))["points"][0]
    assert point["equity"] is None
    assert point["cash"] is None
    assert point["position_value"] == 1.0

    provider.kill_switch = True
    provider.kill_switch_changed_at = 42
    assert payload_of(router.handle("GET", "/api/kill-switch"))["changed_at"] == "42"


# ---------------------------------------------------------------------------
# the operator token seam
# ---------------------------------------------------------------------------


def test_operator_token_from_env_normalises_empty_values() -> None:
    assert operator_token_from_env({"TB_OPERATOR_TOKEN": "abc"}) == "abc"
    assert operator_token_from_env({"TB_OPERATOR_TOKEN": "  "}) is None
    assert operator_token_from_env({"TB_OPERATOR_TOKEN": ""}) is None
    assert operator_token_from_env({}) is None
    assert operator_token_from_env({"OTHER": "abc"}) is None


def test_operator_token_from_env_reads_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TB_OPERATOR_TOKEN", "from-env")
    assert operator_token_from_env() == "from-env"
    monkeypatch.delenv("TB_OPERATOR_TOKEN")
    assert operator_token_from_env() is None


def test_the_router_never_exposes_the_token(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    router = build_router(
        provider, monitor, manual_clock, read_only=False, operator_token="top-secret-token"
    )
    response = router.handle(
        "POST",
        "/api/kill-switch",
        body=json.dumps({"engage": True, "reason": "x"}).encode(),
        headers={"X-Operator-Token": "wrong"},
    )
    assert "top-secret-token" not in response.body.decode()
    assert "top-secret-token" not in repr(router)


# ---------------------------------------------------------------------------
# candle history: GET /api/profiles/{id}/candles
# ---------------------------------------------------------------------------

#: An operator token distinct from every other literal of this module.
TOKEN = "s3cret-operator-token"

#: Every lifecycle command, as ``(method, path, body)``.
LIFECYCLE_CALLS = [
    ("POST", f"/api/profiles/{PROFILE_A}/pause", b"{}"),
    ("POST", f"/api/profiles/{PROFILE_A}/resume", b"{}"),
    ("DELETE", f"/api/profiles/{PROFILE_A}", b""),
    (
        "POST",
        "/api/profiles",
        json.dumps(
            {
                "profile_id": "sol-paper",
                "symbol": "SOL/USDT",
                "timeframe": "15m",
                "strategy": "basic",
                "mode": "paper",
            }
        ).encode(),
    ),
]

#: The three lifecycle commands addressed to an unknown profile.
UNKNOWN_LIFECYCLE_CALLS = [
    ("POST", f"/api/profiles/{UNKNOWN_PROFILE}/pause", b"{}"),
    ("POST", f"/api/profiles/{UNKNOWN_PROFILE}/resume", b"{}"),
    ("DELETE", f"/api/profiles/{UNKNOWN_PROFILE}", b""),
]

#: A complete, valid creation body (the optional fields are left out).
CREATE_BODY: dict[str, Any] = {
    "profile_id": "sol-paper",
    "symbol": "SOL/USDT",
    "timeframe": "15m",
    "strategy": "basic",
    "mode": "paper",
}

AUTH = {"X-Operator-Token": TOKEN}


class ReadOnlyProvider:
    """A provider with **no** engine behind it -- the shape of ``realtime serve``."""

    def __init__(self, candles: Mapping[str, Sequence[Any]]) -> None:
        self._candles = dict(candles)
        self.candle_calls: list[tuple[str, int]] = []

    def snapshot(self) -> PlatformSnapshot:
        return PlatformSnapshot(
            profiles=(),
            generated_at=pd.Timestamp(START),
            kill_switch=False,
        )

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "profiles_total": 0,
            "profiles_running": 0,
            "uptime_seconds": 0.0,
        }

    def profile_snapshot(self, profile_id: str) -> ProfileSnapshot | None:
        return make_profile_snapshot(profile_id, "BTC/USDT")

    def candle_series(self, profile_id: str, limit: int) -> Sequence[Any]:
        self.candle_calls.append((profile_id, limit))
        return list(self._candles.get(profile_id, ()))[:limit]

    def kill_switch_state(self) -> Any:
        return {"engaged": False, "reason": "", "changed_at": None}

    def engage_kill_switch(self, reason: str) -> Any:  # pragma: no cover - read-only
        raise MonitoringError("this provider is read-only")

    def release_kill_switch(self) -> Any:  # pragma: no cover - read-only
        raise MonitoringError("this provider is read-only")


def test_candles_payload_has_exactly_the_documented_keys(router: Router) -> None:
    """``{candles, count}``, oldest first, one documented key set per candle."""
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/candles")
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == CANDLES_KEYS
    assert body["count"] == 4
    assert len(body["candles"]) == 4
    for candle in body["candles"]:
        assert sorted(candle) == CANDLE_KEYS
        assert candle["profile_id"] == PROFILE_A
        assert isinstance(candle["closed"], bool)
    stamps = [datetime.fromisoformat(candle["timestamp"]) for candle in body["candles"]]
    assert stamps == sorted(stamps), "the series must be oldest first"
    assert stamps[0] == START
    assert body["candles"][0]["open"] == 100.0
    assert body["candles"][0]["high"] == 105.0
    assert body["candles"][0]["low"] == 95.0
    assert body["candles"][0]["close"] == 101.0
    assert body["candles"][0]["volume"] == 10.0
    assert body["candles"][0]["closed"] is True
    assert body["candles"][3]["closed"] is False


def test_candles_of_an_unknown_profile_are_the_documented_404(router: Router) -> None:
    response = router.handle("GET", f"/api/profiles/{UNKNOWN_PROFILE}/candles")
    assert response.status == 404
    assert payload_of(response) == {"error": f"unknown profile: {UNKNOWN_PROFILE!r}"}


def test_candles_of_a_profile_without_history_are_200_and_empty(router: Router) -> None:
    response = router.handle("GET", f"/api/profiles/{PROFILE_B}/candles")
    assert response.status == 200
    assert payload_of(response) == {"candles": [], "count": 0}


def test_the_candle_limit_defaults_to_the_documented_value(
    router: Router, provider: FakeProvider
) -> None:
    assert CANDLE_ROUTE_DEFAULT_LIMIT == 500
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/candles")
    assert response.status == 200
    assert provider.candle_calls == [(PROFILE_A, CANDLE_ROUTE_DEFAULT_LIMIT)]


def test_the_candle_limit_is_honoured(router: Router, provider: FakeProvider) -> None:
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/candles", query="limit=2")
    assert response.status == 200
    body = payload_of(response)
    assert body["count"] == 2
    assert provider.candle_calls == [(PROFILE_A, 2)]
    assert [candle["open"] for candle in body["candles"]] == [100.0, 101.0]


def test_the_candle_limit_above_the_cap_is_clamped(router: Router, provider: FakeProvider) -> None:
    assert CANDLE_ROUTE_MAX_LIMIT == 1000
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/candles", query="limit=999999")
    assert response.status == 200
    assert provider.candle_calls == [(PROFILE_A, CANDLE_ROUTE_MAX_LIMIT)]


@pytest.mark.parametrize("query", MALFORMED_LIMITS)
def test_a_malformed_candle_limit_is_the_documented_400(
    router: Router, provider: FakeProvider, query: str
) -> None:
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/candles", query=query)
    assert response.status == 400
    assert payload_of(response) == {
        "error": "malformed query parameter: 'limit' must be a positive integer"
    }
    assert provider.candle_calls == [], "a refused request never reaches the provider"


def test_an_unrelated_query_parameter_is_ignored(router: Router, provider: FakeProvider) -> None:
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/candles", query="other=1&limit=3")
    assert response.status == 200
    assert provider.candle_calls == [(PROFILE_A, 3)]
    provider.candle_calls.clear()
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/candles", query="other=1")
    assert response.status == 200
    assert provider.candle_calls == [(PROFILE_A, CANDLE_ROUTE_DEFAULT_LIMIT)]


def test_the_candles_route_works_without_an_engine(manual_clock: ManualClock) -> None:
    """The route only knows the provider seam: persisted state is enough."""
    provider = ReadOnlyProvider({PROFILE_A: make_candles(PROFILE_A, 2)})
    router = build_router(provider, Monitor(make_store(), clock=manual_clock), manual_clock)
    body = payload_of(router.handle("GET", f"/api/profiles/{PROFILE_A}/candles", query="limit=1"))
    assert body["count"] == 1
    assert provider.candle_calls == [(PROFILE_A, 1)]
    assert payload_of(router.handle("GET", "/api/control")) == {
        "engine_running": False,
        "read_only": True,
        "mutable": False,
        "profiles": [],
    }
    assert payload_of(router.handle("GET", "/api/catalog")) == default_catalog_body()


def test_the_real_candle_row_travels_unchanged(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    """The row the engine persists (``realtime.store.CandleRow``) is accepted as is."""
    row = CandleRow(
        profile_id=PROFILE_A,
        timestamp=pd.Timestamp(START),
        open=100.0,
        high=110.0,
        low=90.0,
        close=105.0,
        volume=3.5,
        closed=True,
    )
    provider.candles = {PROFILE_A: [row]}
    router = build_router(provider, monitor, manual_clock)
    candle = payload_of(router.handle("GET", f"/api/profiles/{PROFILE_A}/candles"))["candles"][0]
    assert candle == {
        "profile_id": PROFILE_A,
        "timestamp": pd.Timestamp(START).isoformat(),
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": 105.0,
        "volume": 3.5,
        "closed": True,
    }


def test_non_finite_candle_values_never_reach_the_wire(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    provider.candles = {
        PROFILE_A: [
            FakeCandle(
                PROFILE_A,
                pd.Timestamp(START),
                float("nan"),
                float("inf"),
                float("-inf"),
                1.0,
                2.0,
                True,
            )
        ]
    }
    router = build_router(provider, monitor, manual_clock)
    response = router.handle("GET", f"/api/profiles/{PROFILE_A}/candles")
    text = response.body.decode()
    assert "NaN" not in text
    assert "Infinity" not in text
    candle = payload_of(response)["candles"][0]
    assert candle["open"] is None
    assert candle["high"] is None
    assert candle["low"] is None
    assert candle["close"] == 1.0


def test_head_on_the_candles_route_answers_like_get(router: Router, provider: FakeProvider) -> None:
    reference = router.handle("GET", f"/api/profiles/{PROFILE_A}/candles", query="limit=2")
    response = router.handle("HEAD", f"/api/profiles/{PROFILE_A}/candles", query="limit=2")
    assert response.status == reference.status == 200
    assert response.content_type == reference.content_type
    assert response.body == b""
    assert provider.candle_calls == [(PROFILE_A, 2), (PROFILE_A, 2)]


# ---------------------------------------------------------------------------
# the catalog: GET /api/catalog
# ---------------------------------------------------------------------------


def test_the_static_catalog_answers_without_a_provider(router: Router) -> None:
    """No catalog seam at all: the documented static fallback answers, never a 500."""
    response = router.handle("GET", "/api/catalog")
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == CATALOG_KEYS
    assert body == default_catalog_body()
    assert body["strategies"] == ["basic"]
    assert body["modes"] == ["paper", "live"]
    assert body["timeframes"], "the picker needs at least one timeframe"
    assert body["symbols"], "the picker needs at least one symbol"
    for entry in body["symbols"]:
        assert sorted(entry) == CATALOG_SYMBOL_KEYS


def test_an_injected_catalog_wins(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    catalog = FakeCatalog()
    router = build_router(provider, monitor, manual_clock, catalog=catalog)
    response = router.handle("GET", "/api/catalog")
    assert response.status == 200
    assert payload_of(response) == catalog.body
    assert catalog.calls == 1


def test_a_failing_catalog_provider_falls_back_to_the_static_catalog(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    catalog = FakeCatalog(error=RuntimeError("the venue is unreachable"))
    router = build_router(provider, monitor, manual_clock, catalog=catalog)
    response = router.handle("GET", "/api/catalog")
    assert response.status == 200
    assert payload_of(response) == default_catalog_body()
    assert catalog.calls == 1


def test_a_malformed_catalog_provider_falls_back_to_the_static_catalog(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    class MalformedCatalog(FakeCatalog):
        def catalog(self) -> Mapping[str, Any]:
            return cast("Any", ["not", "a", "mapping"])

    router = build_router(provider, monitor, manual_clock, catalog=MalformedCatalog())
    response = router.handle("GET", "/api/catalog")
    assert response.status == 200
    assert payload_of(response) == default_catalog_body()


def test_the_catalog_is_served_without_an_engine(manual_clock: ManualClock) -> None:
    provider = ReadOnlyProvider({})
    router = build_router(provider, Monitor(make_store(), clock=manual_clock), manual_clock)
    assert router.handle("GET", "/api/catalog").status == 200


# ---------------------------------------------------------------------------
# runtime control: GET /api/control
# ---------------------------------------------------------------------------


def test_the_control_payload_has_exactly_the_documented_keys(
    monitor: Monitor,
    manual_clock: ManualClock,
    provider: FakeProvider,
    controller: FakeController,
) -> None:
    router = build_router(
        provider,
        monitor,
        manual_clock,
        read_only=False,
        operator_token=TOKEN,
        controller=controller,
    )
    response = router.handle("GET", "/api/control")
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == CONTROL_KEYS
    assert body["engine_running"] is True
    assert body["read_only"] is False
    assert body["mutable"] is True
    assert [entry["profile_id"] for entry in body["profiles"]] == [PROFILE_A, PROFILE_B]
    for entry in body["profiles"]:
        assert sorted(entry) == CONTROL_PROFILE_KEYS
        assert entry["paused"] is False
        assert entry["running"] is True


@pytest.mark.parametrize(
    "read_only,token,with_controller,engine_running,mutable",
    [
        (True, None, True, True, False),
        (False, TOKEN, False, False, False),
        (False, "", True, True, False),
        (False, TOKEN, True, True, True),
    ],
)
def test_the_control_truth_table(
    monitor: Monitor,
    manual_clock: ManualClock,
    provider: FakeProvider,
    controller: FakeController,
    read_only: bool,
    token: str | None,
    with_controller: bool,
    engine_running: bool,
    mutable: bool,
) -> None:
    router = build_router(
        provider,
        monitor,
        manual_clock,
        read_only=read_only,
        operator_token=token,
        controller=controller if with_controller else None,
    )
    body = payload_of(router.handle("GET", "/api/control"))
    assert body["engine_running"] is engine_running
    assert body["read_only"] is read_only
    assert body["mutable"] is mutable
    assert bool(body["profiles"]) is with_controller


def test_control_without_a_controller_is_an_empty_list(router: Router) -> None:
    assert payload_of(router.handle("GET", "/api/control")) == {
        "engine_running": False,
        "read_only": True,
        "mutable": False,
        "profiles": [],
    }


def test_a_failing_controller_never_turns_control_into_a_500(
    monitor: Monitor,
    manual_clock: ManualClock,
    provider: FakeProvider,
) -> None:
    controller = FakeController(
        profiles=provider.profiles, control_failure=RuntimeError("the registry is gone")
    )
    router = build_router(provider, monitor, manual_clock, controller=controller)
    response = router.handle("GET", "/api/control")
    assert response.status == 200
    body = payload_of(response)
    assert body["engine_running"] is True
    assert body["profiles"] == []


# ---------------------------------------------------------------------------
# profile lifecycle: pause, resume, delete and create
# ---------------------------------------------------------------------------


@pytest.fixture
def writable(
    provider: FakeProvider,
    monitor: Monitor,
    manual_clock: ManualClock,
    controller: FakeController,
) -> Router:
    """A writable router carrying the operator token, a controller and a catalog."""
    return build_router(
        provider,
        monitor,
        manual_clock,
        read_only=False,
        operator_token=TOKEN,
        controller=controller,
        catalog=FakeCatalog(),
    )


@pytest.fixture
def read_only_with_controller(
    provider: FakeProvider,
    monitor: Monitor,
    manual_clock: ManualClock,
    controller: FakeController,
) -> Router:
    """A read-only server that *does* carry a controller (the strongest refusal)."""
    return build_router(
        provider,
        monitor,
        manual_clock,
        read_only=True,
        operator_token=TOKEN,
        controller=controller,
    )


def test_pause_returns_the_resulting_profile_state(
    writable: Router, controller: FakeController
) -> None:
    response = writable.handle("POST", f"/api/profiles/{PROFILE_A}/pause", body=b"{}", headers=AUTH)
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == ["paused", "profile"]
    assert body["paused"] is True
    assert body["profile"] == make_profile_snapshot(PROFILE_A, "BTC/USDT").to_dict()
    assert controller.pause_calls == [PROFILE_A]


def test_resume_returns_the_resulting_profile_state(
    writable: Router, controller: FakeController
) -> None:
    response = writable.handle(
        "POST", f"/api/profiles/{PROFILE_A}/resume", body=b"{}", headers=AUTH
    )
    assert response.status == 200
    body = payload_of(response)
    assert sorted(body) == ["paused", "profile"]
    assert body["paused"] is False
    assert body["profile"] == make_profile_snapshot(PROFILE_A, "BTC/USDT").to_dict()
    assert controller.resume_calls == [PROFILE_A]


def test_delete_returns_the_removed_identifier(
    writable: Router, controller: FakeController
) -> None:
    response = writable.handle("DELETE", f"/api/profiles/{PROFILE_A}", headers=AUTH)
    assert response.status == 200
    assert sorted(payload_of(response)) == ["deleted", "profile_id"]
    assert payload_of(response) == {"profile_id": PROFILE_A, "deleted": True}
    assert controller.delete_calls == [PROFILE_A]


def test_create_forwards_the_documented_body_and_answers_201(
    writable: Router, controller: FakeController
) -> None:
    payload: dict[str, Any] = {
        **CREATE_BODY,
        "profile_id": "sol-paper",
        "initial_balance": 2500,
        "params": {"ema_fast": 9, "use_stops": True, "label": "scalp"},
    }
    response = writable.handle(
        "POST", "/api/profiles", body=json.dumps(payload).encode(), headers=AUTH
    )
    assert response.status == 201
    body = payload_of(response)
    assert sorted(body) == ["profile"]
    assert body["profile"]["profile_id"] == "sol-paper"
    assert len(controller.create_calls) == 1
    forwarded = controller.create_calls[0]
    assert forwarded == {
        **CREATE_BODY,
        "profile_id": "sol-paper",
        "initial_balance": 2500.0,
        "params": {"ema_fast": 9, "use_stops": True, "label": "scalp"},
    }


@pytest.mark.parametrize("method,path,body", LIFECYCLE_CALLS)
def test_a_read_only_server_refuses_every_lifecycle_route(
    read_only_with_controller: Router,
    controller: FakeController,
    method: str,
    path: str,
    body: bytes,
) -> None:
    response = read_only_with_controller.handle(method, path, body=body, headers=AUTH)
    assert response.status == 403
    assert payload_of(response) == {"error": "mutations are disabled on this server"}
    assert controller.pause_calls == []
    assert controller.resume_calls == []
    assert controller.delete_calls == []
    assert controller.create_calls == []


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"X-Operator-Token": "wrong"},
        {"X-Operator-Token": ""},
        {"Authorization": "Bearer " + TOKEN},
    ],
)
@pytest.mark.parametrize("method,path,body", LIFECYCLE_CALLS)
def test_every_lifecycle_route_requires_the_operator_token(
    writable: Router,
    controller: FakeController,
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str] | None,
) -> None:
    response = writable.handle(method, path, body=body, headers=headers)
    assert response.status == 403
    assert payload_of(response) == {"error": "missing or invalid operator token"}
    assert TOKEN not in response.body.decode()
    assert all(TOKEN not in f"{key}{value}" for key, value in response.headers)
    assert controller.pause_calls == []
    assert controller.resume_calls == []
    assert controller.delete_calls == []
    assert controller.create_calls == []


@pytest.mark.parametrize("method,path,body", LIFECYCLE_CALLS)
def test_a_writable_server_without_a_controller_refuses_lifecycle_routes(
    monitor: Monitor,
    manual_clock: ManualClock,
    provider: FakeProvider,
    method: str,
    path: str,
    body: bytes,
) -> None:
    """No seam, no command: the same 403 a read-only server answers."""
    router = build_router(provider, monitor, manual_clock, read_only=False, operator_token=TOKEN)
    response = router.handle(method, path, body=body, headers=AUTH)
    assert response.status == 403
    assert payload_of(response) == {"error": "mutations are disabled on this server"}


def test_a_writable_server_without_a_configured_token_refuses_lifecycle_routes(
    monitor: Monitor,
    manual_clock: ManualClock,
    provider: FakeProvider,
    controller: FakeController,
) -> None:
    router = build_router(
        provider,
        monitor,
        manual_clock,
        read_only=False,
        operator_token="",
        controller=controller,
    )
    response = router.handle("POST", "/api/profiles", body=json.dumps(CREATE_BODY).encode())
    assert response.status == 403
    assert payload_of(response) == {"error": "mutations are disabled on this server"}
    assert controller.create_calls == []


@pytest.mark.parametrize("method,path,body", UNKNOWN_LIFECYCLE_CALLS)
def test_lifecycle_routes_on_an_unknown_profile_are_the_documented_404(
    writable: Router,
    controller: FakeController,
    method: str,
    path: str,
    body: bytes,
) -> None:
    response = writable.handle(method, path, body=body, headers=AUTH)
    assert response.status == 404
    assert payload_of(response) == {"error": f"unknown profile: {UNKNOWN_PROFILE!r}"}
    assert controller.pause_calls == []
    assert controller.resume_calls == []
    assert controller.delete_calls == []


@pytest.mark.parametrize(
    "error,status",
    [
        (MonitoringError("the engine is not running: profile control is unavailable"), 503),
        (ProfileError("profile already exists: 'sol-paper'"), 409),
        (ConfigError("unknown strategy: 'nope' (available: basic)"), 400),
    ],
)
@pytest.mark.parametrize("method,path,body", LIFECYCLE_CALLS)
def test_lifecycle_failures_are_mapped_onto_their_status_code(
    writable: Router,
    controller: FakeController,
    method: str,
    path: str,
    body: bytes,
    error: Exception,
    status: int,
) -> None:
    controller.failure = error
    response = writable.handle(method, path, body=body, headers=AUTH)
    assert response.status == status
    assert payload_of(response) == {"error": str(error)}
    assert "Traceback" not in response.body.decode()


def test_an_unexpected_lifecycle_failure_keeps_the_500_boundary(
    writable: Router, controller: FakeController
) -> None:
    controller.failure = RuntimeError("boom")
    response = writable.handle("POST", f"/api/profiles/{PROFILE_A}/pause", body=b"{}", headers=AUTH)
    assert response.status == 500
    assert payload_of(response) == {"error": "RuntimeError: boom"}
    assert "Traceback" not in response.body.decode()


@pytest.mark.parametrize(
    "body,message",
    [
        (b"not json at all", "malformed request body: not valid JSON"),
        (b"", "malformed request body: expected a JSON object"),
        (b"[]", "malformed request body: expected a JSON object"),
        (b"null", "malformed request body: expected a JSON object"),
        (b'"a string"', "malformed request body: expected a JSON object"),
        (
            json.dumps({**CREATE_BODY, "extra": 1}).encode(),
            "malformed request body: unexpected field 'extra'",
        ),
        (
            json.dumps({**CREATE_BODY, "id": "sol-paper"}).encode(),
            "malformed request body: unexpected field 'id'",
        ),
        (
            json.dumps(
                {key: value for key, value in CREATE_BODY.items() if key != "profile_id"}
            ).encode(),
            "malformed request body: 'profile_id' must be a string",
        ),
        (
            json.dumps({**CREATE_BODY, "symbol": 3}).encode(),
            "malformed request body: 'symbol' must be a string",
        ),
        (
            json.dumps(
                {key: value for key, value in CREATE_BODY.items() if key != "timeframe"}
            ).encode(),
            "malformed request body: 'timeframe' must be a string",
        ),
        (
            json.dumps({**CREATE_BODY, "strategy": None}).encode(),
            "malformed request body: 'strategy' must be a string",
        ),
        (
            json.dumps({**CREATE_BODY, "mode": "dry-run"}).encode(),
            "malformed request body: 'mode' must be 'paper' or 'live'",
        ),
        (
            json.dumps(
                {key: value for key, value in CREATE_BODY.items() if key != "mode"}
            ).encode(),
            "malformed request body: 'mode' must be 'paper' or 'live'",
        ),
        (
            json.dumps({**CREATE_BODY, "initial_balance": 0}).encode(),
            "malformed request body: 'initial_balance' must be a positive number",
        ),
        (
            json.dumps({**CREATE_BODY, "initial_balance": -5}).encode(),
            "malformed request body: 'initial_balance' must be a positive number",
        ),
        (
            json.dumps({**CREATE_BODY, "initial_balance": "1000"}).encode(),
            "malformed request body: 'initial_balance' must be a positive number",
        ),
        (
            b'{"profile_id": "sol-paper", "symbol": "SOL/USDT", "timeframe": "15m", '
            b'"strategy": "basic", "mode": "paper", "initial_balance": NaN}',
            "malformed request body: 'initial_balance' must be a positive number",
        ),
        (
            json.dumps({**CREATE_BODY, "params": [1, 2]}).encode(),
            "malformed request body: 'params' must be an object",
        ),
        (
            json.dumps({**CREATE_BODY, "params": {"nested": {"a": 1}}}).encode(),
            "malformed request body: 'params' must be an object",
        ),
    ],
)
def test_the_create_body_validation_messages_are_exact(
    writable: Router, controller: FakeController, body: bytes, message: str
) -> None:
    response = writable.handle("POST", "/api/profiles", body=body, headers=AUTH)
    assert response.status == 400
    assert payload_of(response) == {"error": message}
    assert controller.create_calls == [], "a rejected request never reaches the controller"


@pytest.mark.parametrize(
    "method,path,allow",
    [
        ("HEAD", f"/api/profiles/{PROFILE_A}/pause", "POST"),
        ("GET", f"/api/profiles/{PROFILE_A}/pause", "POST"),
        ("DELETE", f"/api/profiles/{PROFILE_A}/pause", "POST"),
        ("HEAD", f"/api/profiles/{PROFILE_A}/resume", "POST"),
        ("PUT", f"/api/profiles/{PROFILE_A}/resume", "POST"),
        ("POST", "/api/catalog", "GET, HEAD"),
        ("POST", "/api/control", "GET, HEAD"),
        ("POST", f"/api/profiles/{PROFILE_A}/candles", "GET, HEAD"),
        ("POST", f"/api/profiles/{PROFILE_A}", "GET, HEAD, DELETE"),
        ("PUT", f"/api/profiles/{PROFILE_A}", "GET, HEAD, DELETE"),
        ("DELETE", f"/api/profiles/{PROFILE_A}/equity", "GET, HEAD"),
    ],
)
def test_the_wrong_method_on_a_new_route_is_405_with_its_allow_header(
    router: Router, method: str, path: str, allow: str
) -> None:
    response = router.handle(method, path, body=b"{}")
    assert response.status == 405
    assert payload_of(response) == {"error": "method not allowed"}
    assert header_value(response, "Allow") == allow


@pytest.mark.parametrize(
    "path",
    [
        "/api/catalog/",
        "/api/catalog/extra",
        "/api/control/extra",
        f"/api/profiles/{PROFILE_A}/pause/extra",
        f"/api/profiles/{PROFILE_A}/CANDLES",
        f"/api/profiles/{PROFILE_A}/candles/",
    ],
)
def test_the_new_unknown_paths_are_a_documented_404(router: Router, path: str) -> None:
    response = router.handle("GET", path)
    assert response.status == 404
    assert payload_of(response) == {"error": f"not found: {path}"}
    assert header_value(response, "Allow") is None


def test_allowed_methods_covers_every_new_path(router: Router) -> None:
    assert router.allowed_methods("/api/catalog") == ("GET", "HEAD")
    assert router.allowed_methods("/api/control") == ("GET", "HEAD")
    assert router.allowed_methods(f"/api/profiles/{PROFILE_A}/candles") == ("GET", "HEAD")
    assert router.allowed_methods("/api/profiles") == ("GET", "HEAD", "POST")
    assert router.allowed_methods(f"/api/profiles/{PROFILE_A}") == ("GET", "HEAD", "DELETE")
    assert router.allowed_methods(f"/api/profiles/{PROFILE_A}/pause") == ("POST",)
    assert router.allowed_methods(f"/api/profiles/{PROFILE_A}/resume") == ("POST",)
    assert router.allowed_methods("/api/catalog/extra") == ()
    assert router.allowed_methods(f"/api/profiles/{PROFILE_A}/pause/") == ()


def test_the_operator_token_never_travels_back(writable: Router) -> None:
    for method, path, body in LIFECYCLE_CALLS:
        for headers in ({"X-Operator-Token": "wrong"}, AUTH):
            response = writable.handle(method, path, body=body, headers=headers)
            assert TOKEN not in response.body.decode()
            assert all(TOKEN not in f"{key}{value}" for key, value in response.headers)
    assert TOKEN not in repr(writable)


# ---------------------------------------------------------------------------
# the three seams of the web layer
# ---------------------------------------------------------------------------


def test_the_new_seams_are_protocols_with_the_documented_members() -> None:
    assert getattr(CatalogProvider, "_is_protocol", False) is True
    assert getattr(ProfileController, "_is_protocol", False) is True
    assert hasattr(CatalogProvider, "catalog")
    for member in (
        "pause_profile",
        "resume_profile",
        "delete_profile",
        "create_profile",
        "control_state",
    ):
        assert hasattr(ProfileController, member), member
    assert hasattr(SnapshotProvider, "candle_series")


def test_the_package_exports_the_new_seams() -> None:
    import trading_platform.web as web

    assert web.CatalogProvider is CatalogProvider
    assert web.ProfileController is ProfileController
    assert "CatalogProvider" in web.__all__
    assert "ProfileController" in web.__all__


@pytest.mark.parametrize("path", ["/api/catalog", "/api/control", "/api/profiles"])
def test_head_answers_the_new_platform_routes_like_get(router: Router, path: str) -> None:
    reference = router.handle("GET", path)
    response = router.handle("HEAD", path)
    assert response.status == reference.status == 200
    assert response.content_type == reference.content_type
    assert response.headers == reference.headers
    assert response.body == b""


def test_a_controller_returning_a_plain_mapping_is_rendered_as_is(
    monitor: Monitor, manual_clock: ManualClock, provider: FakeProvider
) -> None:
    """The lifecycle payload is built from whatever the controller returned."""

    class MappingController(FakeController):
        def pause_profile(self, profile_id: str) -> Any:
            return {"profile_id": profile_id, "status": "running"}

    class ObjectController(FakeController):
        def pause_profile(self, profile_id: str) -> Any:
            return object()

    profiles = (make_profile_snapshot(PROFILE_A, "BTC/USDT"),)
    routers = [
        build_router(
            provider,
            monitor,
            manual_clock,
            read_only=False,
            operator_token=TOKEN,
            controller=MappingController(profiles=profiles),
        ),
        build_router(
            provider,
            monitor,
            manual_clock,
            read_only=False,
            operator_token=TOKEN,
            controller=ObjectController(profiles=profiles),
        ),
    ]
    mapping = payload_of(
        routers[0].handle("POST", f"/api/profiles/{PROFILE_A}/pause", headers=AUTH)
    )["profile"]
    assert mapping == {"profile_id": PROFILE_A, "status": "running"}
    rendered = payload_of(
        routers[1].handle("POST", f"/api/profiles/{PROFILE_A}/pause", headers=AUTH)
    )["profile"]
    assert isinstance(rendered, str)
    assert rendered.startswith("<object object at")
