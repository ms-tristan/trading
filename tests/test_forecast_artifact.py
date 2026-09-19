"""Contract tests for the versioned forecast artifact and its store.

Everything here is offline and dependency-free (no ``torch``, no ``timesfm``, no
network): the artifact is built with the pure-``numpy`` backends, or with a
recording stub, and read back through :class:`ForecastStore`.

What the file pins, in order:

* the frozen schema (:data:`ARTIFACT_COLUMNS`, dtypes, index name and timezone,
  row-major quantiles) and the sidecar metadata round-trip;
* the **no-look-ahead** property: an artifact built from a truncated candle frame
  is identical, on the origins both frames share, to the artifact built from the
  whole frame;
* the builder contract (origins on a stride, causal contexts, one backend call
  with every request, a copy of the request list, validated backend answers);
* the store contract (lookup, lazy per-origin cache, empty artifact, uncovered
  tail) and every way an artifact can be missing, corrupt or incompatible.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import trading_platform
from trading_platform.core.errors import ForecastArtifactError, ForecastError
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.forecast.artifact import (
    ARTIFACT_COLUMNS,
    ARTIFACT_SCHEMA_VERSION,
    ORIGIN_INDEX_NAME,
    ArtifactMetadata,
    ForecastBuildConfig,
    ForecastStore,
    build_forecast_artifact,
    build_forecast_frame,
    metadata_path,
    write_forecast_artifact,
)
from trading_platform.forecast.registry import BACKENDS
from trading_platform.forecast.series import deseasonalize_log_price
from trading_platform.forecast.types import ForecastRequest, ForecastTrajectory

#: Quantile levels used by almost every test (low / median / high).
LEVELS: tuple[float, ...] = (0.1, 0.5, 0.9)

#: Fixed creation timestamp, so two builds of the same input are comparable.
CREATED_AT = pd.Timestamp("2024-06-01T00:00:00Z")

#: A small but realistic build: 6 origins over 120 hourly candles.
SMALL_CANDLES = 120


def build_config(**overrides: Any) -> ForecastBuildConfig:
    """Return a small, fast build configuration, with ``overrides`` applied."""
    params: dict[str, Any] = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "backend": "naive",
        "context_length": 32,
        "horizon": 5,
        "reforecast_every": 16,
        "quantile_levels": LEVELS,
        "seasonal_period": 24,
        "seasonal_window": 48,
    }
    params.update(overrides)
    return ForecastBuildConfig(**params)


def candles(count: int = SMALL_CANDLES) -> pd.DataFrame:
    """Return a deterministic synthetic OHLCV frame of ``count`` hourly candles."""
    return make_ohlcv(count)


def build_artifact(
    frame: pd.DataFrame, path: Path, **overrides: Any
) -> tuple[Path, ArtifactMetadata]:
    """Build the default (``naive``) artifact of ``frame`` at ``path``."""
    features = overrides.pop("features", ())
    return build_forecast_artifact(
        frame, build_config(**overrides), path, created_at=CREATED_AT, features=features
    )


def write_raw(frame: pd.DataFrame, metadata: ArtifactMetadata, path: Path) -> Path:
    """Write a frame and a sidecar **without validation** (to test the reader)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=True)
    metadata_path(path).write_text(
        json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def read_payload(path: Path) -> dict[str, Any]:
    """Return the sidecar payload of the artifact at ``path``."""
    payload: dict[str, Any] = json.loads(metadata_path(path).read_text(encoding="utf-8"))
    return payload


def assert_rows_equal(left: pd.DataFrame, right: pd.DataFrame) -> None:
    """Assert two artifact frames match row by row (object columns hold arrays)."""
    assert list(left.columns) == list(right.columns)
    assert list(left.index) == list(right.index)
    for column in ARTIFACT_COLUMNS:
        if column in ("quantile_levels", "median", "quantiles"):
            for position, (one, other) in enumerate(zip(left[column], right[column], strict=True)):
                assert isinstance(one, np.ndarray) and isinstance(other, np.ndarray)
                assert np.array_equal(one, other), f"{column} differs on row {position}"
        else:
            assert left[column].tolist() == right[column].tolist()


class RecordingBackend:
    """Backend recording its calls and returning zero paths, or custom rows."""

    name = "recording"

    def __init__(
        self,
        rows: Callable[[ForecastRequest, int, tuple[float, ...]], np.ndarray] | None = None,
        *,
        mutate_requests: bool = False,
    ) -> None:
        self.calls: list[tuple[list[ForecastRequest], int]] = []
        self._rows = rows
        self._mutate = mutate_requests

    def is_available(self) -> bool:
        """Return ``True``: this stub needs nothing."""
        return True

    def predict(
        self, requests: Sequence[ForecastRequest], *, horizon: int
    ) -> list[ForecastTrajectory]:
        """Record the call, then answer one zero (or custom) trajectory per request."""
        received = list(requests)
        self.calls.append((received, horizon))
        if self._mutate and isinstance(requests, list):
            # The model wrappers are known to pad the list they are given in place.
            requests.append(
                ForecastRequest(origin=pd.Timestamp("2000-01-01T00:00:00Z"), context=(0.0, 1.0))
            )
        trajectories: list[ForecastTrajectory] = []
        for request in received:
            if self._rows is None:
                quantiles = np.zeros((len(LEVELS), horizon), dtype="float32")
            else:
                quantiles = self._rows(request, horizon, LEVELS)
            trajectories.append(
                ForecastTrajectory(
                    origin=request.origin,
                    timeframe="1h",
                    horizon=horizon,
                    quantile_levels=LEVELS,
                    quantiles=quantiles,
                )
            )
        return trajectories


class BrokenBackend:
    """Backend returning whatever ``answer`` produces (to test the guard rails)."""

    name = "broken"

    def __init__(self, answer: Callable[[ForecastRequest, int], Any]) -> None:
        self._answer = answer

    def is_available(self) -> bool:
        """Return ``True``: this stub needs nothing."""
        return True

    def predict(self, requests: Sequence[ForecastRequest], *, horizon: int) -> Any:
        """Return the configured (possibly invalid) answer."""
        return [self._answer(request, horizon) for request in requests]


class NonIterableBackend:
    """Backend returning something that is not a sequence at all."""

    name = "non-iterable"

    def is_available(self) -> bool:
        """Return ``True``: this stub needs nothing."""
        return True

    def predict(self, requests: Sequence[ForecastRequest], *, horizon: int) -> Any:
        """Return a non-iterable answer."""
        return 5


def trajectory_at(request: ForecastRequest, horizon: int, **changes: Any) -> ForecastTrajectory:
    """Return a valid trajectory for ``request``, with ``changes`` applied."""
    params: dict[str, Any] = {
        "origin": request.origin,
        "timeframe": "1h",
        "horizon": horizon,
        "quantile_levels": LEVELS,
        "quantiles": np.zeros((len(LEVELS), horizon), dtype="float32"),
    }
    params.update(changes)
    return ForecastTrajectory(**params)


def altered_trajectory(request: ForecastRequest, changes: dict[str, Any]) -> ForecastTrajectory:
    """Return a trajectory for ``request`` whose fields are overridden by ``changes``."""
    params: dict[str, Any] = {
        "origin": request.origin,
        "timeframe": "1h",
        "horizon": 5,
        "quantile_levels": LEVELS,
        "quantiles": np.zeros((len(LEVELS), 5), dtype="float32"),
    }
    params.update(changes)
    return ForecastTrajectory(**params)


# ---------------------------------------------------------------------------
# schema constants and metadata
# ---------------------------------------------------------------------------


def test_schema_constants_are_frozen() -> None:
    assert ARTIFACT_SCHEMA_VERSION == 1
    assert ORIGIN_INDEX_NAME == "origin"
    assert ARTIFACT_COLUMNS == (
        "horizon",
        "stride",
        "n_quantiles",
        "quantile_levels",
        "median",
        "quantiles",
    )


def test_metadata_path_appends_the_sidecar_suffix() -> None:
    assert metadata_path("a.parquet") == Path("a.parquet.meta.json")
    assert metadata_path(Path("data/forecast/btc.parquet")) == Path(
        "data/forecast/btc.parquet.meta.json"
    )


def test_metadata_to_dict_is_sorted_and_json_safe(tmp_path: Path) -> None:
    _, metadata = build_artifact(candles(), tmp_path / "a.parquet", license="Apache-2.0")
    payload = metadata.to_dict()
    assert list(payload) == sorted(payload)
    assert payload["schema_version"] == ARTIFACT_SCHEMA_VERSION
    assert payload["symbol"] == "BTC/USDT"
    assert payload["timeframe"] == "1h"
    assert payload["backend"] == "naive"
    assert payload["model_id"] is None
    assert payload["context_length"] == 32
    assert payload["stride"] == 16
    assert payload["horizon"] == 5
    assert payload["quantile_levels"] == list(LEVELS)
    assert payload["license"] == "Apache-2.0"
    assert payload["package_version"] == trading_platform.__version__
    assert payload["created_at"] == CREATED_AT.isoformat()
    assert payload["deseasonalized"] is True
    assert payload["seasonal_period"] == 24
    assert payload["seasonal_window"] == 48
    assert payload["features"] == []
    assert pd.Timestamp(payload["data_start"]) == pd.Timestamp("2023-01-01T00:00:00Z")
    assert json.loads(json.dumps(payload)) == payload


def test_metadata_round_trips_through_json(tmp_path: Path) -> None:
    _, metadata = build_artifact(
        candles(), tmp_path / "a.parquet", model_id="google/timesfm-2.5-200m-pytorch"
    )
    payload = json.loads(json.dumps(metadata.to_dict()))
    restored = ArtifactMetadata.from_dict(payload)
    assert restored == metadata
    assert restored.quantile_levels == LEVELS
    assert restored.features == ()


def test_metadata_from_dict_lists_every_offending_key(tmp_path: Path) -> None:
    _, metadata = build_artifact(candles(), tmp_path / "a.parquet")
    payload = metadata.to_dict()
    payload.pop("symbol")
    payload["horizon"] = "five"
    payload["deseasonalized"] = 1
    payload["data_start"] = 20240101
    payload["quantile_levels"] = "deciles"
    payload["features"] = [1, 2]
    with pytest.raises(ForecastArtifactError) as info:
        ArtifactMetadata.from_dict(payload)
    issues = " ".join(info.value.issues)
    for key in ("symbol", "horizon", "deseasonalized", "data_start", "quantile_levels", "features"):
        assert key in issues, f"{key!r} is not reported in {info.value.issues!r}"


def test_metadata_from_dict_rejects_a_non_mapping() -> None:
    with pytest.raises(ForecastArtifactError):
        ArtifactMetadata.from_dict([1, 2, 3])  # type: ignore[arg-type]


def test_metadata_from_dict_accepts_a_null_model_id(tmp_path: Path) -> None:
    _, metadata = build_artifact(candles(), tmp_path / "a.parquet")
    payload = metadata.to_dict()
    payload["model_id"] = None
    assert ArtifactMetadata.from_dict(payload).model_id is None


BAD_METADATA_FIELDS: list[tuple[str, dict[str, Any], str]] = [
    ("text", {"symbol": 5}, "must be a string"),
    ("optional_text", {"model_id": 5}, "must be a string or null"),
    ("boolean", {"deseasonalized": "yes"}, "must be a boolean"),
    ("timestamp_type", {"data_start": 5}, "ISO-8601"),
    ("timestamp_value", {"data_start": "not a time"}, "not a valid timestamp"),
    ("float_tuple_type", {"quantile_levels": {"a": 1}}, "must be a list of numbers"),
    ("float_tuple_item", {"quantile_levels": [0.1, "x"]}, "must hold numbers"),
    ("text_tuple_type", {"features": {"a": 1}}, "must be a list of strings"),
    ("text_tuple_item", {"features": [1]}, "must hold strings"),
    ("created_at", {"created_at": 5}, "must be a string"),
]


@pytest.mark.parametrize(
    ("case", "changes", "expected"),
    BAD_METADATA_FIELDS,
    ids=[case for case, _, _ in BAD_METADATA_FIELDS],
)
def test_metadata_from_dict_reports_each_malformed_field(
    tmp_path: Path, case: str, changes: dict[str, Any], expected: str
) -> None:
    _, metadata = build_artifact(candles(), tmp_path / "a.parquet")
    payload = metadata.to_dict()
    payload.update(changes)
    with pytest.raises(ForecastArtifactError) as info:
        ArtifactMetadata.from_dict(payload)
    assert any(expected in issue for issue in info.value.issues), case


# ---------------------------------------------------------------------------
# builder: schema, metadata, origins
# ---------------------------------------------------------------------------


def test_build_forecast_frame_pins_the_schema() -> None:
    frame, metadata = build_forecast_frame(candles(), build_config(), created_at=CREATED_AT)
    assert list(frame.columns) == list(ARTIFACT_COLUMNS)
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.name == ORIGIN_INDEX_NAME
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing
    assert not frame.index.has_duplicates
    assert frame["horizon"].dtype == np.dtype("int16")
    assert frame["stride"].dtype == np.dtype("int16")
    assert frame["n_quantiles"].dtype == np.dtype("int16")
    for column in ("quantile_levels", "median", "quantiles"):
        assert frame[column].dtype == np.dtype("object")
    assert frame["horizon"].tolist() == [5] * len(frame)
    assert frame["stride"].tolist() == [16] * len(frame)
    assert frame["n_quantiles"].tolist() == [len(LEVELS)] * len(frame)
    levels = frame["quantile_levels"].iloc[0]
    assert levels.dtype == np.dtype("float32")
    assert levels.tolist() == pytest.approx(list(LEVELS))
    assert frame["median"].iloc[0].shape == (5,)
    assert frame["median"].iloc[0].dtype == np.dtype("float32")
    assert frame["quantiles"].iloc[0].shape == (len(LEVELS) * 5,)
    assert frame["quantiles"].iloc[0].dtype == np.dtype("float32")
    assert metadata.n_origins == len(frame) == 6


def test_build_forecast_frame_metadata_describes_the_build() -> None:
    frame, metadata = build_forecast_frame(
        candles(),
        build_config(license="Apache-2.0"),
        created_at=CREATED_AT,
        features=("vol_ratio",),
    )
    assert metadata.schema_version == ARTIFACT_SCHEMA_VERSION
    assert metadata.symbol == "BTC/USDT"
    assert metadata.timeframe == "1h"
    assert metadata.backend == "naive"
    assert metadata.model_id is None
    assert metadata.context_length == 32
    assert metadata.stride == 16
    assert metadata.horizon == 5
    assert metadata.quantile_levels == LEVELS
    assert metadata.features == ("vol_ratio",)
    assert metadata.created_at == CREATED_AT.isoformat()
    assert metadata.package_version == trading_platform.__version__
    assert metadata.data_start == pd.Timestamp("2023-01-01T00:00:00Z")
    assert metadata.data_end == pd.Timestamp("2023-01-05T23:00:00Z")
    assert metadata.license == "Apache-2.0"
    assert metadata.deseasonalized is True
    assert metadata.seasonal_period == 24
    assert metadata.seasonal_window == 48
    assert metadata.n_origins == len(frame)


def test_build_forecast_frame_without_deseasonalisation() -> None:
    _, metadata = build_forecast_frame(
        candles(), build_config(seasonal_window=1), created_at=CREATED_AT
    )
    assert metadata.deseasonalized is False
    assert metadata.seasonal_window == 1


def test_build_forecast_frame_never_mutates_the_candle_frame() -> None:
    frame = candles()
    snapshot = frame.copy(deep=True)
    build_forecast_frame(frame, build_config(), created_at=CREATED_AT)
    pd.testing.assert_frame_equal(frame, snapshot)
    assert frame.index.name == "timestamp"


def test_build_forecast_frame_origins_follow_the_stride() -> None:
    frame = candles()
    artifact, _ = build_forecast_frame(frame, build_config(), created_at=CREATED_AT)
    expected = frame.index[[31, 47, 63, 79, 95, 111]]
    assert list(artifact.index) == list(expected)


def test_build_forecast_frame_reads_a_tz_naive_index_as_utc() -> None:
    frame = candles()
    naive = frame.copy()
    naive.index = naive.index.tz_localize(None)
    aware_artifact, aware_metadata = build_forecast_frame(
        frame, build_config(), created_at=CREATED_AT
    )
    naive_artifact, naive_metadata = build_forecast_frame(
        naive, build_config(), created_at=CREATED_AT
    )
    assert str(naive_artifact.index.tz) == "UTC"
    assert list(naive_artifact.index) == list(aware_artifact.index)
    assert naive_metadata.data_start == aware_metadata.data_start
    assert naive_metadata.data_end == aware_metadata.data_end


def test_build_forecast_frame_is_empty_below_the_context() -> None:
    frame, metadata = build_forecast_frame(
        candles(20), build_config(context_length=32), created_at=CREATED_AT
    )
    assert len(frame) == 0
    assert metadata.n_origins == 0
    assert list(frame.columns) == list(ARTIFACT_COLUMNS)
    assert frame.index.name == ORIGIN_INDEX_NAME


def test_build_forecast_frame_resolves_the_registered_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    seen: dict[str, Any] = {}

    def factory(**options: Any) -> RecordingBackend:
        seen.update(options)
        return backend

    monkeypatch.setitem(BACKENDS, "recording", factory)
    config = build_config(backend="recording", backend_options={"dispersion_window": 7})
    frame, metadata = build_forecast_frame(candles(), config, created_at=CREATED_AT)
    assert metadata.backend == "recording"
    assert seen == {"timeframe": "1h", "quantile_levels": LEVELS, "dispersion_window": 7}
    assert len(frame) == 6
    assert len(backend.calls) == 1


def test_build_forecast_frame_forwards_one_call_with_every_request() -> None:
    backend = RecordingBackend()
    frame = candles()
    artifact, _ = build_forecast_frame(
        frame, build_config(), backend=backend, created_at=CREATED_AT
    )
    assert len(backend.calls) == 1
    requests, horizon = backend.calls[0]
    assert horizon == 5
    assert [request.origin for request in requests] == list(artifact.index)
    assert all(len(request.context) == 32 for request in requests)
    assert all(isinstance(request.context, tuple) for request in requests)


def test_build_forecast_frame_survives_a_backend_mutating_the_request_list() -> None:
    backend = RecordingBackend(mutate_requests=True)
    artifact, metadata = build_forecast_frame(
        candles(), build_config(), backend=backend, created_at=CREATED_AT
    )
    assert metadata.n_origins == 6
    assert len(artifact) == 6
    assert list(artifact.index) == list(candles().index[[31, 47, 63, 79, 95, 111]])


def test_build_forecast_frame_contexts_are_causal() -> None:
    backend = RecordingBackend()
    frame = candles()
    build_forecast_frame(frame, build_config(), backend=backend, created_at=CREATED_AT)
    requests = backend.calls[0][0]
    target = deseasonalize_log_price(frame["close"], timeframe="1h", period=24, window=48)
    for position, request in enumerate(requests):
        origin_row = 31 + position * 16
        expected = target.iloc[origin_row + 1 - 32 : origin_row + 1]
        assert len(expected) == len(request.context) == 32
        assert list(request.context) == pytest.approx(list(expected))
        assert request.context[-1] == pytest.approx(float(target.iloc[origin_row]))


def test_build_forecast_frame_keeps_the_caller_context_intact() -> None:
    frame = candles()
    backend = RecordingBackend(mutate_requests=True)
    build_forecast_frame(frame, build_config(), backend=backend, created_at=CREATED_AT)
    requests = backend.calls[0][0]
    origin_row = 31
    target = deseasonalize_log_price(frame["close"], timeframe="1h", period=24, window=48)
    assert list(requests[0].context) == pytest.approx(
        list(target.iloc[origin_row + 1 - 32 : origin_row + 1])
    )


def test_build_forecast_frame_quantiles_are_row_major() -> None:
    def rows(request: ForecastRequest, horizon: int, levels: tuple[float, ...]) -> np.ndarray:
        # level 0 -> 1.0, level 1 -> 2.0, level 2 -> 3.0 on every step.
        return np.array([[1.0] * horizon, [2.0] * horizon, [3.0] * horizon], dtype="float32")

    backend = RecordingBackend(rows)
    artifact, _ = build_forecast_frame(
        candles(), build_config(), backend=backend, created_at=CREATED_AT
    )
    flat = artifact["quantiles"].iloc[0]
    assert flat.tolist() == [1.0] * 5 + [2.0] * 5 + [3.0] * 5
    assert artifact["median"].iloc[0].tolist() == [2.0] * 5


def test_build_forecast_frame_rejects_a_non_iterable_answer() -> None:
    with pytest.raises(ForecastArtifactError, match="sequence"):
        build_forecast_frame(
            candles(),
            build_config(),
            backend=NonIterableBackend(),  # type: ignore[arg-type]
            created_at=CREATED_AT,
        )


def test_build_forecast_frame_rejects_a_bad_created_at() -> None:
    with pytest.raises(ForecastError, match="created_at"):
        build_forecast_frame(candles(), build_config(), created_at="not a timestamp")  # type: ignore[arg-type]
    with pytest.raises(ForecastError, match="NaT"):
        build_forecast_frame(candles(), build_config(), created_at=pd.NaT)


def test_build_forecast_frame_defaults_created_at_to_now() -> None:
    _, metadata = build_forecast_frame(candles(), build_config())
    created = pd.Timestamp(metadata.created_at)
    assert created.tz is not None
    assert abs(created - pd.Timestamp.now(tz="UTC")) < pd.Timedelta(minutes=5)


def test_build_forecast_frame_reads_a_naive_created_at_as_utc() -> None:
    _, metadata = build_forecast_frame(
        candles(), build_config(), created_at=pd.Timestamp("2024-06-01T00:00:00")
    )
    assert metadata.created_at == CREATED_AT.isoformat()


# ---------------------------------------------------------------------------
# builder: validation
# ---------------------------------------------------------------------------


def test_build_forecast_frame_rejects_a_non_frame() -> None:
    with pytest.raises(ForecastError) as info:
        build_forecast_frame("not a frame", build_config())  # type: ignore[arg-type]
    assert "DataFrame" in str(info.value)


def _reversed(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.iloc[::-1]


def _duplicated(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.concat([frame, frame.iloc[[-1]]])


def _empty(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.iloc[0:0]


def _no_close(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.drop(columns=["close"])


def _text_close(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(close="not a number")


def _nan_close(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(close=np.nan)


def _negative_close(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(close=-1.0)


def _range_index(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.reset_index(drop=True)


BAD_CANDLES: list[tuple[str, Callable[[pd.DataFrame], pd.DataFrame]]] = [
    ("empty", _empty),
    ("no_close", _no_close),
    ("text_close", _text_close),
    ("nan_close", _nan_close),
    ("negative_close", _negative_close),
    ("range_index", _range_index),
    ("reversed", _reversed),
    ("duplicated", _duplicated),
]


@pytest.mark.parametrize(("case", "mutate"), BAD_CANDLES, ids=[case for case, _ in BAD_CANDLES])
def test_build_forecast_frame_rejects_invalid_candles(
    case: str, mutate: Callable[[pd.DataFrame], pd.DataFrame]
) -> None:
    with pytest.raises(ForecastError) as info:
        build_forecast_frame(mutate(candles()), build_config())
    assert info.value.issues, f"{case} produced no issue list"
    assert str(info.value)


BAD_CONFIGS: list[tuple[str, dict[str, Any]]] = [
    ("context_below_min", {"context_length": 16}),
    ("context_below_two", {"context_length": 1, "min_context": 1}),
    ("horizon_zero", {"horizon": 0}),
    ("stride_zero", {"reforecast_every": 0}),
    ("period_zero", {"seasonal_period": 0}),
    ("window_negative", {"seasonal_window": -1}),
    ("levels_unsorted", {"quantile_levels": (0.5, 0.4)}),
    ("levels_outside", {"quantile_levels": (0.0, 0.5, 1.0)}),
    ("levels_without_median", {"quantile_levels": (0.1, 0.9)}),
    ("levels_empty", {"quantile_levels": ()}),
    ("levels_not_numeric", {"quantile_levels": ("low", "median", "high")}),
    ("unknown_timeframe", {"timeframe": "7h"}),
    ("empty_backend", {"backend": ""}),
    ("empty_symbol", {"symbol": ""}),
    ("horizon_above_int16", {"horizon": 40_000}),
    ("context_not_an_int", {"context_length": "32"}),
    ("model_id_not_a_string", {"model_id": 5}),
    ("license_not_a_string", {"license": 5}),
    ("backend_options_not_a_mapping", {"backend_options": [("trend_window", 3)]}),
]


@pytest.mark.parametrize(("case", "overrides"), BAD_CONFIGS, ids=[case for case, _ in BAD_CONFIGS])
def test_build_forecast_frame_rejects_invalid_config(case: str, overrides: dict[str, Any]) -> None:
    with pytest.raises(ForecastError) as info:
        build_forecast_frame(candles(), build_config(**overrides))
    assert info.value.issues, f"{case} produced no issue list"


def test_build_forecast_frame_rejects_a_wrong_request_count() -> None:
    backend = BrokenBackend(lambda request, horizon: trajectory_at(request, horizon))
    with pytest.raises(ForecastArtifactError, match="trajectory"):
        build_forecast_frame(
            candles(),
            build_config(),
            backend=_ShortBackend(backend),
            created_at=CREATED_AT,
        )


class _ShortBackend:
    """Backend dropping every answer but the first (wrong trajectory count)."""

    name = "short"

    def __init__(self, inner: BrokenBackend) -> None:
        self._inner = inner

    def is_available(self) -> bool:
        """Return ``True``: this stub needs nothing."""
        return True

    def predict(
        self, requests: Sequence[ForecastRequest], *, horizon: int
    ) -> list[ForecastTrajectory]:
        """Return fewer trajectories than requests."""
        return self._inner.predict(requests, horizon=horizon)[:1]


@pytest.mark.parametrize(
    ("case", "changes"),
    [
        ("wrong_horizon", {"horizon": 6, "quantiles": np.zeros((3, 6), dtype="float32")}),
        (
            "wrong_levels",
            {"quantile_levels": (0.1, 0.5), "quantiles": np.zeros((2, 5), dtype="float32")},
        ),
        ("wrong_origin", {"origin": pd.Timestamp("2000-01-01T00:00:00Z")}),
    ],
)
def test_build_forecast_frame_rejects_a_misleading_trajectory(
    case: str, changes: dict[str, Any]
) -> None:
    backend = BrokenBackend(lambda request, horizon: altered_trajectory(request, changes))
    with pytest.raises(ForecastArtifactError):
        build_forecast_frame(candles(), build_config(), backend=backend, created_at=CREATED_AT)


def test_build_forecast_frame_rejects_a_foreign_backend_answer() -> None:
    backend = BrokenBackend(lambda request, horizon: "not a trajectory")
    with pytest.raises(ForecastArtifactError, match="ForecastTrajectory"):
        build_forecast_frame(candles(), build_config(), backend=backend, created_at=CREATED_AT)


# ---------------------------------------------------------------------------
# writer and parquet round-trip
# ---------------------------------------------------------------------------


def test_parquet_round_trip_pins_schema_dtypes_and_index(tmp_path: Path) -> None:
    path, metadata = build_artifact(
        candles(), tmp_path / "forecast.parquet", features=("vol_ratio",)
    )
    raw = pd.read_parquet(path)
    assert list(raw.columns) == list(ARTIFACT_COLUMNS)
    assert raw.index.name == ORIGIN_INDEX_NAME
    assert str(raw.index.tz) == "UTC"
    assert raw["horizon"].dtype == np.dtype("int16")
    assert raw["stride"].dtype == np.dtype("int16")
    assert raw["n_quantiles"].dtype == np.dtype("int16")
    for column in ("quantile_levels", "median", "quantiles"):
        values = raw[column].to_numpy()
        assert all(value.dtype == np.dtype("float32") for value in values)
    assert raw["median"].iloc[0].shape == (5,)
    assert raw["quantiles"].iloc[0].shape == (15,)

    store = ForecastStore.load(path)
    origin = store.origins()[0]
    trajectory = store.trajectory(origin)
    assert trajectory.origin == origin
    assert trajectory.horizon == 5
    assert trajectory.quantile_levels == LEVELS
    assert trajectory.quantiles.dtype == np.dtype("float32")
    assert trajectory.quantiles.shape == (len(LEVELS), 5)
    assert np.array_equal(trajectory.median, raw["median"].iloc[0])
    assert np.array_equal(trajectory.quantiles.reshape(-1), raw["quantiles"].iloc[0])
    assert store.metadata == metadata
    assert store.timeframe == "1h"
    assert store.horizon == 5
    assert store.stride == 16
    assert store.quantile_levels == LEVELS
    assert store.path == path


def test_parquet_round_trip_keeps_row_major_quantiles(tmp_path: Path) -> None:
    def rows(request: ForecastRequest, horizon: int, levels: tuple[float, ...]) -> np.ndarray:
        return np.array([[1.0] * horizon, [2.0] * horizon, [3.0] * horizon], dtype="float32")

    build_forecast_artifact(
        candles(),
        build_config(),
        tmp_path / "rows.parquet",
        backend=RecordingBackend(rows),
        created_at=CREATED_AT,
    )
    store = ForecastStore.load(tmp_path / "rows.parquet")
    trajectory = store.trajectory(store.origins()[0])
    assert trajectory.quantile(0.1).tolist() == [1.0] * 5
    assert trajectory.median.tolist() == [2.0] * 5
    assert trajectory.quantile(0.9).tolist() == [3.0] * 5


def test_write_creates_parent_directories_and_only_the_two_files(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "forecast.parquet"
    path, _ = build_artifact(candles(), target)
    assert path == target
    assert sorted(item.name for item in target.parent.iterdir()) == [
        "forecast.parquet",
        "forecast.parquet.meta.json",
    ]


def test_write_is_byte_identical_for_the_same_input(tmp_path: Path) -> None:
    frame = candles()
    first, _ = build_artifact(frame, tmp_path / "one.parquet")
    second, _ = build_artifact(frame, tmp_path / "two.parquet")
    assert first.read_bytes() == second.read_bytes()
    assert metadata_path(first).read_bytes() == metadata_path(second).read_bytes()


def test_write_rejects_an_incoherent_frame(tmp_path: Path) -> None:
    def rows(request: ForecastRequest, horizon: int, levels: tuple[float, ...]) -> np.ndarray:
        return np.array([[1.0] * horizon, [2.0] * horizon, [3.0] * horizon], dtype="float32")

    path, metadata = build_forecast_artifact(
        candles(),
        build_config(),
        tmp_path / "forecast.parquet",
        backend=RecordingBackend(rows),
        created_at=CREATED_AT,
    )
    frame = pd.read_parquet(path)
    with pytest.raises(ForecastArtifactError, match="origin"):
        write_forecast_artifact(frame.iloc[:1], metadata, tmp_path / "other.parquet")
    with pytest.raises(ForecastArtifactError, match="median"):
        write_forecast_artifact(
            frame.drop(columns=["median"]), metadata, tmp_path / "other.parquet"
        )
    with pytest.raises(ForecastArtifactError, match="disagrees"):
        write_forecast_artifact(
            frame.assign(median=frame["median"].map(lambda row: np.zeros(5, dtype="float32"))),
            metadata,
            tmp_path / "other.parquet",
        )


def test_write_rejects_a_non_frame(tmp_path: Path) -> None:
    _, metadata = build_artifact(candles(), tmp_path / "forecast.parquet")
    with pytest.raises(ForecastArtifactError, match="DataFrame"):
        write_forecast_artifact("nope", metadata, tmp_path / "other.parquet")  # type: ignore[arg-type]


def test_write_reports_an_unwritable_destination(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    frame, metadata = build_forecast_frame(candles(), build_config(), created_at=CREATED_AT)
    with pytest.raises(ForecastArtifactError, match="cannot write"):
        write_forecast_artifact(frame, metadata, blocker / "forecast.parquet")


def test_build_forecast_artifact_returns_the_path_and_the_metadata(tmp_path: Path) -> None:
    path, metadata = build_artifact(candles(), tmp_path / "forecast.parquet")
    assert path.is_file()
    assert metadata_path(path).is_file()
    assert metadata.n_origins == 6
    assert read_payload(path)["symbol"] == "BTC/USDT"


# ---------------------------------------------------------------------------
# no look-ahead
# ---------------------------------------------------------------------------


def test_truncated_build_is_identical_on_the_shared_origins(tmp_path: Path) -> None:
    frame = candles(160)
    config = build_config(context_length=32, horizon=4, reforecast_every=32)
    full_path, _ = build_forecast_artifact(
        frame, config, tmp_path / "full.parquet", created_at=CREATED_AT
    )
    truncated_path, truncated_metadata = build_forecast_artifact(
        frame.iloc[:100], config, tmp_path / "truncated.parquet", created_at=CREATED_AT
    )

    full_frame = pd.read_parquet(full_path)
    truncated_frame = pd.read_parquet(truncated_path)
    assert list(truncated_frame.index) == list(frame.index[[31, 63, 95]])
    assert set(truncated_frame.index).issubset(set(full_frame.index))
    assert_rows_equal(truncated_frame, full_frame.loc[truncated_frame.index])

    payload = read_payload(full_path)
    truncated_payload = read_payload(truncated_path)
    for key in ("created_at", "n_origins", "data_end"):
        payload.pop(key)
        truncated_payload.pop(key)
    assert payload == truncated_payload
    assert truncated_metadata.n_origins == 3

    full_store = ForecastStore.load(full_path)
    truncated_store = ForecastStore.load(truncated_path)
    for origin in truncated_store.origins():
        assert np.array_equal(
            truncated_store.trajectory(origin).quantiles,
            full_store.trajectory(origin).quantiles,
        )


# ---------------------------------------------------------------------------
# store: lookup and cache
# ---------------------------------------------------------------------------


def test_store_origins_are_a_fresh_utc_index(tmp_path: Path) -> None:
    path, _ = build_artifact(candles(), tmp_path / "forecast.parquet")
    store = ForecastStore.load(path)
    first = store.origins()
    second = store.origins()
    assert first is not second
    assert isinstance(first, pd.DatetimeIndex)
    assert first.name == ORIGIN_INDEX_NAME
    assert str(first.tz) == "UTC"
    assert list(first) == list(pd.read_parquet(path).index)


def test_store_origin_for_returns_the_active_origin(tmp_path: Path) -> None:
    frame = candles()
    path, store_output = build_artifact(frame, tmp_path / "forecast.parquet")
    store = ForecastStore.load(path)
    assert store.origin_for(frame.index[31]) == frame.index[31]
    assert store.origin_for(frame.index[40]) == frame.index[31]
    assert store.origin_for(frame.index[46]) == frame.index[31]
    assert store.origin_for(frame.index[47]) == frame.index[47]
    assert store.origin_for(frame.index[119]) == frame.index[111]
    assert store.origin_for("2023-01-05T00:00:00Z") == frame.index[95]
    assert store.origin_for("2023-01-02T08:00:00") == frame.index[31]
    assert store.origin_for(pd.Timestamp("2023-01-02T08:00:00")) == store.origin_for(
        pd.Timestamp("2023-01-02T08:00:00Z")
    )
    assert store.origin_for("2023-01-01T00:00:00") is None
    assert store.origin_for(frame.index[30]) is None
    assert store.origin_for(pd.Timestamp("2000-01-01T00:00:00Z")) is None
    assert store.origin_for("not a timestamp") is None
    assert store.origin_for(object()) is None
    assert store.origin_for(pd.NaT) is None
    assert store.origin_for(None) is None
    assert store_output.n_origins == 6


def test_store_trajectory_is_decoded_once_and_cached(tmp_path: Path) -> None:
    path, _ = build_artifact(candles(), tmp_path / "forecast.parquet")
    store = ForecastStore.load(path)
    first_origin, second_origin = store.origins()[0], store.origins()[1]
    first = store.trajectory(first_origin)
    assert first is store.trajectory(first_origin)

    other = ForecastStore.load(path).trajectory(first_origin)
    assert other is not first
    assert other.origin == first.origin
    assert other.timeframe == first.timeframe
    assert other.horizon == first.horizon
    assert other.quantile_levels == first.quantile_levels
    assert np.array_equal(other.quantiles, first.quantiles)
    assert store.trajectory(second_origin) is not first
    assert store.trajectory(second_origin) is store.trajectory(second_origin)


def test_store_trajectory_rejects_an_unknown_origin(tmp_path: Path) -> None:
    frame = candles()
    path, _ = build_artifact(frame, tmp_path / "forecast.parquet")
    store = ForecastStore.load(path)
    for bad in (frame.index[0], frame.index[32], "not a timestamp", pd.NaT, None):
        with pytest.raises(ForecastArtifactError):
            store.trajectory(bad)


def test_store_can_be_built_in_memory(tmp_path: Path) -> None:
    frame, metadata = build_forecast_frame(candles(), build_config(), created_at=CREATED_AT)
    store = ForecastStore(frame, metadata)
    assert store.path is None
    assert len(store.origins()) == 6
    assert store.trajectory(store.origins()[0]).horizon == 5


# ---------------------------------------------------------------------------
# store: corrupt and incompatible artifacts
# ---------------------------------------------------------------------------


def test_load_reports_a_missing_parquet(tmp_path: Path) -> None:
    path, _ = build_artifact(candles(), tmp_path / "forecast.parquet")
    path.unlink()
    with pytest.raises(ForecastArtifactError, match="not found"):
        ForecastStore.load(path)


def test_load_reports_a_missing_sidecar(tmp_path: Path) -> None:
    path, _ = build_artifact(candles(), tmp_path / "forecast.parquet")
    metadata_path(path).unlink()
    with pytest.raises(ForecastArtifactError, match="metadata not found"):
        ForecastStore.load(path)


def test_load_reports_corrupt_json(tmp_path: Path) -> None:
    path, _ = build_artifact(candles(), tmp_path / "forecast.parquet")
    metadata_path(path).write_text("{not json", encoding="utf-8")
    with pytest.raises(ForecastArtifactError, match="metadata"):
        ForecastStore.load(path)


def test_load_reports_a_metadata_that_is_not_an_object(tmp_path: Path) -> None:
    path, _ = build_artifact(candles(), tmp_path / "forecast.parquet")
    metadata_path(path).write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ForecastArtifactError):
        ForecastStore.load(path)


def test_load_reports_an_unsupported_schema_version(tmp_path: Path) -> None:
    path, _ = build_artifact(candles(), tmp_path / "forecast.parquet")
    payload = read_payload(path)
    payload["schema_version"] = ARTIFACT_SCHEMA_VERSION + 1
    metadata_path(path).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ForecastArtifactError, match="schema version"):
        ForecastStore.load(path)


def test_load_reports_an_unreadable_parquet(tmp_path: Path) -> None:
    path, _ = build_artifact(candles(), tmp_path / "forecast.parquet")
    path.write_bytes(b"not a parquet file")
    with pytest.raises(ForecastArtifactError, match="cannot read"):
        ForecastStore.load(path)


@pytest.fixture
def artifact(tmp_path: Path) -> tuple[Path, pd.DataFrame, ArtifactMetadata]:
    """A valid artifact plus its raw frame and metadata (for corruption tests)."""
    path, metadata = build_artifact(candles(), tmp_path / "forecast.parquet")
    return path, pd.read_parquet(path), metadata


def test_load_reports_a_missing_column(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(frame.drop(columns=["median"]), metadata, tmp_path / "broken.parquet")
    with pytest.raises(ForecastArtifactError) as info:
        ForecastStore.load(broken)
    assert "median" in str(info.value)


def test_load_reports_an_unexpected_column(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(frame.assign(extra=1), metadata, tmp_path / "broken.parquet")
    with pytest.raises(ForecastArtifactError) as info:
        ForecastStore.load(broken)
    assert "extra" in str(info.value)


def test_load_reports_a_non_datetime_index(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(frame.reset_index(drop=True), metadata, tmp_path / "broken.parquet")
    with pytest.raises(ForecastArtifactError, match="DatetimeIndex"):
        ForecastStore.load(broken)


def test_load_reports_an_unnamed_index(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(frame.rename_axis(None), metadata, tmp_path / "broken.parquet")
    with pytest.raises(ForecastArtifactError, match="origin"):
        ForecastStore.load(broken)


def test_load_reports_unsorted_origins(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(frame.iloc[::-1], metadata, tmp_path / "broken.parquet")
    with pytest.raises(ForecastArtifactError, match="sorted"):
        ForecastStore.load(broken)


def test_load_reports_duplicated_origins(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    duplicated = pd.concat([frame, frame.iloc[[0]]])
    broken = write_raw(
        duplicated, replace(metadata, n_origins=len(duplicated)), tmp_path / "broken.parquet"
    )
    with pytest.raises(ForecastArtifactError, match="unique"):
        ForecastStore.load(broken)


def test_load_reports_an_inconsistent_row_count(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(
        frame, replace(metadata, n_origins=len(frame) + 1), tmp_path / "broken.parquet"
    )
    with pytest.raises(ForecastArtifactError, match="origin"):
        ForecastStore.load(broken)


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("n_quantiles", lambda frame: frame.assign(n_quantiles=np.int16(7).item())),
        ("horizon_not_an_int", lambda frame: frame.assign(horizon=1.5)),
        (
            "median_length",
            lambda frame: frame.assign(median=frame["median"].map(lambda row: row[:-1])),
        ),
        (
            "median_not_numeric",
            lambda frame: frame.assign(median=frame["median"].map(lambda row: "text")),
        ),
        (
            "quantiles_length",
            lambda frame: frame.assign(quantiles=frame["quantiles"].map(lambda row: row[:-1])),
        ),
        (
            "levels",
            lambda frame: frame.assign(
                quantile_levels=frame["quantile_levels"].map(
                    lambda row: np.array([0.2, 0.5, 0.9], dtype="float32")
                )
            ),
        ),
        (
            "non_finite",
            lambda frame: frame.assign(
                median=frame["median"].map(lambda row: np.full(5, np.nan, dtype="float32"))
            ),
        ),
        (
            "median_disagrees",
            lambda frame: frame.assign(
                median=frame["median"].map(lambda row: row + np.float32(1.0))
            ),
        ),
    ],
)
def test_load_reports_an_inconsistent_row(
    tmp_path: Path,
    artifact: tuple[Path, pd.DataFrame, ArtifactMetadata],
    case: str,
    mutate: Callable[[pd.DataFrame], pd.DataFrame],
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(mutate(frame), metadata, tmp_path / "broken.parquet")
    with pytest.raises(ForecastArtifactError) as info:
        ForecastStore.load(broken)
    assert info.value.issues, f"{case} produced no issue list"


def test_load_reports_an_inconsistent_horizon(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(frame, replace(metadata, horizon=99), tmp_path / "broken.parquet")
    with pytest.raises(ForecastArtifactError) as info:
        ForecastStore.load(broken)
    assert "horizon" in str(info.value)


def test_load_reports_an_invalid_stride(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(frame, replace(metadata, stride=0), tmp_path / "broken.parquet")
    with pytest.raises(ForecastArtifactError, match="stride"):
        ForecastStore.load(broken)


def test_load_reports_invalid_levels(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(
        frame, replace(metadata, quantile_levels=(0.9, 0.5)), tmp_path / "broken.parquet"
    )
    with pytest.raises(ForecastArtifactError, match="increasing"):
        ForecastStore.load(broken)


def test_load_reports_a_metadata_above_the_int16_limit(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    broken = write_raw(frame, replace(metadata, horizon=40_000), tmp_path / "broken.parquet")
    with pytest.raises(ForecastArtifactError, match="int16"):
        ForecastStore.load(broken)


def test_a_duplicated_column_name_is_rejected_in_memory(
    artifact: tuple[Path, pd.DataFrame, ArtifactMetadata],
) -> None:
    # Parquet cannot even store a duplicated column name, so this guard is
    # reachable only when a store is built from an in-memory frame.
    _, frame, metadata = artifact
    duplicated = pd.concat([frame, frame[["median"]]], axis=1)
    with pytest.raises(ForecastArtifactError, match="repeated"):
        ForecastStore(duplicated, metadata)


def test_float64_rows_are_normalised_to_float32(
    tmp_path: Path, artifact: tuple[Path, pd.DataFrame, ArtifactMetadata]
) -> None:
    _, frame, metadata = artifact
    as_float64 = frame.assign(
        quantile_levels=frame["quantile_levels"].map(lambda row: row.astype("float64")),
        median=frame["median"].map(lambda row: row.astype("float64")),
        quantiles=frame["quantiles"].map(lambda row: row.astype("float64")),
    )
    broken = write_raw(as_float64, metadata, tmp_path / "wide.parquet")
    store = ForecastStore.load(broken)
    trajectory = store.trajectory(store.origins()[0])
    assert trajectory.quantiles.dtype == np.dtype("float32")
    assert trajectory.median.dtype == np.dtype("float32")
    assert np.array_equal(
        trajectory.quantiles[1], np.asarray(frame["median"].iloc[0], dtype="float32")
    )


# ---------------------------------------------------------------------------
# empty artifact and uncovered tail
# ---------------------------------------------------------------------------


def test_empty_artifact_round_trips(tmp_path: Path) -> None:
    frame = candles(20)
    path, metadata = build_artifact(frame, tmp_path / "empty.parquet", context_length=32)
    assert metadata.n_origins == 0
    store = ForecastStore.load(path)
    origins = store.origins()
    assert isinstance(origins, pd.DatetimeIndex)
    assert len(origins) == 0
    assert origins.name == ORIGIN_INDEX_NAME
    assert str(origins.tz) == "UTC"
    assert store.origin_for(frame.index[-1]) is None
    with pytest.raises(ForecastArtifactError):
        store.trajectory(frame.index[-1])
    assert store.metadata.n_origins == 0


def test_store_serves_an_uncovered_tail_and_head(tmp_path: Path) -> None:
    frame = candles()
    path, _ = build_artifact(frame, tmp_path / "forecast.parquet")
    store = ForecastStore.load(path)
    last = store.origins()[-1]
    assert store.origin_for(frame.index[-1]) == last
    assert store.trajectory(last).horizon == 5
    with pytest.raises(ForecastArtifactError):
        store.trajectory(frame.index[-1])


def test_store_is_stable_across_a_reload(tmp_path: Path) -> None:
    path, _ = build_artifact(candles(), tmp_path / "forecast.parquet")
    first = ForecastStore.load(path)
    second = ForecastStore.load(path)
    assert list(first.origins()) == list(second.origins())
    assert first.metadata == second.metadata
    origin = first.origins()[2]
    assert np.array_equal(first.trajectory(origin).quantiles, second.trajectory(origin).quantiles)
    assert not math.isnan(float(first.trajectory(origin).median[0]))
