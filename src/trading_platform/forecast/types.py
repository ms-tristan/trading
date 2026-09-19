"""Value objects of the forecasting layer: requests, trajectories and their algebra.

These two frozen dataclasses are the only interface between a forecast backend
and everything that consumes a forecast (the artifact builder, the strategy and
the offline skill report).  The module deliberately depends on the standard
library, ``numpy``, ``pandas`` and :mod:`trading_platform.core.errors` only: it
never imports a machine-learning runtime, so it stays importable with the
``.[dev]`` extra alone.

Semantics of a trajectory
-------------------------
``quantiles[j, i]`` is the value of the quantile at level ``quantile_levels[j]``
for step ``i + 1`` after ``origin``, expressed as a **log offset from the origin
close**: ``0.0`` means "the close of ``origin`` is held".  A trajectory is
therefore a *normalised* path, which is what makes it comparable across symbols
and independent of the price level at ``origin``.  A backend that predicts a
positive series is responsible for converting it to that normalised scale before
building the trajectory.

Nothing in this module mutates its inputs, and every accessor returns a fresh
object, so a trajectory can be shared freely between the strategy, the reports
and the tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from trading_platform.core.errors import ForecastError

__all__ = [
    "DEFAULT_QUANTILE_LEVELS",
    "MEDIAN_LEVEL",
    "ForecastRequest",
    "ForecastTrajectory",
]

#: Quantile levels produced by default (the nine deciles of TimesFM 2.5).
DEFAULT_QUANTILE_LEVELS: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)

#: Level of the median, which every trajectory must carry.
MEDIAN_LEVEL: float = 0.5

#: Levels used by the interquartile helpers.
_LOWER_QUARTILE: float = 0.25
_UPPER_QUARTILE: float = 0.75


def _as_int(value: Any, *, name: str) -> int:
    """Return ``value`` as a plain ``int``, or raise :class:`ForecastError`."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ForecastError(f"{name} must be an integer, got {value!r}") from None
    if number != value:
        raise ForecastError(f"{name} must be an integer, got {value!r}")
    return number


@dataclass(frozen=True)
class ForecastRequest:
    """A single univariate forecast request.

    Attributes
    ----------
    origin:
        Timestamp of the **last context candle**.  The forecast covers the
        candles strictly after it, so a backend may only use information dated
        ``origin`` or earlier.
    context:
        The observed series the backend conditions on, **oldest first**.  The
        values are expected to be finite and at least two of them are expected;
        the validation lives in the backends (and in :class:`ForecastTrajectory`)
        because a request is a passive value object.

    Examples
    --------
    >>> request = ForecastRequest(
    ...     origin=pd.Timestamp("2024-01-01T00:00:00Z"), context=(0.0, 0.5, 1.0)
    ... )
    >>> request.context[-1]
    1.0
    """

    origin: pd.Timestamp
    context: tuple[float, ...]


@dataclass(frozen=True)
class ForecastTrajectory:
    """A validated, normalised forecast path with its quantiles.

    Attributes
    ----------
    origin:
        Timestamp of the last context candle (``0.0`` on the path is the close
        of that candle).
    timeframe:
        Candle duration of the context (``"1h"``, ``"4h"``, ...), used for
        labelling and for the reporting layer.
    horizon:
        Number of steps stored on the path (``>= 1``).
    quantile_levels:
        Strictly increasing levels inside ``(0, 1)``; the median level ``0.5``
        must be present.
    quantiles:
        ``float32`` array of shape ``(len(quantile_levels), horizon)``.  It is
        copied and cast by :meth:`__post_init__`, so the caller may reuse (or
        mutate) the array it passed in.

    Raises
    ------
    ForecastError
        If ``origin`` is ``NaT``, if ``horizon`` is smaller than one, if the
        levels are not a strictly increasing subset of ``(0, 1)`` containing
        ``0.5``, if the shape is not ``(len(quantile_levels), horizon)`` or if
        any value is not finite.
    """

    origin: pd.Timestamp
    timeframe: str
    horizon: int
    quantile_levels: tuple[float, ...]
    quantiles: np.ndarray

    def __post_init__(self) -> None:
        origin = _coerce_origin(self.origin)
        object.__setattr__(self, "origin", origin)

        horizon = _as_int(self.horizon, name="horizon")
        if horizon < 1:
            raise ForecastError(f"horizon must be >= 1, got {horizon}")
        object.__setattr__(self, "horizon", horizon)

        try:
            levels = tuple(float(level) for level in self.quantile_levels)
        except (TypeError, ValueError):
            raise ForecastError("quantile_levels must be a sequence of numbers") from None
        _validate_levels(levels)
        object.__setattr__(self, "quantile_levels", levels)

        try:
            array = np.array(self.quantiles, dtype="float32", copy=True)
        except (TypeError, ValueError):
            raise ForecastError("quantiles must be a numeric 2-D array") from None
        if array.ndim != 2:
            raise ForecastError(f"quantiles must be 2-D, got {array.ndim} dimension(s)")
        expected = (len(levels), horizon)
        if array.shape != expected:
            raise ForecastError(
                f"quantiles must have shape (n_levels, horizon) == {expected}, got {array.shape}"
            )
        if not bool(np.all(np.isfinite(array))):
            raise ForecastError("quantiles must not contain NaN or infinite values")
        object.__setattr__(self, "quantiles", array)

    # -- quantile access ---------------------------------------------------

    @property
    def median(self) -> np.ndarray:
        """Return the median path as a fresh ``(horizon,)`` ``float32`` copy."""
        return self.quantile(MEDIAN_LEVEL)

    def quantile(self, level: float) -> np.ndarray:
        """Return the path stored at ``level`` as a fresh ``float32`` copy.

        Raises
        ------
        ForecastError
            If ``level`` is not one of the stored :attr:`quantile_levels`.
        """
        index = self._level_index(level)
        return np.array(self.quantiles[index], dtype="float32", copy=True)

    def _level_index(self, level: float) -> int:
        """Return the row index of ``level``, or raise :class:`ForecastError`."""
        try:
            wanted = float(level)
        except (TypeError, ValueError):
            raise ForecastError(f"quantile level must be a number, got {level!r}") from None
        for index, stored in enumerate(self.quantile_levels):
            if stored == wanted:
                return index
        available = ", ".join(f"{stored:g}" for stored in self.quantile_levels)
        raise ForecastError(f"quantile level {wanted:g} is absent (available: {available})")

    # -- path algebra ------------------------------------------------------

    def alpha(self, k: int, *, offset: int = 0) -> float:
        """Return the expected normalised move over ``k`` steps from ``offset``.

        Parameters
        ----------
        k:
            Number of steps to look ahead (``>= 1``).
        offset:
            First step of the window, relative to the origin (``>= 0``).

        Raises
        ------
        ForecastError
            If ``k < 1``, ``offset < 0`` or the window leaves the path.
        """
        steps = _as_int(k, name="k")
        if steps < 1:
            raise ForecastError(f"k must be >= 1, got {steps}")
        start = _as_int(offset, name="offset")
        if start < 0:
            raise ForecastError(f"offset must be >= 0, got {start}")
        if start + steps > self.horizon:
            raise ForecastError(
                f"window [offset={start}, k={steps}] leaves the path of horizon {self.horizon}"
            )
        return float(self.median[start + steps - 1])

    def slope(self) -> float:
        """Return the average per-step drift of the median path."""
        return float(self.median[-1]) / self.horizon

    def mfe(self) -> float:
        """Return the predicted maximum favourable excursion (highest median value)."""
        return float(np.max(self.median))

    def mae(self) -> float:
        """Return the predicted maximum adverse excursion (lowest median value)."""
        return float(np.min(self.median))

    def path_length(self) -> float:
        """Return the travelled length of the median path (origin included)."""
        median = self.median.astype("float64")
        steps = np.abs(np.diff(median, prepend=0.0))
        return float(np.sum(steps, dtype="float64"))

    def path_efficiency(self) -> float:
        """Return ``net move / path length``, i.e. how straight the path is.

        A perfectly straight path scores ``1.0``; a path that wanders without
        progressing tends to ``0.0``.  A zero-length path scores ``0.0``.
        """
        length = self.path_length()
        if length == 0.0:
            return 0.0
        return float(self.median[-1]) / length

    def iqr(self) -> np.ndarray:
        """Return the interquartile range path (``q75 - q25``) as a ``float32`` copy.

        Raises
        ------
        ForecastError
            If the ``0.25`` or ``0.75`` level is not stored.
        """
        difference = self.quantile(_UPPER_QUARTILE) - self.quantile(_LOWER_QUARTILE)
        return np.asarray(difference, dtype="float32")

    def terminal_iqr(self) -> float:
        """Return the dispersion of the last step of the path."""
        return float(self.iqr()[-1])

    def per_bar_dispersion(self) -> float:
        """Return the terminal dispersion scaled to one step (``IQR / sqrt(horizon)``)."""
        return self.terminal_iqr() / math.sqrt(self.horizon)

    def reliability(self) -> float:
        """Return the predicted edge divided by the predicted per-step dispersion.

        This is the forecast-side analogue of a Sharpe ratio.  It returns ``0.0``
        when the dispersion is zero, and never ``inf`` or ``NaN``.
        """
        dispersion = self.per_bar_dispersion()
        if dispersion == 0.0:
            return 0.0
        value = self.alpha(self.horizon) / dispersion
        if not math.isfinite(value):
            return 0.0
        return value

    def agreement(self) -> float:
        """Return the fraction of stored deciles that agree with the terminal sign.

        Only the levels other than the median are considered: a value of ``1.0``
        means every stored decile ends on the same side of the origin close as
        the median does.  Returns ``0.0`` when the median move is exactly zero or
        when the trajectory stores no other level.
        """
        terminal = self.alpha(self.horizon)
        sign = float(np.sign(terminal))
        if sign == 0.0:
            return 0.0
        others = [
            index for index, level in enumerate(self.quantile_levels) if level != MEDIAN_LEVEL
        ]
        if not others:
            return 0.0
        signs = np.sign(self.quantiles[others, -1])
        return float(np.count_nonzero(signs == sign)) / float(len(others))

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view of the trajectory (lists of floats only).

        The dictionary holds the stored fields plus the path statistics that are
        always defined (``slope``, ``mfe``, ``mae``, ``path_length``,
        ``path_efficiency``, ``agreement``).  Dispersion statistics are left out
        on purpose: they need the ``0.25``/``0.75`` levels, which a decile-only
        trajectory does not carry, and a serialiser must never raise.
        """
        return {
            "origin": self.origin.isoformat(),
            "timeframe": self.timeframe,
            "horizon": self.horizon,
            "quantile_levels": [float(level) for level in self.quantile_levels],
            "median": [float(value) for value in self.median],
            "quantiles": [
                [float(value) for value in row]
                for row in np.asarray(self.quantiles, dtype="float64")
            ],
            "slope": self.slope(),
            "mfe": self.mfe(),
            "mae": self.mae(),
            "path_length": self.path_length(),
            "path_efficiency": self.path_efficiency(),
            "agreement": self.agreement(),
        }


def _coerce_origin(origin: Any) -> pd.Timestamp:
    """Return ``origin`` as a non-``NaT`` :class:`pandas.Timestamp`."""
    try:
        timestamp = pd.Timestamp(origin)
    except (TypeError, ValueError):
        raise ForecastError(f"forecast origin is not a timestamp: {origin!r}") from None
    if pd.isna(timestamp):
        raise ForecastError("forecast origin must be a valid timestamp, got NaT")
    return timestamp


def _validate_levels(levels: tuple[float, ...]) -> None:
    """Validate a quantile-level tuple, raising :class:`ForecastError` otherwise."""
    if not levels:
        raise ForecastError("quantile_levels must not be empty")
    previous: float | None = None
    for level in levels:
        if not math.isfinite(level) or not 0.0 < level < 1.0:
            raise ForecastError(f"quantile level must lie strictly inside (0, 1), got {level!r}")
        if previous is not None and level <= previous:
            raise ForecastError(
                f"quantile_levels must be strictly increasing, got {list(levels)!r}"
            )
        previous = level
    if MEDIAN_LEVEL not in levels:
        raise ForecastError(f"quantile_levels must contain the median level {MEDIAN_LEVEL}")
