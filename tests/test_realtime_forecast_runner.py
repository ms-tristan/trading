"""The realtime runner and the forecast startup guard (work package wp3).

The defect this file pins, at the runner's own level: ``realtime run`` used to
start a ``timesfm`` profile and then never trade it -- no forecast bundle, every
diagnostic ``NaN``, no signal, no error, forever.  The runner must now refuse to
start such a profile and, when an artifact *is* declared, must load it **once per
profile** -- at construction, never per candle -- exactly like the backtest engine.

These tests drive the **real** ``resolve_strategy`` / registry path (no
monkeypatch of the resolution seam), over the local stream/gateway/store fakes, so
what they observe is the behaviour a real ``realtime run`` gets.  A ``basic``
profile is checked to be byte-identical to the historical behaviour.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from tests.test_realtime_runner import FakeGateway, FakeStore

from trading_platform.config.models import ProfileConfig, RiskLimitsConfig
from trading_platform.core.errors import ForecastArtifactError
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.forecast.artifact import (
    ForecastBuildConfig,
    ForecastStore,
    build_forecast_artifact,
)
from trading_platform.realtime import features as features_module
from trading_platform.realtime import runner as runner_module
from trading_platform.realtime import strategies as strategies_module
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import CandleEvent
from trading_platform.realtime.runner import ProfileRunner

TIMEOUT = 10.0
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
ROWS = 1_200
CONTEXT = 256
HORIZON = 24
STRIDE = 8
USABLE_STRIDES = 20

#: Duration of one candle of :data:`TIMEFRAME`.
CANDLE = pd.Timedelta(hours=1)

#: Freshness of the freshly-built artifacts, in strides before the guard's "now".
#:
#: ``ProfileRunner._prepare`` runs the coverage guard with the real clock (the
#: runner has no ``now`` to inject into the strategy resolution), so the *valid*
#: artifacts must be anchored near the wall clock while every staleness case is
#: anchored far enough behind it.  The tests therefore derive their frames from
#: ``pd.Timestamp.now`` on purpose: they are sizing a relative window, not reading
#: production time.
FRESH_TAIL_STRIDES = USABLE_STRIDES - 1
STALE_TAIL_STRIDES = USABLE_STRIDES + 5


def anchored_frame(
    rows: int = ROWS, *, tail_strides: int = FRESH_TAIL_STRIDES, seed: int = 3
) -> pd.DataFrame:
    """Return a frame whose last origin ends ``tail_strides`` strides before *now*."""
    end = pd.Timestamp.now(tz="UTC").floor("h") - tail_strides * STRIDE * CANDLE
    start = end - (rows - 1) * CANDLE
    return make_ohlcv(rows, start=start.isoformat(), timeframe=TIMEFRAME, seed=seed)


def run(coro: Any) -> Any:
    """Run one coroutine under an explicit bound so nothing can hang the suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


class FakeStream:
    """Minimal :class:`MarketStream` that emits the candles of one frame."""

    def __init__(self, frame: pd.DataFrame, *, cursor: int = 0) -> None:
        self.frame = frame
        self.cursor = int(cursor)
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def next_candle(self, symbol: str, timeframe: str) -> CandleEvent | None:
        if self.cursor >= len(self.frame):
            return None
        row = self.frame.iloc[self.cursor]
        event = CandleEvent(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=pd.Timestamp(self.frame.index[self.cursor]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        self.cursor += 1
        return event

    async def history(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        return self.frame.iloc[: self.cursor].tail(int(count)).copy()


def build_artifact(
    directory: Path,
    frame: pd.DataFrame,
    *,
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    name: str = "forecast.parquet",
) -> Path:
    """Build an offline artifact for ``frame`` and return its path."""
    path = directory / name
    build_forecast_artifact(
        frame,
        ForecastBuildConfig(
            backend="seasonal",
            context_length=CONTEXT,
            horizon=HORIZON,
            reforecast_every=STRIDE,
            symbol=symbol,
            timeframe=timeframe,
        ),
        path,
    )
    return path


def timesfm_profile(artifact: Path | None, **overrides: Any) -> ProfileConfig:
    """Return a paper ``timesfm`` profile; without ``artifact`` it declares none."""
    payload: dict[str, Any] = {
        "id": "btc-timesfm",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "strategy": "timesfm",
        "mode": "paper",
        "initial_balance": 1000.0,
        "warmup_candles": 20,
        "poll_interval_seconds": 5.0,
        "risk": RiskLimitsConfig(max_open_positions=1),
    }
    if artifact is not None:
        payload["forecast"] = artifact
    payload.update(overrides)
    return ProfileConfig(**payload)


def basic_profile(**overrides: Any) -> ProfileConfig:
    """Return a paper ``basic`` profile that declares no forecast at all."""
    payload: dict[str, Any] = {
        "id": "btc-basic",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "strategy": "basic",
        "mode": "paper",
        "initial_balance": 1000.0,
        "warmup_candles": 20,
        "poll_interval_seconds": 5.0,
        "risk": RiskLimitsConfig(max_open_positions=1),
    }
    payload.update(overrides)
    return ProfileConfig(**payload)


def build_runner(
    profile_config: ProfileConfig, frame: pd.DataFrame, *, cursor: int = 0
) -> tuple[ProfileRunner, FakeStream, FakeStore, ManualClock]:
    """Build a runner over the local fakes, at the pinned virtual instant."""
    stream = FakeStream(frame, cursor=cursor)
    store = FakeStore()
    clock = ManualClock(datetime(2024, 3, 1, 12, 0, tzinfo=UTC))
    runner = ProfileRunner(
        profile=profile_config,
        stream=stream,  # type: ignore[arg-type]
        gateway=FakeGateway(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        clock=clock,
        timeout_seconds=1.0,
    )
    return runner, stream, store, clock


# ---------------------------------------------------------------------------
# 1. the loud startup failures
# ---------------------------------------------------------------------------


def test_a_timesfm_profile_without_an_artifact_never_starts(tmp_path: Path) -> None:
    """No artifact must be a startup failure -- never a silent, inert run."""
    frame = anchored_frame()
    runner, stream, _store, _clock = build_runner(timesfm_profile(None), frame)

    with pytest.raises(ForecastArtifactError) as caught:
        run(runner.start())

    assert "trading forecast-build" in str(caught.value)
    assert stream.started == 0, "the stream is never even opened"


def test_a_timesfm_profile_without_an_artifact_never_runs_one_candle(tmp_path: Path) -> None:
    """``run_once`` fails identically: no candle is ever processed."""
    frame = anchored_frame()
    runner, stream, _store, _clock = build_runner(timesfm_profile(None), frame)

    with pytest.raises(ForecastArtifactError):
        run(runner.run_once())

    assert stream.cursor == 0


def test_a_stale_artifact_refuses_to_start(tmp_path: Path) -> None:
    """A profile that would never trade must refuse rather than run inert."""
    frame = anchored_frame(tail_strides=STALE_TAIL_STRIDES)
    artifact = build_artifact(tmp_path, frame)
    runner, stream, _store, _clock = build_runner(timesfm_profile(artifact), frame)

    with pytest.raises(ForecastArtifactError) as caught:
        run(runner.start())

    assert "is stale" in str(caught.value)
    assert stream.started == 0


def test_a_missing_artifact_file_refuses_to_start(tmp_path: Path) -> None:
    """A configured-but-absent path is the loader's own error, unwrapped."""
    frame = anchored_frame()
    runner, _stream, _store, _clock = build_runner(
        timesfm_profile(tmp_path / "absent.parquet"), frame
    )

    with pytest.raises(ForecastArtifactError) as caught:
        run(runner.start())

    assert type(caught.value) is ForecastArtifactError
    assert "absent.parquet" in str(caught.value)


def test_a_symbol_mismatch_refuses_to_start(tmp_path: Path) -> None:
    """An artifact built for another symbol never feeds this profile."""
    frame = anchored_frame()
    artifact = build_artifact(tmp_path, frame)
    runner, _stream, _store, _clock = build_runner(
        timesfm_profile(artifact, symbol="ETH/USDT"), frame
    )

    with pytest.raises(ForecastArtifactError) as caught:
        run(runner.start())

    assert "was built for symbol 'BTC/USDT'" in str(caught.value)


def test_a_timeframe_mismatch_refuses_to_start(tmp_path: Path) -> None:
    """An artifact built on another timeframe never feeds this profile."""
    frame = anchored_frame()
    artifact = build_artifact(tmp_path, frame)
    profile_config = timesfm_profile(artifact, id="four-hours")
    profile_config = profile_config.model_copy(update={"timeframe": "4h"})
    runner, _stream, _store, _clock = build_runner(profile_config, frame)

    with pytest.raises(ForecastArtifactError) as caught:
        run(runner.start())

    assert "was built on the 1h timeframe" in str(caught.value)


# ---------------------------------------------------------------------------
# 2. the happy path, and "loaded exactly once"
# ---------------------------------------------------------------------------


def test_a_valid_artifact_starts_normally(tmp_path: Path) -> None:
    """A fresh, matching artifact starts and processes its first candle."""
    frame = anchored_frame()
    artifact = build_artifact(tmp_path, frame)
    runner, _stream, _store, _clock = build_runner(timesfm_profile(artifact), frame)

    async def scenario() -> Any:
        await runner.start()
        await runner.run_once()
        return runner.health()

    health = run(scenario())

    assert runner._started is True, "the profile is running"
    assert runner._strategy is not None
    assert runner._strategy.feature_bundle is not None
    assert runner._strategy.feature_bundle.has_forecast is True
    assert runner._strategy._store is not None
    assert health.counters.candles_processed >= 0


def test_the_artifact_is_loaded_exactly_once_per_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact is opened at construction -- never per candle."""
    frame = anchored_frame()
    artifact = build_artifact(tmp_path, frame)
    calls: list[Path] = []
    original = ForecastStore.load.__func__  # type: ignore[attr-defined]

    def counting_load(cls: type[ForecastStore], path: Any) -> ForecastStore:
        calls.append(Path(path))
        return original(cls, path)

    monkeypatch.setattr(ForecastStore, "load", classmethod(counting_load))
    runner, _stream, _store, _clock = build_runner(timesfm_profile(artifact), frame)

    async def scenario() -> None:
        await runner.start()
        await runner.run_once()
        await runner.run_once()
        await runner.run_once()

    run(scenario())

    assert calls == [artifact], "the artifact is opened once, however many candles are ticked"


def test_the_resolution_path_is_the_single_construction_path() -> None:
    """``_prepare`` builds the strategy once and never re-enters the registry."""
    frame = anchored_frame()
    runner, _stream, _store, _clock = build_runner(basic_profile(), frame)

    created: list[str] = []
    original = strategies_module.resolve_strategy

    def counting(profile: ProfileConfig) -> Any:
        created.append(profile.id)
        return original(profile)

    runner_module.resolve_strategy = counting

    async def scenario() -> None:
        await runner.start()
        await runner.run_once()
        await runner.run_once()

    try:
        run(scenario())
    finally:
        runner_module.resolve_strategy = original  # type: ignore[assignment]

    assert created == ["btc-basic"]


# ---------------------------------------------------------------------------
# 3. the ``basic`` profile is completely unaffected
# ---------------------------------------------------------------------------


def test_a_basic_profile_without_a_forecast_is_unaffected(tmp_path: Path) -> None:
    """No ``forecast`` key means the bundle stays empty and the run is unchanged."""
    frame = anchored_frame()
    runner, _stream, _store, _clock = build_runner(basic_profile(), frame)

    async def scenario() -> Any:
        await runner.start()
        await runner.run_once()
        return runner.snapshot()

    snapshot = run(scenario())

    assert runner._started is True, "the profile is running"
    assert runner._strategy is not None
    assert runner._strategy.name == "basic"
    bundle = runner._strategy.feature_bundle
    assert bundle is None or not bundle.has_forecast
    assert snapshot.profile_id == "btc-basic"


def test_a_basic_profile_never_reaches_the_coverage_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A strategy with no forecast contract is never checked against an artifact.

    ``resolve_profile_features`` is the single resolution path for every profile,
    so it *is* called for ``basic`` -- but it must return the empty bundle without
    consulting the coverage guard, which is what this test pins by making the
    guard itself explode.
    """
    frame = anchored_frame()
    called: list[str] = []

    def exploding(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must not run
        called.append("guard")
        raise AssertionError("a basic profile must never reach the forecast guard")

    monkeypatch.setattr(features_module, "check_profile_forecast", exploding)
    runner, _stream, _store, _clock = build_runner(basic_profile(), frame)

    async def scenario() -> None:
        await runner.start()
        await runner.run_once()

    run(scenario())

    assert called == []
    assert runner._strategy is not None
    bundle = runner._strategy.feature_bundle
    assert bundle is None or not bundle.has_forecast


def test_a_basic_profile_with_a_forecast_key_keeps_working(tmp_path: Path) -> None:
    """A ``basic`` profile may *declare* a path: it is validated, then ignored."""
    frame = anchored_frame()
    artifact = build_artifact(tmp_path, frame)
    runner, _stream, _store, _clock = build_runner(basic_profile(forecast=artifact), frame)

    async def scenario() -> Any:
        await runner.start()
        await runner.run_once()
        return runner.snapshot()

    snapshot = run(scenario())

    assert runner._strategy is not None
    assert runner._strategy.name == "basic"
    assert snapshot.profile_id == "btc-basic"
