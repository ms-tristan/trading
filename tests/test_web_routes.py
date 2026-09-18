"""Tests of the pure monitoring router (work package wp9).

Everything here is offline, deterministic and socket-free: the router is a pure
function, so the whole JSON contract of section 4 of the delivery brief is
exercised without a server.  Time comes from an injected ``ManualClock``, the
data comes from a **local** ``FakeStore``/``FakeProvider`` (never from SQLite,
never from the orchestrator) and the metrics block is compared against a direct
call of ``Monitor.metrics``, so a payload invented by the router instead of read
from the read model would fail these tests.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest

from trading_backtest.config.models import MonitoringConfig, ProfileConfig
from trading_backtest.core.errors import MonitoringError, StateStoreError
from trading_backtest.core.models import Direction, ExitReason, TradeRecord
from trading_backtest.realtime.clock import ManualClock
from trading_backtest.realtime.models import (
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
from trading_backtest.realtime.monitor import Monitor
from trading_backtest.web.routes import (
    HttpResponse,
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
STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "trading_backtest" / "web" / "static"

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


def payload_of(response: HttpResponse) -> dict[str, Any]:
    """Decode the JSON body of ``response`` (every non-asset body is JSON)."""
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
        )
    )


def build_router(
    provider: SnapshotProvider,
    monitor: Monitor,
    clock: ManualClock,
    *,
    read_only: bool = True,
    operator_token: str | None = None,
    version: str = "0.1.0",
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
    )


@pytest.fixture
def router(provider: FakeProvider, monitor: Monitor, manual_clock: ManualClock) -> Router:
    """Read-only router (the default, and the mode of ``realtime serve``)."""
    return build_router(provider, monitor, manual_clock)


# ---------------------------------------------------------------------------
# dashboard document and static assets
# ---------------------------------------------------------------------------


def test_index_returns_the_verbatim_html_document(router: Router) -> None:
    response = router.handle("GET", "/")
    assert response.status == 200
    assert response.content_type == "text/html; charset=utf-8"
    assert response.body == (STATIC_DIR / "index.html").read_bytes()


def test_static_assets_are_served_verbatim_with_the_right_content_type(router: Router) -> None:
    javascript = router.handle("GET", "/static/app.js")
    assert javascript.status == 200
    assert javascript.content_type == "text/javascript; charset=utf-8"
    assert javascript.body == (STATIC_DIR / "app.js").read_bytes()

    stylesheet = router.handle("GET", "/static/styles.css")
    assert stylesheet.status == 200
    assert stylesheet.content_type == "text/css; charset=utf-8"
    assert stylesheet.body == (STATIC_DIR / "styles.css").read_bytes()


@pytest.mark.parametrize(
    "path",
    [
        "/static/../routes.py",
        "/static/%2e%2e/routes.py",
        "/static/..%2Froutes.py",
        "/static/unknown.js",
        "/static/",
        "/static/app.js/extra",
        "/static/index.html",
        "/static/server.py",
    ],
)
def test_static_path_traversal_and_unknown_assets_are_refused(router: Router, path: str) -> None:
    response = router.handle("GET", path)
    assert response.status == 404
    assert payload_of(response) == {"error": f"not found: {path}"}


def test_dashboard_assets_never_reach_the_network() -> None:
    """The three files must render fully offline (mandatory, D2)."""
    for name in ("index.html", "app.js", "styles.css"):
        text = (STATIC_DIR / name).read_text(encoding="utf-8").lower()
        for forbidden in ("http://", "https://", "cdn", "@import", "fonts.googleapis"):
            assert forbidden not in text, f"{name} references {forbidden}"

    document = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'data-refresh-seconds="2"' in document
    assert "/static/styles.css" in document
    assert "/static/app.js" in document

    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "2000" in script
    assert "mutations disabled" in script
    for forbidden_call in (".innerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden_call not in script, f"app.js calls {forbidden_call}"
    assert "textContent" in script
    assert "sessionStorage" in script
    assert "X-Operator-Token" in script
    assert "devicePixelRatio" in script


class _DocumentAudit(HTMLParser):
    """Collect the identifiers, the asset references and any tag imbalance."""

    _VOID = frozenset(
        {"meta", "link", "br", "hr", "img", "input", "source", "area", "base", "col", "wbr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.assets: list[str] = []
        self.problems: list[str] = []
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        if tag == "link" and attributes.get("href"):
            self.assets.append(attributes["href"])
        if tag == "script" and attributes.get("src"):
            self.assets.append(attributes["src"])
        if tag not in self._VOID:
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._VOID:
            return
        if not self._open or self._open[-1] != tag:
            self.problems.append(f"unexpected closing tag </{tag}>")
            return
        self._open.pop()


def test_the_dashboard_document_is_well_formed_and_wires_the_whole_page() -> None:
    """A broken page would render nothing: the structure is asserted, not assumed."""
    audit = _DocumentAudit()
    audit.feed((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
    assert audit.problems == []
    assert audit._open == []
    assert audit.assets == ["/static/styles.css", "/static/app.js"]
    assert {
        "profiles",
        "banner",
        "platform-status",
        "platform-version",
        "profiles-running",
        "profiles-total",
        "checked-at",
        "operator-token",
        "kill-engage",
        "kill-release",
        "kill-message",
    } <= audit.ids


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
    assert router.handle("GET", "/").status == 200
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


@pytest.mark.parametrize("path", ["/api/health", "/api/profiles", f"/api/profiles/{PROFILE_A}"])
def test_post_on_a_get_route_is_405_with_an_allow_header(router: Router, path: str) -> None:
    response = router.handle("POST", path, body=b"{}")
    assert response.status == 405
    assert payload_of(response) == {"error": "method not allowed"}
    assert header_value(response, "Allow") == "GET, HEAD"


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_unsupported_methods_are_405(router: Router, method: str) -> None:
    response = router.handle(method, "/api/profiles")
    assert response.status == 405
    assert header_value(response, "Allow") == "GET, HEAD"


def test_unsupported_method_on_an_unknown_path_is_404(router: Router) -> None:
    response = router.handle("PUT", "/api/nowhere")
    assert response.status == 404
    assert header_value(response, "Allow") is None


def test_allowed_methods_advertises_the_documented_tuples(router: Router) -> None:
    assert router.allowed_methods("/") == ("GET", "HEAD")
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
