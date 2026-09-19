"""Contract tests for the forecasting value objects (work package wp-1).

Everything here is pure and offline: ``ForecastRequest`` and
``ForecastTrajectory`` are the interface every other forecasting package builds
on, so the tests pin the semantics literally (a log offset from the origin close,
``0.0`` == the origin close) and the defensive rules (validated shapes/levels,
fresh copies on every accessor, JSON-safe serialisation).

The last section pins the *packaging* contract of the layer: importing
``trading_platform.forecast`` must never pull in another layer of the platform
and must never import ``torch``/``timesfm``/``jax``/``scipy``.
"""

from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.errors import (
    ForecastArtifactError,
    ForecastError,
    TradingBacktestError,
)
from trading_platform.forecast.types import (
    DEFAULT_QUANTILE_LEVELS,
    MEDIAN_LEVEL,
    ForecastRequest,
    ForecastTrajectory,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
FORECAST_DIR = SRC_DIR / "trading_platform" / "forecast"

#: Heavy optional runtimes a pure module of this layer must never import.
HEAVY_ROOTS = frozenset({"torch", "timesfm", "jax", "scipy", "sklearn", "freqtrade", "ccxt"})

#: The only project packages the forecasting layer may depend on.
ALLOWED_PROJECT_PREFIXES = ("trading_platform.core", "trading_platform.forecast")

ORIGIN = pd.Timestamp("2024-01-01T00:00:00Z")
LEVELS: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)
DECILES = DEFAULT_QUANTILE_LEVELS

#: Canonical path: every quantile ends positive, the median goes 0.01 -> 0.03.
QUANTILES = np.array(
    [
        [-0.01, 0.00, 0.01],
        [0.00, 0.01, 0.02],
        [0.01, 0.02, 0.03],
        [0.02, 0.03, 0.04],
        [0.03, 0.04, 0.05],
    ],
    dtype="float32",
)


def make_trajectory(**overrides: object) -> ForecastTrajectory:
    """Build the canonical trajectory, overriding individual fields."""
    fields: dict[str, object] = {
        "origin": ORIGIN,
        "timeframe": "1h",
        "horizon": 3,
        "quantile_levels": LEVELS,
        "quantiles": QUANTILES,
    }
    fields.update(overrides)
    return ForecastTrajectory(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# constants and the request value object
# ---------------------------------------------------------------------------


def test_default_quantile_levels_are_the_nine_deciles() -> None:
    assert DEFAULT_QUANTILE_LEVELS == (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
    assert list(DEFAULT_QUANTILE_LEVELS) == [round(step / 10.0, 1) for step in range(1, 10)]
    assert MEDIAN_LEVEL == 0.5


def test_forecast_request_is_a_frozen_two_field_value_object() -> None:
    context = (1.0, 2.0, 3.0)
    request = ForecastRequest(origin=ORIGIN, context=context)

    assert request.origin == ORIGIN
    assert request.context is context
    assert request.context == context
    with pytest.raises(FrozenInstanceError):
        request.origin = pd.Timestamp("2025-01-01T00:00:00Z")  # type: ignore[misc]


def test_forecast_request_accepts_an_empty_or_short_context() -> None:
    """The request is passive: bounds are enforced by the backends, not here."""
    assert ForecastRequest(origin=ORIGIN, context=()).context == ()
    assert ForecastRequest(origin=ORIGIN, context=(1.0,)).context == (1.0,)


# ---------------------------------------------------------------------------
# trajectory validation
# ---------------------------------------------------------------------------


def test_trajectory_stores_levels_and_copies_the_quantiles() -> None:
    source = np.array(QUANTILES, dtype="float64")
    trajectory = make_trajectory(quantiles=source)

    assert trajectory.origin == ORIGIN
    assert trajectory.timeframe == "1h"
    assert trajectory.horizon == 3
    assert trajectory.quantile_levels == LEVELS
    assert isinstance(trajectory.quantile_levels, tuple)
    assert trajectory.quantiles.dtype == np.float32
    assert trajectory.quantiles.shape == (5, 3)

    source[0, 0] = 99.0
    assert float(trajectory.quantiles[0, 0]) == pytest.approx(-0.01)


def test_trajectory_accepts_a_numpy_integer_horizon() -> None:
    trajectory = make_trajectory(horizon=np.int16(3))
    assert trajectory.horizon == 3
    assert isinstance(trajectory.horizon, int)


def test_trajectory_coerces_a_string_origin_to_a_timestamp() -> None:
    trajectory = make_trajectory(origin="2024-01-01T00:00:00Z")
    assert trajectory.origin == ORIGIN


@pytest.mark.parametrize("horizon", [0, -1, 2.5, "three", None])
def test_trajectory_rejects_an_invalid_horizon(horizon: object) -> None:
    with pytest.raises(ForecastError):
        make_trajectory(horizon=horizon)


@pytest.mark.parametrize(
    "quantiles",
    [
        np.zeros(3, dtype="float32"),
        np.zeros((1, 3, 1), dtype="float32"),
        np.zeros((4, 3), dtype="float32"),
        np.zeros((5, 4), dtype="float32"),
    ],
)
def test_trajectory_rejects_a_bad_shape(quantiles: np.ndarray) -> None:
    with pytest.raises(ForecastError):
        make_trajectory(quantiles=quantiles)


@pytest.mark.parametrize(
    "levels",
    [
        (),
        (0.1, 0.25, 0.75, 0.9),
        (0.1, 0.25, 0.5, 0.5, 0.9),
        (0.1, 0.5, 0.4, 0.9),
        (0.0, 0.25, 0.5, 0.75, 0.9),
        (0.1, 0.25, 0.5, 0.75, 1.0),
        (-0.1, 0.5, 0.75),
        (float("nan"), 0.5, 0.75),
    ],
)
def test_trajectory_rejects_invalid_quantile_levels(levels: tuple[float, ...]) -> None:
    quantiles = np.resize(np.asarray(QUANTILES, dtype="float32"), (len(levels), 3))
    with pytest.raises(ForecastError):
        make_trajectory(quantile_levels=levels, quantiles=quantiles)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_trajectory_rejects_non_finite_values(bad: float) -> None:
    quantiles = np.array(QUANTILES, dtype="float32")
    quantiles[2, 1] = bad
    with pytest.raises(ForecastError):
        make_trajectory(quantiles=quantiles)


@pytest.mark.parametrize("origin", [pd.NaT, None])
def test_trajectory_rejects_a_missing_origin(origin: object) -> None:
    with pytest.raises(ForecastError):
        make_trajectory(origin=origin)


def test_trajectory_rejects_a_non_timestamp_origin() -> None:
    with pytest.raises(ForecastError):
        make_trajectory(origin="not a timestamp")


def test_forecast_errors_derive_from_the_house_base_class() -> None:
    assert issubclass(ForecastError, TradingBacktestError)
    assert issubclass(ForecastArtifactError, ForecastError)
    assert str(ForecastError("boom")) == "boom"


# ---------------------------------------------------------------------------
# accessors return fresh copies
# ---------------------------------------------------------------------------


def test_median_is_a_copy_of_the_median_row() -> None:
    trajectory = make_trajectory()
    median = trajectory.median

    assert median.dtype == np.float32
    assert median.shape == (3,)
    np.testing.assert_allclose(median, [0.01, 0.02, 0.03], rtol=1e-6)

    median[0] = 42.0
    assert float(trajectory.median[0]) == pytest.approx(0.01)


def test_quantile_is_a_copy_of_the_requested_row() -> None:
    trajectory = make_trajectory()
    upper = trajectory.quantile(0.9)

    assert upper.dtype == np.float32
    np.testing.assert_allclose(upper, [0.03, 0.04, 0.05], rtol=1e-6)

    upper[0] = -42.0
    np.testing.assert_allclose(trajectory.quantile(0.9), [0.03, 0.04, 0.05], rtol=1e-6)


@pytest.mark.parametrize("level", [0.05, 0.2, 0.6, 0.95, 0, 1, -0.5, "median"])
def test_quantile_rejects_an_absent_level(level: object) -> None:
    trajectory = make_trajectory()
    with pytest.raises(ForecastError):
        trajectory.quantile(level)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# path algebra
# ---------------------------------------------------------------------------


def test_alpha_reads_the_path_at_the_requested_horizon() -> None:
    trajectory = make_trajectory()

    assert trajectory.alpha(1) == pytest.approx(0.01, rel=1e-6)
    assert trajectory.alpha(2) == pytest.approx(0.02, rel=1e-6)
    assert trajectory.alpha(3) == pytest.approx(0.03, rel=1e-6)
    assert trajectory.alpha(1, offset=2) == pytest.approx(0.03, rel=1e-6)
    assert trajectory.alpha(2, offset=1) == pytest.approx(0.03, rel=1e-6)


@pytest.mark.parametrize(
    ("k", "offset"),
    [(0, 0), (-1, 0), (1, -1), (3, 1), (4, 0), (3, 3)],
)
def test_alpha_rejects_a_window_outside_the_path(k: int, offset: int) -> None:
    trajectory = make_trajectory()
    with pytest.raises(ForecastError):
        trajectory.alpha(k, offset=offset)


def test_slope_mfe_and_mae_describe_the_median_path() -> None:
    trajectory = make_trajectory()

    assert trajectory.slope() == pytest.approx(0.01, rel=1e-6)
    assert trajectory.mfe() == pytest.approx(0.03, rel=1e-6)
    assert trajectory.mae() == pytest.approx(0.01, rel=1e-6)


def test_path_length_and_efficiency_of_a_straight_path() -> None:
    trajectory = make_trajectory()

    assert trajectory.path_length() == pytest.approx(0.03, rel=1e-6)
    assert trajectory.path_efficiency() == pytest.approx(1.0, rel=1e-6)


def test_path_efficiency_of_a_wandering_path_is_below_one() -> None:
    median = np.array([0.01, -0.02, 0.03], dtype="float32")
    quantiles = np.array(
        [[0.0, -0.03, 0.02], [0.0, 0.0, 0.0], median, [0.02, 0.01, 0.04], [0.03, 0.02, 0.05]]
    )
    trajectory = make_trajectory(quantiles=quantiles)

    assert trajectory.path_length() == pytest.approx(0.01 + 0.03 + 0.05, rel=1e-6)
    assert trajectory.path_efficiency() == pytest.approx(0.03 / 0.09, rel=1e-6)


def test_path_efficiency_of_a_flat_path_is_zero() -> None:
    zeros = np.zeros((5, 3), dtype="float32")
    trajectory = make_trajectory(quantiles=zeros)

    assert trajectory.path_length() == 0.0
    assert trajectory.path_efficiency() == 0.0


def test_iqr_and_dispersion_helpers() -> None:
    trajectory = make_trajectory()

    np.testing.assert_allclose(trajectory.iqr(), [0.02, 0.02, 0.02], rtol=1e-6)
    assert trajectory.terminal_iqr() == pytest.approx(0.02, rel=1e-6)
    assert trajectory.per_bar_dispersion() == pytest.approx(0.02 / math.sqrt(3), rel=1e-6)
    assert trajectory.reliability() == pytest.approx(1.5 * math.sqrt(3), rel=1e-5)


def test_iqr_requires_the_quartile_levels() -> None:
    deciles = np.resize(np.asarray(QUANTILES, dtype="float32"), (len(DECILES), 3))
    trajectory = make_trajectory(quantile_levels=DECILES, quantiles=deciles)

    with pytest.raises(ForecastError):
        trajectory.iqr()
    with pytest.raises(ForecastError):
        trajectory.terminal_iqr()
    with pytest.raises(ForecastError):
        trajectory.per_bar_dispersion()
    with pytest.raises(ForecastError):
        trajectory.reliability()


def test_reliability_is_zero_when_the_dispersion_collapses() -> None:
    quantiles = np.tile(np.array([[0.01, 0.02, 0.03]], dtype="float32"), (5, 1))
    trajectory = make_trajectory(quantiles=quantiles)

    assert trajectory.terminal_iqr() == 0.0
    assert trajectory.per_bar_dispersion() == 0.0
    assert trajectory.reliability() == 0.0
    assert math.isfinite(trajectory.reliability())


def test_reliability_is_zero_when_the_median_is_flat() -> None:
    quantiles = np.array(
        [
            [-0.01, -0.01, -0.01],
            [-0.005, -0.005, -0.005],
            [0.0, 0.0, 0.0],
            [0.005, 0.005, 0.005],
            [0.01, 0.01, 0.01],
        ],
        dtype="float32",
    )
    trajectory = make_trajectory(quantiles=quantiles)

    assert trajectory.alpha(trajectory.horizon) == 0.0
    assert trajectory.reliability() == 0.0
    assert trajectory.agreement() == 0.0


def test_agreement_counts_the_deciles_on_the_median_side() -> None:
    trajectory = make_trajectory()
    assert trajectory.agreement() == 1.0

    mixed = np.array(
        [
            [-0.01, 0.0, -0.005],
            [0.0, 0.01, 0.02],
            [0.01, 0.02, 0.03],
            [0.02, 0.03, 0.04],
            [0.03, 0.04, 0.05],
        ],
        dtype="float32",
    )
    assert make_trajectory(quantiles=mixed).agreement() == pytest.approx(0.75)


def test_agreement_of_a_negative_path_counts_the_negative_deciles() -> None:
    negated = make_trajectory(quantiles=-np.asarray(QUANTILES, dtype="float32"))
    assert negated.agreement() == 1.0
    assert negated.alpha(negated.horizon) < 0.0


def test_agreement_is_zero_when_only_the_median_is_stored() -> None:
    trajectory = make_trajectory(
        quantile_levels=(0.5,), quantiles=np.array([[0.01, 0.02, 0.03]], dtype="float32")
    )
    assert trajectory.agreement() == 0.0


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------


def test_to_dict_is_json_safe_and_matches_the_accessors() -> None:
    trajectory = make_trajectory()
    payload = trajectory.to_dict()

    encoded = json.dumps(payload)
    decoded = json.loads(encoded)

    assert decoded["origin"] == ORIGIN.isoformat()
    assert decoded["timeframe"] == "1h"
    assert decoded["horizon"] == 3
    assert decoded["quantile_levels"] == list(LEVELS)
    assert decoded["median"] == pytest.approx([0.01, 0.02, 0.03], rel=1e-6)
    assert len(decoded["quantiles"]) == len(LEVELS)
    assert len(decoded["quantiles"][0]) == 3
    assert decoded["slope"] == pytest.approx(trajectory.slope(), rel=1e-9)
    assert decoded["path_efficiency"] == pytest.approx(1.0, rel=1e-6)
    assert decoded["agreement"] == 1.0


def test_to_dict_never_raises_for_a_decile_only_trajectory() -> None:
    deciles = np.resize(np.asarray(QUANTILES, dtype="float32"), (len(DECILES), 3))
    payload = make_trajectory(quantile_levels=DECILES, quantiles=deciles).to_dict()

    json.dumps(payload)
    assert set(payload) == {
        "origin",
        "timeframe",
        "horizon",
        "quantile_levels",
        "median",
        "quantiles",
        "slope",
        "mfe",
        "mae",
        "path_length",
        "path_efficiency",
        "agreement",
    }


# ---------------------------------------------------------------------------
# layer rule: the package stays cheap, offline and self-contained
# ---------------------------------------------------------------------------


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


def _module_level_imports(tree: ast.Module) -> list[str]:
    """Return the modules imported at the top level of ``tree`` (not in a function)."""
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    return imported


def _all_imports(tree: ast.Module) -> list[str]:
    """Return every module imported anywhere in ``tree``."""
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    return imported


#: The pure modules shipped by work package wp-1, whose dependency direction is
#: part of the binding contract.  Backends added later may import their heavy
#: runtime *lazily*, so the whole-package check below only looks at module level.
WP1_MODULES = (
    "__init__.py",
    "types.py",
    "registry.py",
    "series.py",
    "backends/__init__.py",
    "backends/naive.py",
    "backends/seasonal.py",
)


def test_no_forecast_module_imports_a_heavy_runtime_eagerly() -> None:
    violations: list[str] = []
    for path in sorted(FORECAST_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _module_level_imports(tree):
            if name.split(".")[0] in HEAVY_ROOTS:
                violations.append(f"{path.relative_to(FORECAST_DIR)}: {name}")

    assert violations == []


def test_wp1_modules_only_import_core_numpy_and_pandas() -> None:
    violations: list[str] = []
    for relative in WP1_MODULES:
        path = FORECAST_DIR / relative
        assert path.is_file(), f"the wp-1 module {relative} is missing"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _all_imports(tree):
            root = name.split(".")[0]
            forbidden_project = root == "trading_platform" and not name.startswith(
                ALLOWED_PROJECT_PREFIXES
            )
            if root in HEAVY_ROOTS or forbidden_project:
                violations.append(f"{relative}: {name}")

    assert violations == []


def test_importing_the_forecast_package_pulls_in_no_other_layer() -> None:
    code = (
        "import json, sys\n"
        "import trading_platform.forecast\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('trading_platform'))))\n"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules} - {'__main__'} "
        "& {'torch', 'timesfm', 'jax', 'scipy', 'sklearn'})))\n"
    )
    result = run_python(code)
    assert result.returncode == 0, result.stderr

    lines = result.stdout.strip().splitlines()
    loaded = json.loads(lines[-2])
    heavy = json.loads(lines[-1])

    assert heavy == []
    for module in loaded:
        assert module == "trading_platform" or module.startswith(ALLOWED_PROJECT_PREFIXES)
    # The TimesFM backend is resolved lazily and must not be imported eagerly.
    assert not any(module.endswith("backends.timesfm") for module in loaded)
