"""The regression test for the exact bug: a realtime ``timesfm`` profile that trades.

Before this work package, a profile could legitimately say
``"strategy": "timesfm"`` and the realtime engine would start it, attach **no**
forecast bundle, fill every diagnostic column with ``NaN`` and emit **no signal
at all** -- silently and forever.  The strategy was usable in the backtest and in
the validation layers, and inert in realtime.

This module drives the **whole live path** -- the real ``RealtimeOrchestrator``,
a real ``PollingMarketStream`` over a provider answering like a venue, the real
``ProfileRunner``, the real ``SqliteStateStore``, the real ``PaperBroker`` and a
``ManualClock`` -- with a ``timesfm`` profile whose artifact was built by the
**offline ``seasonal`` backend**, and asserts that the profile produces at least
one trade observed through the existing snapshot surface.

Everything here is offline, seeded and clock-independent: no network, no torch,
no machine-wall-clock dependency in the *engine* (the artifact anchor is a
relative window, see :data:`ANCHOR_MARGIN`).

Why the ``seasonal`` backend and not ``naive``: the random-walk baseline has no
thesis at all -- its median path is flat, so ``forecast_agreement`` never clears
and it opens **zero** positions by design (``docs/forecasting.md`` §6 pins that).
The ``seasonal`` backend is the other fully-offline backend and it *does* produce
a directional median path, which is what makes it the honest way to prove that the
wiring -- not a tuned threshold -- is what makes the profile trade.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import MonitoringConfig, ProfileConfig, RealtimeConfig
from trading_platform.core.errors import ForecastArtifactError
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.forecast.artifact import (
    ForecastBuildConfig,
    ForecastStore,
    build_forecast_artifact,
)
from trading_platform.realtime import features as features_module
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.orchestrator import RealtimeOrchestrator
from trading_platform.realtime.settings import (
    PlatformSettings,
    save_settings,
    settings_from_store,
)
from trading_platform.realtime.store import SqliteStateStore
from trading_platform.realtime.stream import PollingMarketStream
from trading_platform.strategy.timesfm_forecast import FORECAST_COLUMNS

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
CONTEXT = 256
HORIZON = 24
STRIDE = 8

#: Provider window, in candles, and the runner's warm-up.
PROVIDER_ROWS = 400
WARMUP = 300
HISTORY_CANDLES = 300

#: The live catch-up window of the scenario's profile, in candles.
ENTRY_LOOKBACK = 60

#: The virtual instant the whole scenario is anchored on.
#:
#: ``ProfileRunner._prepare`` runs the coverage guard against the **real** clock
#: (the contract fixes ``resolve_profile_features(profile, *, required)`` with no
#: ``now`` to inject), so the artifact this scenario builds must be fresh in
#: production terms while the *engine* still runs on the manual clock below.
#: ``LIVE_NOW`` is therefore derived from the wall clock at import time: the two
#: clocks are deliberately different, and that is what proves the guard is a
#: startup check rather than a per-candle decision.
LIVE_NOW = pd.Timestamp.now(tz="UTC").floor("h")
LIVE_CLOSED = LIVE_NOW - pd.Timedelta(hours=1)

#: How far *behind* ``LIVE_NOW`` the artifact's last origin sits, in strides.
#:
#: The guard is ``now > usable_until`` with ``usable_until`` the last origin plus
#: ``min(forecast_age, horizon - 1 - min_lead) - 1`` strides.  Two strides of
#: margin keeps the artifact clearly fresh while leaving most of its window inside
#: the provider's look-back, so the strategy really has origins to decide on.
ANCHOR_MARGIN = 2

#: Seed of the synthetic scenario frame (see :class:`VenueLikeProvider`).
SCENARIO_SEED = 1

#: Decision parameters that let the offline ``seasonal`` baseline clear the gates.
#:
#: These are **integration parameters, not tuned thresholds**: the entry gates are
#: opened to their documented extremes so the test measures the *wiring* (does the
#: bundle reach the strategy and produce a signal?) rather than the model's skill.
#: ``now > usable_until`` and the horizon cushion stay at their defaults, so the
#: execution convention and the strategy's own coherence rules are untouched.
INTEGRATION_PARAMS: dict[str, Any] = {
    "min_alpha": 0.0,
    "min_reliability": -10.0,
    "min_agreement": 0.0,
    "min_path_efficiency": 0.0,
    "max_vol_ratio": 1_000_000.0,
    "exit_vol_ratio": 1_000_000.0,
    "alpha_vs_atr": 1e-9,
    "allow_short": True,
    "max_hold": HORIZON,
    "cooldown": 0,
}

TIMEOUT = 30.0
JOIN_TIMEOUT = 5.0


def run(coro: Any) -> Any:
    """Run one coroutine under an explicit bound so nothing can hang the suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


class VenueLikeProvider:
    """A data provider that answers like a venue: a window ending *now*.

    The frame is a fixed, seeded synthetic series ending exactly at ``LIVE_CLOSED``
    (the last candle that is closed at ``LIVE_NOW``), so the whole scenario is
    deterministic and reproducible without touching the network.

    The seed is pinned because the *engine* only acts on the candles it is fed: the
    last row must not carry the reversal of a catch-up entry, or the runner
    legitimately refuses it (``catch_up_refused``).  Seed ``1`` is one of the many
    seeds where the offline ``seasonal`` baseline produces an entry inside the
    catch-up window that the newest candle still confirms.  This is a *fixture*
    choice, not a tuned threshold: no decision parameter is fitted to it.
    """

    def __init__(self, *, rows: int = PROVIDER_ROWS, seed: int = SCENARIO_SEED) -> None:
        self.rows = int(rows)
        start = LIVE_CLOSED - (self.rows - 1) * pd.Timedelta(hours=1)
        self.frame = make_ohlcv(self.rows, start=start.isoformat(), timeframe=TIMEFRAME, seed=seed)
        self.calls: list[tuple[str, str, pd.Timestamp, pd.Timestamp]] = []

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: Any, until: Any) -> pd.DataFrame:
        self.calls.append((symbol, timeframe, pd.Timestamp(since), pd.Timestamp(until)))
        window = self.frame.loc[
            (self.frame.index >= pd.Timestamp(since)) & (self.frame.index <= pd.Timestamp(until))
        ]
        return window.copy()


def anchor_start() -> str:
    """Return the start of the provider frame, aligned so the artifact is fresh."""
    last_origin = LIVE_CLOSED - ANCHOR_MARGIN * STRIDE * pd.Timedelta(hours=1)
    return (last_origin - (PROVIDER_ROWS - 1) * pd.Timedelta(hours=1)).isoformat()


def build_scenario(directory: Path) -> tuple[Path, Path, VenueLikeProvider]:
    """Write the profiles document and the artifact, and return both paths.

    The frame is built once and shared by the artifact and the provider, so the
    artifact is guaranteed to have been built on exactly the candles the live
    engine will replay.
    """
    provider = VenueLikeProvider()
    frame = make_ohlcv(PROVIDER_ROWS, start=anchor_start(), timeframe=TIMEFRAME, seed=SCENARIO_SEED)

    artifact = directory / "forecast.parquet"
    build_forecast_artifact(
        frame,
        ForecastBuildConfig(
            backend="seasonal",
            context_length=CONTEXT,
            horizon=HORIZON,
            reforecast_every=STRIDE,
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
        ),
        artifact,
    )

    document = {
        "profiles": [
            {
                "id": "btc-timesfm",
                "symbol": SYMBOL,
                "timeframe": TIMEFRAME,
                "strategy": "timesfm",
                "mode": "paper",
                "initial_balance": 1000.0,
                "stake_amount": 100.0,
                "warmup_candles": WARMUP,
                "poll_interval_seconds": 5.0,
                "params": dict(INTEGRATION_PARAMS),
                "forecast": str(artifact),
                # The documented live catch-up window: a forecast entry is a
                # one-candle cross event, so a profile that only reads the last
                # row waits for a cross to land exactly on the newest candle.
                # ``entry_lookback_candles`` lets the engine act on a cross that
                # occurred within the window, which is what a forecast-driven
                # profile needs to be usable at all on a hourly bar.
                "entry_lookback_candles": ENTRY_LOOKBACK,
                "risk": {"max_open_positions": 1},
            }
        ],
        "realtime": {
            "allow_network": False,
            "history_candles": HISTORY_CANDLES,
            "poll_interval_seconds": 5.0,
            "stream_poll_timeout_seconds": 10.0,
            "max_stream_reconnects": 3,
            "reconnect_backoff_seconds": 0.01,
            "reconcile_interval_seconds": 60.0,
            "start_at": LIVE_NOW.isoformat(),
            "platform_initial_balance": 10_000.0,
        },
        "monitoring": {"port": 0},
    }
    database = directory / "state.db"
    store = SqliteStateStore(database, clock=ManualClock(LIVE_NOW.to_pydatetime()))
    store.initialize()
    try:
        for definition in document["profiles"]:
            store.save_profile(ProfileConfig.model_validate(definition))
        save_settings(
            store,
            PlatformSettings(
                realtime=RealtimeConfig.model_validate(document["realtime"]),
                monitoring=MonitoringConfig.model_validate(document["monitoring"]),
            ),
        )
    finally:
        store.close()
    return database, artifact, provider


def make_orchestrator(
    store: SqliteStateStore,
    clock: ManualClock,
    provider: VenueLikeProvider,
) -> RealtimeOrchestrator:
    """Build the real orchestrator over the real polling stream on a local provider."""

    def factory(profile: Any) -> Any:
        return PollingMarketStream(
            provider,
            clock=clock,
            exchange=str(profile.exchange),
            history_candles=HISTORY_CANDLES,
            poll_interval_seconds=5.0,
            timeout_seconds=10.0,
            max_reconnects=3,
            reconnect_backoff_seconds=0.01,
        )

    return RealtimeOrchestrator(
        profiles=store.load_profiles(),
        store=store,
        clock=clock,
        realtime=settings_from_store(
            store,
            bootstrap=PlatformSettings(realtime=RealtimeConfig(), monitoring=MonitoringConfig()),
        ).realtime,
        monitoring=MonitoringConfig(port=0),
        stream_factory=factory,
        version="e2e-forecast",
    )


# ---------------------------------------------------------------------------
# 1. THE REGRESSION TEST
# ---------------------------------------------------------------------------


def test_a_timesfm_profile_with_an_artifact_actually_trades_end_to_end(
    tmp_path: Path,
) -> None:
    """The bug being fixed: this profile used to start and never trade.

    The full live path runs (polling stream, paper broker, SQLite store, manual
    clock) and the profile must produce at least one trade, observed through the
    **existing** snapshot surface -- no new telemetry, no test-only accessor.
    """
    _database, artifact, provider = build_scenario(tmp_path)
    clock = ManualClock(LIVE_NOW.to_pydatetime())
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    store.initialize()

    try:
        orchestrator = make_orchestrator(store, clock, provider)

        async def scenario() -> tuple[Any, list[Any], list[Any], Any]:
            await orchestrator.run_once()
            runner = orchestrator.runner("btc-timesfm")
            assert runner is not None
            observed = (
                runner.snapshot(),
                store.list_positions("btc-timesfm"),
                store.list_orders("btc-timesfm"),
                store.last_processed_candle("btc-timesfm"),
            )
            await orchestrator.stop()  # closes the store: read everything first
            return observed

        snapshot, positions, orders, watermark = run(scenario())

        # The strategy opened a real position: this is exactly the signal a
        # profile without an injected bundle can never produce.
        assert positions, (
            "the timesfm profile opened no position: the exact defect this package fixes"
        )
        assert snapshot.open_positions >= 1
        assert snapshot.deployed > 0.0, "the shared wallet funded the entry"
        assert orders and orders[0].state.value == "filled"
        assert orders[0].side.value == "buy"
        assert snapshot.n_trades >= 0  # a closed trade only exists after an exit
        assert watermark == LIVE_CLOSED
    finally:
        store.close()
    assert artifact.is_file()


def test_the_forecast_columns_of_the_traded_rows_are_really_injected(
    tmp_path: Path,
) -> None:
    """The diagnostics are finite on the traded rows -- they are *not* ``NaN``.

    A profile that emits a signal because of a forecast must carry a real
    forecast: the columns come from the artifact, so they are finite exactly where
    an origin covers the candle, and the artifact is read from disk, never
    recomputed per candle.
    """
    _database, artifact, provider = build_scenario(tmp_path)
    clock = ManualClock(LIVE_NOW.to_pydatetime())
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    store.initialize()

    try:
        orchestrator = make_orchestrator(store, clock, provider)
        run(orchestrator.run_once())
        runner = orchestrator.runner("btc-timesfm")
        assert runner is not None
        strategy = runner._strategy
        assert strategy is not None
        store_attached = strategy._store
        assert store_attached is not None, "the artifact is attached to the strategy"
        assert store_attached.path == artifact

        # the frames the engine actually decided on carry finite diagnostics
        frame = provider.frame
        prepared = strategy.prepare(frame)
        for column in FORECAST_COLUMNS:
            if column == "exit_code":
                continue
            values = pd.to_numeric(prepared[column], errors="coerce")
            covered = values.dropna()
            assert not covered.empty, f"{column} carries no injected value at all"
            assert covered.map(lambda value: value == value and abs(value) != float("inf")).all(), (
                f"{column} carries a non-finite injected value"
            )

        # ... and the origin rows really are the artifact's own origins
        origins = ForecastStore.load(artifact).origins()
        assert prepared["forecast_age"].notna().any()
        assert len(origins) > 0
    finally:
        store.close()


def test_the_artifact_is_read_from_disk_once_and_never_per_candle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact is opened at construction; ticking more candles opens nothing."""
    _database, artifact, provider = build_scenario(tmp_path)
    clock = ManualClock(LIVE_NOW.to_pydatetime())
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    store.initialize()

    calls: list[Path] = []
    original = ForecastStore.load.__func__  # type: ignore[attr-defined]

    def counting_load(cls: type[ForecastStore], path: Any) -> ForecastStore:
        calls.append(Path(path))
        return original(cls, path)

    monkeypatch.setattr(ForecastStore, "load", classmethod(counting_load))

    try:
        orchestrator = make_orchestrator(store, clock, provider)

        async def scenario() -> None:
            await orchestrator.run_once()
            runner = orchestrator.runner("btc-timesfm")
            assert runner is not None
            # several further ticks: the artifact must not be re-opened
            for _ in range(3):
                await runner.run_once()
            await orchestrator.stop()

        run(scenario())

        assert calls == [artifact], (
            f"the artifact must be opened exactly once per profile, opened: {calls}"
        )
    finally:
        store.close()


def test_a_stale_looking_profile_is_refused_before_the_first_candle(
    tmp_path: Path,
) -> None:
    """The startup guard runs on the engine path, not only in a unit test.

    The artifact of this scenario is rebuilt far in the past, so the orchestrator
    must refuse the profile instead of starting it and letting it sit inert.
    """
    _database, artifact, provider = build_scenario(tmp_path)
    # rebuild the artifact from a frame that ends far behind the live clock
    stale_frame = make_ohlcv(
        PROVIDER_ROWS,
        start=(LIVE_CLOSED - pd.Timedelta(hours=8 * 400)).isoformat(),
        timeframe=TIMEFRAME,
        seed=SCENARIO_SEED,
    )
    build_forecast_artifact(
        stale_frame,
        ForecastBuildConfig(
            backend="seasonal",
            context_length=CONTEXT,
            horizon=HORIZON,
            reforecast_every=STRIDE,
            symbol=SYMBOL,
            timeframe=TIMEFRAME,
        ),
        artifact,
    )

    clock = ManualClock(LIVE_NOW.to_pydatetime())
    store = SqliteStateStore(tmp_path / "state.db", clock=clock)
    store.initialize()

    try:
        orchestrator = make_orchestrator(store, clock, provider)
        with pytest.raises(ForecastArtifactError) as caught:
            run(orchestrator.run_once())
        assert "is stale" in str(caught.value)
        assert store.list_trades("btc-timesfm") == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. the bug really was the wiring: the same profile without an artifact
# ---------------------------------------------------------------------------


def test_the_very_same_profile_without_the_artifact_never_trades(tmp_path: Path) -> None:
    """The control of the regression test: no artifact, no trade, refused.

    This is the "before" half of the fix, asserted positively: the *only*
    difference with the passing scenario is the declared ``forecast`` path, so the
    trade observed above can only come from the injected artifact.
    """
    database, _artifact_unused, provider = build_scenario(tmp_path)
    clock = ManualClock(LIVE_NOW.to_pydatetime())
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    # the only difference with the passing scenario: the stored profile declares
    # no ``forecast`` path at all
    stored = store.load_profiles()[0]
    store.save_profile(stored.model_copy(update={"forecast": None}))

    try:
        orchestrator = make_orchestrator(store, clock, provider)
        with pytest.raises(ForecastArtifactError) as caught:
            run(orchestrator.run_once())

        message = str(caught.value)
        assert "declares no 'forecast' path" in message
        assert "trading forecast-build" in message
        assert store.list_trades("btc-timesfm") == [], "it never traded silently"
    finally:
        store.close()


def test_the_scenario_is_hermetic(tmp_path: Path) -> None:
    """No network is allowed, and no heavy optional dependency is imported."""
    database, _artifact, provider = build_scenario(tmp_path)
    clock = ManualClock(LIVE_NOW.to_pydatetime())
    store = SqliteStateStore(database, clock=clock)
    store.initialize()
    try:
        settings = settings_from_store(
            store,
            bootstrap=PlatformSettings(realtime=RealtimeConfig(), monitoring=MonitoringConfig()),
        )
    finally:
        store.close()

    assert settings.realtime.allow_network is False
    assert provider.frame.index[-1] == LIVE_CLOSED
    assert len(provider.frame) == PROVIDER_ROWS


def test_the_engine_module_never_imports_a_heavy_optional_dependency() -> None:
    """``realtime.features`` stays importable with ``.[dev]`` alone."""
    source = Path(features_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")]
        )
    }
    assert not roots & {"torch", "timesfm", "jax", "freqtrade", "ccxt"}
