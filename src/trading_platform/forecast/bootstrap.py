"""Bootstrap of the offline forecasting backends.

The two offline backends (``naive`` and ``seasonal``) are pure ``numpy``/
``pandas`` code with no optional dependency, so they are always installable --
but they are only *importable* when the package that ships them is actually on
the path.  An editable install that lost its ``.pth`` file, a wheel built
without the backends, or a bare checkout all produce the same symptom: the
backend registry lists ``naive`` and ``seasonal``, yet every build of an
artifact fails with a bare ``ModuleNotFoundError`` far away from the real cause.

:func:`ensure_forecast_backends` turns that into one loud, actionable error,
raised **before** any artifact work starts.  It is deliberately tiny and
stdlib-only:

* it imports the two backend modules **inside its own body**, through
  :func:`importlib.import_module` and the :data:`_OFFLINE_BACKEND_MODULE` tuple,
  so importing this module never imports a backend -- and never imports
  ``torch``/``timesfm``/``freqtrade``/``ccxt`` either;
* it depends on :mod:`trading_platform.core.errors` exclusively.

The point of the in-body import is testability: a caller (or a test) can purge
a backend module from :data:`sys.modules` and stub it with ``None`` to exercise
the failure path without touching the installed environment -- which is exactly
how ``tests/test_forecast_bootstrap.py`` pins the error message.
"""

from __future__ import annotations

import importlib

from trading_platform.core.errors import ForecastError

__all__ = ["ensure_forecast_backends"]

#: Import path of every *offline* backend shipped with the package.
#:
#: Those are the backends that must always be importable: they carry no optional
#: dependency, so a failure to import them is an installation defect, never a
#: missing extra.  The ``timesfm`` backend is deliberately absent -- it lives
#: behind the ``[timesfm]`` extra and is resolved lazily by the registry.
_OFFLINE_BACKEND_MODULE: tuple[str, ...] = (
    "trading_platform.forecast.backends.naive",
    "trading_platform.forecast.backends.seasonal",
)


def ensure_forecast_backends() -> None:
    """Import both offline forecast backends, or fail with an actionable error.

    Called once at the top of the ``forecast-build`` and ``forecast-skill``
    command bodies, before any artifact is read or written, so an incomplete
    installation is reported as a configuration problem rather than as a
    :class:`ModuleNotFoundError` raised from deep inside the registry.

    Returns
    -------
    None
        The two modules are importable; importing them twice is free because
        Python caches them in :data:`sys.modules`.

    Raises
    ------
    ForecastError
        If either offline backend module cannot be imported; the message names
        the ``.[dev]`` extra that reinstalls them.
    """
    for module_name in _OFFLINE_BACKEND_MODULE:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            raise ForecastError(
                "the offline forecast backends are not installed; "
                "reinstall with 'pip install -e \".[dev]\"' "
                f"(cannot import {module_name!r}: {exc})"
            ) from exc
