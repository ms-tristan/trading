"""Forecast backend registry: names in, configured backends out.

The registry is the single place that knows which forecasting backends exist,
which keeps the CLI, the artifact builder and the configuration layer free of
hard-coded backend imports.

Discovery is **lazy on purpose**: a backend listed in
:data:`BUILTIN_BACKEND_MODULES` is imported only when :func:`get_backend` is
called for it, so a heavy backend module (``torch``/``timesfm``) can live in the
package without being imported by the package root, and without its optional
dependencies being installed.  A missing module or a missing extra therefore
never breaks an import of :mod:`trading_platform.forecast` nor a test run: it
only makes :func:`installed_backends` skip that name.

Backends are **never cached**: every :func:`get_backend` call builds a fresh
instance, exactly like :func:`trading_platform.strategy.registry.get_strategy`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from typing import Any, Protocol, cast, runtime_checkable

from trading_platform.core.errors import ForecastError
from trading_platform.forecast.types import ForecastRequest, ForecastTrajectory

__all__ = [
    "BACKENDS",
    "BUILTIN_BACKEND_MODULES",
    "ForecastBackend",
    "ForecastBackendFactory",
    "available_backends",
    "get_backend",
    "installed_backends",
    "register_backend",
]

#: Import path of every backend shipped with the package, keyed by backend name.
#:
#: ``timesfm`` is resolved lazily, so this package never imports it.
BUILTIN_BACKEND_MODULES: dict[str, str] = {
    "naive": "trading_platform.forecast.backends.naive",
    "seasonal": "trading_platform.forecast.backends.seasonal",
    "timesfm": "trading_platform.forecast.backends.timesfm",
}

#: User-registered backend factories, keyed by backend name (monkeypatchable).
BACKENDS: dict[str, ForecastBackendFactory] = {}


@runtime_checkable
class ForecastBackend(Protocol):
    """Structural contract every forecasting backend must satisfy.

    Attributes
    ----------
    name:
        Backend identifier, used by the artifact metadata and the CLI.
    """

    name: str

    def is_available(self) -> bool:
        """Return ``True`` when the backend can run in this environment.

        This method never raises: a missing optional dependency means ``False``.
        """
        ...

    def predict(
        self, requests: Sequence[ForecastRequest], *, horizon: int
    ) -> list[ForecastTrajectory]:
        """Forecast ``horizon`` steps for every request, in request order."""
        ...


#: Callable building a backend instance from keyword options.
ForecastBackendFactory = Callable[..., ForecastBackend]


def available_backends() -> list[str]:
    """Return the sorted names of every *known* backend (installed or not)."""
    return sorted(set(BACKENDS) | set(BUILTIN_BACKEND_MODULES))


def register_backend(name: str, factory: ForecastBackendFactory) -> None:
    """Register ``factory`` under ``name`` in :data:`BACKENDS`.

    Registering the exact same factory twice is a no-op, so the call is safe to
    repeat (module re-import, test fixtures, plugins).

    Raises
    ------
    ForecastError
        If ``name`` is not a non-empty string, if it shadows a builtin backend,
        if ``factory`` is not callable, or if ``name`` is already registered with
        a different factory.
    """
    if not isinstance(name, str) or not name:
        raise ForecastError(f"register_backend expects a non-empty string name, got {name!r}")
    if name in BUILTIN_BACKEND_MODULES:
        raise ForecastError(f"forecast backend {name!r} is built in and cannot be replaced")
    if not callable(factory):
        raise ForecastError(f"register_backend expects a callable factory, got {factory!r}")
    existing = BACKENDS.get(name)
    if existing is not None and existing is not factory:
        raise ForecastError(f"forecast backend {name!r} is already registered")
    BACKENDS[name] = factory


def get_backend(name: str, **options: Any) -> ForecastBackend:
    """Build a fresh backend registered under ``name``.

    User-registered factories win over the builtin modules.  A builtin backend
    is imported on demand through :data:`BUILTIN_BACKEND_MODULES` and built with
    its module-level ``build_backend(**options)`` factory.

    Raises
    ------
    ForecastError
        If ``name`` is unknown, if its module cannot be imported (missing extra)
        or if that module exposes no ``build_backend`` factory.
    """
    factory: ForecastBackendFactory | None
    try:
        factory = BACKENDS.get(name) if isinstance(name, str) else None
    except TypeError:  # unhashable name (a list, a dict, ...)
        factory = None
    if factory is not None:
        return factory(**options)

    try:
        module_path = BUILTIN_BACKEND_MODULES[name]
    except (KeyError, TypeError):
        available = ", ".join(available_backends())
        raise ForecastError(
            f"unknown forecast backend: {name!r} (available: {available})"
        ) from None

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ForecastError(
            f"forecast backend {name!r} is unavailable: cannot import {module_path!r} ({exc})"
        ) from exc

    builder = getattr(module, "build_backend", None)
    if not callable(builder):
        raise ForecastError(
            f"forecast backend module {module_path!r} does not define build_backend()"
        )
    return cast("ForecastBackend", builder(**options))


def installed_backends() -> list[str]:
    """Return the sorted names of the backends that can actually run now.

    Every candidate is imported and probed inside a ``try``/``except``: a missing
    optional dependency, a broken module or a raising ``is_available`` simply
    removes that name from the result.  This function never raises.
    """
    installed: list[str] = []
    for name in available_backends():
        try:
            backend = get_backend(name)
            if backend.is_available():
                installed.append(name)
        except Exception:  # discovery must never break the caller
            continue
    return installed
