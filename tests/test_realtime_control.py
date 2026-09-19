"""Tests of the thread -> event-loop bridge that carries the runtime commands.

The bridge exists because the monitoring transport is threaded (standard library
``ThreadingHTTPServer``) while the engine is asynchronous.  These tests therefore
use a **real** event loop, running in its own thread, and a local fake orchestrator
that records the calls it receives: nothing here imports the engine, binds a port,
touches the network or reads the wall clock (the one timeout under test is measured
by ``concurrent.futures``, which is the behaviour being pinned).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from trading_platform.config.models import ProfileConfig
from trading_platform.core.errors import ConfigError, MonitoringError, ProfileError
from trading_platform.realtime.control import RuntimeProfileController
from trading_platform.realtime.models import (
    ProfileHealth,
    ProfileSnapshot,
    ProfileStatus,
    RunMode,
)

#: How long a test may wait for the engine thread.  Every wait is bounded.
_WAIT = 5.0

#: The documented message of every mutating call made on an unbound controller.
UNBOUND = "the engine is not running: profile control is unavailable"


def snapshot(profile_id: str = "btc-paper") -> ProfileSnapshot:
    """Return a minimal, fully populated profile snapshot."""
    return ProfileSnapshot(
        profile_id=profile_id,
        symbol="BTC/USDT",
        timeframe="1h",
        strategy="basic",
        mode=RunMode.PAPER,
        status=ProfileStatus.RUNNING,
        initial_balance=1000.0,
        equity=1000.0,
        cash=1000.0,
        position_value=0.0,
        total_return=0.0,
        n_trades=0,
        open_positions=0,
        health=ProfileHealth(profile_id=profile_id, status=ProfileStatus.RUNNING),
    )


class EngineThread:
    """A real event loop owned by a background thread: the bridge's other side."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="engine-test-thread", daemon=True
        )
        self.thread_id: int | None = None

    def __enter__(self) -> EngineThread:
        self._thread.start()
        self.thread_id = self._thread.ident
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=_WAIT)
        self.loop.close()

    def bind(self, controller: RuntimeProfileController) -> None:
        """Bind the controller from *inside* the engine loop, like the CLI does."""
        future = asyncio.run_coroutine_threadsafe(self._bind(controller), self.loop)
        future.result(timeout=_WAIT)

    @staticmethod
    async def _bind(controller: RuntimeProfileController) -> None:
        controller.bind(asyncio.get_running_loop())


class FakeOrchestrator:
    """Local stand-in for the platform, recording every (awaited) call it gets."""

    def __init__(
        self,
        *,
        failure: BaseException | None = None,
        hang: bool = False,
        profiles: Mapping[str, ProfileConfig] | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.threads: list[int] = []
        self.failure = failure
        self.hang = hang
        self.profiles: dict[str, ProfileConfig] = dict(profiles or {})
        self.state: dict[str, Any] = {"profiles": [{"profile_id": "btc-paper"}]}
        self.entered = threading.Event()
        self.cancelled = threading.Event()

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        self.threads.append(threading.get_ident())

    async def _mutate(self, name: str, result: Any, *args: Any, **kwargs: Any) -> Any:
        self._record(name, *args, **kwargs)
        self.entered.set()
        if self.hang:
            try:
                await asyncio.sleep(3600.0)
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if self.failure is not None:
            raise self.failure
        return result

    async def pause_profile(self, profile_id: str) -> ProfileSnapshot:
        """Record a pause and return the snapshot of the paused profile."""
        return await self._mutate("pause_profile", snapshot(profile_id), profile_id)

    async def resume_profile(self, profile_id: str) -> ProfileSnapshot:
        """Record a resume and return the snapshot of the resumed profile."""
        return await self._mutate("resume_profile", snapshot(profile_id), profile_id)

    async def delete_profile(self, profile_id: str, *, profiles_path: str | Path) -> str:
        """Record a delete and return the removed identifier."""
        return await self._mutate(
            "delete_profile", str(profile_id), profile_id, profiles_path=profiles_path
        )

    async def add_profile(self, profile: ProfileConfig, *, profiles_path: str | Path) -> Any:
        """Record a create and return the snapshot of the created profile."""
        return await self._mutate(
            "add_profile", snapshot(str(profile.id)), profile, profiles_path=profiles_path
        )

    def profile_config(self, profile_id: str) -> ProfileConfig | None:
        """Return a declared profile, if the test declared one."""
        self.calls.append(("profile_config", (profile_id,), {}))
        return self.profiles.get(str(profile_id))

    def control_state(self) -> Mapping[str, Any]:
        """Return the fake pause/run state."""
        self.calls.append(("control_state", (), {}))
        return self.state

    def names(self) -> list[str]:
        """Return the recorded call names, in order."""
        return [name for name, _args, _kwargs in self.calls]


def controller_for(
    tmp_path: Path,
    fake: FakeOrchestrator,
    *,
    timeout_seconds: float = 10.0,
) -> RuntimeProfileController:
    """Build a controller over one local fake platform."""
    return RuntimeProfileController(
        orchestrator=fake,  # type: ignore[arg-type]
        profiles_path=tmp_path / "profiles.json",
        timeout_seconds=timeout_seconds,
    )


def valid_payload(**overrides: Any) -> dict[str, Any]:
    """Return a create payload the catalog accepts."""
    payload: dict[str, Any] = {
        "profile_id": "sol-paper",
        "symbol": "SOL/USDT",
        "timeframe": "15m",
        "strategy": "basic",
        "mode": "paper",
        "initial_balance": 500.0,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# the nominal bridge
# ---------------------------------------------------------------------------


def test_every_mutating_command_runs_on_the_engine_loop(tmp_path: Path) -> None:
    """The call blocks until the coroutine returned, and it ran in the engine thread."""
    fake = FakeOrchestrator()
    controller = controller_for(tmp_path, fake)
    with EngineThread() as engine:
        engine.bind(controller)
        assert controller.bound is True
        paused = controller.pause_profile("btc-paper")
        resumed = controller.resume_profile("btc-paper")
        removed = controller.delete_profile("btc-paper")
        created = controller.create_profile(valid_payload())

    assert paused.profile_id == "btc-paper"
    assert resumed.profile_id == "btc-paper"
    assert removed == "btc-paper"
    assert created.profile_id == "sol-paper"
    assert fake.names() == [
        "pause_profile",
        "resume_profile",
        "delete_profile",
        "profile_config",
        "add_profile",
    ]
    # the coroutine is awaited inside the engine loop, never in the HTTP thread
    assert fake.threads == [engine.thread_id] * 4
    assert all(identifier != threading.get_ident() for identifier in fake.threads)
    assert fake.calls[0][1] == ("btc-paper",)
    assert fake.calls[2][2]["profiles_path"] == tmp_path / "profiles.json"
    created_profile = fake.calls[4][1][0]
    assert isinstance(created_profile, ProfileConfig)
    assert created_profile.id == "sol-paper"
    assert created_profile.symbol == "SOL/USDT"
    assert created_profile.timeframe == "15m"
    assert created_profile.initial_balance == pytest.approx(500.0)


def test_control_state_is_a_synchronous_read_of_the_platform(tmp_path: Path) -> None:
    """The refresh payload needs no loop, and it never mutates anything."""
    fake = FakeOrchestrator()
    controller = controller_for(tmp_path, fake)
    assert controller.bound is False
    assert controller.control_state() == {"profiles": [{"profile_id": "btc-paper"}]}
    assert fake.names() == ["control_state"]
    assert controller.profiles_path == tmp_path / "profiles.json"
    assert "RuntimeProfileController" in repr(controller)


def test_bind_is_idempotent(tmp_path: Path) -> None:
    """Binding twice keeps the same loop, and the bridge keeps working."""
    fake = FakeOrchestrator()
    controller = controller_for(tmp_path, fake)
    with EngineThread() as engine:
        engine.bind(controller)
        first = controller.pause_profile("btc-paper")
        engine.bind(controller)
        second = controller.pause_profile("btc-paper")
    assert controller.bound is True
    assert first.profile_id == second.profile_id == "btc-paper"
    assert fake.names() == ["pause_profile", "pause_profile"]


# ---------------------------------------------------------------------------
# the failure surface
# ---------------------------------------------------------------------------


def test_a_command_on_an_unbound_controller_is_refused(tmp_path: Path) -> None:
    """Without a running engine there is no loop to marshal onto."""
    fake = FakeOrchestrator()
    controller = controller_for(tmp_path, fake)
    assert controller.bound is False
    with pytest.raises(MonitoringError) as pause:
        controller.pause_profile("btc-paper")
    assert str(pause.value) == UNBOUND
    with pytest.raises(MonitoringError) as resume:
        controller.resume_profile("btc-paper")
    assert str(resume.value) == UNBOUND
    with pytest.raises(MonitoringError) as delete:
        controller.delete_profile("btc-paper")
    assert str(delete.value) == UNBOUND
    with pytest.raises(MonitoringError) as create:
        controller.create_profile(valid_payload())
    assert str(create.value) == UNBOUND
    # a rejected payload is refused in the calling thread, before the bridge
    with pytest.raises(ConfigError):
        controller.create_profile(valid_payload(strategy="nope"))
    # only the duplicate check reads the platform; nothing was ever marshalled
    assert fake.names() == ["profile_config"]


@pytest.mark.parametrize(
    "failure",
    [
        ProfileError("unknown profile: 'nope'"),
        ConfigError("cannot write profiles file /tmp/profiles.json: read-only"),
        MonitoringError("the read model is unavailable"),
    ],
)
def test_an_error_raised_inside_the_loop_is_re_raised_unchanged(
    tmp_path: Path, failure: Exception
) -> None:
    """The router maps the type it receives, so the type must survive the bridge."""
    fake = FakeOrchestrator(failure=failure)
    controller = controller_for(tmp_path, fake)
    with EngineThread() as engine:
        engine.bind(controller)
        with pytest.raises(type(failure)) as excinfo:
            controller.pause_profile("btc-paper")
        with pytest.raises(type(failure)) as delete:
            controller.delete_profile("btc-paper")
    assert str(excinfo.value) == str(failure)
    assert str(delete.value) == str(failure)
    assert fake.names() == ["pause_profile", "delete_profile"]


def test_a_command_that_never_returns_times_out_and_is_cancelled(tmp_path: Path) -> None:
    """A stuck engine must not pin an HTTP worker for ever."""
    fake = FakeOrchestrator(hang=True)
    controller = controller_for(tmp_path, fake, timeout_seconds=0.05)
    with EngineThread() as engine:
        engine.bind(controller)
        with pytest.raises(MonitoringError) as excinfo:
            controller.pause_profile("btc-paper")
        assert fake.entered.wait(timeout=_WAIT) is True
        # the pending future is cancelled: the engine stops working on a command
        # nobody is waiting for any more.
        assert fake.cancelled.wait(timeout=_WAIT) is True
    assert str(excinfo.value) == "profile control timed out after 0.05s"


# ---------------------------------------------------------------------------
# create: the validation messages the API documents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            valid_payload(profile_id="bad id"),
            "invalid profile id: 'bad id' (expected ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$)",
        ),
        (valid_payload(profile_id=7), "invalid profile id: 7 (expected"),
        (
            valid_payload(strategy="nope"),
            "unknown strategy: 'nope' (available: basic, timesfm)",
        ),
        (
            valid_payload(timeframe="2h"),
            "unsupported timeframe: '2h' (supported: 1m, 5m, 15m, 30m, 1h, 4h, 1d)",
        ),
    ],
)
def test_create_profile_naming_errors_are_literal(
    tmp_path: Path, payload: dict[str, Any], message: str
) -> None:
    """The catalog decides, and the message says exactly what is allowed."""
    fake = FakeOrchestrator()
    controller = controller_for(tmp_path, fake)
    with pytest.raises(ConfigError) as excinfo:
        controller.create_profile(payload)
    assert str(excinfo.value).startswith(message)
    assert fake.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        valid_payload(mode="turbo"),
        valid_payload(initial_balance=0),
        valid_payload(symbol=""),
        {"profile_id": "sol-paper", "strategy": "basic", "timeframe": "1h"},
        valid_payload(params={"nope": object()}),
    ],
)
def test_create_profile_delegates_the_rest_to_the_profile_model(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    """Mode, balance, symbol and params are the model's rules, not the bridge's."""
    fake = FakeOrchestrator()
    controller = controller_for(tmp_path, fake)
    with pytest.raises(ConfigError, match="invalid profile:"):
        controller.create_profile(payload)
    assert fake.calls == []


def test_create_profile_refuses_a_duplicate_identifier(tmp_path: Path) -> None:
    """A declared identifier is refused before the engine is asked to start it."""
    declared = ProfileConfig(id="sol-paper", symbol="BTC/USDT")
    fake = FakeOrchestrator(profiles={"sol-paper": declared})
    controller = controller_for(tmp_path, fake)
    with EngineThread() as engine:
        engine.bind(controller)
        with pytest.raises(ProfileError) as excinfo:
            controller.create_profile(valid_payload())
    assert str(excinfo.value) == "profile already exists: 'sol-paper'"
    assert fake.names() == ["profile_config"]
