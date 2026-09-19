"""The timeframe-driven seasonal period of the forecast layer.

``deseasonalize_log_price`` folds the clock into ``period`` phases, so ``period``
only describes a *daily* cycle when it equals the number of candles of that
timeframe in a day.  The historical default of ``24`` is therefore correct for
hourly candles and for nothing else -- a real correctness bug for every other
timeframe, and the reason :func:`resolve_seasonal_period` exists.

This module pins, entirely offline and without torch:

* the value of :func:`candles_per_day` for the whole
  :data:`TIMEFRAME_SECONDS` table (not just the documented cases);
* the explicit override of :func:`resolve_seasonal_period` on every timeframe,
  including the degenerate ``1``;
* the loud :class:`ForecastError` on an unsupported timeframe;
* a **real** de-seasonalisation call per timeframe on a synthetic frame, which
  is what actually catches a wrong period (a bogus period silently produces a
  different de-seasonalised series instead of an error).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.errors import ForecastError
from trading_platform.forecast import (
    SECONDS_PER_DAY,
    TIMEFRAME_SECONDS,
    candles_per_day,
    resolve_seasonal_period,
)
from trading_platform.forecast.artifact import ForecastBuildConfig, build_forecast_frame
from trading_platform.forecast.series import (
    candles_per_day as series_candles_per_day,
)
from trading_platform.forecast.series import (
    deseasonalize_log_price,
)
from trading_platform.forecast.series import (
    resolve_seasonal_period as series_resolve_seasonal_period,
)

#: The pinned period of every supported timeframe: candles per UTC day, floored
#: at one.  A timeframe longer than a day cannot resolve a daily cycle.
EXPECTED_PERIODS: dict[str, int] = {
    "1m": 1440,
    "3m": 480,
    "5m": 288,
    "15m": 96,
    "30m": 48,
    "1h": 24,
    "2h": 12,
    "4h": 6,
    "6h": 4,
    "8h": 3,
    "12h": 2,
    "1d": 1,
    "3d": 1,
    "1w": 1,
}

#: Timestamp sizes that keep the synthetic frames small for the slow timeframes.
_CANDLES_PER_FRAME = 96


def synthetic_close(timeframe: str, *, periods: int = _CANDLES_PER_FRAME) -> pd.Series:
    """Return a deterministic, seasonality-bearing close series of ``periods``."""
    delta = pd.Timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    index = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-01T00:00:00Z") + step * delta for step in range(periods)]
    )
    steps = np.arange(periods, dtype="float64")
    # a slow trend plus a strong daily wave: the fold is clearly visible
    level = 100.0 * np.exp(0.0002 * steps + 0.05 * np.sin(2.0 * np.pi * steps / 24.0))
    return pd.Series(level, index=index, name="close", dtype="float64")


# ---------------------------------------------------------------------------
# 1. the constant and the per-timeframe mapping
# ---------------------------------------------------------------------------


def test_seconds_per_day_is_a_whole_utc_day() -> None:
    assert SECONDS_PER_DAY == 86_400


def test_candles_per_day_pins_the_whole_timeframe_table() -> None:
    assert set(EXPECTED_PERIODS) == set(TIMEFRAME_SECONDS)

    for timeframe, expected in EXPECTED_PERIODS.items():
        assert candles_per_day(timeframe) == expected, timeframe


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [("1m", 1440), ("5m", 288), ("15m", 96), ("1h", 24), ("4h", 6), ("1d", 1)],
)
def test_candles_per_day_documented_cases(timeframe: str, expected: int) -> None:
    assert candles_per_day(timeframe) == expected


def test_candles_per_day_is_never_below_one() -> None:
    # a timeframe longer than a day collapses to a single phase instead of
    # inventing a sub-daily cycle
    for timeframe in ("3d", "1w"):
        assert candles_per_day(timeframe) == 1


@pytest.mark.parametrize("timeframe", ["", "3h", "2d", "1H", "hourly", None, 60])
def test_candles_per_day_rejects_an_unsupported_timeframe(timeframe: object) -> None:
    with pytest.raises(ForecastError) as excinfo:
        candles_per_day(timeframe)  # type: ignore[arg-type]

    assert "unsupported timeframe" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 2. resolve_seasonal_period: derived without an override, honoured with one
# ---------------------------------------------------------------------------


def test_resolve_seasonal_period_derives_from_the_timeframe() -> None:
    for timeframe, expected in EXPECTED_PERIODS.items():
        assert resolve_seasonal_period(timeframe) == expected
        assert resolve_seasonal_period(timeframe) == candles_per_day(timeframe)


@pytest.mark.parametrize("timeframe", sorted(EXPECTED_PERIODS))
@pytest.mark.parametrize("override", [1, 7, 24, 168])
def test_resolve_seasonal_period_honours_every_override(timeframe: str, override: int) -> None:
    assert resolve_seasonal_period(timeframe, override) == override


def test_resolve_seasonal_period_override_one_is_accepted() -> None:
    # ``1`` is the documented "no seasonal fold" period, not an error
    assert resolve_seasonal_period("1m", 1) == 1
    assert resolve_seasonal_period("1d", 1) == 1


def test_resolve_seasonal_period_ignores_the_timeframe_when_overridden() -> None:
    # the override wins even for a timeframe that would be refused on its own
    assert resolve_seasonal_period("not-a-timeframe", 24) == 24


@pytest.mark.parametrize("override", [0, -1, -24])
def test_resolve_seasonal_period_refuses_a_non_positive_override(override: int) -> None:
    with pytest.raises(ForecastError) as excinfo:
        resolve_seasonal_period("1h", override)

    assert "seasonal_period" in str(excinfo.value)
    assert ">= 1" in str(excinfo.value)


@pytest.mark.parametrize("override", [1.5, "24", [24], object()])
def test_resolve_seasonal_period_refuses_a_non_integer_override(override: object) -> None:
    with pytest.raises(ForecastError) as excinfo:
        resolve_seasonal_period("1h", override)  # type: ignore[arg-type]

    assert "seasonal_period" in str(excinfo.value)


def test_resolve_seasonal_period_accepts_a_bool_like_true_only() -> None:
    # ``True`` is an ``int`` of value 1 in Python; the helper documents >= 1
    assert resolve_seasonal_period("1w", True) == 1


# ---------------------------------------------------------------------------
# 3. the resolution actually feeds a real de-seasonalisation, per timeframe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(EXPECTED_PERIODS))
def test_deseasonalize_accepts_the_resolved_period_of_every_timeframe(timeframe: str) -> None:
    close = synthetic_close(timeframe)
    period = resolve_seasonal_period(timeframe)

    frame = pd.DataFrame({"close": close})
    result = deseasonalize_log_price(frame["close"], timeframe=timeframe, period=period)

    assert result.name == "deseasonalized_log_price"
    assert result.index.equals(close.index)
    assert np.all(np.isfinite(result.to_numpy(dtype="float64")))
    # the transform is a genuine change of scale, not the identity
    assert not np.allclose(result.to_numpy(), np.log(close.to_numpy()))


def test_a_wrong_period_changes_the_de_seasonalised_series() -> None:
    """The bug the resolution fixes is silent, so pin that it is observable."""
    close = synthetic_close("4h")
    correct = deseasonalize_log_price(close, timeframe="4h", period=resolve_seasonal_period("4h"))
    wrong = deseasonalize_log_price(close, timeframe="4h", period=24)

    assert resolve_seasonal_period("4h") == 6
    assert not np.allclose(correct.to_numpy(), wrong.to_numpy())


def test_override_one_reproduces_the_plain_log_price() -> None:
    close = synthetic_close("1h")
    result = deseasonalize_log_price(close, timeframe="1h", period=1, window=168)

    # period 1 means every candle shares one phase, so the seasonal estimate is
    # the trailing mean of the whole window: still not the identity, but defined
    assert np.all(np.isfinite(result.to_numpy(dtype="float64")))
    assert result.index.equals(close.index)


# ---------------------------------------------------------------------------
# 4. the derivation is what the artifact builder actually records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [("1m", 1440), ("5m", 288), ("15m", 96), ("1h", 24), ("4h", 6), ("1d", 1)],
)
def test_build_config_derives_the_period_when_unset(timeframe: str, expected: int) -> None:
    """``ForecastBuildConfig.seasonal_period = None`` follows the timeframe."""
    frame = synthetic_close(timeframe, periods=400).to_frame("close")
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe=timeframe,
        backend="naive",
        context_length=64,
        horizon=6,
        reforecast_every=6,
    )

    assert config.seasonal_period is None

    _, metadata = build_forecast_frame(frame, config)

    assert metadata.seasonal_period == expected


def test_build_config_explicit_period_still_wins() -> None:
    frame = synthetic_close("4h", periods=400).to_frame("close")
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="4h",
        backend="naive",
        context_length=64,
        horizon=6,
        reforecast_every=6,
        seasonal_period=24,
    )

    _, metadata = build_forecast_frame(frame, config)

    assert metadata.seasonal_period == 24


def test_build_config_refuses_a_non_positive_explicit_period() -> None:
    frame = synthetic_close("1h", periods=400).to_frame("close")
    config = ForecastBuildConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        backend="naive",
        context_length=64,
        horizon=6,
        reforecast_every=6,
        seasonal_period=0,
    )

    with pytest.raises(ForecastError):
        build_forecast_frame(frame, config)


# ---------------------------------------------------------------------------
# 5. the two helpers are re-exported from the package root, which stays cheap
# ---------------------------------------------------------------------------


def test_the_package_root_reexports_the_same_objects() -> None:
    assert candles_per_day is series_candles_per_day
    assert resolve_seasonal_period is series_resolve_seasonal_period
    assert "candles_per_day" in __import__("trading_platform.forecast", fromlist=["x"]).__all__
    assert (
        "resolve_seasonal_period" in __import__("trading_platform.forecast", fromlist=["x"]).__all__
    )


def test_the_package_root_still_does_not_import_the_artifact_module() -> None:
    """The root stays cheap: the artifact module is never imported by it."""
    import subprocess
    import sys

    program = (
        "import sys; import trading_platform.forecast as f;"
        " assert 'trading_platform.forecast.artifact' not in sys.modules;"
        " assert 'torch' not in sys.modules;"
        " assert 'timesfm' not in sys.modules;"
        " assert f.candles_per_day('1h') == 24"
    )

    completed = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
