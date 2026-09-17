"""In-sample / out-of-sample splitting and walk-forward window construction.

This module is the geometric foundation of the validation layer: it turns a
contract-conformant OHLCV frame into either a single IS/OOS split
(:func:`split_is_oos`) or a series of walk-forward windows
(:func:`make_windows`).

Two invariants are enforced everywhere:

* every slice is a **contiguous** block of the input frame — columns and their
  order are preserved and the index is never rebuilt (no ``reindex``);
* in-sample data always *ends before* out-of-sample data starts, and
  ``purge_candles`` rows are removed from the **end of the in-sample block** so
  that the purged candles sit strictly between the two slices (this is the
  mechanical answer to the leakage problem: neither side sees the other's
  boundary).

Nothing here runs a backtest: the runner is always injected by the caller.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from trading_backtest.core.errors import (
    InsufficientDataError,
    ValidationLayerError,
)
from trading_backtest.data.validation import ensure_ohlcv

__all__ = [
    "MIN_ROWS_PER_SLICE",
    "WINDOW_MODES",
    "Window",
    "make_windows",
    "split_is_oos",
]

#: Minimum number of candles held by each side of a split (in-sample and
#: out-of-sample).  A one-candle slice is considered degenerate: no metric and
#: no trade can be derived from it, so :func:`split_is_oos` refuses to produce
#: one and raises :class:`~trading_backtest.core.errors.InsufficientDataError`.
MIN_ROWS_PER_SLICE = 2

#: Supported walk-forward schemes.
WINDOW_MODES: tuple[str, ...] = ("rolling", "anchored")


@dataclass(frozen=True)
class Window:
    """One walk-forward window, described by the timestamps of its four edges.

    All timestamps are the *actual* first/last timestamps of the corresponding
    slices of the input frame (inclusive bounds), so a window is enough to
    recover its two slices without storing rows.
    """

    index: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping (timestamps as ISO-8601 strings)."""
        return {
            "index": int(self.index),
            "is_start": pd.Timestamp(self.is_start).isoformat(),
            "is_end": pd.Timestamp(self.is_end).isoformat(),
            "oos_start": pd.Timestamp(self.oos_start).isoformat(),
            "oos_end": pd.Timestamp(self.oos_end).isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Window:
        """Exact inverse of :meth:`to_dict`."""
        return cls(
            index=int(payload["index"]),
            is_start=pd.Timestamp(payload["is_start"]),
            is_end=pd.Timestamp(payload["is_end"]),
            oos_start=pd.Timestamp(payload["oos_start"]),
            oos_end=pd.Timestamp(payload["oos_end"]),
        )


# ---------------------------------------------------------------------------
# parameter guards
# ---------------------------------------------------------------------------


def _check_ratio(value: float) -> float:
    """Validate ``in_sample_ratio`` and return it as a float."""
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        raise ValidationLayerError(
            f"in_sample_ratio must be a float in the open interval (0, 1), got {value!r}"
        ) from None
    if not math.isfinite(ratio) or not 0.0 < ratio < 1.0:
        raise ValidationLayerError(
            f"in_sample_ratio must be in the open interval (0, 1), got {value!r}"
        )
    return ratio


def _check_purge(value: int) -> int:
    """Validate ``purge_candles`` and return it as an int."""
    if isinstance(value, bool):
        raise ValidationLayerError(f"purge_candles must be a non-negative integer, got {value!r}")
    try:
        purge = int(value)
    except (TypeError, ValueError):
        raise ValidationLayerError(
            f"purge_candles must be a non-negative integer, got {value!r}"
        ) from None
    if purge != value or purge < 0:
        raise ValidationLayerError(f"purge_candles must be a non-negative integer, got {value!r}")
    return purge


def _check_n_windows(value: int) -> int:
    """Validate ``n_windows`` and return it as an int."""
    if isinstance(value, bool):
        raise ValidationLayerError(f"n_windows must be an integer >= 1, got {value!r}")
    try:
        n_windows = int(value)
    except (TypeError, ValueError):
        raise ValidationLayerError(f"n_windows must be an integer >= 1, got {value!r}") from None
    if n_windows != value or n_windows < 1:
        raise ValidationLayerError(f"n_windows must be an integer >= 1, got {value!r}")
    return n_windows


def _check_mode(value: str) -> Literal["rolling", "anchored"]:
    """Validate the walk-forward scheme."""
    if value not in WINDOW_MODES:
        raise ValidationLayerError(
            f"unknown walk-forward mode: {value!r} (available: {', '.join(WINDOW_MODES)})"
        )
    return "rolling" if value == "rolling" else "anchored"


def _contract_frame(data: pd.DataFrame) -> pd.DataFrame:
    """Return the contract-conformant copy of ``data`` (raises on bad input)."""
    return ensure_ohlcv(data, name="data")


# ---------------------------------------------------------------------------
# single split
# ---------------------------------------------------------------------------


def split_is_oos(
    data: pd.DataFrame,
    *,
    in_sample_ratio: float = 0.7,
    purge_candles: int = 0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split ``data`` into an in-sample frame and an out-of-sample frame.

    The split is positional and mechanical:

    * the in-sample slice is the first ``floor(n_rows * in_sample_ratio)`` rows
      **minus** the ``purge_candles`` rows dropped from its end;
    * the out-of-sample slice is the remaining row range, starting exactly at
      ``floor(n_rows * in_sample_ratio)``;
    * therefore exactly ``purge_candles`` rows of the input sit between the two
      returned frames and belong to neither of them.

    Both returned frames keep the columns (and their order) of the input and
    keep their original index labels: the input is never reindexed, and the two
    slices are disjoint.

    Parameters
    ----------
    data:
        OHLCV frame satisfying the contract enforced by
        :func:`trading_backtest.data.validation.ensure_ohlcv`.
    in_sample_ratio:
        Share of the rows used for the in-sample slice; must lie strictly
        between 0 and 1.
    purge_candles:
        Number of rows dropped between the two slices (embargo).

    Returns
    -------
    tuple of pandas.DataFrame
        ``(in_sample, out_of_sample)``.

    Raises
    ------
    ValidationLayerError
        If ``in_sample_ratio`` is outside ``(0, 1)`` or ``purge_candles`` is
        not a non-negative integer.
    DataValidationError
        If ``data`` does not satisfy the OHLCV contract.
    InsufficientDataError
        If either side would hold fewer than :data:`MIN_ROWS_PER_SLICE` rows.
    """
    ratio = _check_ratio(in_sample_ratio)
    purge = _check_purge(purge_candles)
    frame = _contract_frame(data)

    n_rows = len(frame)
    n_in_sample = math.floor(n_rows * ratio)
    in_sample_rows = n_in_sample - purge
    out_sample_rows = n_rows - n_in_sample

    if in_sample_rows < MIN_ROWS_PER_SLICE or out_sample_rows < MIN_ROWS_PER_SLICE:
        raise InsufficientDataError(
            f"cannot split {n_rows} row(s) with in_sample_ratio={ratio:g} and "
            f"purge_candles={purge}: in-sample would hold {max(in_sample_rows, 0)} row(s) and "
            f"out-of-sample {out_sample_rows} row(s), each side needs at least "
            f"{MIN_ROWS_PER_SLICE} row(s)",
            [
                f"in-sample slice: {max(in_sample_rows, 0)} row(s) (minimum {MIN_ROWS_PER_SLICE})",
                f"out-of-sample slice: {out_sample_rows} row(s) (minimum {MIN_ROWS_PER_SLICE})",
            ],
        )

    in_sample = frame.iloc[0:in_sample_rows]
    out_of_sample = frame.iloc[n_in_sample:]
    return in_sample, out_of_sample


# ---------------------------------------------------------------------------
# walk-forward windows
# ---------------------------------------------------------------------------


def make_windows(
    data: pd.DataFrame,
    *,
    n_windows: int = 5,
    in_sample_ratio: float = 0.7,
    mode: Literal["rolling", "anchored"] = "rolling",
    purge_candles: int = 0,
) -> list[Window]:
    """Cut ``data`` into ``n_windows`` contiguous walk-forward blocks.

    ``block = n_rows // n_windows`` rows are used per window; the trailing
    ``n_rows % n_windows`` rows are deliberately left out so that every block
    has exactly the same size.  Within block ``i`` (rows
    ``[i * block, (i + 1) * block)``):

    ``rolling``
        in-sample = the first ``floor(block * in_sample_ratio)`` rows of the
        block minus the purge rows; out-of-sample = the rest of the block;
    ``anchored``
        in-sample = rows ``[0, i * block + floor(block * in_sample_ratio))``
        minus the purge rows at its end (so it grows with ``i`` and always
        starts at the very first candle); out-of-sample = the same tail of
        block ``i`` as in rolling mode.

    Windows are returned in ascending ``index`` order, their out-of-sample
    ranges never overlap, and every window has a non-empty in-sample and
    out-of-sample slice.

    Raises
    ------
    ValidationLayerError
        If ``n_windows < 1``, if ``mode`` is unknown, or if the ratio/purge
        arguments are invalid.
    DataValidationError
        If ``data`` does not satisfy the OHLCV contract.
    InsufficientDataError
        If ``data`` holds fewer than ``2 * n_windows`` rows, or if a block
        cannot hold at least one in-sample and one out-of-sample row after the
        purge.
    """
    windows_count = _check_n_windows(n_windows)
    ratio = _check_ratio(in_sample_ratio)
    purge = _check_purge(purge_candles)
    window_mode = _check_mode(mode)
    frame = _contract_frame(data)

    n_rows = len(frame)
    min_rows = windows_count * 2
    if n_rows < min_rows:
        raise InsufficientDataError(
            f"cannot build {windows_count} walk-forward window(s) from {n_rows} row(s): "
            f"at least {min_rows} row(s) are required (two rows per window)",
            [
                f"available rows: {n_rows}",
                f"required rows: {min_rows} = 2 * n_windows ({windows_count})",
            ],
        )

    block = n_rows // windows_count
    index = pd.DatetimeIndex(frame.index)
    is_length = math.floor(block * ratio)
    windows: list[Window] = []

    for window_index in range(windows_count):
        block_start = window_index * block
        block_end = block_start + block
        oos_start_position = block_start + is_length
        oos_stop_position = block_end
        is_start_position = block_start if window_mode == "rolling" else 0
        is_stop_position = oos_start_position - purge
        if is_stop_position - is_start_position < 1 or oos_stop_position - oos_start_position < 1:
            raise InsufficientDataError(
                f"window {window_index} is degenerate with n_windows={windows_count}, "
                f"in_sample_ratio={ratio:g} and purge_candles={purge}: in-sample would hold "
                f"{max(is_stop_position - is_start_position, 0)} row(s) and out-of-sample "
                f"{oos_stop_position - oos_start_position} row(s)",
                [
                    f"block size: {block} row(s)",
                    f"in-sample length before purge: {is_length} row(s)",
                ],
            )
        windows.append(
            Window(
                index=window_index,
                is_start=index[is_start_position],
                is_end=index[is_stop_position - 1],
                oos_start=index[oos_start_position],
                oos_end=index[oos_stop_position - 1],
            )
        )
    return windows
