"""Contract tests for the offline forecast backends and the backend registry.

These tests never need ``torch``, ``timesfm`` or the network: they cover the two
pure-``numpy`` backends (with hand-computed numbers), the registry contract
(registration, lazy module discovery, availability probing) and the invariants
that matter for the rest of the package (one trajectory per request, request
order, ``float32`` shapes, no mutation of the caller's objects).
"""

from __future__ import annotations

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
from trading_platform.forecast.backends.seasonal import SeasonalBackend
from trading_platform.forecast.registry import (
    BACKENDS,
    BUILTIN_BACKEND_MODULES,
    ForecastBackend,
    available_backends,
    get_backend,
    installed_backends,
    register_backend,
)
from trading_platform.forecast.types import ForecastRequest, ForecastTrajectory

ORIGIN = pd.Timestamp("2024-01-01T00:00:00Z")
LEVELS: tuple[float, ...] = (0.1, 0.5, 0.9)


def request_at(origin: pd.Timestamp, context: Sequence[float]) -> ForecastRequest:
    """Build a request whose context is a tuple (the shape the contract expects)."""
    return ForecastRequest(origin=origin, context=tuple(float(value) for value in context))


def naive_request() -> ForecastRequest:
    """A request with a context whose trailing log differences are all ``1.0``."""
    return request_at(ORIGIN, (0.0, 1.0, 2.0))


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
