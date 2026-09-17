"""Contract tests for OHLCV validation (``ensure_ohlcv`` / ``validate_ohlcv``)."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.errors import DataValidationError
from trading_backtest.data.synthetic import make_ohlcv
from trading_backtest.data.validation import DataQualityReport, ensure_ohlcv, validate_ohlcv


def _issues(report: DataQualityReport) -> str:
    return " | ".join(report.issues)


# ---------------------------------------------------------------------------
# validate_ohlcv -- happy path
# ---------------------------------------------------------------------------


def test_clean_frame_is_ok_without_issues(ohlcv_frame: pd.DataFrame) -> None:
    report = validate_ohlcv(ohlcv_frame)
    assert report.ok is True
    assert report.issues == ()
    assert report.n_rows == len(ohlcv_frame)
    assert report.n_missing == 0
    assert report.missing_ratio == 0.0
    assert report.max_gap_candles == 0
    assert report.duplicate_timestamps == 0
    assert report.n_nan == 0
    assert report.n_non_positive == 0
    assert report.is_regular is True
    assert report.start == ohlcv_frame.index[0]
    assert report.end == ohlcv_frame.index[-1]


def test_report_is_json_serialisable_and_frozen(ohlcv_frame: pd.DataFrame) -> None:
    report = validate_ohlcv(ohlcv_frame)
    payload = report.to_dict()
    assert json.loads(json.dumps(payload))["ok"] is True
    assert payload["start"].endswith("+00:00")
    assert isinstance(payload["issues"], list)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.n_rows = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# validate_ohlcv -- issues
# ---------------------------------------------------------------------------


def test_five_candle_gap_is_detected(ohlcv_frame: pd.DataFrame) -> None:
    gapped = ohlcv_frame.drop(ohlcv_frame.index[10:15])
    report = validate_ohlcv(gapped, max_gap_factor=3.0, max_missing_ratio=1.0)
    assert report.max_gap_candles == 5
    assert report.ok is False
    assert "gap" in _issues(report)
    assert report.is_regular is False


def test_gap_within_the_tolerance_is_accepted(ohlcv_frame: pd.DataFrame) -> None:
    gapped = ohlcv_frame.drop(ohlcv_frame.index[[10]])
    report = validate_ohlcv(gapped, max_gap_factor=3.0, max_missing_ratio=1.0)
    assert report.max_gap_candles == 1
    assert "gap" not in _issues(report)


def test_nan_row_is_detected(ohlcv_frame: pd.DataFrame) -> None:
    broken = ohlcv_frame.copy()
    broken.iloc[7, 3] = np.nan
    report = validate_ohlcv(broken)
    assert report.n_nan == 1
    assert report.ok is False
    assert "nan" in _issues(report).lower()


def test_duplicate_timestamps_are_detected(ohlcv_frame: pd.DataFrame) -> None:
    duplicated = pd.concat([ohlcv_frame.iloc[:20], ohlcv_frame.iloc[[5]], ohlcv_frame.iloc[20:]])
    report = validate_ohlcv(duplicated)
    assert report.duplicate_timestamps == 1
    assert report.ok is False
    assert "duplicate" in _issues(report)


def test_non_positive_price_is_detected(ohlcv_frame: pd.DataFrame) -> None:
    broken = ohlcv_frame.copy()
    broken.iloc[3, broken.columns.get_loc("close")] = 0.0
    broken.iloc[4, broken.columns.get_loc("low")] = -1.0
    report = validate_ohlcv(broken)
    assert report.n_non_positive == 2
    assert report.ok is False
    assert "non-positive price" in _issues(report)


def test_non_positive_volume_is_detected(ohlcv_frame: pd.DataFrame) -> None:
    broken = ohlcv_frame.copy()
    broken.iloc[2, broken.columns.get_loc("volume")] = 0.0
    report = validate_ohlcv(broken)
    assert report.n_non_positive == 0
    assert "non-positive volume" in _issues(report)


def test_empty_frame_is_detected(ohlcv_frame: pd.DataFrame) -> None:
    report = validate_ohlcv(ohlcv_frame.iloc[:0])
    assert report.n_rows == 0
    assert report.start is None
    assert report.end is None
    assert report.ok is False
    assert "empty" in _issues(report)


def test_unsorted_index_is_detected(ohlcv_frame: pd.DataFrame) -> None:
    shuffled = ohlcv_frame.iloc[::-1]
    report = validate_ohlcv(shuffled)
    assert report.ok is False
    assert "sorted" in _issues(report)


def test_timezone_naive_index_is_detected(ohlcv_frame: pd.DataFrame) -> None:
    naive = ohlcv_frame.copy()
    naive.index = naive.index.tz_localize(None)
    report = validate_ohlcv(naive)
    assert report.ok is False
    assert "tz-aware" in _issues(report)


def test_non_datetime_index_is_detected(ohlcv_frame: pd.DataFrame) -> None:
    report = validate_ohlcv(ohlcv_frame.reset_index(drop=True))
    assert report.ok is False
    assert "DatetimeIndex" in _issues(report)


def test_missing_required_column_is_detected(ohlcv_frame: pd.DataFrame) -> None:
    report = validate_ohlcv(ohlcv_frame.drop(columns=["volume"]))
    assert report.ok is False
    assert "missing" in _issues(report)
    assert "volume" in _issues(report)


def test_unknown_timeframe_raises_config_error(ohlcv_frame: pd.DataFrame) -> None:
    from trading_backtest.core.errors import ConfigError

    with pytest.raises(ConfigError):
        validate_ohlcv(ohlcv_frame, timeframe="2h")


def test_non_dataframe_input_raises() -> None:
    with pytest.raises(DataValidationError):
        validate_ohlcv([1, 2, 3])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_ohlcv -- missing-ratio boundary and raise_on_error
# ---------------------------------------------------------------------------


def test_max_missing_ratio_boundary() -> None:
    frame = make_ohlcv(20)
    at_limit = frame.drop(frame.index[[5, 10]])
    report = validate_ohlcv(at_limit, max_gap_factor=10.0, max_missing_ratio=0.1)
    assert report.n_rows == 18
    assert report.n_missing == 2
    assert report.missing_ratio == pytest.approx(0.1)
    assert report.ok is True, report.issues

    above_limit = frame.drop(frame.index[[5, 10, 15]])
    report = validate_ohlcv(above_limit, max_gap_factor=10.0, max_missing_ratio=0.1)
    assert report.n_missing == 3
    assert report.missing_ratio == pytest.approx(0.15)
    assert report.ok is False
    assert "missing" in _issues(report)


def test_raise_on_error_carries_the_issues(ohlcv_frame: pd.DataFrame) -> None:
    broken = ohlcv_frame.copy()
    broken.iloc[7, 3] = np.nan
    report = validate_ohlcv(broken)
    with pytest.raises(DataValidationError) as excinfo:
        validate_ohlcv(broken, raise_on_error=True)
    assert excinfo.value.issues == report.issues
    assert excinfo.value.issues
    assert "nan" in str(excinfo.value).lower()


def test_raise_on_error_does_not_raise_on_a_clean_frame(ohlcv_frame: pd.DataFrame) -> None:
    report = validate_ohlcv(ohlcv_frame, raise_on_error=True)
    assert report.ok is True


# ---------------------------------------------------------------------------
# ensure_ohlcv
# ---------------------------------------------------------------------------


def test_ensure_ohlcv_returns_a_copy_and_never_mutates_the_input(ohlcv_frame: pd.DataFrame) -> None:
    before = ohlcv_frame.copy(deep=True)
    out = ensure_ohlcv(ohlcv_frame)
    assert out is not ohlcv_frame
    out.iloc[0, 0] = -999.0
    assert ohlcv_frame.iloc[0, 0] != -999.0
    pd.testing.assert_frame_equal(ohlcv_frame, before)


def test_ensure_ohlcv_coerces_to_float64(ohlcv_frame: pd.DataFrame) -> None:
    frame = ohlcv_frame.copy()
    frame["open"] = frame["open"].round(2).astype("int64")
    out = ensure_ohlcv(frame)
    assert all(dtype == np.dtype("float64") for dtype in out.dtypes)
    assert frame["open"].dtype == np.dtype("int64")  # input untouched


def test_ensure_ohlcv_sorts_sets_index_name_and_timezone(ohlcv_frame: pd.DataFrame) -> None:
    frame = ohlcv_frame.iloc[::-1].copy()
    frame.index = frame.index.tz_localize(None)
    out = ensure_ohlcv(frame)
    assert out.index.is_monotonic_increasing
    assert out.index.name == "timestamp"
    assert str(out.index.tz) == "UTC"
    assert out.index[0] == ohlcv_frame.index[0]


def test_ensure_ohlcv_dedupes_keeping_the_last_occurrence(ohlcv_frame: pd.DataFrame) -> None:
    frame = pd.concat([ohlcv_frame.iloc[:5], ohlcv_frame.iloc[[2]].assign(close=1.0)])
    out = ensure_ohlcv(frame)
    assert not out.index.has_duplicates
    assert len(out) == 5
    assert out["close"].iloc[2] == pytest.approx(1.0)


def test_ensure_ohlcv_preserves_extra_columns(ohlcv_frame: pd.DataFrame) -> None:
    frame = ohlcv_frame.assign(entry_long=True)
    out = ensure_ohlcv(frame)
    assert "entry_long" in out.columns
    assert list(out.columns) == [*ohlcv_frame.columns, "entry_long"]


def test_ensure_ohlcv_reports_missing_columns(ohlcv_frame: pd.DataFrame) -> None:
    with pytest.raises(DataValidationError) as excinfo:
        ensure_ohlcv(ohlcv_frame.drop(columns=["volume"]))
    assert "volume" in str(excinfo.value)
    assert any("volume" in issue for issue in excinfo.value.issues)


def test_ensure_ohlcv_reports_a_non_datetime_index(ohlcv_frame: pd.DataFrame) -> None:
    with pytest.raises(DataValidationError) as excinfo:
        ensure_ohlcv(ohlcv_frame.reset_index(drop=True))
    assert "DatetimeIndex" in str(excinfo.value)


def test_ensure_ohlcv_reports_every_violation_at_once(ohlcv_frame: pd.DataFrame) -> None:
    broken = ohlcv_frame.drop(columns=["close"]).reset_index(drop=True)
    with pytest.raises(DataValidationError) as excinfo:
        ensure_ohlcv(broken, name="myframe")
    assert len(excinfo.value.issues) == 2
    assert "myframe" in str(excinfo.value)


def test_ensure_ohlcv_reports_unconvertible_columns(ohlcv_frame: pd.DataFrame) -> None:
    frame = ohlcv_frame.assign(low="not-a-number")
    with pytest.raises(DataValidationError) as excinfo:
        ensure_ohlcv(frame)
    assert any("float64" in issue for issue in excinfo.value.issues)


def test_ensure_ohlcv_rejects_non_dataframes() -> None:
    with pytest.raises(DataValidationError) as excinfo:
        ensure_ohlcv("nope")  # type: ignore[arg-type]
    assert "DataFrame" in str(excinfo.value)


def test_ensure_ohlcv_accepts_an_empty_frame(ohlcv_frame: pd.DataFrame) -> None:
    out = ensure_ohlcv(ohlcv_frame.iloc[:0])
    assert len(out) == 0
    assert out.index.name == "timestamp"
    assert str(out.index.tz) == "UTC"
