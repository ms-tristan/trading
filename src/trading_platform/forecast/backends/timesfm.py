"""The ``timesfm`` backend: Google TimesFM 2.5 behind the optional ``[timesfm]`` extra.

What this module is
-------------------
It is the only place of the package that talks to a machine-learning runtime, and
it does so **lazily**: ``torch`` and ``timesfm`` are imported inside the methods
that need them, so importing this module -- and therefore the CLI, the artifact
builder and the whole test-suite -- never requires the extra.  When the extra is
absent, :meth:`TimesFMBackend.is_available` returns ``False`` and
:meth:`TimesFMBackend.predict` raises
:class:`~trading_platform.core.errors.ForecastError` naming the exact
``pip install`` command.

Model choice and licences
-------------------------
``google/timesfm-2.5-200m-pytorch`` is the default because it is the newest
checkpoint whose **weights are Apache-2.0**.  TimesFM 3.0 is the better model
(native multivariate and native covariate support) but its weights ship under the
TimesFM Non-Commercial License v1.0, so it is strictly **opt-in**: build the
backend with ``model_id="google/timesfm-3.0-pytorch"``, accept the terms and read
:func:`license_note`.  The 2.5 checkpoint must never stop being the default.

Device policy (CPU first)
-------------------------
The 2.5 PyTorch code path selects ``cuda`` when CUDA is available and ``cpu``
otherwise; it **never selects MPS**, and moving the model to MPS by hand fails on
the library's internal ``float64`` padding row (``Cannot convert a MPS Tensor to
float64``).  ``device="mps"`` is therefore rejected with an explicit error and
``device=None`` resolves to ``cuda``/``cpu`` only.  The resolved device is
validated and recorded on the instance; this backend never moves the model
itself.

Measured costs and library traps this module defends against
------------------------------------------------------------
* ``forecast()`` **mutates the list it is given**: when the batch is not a
  multiple of ``global_batch_size`` it appends dummy rows to the *caller's* list,
  so a reused list silently grows (a 3-element list becomes 32 and the next call
  returns 32 forecasts).  Every call here builds a fresh list of fresh
  ``float32`` arrays, and rows beyond the requested batch are treated as padding.
* ``max_context + max_horizon <= 16384`` is enforced by the library at
  ``compile()`` time, and the continuous quantile head only decodes
  ``max_horizon <= 1024`` steps.  A horizon larger than the configured
  ``max_horizon`` would raise ``ValueError`` inside the library, so it is
  rejected here first, before anything is imported.
* ``horizon`` is nearly free while ``max_horizon`` and ``context`` drive the cost
  (the decoder always runs to ``max_horizon`` steps): compile the smallest
  ``max_horizon`` you need.  Batching also matters -- context 1024, batch 32
  costs about 741 ms on an M4 CPU (~23 ms per series) -- so equal-length requests
  are grouped and sent in ``per_core_batch_size`` chunks.
* ``force_flip_invariance=True`` **exactly doubles** the latency and is off by
  default.
* The output head returns ``(batch, horizon, 10)``: channel 0 is the **mean** (it
  is not a quantile and is not ordered with them) and channels 1..9 are
  ``q10...q90``.  The point forecast is the median, i.e. channel 5, so the level
  ``0.5`` row always comes from the point array.

Input, output and the no-look-ahead rule
----------------------------------------
A :class:`~trading_platform.forecast.types.ForecastRequest` carries the
deterministically de-seasonalised log price of the candles up to and including
``origin`` (see :mod:`trading_platform.forecast.series`); TimesFM has **no
calendar channel**, so the seasonality is removed before the model sees the
series and only the optional XReg mode receives a calendar covariate.  The model
forecasts the level of that series, while the package contract is a *normalised*
path (``0.0`` is the close of ``origin``), so the origin level -- the last context
value -- is subtracted from every returned value.  For a caller that already
passes an origin-relative series that subtraction is a no-op.  Only candles
``<= origin`` are ever read.

XReg covariates are **not** a transformer channel
-------------------------------------------------
``use_xreg=True`` calls ``forecast_with_covariates`` with a single deterministic
calendar-phase covariate (never ``RSI`` or momentum: a future covariate value
would have to be fabricated, which is look-ahead leakage).  It requires
``return_backcast=True`` and the ``timesfm[xreg]`` extra (JAX + scikit-learn), and
it is a per-series **linear/ridge residual corrector wrapped around** TimesFM --
the transformer never attends to the covariate.  It is off by default and stays
opt-in.

Honesty note
------------
Three independent 2025-26 studies find no reliable directional edge of
off-the-shelf or LoRA-tuned time-series foundation models on equity returns, and
TimesFM-2.5 loses to a Log-HAR baseline on realised volatility.  This backend
therefore stays a *producer of features*: measure it with the offline
forecast-skill report (MASE/RMSE against the random walk, quantile coverage,
directional accuracy) before trusting any backtest that consumes it.
"""

from __future__ import annotations

import importlib.util
import logging
import math
from collections.abc import Sequence
from typing import Any, cast

import numpy as np
import pandas as pd

from trading_platform.core.errors import ForecastError
from trading_platform.forecast.types import (
    DEFAULT_QUANTILE_LEVELS,
    MEDIAN_LEVEL,
    ForecastRequest,
    ForecastTrajectory,
)

__all__ = [
    "BACKEND_NAME",
    "DEFAULT_MODEL_ID",
    "LICENSE_APACHE",
    "LICENSE_NON_COMMERCIAL",
    "NON_COMMERCIAL_MODEL_IDS",
    "TIMESFM_EXTRA_HINT",
    "TIMESFM_XREG_EXTRA_HINT",
    "TimesFMBackend",
    "build_backend",
    "license_note",
]

#: Registry key of this backend.
BACKEND_NAME: str = "timesfm"

#: Newest checkpoint whose **weights** are Apache-2.0: the default, always.
DEFAULT_MODEL_ID: str = "google/timesfm-2.5-200m-pytorch"

#: Checkpoints whose weights are *not* Apache-2.0: opt-in only, never a default.
NON_COMMERCIAL_MODEL_IDS: tuple[str, ...] = ("google/timesfm-3.0-pytorch",)

#: Install hint shown when ``torch``/``timesfm`` are missing.
TIMESFM_EXTRA_HINT: str = "pip install 'trading-platform[timesfm]'"

#: Install hint shown when the optional XReg covariate mode is unusable.
TIMESFM_XREG_EXTRA_HINT: str = "pip install 'trading-platform[timesfm-xreg]'"

#: Licence of the Apache-2.0 checkpoints (2.5 and older).
LICENSE_APACHE: str = "Apache-2.0"

#: Licence of the non-commercial checkpoints (3.0).
LICENSE_NON_COMMERCIAL: str = (
    "TimesFM Non-Commercial License v1.0 (non-commercial, non-production use only)"
)

#: Modules of the ``[timesfm]`` extra, both required to run this backend.
_EXTRA_MODULES: tuple[str, ...] = ("torch", "timesfm")

#: Name of the deterministic calendar covariate used by the optional XReg mode.
_CALENDAR_COVARIATE: str = "calendar_phase"

#: Precision requested from PyTorch: the checkpoint was trained with it.
_MATMUL_PRECISION: str = "high"

#: Quantile channels of the 2.5 head: 1 mean + the 9 deciles.
_HEAD_CHANNELS: int = 10

#: Channels 1..9 of the head carry ``q10...q90`` in the order of the default levels.
_FIRST_QUANTILE_CHANNEL: int = 1

#: Row of the decile block that carries the median, i.e. the point forecast.
_MEDIAN_ROW: int = DEFAULT_QUANTILE_LEVELS.index(MEDIAN_LEVEL)

#: Seconds of one candle unit, for the small timeframe parser below.
_UNIT_SECONDS: dict[str, int] = {"m": 60, "h": 3600, "d": 86400, "w": 604800}

_SECONDS_PER_DAY: float = 86400.0

_LOGGER = logging.getLogger(__name__)


def license_note(model_id: str) -> str:
    """Return the licence that governs the weights of ``model_id``.

    Parameters
    ----------
    model_id:
        Hugging Face repository id of the checkpoint.

    Returns
    -------
    str
        :data:`LICENSE_NON_COMMERCIAL` for a checkpoint listed in
        :data:`NON_COMMERCIAL_MODEL_IDS` (TimesFM 3.0), :data:`LICENSE_APACHE`
        for every other checkpoint (TimesFM 2.5 and older).
    """
    return LICENSE_NON_COMMERCIAL if model_id in NON_COMMERCIAL_MODEL_IDS else LICENSE_APACHE


def build_backend(**options: Any) -> TimesFMBackend:
    """Build a :class:`TimesFMBackend` from keyword ``options`` (registry factory).

    The registry calls this factory with the user options, so every keyword of
    :meth:`TimesFMBackend.__init__` can be set from the configuration layer.
    """
    return TimesFMBackend(**options)


def _module_available(name: str) -> bool:
    """Return ``True`` when ``name`` can be imported, never raising."""
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # a broken environment must never break availability probing
        return False


def _int_value(value: Any, *, name: str) -> int:
    """Return ``value`` as a plain ``int``, or raise :class:`ForecastError`."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ForecastError(f"{name} must be an integer, got {value!r}") from None
    if number != value:
        raise ForecastError(f"{name} must be an integer, got {value!r}")
    return number


def _positive_int(value: Any, *, name: str) -> int:
    """Return ``value`` as a plain ``int`` >= 1, or raise :class:`ForecastError`."""
    number = _int_value(value, name=name)
    if number < 1:
        raise ForecastError(f"{name} must be >= 1, got {number}")
    return number


def _timeframe_delta(timeframe: Any) -> pd.Timedelta:
    """Return the duration of one ``timeframe`` candle.

    The parser is deliberately tiny (``15m``, ``1h``, ``4h``, ``1d``, ``1w``) and
    builds the duration from a number of seconds, which keeps it free of locale,
    calendar and pandas unit-deprecation surprises.

    Raises
    ------
    ForecastError
        If ``timeframe`` is not a supported candle duration.
    """
    if not isinstance(timeframe, str) or not timeframe:
        raise ForecastError(f"timeframe must be a non-empty string, got {timeframe!r}")
    unit = _UNIT_SECONDS.get(timeframe[-1])
    count = timeframe[:-1]
    if unit is None or not count.isdigit() or int(count) < 1:
        supported = ", ".join(f"1{key}" for key in sorted(_UNIT_SECONDS))
        raise ForecastError(
            f"unsupported timeframe: {timeframe!r} (expected a candle duration such as {supported})"
        )
    return pd.Timedelta(seconds=int(count) * unit)


def _decile_index(level: float) -> int | None:
    """Return the decile row of ``level``, or ``None`` when it is not a decile."""
    if not math.isfinite(level):
        return None
    for index, decile in enumerate(DEFAULT_QUANTILE_LEVELS):
        if math.isclose(level, decile, rel_tol=0.0, abs_tol=1e-9):
            return index
    return None


def _validate_levels(levels: Sequence[float]) -> tuple[tuple[float, ...], tuple[int, ...]]:
    """Validate the requested quantile levels and locate them in the model head.

    Parameters
    ----------
    levels:
        Requested quantile levels.

    Returns
    -------
    tuple of tuple
        The levels as floats, and the index of each of them among the nine deciles
        of :data:`~trading_platform.forecast.types.DEFAULT_QUANTILE_LEVELS`.

    Raises
    ------
    ForecastError
        If the levels are not a strictly increasing subset of ``0.1 ... 0.9`` that
        contains the median ``0.5``.  The 2.5 continuous quantile head only
        provides those nine deciles; any other level would have to be
        interpolated, which is the caller's job, not the backend's.
    """
    try:
        values = tuple(float(level) for level in levels)
    except (TypeError, ValueError):
        raise ForecastError("quantile_levels must be a sequence of numbers") from None
    if not values:
        raise ForecastError("quantile_levels must not be empty")

    indices: list[int] = []
    previous: float | None = None
    for level in values:
        index = _decile_index(level)
        if index is None:
            raise ForecastError(
                "the timesfm backend exposes the nine deciles of the continuous quantile "
                f"head (0.1 ... 0.9) only, got {level!r}: interpolate another level yourself"
            )
        if previous is not None and level <= previous:
            raise ForecastError(
                f"quantile_levels must be strictly increasing, got {list(values)!r}"
            )
        indices.append(index)
        previous = level

    if _decile_index(MEDIAN_LEVEL) not in indices:
        raise ForecastError(
            f"quantile_levels must contain the median level {MEDIAN_LEVEL}, got {list(values)!r}"
        )
    return values, tuple(indices)


def _context_values(request: ForecastRequest) -> np.ndarray:
    """Return the finite ``float64`` context of ``request``.

    Raises
    ------
    ForecastError
        If the context is not numeric, holds fewer than two values or holds a
        non-finite value.  Trailing NaNs are not handled by the TimesFM wrappers,
        so the message tells the caller to drop them first.
    """
    try:
        values = np.asarray(request.context, dtype="float64").reshape(-1)
    except (TypeError, ValueError):
        raise ForecastError("context must be a sequence of numbers") from None
    if values.size < 2 or not bool(np.all(np.isfinite(values))):
        raise ForecastError(
            "context must hold at least 2 finite values; drop leading and trailing NaNs "
            "before forecasting"
        )
    return values


def _as_float_array(value: Any, *, name: str) -> np.ndarray:
    """Return ``value`` as a ``float32`` array, or raise :class:`ForecastError`."""
    try:
        return np.asarray(value, dtype="float32")
    except (TypeError, ValueError):
        raise ForecastError(f"the timesfm backend returned a non-numeric {name}") from None


def _quantile_rows(point: Any, quantiles: Any, *, batch: int, horizon: int) -> np.ndarray:
    """Return the decile block of one model call as ``(batch, 9, horizon)`` float32.

    Channel 0 of the head is the **mean** and is dropped; channel 5 (the median)
    is replaced by the point forecast, which the library defines as the median.

    Parameters
    ----------
    point:
        ``(batch, horizon)`` point forecast returned by the model (the median).
    quantiles:
        ``(batch, horizon, 10)`` head output: index 0 is the mean, indices 1..9 are
        ``q10...q90``.
    batch:
        Number of series that were actually requested.  The library pads the batch
        it receives to ``global_batch_size``, so more rows may come back; the
        requested series always come first and the padding is dropped.
    horizon:
        Number of steps requested.

    Raises
    ------
    ForecastError
        If the arrays are not numeric, have an unexpected rank or shape, or hold
        fewer rows than were requested.
    """
    point_array = _as_float_array(point, name="point forecast")
    quantile_array = _as_float_array(quantiles, name="quantile forecast")
    if point_array.ndim != 2:
        raise ForecastError(
            "the timesfm backend returned a point forecast with "
            f"{point_array.ndim} dimension(s), expected 2 (batch, horizon)"
        )
    if quantile_array.ndim != 3:
        raise ForecastError(
            "the timesfm backend returned a quantile forecast with "
            f"{quantile_array.ndim} dimension(s), expected 3 (batch, horizon, channels)"
        )
    if point_array.shape[1] != horizon:
        raise ForecastError(
            f"the timesfm backend returned {point_array.shape[1]} step(s), expected {horizon}"
        )
    expected = (point_array.shape[0], horizon, _HEAD_CHANNELS)
    if quantile_array.shape != expected:
        raise ForecastError(
            "the timesfm backend returned a quantile forecast of shape "
            f"{quantile_array.shape}, expected {expected}"
        )
    if point_array.shape[0] < batch:
        raise ForecastError(
            f"the timesfm backend returned {point_array.shape[0]} forecast(s) for {batch} "
            "request(s)"
        )

    # ``(batch, horizon, channels)`` -> ``(batch, n_levels, horizon)`` for the trajectory.
    rows = np.array(
        np.transpose(quantile_array[:batch, :, _FIRST_QUANTILE_CHANNEL:], (0, 2, 1)),
        dtype="float32",
        copy=True,
    )
    rows[:, _MEDIAN_ROW, :] = point_array[:batch]
    return rows


def _calendar_phase(origin: pd.Timestamp, timeframe: str, length: int) -> list[int]:
    """Return the deterministic calendar phase of ``length`` candles ending at ``origin``.

    The timestamps are implied positionally by ``origin`` and ``timeframe``: candle
    ``i`` of the returned list is ``origin - (length - 1 - i)`` candles, so the last
    entry is always ``origin`` itself.  This is what makes the optional XReg
    covariate causal: it only describes *when* a candle is, never *what* happened.

    The phase is the position of the candle inside its calendar cycle:

    * a sub-daily timeframe yields the number of whole candles since the start of
      the UTC day (``24`` phases for hourly candles, ``6`` for 4-hour candles);
    * a daily or slower timeframe yields the day of the week (``0`` = Monday).
      Every weekly candle starts on the same weekday, so that phase is constant
      for ``1w`` and slower timeframes: a documented degeneracy of the calendar
      covariate, which the optional XReg mode would simply drop.

    Parameters
    ----------
    origin:
        Timestamp of the last context candle.  A timezone-naive timestamp is read
        as UTC.
    timeframe:
        Candle duration, a supported value of :func:`_timeframe_delta`.
    length:
        Number of candles to describe (context plus horizon).

    Returns
    -------
    list of int
        One phase per candle, oldest first.
    """
    delta = _timeframe_delta(timeframe)
    stamp = pd.Timestamp(origin)
    stamp = stamp.tz_localize("UTC") if stamp.tz is None else stamp.tz_convert("UTC")
    stamps = pd.date_range(end=stamp, periods=length, freq=delta)
    seconds = float(delta.total_seconds())
    if seconds >= _SECONDS_PER_DAY:
        return [int(value) for value in stamps.dayofweek]
    offsets = (stamps - stamps.normalize()).total_seconds()
    return [int(value // seconds) for value in offsets]


class TimesFMBackend:
    """Zero-shot TimesFM 2.5 backend, CPU first, behind the ``[timesfm]`` extra.

    Parameters
    ----------
    model_id:
        Hugging Face checkpoint to load.  Defaults to the Apache-2.0
        :data:`DEFAULT_MODEL_ID`; a checkpoint listed in
        :data:`NON_COMMERCIAL_MODEL_IDS` is accepted but logs a licence warning.
    device:
        ``None`` (let the library pick ``cuda`` when available, else ``cpu``),
        ``"cpu"`` or ``"cuda"``.  ``"mps"`` is rejected: the 2.5 code path never
        selects MPS and a manual move fails on the library's ``float64`` padding
        row.  A requested ``"cuda"`` that is unavailable falls back to ``cpu``
        with a warning.
    torch_compile:
        Forwarded to ``from_pretrained``.  Off by default: compilation is not
        verified on Apple silicon and costs a long first call.
    max_context:
        Maximum context length the model is compiled for (the library truncates
        longer series to the last ``max_context`` points).
    max_horizon:
        Maximum horizon the model is compiled for.  The decoder always runs to
        ``max_horizon`` steps, so this is the real cost driver; keep it a small
        multiple of 128 and remember ``max_context + max_horizon <= 16384``.
    per_core_batch_size:
        Number of series per ``forecast`` call (the library's
        ``global_batch_size``); requests are grouped and chunked to this size.
    normalize_inputs:
        ``ForecastConfig`` flag; kept on for the de-seasonalised log price.
    use_continuous_quantile_head:
        ``ForecastConfig`` flag; the continuous head provides the quantiles.
    fix_quantile_crossing:
        ``ForecastConfig`` flag; sorts the quantiles so they cannot cross.
    infer_is_positive:
        ``ForecastConfig`` flag, **off**: the target is a de-seasonalised log
        price whose increments are signed, so forcing a positive series would
        clip the forecast.
    force_flip_invariance:
        ``ForecastConfig`` flag, off: enabling it **exactly doubles** the latency.
    return_backcast:
        ``ForecastConfig`` flag; forced on when ``use_xreg`` is set, because the
        XReg wrapper needs the backcast residual.
    use_xreg:
        Opt-in covariate mode.  It passes a single deterministic calendar-phase
        covariate and needs the ``timesfm[xreg]`` extra.  XReg is a per-series
        linear/ridge regression wrapped *around* the model, never a channel the
        transformer attends to.
    xreg_mode:
        ``"xreg + timesfm"`` (regression on the context, model on the residual) or
        ``"timesfm + xreg"`` (model first, regression on its residual).
    timeframe:
        Candle duration of the context, stored on every trajectory and used by the
        calendar covariate.
    quantile_levels:
        Levels to store, a strictly increasing subset of ``0.1 ... 0.9`` that
        contains ``0.5``.  They are validated by :meth:`predict` rather than by the
        constructor, so registry introspection can never raise.

    Examples
    --------
    >>> backend = TimesFMBackend(max_context=512, max_horizon=128)  # doctest: +SKIP
    >>> backend.is_available()  # doctest: +SKIP
    False
    """

    name: str = BACKEND_NAME

    def __init__(
        self,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        device: str | None = None,
        torch_compile: bool = False,
        max_context: int = 1024,
        max_horizon: int = 128,
        per_core_batch_size: int = 32,
        normalize_inputs: bool = True,
        use_continuous_quantile_head: bool = True,
        fix_quantile_crossing: bool = True,
        infer_is_positive: bool = False,
        force_flip_invariance: bool = False,
        return_backcast: bool = False,
        use_xreg: bool = False,
        xreg_mode: str = "xreg + timesfm",
        timeframe: str = "1h",
        quantile_levels: Sequence[float] = DEFAULT_QUANTILE_LEVELS,
    ) -> None:
        if not isinstance(model_id, str) or not model_id:
            raise ForecastError(f"model_id must be a non-empty string, got {model_id!r}")
        self._model_id = model_id
        if device is not None and not isinstance(device, str):
            raise ForecastError(f"device must be a string or None, got {device!r}")
        self._device = device
        self._torch_compile = bool(torch_compile)
        self._max_context = _positive_int(max_context, name="max_context")
        self._max_horizon = _positive_int(max_horizon, name="max_horizon")
        self._per_core_batch_size = _positive_int(per_core_batch_size, name="per_core_batch_size")
        self._normalize_inputs = bool(normalize_inputs)
        self._use_continuous_quantile_head = bool(use_continuous_quantile_head)
        self._fix_quantile_crossing = bool(fix_quantile_crossing)
        self._infer_is_positive = bool(infer_is_positive)
        self._force_flip_invariance = bool(force_flip_invariance)
        self._return_backcast = bool(return_backcast)
        self._use_xreg = bool(use_xreg)
        self._xreg_mode = xreg_mode
        self._timeframe = timeframe
        _timeframe_delta(self._timeframe)
        try:
            self._quantile_levels: tuple[float, ...] = tuple(quantile_levels)
        except TypeError:
            raise ForecastError("quantile_levels must be a sequence of numbers") from None
        self._model: Any = None
        self._resolved_device: str | None = None

        if license_note(self._model_id) == LICENSE_NON_COMMERCIAL:
            _LOGGER.warning(
                "TimesFM checkpoint %s ships under the %s; use %s (%s) unless those terms "
                "are acceptable",
                self._model_id,
                LICENSE_NON_COMMERCIAL,
                DEFAULT_MODEL_ID,
                LICENSE_APACHE,
            )

    # -- introspection -----------------------------------------------------

    @property
    def model_id(self) -> str:
        """Hugging Face checkpoint this backend loads."""
        return self._model_id

    @property
    def timeframe(self) -> str:
        """Candle duration stored on every produced trajectory."""
        return self._timeframe

    @property
    def quantile_levels(self) -> tuple[float, ...]:
        """Levels as passed to the constructor (validated by :meth:`predict`)."""
        return self._quantile_levels

    @property
    def max_context(self) -> int:
        """Maximum context length the model is compiled for."""
        return self._max_context

    @property
    def max_horizon(self) -> int:
        """Maximum horizon the model is compiled for (the real cost driver)."""
        return self._max_horizon

    @property
    def per_core_batch_size(self) -> int:
        """Number of series sent to the model per call."""
        return self._per_core_batch_size

    @property
    def use_xreg(self) -> bool:
        """Whether the optional XReg covariate mode is enabled."""
        return self._use_xreg

    @property
    def device(self) -> str | None:
        """Device as configured (``None`` means "let the library choose")."""
        return self._device

    @property
    def resolved_device(self) -> str | None:
        """Device the last :meth:`predict` call resolved to (``None`` before that)."""
        return self._resolved_device

    def license_note(self) -> str:
        """Return the licence governing the weights of this backend's checkpoint."""
        return license_note(self._model_id)

    def is_available(self) -> bool:
        """Return ``True`` when ``torch`` and ``timesfm`` can both be imported.

        The probe never raises: a missing extra, a broken installation or a
        failing ``find_spec`` simply means ``False``.
        """
        return all(_module_available(module) for module in _EXTRA_MODULES)

    # -- forecasting -------------------------------------------------------

    def predict(
        self, requests: Sequence[ForecastRequest], *, horizon: int
    ) -> list[ForecastTrajectory]:
        """Forecast ``horizon`` steps for every request, in request order.

        Every request is validated *before* anything is imported, so an invalid
        call fails fast and never loads a model.  Requests are then grouped by
        identical context length, chunked to ``per_core_batch_size`` and sent as
        one ``forecast`` call per chunk (a fresh list of fresh ``float32`` arrays
        each time, see the module docstring).

        Parameters
        ----------
        requests:
            The requests to forecast.  They are read, never modified.
        horizon:
            Number of steps of every trajectory (``1 <= horizon <= max_horizon``).

        Returns
        -------
        list of ForecastTrajectory
            One trajectory per request, in the original request order.  Each path
            is normalised to the close of its origin: the origin level is
            subtracted from the model output, and the level ``0.5`` row is the
            point forecast (the library's median channel).

        Raises
        ------
        ForecastError
            If an argument is invalid, if the ``[timesfm]`` extra is missing, if
            the device is unsupported, if the XReg covariate mode is requested
            without ``timesfm[xreg]``, or if the model returns an unexpected shape.
        """
        if not requests:
            return []

        steps = _positive_int(horizon, name="horizon")
        if steps > self._max_horizon:
            raise ForecastError(
                f"horizon {steps} exceeds the configured max_horizon {self._max_horizon}: the "
                "TimesFM 2.5 decoder always decodes exactly max_horizon steps and the library "
                "raises ValueError above it"
            )

        levels, level_rows = _validate_levels(self._quantile_levels)
        contexts = [_context_values(request) for request in requests]

        torch_module, timesfm_module = self._import_runtime()
        torch_module.set_float32_matmul_precision(_MATMUL_PRECISION)
        self._resolved_device = self._resolve_device()
        model = self._ensure_model(timesfm_module)

        # Group by identical context length: one library call per group chunk.
        groups: dict[int, list[tuple[int, np.ndarray]]] = {}
        for index, context in enumerate(contexts):
            groups.setdefault(int(context.size), []).append((index, context))

        trajectories: dict[int, ForecastTrajectory] = {}
        for entries in groups.values():
            for start in range(0, len(entries), self._per_core_batch_size):
                chunk = entries[start : start + self._per_core_batch_size]
                # Fresh list, fresh arrays: the library pads and edits what it gets.
                inputs = [np.array(context, dtype="float32", copy=True) for _, context in chunk]
                point, quantiles = self._invoke(
                    model,
                    inputs,
                    origins=[requests[index].origin for index, _ in chunk],
                    horizon=steps,
                )
                rows = _quantile_rows(point, quantiles, batch=len(chunk), horizon=steps)
                for position, (index, context) in enumerate(chunk):
                    trajectories[index] = ForecastTrajectory(
                        origin=requests[index].origin,
                        timeframe=self._timeframe,
                        horizon=steps,
                        quantile_levels=levels,
                        quantiles=self._normalise(rows[position], level_rows, context),
                    )
        return [trajectories[index] for index in range(len(requests))]

    # -- internals ---------------------------------------------------------

    def _import_runtime(self) -> tuple[Any, Any]:
        """Import ``torch`` and ``timesfm`` lazily.

        Raises
        ------
        ForecastError
            If either module is missing, naming the ``[timesfm]`` extra.
        """
        try:
            import timesfm
            import torch
        except ImportError as exc:
            raise ForecastError(
                f"the 'timesfm' backend needs the optional extra: {TIMESFM_EXTRA_HINT}"
            ) from exc
        return torch, timesfm

    def _resolve_device(self) -> str:
        """Return the device the library will actually use, validating the request.

        ``None`` means "let the library choose" and resolves to ``cuda`` when CUDA
        is available, else ``cpu``.  The value is recorded on the instance for
        introspection; the model itself is never moved, because the 2.5 code path
        never selects MPS and a manual move fails on the library's ``float64``
        padding row.

        Raises
        ------
        ForecastError
            If ``device`` is ``"mps"`` (unsupported by the 2.5 code path) or an
            unknown value.
        """
        try:
            import torch
        except ImportError as exc:
            raise ForecastError(
                f"the 'timesfm' backend needs the optional extra: {TIMESFM_EXTRA_HINT}"
            ) from exc

        if self._device is None:
            return "cuda" if bool(torch.cuda.is_available()) else "cpu"
        if self._device == "cpu":
            return "cpu"
        if self._device == "cuda":
            if bool(torch.cuda.is_available()):
                return "cuda"
            _LOGGER.warning("CUDA is unavailable: running the timesfm backend on the CPU")
            return "cpu"
        if self._device == "mps":
            raise ForecastError(
                "device 'mps' is not supported by the TimesFM 2.5 code path, which never "
                "selects MPS, and a manual move fails on the library's float64 padding row: "
                "use 'cpu'"
            )
        raise ForecastError(
            f"unsupported device: {self._device!r} (use 'cpu', 'cuda' or None for the default)"
        )

    def _ensure_model(self, timesfm_module: Any) -> Any:
        """Load and compile the checkpoint once, then reuse it.

        The real ``from_pretrained``/``compile`` pair downloads weights and needs
        the extra installed; it is reached only through :meth:`predict`, which has
        already imported the runtime.
        """
        if self._model is not None:
            return self._model
        config = timesfm_module.ForecastConfig(
            max_context=self._max_context,
            max_horizon=self._max_horizon,
            normalize_inputs=self._normalize_inputs,
            per_core_batch_size=self._per_core_batch_size,
            use_continuous_quantile_head=self._use_continuous_quantile_head,
            force_flip_invariance=self._force_flip_invariance,
            infer_is_positive=self._infer_is_positive,
            fix_quantile_crossing=self._fix_quantile_crossing,
            # XReg reads the backcast residual, so the flag is forced on there.
            return_backcast=self._return_backcast or self._use_xreg,
        )
        model = timesfm_module.TimesFM_2p5_200M_torch.from_pretrained(
            self._model_id, torch_compile=self._torch_compile
        )
        model.compile(config)
        self._model = model
        return model

    def _invoke(
        self,
        model: Any,
        inputs: list[np.ndarray],
        *,
        origins: Sequence[pd.Timestamp],
        horizon: int,
    ) -> tuple[Any, Any]:
        """Run one library call for a batch of equal-length inputs.

        ``inputs`` is always a list owned by this call.  Without XReg the call is
        a plain ``forecast``; with XReg it is ``forecast_with_covariates`` carrying
        a single deterministic calendar-phase covariate per series, covering the
        context **and** the horizon (the library requires the future values, and a
        calendar is the only covariate that can be known in advance).

        Raises
        ------
        ForecastError
            If the XReg mode is requested without the ``timesfm[xreg]`` extra.
        """
        if not self._use_xreg:
            return cast("tuple[Any, Any]", model.forecast(horizon=horizon, inputs=list(inputs)))

        covariates = [
            _calendar_phase(origin, self._timeframe, len(series) + horizon)
            for origin, series in zip(origins, inputs, strict=True)
        ]
        try:
            return cast(
                "tuple[Any, Any]",
                model.forecast_with_covariates(
                    inputs=list(inputs),
                    dynamic_categorical_covariates={_CALENDAR_COVARIATE: covariates},
                    xreg_mode=self._xreg_mode,
                    normalize_xreg_target_per_input=True,
                ),
            )
        except ImportError as exc:
            raise ForecastError(
                f"the XReg covariate mode needs the optional extra: {TIMESFM_XREG_EXTRA_HINT}"
            ) from exc

    @staticmethod
    def _normalise(
        rows: np.ndarray, level_rows: tuple[int, ...], context: np.ndarray
    ) -> np.ndarray:
        """Return the requested decile rows, normalised to the origin close.

        The model forecasts the *level* of the de-seasonalised log price, while a
        trajectory stores an offset from the close of ``origin``: subtracting the
        last context value performs that conversion (and is a no-op for a caller
        that already passes an origin-relative series).

        Parameters
        ----------
        rows:
            ``(9, horizon)`` decile block of one series, median row included.
        level_rows:
            Index of each requested level among the nine deciles.
        context:
            The series handed to the model; its last value is the origin level.
        """
        selected = rows[list(level_rows), :]
        return np.asarray(selected - np.float32(context[-1]), dtype="float32")
