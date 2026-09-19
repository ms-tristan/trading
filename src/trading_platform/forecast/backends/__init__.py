"""Forecast backends, discovered lazily by :mod:`trading_platform.forecast.registry`.

This package intentionally imports **nothing**: a backend module is loaded on
demand through :data:`trading_platform.forecast.registry.BUILTIN_BACKEND_MODULES`
and every module exposes the same two names:

* ``BACKEND_NAME`` — the registry key;
* ``build_backend(**options) -> ForecastBackend`` — the module-level factory the
  registry calls with the user options.

Each module also exposes its backend class (``NaiveBackend``, ``SeasonalBackend``,
``TimesFMBackend``, ...).  Keeping the imports lazy is what allows a backend with
heavy optional dependencies (``torch``/``timesfm``) to live here while the test
suite, the CLI and the package root stay importable without those dependencies.
Adding a backend therefore means adding one module next to this file and one
entry in :data:`~trading_platform.forecast.registry.BUILTIN_BACKEND_MODULES`: it
never means editing this ``__init__``.
"""

from __future__ import annotations

__all__: list[str] = []
