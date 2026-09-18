"""Unit tests for the IS/OOS splitting layer (``trading_platform.validation.split``).

Everything is offline and deterministic: the frames come from the shared
synthetic generators and every expected value is derived by hand from the frame
geometry (no engine, no network, no real data).
"""

from __future__ import annotations

import json
from itertools import pairwise

import pandas as pd
import pytest

from trading_platform.core.errors import (
    DataValidationError,
    InsufficientDataError,
    ValidationLayerError,
)
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.validation.split import (
    MIN_ROWS_PER_SLICE,
    Window,
    make_windows,
    split_is_oos,
)

FRAME_SIZE = 1000
N_WINDOWS = 5
BLOCK = FRAME_SIZE // N_WINDOWS  # 200
IS_LENGTH = int(BLOCK * 0.7)  # 140
OOS_LENGTH = BLOCK - IS_LENGTH  # 60


@pytest.fixture
def frame() -> pd.DataFrame:
    """1000 deterministic 1h candles (same frame the other tests slice)."""
    return make_ohlcv(FRAME_SIZE)


def _is_slice(frame: pd.DataFrame, window: Window) -> pd.DataFrame:
    return frame.loc[window.is_start : window.is_end]


def _oos_slice(frame: pd.DataFrame, window: Window) -> pd.DataFrame:
    return frame.loc[window.oos_start : window.oos_end]


# ---------------------------------------------------------------------------
# split_is_oos
# ---------------------------------------------------------------------------


def test_split_is_oos_100_rows_ratio_07_gives_70_30_without_overlap(
    ohlcv_frame: pd.DataFrame,
) -> None:
    data = ohlcv_frame.iloc[:100]

    in_sample, out_sample = split_is_oos(data, in_sample_ratio=0.7)

    assert len(in_sample) == 70
    assert len(out_sample) == 30
    assert list(in_sample.columns) == list(data.columns)
    assert list(out_sample.columns) == list(data.columns)
    assert set(in_sample.index).isdisjoint(out_sample.index)
    assert in_sample.index[-1] < out_sample.index[0]
    assert in_sample.index[0] == data.index[0]
    assert out_sample.index[-1] == data.index[-1]
    assert in_sample.index.name == "timestamp"


def test_split_is_oos_does_not_reindex_and_keeps_values(ohlcv_frame: pd.DataFrame) -> None:
    data = ohlcv_frame.iloc[:100]

    in_sample, out_sample = split_is_oos(data, in_sample_ratio=0.7)

    assert in_sample["close"].tolist() == data["close"].iloc[:70].tolist()
    assert out_sample["close"].tolist() == data["close"].iloc[70:].tolist()
    assert in_sample.index.tolist() == data.index[:70].tolist()
    assert out_sample.index.tolist() == data.index[70:].tolist()


def test_split_is_oos_purge_drops_exactly_five_rows_between_the_slices(
    ohlcv_frame: pd.DataFrame,
) -> None:
    data = ohlcv_frame.iloc[:100]

    in_sample, out_sample = split_is_oos(data, in_sample_ratio=0.7, purge_candles=5)

    assert len(in_sample) == 65
    assert len(out_sample) == 30
    assert in_sample.index[-1] == data.index[64]
    assert out_sample.index[0] == data.index[70]
    purged = data.index[65:70]
    assert len(purged) == 5
    assert set(purged).isdisjoint(in_sample.index)
    assert set(purged).isdisjoint(out_sample.index)


@pytest.mark.parametrize("ratio", [0.0, 1.0, 1.5, -0.1, float("nan"), None])
def test_split_is_oos_rejects_invalid_ratio(ohlcv_frame: pd.DataFrame, ratio: object) -> None:
    with pytest.raises(ValidationLayerError):
        split_is_oos(ohlcv_frame.iloc[:100], in_sample_ratio=ratio)  # type: ignore[arg-type]


@pytest.mark.parametrize("purge", [-1, -10, 1.5, True, None, "two"])
def test_split_is_oos_rejects_invalid_purge(ohlcv_frame: pd.DataFrame, purge: object) -> None:
    with pytest.raises(ValidationLayerError):
        split_is_oos(ohlcv_frame.iloc[:100], purge_candles=purge)  # type: ignore[arg-type]


def test_split_is_oos_three_rows_is_insufficient(ohlcv_frame: pd.DataFrame) -> None:
    data = ohlcv_frame.iloc[:3]

    with pytest.raises(InsufficientDataError) as excinfo:
        split_is_oos(data, in_sample_ratio=0.7)

    message = str(excinfo.value)
    assert "3" in message
    assert "2" in message and "1" in message  # in-sample / out-of-sample sizes
    assert f"{MIN_ROWS_PER_SLICE}" in message


def test_split_is_oos_purge_larger_than_in_sample_is_insufficient(
    ohlcv_frame: pd.DataFrame,
) -> None:
    with pytest.raises(InsufficientDataError):
        split_is_oos(ohlcv_frame.iloc[:20], in_sample_ratio=0.5, purge_candles=10)


@pytest.mark.parametrize(
    "bad_frame",
    [
        pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}),
        pd.DataFrame(
            {"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0]},
            index=pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC"),
        ),
    ],
)
def test_split_is_oos_requires_a_contract_frame(bad_frame: pd.DataFrame) -> None:
    with pytest.raises(DataValidationError):
        split_is_oos(bad_frame)


# ---------------------------------------------------------------------------
# make_windows: rolling
# ---------------------------------------------------------------------------


def test_make_windows_rolling_is_ascending_and_non_overlapping(frame: pd.DataFrame) -> None:
    windows = make_windows(frame, n_windows=N_WINDOWS, in_sample_ratio=0.7)

    assert [window.index for window in windows] == [0, 1, 2, 3, 4]
    for window in windows:
        in_sample = _is_slice(frame, window)
        out_sample = _oos_slice(frame, window)
        assert len(in_sample) == IS_LENGTH
        assert len(out_sample) == OOS_LENGTH
        assert window.is_start == in_sample.index[0]
        assert window.is_end == in_sample.index[-1]
        assert window.oos_start == out_sample.index[0]
        assert window.oos_end == out_sample.index[-1]
        assert window.is_start <= window.is_end < window.oos_start <= window.oos_end
    for previous, following in pairwise(windows):
        assert previous.oos_end < following.oos_start


def test_make_windows_rolling_blocks_are_contiguous(frame: pd.DataFrame) -> None:
    windows = make_windows(frame, n_windows=N_WINDOWS)

    assert windows[0].oos_end < windows[1].is_start
    assert frame.index.get_loc(windows[1].is_start) - frame.index.get_loc(windows[0].oos_end) == 1
    assert frame.index.get_loc(windows[0].is_start) == 0
    assert frame.index.get_loc(windows[-1].oos_end) == BLOCK * N_WINDOWS - 1


def test_make_windows_anchored_grows_the_in_sample_slice(frame: pd.DataFrame) -> None:
    windows = make_windows(frame, n_windows=N_WINDOWS, mode="anchored")

    lengths = [len(_is_slice(frame, window)) for window in windows]
    first_timestamp = frame.index[0]
    assert all(window.is_start == first_timestamp for window in windows)
    assert lengths == [
        IS_LENGTH,
        BLOCK + IS_LENGTH,
        2 * BLOCK + IS_LENGTH,
        3 * BLOCK + IS_LENGTH,
        4 * BLOCK + IS_LENGTH,
    ]
    assert all(previous.is_end < following.is_end for previous, following in pairwise(windows))
    assert all(len(_oos_slice(frame, window)) == OOS_LENGTH for window in windows)


def test_make_windows_single_window_matches_split_is_oos(frame: pd.DataFrame) -> None:
    (window,) = make_windows(frame, n_windows=1, in_sample_ratio=0.7)
    in_sample, out_sample = split_is_oos(frame, in_sample_ratio=0.7)

    assert len(_is_slice(frame, window)) == len(in_sample)
    assert len(_oos_slice(frame, window)) == len(out_sample)
    assert window.is_start == in_sample.index[0]
    assert window.is_end == in_sample.index[-1]
    assert window.oos_start == out_sample.index[0]
    assert window.oos_end == out_sample.index[-1]


def test_make_windows_purge_shortens_in_sample_and_leaves_a_gap(frame: pd.DataFrame) -> None:
    (window,) = make_windows(frame, n_windows=1, in_sample_ratio=0.7, purge_candles=5)

    assert len(_is_slice(frame, window)) == 700 - 5
    assert len(_oos_slice(frame, window)) == 300
    assert frame.index.get_loc(window.oos_start) - frame.index.get_loc(window.is_end) == 6


def test_make_windows_leaves_trailing_rows_unused() -> None:
    frame = make_ohlcv(FRAME_SIZE + 3)  # 1003 rows, 3 trailing rows

    windows = make_windows(frame, n_windows=N_WINDOWS)

    assert len(windows) == N_WINDOWS
    assert windows[-1].oos_end == frame.index[BLOCK * N_WINDOWS - 1]
    assert windows[-1].oos_end < frame.index[-1]
    assert frame.index.get_loc(windows[-1].oos_end) == 999


# ---------------------------------------------------------------------------
# make_windows: validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_windows", [0, -1, 2.5, True, None, "5"])
def test_make_windows_rejects_invalid_window_count(frame: pd.DataFrame, n_windows: object) -> None:
    with pytest.raises(ValidationLayerError):
        make_windows(frame, n_windows=n_windows)  # type: ignore[arg-type]


def test_make_windows_rejects_unknown_mode(frame: pd.DataFrame) -> None:
    with pytest.raises(ValidationLayerError, match="unknown walk-forward mode"):
        make_windows(frame, n_windows=2, mode="sliding")  # type: ignore[arg-type]


def test_make_windows_rejects_invalid_ratio_and_purge(frame: pd.DataFrame) -> None:
    with pytest.raises(ValidationLayerError):
        make_windows(frame, n_windows=2, in_sample_ratio=1.2)
    with pytest.raises(ValidationLayerError):
        make_windows(frame, n_windows=2, purge_candles=-3)


def test_make_windows_needs_two_rows_per_window() -> None:
    frame = make_ohlcv(8)

    with pytest.raises(InsufficientDataError) as excinfo:
        make_windows(frame, n_windows=5)

    message = str(excinfo.value)
    assert "8" in message and "10" in message


def test_make_windows_rejects_a_degenerate_block() -> None:
    frame = make_ohlcv(10)  # 5 blocks of 2 rows

    with pytest.raises(InsufficientDataError):
        make_windows(frame, n_windows=5, in_sample_ratio=0.3)


def test_make_windows_requires_a_contract_frame() -> None:
    with pytest.raises(DataValidationError):
        make_windows(pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}), n_windows=1)


# ---------------------------------------------------------------------------
# Window serialisation
# ---------------------------------------------------------------------------


def test_window_round_trip_is_json_serialisable(frame: pd.DataFrame) -> None:
    window = make_windows(frame, n_windows=N_WINDOWS)[2]

    payload = window.to_dict()

    assert set(payload) == {"index", "is_start", "is_end", "oos_start", "oos_end"}
    assert payload["index"] == 2
    assert payload["is_start"] == window.is_start.isoformat()
    assert json.loads(json.dumps(payload)) == payload
    assert Window.from_dict(payload) == window
    assert Window.from_dict(json.loads(json.dumps(payload))) == window
