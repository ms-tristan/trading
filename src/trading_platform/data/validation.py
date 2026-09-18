"""OHLCV quality control: contract enforcement and data-quality reporting.

Two complementary entry points are provided:

``ensure_ohlcv``
    Strict, structural.  Returns a *copy* of the frame that satisfies the OHLCV
    contract, or raises :class:`~trading_platform.core.errors.DataValidationError`
    listing every violated rule.

``validate_ohlcv``
    Statistical.  Never raises unless asked to, and returns a
    :class:`DataQualityReport` describing gaps, duplicates, NaN and missing
    candles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from trading_platform.core.constants import (
    DEFAULT_TIMEFRAME,
    OHLCV_INDEX_NAME,
    REQUIRED_OHLCV_COLUMNS,
    UTC,
    candle_delta,
)
from trading_platform.core.errors import DataValidationError

__all__ = [
    "DataQualityReport",
    "ensure_ohlcv",
    "validate_ohlcv",
]

_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")


@dataclass(frozen=True)
class DataQualityReport:
    """Outcome of :func:`validate_ohlcv`.

    ``ok`` is ``True`` when no issue at all was detected; ``is_regular`` is
    stricter and additionally requires a perfectly regular candle grid (no
    missing candle, no gap, no duplicate).
    """

    n_rows: int
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    n_missing: int
    missing_ratio: float
    max_gap_candles: int
    duplicate_timestamps: int
    n_nan: int
    n_non_positive: int
    is_regular: bool
    issues: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """``True`` when no data-quality issue was detected."""
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the report."""
        return {
            "n_rows": int(self.n_rows),
            "start": None if self.start is None else pd.Timestamp(self.start).isoformat(),
            "end": None if self.end is None else pd.Timestamp(self.end).isoformat(),
            "n_missing": int(self.n_missing),
            "missing_ratio": float(self.missing_ratio),
            "max_gap_candles": int(self.max_gap_candles),
            "duplicate_timestamps": int(self.duplicate_timestamps),
            "n_nan": int(self.n_nan),
            "n_non_positive": int(self.n_non_positive),
            "is_regular": bool(self.is_regular),
            "ok": bool(self.ok),
            "issues": list(self.issues),
        }


def ensure_ohlcv(data: pd.DataFrame, *, name: str = "data") -> pd.DataFrame:
    """Return a copy of ``data`` that satisfies the OHLCV contract.

    Guarantees on the returned frame:

    * ``float64`` values for every required column;
    * a timezone-aware UTC ``DatetimeIndex`` named ``"timestamp"``;
    * an ascending index, with duplicated timestamps removed
      (``keep="last"``, i.e. the newest row wins);
    * the five :data:`~trading_platform.core.constants.REQUIRED_OHLCV_COLUMNS`
      present (extra columns such as signals are preserved).

    The input frame is never mutated.

    Raises
    ------
    DataValidationError
        If the input is not a DataFrame, if required columns are missing, if the
        index is not a ``DatetimeIndex`` or if a required column cannot be cast
        to ``float64``.  The error lists every violated rule.
    """
    if not isinstance(data, pd.DataFrame):
        raise DataValidationError(
            f"{name} must be a pandas DataFrame, got {type(data).__name__}",
            [f"{name} is a {type(data).__name__}, expected pandas.DataFrame"],
        )

    issues: list[str] = []
    missing_columns = [column for column in REQUIRED_OHLCV_COLUMNS if column not in data.columns]
    if missing_columns:
        issues.append(f"missing required column(s): {', '.join(missing_columns)}")
    if not isinstance(data.index, pd.DatetimeIndex):
        issues.append(f"index must be a DatetimeIndex, got {type(data.index).__name__}")
    if issues:
        raise DataValidationError(f"{name} does not satisfy the OHLCV contract", issues)

    out = data.copy(deep=True)
    unconvertible: list[str] = []
    for column in REQUIRED_OHLCV_COLUMNS:
        try:
            out[column] = pd.to_numeric(out[column], errors="raise").astype("float64")
        except (TypeError, ValueError):
            unconvertible.append(column)
    if unconvertible:
        raise DataValidationError(
            f"{name} does not satisfy the OHLCV contract",
            [f"column(s) not convertible to float64: {', '.join(unconvertible)}"],
        )

    index = pd.DatetimeIndex(out.index)
    index = index.tz_localize(UTC) if index.tz is None else index.tz_convert(UTC)
    index.name = OHLCV_INDEX_NAME
    out.index = index
    if out.index.has_duplicates:
        out = out[~out.index.duplicated(keep="last")]
    if not out.index.is_monotonic_increasing:
        out = out.sort_index()
    return out


def validate_ohlcv(
    data: pd.DataFrame,
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
    max_gap_factor: float = 3.0,
    max_missing_ratio: float = 0.0,
    raise_on_error: bool = False,
) -> DataQualityReport:
    """Analyse ``data`` and return a :class:`DataQualityReport`.

    ``expected`` candles are derived from the first and last timestamps and the
    candle duration of ``timeframe``; ``n_missing`` is the difference with the
    number of available rows and ``missing_ratio`` is normalised by ``expected``.

    Parameters
    ----------
    max_gap_factor:
        Maximum tolerated hole between two consecutive candles, expressed in
        candle units (``3.0`` means "up to 3 missing candles in a row").
    max_missing_ratio:
        Maximum tolerated share of missing candles over the whole window.
    raise_on_error:
        When ``True`` and the report is not ``ok``, raise
        :class:`~trading_platform.core.errors.DataValidationError` carrying the
        issues instead of returning the report.

    Raises
    ------
    DataValidationError
        If ``data`` is not a DataFrame, or if ``raise_on_error`` is ``True`` and
        at least one issue was found.
    """
    if not isinstance(data, pd.DataFrame):
        raise DataValidationError(
            f"cannot validate a {type(data).__name__}, expected a pandas DataFrame"
        )

    issues: list[str] = []
    n_rows = len(data)

    present = [column for column in REQUIRED_OHLCV_COLUMNS if column in data.columns]
    absent = [column for column in REQUIRED_OHLCV_COLUMNS if column not in data.columns]
    if absent:
        issues.append(f"missing required column(s): {', '.join(absent)}")

    n_nan = int(sum(int(data[column].isna().sum()) for column in present))
    if n_nan:
        issues.append(f"nan values found in OHLCV columns: {n_nan} cell(s)")

    price_columns = [column for column in _PRICE_COLUMNS if column in data.columns]
    n_non_positive = 0
    for column in price_columns:
        numeric = pd.to_numeric(data[column], errors="coerce")
        n_non_positive += int((numeric <= 0).sum())
    if n_non_positive:
        issues.append(f"non-positive price value(s): {n_non_positive} cell(s) in OHLC columns")

    if "volume" in data.columns:
        volume = pd.to_numeric(data["volume"], errors="coerce")
        n_bad_volume = int((volume <= 0).sum())
        if n_bad_volume:
            issues.append(f"non-positive volume: {n_bad_volume} cell(s)")

    index = data.index
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    n_missing = 0
    missing_ratio = 0.0
    max_gap_candles = 0
    duplicate_timestamps = 0
    is_regular = False

    if not isinstance(index, pd.DatetimeIndex):
        issues.append(f"index must be a DatetimeIndex, got {type(index).__name__}")
    elif n_rows == 0:
        issues.append("empty frame: no candle to validate")
    else:
        start = pd.Timestamp(index[0])
        end = pd.Timestamp(index[-1])
        if index.tz is None:
            issues.append("index is timezone-naive, expected tz-aware UTC timestamps")
        duplicate_timestamps = int(index.duplicated().sum())
        if duplicate_timestamps:
            issues.append(
                f"duplicate timestamps: {duplicate_timestamps} row(s) reuse an existing candle"
            )
        if not index.is_monotonic_increasing:
            issues.append("index is not sorted in ascending order")
        else:
            delta = candle_delta(timeframe)
            expected = int((index[-1] - index[0]) / delta) + 1
            n_missing = max(0, expected - n_rows)
            missing_ratio = n_missing / expected if expected else 0.0
            steps = (index.to_series().diff().dropna() / delta).to_numpy(dtype="float64")
            if steps.size:
                max_gap_candles = int(max(0.0, float(np.rint(steps.max())) - 1.0))
            is_regular = n_missing == 0 and max_gap_candles == 0 and duplicate_timestamps == 0
            if n_missing and missing_ratio > max_missing_ratio:
                issues.append(
                    f"missing candles: {n_missing} of {expected} expected "
                    f"({missing_ratio:.2%}) exceeds max_missing_ratio={max_missing_ratio:.2%}"
                )
            if max_gap_candles > max_gap_factor:
                issues.append(
                    f"gap of {max_gap_candles} candle(s) between consecutive rows "
                    f"exceeds max_gap_factor={max_gap_factor}"
                )

    report = DataQualityReport(
        n_rows=n_rows,
        start=start,
        end=end,
        n_missing=n_missing,
        missing_ratio=missing_ratio,
        max_gap_candles=max_gap_candles,
        duplicate_timestamps=duplicate_timestamps,
        n_nan=n_nan,
        n_non_positive=n_non_positive,
        is_regular=is_regular,
        issues=tuple(issues),
    )
    if raise_on_error and not report.ok:
        raise DataValidationError(
            f"{timeframe} OHLCV data failed validation ({n_rows} row(s))", report.issues
        )
    return report
