"""The bootstrap guard of the offline forecast backends.

``ensure_forecast_backends`` is the single call the CLI makes before it touches
an artifact.  Its whole value is that it imports the two offline backends
**lazily, inside its own body**: importing
:mod:`trading_platform.forecast.bootstrap` must therefore stay free of any
backend import, and of any optional dependency (``torch``, ``timesfm``,
``freqtrade``, ``ccxt``).

The module is pinned here without a module-level guard -- the failure path is
exercised with an in-body ``sys.modules`` purge and ``monkeypatch``, exactly as
``docs/testing-policy.md`` requires.  Everything runs offline: no network, no
torch, no exchange client.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from trading_platform.core.errors import ForecastError
from trading_platform.forecast.bootstrap import _OFFLINE_BACKEND_MODULE, ensure_forecast_backends
from trading_platform.forecast.registry import installed_backends

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

#: Modules the bootstrap helper must never import, directly or indirectly.
OPTIONAL_MODULES = ("torch", "timesfm", "freqtrade", "ccxt")


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter that sees ``src`` on ``PYTHONPATH``."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) if not existing else f"{SRC_DIR}{os.pathsep}{existing}"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=120,
    )


# ---------------------------------------------------------------------------
# 1. the module contract
# ---------------------------------------------------------------------------


def test_the_helper_covers_both_offline_backends() -> None:
    assert _OFFLINE_BACKEND_MODULE == (
        "trading_platform.forecast.backends.naive",
        "trading_platform.forecast.backends.seasonal",
    )
    # the heavy backend is never bootstrapped: it lives behind its own extra
    assert all("timesfm" not in name for name in _OFFLINE_BACKEND_MODULE)


def test_ensure_forecast_backends_is_exported_and_returns_none() -> None:
    from trading_platform.forecast import bootstrap

    assert bootstrap.__all__ == ["ensure_forecast_backends"]
    assert ensure_forecast_backends() is None
    # idempotent: the modules are cached in sys.modules
    assert ensure_forecast_backends() is None


def test_ensure_forecast_backends_registers_both_names_as_installed() -> None:
    ensure_forecast_backends()

    installed = installed_backends()

    assert "naive" in installed
    assert "seasonal" in installed


def test_the_helper_imports_the_backends_lazily() -> None:
    """Neither backend is imported by importing the helper's own module.

    The package root does import the two offline backends (it always has), so
    the meaningful property is the one of ``bootstrap.py`` itself: its module
    body imports nothing, and the backends only appear in ``sys.modules`` once
    :func:`ensure_forecast_backends` runs.
    """
    program = "\n".join(
        [
            "import importlib.util",
            "import sys",
            "spec = importlib.util.find_spec('trading_platform.forecast.bootstrap')",
            "assert spec is not None and spec.loader is not None",
            "sys.modules.pop('trading_platform.forecast.backends.naive', None)",
            "sys.modules.pop('trading_platform.forecast.backends.seasonal', None)",
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            "assert 'trading_platform.forecast.backends.naive' not in sys.modules",
            "assert 'trading_platform.forecast.backends.seasonal' not in sys.modules",
            "assert module._OFFLINE_BACKEND_MODULE",
            "module.ensure_forecast_backends()",
            "assert 'trading_platform.forecast.backends.naive' in sys.modules",
            "assert 'trading_platform.forecast.backends.seasonal' in sys.modules",
        ]
    )

    completed = run_python(program)

    assert completed.returncode == 0, completed.stderr


def test_the_helper_never_imports_an_optional_dependency() -> None:
    code = "\n".join(
        [
            "import sys",
            "from trading_platform.forecast import bootstrap as b",
            "b.ensure_forecast_backends()",
            f"leaked = [name for name in {OPTIONAL_MODULES!r} if name in sys.modules]",
            "assert not leaked, leaked",
        ]
    )

    completed = run_python(code)

    assert completed.returncode == 0, completed.stderr


# ---------------------------------------------------------------------------
# 2. the loud, actionable failure path (never a bare ModuleNotFoundError)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _OFFLINE_BACKEND_MODULE)
def test_a_purged_backend_raises_a_forecast_error(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    # ``None`` in sys.modules makes ``importlib.import_module`` raise ImportError
    monkeypatch.setitem(sys.modules, module_name, None)

    with pytest.raises(ForecastError) as excinfo:
        ensure_forecast_backends()

    message = str(excinfo.value)
    assert "offline forecast backends are not installed" in message
    assert ".[dev]" in message
    assert module_name in message


def test_the_failure_message_names_the_reinstall_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _OFFLINE_BACKEND_MODULE:
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, None)

    with pytest.raises(ForecastError) as excinfo:
        ensure_forecast_backends()

    assert 'pip install -e ".[dev]"' in str(excinfo.value)


def test_a_missing_backend_does_not_break_the_package_import() -> None:
    """The failure is raised by the call, never by importing the layer."""
    code = "\n".join(
        [
            "import sys",
            "import trading_platform.forecast",
            "assert 'trading_platform.forecast.bootstrap' not in sys.modules",
            "from trading_platform.forecast.bootstrap import ensure_forecast_backends",
            "sys.modules['trading_platform.forecast.backends.naive'] = None",
            "try:",
            "    ensure_forecast_backends()",
            "except Exception as exc:",
            "    assert type(exc).__name__ == 'ForecastError', exc",
            "else:",
            "    raise AssertionError('the guard did not fire')",
        ]
    )

    completed = run_python(code)

    assert completed.returncode == 0, completed.stderr


def test_the_bootstrap_module_depends_on_core_errors_only() -> None:
    """Declared dependencies of the module: the standard library and core.errors."""
    source = (REPO_ROOT / "src" / "trading_platform" / "forecast" / "bootstrap.py").read_text(
        encoding="utf-8"
    )
    imported = [
        line.strip()
        for line in source.splitlines()
        if (line.startswith("import ") or line.startswith("from "))
        and not line.startswith("from __future__")
    ]

    assert imported == [
        "import importlib",
        "from trading_platform.core.errors import ForecastError",
    ]
