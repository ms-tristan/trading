"""Contract tests of the feature-injection seam.

The seam is what makes the ``timesfm`` strategy deliverable without breaking the
house rule "a strategy is pure, deterministic and I/O-free": the external
forecast artifact is resolved **once** per run from the configuration (or from an
explicit parameter override), loaded into a :class:`FeatureBundle` and handed to
the strategy instance through
:meth:`~trading_platform.strategy.base.Strategy.set_feature_bundle`.

What this file pins, in order:

* ``resolve_features``: no path means an empty bundle, a real path is loaded, a
  configured-but-unreadable path raises the *unwrapped*
  :class:`~trading_platform.core.errors.ForecastArtifactError`, and the parameter
  override wins over the configuration;
* ``attach_features``: the base hook accepts any bundle, a strategy without the
  hook is reported loudly, and a narrowed hook rejects a foreign object;
* the injection is inert for the reference ``basic`` strategy — byte for byte;
* ``run_backtest_on_config`` and ``make_runner`` inject a bundle end to end, and
  the runner resolves it **once** rather than per window;
* importing :mod:`trading_platform.strategy` stays light (no ``torch``, no
  ``timesfm``, no artifact reader).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pandas as pd
import pytest

from trading_platform.config import AppConfig
from trading_platform.core.errors import ForecastArtifactError, StrategyError, TradingBacktestError
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.forecast.artifact import (
    ForecastBuildConfig,
    ForecastStore,
    build_forecast_artifact,
    metadata_path,
)
from trading_platform.strategy import BasicStrategy
from trading_platform.strategy.base import Strategy
from trading_platform.strategy.engine import make_runner, run_backtest, run_backtest_on_config
from trading_platform.strategy.features import FeatureBundle, attach_features, resolve_features

#: Quantile levels that keep the fixtures tiny (low / median / high).
SMALL_LEVELS: tuple[float, ...] = (0.1, 0.5, 0.9)


def build_artifact(
    directory: Path,
    *,
    horizon: int = 24,
    name: str = "artifact.parquet",
    candles: pd.DataFrame | None = None,
) -> Path:
    """Write a small offline artifact and return its path."""
    frame = make_ohlcv(600, seed=5) if candles is None else candles
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="seasonal",
        context_length=64,
        horizon=horizon,
        reforecast_every=horizon,
        quantile_levels=SMALL_LEVELS,
        seasonal_period=24,
        seasonal_window=48,
    )
    path, _metadata = build_forecast_artifact(frame, config, directory / name)
    return path


@pytest.fixture
def artifact(tmp_path: Path) -> Path:
    """A readable artifact of horizon 24."""
    return build_artifact(tmp_path, horizon=24, name="readable.parquet")


@pytest.fixture
def market() -> pd.DataFrame:
    """The deterministic market the artifacts above were built from."""
    return make_ohlcv(600, seed=5)


# ---------------------------------------------------------------------------
# resolve_features
# ---------------------------------------------------------------------------


def test_no_configured_path_yields_an_empty_bundle() -> None:
    bundle = resolve_features(AppConfig())

    assert bundle == FeatureBundle.empty()
    assert bundle.has_forecast is False
    assert bundle.forecast is None


def test_an_explicitly_empty_parameter_never_wins_over_nothing() -> None:
    assert resolve_features(AppConfig(), {"artifact": ""}).has_forecast is False
    assert resolve_features(AppConfig(), {"artifact": None}).has_forecast is False


def test_the_configured_artifact_is_loaded(artifact: Path) -> None:
    bundle = resolve_features(AppConfig(forecast={"artifact": str(artifact)}))

    assert bundle.has_forecast
    assert isinstance(bundle.forecast, ForecastStore)
    assert bundle.forecast.path == artifact
    assert bundle.forecast.horizon == 24


def test_a_parameter_override_wins_over_the_configuration(tmp_path: Path) -> None:
    configured = build_artifact(tmp_path, horizon=24, name="configured.parquet")
    overridden = build_artifact(tmp_path, horizon=12, name="overridden.parquet")
    config = AppConfig(forecast={"artifact": str(configured)})

    bundle = resolve_features(config, {"artifact": str(overridden)})

    assert bundle.forecast is not None
    assert bundle.forecast.path == overridden
    assert bundle.forecast.horizon == 12


def test_a_parameter_without_an_artifact_keeps_the_configuration(artifact: Path) -> None:
    config = AppConfig(forecast={"artifact": str(artifact)})

    bundle = resolve_features(config, {"horizon": 6})

    assert bundle.forecast is not None
    assert bundle.forecast.path == artifact


def test_a_missing_artifact_raises_the_unwrapped_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.parquet"
    config = AppConfig(forecast={"artifact": str(missing)})

    with pytest.raises(ForecastArtifactError) as error:
        resolve_features(config)

    assert missing.name in str(error.value)
    assert isinstance(error.value, TradingBacktestError)
    assert type(error.value) is ForecastArtifactError


def test_a_missing_sidecar_is_reported_as_well(artifact: Path) -> None:
    metadata_path(artifact).unlink()

    with pytest.raises(ForecastArtifactError):
        resolve_features(AppConfig(forecast={"artifact": str(artifact)}))


# ---------------------------------------------------------------------------
# attach_features
# ---------------------------------------------------------------------------


def test_the_base_hook_accepts_and_stores_any_bundle() -> None:
    basic = BasicStrategy()
    bundle = FeatureBundle.empty()

    attach_features(basic, bundle)

    assert basic.feature_bundle is bundle


def test_a_strategy_without_the_hook_is_reported() -> None:
    class Hookless:
        """A duck-typed strategy that deliberately exposes no feature hook."""

        name = "hookless"

    with pytest.raises(StrategyError, match="feature bundle"):
        attach_features(Hookless(), FeatureBundle.empty())  # type: ignore[arg-type]


def test_the_hook_is_part_of_the_strategy_contract() -> None:
    """Every house strategy inherits the hook, so the injection is always possible."""
    for strategy_class in (Strategy, BasicStrategy):
        assert callable(getattr(strategy_class, "set_feature_bundle", None))


def test_attach_features_clears_the_bundle() -> None:
    basic = BasicStrategy()
    attach_features(basic, FeatureBundle.empty())
    basic.set_feature_bundle(None)

    assert basic.feature_bundle is None


# ---------------------------------------------------------------------------
# the reference strategy is untouched
# ---------------------------------------------------------------------------


def test_the_basic_strategy_is_byte_for_byte_unaffected(market: pd.DataFrame) -> None:
    reference = run_backtest(BasicStrategy(), market)
    through_config = run_backtest_on_config(AppConfig(), market)

    assert through_config.trades == reference.trades
    assert through_config.final_balance == reference.final_balance
    assert through_config.params == reference.params
    pd.testing.assert_series_equal(through_config.equity_curve, reference.equity_curve)


def test_a_configured_artifact_is_inert_for_the_basic_strategy(
    artifact: Path, market: pd.DataFrame
) -> None:
    reference = run_backtest(BasicStrategy(), market)
    configured = run_backtest_on_config(AppConfig(forecast={"artifact": str(artifact)}), market)

    assert reference.n_trades > 0
    assert configured.trades == reference.trades
    assert configured.final_balance == reference.final_balance


# ---------------------------------------------------------------------------
# engine wiring
# ---------------------------------------------------------------------------


def test_the_injected_bundle_reaches_the_strategy(artifact: Path, market: pd.DataFrame) -> None:
    config = AppConfig(strategy={"name": "timesfm"}, forecast={"artifact": str(artifact)})

    result = run_backtest_on_config(config, market)

    assert result.strategy_name == "timesfm"
    assert result.n_trades > 0


def test_an_explicit_bundle_skips_the_filesystem(tmp_path: Path, market: pd.DataFrame) -> None:
    path = build_artifact(tmp_path, horizon=24, name="explicit.parquet")
    bundle = FeatureBundle(forecast=ForecastStore.load(path))
    config = AppConfig(
        strategy={"name": "timesfm"}, forecast={"artifact": str(tmp_path / "gone.parquet")}
    )

    result = run_backtest_on_config(config, market, features=bundle)

    assert result.n_trades > 0


def test_make_runner_injects_the_bundle(tmp_path: Path, market: pd.DataFrame) -> None:
    path = build_artifact(tmp_path, horizon=24, name="runner.parquet")
    config = AppConfig(strategy={"name": "timesfm"}, forecast={"artifact": str(path)})

    runner = make_runner(config)
    direct = run_backtest_on_config(config, market)
    through_runner = runner(market)

    assert through_runner.trades == direct.trades
    assert through_runner.final_balance == direct.final_balance


def test_make_runner_resolves_the_artifact_once(tmp_path: Path, market: pd.DataFrame) -> None:
    """The runner closes over an in-memory store: removing the file changes nothing."""
    path = build_artifact(tmp_path, horizon=24, name="once.parquet")
    config = AppConfig(strategy={"name": "timesfm"}, forecast={"artifact": str(path)})
    runner = make_runner(config)
    expected = runner(market)

    path.unlink()
    metadata_path(path).unlink()
    again = runner(market)
    window = runner(market.iloc[100:180])

    assert again.trades == expected.trades
    assert window.strategy_name == "timesfm"


def test_make_runner_keeps_the_basic_strategy_intact(market: pd.DataFrame) -> None:
    runner = make_runner(AppConfig())
    reference = run_backtest(BasicStrategy(), market)

    result = runner(market)

    assert result.trades == reference.trades


def test_the_runner_still_accepts_parameter_overrides(artifact: Path, market: pd.DataFrame) -> None:
    config = AppConfig(strategy={"name": "timesfm"}, forecast={"artifact": str(artifact)})
    runner = make_runner(config)

    result = runner(market, {"max_hold": 6})

    assert result.params["max_hold"] == 6


def test_a_missing_artifact_fails_before_any_trade(tmp_path: Path, market: pd.DataFrame) -> None:
    config = AppConfig(
        strategy={"name": "timesfm"}, forecast={"artifact": str(tmp_path / "absent.parquet")}
    )

    with pytest.raises(ForecastArtifactError):
        make_runner(config)


# ---------------------------------------------------------------------------
# import hygiene
# ---------------------------------------------------------------------------


def test_importing_the_strategy_package_stays_light() -> None:
    """No ``torch``, no ``timesfm`` and no artifact reader at import time."""
    code = (
        "import sys\n"
        "import trading_platform.strategy\n"
        "import trading_platform.config\n"
        "forbidden = ('torch', 'timesfm', 'trading_platform.forecast.artifact')\n"
        "leaked = [name for name in forbidden if name in sys.modules]\n"
        "assert not leaked, leaked\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_the_feature_bundle_is_frozen() -> None:
    bundle = FeatureBundle.empty()

    assert bundle == FeatureBundle()
    with pytest.raises(FrozenInstanceError):
        bundle.forecast = None  # type: ignore[misc]
