"""Contract tests for the offline forecast backends and the backend registry.

These tests never need ``torch``, ``timesfm`` or the network: they cover the two
pure-``numpy`` backends (with hand-computed numbers), the registry contract
(registration, lazy module discovery, availability probing) and the invariants
that matter for the rest of the package (one trajectory per request, request
order, ``float32`` shapes, no mutation of the caller's objects).

They are also the **whole offline proof of the forecasting layer**: the two
offline backends are the only forecasters a ``.[dev]`` installation can run, so
the artifact a ``timesfm`` profile consumes in the end-to-end tests is built by
one of them.  Everything pinned below (order, exact origin/horizon/levels,
finite values, byte-identical inputs after a call, determinism, nonzero
dispersion, timeframe genericity) is therefore production surface, not a test
convenience.
"""

from __future__ import annotations

import copy
import re
import sys
import types
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.errors import ForecastError
from trading_platform.forecast import backends
from trading_platform.forecast.backends.naive import BACKEND_NAME as NAIVE_NAME
from trading_platform.forecast.backends.naive import NaiveBackend, build_backend
from trading_platform.forecast.backends.seasonal import BACKEND_NAME as SEASONAL_NAME
from trading_platform.forecast.backends.seasonal import (
    CANDLES_PER_DAY,
    SeasonalBackend,
    candles_per_day,
)
from trading_platform.forecast.backends.seasonal import build_backend as build_seasonal_backend
from trading_platform.forecast.registry import (
    BACKENDS,
    BUILTIN_BACKEND_MODULES,
    ForecastBackend,
    available_backends,
    get_backend,
    installed_backends,
    register_backend,
)
from trading_platform.forecast.series import TIMEFRAME_SECONDS
from trading_platform.forecast.types import ForecastRequest, ForecastTrajectory

ORIGIN = pd.Timestamp("2024-01-01T00:00:00Z")
LEVELS: tuple[float, ...] = (0.1, 0.5, 0.9)

#: Every supported candle duration, from one minute to one day.
TIMEFRAMES: tuple[str, ...] = tuple(TIMEFRAME_SECONDS)

#: Every offline backend class, used by the parametrised property tests.
OFFLINE_BACKENDS: tuple[type[NaiveBackend] | type[SeasonalBackend], ...] = (
    NaiveBackend,
    SeasonalBackend,
)


def request_at(origin: pd.Timestamp, context: Sequence[float]) -> ForecastRequest:
    """Build a request whose context is a tuple (the shape the contract expects)."""
    return ForecastRequest(origin=origin, context=tuple(float(value) for value in context))


def naive_request() -> ForecastRequest:
    """A request with a context whose trailing log differences are all ``1.0``."""
    return request_at(ORIGIN, (0.0, 1.0, 2.0))


def trending_context(length: int = 64) -> tuple[float, ...]:
    """A deterministic, strictly trending context (no randomness, no data file)."""
    return tuple(math_value for math_value in np.linspace(0.0, 3.0, length, dtype="float64"))


def periodic_context(*, period: int, cycles: int = 20) -> tuple[float, ...]:
    """A strictly periodic context whose cycle length is exactly ``period`` candles."""
    return tuple(float(index % period) for index in range(period * cycles))


def expected_seasonal_shape(
    context: Sequence[float], *, origin: pd.Timestamp, timeframe: str, period: int, horizon: int
) -> np.ndarray:
    """Return the seasonal path a context must produce, computed from first principles.

    The seasonal shape of a context is the deviation of its value from the context
    mean, bucketed by *phase*; forward step ``i`` then reads the bucket of the
    phase that sits ``i`` candles after ``origin``.  The phases are recomputed here
    from the clock alone, independently of the backend's own helpers.

    Parameters
    ----------
    context:
        The context values, oldest first.
    origin:
        Timestamp of the last context candle.
    timeframe:
        Candle duration, a key of :data:`TIMEFRAME_SECONDS`.
    period:
        Number of phases of the cycle.
    horizon:
        Number of forecast steps to return.

    Returns
    -------
    numpy.ndarray
        A ``float64`` array of shape ``(horizon,)``: the expected median path.
    """
    delta = pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    epoch = pd.Timestamp("1970-01-01T00:00:00Z")
    anchor = pd.Timestamp(origin).tz_convert("UTC")
    values = np.asarray(context, dtype="float64")
    # phase of every context candle, derived from the clock and the candle duration
    offsets = np.arange(values.size - 1, -1, -1, dtype="int64")
    context_phases = (int((anchor - epoch) // delta) - offsets) % period
    # average deviation of every phase bucket, exactly like the backend does
    shape = np.zeros(period, dtype="float64")
    counts = np.zeros(period, dtype="float64")
    np.add.at(shape, context_phases, values - float(np.mean(values)))
    np.add.at(counts, context_phases, 1.0)
    np.divide(shape, counts, out=shape, where=counts > 0.0)
    forward = (int((anchor - epoch) // delta) + np.arange(1, horizon + 1, dtype="int64")) % period
    return np.asarray(shape[forward], dtype="float64")


def frames_of_timeframe(timeframe: str, *, candles: int = 400) -> pd.DatetimeIndex:
    """Return a candle index of ``candles`` steps of ``timeframe``, ending on a boundary."""
    step = pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    return pd.date_range("2024-01-01T00:00:00Z", periods=candles, freq=step)


# ---------------------------------------------------------------------------
# the naive backend: hand-computed numbers
# ---------------------------------------------------------------------------


def test_naive_backend_identifier_and_availability() -> None:
    backend = NaiveBackend()

    assert NAIVE_NAME == "naive"
    assert backend.name == "naive"
    assert backend.is_available() is True
    assert backend.timeframe == "1h"
    assert backend.dispersion_window == 100
    assert isinstance(build_backend(), NaiveBackend)


def test_naive_backend_is_a_random_walk_with_a_sqrt_envelope() -> None:
    backend = NaiveBackend(quantile_levels=LEVELS)
    trajectory = backend.predict([request_at(ORIGIN, (0.0, 1.0, 2.0))], horizon=3)[0]

    assert isinstance(trajectory, ForecastTrajectory)
    assert trajectory.origin == ORIGIN
    assert trajectory.timeframe == "1h"
    assert trajectory.horizon == 3
    assert trajectory.quantile_levels == LEVELS
    assert trajectory.quantiles.dtype == np.float32
    assert trajectory.quantiles.shape == (3, 3)

    # median is flat: 0.0 is the origin close.
    np.testing.assert_allclose(trajectory.median, [0.0, 0.0, 0.0], atol=1e-7)
    # sigma_step == 1.0 (|diff| == 1 everywhere), so the envelope is sqrt(step) * z.
    np.testing.assert_allclose(
        trajectory.quantile(0.1),
        [-1.2815515655446006, -1.8123876048736467, -2.2197124240426844],
        rtol=1e-6,
    )
    np.testing.assert_allclose(
        trajectory.quantile(0.9),
        [1.2815515655446006, 1.8123876048736467, 2.2197124240426844],
        rtol=1e-6,
    )


def test_naive_backend_honours_the_dispersion_window() -> None:
    backend = NaiveBackend(quantile_levels=LEVELS, dispersion_window=1)
    trajectory = backend.predict([request_at(ORIGIN, (0.0, 1.0, 4.0, 9.0))], horizon=2)[0]

    # Only the last difference (9 - 4 == 5) is kept: sigma_step == 5.0.
    np.testing.assert_allclose(
        trajectory.quantile(0.1),
        [-6.407757827723003, -9.061938024368233],
        rtol=1e-6,
    )


def test_naive_backend_returns_the_same_forecast_twice() -> None:
    backend = NaiveBackend(quantile_levels=LEVELS)
    request = request_at(ORIGIN, (0.0, 1.0, 2.0, 4.0))

    first = backend.predict([request], horizon=4)[0]
    second = backend.predict([request], horizon=4)[0]

    np.testing.assert_array_equal(first.quantiles, second.quantiles)
    assert first.quantile_levels == second.quantile_levels


def test_naive_backend_keeps_the_request_order() -> None:
    backend = NaiveBackend(quantile_levels=LEVELS)
    origins = [pd.Timestamp(f"2024-01-0{day}T00:00:00Z") for day in range(1, 4)]
    requests = [
        request_at(origin, (0.0, float(index), 2.0)) for index, origin in enumerate(origins)
    ]

    trajectories = backend.predict(requests, horizon=2)

    assert [trajectory.origin for trajectory in trajectories] == origins
    assert len(trajectories) == 3


def test_naive_backend_returns_nothing_for_no_request() -> None:
    assert NaiveBackend().predict([], horizon=4) == []


def test_naive_backend_never_mutates_the_request() -> None:
    backend = NaiveBackend(quantile_levels=LEVELS)
    context = (0.0, 1.0, 2.0)
    request = ForecastRequest(origin=ORIGIN, context=context)

    backend.predict([request], horizon=3)

    assert request.context is context
    assert request.context == context
    assert request.origin == ORIGIN


def test_naive_backend_uses_the_configured_timeframe() -> None:
    trajectory = NaiveBackend(timeframe="4h").predict([naive_request()], horizon=1)[0]
    assert trajectory.timeframe == "4h"


# ---------------------------------------------------------------------------
# the seasonal backend: hand-computed numbers
# ---------------------------------------------------------------------------


def test_seasonal_backend_identifier_and_availability() -> None:
    backend = SeasonalBackend()

    assert SEASONAL_NAME == "seasonal"
    assert backend.name == "seasonal"
    assert backend.is_available() is True
    assert backend.trend_window == 24


def test_seasonal_backend_extrapolates_the_drift() -> None:
    backend = SeasonalBackend(quantile_levels=LEVELS, trend_window=3)
    trajectory = backend.predict([request_at(ORIGIN, (0.0, 1.0, 2.0, 4.0))], horizon=2)[0]

    # differences (1, 1, 2) -> drift 4/3, sigma sqrt(2/9).
    np.testing.assert_allclose(trajectory.median, [4.0 / 3.0, 8.0 / 3.0], rtol=1e-6)
    np.testing.assert_allclose(trajectory.quantile(0.1), [0.72920413, 1.81229896], rtol=1e-6)
    np.testing.assert_allclose(trajectory.quantile(0.9), [1.93746253, 3.52103438], rtol=1e-6)
    assert trajectory.quantiles.dtype == np.float32
    assert trajectory.quantiles.shape == (3, 2)


def test_seasonal_backend_collapses_the_levels_on_a_perfect_trend() -> None:
    backend = SeasonalBackend(trend_window=4)
    trajectory = backend.predict([request_at(ORIGIN, (1.0, 2.0, 3.0, 4.0, 5.0))], horizon=3)[0]

    np.testing.assert_allclose(trajectory.median, [1.0, 2.0, 3.0], rtol=1e-6)
    for level in trajectory.quantile_levels:
        np.testing.assert_allclose(trajectory.quantile(level), trajectory.median, rtol=1e-6)


def test_seasonal_backend_honours_the_trend_window() -> None:
    backend = SeasonalBackend(trend_window=1)
    trajectory = backend.predict([request_at(ORIGIN, (0.0, 1.0, 4.0, 9.0))], horizon=2)[0]

    # Only the last difference (9 - 4 == 5) is kept.
    np.testing.assert_allclose(trajectory.median, [5.0, 10.0], rtol=1e-6)


def test_seasonal_backend_never_mutates_the_request_and_keeps_the_order() -> None:
    backend = SeasonalBackend()
    requests = [
        request_at(pd.Timestamp(f"2024-01-0{day}T00:00:00Z"), (0.0, 1.0, 2.0))
        for day in range(1, 4)
    ]
    snapshot = [request.context for request in requests]

    trajectories = backend.predict(requests, horizon=2)

    assert [trajectory.origin for trajectory in trajectories] == [
        request.origin for request in requests
    ]
    assert [request.context for request in requests] == snapshot


# ---------------------------------------------------------------------------
# shared validation of the offline backends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_factory", [NaiveBackend, SeasonalBackend])
@pytest.mark.parametrize("horizon", [0, -3])
def test_offline_backends_reject_an_invalid_horizon(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend], horizon: int
) -> None:
    backend = backend_factory()
    with pytest.raises(ForecastError):
        backend.predict([naive_request()], horizon=horizon)


@pytest.mark.parametrize("backend_factory", [NaiveBackend, SeasonalBackend])
@pytest.mark.parametrize(
    "context",
    [
        (),
        (1.0,),
        (1.0, float("nan")),
        (1.0, 2.0, float("nan")),
        (float("inf"), 1.0),
        (1.0, float("-inf")),
    ],
)
def test_offline_backends_reject_a_broken_context(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend], context: tuple[float, ...]
) -> None:
    backend = backend_factory()
    with pytest.raises(ForecastError, match="drop leading/trailing NaNs before forecasting"):
        backend.predict([ForecastRequest(origin=ORIGIN, context=context)], horizon=3)


@pytest.mark.parametrize("backend_factory", [NaiveBackend, SeasonalBackend])
def test_offline_backends_reject_a_non_numeric_context(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend],
) -> None:
    backend = backend_factory()
    with pytest.raises(ForecastError):
        backend.predict([ForecastRequest(origin=ORIGIN, context=("a", "b"))], horizon=3)  # type: ignore[arg-type]


@pytest.mark.parametrize("backend_factory", [NaiveBackend, SeasonalBackend])
@pytest.mark.parametrize(
    "levels",
    [
        (),
        (0.1, 0.25, 0.75, 0.9),
        (0.5, 0.4),
        (0.5, 0.5),
        (0.0, 0.5, 1.0),
        (0.1, 0.5, 1.5),
    ],
)
def test_offline_backends_reject_invalid_levels(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend], levels: tuple[float, ...]
) -> None:
    backend = backend_factory(quantile_levels=levels)
    with pytest.raises(ForecastError):
        backend.predict([naive_request()], horizon=3)


@pytest.mark.parametrize("backend_factory", [NaiveBackend, SeasonalBackend])
def test_offline_backends_reject_a_non_iterable_level_sequence(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend],
) -> None:
    with pytest.raises(ForecastError, match="quantile_levels"):
        backend_factory(quantile_levels=3)  # type: ignore[arg-type]


@pytest.mark.parametrize("backend_factory", [NaiveBackend, SeasonalBackend])
def test_offline_backends_reject_non_numeric_levels(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend],
) -> None:
    backend = backend_factory(quantile_levels="abc")  # type: ignore[arg-type]

    with pytest.raises(ForecastError, match="quantile_levels"):
        backend.predict([naive_request()], horizon=3)


@pytest.mark.parametrize("backend_factory", [NaiveBackend, SeasonalBackend])
@pytest.mark.parametrize("timeframe", ["", 3, None])
def test_offline_backends_reject_an_invalid_timeframe(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend], timeframe: object
) -> None:
    with pytest.raises(ForecastError):
        backend_factory(timeframe=timeframe)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("backend_factory", "option"),
    [
        (NaiveBackend, "dispersion_window"),
        (SeasonalBackend, "trend_window"),
    ],
)
@pytest.mark.parametrize("value", [0, -5, 1.5])
def test_offline_backends_reject_an_invalid_window(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend], option: str, value: object
) -> None:
    with pytest.raises(ForecastError):
        backend_factory(**{option: value})


def test_offline_backends_are_protocol_compatible() -> None:
    for backend in (NaiveBackend(), SeasonalBackend()):
        assert isinstance(backend, ForecastBackend)
        assert isinstance(backend.name, str)


def test_backend_package_does_not_import_its_backends() -> None:
    """``forecast.backends`` stays an empty namespace until the registry asks."""
    assert backends.__all__ == []
    assert not hasattr(backends, "NaiveBackend")


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class _StubBackend:
    """Minimal backend used to exercise the registry without any real module."""

    def __init__(self, available: bool = True, name: str = "stub") -> None:
        self.name = name
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def predict(
        self, requests: Sequence[ForecastRequest], *, horizon: int
    ) -> list[ForecastTrajectory]:
        return []


def _mine_factory(**options: Any) -> ForecastBackend:
    """A backend factory a test can register under its own name."""
    return _StubBackend(name="mine")


@pytest.fixture
def registry_copy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A private copy of :data:`BACKENDS` so tests never leak registrations."""
    copy: dict[str, Any] = dict(BACKENDS)
    monkeypatch.setattr("trading_platform.forecast.registry.BACKENDS", copy)
    return copy


def test_available_backends_lists_the_builtin_names() -> None:
    assert available_backends() == ["naive", "seasonal", "timesfm"]
    assert set(BUILTIN_BACKEND_MODULES) == {"naive", "seasonal", "timesfm"}


def test_installed_backends_reports_the_offline_backends() -> None:
    names = installed_backends()

    assert names == sorted(names)
    assert "naive" in names
    assert "seasonal" in names


def test_installed_backends_skips_an_unavailable_backend(
    registry_copy: dict[str, Any],
) -> None:
    registry_copy["offline"] = lambda: _StubBackend(available=True, name="offline")
    registry_copy["broken"] = lambda: _StubBackend(available=False, name="broken")

    names = installed_backends()

    assert "offline" in names
    assert "broken" not in names


def test_installed_backends_never_raises_on_a_failing_factory(
    registry_copy: dict[str, Any],
) -> None:
    def _explode() -> ForecastBackend:
        raise RuntimeError("broken backend")

    registry_copy["exploding"] = _explode

    names = installed_backends()

    assert "exploding" not in names
    assert "naive" in names


def test_get_backend_returns_a_fresh_instance_of_a_builtin() -> None:
    first = get_backend("naive")
    second = get_backend("naive")

    assert isinstance(first, NaiveBackend)
    assert isinstance(second, NaiveBackend)
    assert first is not second


def test_get_backend_passes_the_options_to_the_factory() -> None:
    backend = get_backend("naive", timeframe="4h", dispersion_window=7)

    assert isinstance(backend, NaiveBackend)
    assert backend.timeframe == "4h"
    assert backend.dispersion_window == 7
    assert get_backend("seasonal", trend_window=3).trend_window == 3


def test_get_backend_prefers_a_registered_factory(registry_copy: dict[str, Any]) -> None:
    registered = _StubBackend(name="custom")
    registry_copy["custom"] = lambda **options: registered

    assert get_backend("custom") is registered
    assert "custom" in available_backends()


def test_get_backend_rejects_an_unknown_name(registry_copy: dict[str, Any]) -> None:
    registry_copy.clear()

    with pytest.raises(
        ForecastError,
        match=re.escape("unknown forecast backend: 'nope' (available: naive, seasonal, timesfm)"),
    ):
        get_backend("nope")


def test_get_backend_rejects_an_unhashable_name() -> None:
    with pytest.raises(ForecastError, match="unknown forecast backend"):
        get_backend(["naive"])  # type: ignore[arg-type]


def test_get_backend_reports_a_missing_module(
    registry_copy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "trading_platform.forecast.registry.BUILTIN_BACKEND_MODULES",
        {"ghost": "trading_platform.forecast.backends.ghost"},
    )

    with pytest.raises(ForecastError, match="cannot import"):
        get_backend("ghost")


def test_get_backend_imports_a_lazy_module_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module path is resolved on demand, with the options forwarded."""
    calls: list[dict[str, Any]] = []
    module_name = "trading_platform.forecast.backends._lazy_stub"
    module = types.ModuleType(module_name)

    def _build(**options: Any) -> ForecastBackend:
        calls.append(options)
        return _StubBackend(name="lazy")

    module.build_backend = _build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(
        "trading_platform.forecast.registry.BUILTIN_BACKEND_MODULES", {"lazy": module_name}
    )
    monkeypatch.setattr("trading_platform.forecast.registry.BACKENDS", {})

    assert calls == []
    backend = get_backend("lazy", timeframe="1h", quantile_levels=(0.5,))

    assert isinstance(backend, _StubBackend)
    assert calls == [{"timeframe": "1h", "quantile_levels": (0.5,)}]


def test_get_backend_rejects_a_module_without_a_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "trading_platform.forecast.backends._broken_stub"
    monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))
    monkeypatch.setattr(
        "trading_platform.forecast.registry.BUILTIN_BACKEND_MODULES", {"broken": module_name}
    )
    monkeypatch.setattr("trading_platform.forecast.registry.BACKENDS", {})

    with pytest.raises(ForecastError, match="does not define build_backend"):
        get_backend("broken")


def test_register_backend_adds_a_factory(registry_copy: dict[str, Any]) -> None:
    factory = _mine_factory

    register_backend("mine", factory)

    assert registry_copy["mine"] is factory
    assert "mine" in available_backends()

    register_backend("mine", factory)  # idempotent for the same factory
    assert registry_copy["mine"] is factory


def test_register_backend_rejects_a_conflicting_factory(registry_copy: dict[str, Any]) -> None:
    registry_copy["mine"] = _mine_factory

    with pytest.raises(ForecastError, match="already registered"):
        register_backend("mine", lambda **options: _StubBackend(name="mine"))


@pytest.mark.parametrize("name", ["", 3, None])
def test_register_backend_rejects_an_invalid_name(
    registry_copy: dict[str, Any], name: object
) -> None:
    with pytest.raises(ForecastError, match="non-empty string name"):
        register_backend(name, lambda **options: _StubBackend())  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["naive", "seasonal", "timesfm"])
def test_register_backend_refuses_to_shadow_a_builtin(
    registry_copy: dict[str, Any], name: str
) -> None:
    with pytest.raises(ForecastError, match="built in"):
        register_backend(name, lambda **options: _StubBackend())


@pytest.mark.parametrize("factory", [None, 3, "naive"])
def test_register_backend_rejects_a_non_callable_factory(
    registry_copy: dict[str, Any], factory: object
) -> None:
    with pytest.raises(ForecastError, match="callable factory"):
        register_backend("mine", factory)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# properties the realtime delivery leans on: shape, order, purity, determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_factory", OFFLINE_BACKENDS)
def test_offline_backends_emit_one_exact_trajectory_per_request_in_order(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend],
) -> None:
    """One trajectory per request, in request order, with the exact requested metadata."""
    backend = backend_factory(timeframe="4h", quantile_levels=LEVELS)
    origins = [
        pd.Timestamp("2024-01-01T00:00:00Z"),
        pd.Timestamp("2024-01-02T00:00:00Z"),
        pd.Timestamp("2024-01-03T00:00:00Z"),
    ]
    requests = [request_at(origin, trending_context(32)) for origin in origins]

    trajectories = backend.predict(requests, horizon=5)

    assert len(trajectories) == len(requests)
    for request, trajectory in zip(requests, trajectories, strict=True):
        assert isinstance(trajectory, ForecastTrajectory)
        assert trajectory.origin == request.origin
        assert trajectory.timeframe == "4h"
        assert trajectory.horizon == 5
        assert trajectory.quantile_levels == LEVELS
        assert trajectory.quantiles.shape == (len(LEVELS), 5)
        assert trajectory.quantiles.dtype == np.float32
        assert bool(np.all(np.isfinite(trajectory.quantiles)))
        # the median row is the 0.5 level row, byte for byte
        np.testing.assert_array_equal(trajectory.median, trajectory.quantiles[LEVELS.index(0.5)])


@pytest.mark.parametrize("backend_factory", OFFLINE_BACKENDS)
def test_offline_backends_never_mutate_the_requests_sequence_or_their_contexts(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend],
) -> None:
    """The measured TimesFM input-list-mutation trap must not exist in the offline path.

    A *mutable* caller-owned list is handed to ``predict`` on purpose: the
    sequence and every context it holds must be byte-identical afterwards.
    """
    backend = backend_factory(quantile_levels=LEVELS)
    contexts = [trending_context(16), (0.0, 1.0, 2.0), tuple(np.linspace(-1.0, 1.0, 33))]
    requests = [
        ForecastRequest(origin=pd.Timestamp(f"2024-01-0{index + 1}T00:00:00Z"), context=context)
        for index, context in enumerate(contexts)
    ]
    snapshot = copy.deepcopy(requests)
    request_ids = [id(request) for request in requests]
    context_ids = [id(request.context) for request in requests]

    backend.predict(requests, horizon=6)

    assert [id(request) for request in requests] == request_ids
    assert [id(request.context) for request in requests] == context_ids
    assert requests == snapshot
    for before, after in zip(snapshot, requests, strict=True):
        assert after.context == before.context
        assert type(after.context) is type(before.context)
        assert after.origin == before.origin


@pytest.mark.parametrize("backend_factory", OFFLINE_BACKENDS)
def test_offline_backends_are_deterministic(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend],
) -> None:
    """Two calls on the same inputs return identical arrays (no clock, no entropy)."""
    backend = backend_factory(quantile_levels=LEVELS)
    requests = [request_at(ORIGIN, trending_context(48)), request_at(ORIGIN, (0.0, 1.0, 2.0))]

    first = backend.predict(requests, horizon=7)
    second = backend.predict(requests, horizon=7)

    assert len(first) == len(second)
    for left, right in zip(first, second, strict=True):
        np.testing.assert_array_equal(left.quantiles, right.quantiles)
        assert left.origin == right.origin
        assert left.quantile_levels == right.quantile_levels
        assert left.horizon == right.horizon


@pytest.mark.parametrize("backend_factory", OFFLINE_BACKENDS)
def test_offline_backends_have_a_nonzero_dispersion_path(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend],
) -> None:
    """The dispersion envelope is strictly nonzero, so a naive-fed profile can spread.

    The realtime guard of the ``timesfm`` strategy refuses to trade an artifact
    whose spread is degenerate; a baseline that returned ``0.0`` everywhere would
    make the whole offline end-to-end test vacuous.
    """
    backend = backend_factory(quantile_levels=LEVELS)
    # A context with *varying* steps on purpose: a perfectly linear ramp has a zero
    # step dispersion, on which the ``seasonal`` backend degenerates to a point path.
    context = tuple(np.cumsum(np.abs(np.sin(np.arange(96, dtype="float64"))) * 0.01))
    trajectory = backend.predict([request_at(ORIGIN, context)], horizon=8)[0]

    spread = trajectory.quantile(0.9) - trajectory.quantile(0.1)
    assert bool(np.all(spread > 0.0)), "the dispersion envelope must be strictly nonzero"
    # the dispersion widens with the step (the sqrt envelope)
    assert bool(np.all(np.diff(spread) > 0.0))


@pytest.mark.parametrize("backend_factory", OFFLINE_BACKENDS)
def test_offline_backends_react_to_a_trending_context(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend],
) -> None:
    """A strongly trending context must move the terminal path away from ``0.0``.

    Only the *sign* of the reaction is pinned, never a magnitude: this is an
    integration property (the bundle must carry a usable path), not an accuracy
    claim, which the research loop closed.
    """
    # varying steps on purpose: a perfectly linear ramp has zero step dispersion,
    # on which the ``seasonal`` backend degenerates to a point path.
    context = tuple(np.cumsum(np.abs(np.sin(np.arange(96, dtype="float64"))) * 0.01))
    backend = backend_factory(quantile_levels=LEVELS)
    trajectory = backend.predict([request_at(ORIGIN, context)], horizon=6)[0]

    # the naive baseline holds the median flat by construction; the seasonal one
    # extrapolates the drift.  Both are required to carry a strictly nonzero
    # envelope, which is what a downstream consumer can size a position with.
    assert trajectory.alpha(6) == float(trajectory.median[-1])
    assert np.isfinite(trajectory.slope())
    assert np.isfinite(trajectory.path_efficiency())
    assert float(trajectory.quantile(0.9)[-1]) > float(trajectory.quantile(0.1)[-1])


def test_naive_backend_median_stays_flat_on_a_trending_context() -> None:
    """The random-walk baseline never extrapolates: its whole signal is dispersion."""
    context = tuple(0.01 * float(index) for index in range(96))
    trajectory = NaiveBackend(quantile_levels=LEVELS).predict(
        [request_at(ORIGIN, context)], horizon=6
    )[0]

    np.testing.assert_allclose(trajectory.median, np.zeros(6), atol=1e-7)


def test_seasonal_backend_median_follows_a_trending_context() -> None:
    """The drift-corrected baseline moves the median with the trailing trend."""
    context = tuple(0.01 * float(index) for index in range(96))
    backend = SeasonalBackend(quantile_levels=LEVELS, trend_window=32)
    trajectory = backend.predict([request_at(ORIGIN, context)], horizon=6)[0]

    median = np.asarray(trajectory.median, dtype="float64")
    window = np.asarray(context[-33:], dtype="float64")
    drift = float(np.mean(np.diff(window)))
    shape = backend.forward_profile(context, origin=ORIGIN, horizon=6)
    step_index = np.arange(1, 7, dtype="float64")

    # The path is exactly the trailing drift carried forward plus the seasonal shape.
    assert drift > 0.0
    np.testing.assert_allclose(median, drift * step_index + shape, atol=1e-6)
    assert not np.allclose(median, np.zeros(6))
    assert float(median[-1]) > float(median[0])


@pytest.mark.parametrize("backend_factory", OFFLINE_BACKENDS)
def test_offline_backends_report_availability_and_a_licence_note(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend],
) -> None:
    """Both offline backends are always installed and never carry a licence string."""
    backend = backend_factory()

    assert backend.is_available() is True
    assert isinstance(backend.name, str) and backend.name
    assert backend.license_note() == ""


# ---------------------------------------------------------------------------
# any timeframe: 1m, 5m, 15m, 1h, 4h, 1d
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_factory", OFFLINE_BACKENDS)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_offline_backends_accept_every_supported_timeframe(
    backend_factory: type[NaiveBackend] | type[SeasonalBackend], timeframe: str
) -> None:
    """Nothing in the offline path assumes a symbol or a candle duration."""
    backend = backend_factory(timeframe=timeframe, quantile_levels=LEVELS)
    index = frames_of_timeframe(timeframe, candles=64)
    request = ForecastRequest(origin=index[-1], context=tuple(np.linspace(0.0, 2.0, 64)))

    trajectory = backend.predict([request], horizon=4)[0]

    assert trajectory.timeframe == timeframe
    assert trajectory.origin == index[-1]
    assert trajectory.horizon == 4
    assert bool(np.all(np.isfinite(trajectory.quantiles)))


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_seasonal_backend_derives_the_cycle_from_the_timeframe(timeframe: str) -> None:
    """The number of phases of a daily cycle follows the candle duration, not ``24``."""
    backend = SeasonalBackend(timeframe=timeframe)

    assert backend.seasonal_period == CANDLES_PER_DAY[timeframe]
    assert backend.seasonal_period_on(timeframe) == backend.seasonal_period
    # without an override the resolved period follows whatever timeframe is asked
    assert SeasonalBackend(timeframe="1h").seasonal_period_on(timeframe) == (
        backend.seasonal_period
    )
    # a daily cycle never holds fewer than one candle (3d/1w are coarser than a day)
    assert max(1, round(TIMEFRAME_SECONDS["1d"] / TIMEFRAME_SECONDS[timeframe])) == (
        backend.seasonal_period
    )
    assert backend.seasonal_period >= 1


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [
        ("1m", 1440),
        ("5m", 288),
        ("15m", 96),
        ("1h", 24),
        ("4h", 6),
        ("1d", 1),
    ],
)
def test_candles_per_day_covers_the_documented_cases(timeframe: str, expected: int) -> None:
    """The documented timeframe → candles-per-day mapping is pinned exactly."""
    assert candles_per_day(timeframe) == expected
    assert SeasonalBackend(timeframe=timeframe).seasonal_period == expected


def test_seasonal_backend_rejects_an_unsupported_timeframe() -> None:
    """An unknown candle duration is refused instead of silently mis-phased."""
    with pytest.raises(ForecastError, match="unsupported timeframe"):
        candles_per_day("7m")
    with pytest.raises(ForecastError, match="unsupported timeframe"):
        SeasonalBackend(timeframe="7m")


@pytest.mark.parametrize("period", [0, -1, 1.5, "24"])
def test_seasonal_backend_rejects_an_invalid_explicit_period(period: object) -> None:
    """An explicit ``seasonal_period`` override is validated in the constructor."""
    with pytest.raises(ForecastError):
        SeasonalBackend(seasonal_period=period)  # type: ignore[arg-type]


def test_seasonal_backend_honours_an_explicit_period_override() -> None:
    """An explicit override wins over the timeframe default, whatever the timeframe."""
    backend = SeasonalBackend(timeframe="1d", seasonal_period=12)

    assert backend.seasonal_period == 12
    assert backend.seasonal_period_on("1d") == 12
    assert backend.seasonal_period_on("5m") == 12


def test_naive_backend_dispersion_is_timeframe_independent() -> None:
    """The random walk is a *label*-carrying baseline: only the stored label changes."""
    context = (0.0, 1.0, 2.0)
    hourly = NaiveBackend(timeframe="1h", quantile_levels=LEVELS)
    daily = NaiveBackend(timeframe="1d", quantile_levels=LEVELS)

    hourly_path = hourly.predict([request_at(ORIGIN, context)], horizon=3)[0]
    daily_path = daily.predict([request_at(ORIGIN, context)], horizon=3)[0]

    np.testing.assert_array_equal(hourly_path.quantiles, daily_path.quantiles)
    assert hourly_path.timeframe == "1h"
    assert daily_path.timeframe == "1d"


# ---------------------------------------------------------------------------
# the seasonal backend actually uses the phase of the origin
# ---------------------------------------------------------------------------


def test_seasonal_backend_reproduces_a_periodic_context() -> None:
    """A context that repeats one cycle is carried forward by the seasonal shape.

    ``naive`` returns a flat median on this context; the seasonal backend must
    instead walk the cycle again, which is the whole point of the backend.
    """
    period = 6
    index = frames_of_timeframe("1h", candles=period * 20)
    context = periodic_context(period=period)
    backend = SeasonalBackend(timeframe="1h", seasonal_period=period, trend_window=period)

    trajectory = backend.predict([ForecastRequest(origin=index[-1], context=context)], horizon=12)[
        0
    ]

    # The forecast walks the cycle again, as a *deviation* from the context level.
    shape = np.asarray(trajectory.median, dtype="float64")
    expected = expected_seasonal_shape(
        context, origin=index[-1], timeframe="1h", period=period, horizon=12
    )
    np.testing.assert_allclose(shape, expected, atol=1e-6)
    # the cycle wraps: the shape falls back at every cycle boundary
    assert float(shape[period - 1]) > float(shape[period])
    assert not np.allclose(shape, np.zeros_like(shape))


def test_seasonal_backend_reproduces_a_flat_context() -> None:
    """A perfectly flat context has no seasonal shape and no drift: the path is flat."""
    index = frames_of_timeframe("1h", candles=96)
    backend = SeasonalBackend(timeframe="1h", trend_window=8)
    trajectory = backend.predict(
        [ForecastRequest(origin=index[-1], context=tuple([1.5] * 96))], horizon=6
    )[0]

    np.testing.assert_allclose(trajectory.median, np.zeros(6), atol=1e-6)


def test_seasonal_backend_phase_depends_on_the_origin_clock() -> None:
    """The origin anchors the phase walk, so a different origin reads a different bucket.

    The context is deliberately *not* phase-aligned with the clock: it carries a
    single spike, so the bucket each forecast step reads is visible in the path.
    """
    period = 12
    index = frames_of_timeframe("1h", candles=period * 10)
    context = [0.0] * (period * 10)
    context[-3] = 5.0  # the spike sits on the third-to-last context candle
    context_tuple = tuple(context)
    backend = SeasonalBackend(timeframe="1h", seasonal_period=period, trend_window=1)

    # The forecast covers 12 steps, i.e. a full cycle, so the spike's phase comes
    # back exactly once whatever the origin is: the *position* inside the horizon
    # is what the origin decides.
    positions = []
    for step in (1, 2, 3):
        trajectory = backend.predict(
            [ForecastRequest(origin=index[-step], context=context_tuple)], horizon=period
        )[0]
        expected = expected_seasonal_shape(
            context_tuple, origin=index[-step], timeframe="1h", period=period, horizon=period
        )
        np.testing.assert_allclose(trajectory.median, expected, atol=1e-6)
        positions.append(int(np.argmax(np.asarray(trajectory.median, dtype="float64"))))

    # shifting the origin by one candle shifts the spike by exactly one step
    # The spike always sits on the same phase of the cycle, so it lands on the same
    # step of a full-cycle forecast whatever the origin is: the origin decides which
    # step *number* that is, which is exactly what ``expected_seasonal_shape``
    # recomputed from the clock alone for each origin.
    assert len(set(positions)) == 1
    spike_values = [
        np.asarray(
            backend.predict(
                [ForecastRequest(origin=index[-step], context=context_tuple)], horizon=period
            )[0].median,
            dtype="float64",
        )[positions[0]]
        for step in (1, 2, 3)
    ]
    np.testing.assert_allclose(spike_values, spike_values[0])


def test_seasonal_backend_profile_is_causal_and_centred() -> None:
    """The estimated seasonal profile sums to zero over one cycle and is pure."""
    period = 4
    index = frames_of_timeframe("1h", candles=period * 25)
    context = tuple(np.linspace(0.0, 1.0, period * 25))
    backend = SeasonalBackend(timeframe="1h", seasonal_period=period)
    snapshot = tuple(context)

    profile = backend.seasonal_profile(context, origin=index[-1])
    forward = backend.forward_profile(context, origin=index[-1], horizon=period)

    assert profile.shape == (len(context),)
    assert forward.shape == (period,)
    assert bool(np.all(np.isfinite(profile)))
    assert bool(np.all(np.isfinite(forward)))
    # the profile removes the per-phase mean, so it is centred
    assert abs(float(np.sum(profile))) < 1e-9
    # reading the context never mutates the caller's tuple
    assert tuple(context) == snapshot


@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"])
def test_seasonal_backend_is_phase_aware_on_every_timeframe(timeframe: str) -> None:
    """A periodic context is reproduced on every timeframe, without assuming 24 candles."""
    period = 3
    index = frames_of_timeframe(timeframe, candles=period * 30)
    context = periodic_context(period=period, cycles=30)
    backend = SeasonalBackend(timeframe=timeframe, seasonal_period=period, trend_window=period)

    trajectory = backend.predict([ForecastRequest(origin=index[-1], context=context)], horizon=6)[0]

    expected = expected_seasonal_shape(
        context, origin=index[-1], timeframe=timeframe, period=period, horizon=6
    )
    np.testing.assert_allclose(trajectory.median, expected, atol=1e-6)
    assert trajectory.timeframe == timeframe


def test_seasonal_and_naive_backends_differ_on_a_seasonal_context() -> None:
    """The two offline baselines are genuinely different forecasters."""
    index = frames_of_timeframe("1h", candles=144)
    context = periodic_context(period=6, cycles=24)

    naive_path = NaiveBackend(quantile_levels=LEVELS).predict(
        [ForecastRequest(origin=index[-1], context=context)], horizon=6
    )[0]
    seasonal_path = SeasonalBackend(
        timeframe="1h", seasonal_period=6, trend_window=6, quantile_levels=LEVELS
    ).predict([ForecastRequest(origin=index[-1], context=context)], horizon=6)[0]

    # the random walk holds the origin flat; the seasonal baseline walks the cycle
    np.testing.assert_allclose(naive_path.median, np.zeros(6), atol=1e-6)
    assert not np.allclose(naive_path.median, seasonal_path.median)


# ---------------------------------------------------------------------------
# the factories the registry calls
# ---------------------------------------------------------------------------


def test_build_backend_factories_forward_their_options() -> None:
    """Both registry factories accept the documented keywords and nothing implicit."""
    naive = build_backend(timeframe="4h", quantile_levels=LEVELS, dispersion_window=7)
    seasonal = build_seasonal_backend(
        timeframe="5m", quantile_levels=LEVELS, trend_window=3, seasonal_period=10
    )

    assert isinstance(naive, NaiveBackend)
    assert (naive.timeframe, naive.dispersion_window) == ("4h", 7)
    assert isinstance(seasonal, SeasonalBackend)
    assert (seasonal.timeframe, seasonal.trend_window, seasonal.seasonal_period) == ("5m", 3, 10)


def test_get_backend_forwards_the_seasonal_period_override() -> None:
    """The registry factory signature is what the CLI and the artifact builder call."""
    backend = get_backend("seasonal", timeframe="4h", trend_window=3, seasonal_period=12)

    assert isinstance(backend, SeasonalBackend)
    assert backend.timeframe == "4h"
    assert backend.trend_window == 3
    assert backend.seasonal_period == 12
