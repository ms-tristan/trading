"""Tests of the monitoring HTTP transport (work package wp9).

Every test here binds ``port=0`` and reads the chosen port back from
``server.server_address[1]``: no fixed port is ever used, nothing leaves
``127.0.0.1``, every client call carries an explicit timeout and every server is
stopped (``shutdown()`` + ``server_close()`` + bounded ``join``) inside the test
that started it, so no test can hang or leak a thread.

The data sources are the same local fakes as the router suite: a ``FakeStore``
for the read model and a ``FakeProvider`` behind the ``SnapshotProvider``
protocol.  The orchestrator is never imported.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import socket
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import MonitoringConfig, ProfileConfig
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import (
    EngineCounters,
    EquityPoint,
    PlatformSnapshot,
    ProfileHealth,
    ProfileSnapshot,
    ProfileState,
    ProfileStatus,
    RunMode,
)
from trading_platform.realtime.monitor import Monitor
from trading_platform.realtime.store import CandleRow, SqliteStateStore
from trading_platform.web.server import (
    CatalogProvider,
    MonitoringHandler,
    MonitoringServer,
    ProfileController,
    SnapshotProvider,
    create_server,
    operator_token_from_env,
    serve,
    start_in_thread,
)

PROFILE_A = "btc-paper"
UNKNOWN_PROFILE = "ghost"
START = datetime(2024, 1, 1, tzinfo=UTC)
CLIENT_TIMEOUT = 5.0
SHUTDOWN_TIMEOUT = 5.0
TOKEN = "s3cret-operator-token"


# ---------------------------------------------------------------------------
# local fakes (never SQLite, never the orchestrator)
# ---------------------------------------------------------------------------


class FakeStore:
    """Minimal in-memory ``StateStore`` covering what the read model reads."""

    def __init__(self, specs: Sequence[ProfileConfig] = ()) -> None:
        self._specs = list(specs)

    def load_profiles(self) -> list[ProfileConfig]:
        return list(self._specs)

    def equity_curve(self, profile_id: str) -> list[EquityPoint]:
        if profile_id != PROFILE_A:
            return []
        return [
            EquityPoint(
                profile_id=profile_id,
                timestamp=pd.Timestamp(START + timedelta(hours=step)),
                equity=10000.0 + 10.0 * step,
                cash=9000.0,
                position_value=1000.0 + 10.0 * step,
            )
            for step in range(2)
        ]

    def list_trades(self, profile_id: str) -> list[Any]:
        return []

    def list_positions(self, profile_id: str) -> list[Any]:
        return []

    def list_orders(self, profile_id: str, *, limit: int = 100) -> list[Any]:
        return []

    def profile_state(self, profile_id: str) -> ProfileState:
        return ProfileState(
            profile_id=profile_id,
            status=ProfileStatus.RUNNING,
            mode=RunMode.PAPER,
            last_candle_at=pd.Timestamp(START + timedelta(hours=2)),
            lag_seconds=3.0,
        )


def make_candles(profile_id: str, count: int = 3) -> list[CandleRow]:
    """Build ``count`` deterministic persisted candles, oldest first."""
    return [
        CandleRow(
            profile_id=profile_id,
            timestamp=pd.Timestamp(START + timedelta(hours=step)),
            open=100.0 + step,
            high=110.0 + step,
            low=90.0 + step,
            close=105.0 + step,
            volume=2.5 + step,
            closed=step < count - 1,
        )
        for step in range(count)
    ]


def make_profile_snapshot(profile_id: str) -> ProfileSnapshot:
    """Build a deterministic profile snapshot."""
    return ProfileSnapshot(
        profile_id=profile_id,
        symbol="BTC/USDT",
        timeframe="1h",
        strategy="basic",
        mode=RunMode.PAPER,
        status=ProfileStatus.RUNNING,
        initial_balance=10000.0,
        equity=10020.0,
        cash=9000.0,
        position_value=1020.0,
        total_return=0.002,
        n_trades=0,
        open_positions=0,
        health=ProfileHealth(
            profile_id=profile_id,
            status=ProfileStatus.RUNNING,
            last_candle_at=pd.Timestamp(START + timedelta(hours=2)),
            lag_seconds=3.0,
            counters=EngineCounters(candles_processed=5),
        ),
        started_at=pd.Timestamp(START),
        updated_at=pd.Timestamp(START + timedelta(hours=2)),
    )


@dataclass
class FakeProvider:
    """Local implementation of :class:`SnapshotProvider`."""

    profiles: tuple[ProfileSnapshot, ...] = (make_profile_snapshot(PROFILE_A),)
    kill_switch: bool = False
    kill_switch_reason: str = ""
    health_body: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "ok",
            "profiles_total": 1,
            "profiles_running": 1,
            "uptime_seconds": 9.5,
        }
    )

    def snapshot(self) -> PlatformSnapshot:
        return PlatformSnapshot(
            profiles=self.profiles,
            generated_at=pd.Timestamp(START + timedelta(seconds=5)),
            kill_switch=self.kill_switch,
            uptime_seconds=9.5,
        )

    def health(self) -> dict[str, Any]:
        return dict(self.health_body)

    def profile_snapshot(self, profile_id: str) -> ProfileSnapshot | None:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        return None

    def candle_series(self, profile_id: str, limit: int) -> list[CandleRow]:
        if profile_id != PROFILE_A:
            return []
        return make_candles(profile_id)[:limit]

    def engage_kill_switch(self, reason: str) -> Any:
        self.kill_switch = True
        self.kill_switch_reason = reason
        return self.kill_switch_state()

    def release_kill_switch(self) -> Any:
        self.kill_switch = False
        self.kill_switch_reason = ""
        return self.kill_switch_state()

    def kill_switch_state(self) -> Any:
        return {
            "engaged": self.kill_switch,
            "reason": self.kill_switch_reason,
            "changed_at": pd.Timestamp(START) if self.kill_switch else None,
        }


# ---------------------------------------------------------------------------
# fixtures and HTTP helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def manual_clock() -> ManualClock:
    """Deterministic clock anchored on a fixed instant."""
    return ManualClock(START)


@pytest.fixture
def monitor(manual_clock: ManualClock) -> Monitor:
    """Read model over the local store."""
    return Monitor(
        FakeStore([ProfileConfig(id=PROFILE_A, symbol="BTC/USDT", timeframe="1h")]),
        clock=manual_clock,
    )


@pytest.fixture
def provider() -> FakeProvider:
    """Snapshot provider holding one deterministic profile."""
    return FakeProvider()


def build_server(
    provider: SnapshotProvider,
    monitor: Monitor,
    *,
    read_only: bool = True,
    operator_token: str | None = None,
    max_request_bytes: int = 65536,
    controller: ProfileController | None = None,
    catalog: CatalogProvider | None = None,
) -> MonitoringServer:
    """Build a bound server on an ephemeral loopback port."""
    return create_server(
        provider,
        monitor=monitor,
        config=MonitoringConfig(host="127.0.0.1", port=0, max_request_bytes=max_request_bytes),
        read_only=read_only,
        operator_token=operator_token,
        version="0.1.0",
        controller=controller,
        catalog=catalog,
    )


@contextlib.contextmanager
def running(server: MonitoringServer) -> Iterator[MonitoringServer]:
    """Serve ``server`` in a daemon thread and stop it deterministically."""
    thread = start_in_thread(server)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=SHUTDOWN_TIMEOUT)
        assert not thread.is_alive(), "the serving thread did not stop"


def http_request(
    server: MonitoringServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Perform one loopback request with an explicit timeout."""
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=CLIENT_TIMEOUT
    )
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


def decode(payload: bytes) -> dict[str, Any]:
    """Decode a JSON response body."""
    decoded = json.loads(payload.decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


@pytest.fixture
def server(provider: FakeProvider, monitor: Monitor) -> Iterator[MonitoringServer]:
    """A read-only server running on an ephemeral port."""
    built = build_server(provider, monitor)
    with running(built) as started:
        yield started


# ---------------------------------------------------------------------------
# binding, serving and real round trips
# ---------------------------------------------------------------------------


def test_create_server_binds_an_ephemeral_loopback_port(
    provider: FakeProvider, monitor: Monitor
) -> None:
    built = build_server(provider, monitor)
    try:
        port = built.server_address[1]
        assert isinstance(port, int)
        assert port > 0
        assert built.port == port
        assert built.server_address[0] == "127.0.0.1"
        assert built.router.read_only is True
        assert built.daemon_threads is True
        assert MonitoringHandler.server_version == "trading-platform-monitoring/0.1"
    finally:
        built.server_close()


def test_host_and_port_default_to_the_monitoring_config(
    provider: FakeProvider, monitor: Monitor
) -> None:
    built = create_server(
        provider,
        monitor=monitor,
        config=MonitoringConfig(host="127.0.0.1", port=0),
    )
    try:
        assert built.server_address[1] > 0
    finally:
        built.server_close()


def test_real_round_trip_on_health_and_profiles(server: MonitoringServer) -> None:
    status, headers, payload = http_request(server, "GET", "/api/health")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    body = decode(payload)
    assert sorted(body) == [
        "checked_at",
        "kill_switch",
        "profiles_running",
        "profiles_total",
        "status",
        "uptime_seconds",
        "version",
        "wallet",
    ]
    # The shared platform wallet is **additive**: the key is always present, and a
    # provider that reports no wallet answers an explicit ``null`` instead of
    # omitting the key, so a consumer never has to guess whether it is missing.
    assert body["wallet"] is None
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"
    # ``create_server`` owns the clock (no clock argument in the frozen
    # signature), so the timestamp is asserted by shape, not by value.
    assert body["checked_at"].endswith("+00:00")
    assert datetime.fromisoformat(body["checked_at"]).tzinfo is not None

    status, _, payload = http_request(server, "GET", "/api/profiles")
    assert status == 200
    profiles = decode(payload)
    assert sorted(profiles) == ["generated_at", "profiles", "wallet"]
    assert profiles["wallet"] is None
    assert [item["profile_id"] for item in profiles["profiles"]] == [PROFILE_A]


def test_head_returns_the_same_headers_with_an_empty_body(server: MonitoringServer) -> None:
    get_status, get_headers, get_body = http_request(server, "GET", "/api/health")
    head_status, head_headers, head_body = http_request(server, "HEAD", "/api/health")
    assert head_status == get_status == 200
    assert head_body == b""
    assert get_body != b""
    assert head_headers["Content-Type"] == get_headers["Content-Type"]
    assert head_headers["Server"] == get_headers["Server"]
    assert "Allow" not in get_headers
    assert "Allow" not in head_headers


def test_the_removed_html_surface_is_a_documented_json_404(
    server: MonitoringServer,
) -> None:
    """Layer 7 serves JSON only: ``/`` and every ``/static/...`` path are unknown.

    The transport adds nothing of its own here: the response is exactly the
    documented router 404, with a JSON content type and the requested path echoed
    verbatim -- no HTML document, no asset, and no stdlib error page.
    """
    for path in (
        "/",
        "/static/app.js",
        "/static/styles.css",
        "/static/index.html",
        "/static/../routes.py",
        "/static/%2e%2e/routes.py",
    ):
        status, headers, payload = http_request(server, "GET", path)
        assert status == 404, path
        assert headers["Content-Type"] == "application/json; charset=utf-8", path
        assert decode(payload) == {"error": f"not found: {path}"}, path


def test_unknown_route_and_wrong_method_over_http(server: MonitoringServer) -> None:
    status, _, payload = http_request(server, "GET", "/api/nowhere")
    assert status == 404
    assert decode(payload) == {"error": "not found: /api/nowhere"}

    status, headers, payload = http_request(server, "POST", "/api/health", body=b"{}")
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert decode(payload) == {"error": "method not allowed"}


def test_every_unsupported_verb_answers_the_documented_json_405(
    server: MonitoringServer,
) -> None:
    """A wrong method is always the documented ``405``, never the stdlib's HTML 501.

    The verbs the handler does not route explicitly (``PUT``/``PATCH``/``DELETE``/
    ``OPTIONS``) are the ones ``BaseHTTPRequestHandler`` would otherwise refuse with
    its own ``501 Unsupported method`` HTML page.  The API contract is JSON
    everywhere and ``405`` for a wrong method, so all of them go through the router.
    """
    for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
        status, headers, payload = http_request(server, method, "/api/health")
        assert status == 405, method
        assert headers["Allow"] == "GET, HEAD", method
        assert decode(payload) == {"error": "method not allowed"}, method

        # an unknown path stays a 404 for every verb as well
        status, _, payload = http_request(server, method, "/api/nowhere")
        assert status == 404, method
        assert decode(payload) == {"error": "not found: /api/nowhere"}, method


def test_a_per_profile_detail_route_round_trips(server: MonitoringServer) -> None:
    status, _, payload = http_request(server, "GET", f"/api/profiles/{PROFILE_A}/equity")
    assert status == 200
    body = decode(payload)
    assert sorted(body) == ["points"]
    assert sorted(body["points"][0]) == ["cash", "equity", "position_value", "timestamp"]

    status, _, payload = http_request(server, "GET", "/api/profiles/ghost")
    assert status == 404
    assert decode(payload) == {"error": "unknown profile: 'ghost'"}


# ---------------------------------------------------------------------------
# read-only mode (mandatory) and mutation
# ---------------------------------------------------------------------------


def test_a_read_only_server_refuses_the_kill_switch(server: MonitoringServer) -> None:
    status, _, payload = http_request(
        server,
        "POST",
        "/api/kill-switch",
        body=json.dumps({"engage": True, "reason": "stop"}).encode(),
        headers={"Content-Type": "application/json", "X-Operator-Token": "whatever"},
    )
    assert status == 403
    assert decode(payload) == {"error": "mutations are disabled on this server"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/api/profiles",
        f"/api/profiles/{PROFILE_A}",
        f"/api/profiles/{PROFILE_A}/equity",
        f"/api/profiles/{PROFILE_A}/trades",
        f"/api/profiles/{PROFILE_A}/orders",
        f"/api/profiles/{PROFILE_A}/positions",
        f"/api/profiles/{PROFILE_A}/metrics",
        "/api/kill-switch",
    ],
)
def test_a_read_only_server_answers_every_get(server: MonitoringServer, path: str) -> None:
    status, _, _ = http_request(server, "GET", path)
    assert status == 200


@pytest.mark.parametrize("path", ["/", "/static/styles.css"])
def test_a_read_only_server_answers_the_removed_surface_with_a_404(
    server: MonitoringServer, path: str
) -> None:
    """Read-only mode is beside the point: these paths are simply not routes."""
    status, headers, payload = http_request(server, "GET", path)
    assert status == 404
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert decode(payload) == {"error": f"not found: {path}"}


def test_a_mutating_server_engages_the_kill_switch(
    provider: FakeProvider, monitor: Monitor
) -> None:
    built = build_server(provider, monitor, read_only=False, operator_token="s3cret")
    with running(built) as server:
        status, _, payload = http_request(
            server,
            "POST",
            "/api/kill-switch",
            body=json.dumps({"engage": True, "reason": "operator"}).encode(),
            headers={"Content-Type": "application/json", "X-Operator-Token": "s3cret"},
        )
        assert status == 200
        body = decode(payload)
        assert sorted(body) == ["changed_at", "kill_switch", "reason"]
        assert body["kill_switch"] is True
        assert body["reason"] == "operator"

        status, _, payload = http_request(server, "GET", "/api/health")
        assert status == 200
        assert decode(payload)["status"] == "degraded"


def test_a_mutating_server_refuses_a_wrong_token(provider: FakeProvider, monitor: Monitor) -> None:
    built = build_server(provider, monitor, read_only=False, operator_token="s3cret")
    with running(built) as server:
        status, _, payload = http_request(
            server,
            "POST",
            "/api/kill-switch",
            body=b'{"engage": true, "reason": "x"}',
            headers={"X-Operator-Token": "nope"},
        )
        assert status == 403
        assert "s3cret" not in payload.decode()


def test_the_operator_token_is_resolved_from_the_environment(
    provider: FakeProvider, monitor: Monitor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TB_OPERATOR_TOKEN", "env-token")
    assert operator_token_from_env() == "env-token"
    built = build_server(provider, monitor, read_only=False)
    with running(built) as server:
        status, _, _ = http_request(
            server,
            "POST",
            "/api/kill-switch",
            body=b'{"engage": false, "reason": "resume"}',
            headers={"X-Operator-Token": "env-token"},
        )
        assert status == 200


# ---------------------------------------------------------------------------
# robustness of the transport
# ---------------------------------------------------------------------------


def test_a_malformed_request_line_does_not_kill_the_server(server: MonitoringServer) -> None:
    with socket.create_connection(
        ("127.0.0.1", server.server_address[1]), timeout=CLIENT_TIMEOUT
    ) as client:
        client.sendall(b"THIS IS NOT A REQUEST LINE\r\n\r\n")
        client.settimeout(CLIENT_TIMEOUT)
        with contextlib.suppress(OSError):
            answer = client.recv(4096)
            assert answer == b"" or b"400" in answer

    status, _, payload = http_request(server, "GET", "/api/health")
    assert status == 200
    assert decode(payload)["status"] == "ok"


def test_an_oversized_body_does_not_kill_the_server(
    provider: FakeProvider, monitor: Monitor
) -> None:
    built = build_server(
        provider, monitor, read_only=False, operator_token="tok", max_request_bytes=64
    )
    with running(built) as server:
        big = b"x" * 512
        status, _, payload = http_request(
            server,
            "POST",
            "/api/kill-switch",
            body=big,
            headers={"Content-Length": str(len(big)), "X-Operator-Token": "tok"},
        )
        assert status == 413
        assert "too large" in decode(payload)["error"]

        status, _, payload = http_request(server, "GET", "/api/health")
        assert status == 200
        assert decode(payload)["status"] == "ok"


def test_an_invalid_content_length_is_a_400(server: MonitoringServer) -> None:
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=CLIENT_TIMEOUT
    )
    try:
        connection.putrequest("POST", "/api/kill-switch")
        connection.putheader("Content-Length", "not-a-number")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        assert "invalid Content-Length" in response.read().decode()
    finally:
        connection.close()

    status, _, _ = http_request(server, "GET", "/api/health")
    assert status == 200


def test_a_negative_content_length_is_a_400(server: MonitoringServer) -> None:
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=CLIENT_TIMEOUT
    )
    try:
        connection.putrequest("POST", "/api/kill-switch")
        connection.putheader("Content-Length", "-5")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        assert "invalid Content-Length" in response.read().decode()
    finally:
        connection.close()

    status, _, _ = http_request(server, "GET", "/api/health")
    assert status == 200


def test_an_empty_body_on_the_mutating_route_is_a_400(
    provider: FakeProvider, monitor: Monitor
) -> None:
    built = build_server(provider, monitor, read_only=False, operator_token="tok")
    with running(built) as server:
        status, _, payload = http_request(
            server,
            "POST",
            "/api/kill-switch",
            body=b"",
            headers={"X-Operator-Token": "tok"},
        )
        assert status == 400
        assert "malformed request body" in decode(payload)["error"]
        assert provider.kill_switch is False


def test_log_message_never_writes_to_stderr(
    server: MonitoringServer, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="trading_platform.web")
    status, _, _ = http_request(server, "GET", "/api/health")
    assert status == 200
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
    assert any("GET /api/health" in record.getMessage() for record in caplog.records)
    assert all(record.name == "trading_platform.web" for record in caplog.records)


def test_a_transport_error_is_logged_and_not_printed_on_stderr(
    server: MonitoringServer, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="trading_platform.web")
    with socket.create_connection(
        ("127.0.0.1", server.server_address[1]), timeout=CLIENT_TIMEOUT
    ) as client:
        client.sendall(b"GARBAGE\r\n\r\n")
        client.settimeout(CLIENT_TIMEOUT)
        with contextlib.suppress(OSError):
            client.recv(4096)
    assert capsys.readouterr().err == ""
    assert any(record.name == "trading_platform.web" for record in caplog.records)


def test_shutdown_is_clean_and_bounded(provider: FakeProvider, monitor: Monitor) -> None:
    built = build_server(provider, monitor)
    thread = start_in_thread(built)
    assert thread.daemon is True
    try:
        status, _, _ = http_request(built, "GET", "/api/health")
        assert status == 200
        started = time.monotonic()
        built.shutdown()
        built.server_close()
        thread.join(timeout=SHUTDOWN_TIMEOUT)
        assert not thread.is_alive()
        assert time.monotonic() - started < SHUTDOWN_TIMEOUT
    finally:
        if thread.is_alive():  # pragma: no cover - only on a hung server
            built.server_close()


def test_serve_can_handle_exactly_one_request(provider: FakeProvider, monitor: Monitor) -> None:
    built = build_server(provider, monitor)
    worker = threading.Thread(target=serve, args=(built,), kwargs={"block": False}, daemon=True)
    worker.start()
    try:
        status, _, payload = http_request(built, "GET", "/api/profiles")
        assert status == 200
        assert sorted(decode(payload)) == ["generated_at", "profiles", "wallet"]
        worker.join(timeout=SHUTDOWN_TIMEOUT)
        assert not worker.is_alive()
    finally:
        # ``shutdown()`` would block for ever here: it waits for a
        # ``serve_forever()`` loop that was never started.
        built.server_close()
        worker.join(timeout=SHUTDOWN_TIMEOUT)


def test_serve_blocking_runs_until_shutdown(provider: FakeProvider, monitor: Monitor) -> None:
    built = build_server(provider, monitor)
    worker = threading.Thread(target=serve, args=(built,), daemon=True)
    worker.start()
    try:
        status, _, _ = http_request(built, "GET", "/api/health")
        assert status == 200
    finally:
        built.shutdown()
        built.server_close()
        worker.join(timeout=SHUTDOWN_TIMEOUT)
        assert not worker.is_alive()


# ---------------------------------------------------------------------------
# a polling browser must never exhaust the server
# ---------------------------------------------------------------------------


def test_a_polling_browser_never_exhausts_the_store_connections(tmp_path: Path) -> None:
    """The dashboard's own polling must not leak a SQLite connection per request.

    Regression test for the deployed dashboard going dark.  The browser client
    polls five documented routes every two seconds
    (``monitoring.refresh_seconds``), three of which are read from the SQLite
    store (``/equity``, ``/positions``, ``/trades``).  The store keeps one
    connection per *caller thread* and retained every one of them, while
    ``ThreadingHTTPServer`` runs a thread per request -- so a browser left open
    leaked descriptors until the container hit ``OSError: Too many open files``
    and answered ``500`` to everything, the dashboard's own polls included: the
    page stopped updating.

    The client threads run *concurrently* on purpose: threads alive at the same time
    necessarily have distinct ids, so the leak is visible on this host too (a
    sequential loop is masked by the OS reusing one id).  The store is real here --
    the local fake has no descriptors to leak -- and the routes are the ones the
    dashboard actually polls.
    """
    clock = ManualClock(START)
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    store.initialize()
    polled = (
        "/api/health",
        "/api/profiles",
        f"/api/profiles/{PROFILE_A}/equity",
        f"/api/profiles/{PROFILE_A}/positions",
        f"/api/profiles/{PROFILE_A}/trades",
    )
    rounds = 4
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def poll(offset: int) -> None:
        for index in range(rounds):
            path = polled[(offset + index) % len(polled)]
            status, _headers, _payload = http_request(started, "GET", path)
            with statuses_lock:
                statuses.append(status)

    try:
        built = build_server(FakeProvider(), Monitor(store, clock=clock), read_only=True)
        with running(built) as started:
            workers = [threading.Thread(target=poll, args=(index,)) for index in range(15)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)
                assert worker.is_alive() is False

            assert statuses == [200] * (len(workers) * rounds), sorted(set(statuses))
            # one final store-backed poll: it is the moment the finished request
            # threads are reaped
            status, _headers, _payload = http_request(
                started, "GET", f"/api/profiles/{PROFILE_A}/equity"
            )
            assert status == 200
            assert store.connection_count <= 2, (
                f"{store.connection_count} connections retained after "
                f"{len(workers) * rounds} concurrent polls"
            )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# the profile surface of the transport: candles, catalog, control, lifecycle
# ---------------------------------------------------------------------------


@dataclass
class FakeCatalog:
    """Local catalog seam (no network: the symbols are literals)."""

    calls: int = 0

    def catalog(self) -> dict[str, Any]:
        self.calls += 1
        return {
            "symbols": [{"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"}],
            "strategies": ["basic"],
            "timeframes": ["1m", "1h"],
            "modes": ["paper", "live"],
        }


@dataclass
class FakeController:
    """Local lifecycle seam recording every command it is handed."""

    profiles: tuple[ProfileSnapshot, ...] = ()
    commands: list[tuple[str, str]] = field(default_factory=list)

    def pause_profile(self, profile_id: str) -> ProfileSnapshot:
        self.commands.append(("pause", profile_id))
        return self._snapshot(profile_id)

    def resume_profile(self, profile_id: str) -> ProfileSnapshot:
        self.commands.append(("resume", profile_id))
        return self._snapshot(profile_id)

    def delete_profile(self, profile_id: str) -> str:
        self.commands.append(("delete", profile_id))
        return profile_id

    def create_profile(self, payload: Mapping[str, Any]) -> ProfileSnapshot:
        self.commands.append(("create", str(payload["profile_id"])))
        return make_profile_snapshot(str(payload["profile_id"]))

    def control_state(self) -> Mapping[str, Any]:
        return {
            "profiles": [
                {"profile_id": profile.profile_id, "paused": False, "running": True}
                for profile in self.profiles
            ]
        }

    @staticmethod
    def _snapshot(profile_id: str) -> ProfileSnapshot:
        return make_profile_snapshot(profile_id)


def make_controller() -> FakeController:
    """Build a controller exposing the one deterministic profile."""
    return FakeController(profiles=(make_profile_snapshot(PROFILE_A),))


def test_catalog_and_control_round_trip_over_http(server: MonitoringServer) -> None:
    """Both new read routes answer the documented shapes with no engine running."""
    status, headers, payload = http_request(server, "GET", "/api/catalog")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    catalog = decode(payload)
    assert sorted(catalog) == ["modes", "strategies", "symbols", "timeframes"]
    assert sorted(catalog["symbols"][0]) == ["base", "quote", "symbol"]
    assert catalog["modes"] == ["paper", "live"]

    status, _, payload = http_request(server, "GET", "/api/control")
    assert status == 200
    control = decode(payload)
    assert sorted(control) == ["engine_running", "mutable", "profiles", "read_only"]
    assert control["engine_running"] is False
    assert control["read_only"] is True
    assert control["mutable"] is False
    assert control["profiles"] == []


def test_the_catalog_route_answers_an_injected_catalog(
    provider: FakeProvider, monitor: Monitor
) -> None:
    catalog = FakeCatalog()
    built = build_server(provider, monitor, catalog=catalog)
    with running(built) as server:
        status, _, payload = http_request(server, "GET", "/api/catalog")
        assert status == 200
        assert decode(payload)["symbols"] == [
            {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"}
        ]
        assert catalog.calls == 1


def test_the_candles_route_round_trips_and_validates_its_limit(
    server: MonitoringServer,
) -> None:
    status, headers, payload = http_request(server, "GET", f"/api/profiles/{PROFILE_A}/candles")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    body = decode(payload)
    assert sorted(body) == ["candles", "count"]
    assert body["count"] == 3
    for candle in body["candles"]:
        assert sorted(candle) == [
            "close",
            "closed",
            "high",
            "low",
            "open",
            "profile_id",
            "timestamp",
            "volume",
        ]
    stamps = [datetime.fromisoformat(candle["timestamp"]) for candle in body["candles"]]
    assert stamps == sorted(stamps)

    status, _, payload = http_request(server, "GET", f"/api/profiles/{PROFILE_A}/candles?limit=1")
    assert status == 200
    assert decode(payload)["count"] == 1

    status, _, payload = http_request(server, "GET", f"/api/profiles/{PROFILE_A}/candles?limit=0")
    assert status == 400
    assert decode(payload) == {
        "error": "malformed query parameter: 'limit' must be a positive integer"
    }

    status, _, payload = http_request(server, "GET", f"/api/profiles/{UNKNOWN_PROFILE}/candles")
    assert status == 404
    assert decode(payload) == {"error": f"unknown profile: {UNKNOWN_PROFILE!r}"}


@pytest.mark.parametrize(
    "method,path,body",
    [
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
    ],
)
def test_a_read_only_server_refuses_every_lifecycle_route_over_http(
    server: MonitoringServer, method: str, path: str, body: bytes
) -> None:
    status, _, payload = http_request(
        server, method, path, body=body, headers={"X-Operator-Token": TOKEN}
    )
    assert status == 403
    assert decode(payload) == {"error": "mutations are disabled on this server"}


@pytest.mark.parametrize(
    "method,path,allow",
    [
        ("PUT", "/api/catalog", "GET, HEAD"),
        ("POST", "/api/control", "GET, HEAD"),
        ("PUT", f"/api/profiles/{PROFILE_A}/pause", "POST"),
        ("GET", f"/api/profiles/{PROFILE_A}/pause", "POST"),
        ("POST", f"/api/profiles/{PROFILE_A}", "GET, HEAD, DELETE"),
        ("PUT", f"/api/profiles/{PROFILE_A}/candles", "GET, HEAD"),
    ],
)
def test_the_wrong_method_on_a_new_route_over_http(
    server: MonitoringServer, method: str, path: str, allow: str
) -> None:
    status, headers, payload = http_request(server, method, path, body=b"{}")
    assert status == 405
    assert headers["Allow"] == allow
    assert decode(payload) == {"error": "method not allowed"}


def test_a_writable_server_runs_the_whole_lifecycle_over_http(
    provider: FakeProvider, monitor: Monitor
) -> None:
    """The transport routes ``POST``/``DELETE`` to the injected controller."""
    controller = make_controller()
    built = build_server(
        provider,
        monitor,
        read_only=False,
        operator_token=TOKEN,
        controller=controller,
        catalog=FakeCatalog(),
    )
    auth = {"X-Operator-Token": TOKEN}
    with running(built) as server:
        status, _, payload = http_request(server, "GET", "/api/control")
        assert status == 200
        control = decode(payload)
        assert control["engine_running"] is True
        assert control["mutable"] is True
        assert [entry["profile_id"] for entry in control["profiles"]] == [PROFILE_A]

        status, _, payload = http_request(
            server, "POST", f"/api/profiles/{PROFILE_A}/pause", body=b"{}", headers=auth
        )
        assert status == 200
        assert decode(payload)["paused"] is True

        status, _, payload = http_request(
            server, "POST", f"/api/profiles/{PROFILE_A}/resume", body=b"{}", headers=auth
        )
        assert status == 200
        assert decode(payload)["paused"] is False

        body = json.dumps(
            {
                "profile_id": "sol-paper",
                "symbol": "SOL/USDT",
                "timeframe": "15m",
                "strategy": "basic",
                "mode": "paper",
            }
        ).encode()
        status, _, payload = http_request(server, "POST", "/api/profiles", body=body, headers=auth)
        assert status == 201
        assert decode(payload)["profile"]["profile_id"] == "sol-paper"

        status, _, payload = http_request(
            server, "DELETE", f"/api/profiles/{PROFILE_A}", headers=auth
        )
        assert status == 200
        assert decode(payload) == {"profile_id": PROFILE_A, "deleted": True}

        status, _, payload = http_request(
            server, "POST", f"/api/profiles/{UNKNOWN_PROFILE}/pause", body=b"{}", headers=auth
        )
        assert status == 404
        assert decode(payload) == {"error": f"unknown profile: {UNKNOWN_PROFILE!r}"}

    assert controller.commands == [
        ("pause", PROFILE_A),
        ("resume", PROFILE_A),
        ("create", "sol-paper"),
        ("delete", PROFILE_A),
    ]
