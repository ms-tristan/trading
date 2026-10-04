"""``GET /api/strategies``: the discovered catalogue and its live performance.

The strategy payload is the join of two sources -- the catalogue metadata of
``config/strategies.json`` (plus the strategy files on disk) and the profiles that
use each strategy -- so both halves are pinned here, including a strategy nobody
runs yet.

The catalogue *apply* route is pinned here as well, because it is reachable from
``config/profiles.json``: the document may change ``strategy`` and ``timeframe``,
which the PATCH body deliberately cannot, so an apply that changes one of them on
a running profile has to restart its worker. That claim is asserted against the
real engine and a recording launcher, since it is exactly the spawn that proves
it.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trading_platform.api.app import create_app
from trading_platform.api.security import OPERATOR_TOKEN_HEADER
from trading_platform.config import ENV_OPERATOR_TOKEN, PlatformSettings
from trading_platform.engine.config_builder import profile_config_path
from trading_platform.engine.supervisor import Supervisor
from trading_platform.models import ProfileConfig, ProfileSnapshot, StrategyMeta, format_ts, utc_now
from trading_platform.profiles.catalogue import StrategyCatalogue
from trading_platform.profiles.store import StateStore

TOKEN = "operator-token-of-the-test"


class _StubSupervisor:
    """Engine double: the state store is real, the process layer is not."""

    def __init__(
        self,
        store: StateStore,
        settings: PlatformSettings,
        *,
        alive: Iterable[str] = (),
    ) -> None:
        self.store = store
        self.settings = settings
        self.alive = set(alive)
        self.kill_switch = False
        self.calls: list[tuple[str, str]] = []

    def is_running(self, profile_id: str) -> bool:
        return profile_id in self.alive

    def healthy_count(self) -> int:
        return len(self.alive)

    def kill_switch_engaged(self) -> bool:
        return self.kill_switch

    def engage_kill_switch(self) -> None:
        self.kill_switch = True
        self.alive.clear()
        self.calls.append(("engage", ""))

    def release_kill_switch(self) -> None:
        self.kill_switch = False
        self.calls.append(("release", ""))

    def start_profile(self, profile_id: str, *_args: Any) -> None:
        self.alive.add(profile_id)
        self.calls.append(("start", profile_id))

    def stop_profile(self, profile_id: str, *_args: Any) -> None:
        self.alive.discard(profile_id)
        self.calls.append(("stop", profile_id))

    def restart_profile(self, profile_id: str) -> None:
        self.alive.add(profile_id)
        self.calls.append(("restart", profile_id))

    def apply_config_change(self, profile_id: str, changed_fields: Iterable[str]) -> bool:
        """Mirror the engine: a config-affecting catalogue change restarts a worker.

        The restart-forcing set is spelled out here rather than imported, so a
        change to the engine's ``CONFIG_AFFECTING_FIELDS`` that the apply route
        relies on shows up as a mismatch between this double and the real engine.
        """
        self.calls.append(("apply_config_change", profile_id))
        affecting = {
            "strategy",
            "timeframe",
            "mode",
            "exchange",
            "pairs",
            "initial_capital",
            "max_open_trades",
        }
        if not affecting.intersection(changed_fields) or not self.is_running(profile_id):
            return False
        self.restart_profile(profile_id)
        return True

    def apply_settings(self, *, snapshot_interval_seconds: int | None = None) -> PlatformSettings:
        changes: dict[str, int] = {}
        if snapshot_interval_seconds is not None:
            changes["snapshot_interval_seconds"] = int(snapshot_interval_seconds)
        self.settings = self.settings.with_overrides(**changes)
        return self.settings

    def schedule(self) -> None:
        self.calls.append(("schedule", ""))


def _catalogue() -> StrategyCatalogue:
    """Two documented strategies and one discovered file without metadata."""
    return StrategyCatalogue(
        [
            StrategyMeta(
                id="basic",
                class_name="BasicStrategy",
                file="BasicStrategy.py",
                title="EMA cross baseline",
                category="baseline",
                summary="Minimal EMA crossover.",
                indicators=["EMA(12)", "EMA(26)"],
            ),
            StrategyMeta(
                id="momentum",
                class_name="MomentumStrategy",
                file="MomentumStrategy.py",
                title="Momentum breakout",
                category="trend",
            ),
            StrategyMeta(id="faber", class_name="FaberStrategy", file="FaberStrategy.py"),
        ]
    )


def _profile(profile_id: str, **overrides: Any) -> ProfileConfig:
    fields: dict[str, Any] = {
        "id": profile_id,
        "name": profile_id.title(),
        "strategy": "basic",
        "timeframe": "1h",
        "pairs": ["BTC/USDT"],
        "initial_capital": 1000.0,
        "priority": 100,
    }
    fields.update(overrides)
    return ProfileConfig(**fields)


def _snapshot(profile_id: str, value: float, **overrides: Any) -> ProfileSnapshot:
    fields: dict[str, Any] = {
        "profile_id": profile_id,
        "ts": format_ts(),
        "portfolio_value": value,
        "cash": value,
    }
    fields.update(overrides)
    return ProfileSnapshot(**fields)


def _make_engine(
    tmp_path: Path,
    *,
    profiles: Sequence[ProfileConfig] = (),
    states: Mapping[str, str] | None = None,
    alive: Iterable[str] = (),
    snapshots: Sequence[ProfileSnapshot] = (),
) -> tuple[TestClient, _StubSupervisor, StateStore]:
    """Build a client over a real SQLite store and a stubbed process layer."""
    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    for profile in profiles:
        store.upsert_profile(profile, source="catalogue")
    for profile_id, state in (states or {}).items():
        store.set_profile_state(profile_id, state)
    for snapshot in snapshots:
        store.record_snapshot(snapshot)
    settings = PlatformSettings()
    supervisor = _StubSupervisor(store, settings, alive=alive)
    app = create_app(
        supervisor=supervisor,
        settings=settings,
        state_dir=tmp_path,
        start_engine=False,
    )
    app.state.strategy_catalogue = _catalogue()
    return TestClient(app), supervisor, store


# ---------------------------------------------------------------------------
# A real engine, for the apply route that is supposed to reach a worker
# ---------------------------------------------------------------------------
#: The strategy metadata document of the temporary deployments built below.
LIVE_STRATEGIES_DOCUMENT: dict[str, Any] = {
    "strategies": [
        {"id": "basic", "class_name": "BasicStrategy", "file": "BasicStrategy.py"},
        {"id": "momentum", "class_name": "MomentumStrategy", "file": "MomentumStrategy.py"},
    ]
}

#: Environment of those deployments: the Freqtrade binary is pinned, so the
#: recorded command line does not depend on the machine running the suite.
LIVE_ENV: dict[str, str] = {"TB_FREQTRADE_BIN": sys.executable}


class _FakeProcess:
    """A child handle: a pid, ``poll``, ``terminate``, ``kill`` and ``wait``."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        """Return the exit status, or ``None`` while the child is alive."""
        return self.returncode

    def terminate(self) -> None:
        """Record SIGTERM; the fake child exits with Freqtrade's status 130."""
        if self.returncode is None:
            self.returncode = 130

    def kill(self) -> None:
        """Record SIGKILL."""
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        """Return the exit status without ever blocking."""
        return 0 if self.returncode is None else self.returncode


class _RecordingLauncher:
    """A ``ProcessLauncher`` double that records the children it would start."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.processes: list[_FakeProcess] = []
        self.terminated: list[_FakeProcess] = []
        self._pid = 7000

    def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
    ) -> _FakeProcess:
        """Record the command line and return a fresh fake child."""
        self._pid += 1
        process = _FakeProcess(self._pid)
        self.calls.append(list(argv))
        self.processes.append(process)
        return process

    def terminate(self, process: _FakeProcess, *, grace_seconds: float = 0.0) -> None:
        """Record the stop and terminate the fake child."""
        self.terminated.append(process)
        process.terminate()


def _write_live_documents(config_dir: Path) -> None:
    """Write the three configuration documents of a temporary deployment."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "platform.json").write_text("{}", encoding="utf-8")
    (config_dir / "profiles.json").write_text(json.dumps({"profiles": []}), encoding="utf-8")
    (config_dir / "strategies.json").write_text(
        json.dumps(LIVE_STRATEGIES_DOCUMENT), encoding="utf-8"
    )


def _make_live_engine(
    tmp_path: Path,
    *,
    profiles: Sequence[ProfileConfig] = (),
) -> tuple[TestClient, Supervisor, StateStore, _RecordingLauncher]:
    """Build a client over a real engine whose children are only recorded.

    Nothing is started for real: the launcher records the command line and the
    port probe answers "free" instead of binding a socket. The supervisor itself
    is the production one, so the apply route really regenerates the worker
    configuration and respawns the worker.
    """
    state_dir = tmp_path / "realtime"
    state_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tmp_path / "config"
    _write_live_documents(config_dir)
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_dir / "state.db")
    store.bootstrap()
    for profile in profiles:
        store.upsert_profile(profile, source="catalogue", state="stopped")
    settings = PlatformSettings()
    launcher = _RecordingLauncher()
    supervisor = Supervisor(
        store=store,
        settings=settings,
        state_dir=state_dir,
        config_dir=config_dir,
        strategies_dir=strategies_dir,
        launcher=launcher,
        env=dict(LIVE_ENV),
        clock=utc_now,
        port_probe=lambda port: True,
    )
    app = create_app(
        supervisor=supervisor,
        settings=settings,
        state_dir=state_dir,
        start_engine=False,
    )
    app.state.strategy_catalogue = _catalogue()
    supervisor.bootstrap()
    return TestClient(app), supervisor, store, launcher


def test_strategies_keep_the_catalogue_order_and_metadata(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    payload = client.get("/api/strategies").json()

    assert [strategy["id"] for strategy in payload["strategies"]] == [
        "basic",
        "momentum",
        "faber",
    ]
    basic = payload["strategies"][0]
    assert basic["class_name"] == "BasicStrategy"
    assert basic["title"] == "EMA cross baseline"
    assert basic["category"] == "baseline"
    assert basic["summary"] == "Minimal EMA crossover."
    assert basic["indicators"] == ["EMA(12)", "EMA(26)"]


def test_strategies_aggregate_the_profiles_that_use_them(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(
        tmp_path,
        profiles=[
            _profile("basic-one", strategy="basic"),
            _profile("basic-two", strategy="basic", initial_capital=500.0),
            _profile("momentum-one", strategy="momentum"),
        ],
        states={"basic-one": "running", "basic-two": "stopped", "momentum-one": "running"},
        alive=["basic-one", "momentum-one"],
        snapshots=[
            _snapshot("basic-one", 1200.0, profit_abs=200.0),
            _snapshot("basic-two", 400.0, profit_abs=-100.0),
            _snapshot("momentum-one", 900.0, profit_abs=-100.0),
        ],
    )

    strategies = {item["id"]: item for item in client.get("/api/strategies").json()["strategies"]}

    assert strategies["basic"]["profile_count"] == 2
    assert strategies["basic"]["profiles_running"] == 1
    assert strategies["basic"]["portfolio_value"] == 1600.0
    assert strategies["basic"]["profit_abs"] == 100.0
    assert strategies["basic"]["profit_pct"] == pytest.approx(100.0 / 1500.0)
    assert strategies["basic"]["best_profile_id"] == "basic-one"
    assert strategies["momentum"]["profile_count"] == 1
    assert strategies["momentum"]["profiles_running"] == 1
    assert strategies["momentum"]["portfolio_value"] == 900.0


def test_a_strategy_nobody_runs_is_still_reported(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path, profiles=[_profile("basic-one")])

    faber = next(
        item for item in client.get("/api/strategies").json()["strategies"] if item["id"] == "faber"
    )

    assert faber["profile_count"] == 0
    assert faber["profiles_running"] == 0
    assert faber["portfolio_value"] == 0.0
    assert faber["profit_pct"] == 0.0
    assert faber["best_profile_id"] is None


def test_strategies_ignore_profiles_of_unknown_strategies(tmp_path: Path) -> None:
    """A profile pointing outside the catalogue contributes to no strategy."""
    client, _supervisor, _store = _make_engine(
        tmp_path, profiles=[_profile("orphan", strategy="not-declared")]
    )

    payload = client.get("/api/strategies").json()

    assert [strategy["profile_count"] for strategy in payload["strategies"]] == [0, 0, 0]


def test_strategies_need_no_operator_token(tmp_path: Path) -> None:
    client, _supervisor, _store = _make_engine(tmp_path)

    assert client.get("/api/strategies").status_code == 200


def test_strategies_fall_back_to_the_catalogue_of_the_checkout(tmp_path: Path) -> None:
    """Without an application override, the API discovers the real catalogue."""
    store = StateStore(tmp_path / "state.db")
    store.bootstrap()
    settings = PlatformSettings()
    app = create_app(
        supervisor=_StubSupervisor(store, settings),
        settings=settings,
        state_dir=tmp_path,
        start_engine=False,
    )

    strategies = TestClient(app).get("/api/strategies").json()["strategies"]

    ids = [strategy["id"] for strategy in strategies]
    assert "basic" in ids
    assert len(ids) == len(set(ids)) > 0
    assert all(strategy["title"] for strategy in strategies)


# ---------------------------------------------------------------------------
# The catalogue apply reaches the workers it changes
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OPERATOR_TOKEN, TOKEN)


def _with_catalogue(client: TestClient, profiles: Sequence[ProfileConfig]) -> None:
    """Install the declarative catalogue the apply route works from."""
    client.app.state.profile_catalogue = list(profiles)  # type: ignore[attr-defined]


def test_apply_restarts_a_running_profile_whose_strategy_changed(tmp_path: Path) -> None:
    """A catalogue change to ``strategy``/``timeframe`` reaches the running worker.

    The PATCH body cannot carry those two fields, so ``config/profiles.json`` is
    the only way an operator changes them -- and without the restart the worker
    would keep trading the strategy and the timeframe it was spawned with while
    the dashboard showed the new ones.
    """
    client, supervisor, store, launcher = _make_live_engine(tmp_path, profiles=[_profile("alpha")])
    assert len(launcher.calls) == 1
    _with_catalogue(client, [_profile("alpha", strategy="momentum", timeframe="4h")])

    payload = client.post(
        "/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    ).json()

    assert payload["updated"] == ["alpha"]
    assert len(launcher.calls) == 2
    assert launcher.terminated == [launcher.processes[0]]
    config = json.loads(
        profile_config_path(tmp_path / "realtime", "alpha").read_text(encoding="utf-8")
    )
    assert config["strategy"] == "MomentumStrategy"
    assert config["timeframe"] == "4h"
    stored = store.get_profile("alpha")
    assert stored is not None
    assert (stored.strategy, stored.timeframe) == ("momentum", "4h")
    assert supervisor.is_running("alpha")


def test_apply_restarts_nothing_when_the_catalogue_is_unchanged(tmp_path: Path) -> None:
    """A second apply is a no-op, and a ``name``-only change is one as well."""
    client, supervisor, _store, launcher = _make_live_engine(
        tmp_path, profiles=[_profile("alpha", name="Alpha")]
    )
    assert len(launcher.calls) == 1
    _with_catalogue(client, [_profile("alpha", name="Alpha")])

    unchanged = client.post(
        "/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    ).json()

    assert unchanged == {
        "created": [],
        "updated": [],
        "skipped": ["alpha"],
        "pruned": [],
        "refused_live": [],
    }
    assert len(launcher.calls) == 1

    _with_catalogue(client, [_profile("alpha", name="Renamed")])
    renamed = client.post(
        "/api/catalogue/apply", json={}, headers={OPERATOR_TOKEN_HEADER: TOKEN}
    ).json()

    assert renamed["updated"] == ["alpha"]
    assert len(launcher.calls) == 1
    assert supervisor.is_running("alpha")
    stored = supervisor.store.get_profile("alpha")
    assert stored is not None
    assert stored.name == "Renamed"
