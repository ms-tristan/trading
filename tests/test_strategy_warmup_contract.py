"""The strategy half of the warm-up contract: declared, exact, and never silent.

The incident this file guards against was a delivered strategy whose warm-up had
never been declared: the platform accepted a ``momentum`` profile on a 1m grid,
ran it, and it produced zero signals for ever without a single error or warning.
Three properties have to hold, and each is tested from both sides where a
boundary exists:

* the strategy **declares** how many candles a frame must hold before it can
  emit any signal (:meth:`Strategy.required_candles`) -- the declared number is
  exactly the first row on which the score is defined, not a loose upper bound;
* the boundary really is a boundary: a frame one candle below the requirement
  emits **nothing**, a frame at the requirement emits a signal;
* :meth:`MomentumStrategy.prepare` **warns** on a frame it cannot warm up and is
  completely silent on a frame it can, so the silent no-op became a visible one.

Everything here is offline and deterministic: the frames are hand-built from an
explicit arithmetic series (or the shared synthetic generator with a fixed
seed), no test reads the network, the wall clock or a checked-in binary.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig
from trading_platform.core.constants import SUPPORTED_TIMEFRAMES, timeframe_minutes
from trading_platform.core.errors import ConfigError, StrategyError
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.realtime.warmup import candles_per_day, required_candles_for
from trading_platform.strategy import momentum as momentum_module
from trading_platform.strategy.base import BOOL_SIGNAL_COLUMNS, Strategy, StrategyParams
from trading_platform.strategy.basic import BasicStrategy
from trading_platform.strategy.momentum import MomentumStrategy
from trading_platform.strategy.registry import get_strategy, strategy_names

#: The module logger the warm-up warning is emitted on (frozen contract).
MOMENTUM_LOGGER = "trading_platform.strategy.momentum"

#: The frozen event name of the "this frame cannot warm up" warning.
WARMUP_EVENT = "strategy.warmup_incomplete"

#: Candles a momentum frame must hold, per supported timeframe, with the default
#: parameters (7 / 14 / 28 days) -- the table of the delivery brief.
REQUIRED_BY_TIMEFRAME: dict[str, int] = {
    "1m": 40321,
    "5m": 8065,
    "15m": 2689,
    "30m": 1345,
    "1h": 673,
    "4h": 169,
    "1d": 29,
}

#: Candles per 24-hour day of each supported timeframe (the grid, independently).
CANDLES_PER_DAY: dict[str, float] = {
    "1m": 1440.0,
    "5m": 288.0,
    "15m": 96.0,
    "30m": 48.0,
    "1h": 24.0,
    "4h": 6.0,
    "1d": 1.0,
}

#: Pandas frequency of each supported timeframe, for the hand-built frames.
PANDAS_FREQ: dict[str, str] = {
    "1m": "min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "h",
    "4h": "4h",
    "1d": "D",
}

#: Lookbacks that stay cheap on an intraday grid while keeping the same shape.
SHORT_PARAMS: dict[str, Any] = {"fast_days": 1, "mid_days": 2, "slow_days": 3}


def rising_frame(rows: int, *, freq: str = "h") -> pd.DataFrame:
    """Return a deterministic, strictly rising OHLCV frame of ``rows`` candles.

    Every close is higher than the previous one by the same amount, so all three
    momentum horizons are strictly positive on every row where they are defined
    and the score is exactly ``1.0``: the frame therefore isolates the *warm-up*
    from the signal rule, which is what the boundary tests measure.  ``open`` is
    the previous close (the first candle opens at the first close), ``high`` is
    one above the body and ``low`` one below, so the ATR column is defined too.
    """
    if rows < 0:
        raise ValueError(f"rows must be >= 0, got {rows}")
    index = pd.date_range(
        start="2024-01-01T00:00:00Z", periods=rows, freq=freq, tz="UTC", name="timestamp"
    )
    close = 100.0 + 0.5 * np.arange(rows, dtype="float64")
    opens = np.concatenate(([close[0]], close[:-1])) if rows else close
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, close) + 1.0,
            "low": np.minimum(opens, close) - 1.0,
            "close": close,
            "volume": np.full(rows, 10.0, dtype="float64"),
        },
        index=index,
    )


class TrivialStrategy(Strategy):
    """A minimal concrete strategy: the abstract surface and nothing else."""

    name: ClassVar[str] = "trivial"
    ParamsModel: ClassVar[type[StrategyParams]] = StrategyParams

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return a copy: this strategy carries no indicator at all."""
        return data.copy()

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return an all-false signal frame plus a NaN stop column."""
        frame = pd.DataFrame(
            {column: np.zeros(len(data), dtype=bool) for column in BOOL_SIGNAL_COLUMNS},
            index=data.index,
        )
        frame["stop_loss"] = np.full(len(data), np.nan, dtype="float64")
        return frame


# ---------------------------------------------------------------------------
# 1. the declaration itself
# ---------------------------------------------------------------------------


def test_the_base_contract_declares_no_warmup() -> None:
    """``Strategy.required_candles`` defaults to ``0`` -- an opt-in declaration.

    The safe default is what lets a strategy with no warm-up inherit the member
    untouched: ``0`` means "no requirement", never "unknown requirement".
    """
    strategy = TrivialStrategy()
    assert strategy.required_candles() == 0
    # The default grid is irrelevant to a strategy that declares nothing.
    assert strategy.required_candles(1440.0) == 0


def test_basic_declares_no_warmup_so_no_working_run_starts_failing() -> None:
    """``basic`` sizes its lookbacks in candles: it keeps the ``0`` default.

    Giving it a floor would be a behaviour change for every working profile, and
    the incident must not be "fixed" by making previously healthy runs fail: the
    strategy whose lookbacks are already candles declares nothing.
    """
    strategy = BasicStrategy()
    assert strategy.required_candles() == 0
    assert strategy.required_candles(1440.0) == 0
    assert strategy.required_candles(1.0) == 0


@pytest.mark.parametrize("timeframe", sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes))
def test_momentum_declares_the_documented_requirement_on_every_grid(timeframe: str) -> None:
    """The declared warm-up of ``momentum`` is the table of the delivery brief."""
    required = MomentumStrategy().required_candles(candles_per_day(timeframe))
    assert isinstance(required, int)
    assert required == REQUIRED_BY_TIMEFRAME[timeframe]


def test_every_registered_strategy_declares_a_non_negative_int() -> None:
    """The member is total for every strategy the platform can build."""
    names = strategy_names()
    assert names  # the registry is never empty
    for name in names:
        strategy = get_strategy(name)
        for per_day in (1.0, 6.0, 24.0, 96.0, 1440.0):
            required = strategy.required_candles(per_day)
            assert isinstance(required, int)
            assert not isinstance(required, bool)
            assert required >= 0


@pytest.mark.parametrize(
    "per_day",
    [0.0, -1.0, -1440.0, float("nan"), float("inf"), float("-inf"), None, "not-a-number"],
)
def test_required_candles_never_raises_for_a_degenerate_grid(per_day: Any) -> None:
    """A non-finite, zero or negative grid degrades to the daily grid (``1.0``)."""
    assert MomentumStrategy().required_candles(per_day) == REQUIRED_BY_TIMEFRAME["1d"]


def test_required_candles_is_pure_and_repeatable() -> None:
    """Two calls answer the same number and never touch the parameters."""
    strategy = MomentumStrategy({"fast_days": 3, "mid_days": 5, "slow_days": 9})
    before = strategy.params.model_dump()
    assert strategy.required_candles(24.0) == strategy.required_candles(24.0) == 9 * 24 + 1
    assert strategy.params.model_dump() == before


def test_required_candles_follows_the_parameters_not_a_constant() -> None:
    """The declaration is derived from ``params`` and the grid, never hard-coded."""
    strategy = MomentumStrategy(SHORT_PARAMS)
    assert strategy.required_candles(1440.0) == 3 * 1440 + 1
    assert strategy.required_candles(24.0) == 3 * 24 + 1
    assert strategy.required_candles(6.0) == 3 * 6 + 1
    assert strategy.required_candles(1.0) == 3 * 1 + 1


def test_the_declaration_is_exactly_the_first_defined_score_row_plus_one() -> None:
    """The declared number is tight: the score is ``NaN`` before it, defined at it.

    A declaration that over-estimated would refuse profiles that do work; one
    that under-estimated would let the silent no-op back in.  The invariant is
    checked against the very frame the strategy prepares: the trailing five rows
    are all defined, and every earlier row is not.
    """
    strategy = MomentumStrategy()
    required = strategy.required_candles(candles_per_day("1h"))
    prepared, signals = strategy.run(rising_frame(required + 5, freq="h"))
    score = prepared["momentum_score"]
    assert int(score.isna().sum()) == required - 1
    assert pd.isna(score.iloc[required - 2])
    assert float(score.iloc[required - 1]) == pytest.approx(1.0)
    assert bool(signals["entry_long"].iloc[required - 1]) is True


# ---------------------------------------------------------------------------
# 2. both sides of the boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("freq", "params", "per_day", "expected"),
    [
        pytest.param("h", {}, 24.0, 673, id="1h-default-params"),
        pytest.param("4h", {}, 6.0, 169, id="4h-default-params"),
        pytest.param("D", {}, 1.0, 29, id="1d-default-params"),
        pytest.param("15min", {}, 96.0, 2689, id="15m-default-params"),
        pytest.param("min", SHORT_PARAMS, 1440.0, 4321, id="1m-short-lookbacks"),
    ],
)
def test_a_frame_below_the_requirement_emits_nothing_and_one_at_it_does(
    freq: str, params: dict[str, Any], per_day: float, expected: int
) -> None:
    """The boundary pair: ``required - 1`` rows is silent, ``required`` rows fires.

    Both sides are asserted on the **same** strictly rising series, so the only
    difference between them is the warm-up: nothing in the signal rule changed.
    """
    strategy = MomentumStrategy(params)
    required = strategy.required_candles(per_day)
    assert required == expected

    prepared_below, signals_below = strategy.run(rising_frame(required - 1, freq=freq))
    assert len(prepared_below) == required - 1
    assert prepared_below["momentum_score"].isna().all()
    assert not signals_below[list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()
    assert int(signals_below["entry_long"].sum()) == 0

    _prepared_at, signals_at = strategy.run(rising_frame(required, freq=freq))
    assert bool(signals_at["entry_long"].iloc[-1]) is True
    assert int(signals_at["entry_long"].sum()) == 1


def test_the_one_candle_that_warms_the_frame_up_is_the_whole_difference() -> None:
    """Growing a frame by exactly one candle is what turns the profile on.

    This is the incident in one test: before the extra candle the platform has
    nothing to trade on, after it the very same parameters emit a signal.
    """
    strategy = MomentumStrategy()
    frame = rising_frame(strategy.required_candles(candles_per_day("1h")), freq="h")
    _prepared_before, before = strategy.run(frame.iloc[:-1])
    _prepared_after, after = strategy.run(frame)
    assert not before["entry_long"].any()
    assert bool(after["entry_long"].iloc[-1]) is True


# ---------------------------------------------------------------------------
# 3. the warning: visible, structured, and only when it is true
# ---------------------------------------------------------------------------


def test_prepare_warns_when_the_frame_cannot_warm_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A short frame logs exactly one structured warning naming the arithmetic."""
    strategy = MomentumStrategy()
    with caplog.at_level(logging.WARNING, logger=MOMENTUM_LOGGER):
        prepared, _signals = strategy.run(rising_frame(100, freq="h"))

    records = [record for record in caplog.records if record.name == MOMENTUM_LOGGER]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert getattr(record, "event", None) == WARMUP_EVENT
    assert getattr(record, "strategy", None) == "momentum"
    assert getattr(record, "rows", None) == 100
    assert getattr(record, "required_candles", None) == 673
    assert getattr(record, "candles_per_day", None) == pytest.approx(24.0)
    # The human-readable rendering carries the same numbers.
    message = record.getMessage()
    assert "100 row(s) received" in message
    assert "673 required" in message
    assert "24 candles/day" in message
    # A warning, never an exception: the frame still comes back prepared.
    assert len(prepared) == 100
    assert "momentum_score" in prepared.columns


def test_prepare_is_silent_when_the_frame_is_long_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At the requirement the warning does not fire -- and neither does any other."""
    strategy = MomentumStrategy()
    required = strategy.required_candles(candles_per_day("1h"))
    with caplog.at_level(logging.DEBUG, logger=MOMENTUM_LOGGER):
        strategy.run(rising_frame(required, freq="h"))
    assert [record for record in caplog.records if record.name == MOMENTUM_LOGGER] == []


def test_prepare_warns_once_per_short_frame_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Short frames are legitimate (walk-forward windows, unit tests): no raise."""
    strategy = MomentumStrategy()
    with caplog.at_level(logging.WARNING, logger=MOMENTUM_LOGGER):
        for rows in (1, 2, 10):
            assert len(strategy.prepare(rising_frame(rows, freq="h"))) == rows
    events = [
        str(getattr(record, "event", ""))
        for record in caplog.records
        if record.name == MOMENTUM_LOGGER
    ]
    assert events == [WARMUP_EVENT] * 3


def test_a_four_hour_frame_of_two_hundred_candles_never_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 4h grid really works at the default warm-up: 169 candles are needed.

    ``momentum`` needs 169 candles on a 4h grid, so the profile the operator
    could have created on 4h warms up at the default ``warmup_candles`` (200) and
    logs nothing at all.
    """
    with caplog.at_level(logging.DEBUG, logger=MOMENTUM_LOGGER):
        MomentumStrategy().run(rising_frame(200, freq="4h"))
    assert [record for record in caplog.records if record.name == MOMENTUM_LOGGER] == []


# ---------------------------------------------------------------------------
# 4. the cross-layer invariant: one grid, two layers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes))
def test_the_strategy_grid_inference_agrees_with_the_warmup_authority(timeframe: str) -> None:
    """``warmup.candles_per_day(tf)`` equals the grid ``momentum`` infers from the frame.

    This is the invariant that makes the two layers comparable at all: the
    realtime layer's authority and the strategy's own frame inference must answer
    the same number on **every** supported timeframe, or a refusal would be
    computed on a grid the strategy does not use.  The frame comes from the
    repository's own synthetic generator, with an explicit seed.
    """
    frame = make_ohlcv(64, timeframe=timeframe, seed=5)
    inferred = momentum_module._candles_per_day(pd.DatetimeIndex(frame.index))
    authority = candles_per_day(timeframe)
    assert authority == inferred
    assert authority == pytest.approx(CANDLES_PER_DAY[timeframe])


@pytest.mark.parametrize("timeframe", sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes))
def test_the_realtime_requirement_of_a_momentum_profile_matches_the_strategy(
    timeframe: str,
) -> None:
    """``warmup.required_candles_for`` answers what the strategy itself declares.

    The realtime layer builds the strategy through the registry and adds the
    grid; the strategy answers the same number for the same profile, which is
    what lets the create refusal and the runner's start-time check agree.
    """
    profile = ProfileConfig(
        id=f"momentum-{timeframe}",
        symbol="BTC/USDT",
        timeframe=timeframe,
        strategy="momentum",
    )
    expected = MomentumStrategy().required_candles(candles_per_day(timeframe))
    assert required_candles_for(profile) == expected == REQUIRED_BY_TIMEFRAME[timeframe]


def test_candles_per_day_rejects_an_unsupported_timeframe() -> None:
    """The grid authority stays strict about its input, like every other seam."""
    with pytest.raises(ConfigError):
        candles_per_day("7m")


def test_the_strategy_layer_never_imports_the_realtime_layer() -> None:
    """The direction is frozen: ``momentum`` declares its warm-up by itself.

    ``required_candles`` is derived from the strategy's own parameters and the
    grid it is handed, so the strategy layer needs no realtime helper -- and the
    module must not grow a convenience import of one.  The check reads the
    module's actual import statements, not its prose.
    """
    source = momentum_module.__file__
    assert source is not None
    tree = ast.parse(Path(source).read_text(encoding="utf-8"), filename=source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    assert imported  # the module really imports something
    assert not [name for name in imported if name.startswith("trading_platform.realtime")]
    with pytest.raises(StrategyError):
        get_strategy("definitely-not-a-strategy")
