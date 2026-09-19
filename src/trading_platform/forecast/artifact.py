"""Versioned parquet artifacts: the offline bridge between a model and a strategy.

The house ``Strategy.prepare``/``signals`` contract is pure, deterministic and
I/O-free, and the whole test suite must stay green with only the ``.[dev]`` extra
installed.  A forecasting model therefore never runs inside a strategy: it runs
**offline**, in the ``trading forecast-build`` command, and writes one row per
forecast origin into a parquet artifact that the strategy consumes as a
deterministic external input.

This module is the producer *and* the reader of that artifact:

* :func:`build_forecast_frame` turns a candle frame into the artifact frame and
  its :class:`ArtifactMetadata`;
* :func:`write_forecast_artifact` writes the parquet plus its JSON sidecar
  atomically, and :func:`build_forecast_artifact` does both;
* :class:`ForecastStore` loads an artifact and answers the two questions a
  strategy asks: *which origin is active at this candle?* and *what did that
  origin predict?*.

The no-look-ahead property
--------------------------
The artifact is reproducible from a candle file, and it cannot leak: the target
is the causally de-seasonalised log price of
:mod:`trading_platform.forecast.series`, every origin uses only the candles up to
and including itself, and the stored context of an origin is exactly
``target[origin - context_length + 1 : origin + 1]``.  Building from a truncated
candle frame therefore yields the very same rows on the origins both frames
share — the property the test suite pins.

Artifact schema (parquet, schema version :data:`ARTIFACT_SCHEMA_VERSION`)
-------------------------------------------------------------------------
* index: ``DatetimeIndex`` named ``origin``, tz-aware UTC, ascending, unique —
  the timestamp of the **last context candle**;
* columns, exactly :data:`ARTIFACT_COLUMNS`:

  ================  =========  ==================================================
  column            dtype      meaning
  ================  =========  ==================================================
  ``horizon``       int16      number of forecast steps stored per row
  ``stride``        int16      candles between two stored origins
  ``n_quantiles``   int16      number of stored quantile levels
  ``quantile_levels`` list<f32> the stored levels, ascending
  ``median``        list<f32>  the median path, ``horizon`` values, log offsets
  ``quantiles``     list<f32>  ``n_quantiles * horizon`` values, **row-major**
  ================  =========  ==================================================

  A path value is a **log offset from the origin close**: ``0.0`` means "the
  close of ``origin`` is held".  The sidecar ``<artifact>.meta.json`` holds the
  matching :class:`ArtifactMetadata` as one flat JSON object.

Dependency direction and cost
-----------------------------
This module needs the standard library, ``numpy``, ``pandas`` and
:mod:`trading_platform.core` only.  It never imports ``torch``/``timesfm``: the
backend is resolved through :func:`trading_platform.forecast.registry.get_backend`,
which imports a heavy backend lazily, or injected as a plain ``numpy`` backend
(``naive``/``seasonal``) so the whole pipeline stays runnable offline.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from trading_platform import __version__
from trading_platform.core.constants import UTC
from trading_platform.core.errors import ForecastArtifactError, ForecastError
from trading_platform.forecast.registry import ForecastBackend, get_backend
from trading_platform.forecast.series import deseasonalize_log_price, timeframe_delta
from trading_platform.forecast.types import (
    DEFAULT_QUANTILE_LEVELS,
    MEDIAN_LEVEL,
    ForecastRequest,
    ForecastTrajectory,
)

__all__ = [
    "ARTIFACT_COLUMNS",
    "ARTIFACT_SCHEMA_VERSION",
    "ORIGIN_INDEX_NAME",
    "ArtifactMetadata",
    "ForecastBuildConfig",
    "ForecastStore",
    "build_forecast_artifact",
    "build_forecast_frame",
    "metadata_path",
    "write_forecast_artifact",
]

#: Version of the artifact layout below.  A reader refuses any other value.
ARTIFACT_SCHEMA_VERSION: int = 1

#: Name of the artifact index, which carries the forecast origins.
ORIGIN_INDEX_NAME: str = "origin"

#: Columns of an artifact frame, in the order they are written.
ARTIFACT_COLUMNS: tuple[str, ...] = (
    "horizon",
    "stride",
    "n_quantiles",
    "quantile_levels",
    "median",
    "quantiles",
)

#: Largest value an ``int16`` artifact column can hold.
_INT16_MAX: int = int(np.iinfo(np.int16).max)

#: Problems reported in one error, so a fully corrupt artifact stays readable.
_MAX_REPORTED_ISSUES: int = 10

#: Suffix appended to the artifact path to locate the sidecar metadata file.
_METADATA_SUFFIX: str = ".meta.json"

#: Sentinel distinguishing "key absent" from "key present but null".
_MISSING: Any = object()


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactMetadata:
    """Everything a reader must know about one forecast artifact.

    The instance is written verbatim (through :meth:`to_dict`) into the sidecar
    ``<artifact>.meta.json``, so it only holds JSON-safe values:
    :attr:`created_at` is an ISO-8601 string and the two data bounds are exposed
    as :class:`pandas.Timestamp` objects that :meth:`to_dict` renders as ISO-8601
    strings again.

    Attributes
    ----------
    schema_version:
        Layout version of the artifact, see :data:`ARTIFACT_SCHEMA_VERSION`.
    symbol:
        Traded symbol the artifact was built for (a label).
    timeframe:
        Candle duration, for example ``"1h"``.
    backend:
        Registry name of the forecasting backend.
    model_id:
        Checkpoint identifier for a model-backed backend, ``None`` otherwise.
    context_length:
        Number of candles handed to the backend at every origin.
    stride:
        Candles between two stored origins (``reforecast_every``).
    horizon:
        Number of forecast steps stored per origin.
    quantile_levels:
        Ascending levels stored in the artifact, the median ``0.5`` included.
    features:
        Diagnostic feature names the consumer expects, as recorded by the caller
        (empty when the caller recorded none).
    created_at:
        ISO-8601 creation timestamp of the artifact.
    package_version:
        Version of ``trading_platform`` that built the artifact.
    data_start:
        First candle of the input frame.
    data_end:
        Last candle of the input frame.
    n_origins:
        Number of rows of the artifact.
    license:
        Licence note of the checkpoint, surfaced to the operator.
    deseasonalized:
        ``True`` when the target was de-seasonalised before forecasting.
    seasonal_period:
        Number of phases of the seasonal cycle.
    seasonal_window:
        Trailing window, in candles, used to estimate the seasonal component.

    Examples
    --------
    >>> payload = {
    ...     "schema_version": 1,
    ...     "symbol": "BTC/USDT",
    ...     "timeframe": "1h",
    ...     "backend": "naive",
    ...     "model_id": None,
    ...     "context_length": 512,
    ...     "stride": 24,
    ...     "horizon": 24,
    ...     "quantile_levels": [0.1, 0.5, 0.9],
    ...     "features": [],
    ...     "created_at": "2024-01-01T00:00:00+00:00",
    ...     "package_version": "0.1.0",
    ...     "data_start": "2023-01-01T00:00:00+00:00",
    ...     "data_end": "2024-01-01T00:00:00+00:00",
    ...     "n_origins": 10,
    ...     "license": "Apache-2.0",
    ...     "deseasonalized": True,
    ...     "seasonal_period": 24,
    ...     "seasonal_window": 168,
    ... }
    >>> ArtifactMetadata.from_dict(payload).backend
    'naive'
    """

    schema_version: int
    symbol: str
    timeframe: str
    backend: str
    model_id: str | None
    context_length: int
    stride: int
    horizon: int
    quantile_levels: tuple[float, ...]
    features: tuple[str, ...]
    created_at: str
    package_version: str
    data_start: pd.Timestamp
    data_end: pd.Timestamp
    n_origins: int
    license: str
    deseasonalized: bool
    seasonal_period: int
    seasonal_window: int

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe, key-sorted view written into the sidecar."""
        return {
            "backend": str(self.backend),
            "context_length": int(self.context_length),
            "created_at": str(self.created_at),
            "data_end": pd.Timestamp(self.data_end).isoformat(),
            "data_start": pd.Timestamp(self.data_start).isoformat(),
            "deseasonalized": bool(self.deseasonalized),
            "features": [str(feature) for feature in self.features],
            "horizon": int(self.horizon),
            "license": str(self.license),
            "model_id": None if self.model_id is None else str(self.model_id),
            "n_origins": int(self.n_origins),
            "package_version": str(self.package_version),
            "quantile_levels": [float(level) for level in self.quantile_levels],
            "schema_version": int(self.schema_version),
            "seasonal_period": int(self.seasonal_period),
            "seasonal_window": int(self.seasonal_window),
            "stride": int(self.stride),
            "symbol": str(self.symbol),
            "timeframe": str(self.timeframe),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ArtifactMetadata:
        """Rebuild metadata from a sidecar payload.

        Parameters
        ----------
        payload:
            The JSON object read from ``<artifact>.meta.json``.

        Raises
        ------
        ForecastArtifactError
            If the payload is not a mapping, or if a key is missing or wrongly
            typed.  Every offending key is listed in :attr:`issues`.
        """
        if not isinstance(payload, Mapping):
            raise ForecastArtifactError(
                f"forecast artifact metadata must be a JSON object, got {type(payload).__name__}"
            )
        issues: list[str] = []
        values: dict[str, Any] = {
            "schema_version": _payload_int(payload, "schema_version", issues),
            "symbol": _payload_text(payload, "symbol", issues),
            "timeframe": _payload_text(payload, "timeframe", issues),
            "backend": _payload_text(payload, "backend", issues),
            "model_id": _payload_optional_text(payload, "model_id", issues),
            "context_length": _payload_int(payload, "context_length", issues),
            "stride": _payload_int(payload, "stride", issues),
            "horizon": _payload_int(payload, "horizon", issues),
            "quantile_levels": _payload_float_tuple(payload, "quantile_levels", issues),
            "features": _payload_text_tuple(payload, "features", issues),
            "created_at": _payload_text(payload, "created_at", issues),
            "package_version": _payload_text(payload, "package_version", issues),
            "data_start": _payload_timestamp(payload, "data_start", issues),
            "data_end": _payload_timestamp(payload, "data_end", issues),
            "n_origins": _payload_int(payload, "n_origins", issues),
            "license": _payload_text(payload, "license", issues),
            "deseasonalized": _payload_bool(payload, "deseasonalized", issues),
            "seasonal_period": _payload_int(payload, "seasonal_period", issues),
            "seasonal_window": _payload_int(payload, "seasonal_window", issues),
        }
        if issues:
            raise ForecastArtifactError(
                "invalid forecast artifact metadata", _limited_issues(issues)
            )
        return cls(**values)


def metadata_path(path: str | Path) -> Path:
    """Return the sidecar path of an artifact (``a.parquet`` → ``a.parquet.meta.json``)."""
    return Path(f"{Path(path)}{_METADATA_SUFFIX}")


# ---------------------------------------------------------------------------
# build configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForecastBuildConfig:
    """Inputs of one offline artifact build.

    Attributes
    ----------
    symbol:
        Symbol label recorded in the metadata (never used to load data here).
    timeframe:
        Candle duration of the input frame, a key of
        :data:`trading_platform.forecast.series.TIMEFRAME_SECONDS`.
    backend:
        Registry name of the backend, resolved lazily by
        :func:`trading_platform.forecast.registry.get_backend`.
    context_length:
        Candles handed to the backend at every origin (``>= min_context``).
    horizon:
        Forecast steps stored per origin.
    reforecast_every:
        Candles between two stored origins (the re-anchoring stride).
    quantile_levels:
        Levels to store, ascending, inside ``(0, 1)``, median included.
    seasonal_period:
        Number of phases of the daily/seasonal cycle, for example ``24``.
    seasonal_window:
        Trailing window of the seasonal estimate; ``<= 1`` disables the
        de-seasonalisation (``0`` and ``1`` behave the same, ``0`` marks it
        explicitly).
    min_context:
        Hard floor of :attr:`context_length` (the model needs at least 32
        points, so the floor defaults to 32).
    model_id:
        Checkpoint identifier recorded in the metadata, ``None`` for the pure
        ``numpy`` backends.
    backend_options:
        Extra keyword arguments forwarded to the backend factory.
    license:
        Licence note of the checkpoint, recorded in the metadata.

    Examples
    --------
    >>> ForecastBuildConfig(symbol="BTC/USDT", horizon=12).horizon
    12
    """

    symbol: str = "UNKNOWN/USDT"
    timeframe: str = "1h"
    backend: str = "naive"
    context_length: int = 512
    horizon: int = 24
    reforecast_every: int = 24
    quantile_levels: tuple[float, ...] = DEFAULT_QUANTILE_LEVELS
    seasonal_period: int = 24
    seasonal_window: int = 168
    min_context: int = 32
    model_id: str | None = None
    backend_options: Mapping[str, Any] = field(default_factory=dict)
    license: str = ""


# ---------------------------------------------------------------------------
# public builder API
# ---------------------------------------------------------------------------


def build_forecast_frame(
    candles: pd.DataFrame,
    config: ForecastBuildConfig,
    *,
    backend: ForecastBackend | None = None,
    created_at: pd.Timestamp | None = None,
    features: Sequence[str] = (),
) -> tuple[pd.DataFrame, ArtifactMetadata]:
    """Forecast every origin of ``candles`` and return the artifact frame.

    Origins are the row positions ``range(context_length - 1, len(candles),
    reforecast_every)``: the first origin is the candle that completes a full
    context, and every origin uses only the candles up to and including itself.
    All requests are handed to the backend in **one** :meth:`predict` call (the
    backend owns the chunking), which is what keeps a batched model call cheap.

    Parameters
    ----------
    candles:
        OHLCV frame (or any frame with a ``close`` column) indexed by candle
        timestamp.  A tz-naive index is read as UTC, a tz-aware one is converted
        to UTC; the frame itself is never modified.
    config:
        Build configuration, see :class:`ForecastBuildConfig`.
    backend:
        Backend instance to use.  When ``None`` (the default) the registry
        builds ``config.backend`` with ``config.backend_options``.
    created_at:
        Creation timestamp recorded in the metadata; defaults to *now* in UTC.
        A tz-naive value is read as UTC.
    features:
        Diagnostic feature names to record in the metadata (may be empty).

    Returns
    -------
    tuple[pandas.DataFrame, ArtifactMetadata]
        The artifact frame (columns :data:`ARTIFACT_COLUMNS`, index ``origin``)
        and its metadata.  A frame shorter than the context yields an **empty**
        artifact rather than an error, so an under-sized download is visible in
        the metadata instead of aborting a pipeline.

    Raises
    ------
    ForecastError
        If the candle frame or the configuration violates the contract.
    ForecastArtifactError
        If the backend returns a wrong number of trajectories, a wrong horizon,
        wrong quantile levels, a wrong origin or non-finite values.
    """
    _validate_build_config(config)
    close = _validated_close(candles)
    target = deseasonalize_log_price(
        close,
        timeframe=config.timeframe,
        period=config.seasonal_period,
        window=config.seasonal_window,
    )
    values = target.to_numpy(dtype="float64")

    context_length = int(config.context_length)
    stride = int(config.reforecast_every)
    horizon = int(config.horizon)
    levels = tuple(float(level) for level in config.quantile_levels)

    origins = pd.DatetimeIndex(close.index[context_length - 1 :: stride])
    requests = [
        ForecastRequest(
            origin=origin,
            context=tuple(
                float(value) for value in values[position + 1 - context_length : position + 1]
            ),
        )
        for position, origin in zip(
            range(context_length - 1, len(close), stride), origins, strict=True
        )
    ]

    medians: list[np.ndarray] = []
    quantile_rows: list[np.ndarray] = []
    if requests:
        resolved = (
            backend
            if backend is not None
            else get_backend(config.backend, **_backend_options(config))
        )
        # The list handed to a backend is a copy on purpose: the model wrappers of
        # a heavy backend are known to pad their input list in place.
        produced = resolved.predict(list(requests), horizon=horizon)
        try:
            trajectories = list(produced)
        except TypeError as exc:  # a backend that does not return an iterable
            raise ForecastArtifactError(
                f"the {config.backend!r} backend did not return a sequence of trajectories: {exc}"
            ) from exc
        if len(trajectories) != len(requests):
            raise ForecastArtifactError(
                f"the {config.backend!r} backend returned {len(trajectories)} trajectory/ies "
                f"for {len(requests)} request(s)"
            )
        for request, trajectory in zip(requests, trajectories, strict=True):
            checked = _checked_trajectory(trajectory, request, config=config)
            medians.append(np.asarray(checked.median, dtype="float32"))
            quantile_rows.append(np.asarray(checked.quantiles, dtype="float32").reshape(-1))

    frame = _artifact_frame(
        origins,
        horizon=horizon,
        stride=stride,
        levels=levels,
        medians=medians,
        quantiles=quantile_rows,
    )
    metadata = ArtifactMetadata(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        symbol=str(config.symbol),
        timeframe=str(config.timeframe),
        backend=str(config.backend),
        model_id=config.model_id,
        context_length=context_length,
        stride=stride,
        horizon=horizon,
        quantile_levels=levels,
        features=tuple(str(feature) for feature in features),
        created_at=_created_at_iso(created_at),
        package_version=str(__version__),
        data_start=pd.Timestamp(close.index[0]),
        data_end=pd.Timestamp(close.index[-1]),
        n_origins=len(origins),
        license=str(config.license),
        deseasonalized=bool(int(config.seasonal_window) > 1),
        seasonal_period=int(config.seasonal_period),
        seasonal_window=int(config.seasonal_window),
    )
    return frame, metadata


def write_forecast_artifact(
    frame: pd.DataFrame, metadata: ArtifactMetadata, path: str | Path
) -> Path:
    """Validate ``frame``, then write the parquet artifact and its sidecar.

    Both files are written atomically: the payload goes to a temporary file in
    the target directory and is then moved onto the destination with
    :meth:`pathlib.Path.replace`, so a crashed build never leaves a half-written
    artifact behind.

    Parameters
    ----------
    frame:
        Artifact frame, see :data:`ARTIFACT_COLUMNS`.  It is validated and
        normalised (``int16`` / ``float32`` columns) before being written; the
        caller's frame is never modified.
    metadata:
        Metadata of that frame; it must describe it exactly (same origin count,
        horizon, stride and quantile levels).
    path:
        Destination of the parquet file.  The sidecar is written next to it as
        ``<path>.meta.json``; parent directories are created.

    Returns
    -------
    pathlib.Path
        The parquet path.

    Raises
    ------
    ForecastArtifactError
        If the frame does not match the schema or the metadata, or if the files
        cannot be written.
    """
    target = Path(path)
    normalised = _normalise_artifact_frame(
        frame, metadata, source=f"the forecast artifact {target}"
    )
    sidecar = metadata_path(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_parquet(normalised, target)
        _write_text(sidecar, json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        raise ForecastArtifactError(f"cannot write the forecast artifact {target}: {exc}") from exc
    return target


def build_forecast_artifact(
    candles: pd.DataFrame,
    config: ForecastBuildConfig,
    path: str | Path,
    *,
    backend: ForecastBackend | None = None,
    created_at: pd.Timestamp | None = None,
    features: Sequence[str] = (),
) -> tuple[Path, ArtifactMetadata]:
    """Build an artifact and write it (the call behind ``trading forecast-build``).

    Returns
    -------
    tuple[pathlib.Path, ArtifactMetadata]
        The parquet path and the metadata that was written next to it.

    Raises
    ------
    ForecastError
        If the candle frame or the configuration violates the contract.
    ForecastArtifactError
        If the backend misbehaves or the artifact cannot be written.
    """
    frame, metadata = build_forecast_frame(
        candles, config, backend=backend, created_at=created_at, features=features
    )
    return write_forecast_artifact(frame, metadata, path), metadata


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class ForecastStore:
    """Random access to one forecast artifact.

    The store is a *reader*: it validates the artifact once, then answers
    :meth:`origin_for` (the active origin of a candle) and :meth:`trajectory`
    (the path that origin predicted) without touching the filesystem again.  A
    strategy can therefore consume an artifact as a deterministic, in-memory
    external input.

    Parameters
    ----------
    frame:
        Artifact frame, see :data:`ARTIFACT_COLUMNS`.
    metadata:
        Metadata describing that frame.
    path:
        Location the artifact was read from, when known.  It is only used for
        error messages and exposed through :attr:`path`.

    Raises
    ------
    ForecastArtifactError
        If the frame is not a valid, metadata-consistent artifact.

    Examples
    --------
    >>> store = ForecastStore.load("data/forecast/btc.parquet")  # doctest: +SKIP
    >>> origin = store.origin_for("2024-01-01T07:30:00Z")        # doctest: +SKIP
    >>> path = store.trajectory(origin).median                   # doctest: +SKIP
    """

    def __init__(
        self,
        frame: pd.DataFrame,
        metadata: ArtifactMetadata,
        *,
        path: str | Path | None = None,
    ) -> None:
        self._metadata = metadata
        self._path = None if path is None else Path(path)
        source = (
            "the forecast artifact" if self._path is None else f"the forecast artifact {self._path}"
        )
        self._frame = _normalise_artifact_frame(frame, metadata, source=source)
        self._origins = pd.DatetimeIndex(self._frame.index)
        self._positions: dict[pd.Timestamp, int] | None = None
        self._cache: dict[pd.Timestamp, ForecastTrajectory] = {}

    @classmethod
    def load(cls, path: str | Path) -> ForecastStore:
        """Read the parquet artifact and its sidecar, validating both.

        Raises
        ------
        ForecastArtifactError
            If the parquet or the sidecar is missing, if the sidecar is not
            valid JSON, if the schema version is not
            :data:`ARTIFACT_SCHEMA_VERSION`, or if the frame does not satisfy the
            artifact contract.  The message always names the offending file.
        """
        target = Path(path)
        sidecar = metadata_path(target)
        if not target.is_file():
            raise ForecastArtifactError(f"forecast artifact not found: {target}")
        if not sidecar.is_file():
            raise ForecastArtifactError(f"forecast artifact metadata not found: {sidecar}")
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ForecastArtifactError(
                f"cannot read the forecast artifact metadata {sidecar}: {exc}"
            ) from exc
        metadata = ArtifactMetadata.from_dict(payload)
        if int(metadata.schema_version) != ARTIFACT_SCHEMA_VERSION:
            raise ForecastArtifactError(
                f"forecast artifact {target} uses schema version {metadata.schema_version}, "
                f"this package reads version {ARTIFACT_SCHEMA_VERSION}"
            )
        try:
            frame = pd.read_parquet(target)
        except Exception as exc:  # pyarrow raises several unrelated types
            raise ForecastArtifactError(
                f"cannot read the forecast artifact {target}: {exc}"
            ) from exc
        return cls(frame, metadata, path=target)

    # -- description -------------------------------------------------------

    @property
    def metadata(self) -> ArtifactMetadata:
        """Metadata of the artifact."""
        return self._metadata

    @property
    def path(self) -> Path | None:
        """Location the artifact was read from, or ``None`` when built in memory."""
        return self._path

    @property
    def timeframe(self) -> str:
        """Candle duration of the artifact."""
        return str(self._metadata.timeframe)

    @property
    def horizon(self) -> int:
        """Forecast steps stored per origin."""
        return int(self._metadata.horizon)

    @property
    def stride(self) -> int:
        """Candles between two stored origins."""
        return int(self._metadata.stride)

    @property
    def quantile_levels(self) -> tuple[float, ...]:
        """Quantile levels stored in the artifact, ascending."""
        return tuple(float(level) for level in self._metadata.quantile_levels)

    # -- lookup ------------------------------------------------------------

    def origins(self) -> pd.DatetimeIndex:
        """Return a copy of the stored origins (tz-aware UTC, named ``origin``)."""
        return pd.DatetimeIndex(self._origins)

    def origin_for(self, timestamp: object) -> pd.Timestamp | None:
        """Return the most recent origin at or before ``timestamp``.

        Parameters
        ----------
        timestamp:
            Anything :class:`pandas.Timestamp` accepts.  A tz-naive value is read
            as UTC, a tz-aware one is converted to UTC.

        Returns
        -------
        pandas.Timestamp | None
            The active origin, or ``None`` when ``timestamp`` is unparseable,
            ``NaT``, or earlier than the first stored origin.  This method never
            raises: a candle outside the covered window simply has no forecast.
        """
        stamp = _coerce_timestamp(timestamp)
        if stamp is None:
            return None
        position = int(self._origins.searchsorted(stamp, side="right")) - 1
        if position < 0:
            return None
        return pd.Timestamp(self._origins[position])

    def trajectory(self, origin: object) -> ForecastTrajectory:
        """Return the trajectory stored at ``origin`` (decoded once, then cached).

        Parameters
        ----------
        origin:
            An exact origin of the artifact.  A tz-naive value is read as UTC.

        Raises
        ------
        ForecastArtifactError
            If ``origin`` is unparseable, ``NaT``, or not an origin of this
            artifact.  Use :meth:`origin_for` to map a candle to its active
            origin.
        """
        stamp = _coerce_timestamp(origin)
        if stamp is None:
            raise ForecastArtifactError(f"forecast origin is not a timestamp: {origin!r}")
        cached = self._cache.get(stamp)
        if cached is not None:
            return cached
        position = self._position(stamp)
        horizon = int(self._metadata.horizon)
        levels = tuple(float(level) for level in self._metadata.quantile_levels)
        flat = np.asarray(self._frame["quantiles"].iloc[position], dtype="float32")
        trajectory = ForecastTrajectory(
            origin=stamp,
            timeframe=str(self._metadata.timeframe),
            horizon=horizon,
            quantile_levels=levels,
            quantiles=flat.reshape(len(levels), horizon),
        )
        self._cache[stamp] = trajectory
        return trajectory

    def _position(self, stamp: pd.Timestamp) -> int:
        """Return the row position of ``stamp``, or raise :class:`ForecastArtifactError`."""
        if self._positions is None:
            self._positions = {
                pd.Timestamp(value): position for position, value in enumerate(self._origins)
            }
        try:
            return self._positions[stamp]
        except KeyError:
            raise ForecastArtifactError(
                f"forecast artifact has no origin at {stamp.isoformat()}"
            ) from None


# ---------------------------------------------------------------------------
# frame construction and validation
# ---------------------------------------------------------------------------


def _artifact_frame(
    origins: pd.DatetimeIndex,
    *,
    horizon: int,
    stride: int,
    levels: tuple[float, ...],
    medians: Sequence[np.ndarray],
    quantiles: Sequence[np.ndarray],
) -> pd.DataFrame:
    """Build the canonical artifact frame (schema dtypes, column order)."""
    index = pd.DatetimeIndex(origins, name=ORIGIN_INDEX_NAME)
    count = len(index)
    if len(medians) != count or len(quantiles) != count:
        raise ForecastArtifactError(
            f"expected {count} per-origin forecast path(s), got {len(medians)} median "
            f"and {len(quantiles)} quantile row(s)"
        )
    n_quantiles = len(levels)
    return pd.DataFrame(
        {
            "horizon": pd.Series(np.full(count, horizon, dtype="int16"), index=index),
            "stride": pd.Series(np.full(count, stride, dtype="int16"), index=index),
            "n_quantiles": pd.Series(np.full(count, n_quantiles, dtype="int16"), index=index),
            "quantile_levels": _array_column(
                [np.asarray(levels, dtype="float32") for _ in range(count)], index
            ),
            "median": _array_column([np.asarray(row, dtype="float32") for row in medians], index),
            "quantiles": _array_column(
                [np.asarray(row, dtype="float32") for row in quantiles], index
            ),
        },
        index=index,
        columns=list(ARTIFACT_COLUMNS),
    )


def _array_column(values: Sequence[np.ndarray], index: pd.DatetimeIndex) -> pd.Series:
    """Return one ``object`` column of ``float32`` arrays (the artifact layout)."""
    if len(values) != len(index):
        raise ForecastArtifactError(f"expected {len(index)} per-origin array(s), got {len(values)}")
    if not values:
        return pd.Series([], index=index, dtype=object)
    return pd.Series(list(values), index=index, dtype=object)


def _normalise_artifact_frame(
    frame: pd.DataFrame, metadata: ArtifactMetadata, *, source: str
) -> pd.DataFrame:
    """Validate ``frame`` against the artifact contract and return a canonical copy.

    The input is never modified.  Every problem raises
    :class:`ForecastArtifactError` naming ``source`` and listing the reasons, so
    a corrupt file is always reported against the file it came from.

    Raises
    ------
    ForecastArtifactError
        If a column is missing or unexpected, if the index is not a
        ``DatetimeIndex`` named ``origin``, if the origins are unsorted or
        duplicated, if the metadata is incoherent with the frame, or if a row
        violates the schema.
    """
    if not isinstance(frame, pd.DataFrame):
        raise ForecastArtifactError(
            f"{source} is not a forecast artifact: expected a pandas DataFrame, "
            f"got {type(frame).__name__}"
        )

    columns = [str(name) for name in frame.columns]
    problems: list[str] = []
    missing = [name for name in ARTIFACT_COLUMNS if name not in columns]
    extra = [name for name in columns if name not in ARTIFACT_COLUMNS]
    if missing:
        problems.append(f"missing column(s): {', '.join(missing)}")
    if extra:
        problems.append(f"unexpected column(s): {', '.join(extra)}")
    if frame.columns.duplicated().any():
        problems.append("a column name is repeated")
    if problems:
        raise ForecastArtifactError(
            f"{source} is not a valid forecast artifact", _limited_issues(problems)
        )

    raw_index = frame.index
    if not isinstance(raw_index, pd.DatetimeIndex):
        raise ForecastArtifactError(
            f"{source} is not a valid forecast artifact",
            [f"the index must be a DatetimeIndex, got {type(raw_index).__name__}"],
        )
    if raw_index.name != ORIGIN_INDEX_NAME:
        raise ForecastArtifactError(
            f"{source} is not a valid forecast artifact",
            [f"the index must be named {ORIGIN_INDEX_NAME!r}, got {raw_index.name!r}"],
        )

    stamps = raw_index.tz_localize(UTC) if raw_index.tz is None else raw_index.tz_convert(UTC)
    if stamps.has_duplicates:
        raise ForecastArtifactError(
            f"{source} is not a valid forecast artifact",
            ["forecast origins must be unique"],
        )
    if not stamps.is_monotonic_increasing:
        raise ForecastArtifactError(
            f"{source} is not a valid forecast artifact",
            ["forecast origins must be sorted ascending"],
        )

    levels = tuple(float(level) for level in metadata.quantile_levels)
    horizon = int(metadata.horizon)
    stride = int(metadata.stride)
    n_quantiles = len(levels)
    metadata_problems = _level_issues(levels)
    if int(metadata.n_origins) != len(frame):
        metadata_problems.append(
            f"the metadata declares {int(metadata.n_origins)} origin(s), the frame holds "
            f"{len(frame)} row(s)"
        )
    for name, value in (("horizon", horizon), ("stride", stride)):
        if value < 1:
            metadata_problems.append(f"the metadata {name} must be >= 1, got {value}")
    for name, value in (
        ("horizon", horizon),
        ("stride", stride),
        ("n_quantiles", n_quantiles),
    ):
        if value > _INT16_MAX:
            metadata_problems.append(
                f"the metadata {name} is {value}, above the int16 limit {_INT16_MAX}"
            )
    if metadata_problems:
        raise ForecastArtifactError(
            f"{source} is not a valid forecast artifact", _limited_issues(metadata_problems)
        )

    expected_levels = np.asarray(levels, dtype="float32")
    median_index = levels.index(MEDIAN_LEVEL)
    raw = {name: frame[name].to_numpy() for name in ARTIFACT_COLUMNS}

    row_problems: list[str] = []
    level_rows: list[np.ndarray] = []
    median_rows: list[np.ndarray] = []
    quantile_rows: list[np.ndarray] = []

    for position in range(len(frame)):
        label = f"row {position}"
        stored_horizon = _row_int(
            raw["horizon"][position], label=label, name="horizon", problems=row_problems
        )
        stored_stride = _row_int(
            raw["stride"][position], label=label, name="stride", problems=row_problems
        )
        stored_n = _row_int(
            raw["n_quantiles"][position], label=label, name="n_quantiles", problems=row_problems
        )
        if stored_horizon is not None and stored_horizon != horizon:
            row_problems.append(
                f"{label}: the horizon column says {stored_horizon}, the metadata says {horizon}"
            )
        if stored_stride is not None and stored_stride != stride:
            row_problems.append(
                f"{label}: the stride column says {stored_stride}, the metadata says {stride}"
            )
        if stored_n is not None and stored_n != n_quantiles:
            row_problems.append(
                f"{label}: the n_quantiles column says {stored_n}, the metadata stores "
                f"{n_quantiles} level(s)"
            )

        row_levels = _row_array(
            raw["quantile_levels"][position],
            label=label,
            name="quantile_levels",
            problems=row_problems,
        )
        if row_levels is not None and not np.array_equal(row_levels, expected_levels):
            row_problems.append(f"{label}: quantile_levels do not match the metadata")
        row_median = _row_array(
            raw["median"][position], label=label, name="median", problems=row_problems
        )
        if row_median is not None and row_median.size != horizon:
            row_problems.append(
                f"{label}: median holds {row_median.size} value(s), expected {horizon}"
            )
        row_quantiles = _row_array(
            raw["quantiles"][position], label=label, name="quantiles", problems=row_problems
        )
        if row_quantiles is not None and row_quantiles.size != n_quantiles * horizon:
            row_problems.append(
                f"{label}: quantiles holds {row_quantiles.size} value(s), expected "
                f"{n_quantiles * horizon}"
            )
        if (
            row_median is not None
            and row_quantiles is not None
            and row_median.shape == (horizon,)
            and row_quantiles.shape == (n_quantiles * horizon,)
        ):
            stored_at_median = row_quantiles.reshape(n_quantiles, horizon)[median_index]
            if not bool(np.allclose(row_median, stored_at_median, rtol=1e-4, atol=1e-6)):
                row_problems.append(
                    f"{label}: the median column disagrees with the {MEDIAN_LEVEL:g} quantile row"
                )

        level_rows.append(row_levels if row_levels is not None else np.empty(0, dtype="float32"))
        median_rows.append(row_median if row_median is not None else np.empty(0, dtype="float32"))
        quantile_rows.append(
            row_quantiles if row_quantiles is not None else np.empty(0, dtype="float32")
        )

    if row_problems:
        raise ForecastArtifactError(
            f"{source} is not a valid forecast artifact", _limited_issues(row_problems)
        )

    return _artifact_frame(
        stamps,
        horizon=horizon,
        stride=stride,
        levels=levels,
        medians=median_rows,
        quantiles=quantile_rows,
    )


def _row_int(value: Any, *, label: str, name: str, problems: list[str]) -> int | None:
    """Return one per-row integer, recording a problem when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        problems.append(f"{label}: {name} must be an integer, got {value!r}")
        return None
    return int(value)


def _row_array(value: Any, *, label: str, name: str, problems: list[str]) -> np.ndarray | None:
    """Return one per-row ``float32`` array, recording a problem when it is not finite."""
    try:
        array = np.asarray(value, dtype="float32").reshape(-1)
    except (TypeError, ValueError):
        problems.append(f"{label}: {name} must be a sequence of numbers")
        return None
    if not bool(np.all(np.isfinite(array))):
        problems.append(f"{label}: {name} must not contain NaN or infinite values")
        return None
    return array


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def _validated_close(candles: pd.DataFrame) -> pd.Series:
    """Return the ``close`` column of ``candles`` as a UTC-indexed ``float64`` series.

    Raises
    ------
    ForecastError
        If ``candles`` is not a non-empty ``DataFrame`` with an ascending,
        unique ``DatetimeIndex`` and a numeric, strictly positive ``close``
        column.  The input frame is never modified.
    """
    if not isinstance(candles, pd.DataFrame):
        raise ForecastError(f"candles must be a pandas DataFrame, got {type(candles).__name__}")
    raw_index = candles.index
    if not isinstance(raw_index, pd.DatetimeIndex):
        raise ForecastError(
            "invalid candle frame",
            [f"the index must be a DatetimeIndex, got {type(raw_index).__name__}"],
        )

    issues: list[str] = []
    if len(candles) == 0:
        issues.append("the candle frame must not be empty")
    if "close" not in candles.columns:
        issues.append("a 'close' column is required")
    if issues:
        raise ForecastError("invalid candle frame", issues)

    stamps = raw_index.tz_localize(UTC) if raw_index.tz is None else raw_index.tz_convert(UTC)
    if stamps.has_duplicates:
        issues.append("candle timestamps must be unique")
    if not stamps.is_monotonic_increasing:
        issues.append("candle timestamps must be sorted ascending")
    try:
        values = candles["close"].to_numpy(dtype="float64")
    except (TypeError, ValueError):
        raise ForecastError(
            "invalid candle frame", ["the 'close' column must hold numbers"]
        ) from None
    if not bool(np.all(np.isfinite(values))):
        issues.append("the 'close' column must not contain NaN or infinite values")
    elif bool(np.any(values <= 0.0)):
        issues.append("the 'close' column must be strictly positive to define a log price")
    if issues:
        raise ForecastError("invalid candle frame", issues)
    return pd.Series(values, index=stamps, name="close", dtype="float64")


def _validate_build_config(config: ForecastBuildConfig) -> None:
    """Validate a build configuration, raising :class:`ForecastError` with every issue."""
    issues: list[str] = []
    if not isinstance(config.symbol, str) or not config.symbol:
        issues.append(f"symbol must be a non-empty string, got {config.symbol!r}")
    if not isinstance(config.backend, str) or not config.backend:
        issues.append(f"backend must be a non-empty string, got {config.backend!r}")
    if config.model_id is not None and not isinstance(config.model_id, str):
        issues.append(f"model_id must be a string or None, got {config.model_id!r}")
    if not isinstance(config.license, str):
        issues.append(f"license must be a string, got {config.license!r}")
    if not isinstance(config.backend_options, Mapping):
        issues.append(
            f"backend_options must be a mapping, got {type(config.backend_options).__name__}"
        )
    try:
        timeframe_delta(config.timeframe)
    except ForecastError as exc:
        issues.append(str(exc))

    numbers = {
        "context_length": _config_int(config.context_length, name="context_length", issues=issues),
        "horizon": _config_int(config.horizon, name="horizon", issues=issues),
        "reforecast_every": _config_int(
            config.reforecast_every, name="reforecast_every", issues=issues
        ),
        "seasonal_period": _config_int(
            config.seasonal_period, name="seasonal_period", issues=issues
        ),
        "seasonal_window": _config_int(
            config.seasonal_window, name="seasonal_window", issues=issues
        ),
        "min_context": _config_int(config.min_context, name="min_context", issues=issues),
    }
    context_length = numbers["context_length"]
    min_context = numbers["min_context"]
    if context_length is not None:
        if context_length < 2:
            issues.append(f"context_length must be >= 2, got {context_length}")
        if min_context is not None and context_length < min_context:
            issues.append(
                f"context_length ({context_length}) must be >= min_context ({min_context})"
            )
    for name in ("horizon", "reforecast_every", "seasonal_period"):
        value = numbers[name]
        if value is not None and value < 1:
            issues.append(f"{name} must be >= 1, got {value}")
    seasonal_window = numbers["seasonal_window"]
    if seasonal_window is not None and seasonal_window < 0:
        issues.append(f"seasonal_window must be >= 0, got {seasonal_window}")
    for name in ("context_length", "horizon", "reforecast_every"):
        value = numbers[name]
        if value is not None and value > _INT16_MAX:
            issues.append(f"{name} is {value}, above the int16 storage limit {_INT16_MAX}")

    try:
        levels = tuple(float(level) for level in config.quantile_levels)
    except (TypeError, ValueError):
        issues.append("quantile_levels must be a sequence of numbers")
    else:
        issues.extend(_level_issues(levels))
        if len(levels) > _INT16_MAX:
            issues.append(
                f"quantile_levels holds {len(levels)} level(s), above the int16 limit {_INT16_MAX}"
            )
    if issues:
        raise ForecastError("invalid forecast build configuration", issues)


def _config_int(value: Any, *, name: str, issues: list[str]) -> int | None:
    """Return a configuration integer, recording an issue when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        issues.append(f"{name} must be an integer, got {value!r}")
        return None
    return int(value)


def _level_issues(levels: Sequence[float]) -> list[str]:
    """Return the problems of a quantile-level sequence (empty when it is valid)."""
    issues: list[str] = []
    if not levels:
        issues.append("quantile_levels must not be empty")
        return issues
    previous: float | None = None
    for level in levels:
        if not math.isfinite(level) or not 0.0 < level < 1.0:
            issues.append(f"quantile level must lie strictly inside (0, 1), got {level!r}")
            continue
        if previous is not None and level <= previous:
            issues.append(f"quantile_levels must be strictly increasing, got {list(levels)!r}")
            break
        previous = level
    if MEDIAN_LEVEL not in levels:
        issues.append(f"quantile_levels must contain the median level {MEDIAN_LEVEL}")
    return issues


def _backend_options(config: ForecastBuildConfig) -> dict[str, Any]:
    """Return the keyword options the registry factory is called with.

    The target transform and the stored levels belong to the artifact, so they
    are always forwarded; an explicit ``backend_options`` entry wins over the
    default, which lets a backend be tuned without editing this module.
    """
    options: dict[str, Any] = {
        "timeframe": config.timeframe,
        "quantile_levels": tuple(float(level) for level in config.quantile_levels),
    }
    options.update({str(key): value for key, value in dict(config.backend_options).items()})
    return options


def _checked_trajectory(
    trajectory: object, request: ForecastRequest, *, config: ForecastBuildConfig
) -> ForecastTrajectory:
    """Validate one backend answer against its request.

    Raises
    ------
    ForecastArtifactError
        If the trajectory is not a :class:`ForecastTrajectory`, if its horizon,
        levels or origin differ from the request, or if its values are not
        finite.
    """
    if not isinstance(trajectory, ForecastTrajectory):
        raise ForecastArtifactError(
            f"the {config.backend!r} backend returned {type(trajectory).__name__} "
            "instead of a ForecastTrajectory"
        )
    if int(trajectory.horizon) != int(config.horizon):
        raise ForecastArtifactError(
            f"the {config.backend!r} backend returned a horizon of {trajectory.horizon}, "
            f"expected {config.horizon}"
        )
    produced = tuple(float(level) for level in trajectory.quantile_levels)
    wanted = tuple(float(level) for level in config.quantile_levels)
    if produced != wanted:
        raise ForecastArtifactError(
            f"the {config.backend!r} backend returned the quantile levels {produced}, "
            f"expected {wanted}"
        )
    if pd.Timestamp(trajectory.origin) != pd.Timestamp(request.origin):
        raise ForecastArtifactError(
            f"the {config.backend!r} backend returned the origin {trajectory.origin} "
            f"for the request at {request.origin}"
        )
    if not bool(np.all(np.isfinite(np.asarray(trajectory.quantiles, dtype="float64")))):
        raise ForecastArtifactError(
            f"the {config.backend!r} backend returned non-finite quantile values"
        )
    return trajectory


def _created_at_iso(created_at: pd.Timestamp | None) -> str:
    """Return an ISO-8601 UTC creation timestamp (``None`` means *now*)."""
    if created_at is None:
        return pd.Timestamp.now(tz=UTC).isoformat()
    try:
        stamp = pd.Timestamp(created_at)
    except (TypeError, ValueError, OverflowError):
        raise ForecastError(f"created_at is not a timestamp: {created_at!r}") from None
    if pd.isna(stamp):
        raise ForecastError("created_at must be a valid timestamp, got NaT")
    return (stamp.tz_localize(UTC) if stamp.tz is None else stamp.tz_convert(UTC)).isoformat()


def _coerce_timestamp(value: object) -> pd.Timestamp | None:
    """Return ``value`` as a UTC :class:`pandas.Timestamp`, or ``None``.

    ``None`` means "not a usable timestamp": an unparseable value, ``None`` or
    ``NaT``.  A tz-naive timestamp is read as UTC, a tz-aware one is converted.
    """
    try:
        stamp = pd.Timestamp(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.tz_localize(UTC) if stamp.tz is None else stamp.tz_convert(UTC)


def _limited_issues(issues: Sequence[str]) -> list[str]:
    """Return at most :data:`_MAX_REPORTED_ISSUES` issues plus a summary of the rest."""
    if len(issues) <= _MAX_REPORTED_ISSUES:
        return list(issues)
    extra = len(issues) - _MAX_REPORTED_ISSUES
    return [*issues[:_MAX_REPORTED_ISSUES], f"... and {extra} more problem(s)"]


def _write_parquet(frame: pd.DataFrame, path: Path) -> None:
    """Write ``frame`` to ``path`` atomically (temporary file + ``replace``)."""
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    os.close(handle)
    temporary_path = Path(temporary)
    try:
        frame.to_parquet(temporary_path, index=True)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temporary file + ``replace``)."""
    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# sidecar field readers
# ---------------------------------------------------------------------------


def _payload_value(payload: Mapping[str, Any], key: str, issues: list[str]) -> Any:
    """Return one payload value, recording a missing key."""
    value = payload.get(key, _MISSING)
    if value is _MISSING:
        issues.append(f"missing key {key!r}")
        return None
    return value


def _payload_int(payload: Mapping[str, Any], key: str, issues: list[str]) -> int | None:
    """Return one integer payload field."""
    value = _payload_value(payload, key, issues)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(f"{key!r} must be an integer, got {type(value).__name__}")
        return None
    return int(value)


def _payload_text(payload: Mapping[str, Any], key: str, issues: list[str]) -> str | None:
    """Return one string payload field."""
    value = _payload_value(payload, key, issues)
    if value is None:
        return None
    if not isinstance(value, str):
        issues.append(f"{key!r} must be a string, got {type(value).__name__}")
        return None
    return value


def _payload_optional_text(payload: Mapping[str, Any], key: str, issues: list[str]) -> str | None:
    """Return one nullable string payload field (``None`` stays ``None``)."""
    value = _payload_value(payload, key, issues)
    if value is None:
        return None
    if not isinstance(value, str):
        issues.append(f"{key!r} must be a string or null, got {type(value).__name__}")
        return None
    return value


def _payload_bool(payload: Mapping[str, Any], key: str, issues: list[str]) -> bool | None:
    """Return one boolean payload field."""
    value = _payload_value(payload, key, issues)
    if value is None:
        return None
    if not isinstance(value, bool):
        issues.append(f"{key!r} must be a boolean, got {type(value).__name__}")
        return None
    return value


def _payload_timestamp(
    payload: Mapping[str, Any], key: str, issues: list[str]
) -> pd.Timestamp | None:
    """Return one ISO-8601 payload field as a timestamp."""
    value = _payload_value(payload, key, issues)
    if value is None:
        return None
    if not isinstance(value, str):
        issues.append(f"{key!r} must be an ISO-8601 string, got {type(value).__name__}")
        return None
    stamp = _coerce_timestamp(value)
    if stamp is None:
        issues.append(f"{key!r} is not a valid timestamp: {value!r}")
        return None
    return stamp


def _payload_float_tuple(
    payload: Mapping[str, Any], key: str, issues: list[str]
) -> tuple[float, ...] | None:
    """Return one payload field as a tuple of floats."""
    value = _payload_value(payload, key, issues)
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        issues.append(f"{key!r} must be a list of numbers, got {type(value).__name__}")
        return None
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            issues.append(f"{key!r} must hold numbers, got {item!r}")
            return None
        numbers.append(float(item))
    return tuple(numbers)


def _payload_text_tuple(
    payload: Mapping[str, Any], key: str, issues: list[str]
) -> tuple[str, ...] | None:
    """Return one payload field as a tuple of strings."""
    value = _payload_value(payload, key, issues)
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        issues.append(f"{key!r} must be a list of strings, got {type(value).__name__}")
        return None
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            issues.append(f"{key!r} must hold strings, got {item!r}")
            return None
        names.append(item)
    return tuple(names)
