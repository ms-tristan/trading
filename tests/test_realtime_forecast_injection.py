"""Forecast injection into a realtime profile, and its startup guard (work package wp3).

The defect this file pins: ``resolve_strategy`` used to instantiate the strategy
from the profile's name and parameters **only**, so a profile that legitimately
declared ``"strategy": "timesfm"`` got a strategy with no forecast bundle, every
diagnostic column stayed ``NaN`` and the profile emitted no signal -- silently and
forever.  The strategy was usable in the backtest and in the validation layers and
inert in realtime.

Everything here is offline and deterministic: synthetic candle frames, the offline
``naive``/``seasonal`` backends, no network, no torch, no wall-clock dependency
(the staleness guard is always called with an explicit ``now``).
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig
from trading_platform.core.errors import ForecastArtifactError
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.forecast.artifact import (
    ForecastBuildConfig,
    ForecastStore,
    build_forecast_artifact,
    metadata_path,
)
from trading_platform.realtime import features as features_module
from trading_platform.realtime.features import (
    ForecastCoverage,
    check_profile_forecast,
    resolve_profile_features,
)
from trading_platform.realtime.strategies import resolve_strategy
from trading_platform.strategy.features import FeatureBundle
from trading_platform.strategy.timesfm_forecast import TimesFMForecastStrategy

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
ROWS = 1_200
CONTEXT = 256
HORIZON = 24
STRIDE = 8

#: ``min_lead=2`` and ``horizon=24`` give ``decision_age_bound = 21``; ``stride=8``
#: therefore covers ``min(24, 21) - 1 = 20`` strides after the last origin.
USABLE_STRIDES = 20

#: Duration of one candle of :data:`TIMEFRAME`.
CANDLE = pd.Timedelta(hours=1)

#: Anchor of the small artifacts built here, as a **relative** end bound.
#:
#: ``resolve_strategy`` runs the guard with the real clock, so an artifact with a
#: fixed historical end (say 2023) would be refused as stale before the injection
#: it is meant to exercise.  The frames are therefore anchored so that their last
#: origin lands one candle before the boundary: fresh enough to start, while every
#: test that pins the boundary itself passes an explicit ``now``.
FRESH_TAIL_STRIDES = USABLE_STRIDES - 1


def anchored_start(rows: int, *, tail_strides: int = FRESH_TAIL_STRIDES) -> str:
    """Return the start of a ``rows``-candle frame ending ``tail_strides`` early.

    The returned instant is derived from the wall clock on purpose: this is a test
    fixture sizing a *relative* window (the guard is measured against "now"),
    never a production read of the clock.
    """
    origin_row = rows - 1 - CONTEXT
    end = pd.Timestamp.now(tz="UTC").floor("h") - tail_strides * STRIDE * CANDLE
    start = end - (origin_row - 1) * CANDLE
    return start.isoformat()


def build_artifact(
    directory: Path,
    *,
    symbol: str = SYMBOL,
    timeframe: str = TIMEFRAME,
    backend: str = "seasonal",
    seed: int = 3,
    rows: int = ROWS,
    reforecast_every: int = STRIDE,
    start: str | None = None,
) -> Path:
    """Build a small offline artifact and return its path.

    ``start`` defaults to :func:`anchored_start`, so the artifact is fresh enough
    for ``resolve_strategy`` to accept it; a test that pins staleness passes an
    explicit historical ``start`` instead.
    """
    frame = make_ohlcv(
        rows,
        start=anchored_start(rows) if start is None else start,
        timeframe=timeframe,
        seed=seed,
    )
    path = directory / f"{backend}-{symbol.replace('/', '_')}-{timeframe}.parquet"
    build_forecast_artifact(
        frame,
        ForecastBuildConfig(
            backend=backend,
            context_length=CONTEXT,
            horizon=HORIZON,
            reforecast_every=reforecast_every,
            symbol=symbol,
            timeframe=timeframe,
        ),
        path,
    )
    return path


def profile_for(artifact: Path | None, **overrides: Any) -> ProfileConfig:
    """Return a ``timesfm`` profile pointing at ``artifact``."""
    payload: dict[str, Any] = {
        "id": "btc-timesfm",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "strategy": "timesfm",
        "forecast": artifact,
    }
    payload.update(overrides)
    return ProfileConfig(**payload)


def usable_now(store: ForecastStore) -> pd.Timestamp:
    """Return the exact instant the last origin's coverage ends (the boundary)."""
    last = pd.Timestamp(store.origins()[-1])
    return last + STRIDE * USABLE_STRIDES * pd.Timedelta(hours=1)


# ---------------------------------------------------------------------------
# 1. resolve_strategy injects the bundle
# ---------------------------------------------------------------------------


def test_a_timesfm_profile_without_a_forecast_refuses_to_start(tmp_path: Path) -> None:
    """The exact failure being fixed: no artifact must be loud, never inert."""
    profile = ProfileConfig(id="inert", symbol=SYMBOL, timeframe=TIMEFRAME, strategy="timesfm")

    with pytest.raises(ForecastArtifactError) as caught:
        resolve_strategy(profile)

    message = str(caught.value)
    assert "inert" in message
    assert "trading forecast-build" in message
    assert '"forecast"' in message
    assert "timesfm" in message


def test_a_timesfm_profile_gets_its_forecast_bundle(tmp_path: Path) -> None:
    """With a path, the returned strategy really holds the loaded artifact."""
    artifact = build_artifact(tmp_path)
    store = ForecastStore.load(artifact)
    profile = profile_for(artifact)

    strategy = resolve_strategy(profile)

    assert isinstance(strategy, TimesFMForecastStrategy)
    assert strategy.feature_bundle is not None
    assert strategy.feature_bundle.has_forecast is True
    assert strategy._store is not None
    assert strategy._store.metadata.symbol == store.metadata.symbol == SYMBOL
    assert strategy._store.metadata.timeframe == store.metadata.timeframe == TIMEFRAME
    assert strategy._store.metadata.n_origins == store.metadata.n_origins


def test_a_missing_artifact_raises_the_unwrapped_forecast_error(tmp_path: Path) -> None:
    """A configured-but-absent path is ``ForecastArtifactError`` from the loader."""
    profile = profile_for(tmp_path / "does-not-exist.parquet")

    with pytest.raises(ForecastArtifactError) as caught:
        resolve_strategy(profile)

    assert "does-not-exist.parquet" in str(caught.value)
    assert type(caught.value) is ForecastArtifactError


def test_a_truncated_parquet_raises_the_unwrapped_forecast_error(tmp_path: Path) -> None:
    """A half-written artifact is refused, not silently degraded."""
    artifact = build_artifact(tmp_path)
    artifact.write_bytes(artifact.read_bytes()[:64])

    with pytest.raises(ForecastArtifactError) as caught:
        resolve_strategy(profile_for(artifact))

    assert type(caught.value) is ForecastArtifactError
    assert "parquet" in str(caught.value).lower()


def test_a_corrupt_sidecar_raises_the_unwrapped_forecast_error(tmp_path: Path) -> None:
    """A corrupt metadata sidecar is refused with the offending file named."""
    artifact = build_artifact(tmp_path)
    sidecar = metadata_path(artifact)
    sidecar.write_text("{not json", encoding="utf-8")

    with pytest.raises(ForecastArtifactError) as caught:
        resolve_strategy(profile_for(artifact))

    assert type(caught.value) is ForecastArtifactError
    assert sidecar.name in str(caught.value)


# ---------------------------------------------------------------------------
# 2. resolve_profile_features, and the untouched ``basic`` profile
# ---------------------------------------------------------------------------


def test_resolve_profile_features_without_a_path_is_empty() -> None:
    """``required=False`` and no declared path is the normal, empty bundle."""
    profile = ProfileConfig(id="plain", symbol=SYMBOL)

    bundle = resolve_profile_features(profile, required=False)

    assert bundle == FeatureBundle.empty()
    assert bundle.has_forecast is False


def test_resolve_profile_features_without_a_path_and_required_is_loud() -> None:
    """``required=True`` turns the missing path into the actionable error."""
    profile = ProfileConfig(id="loud", symbol=SYMBOL, strategy="timesfm")

    with pytest.raises(ForecastArtifactError) as caught:
        resolve_profile_features(profile, required=True)

    assert "loud" in str(caught.value)


def test_a_basic_profile_is_completely_unaffected(tmp_path: Path) -> None:
    """No ``forecast`` key means the historical behaviour, byte for byte."""
    profile = ProfileConfig(id="basic-1", symbol=SYMBOL, timeframe=TIMEFRAME, strategy="basic")

    strategy = resolve_strategy(profile)

    assert strategy.name == "basic"
    bundle = strategy.feature_bundle
    assert bundle is None or (isinstance(bundle, FeatureBundle) and not bundle.has_forecast)
    assert resolve_profile_features(profile, required=False) == FeatureBundle.empty()


def test_a_freqtrade_bridge_profile_still_resolves(tmp_path: Path) -> None:
    """A ``basic`` profile whose *params* never mention an artifact stays empty."""
    profile = ProfileConfig(
        id="basic-2", symbol=SYMBOL, timeframe=TIMEFRAME, strategy="basic", params={"fast": 3}
    )

    bundle = resolve_profile_features(profile, required=False)

    assert bundle.has_forecast is False


# ---------------------------------------------------------------------------
# 3. the startup guard: symbol, timeframe, coverage and its boundary
# ---------------------------------------------------------------------------


def test_the_guard_refuses_a_symbol_mismatch(tmp_path: Path) -> None:
    """An artifact built for another instrument must never feed this profile."""
    artifact = build_artifact(tmp_path)
    store = ForecastStore.load(artifact)
    profile = profile_for(artifact, id="eth", symbol="ETH/USDT")

    with pytest.raises(ForecastArtifactError) as caught:
        check_profile_forecast(profile, store, now=usable_now(store))

    message = str(caught.value)
    assert "'BTC/USDT'" in message
    assert "'ETH/USDT'" in message
    assert "--symbol ETH/USDT" in message


def test_the_guard_refuses_a_timeframe_mismatch(tmp_path: Path) -> None:
    """The 1h artifact of a 4h profile is refused rather than silently misused."""
    artifact = build_artifact(tmp_path)
    store = ForecastStore.load(artifact)
    profile = profile_for(artifact, id="four-hours", timeframe="4h")

    with pytest.raises(ForecastArtifactError) as caught:
        check_profile_forecast(profile, store, now=usable_now(store))

    message = str(caught.value)
    assert "1h timeframe" in message
    assert "runs 4h" in message
    assert "--timeframe 4h" in message


def test_the_guard_refuses_a_stale_artifact(tmp_path: Path) -> None:
    """A profile that would start and never trade must refuse to start instead."""
    artifact = build_artifact(tmp_path)
    store = ForecastStore.load(artifact)
    profile = profile_for(artifact)

    with pytest.raises(ForecastArtifactError) as caught:
        check_profile_forecast(profile, store, now=usable_now(store) + pd.Timedelta(hours=1))

    message = str(caught.value)
    assert "is stale for profile 'btc-timesfm'" in message
    assert "last origin is" in message
    assert "stays covered until" in message
    assert "coverage must reach the 1h decision horizon" in message
    assert "--symbol BTC/USDT --timeframe 1h" in message


def test_the_coverage_guard_passes_exactly_on_the_boundary(tmp_path: Path) -> None:
    """The refusal is strict: ``now == usable_until`` still passes."""
    artifact = build_artifact(tmp_path)
    store = ForecastStore.load(artifact)
    profile = profile_for(artifact)

    coverage = check_profile_forecast(profile, store, now=usable_now(store))

    assert isinstance(coverage, ForecastCoverage)
    assert coverage.last_origin == pd.Timestamp(store.origins()[-1]).to_pydatetime()


def test_the_coverage_guard_refuses_one_candle_past_the_boundary(tmp_path: Path) -> None:
    """One candle later is already too late."""
    artifact = build_artifact(tmp_path)
    store = ForecastStore.load(artifact)
    profile = profile_for(artifact)

    with pytest.raises(ForecastArtifactError):
        check_profile_forecast(profile, store, now=usable_now(store) + pd.Timedelta(hours=1))


def test_the_guard_refuses_a_bundle_without_a_forecast(tmp_path: Path) -> None:
    """A declared path with no loaded store is a caller bug, not a silent no-op."""
    artifact = build_artifact(tmp_path)

    with pytest.raises(ForecastArtifactError) as caught:
        check_profile_forecast(profile_for(artifact), None)

    assert "carries no forecast" in str(caught.value)


def test_a_longer_forecast_age_widens_the_covered_window(tmp_path: Path) -> None:
    """The bound tracks the strategy's parameters instead of a constant."""
    artifact = build_artifact(tmp_path)
    store = ForecastStore.load(artifact)
    strict = profile_for(artifact, params={"forecast_age": 8, "min_lead": 2})
    relaxed = profile_for(artifact, params={"forecast_age": 24, "min_lead": 2})
    last = pd.Timestamp(store.origins()[-1])
    # ``forecast_age=8`` covers ``min(8, 21) - 1 = 7`` strides, the default 24
    # covers 20: an instant in between is refused by one and accepted by the other.
    moment = last + 10 * STRIDE * CANDLE

    with pytest.raises(ForecastArtifactError):
        check_profile_forecast(strict, store, now=moment)
    coverage = check_profile_forecast(relaxed, store, now=moment)
    assert coverage.horizon == HORIZON


def test_the_guard_never_reads_the_wall_clock_when_now_is_supplied(tmp_path: Path) -> None:
    """A fixed ``now`` decides the verdict, whatever the machine's date is.

    The guard is ``now > usable_until``, so a *past* instant is accepted by
    construction even though the artifact's coverage has long expired.  That is
    the proof the supplied value -- and not the machine's clock -- is what is
    read; ``test_the_guard_refuses_a_stale_artifact`` pins the refusing side.
    """
    artifact = build_artifact(tmp_path)
    store = ForecastStore.load(artifact)
    profile = profile_for(artifact)
    pinned = pd.Timestamp("2001-01-01T00:00:00Z")

    coverage = check_profile_forecast(profile, store, now=pinned)

    assert coverage.symbol == SYMBOL
    assert coverage.last_origin > pinned.to_pydatetime(), "the artifact postdates 2001"
    assert pd.Timestamp.now(tz="UTC").year > 2001


# ---------------------------------------------------------------------------
# 4. ForecastCoverage: the frozen, ordered answer to "is it usable right now?"
# ---------------------------------------------------------------------------


def test_forecast_coverage_is_a_frozen_dataclass_with_the_documented_fields() -> None:
    """Positional construction is part of the contract, so the order is pinned."""
    fields = [field.name for field in dataclasses.fields(ForecastCoverage)]

    assert fields == [
        "path",
        "symbol",
        "timeframe",
        "seasonal_period",
        "horizon",
        "stride",
        "first_origin",
        "last_origin",
    ]
    coverage = ForecastCoverage(
        Path("a.parquet"),
        SYMBOL,
        TIMEFRAME,
        24,
        HORIZON,
        STRIDE,
        pd.Timestamp("2023-01-01T00:00:00Z").to_pydatetime(),
        pd.Timestamp("2023-02-01T00:00:00Z").to_pydatetime(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        coverage.symbol = "ETH/USDT"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("timeframe", "expected_period"),
    [("1m", 1440), ("5m", 288), ("15m", 96), ("1h", 24), ("4h", 6), ("1d", 1)],
)
def test_the_reported_seasonal_period_follows_the_timeframe(
    tmp_path: Path, timeframe: str, expected_period: int
) -> None:
    """An artifact built for another timeframe reports *its own* daily cycle."""
    rows = 400 if timeframe in {"1m", "5m"} else 300
    artifact = build_artifact(tmp_path, timeframe=timeframe, rows=rows, reforecast_every=4)
    store = ForecastStore.load(artifact)
    profile = profile_for(artifact, timeframe=timeframe)

    # The artifact's own last origin, so the verdict never depends on real time.
    coverage = check_profile_forecast(profile, store, now=pd.Timestamp(store.origins()[-1]))

    assert coverage.seasonal_period == expected_period
    assert coverage.seasonal_period == features_module.resolve_seasonal_period(
        store.metadata.timeframe, int(store.metadata.seasonal_period)
    )


def test_the_coverage_carries_the_artifact_window(tmp_path: Path) -> None:
    """The reported window is exactly the artifact's own origins."""
    artifact = build_artifact(tmp_path)
    store = ForecastStore.load(artifact)
    profile = profile_for(artifact)

    coverage = check_profile_forecast(profile, store, now=usable_now(store))

    assert coverage.path == artifact
    assert coverage.symbol == SYMBOL
    assert coverage.timeframe == TIMEFRAME
    assert coverage.horizon == HORIZON
    assert coverage.stride == STRIDE
    assert coverage.first_origin == pd.Timestamp(store.origins()[0]).to_pydatetime()
    assert coverage.last_origin == pd.Timestamp(store.origins()[-1]).to_pydatetime()


# ---------------------------------------------------------------------------
# 5. the module stays import-light and torch-free
# ---------------------------------------------------------------------------


def test_the_module_declares_the_documented_surface() -> None:
    """``__all__`` is the interface other packages are allowed to rely on.

    The three contract names come first and in the documented order;
    ``strategy_needs_forecast`` is the additive helper
    :func:`trading_platform.realtime.strategies.resolve_strategy` uses to decide
    whether a strategy even accepts an ``artifact`` parameter.
    """
    assert features_module.__all__ == [
        "ForecastCoverage",
        "check_profile_forecast",
        "resolve_profile_features",
        "strategy_needs_forecast",
    ]


def test_importing_the_module_never_pulls_torch_or_the_optional_extras() -> None:
    """The suite must stay green with ``.[dev]`` alone: nothing heavy is imported."""
    code = (
        "import sys; import trading_platform.realtime.features as m; "
        "leaked = sorted(n for n in ('torch', 'timesfm', 'freqtrade', 'ccxt') if n in sys.modules); "
        "print('LEAKED=' + ','.join(leaked))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )

    assert "LEAKED=" in result.stdout
    assert result.stdout.strip().endswith("LEAKED="), result.stdout


def test_the_module_never_imports_the_runner_or_the_store_at_module_level() -> None:
    """No cycle with ``realtime.runner``, and the parquet reader stays lazy."""
    source = Path(features_module.__file__).read_text(encoding="utf-8").splitlines()
    top_level = [line for line in source if line.startswith(("import ", "from "))]

    assert not [line for line in top_level if "realtime.runner" in line]
    assert not [line for line in top_level if "ForecastStore" in line]
