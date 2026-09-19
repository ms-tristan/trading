"""Backtest non-regression proof for the catch-up entry work (work package wp4).

This module is **pure evidence**: it adds no production behaviour. It pins
requirement 8 of ``.scratch/catch-up-entry-brief.md``:

    "Backtest non-regression. Prove the backtest numbers on the same fixture are
    IDENTICAL before and after -- run both, compare the numbers."

The decision being implemented is *engine-level only* -- a new optional
per-profile field ``entry_lookback_candles`` that the **live** runner reads, with
``BasicStrategy`` and ``strategy/engine.py`` staying byte-identical. This file is
the independent check that the feature changed no number on the backtest side.

Baseline provenance
-------------------
Every literal below was measured on the branch point ``9b517fa`` over
``tests/fixtures/BTC_USDT-1h.csv`` (200 synthetic, rising 1h candles,
tz-aware UTC) with::

    run_backtest(get_strategy("basic"), frame, symbol="BTC/USDT", timeframe="1h",
                 fee_rate=0.001, slippage=0.0005)

and, for the default run, the same call with the engine defaults
(``fee_rate=0.001``, ``slippage=0.0``).  The numbers were re-measured on the
frozen tree immediately before this file was written and matched digit for
digit; the trade signatures are asserted verbatim (no approximation) and the
three floats with ``pytest.approx(..., rel=1e-12)`` -- tight enough that any real
drift fails, loose enough to survive the last-bit noise of a different BLAS.

A red test here is **not** a tolerance to widen: it means the feature regressed
the backtest, which is exactly what requirement 8 exists to catch.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pandas as pd
import pytest

from trading_platform.config import AppConfig, BacktestConfig, ProfileConfig, StrategyConfig
from trading_platform.strategy.engine import run_backtest, run_backtest_on_config
from trading_platform.strategy.registry import get_strategy

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "BTC_USDT-1h.csv"

#: The frozen fixture is exactly 200 hourly candles.
FIXTURE_ROWS = 200

#: The five OHLCV columns of the fixture, in the engine's documented order.
FIXTURE_COLUMNS = ("open", "high", "low", "close", "volume")

#: The two source files the catch-up entry must never touch.
FROZEN_SOURCE_FILES = (
    REPO_ROOT / "src" / "trading_platform" / "strategy" / "engine.py",
    REPO_ROOT / "src" / "trading_platform" / "strategy" / "basic.py",
)

#: Tokens of the catch-up entry. Their presence in either frozen file would mean
#: the feature was implemented as a strategy/engine change -- the exact failure
#: mode the brief forbids.
FORBIDDEN_TOKENS = ("lookback", "entry_lookback_candles")

#: Tokens shared with ``realtime/runner.py``: the field is live-only, so it must
#: never reach the backtest or the strategy configuration models.
LIVE_ONLY_FIELD = "entry_lookback_candles"
BACKTEST_SIDE_MODELS = (BacktestConfig, StrategyConfig, AppConfig)

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"


class Baseline(NamedTuple):
    """The frozen digest of one reference backtest run."""

    n_trades: int
    final_equity: float
    equity_sum: float
    pnl_sum: float
    trade_signature: tuple[str, ...]


#: Fee-bearing run: ``fee_rate=0.001``, ``slippage=0.0005``.
FEE_BASELINE = Baseline(
    n_trades=4,
    final_equity=10701.653268164513,
    equity_sum=2061284.4615188213,
    pnl_sum=701.653268164513,
    trade_signature=(
        "2023-01-02T22:00:00+00:00->2023-01-03T21:00:00+00:00:-37.8856801221",
        "2023-01-04T18:00:00+00:00->2023-01-05T18:00:00+00:00:66.4796901753",
        "2023-01-06T15:00:00+00:00->2023-01-07T14:00:00+00:00:163.4027126731",
        "2023-01-08T12:00:00+00:00->2023-01-09T07:00:00+00:00:509.6565454381",
    ),
)

#: Default run: ``fee_rate=0.001`` (engine default), ``slippage=0.0``.
DEFAULT_BASELINE = Baseline(
    n_trades=4,
    final_equity=10739.173339503885,
    equity_sum=2064463.9584561922,
    pnl_sum=739.173339503883,
    trade_signature=(
        "2023-01-02T22:00:00+00:00->2023-01-03T21:00:00+00:00:-27.9185822532",
        "2023-01-04T18:00:00+00:00->2023-01-05T18:00:00+00:00:76.5898525676",
        "2023-01-06T15:00:00+00:00->2023-01-07T14:00:00+00:00:173.9473550196",
        "2023-01-08T12:00:00+00:00->2023-01-09T07:00:00+00:00:516.5547141700",
    ),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def load_fixture() -> pd.DataFrame:
    """Return ``tests/fixtures/BTC_USDT-1h.csv`` as a tz-aware UTC OHLCV frame.

    Read with pandas only: no network, no newly generated fixture.
    """
    frame = pd.read_csv(FIXTURE, parse_dates=["timestamp"], index_col="timestamp")
    frame.index.name = "timestamp"
    return frame


def digest(result: object) -> Baseline:
    """Return the comparable digest of a :class:`BacktestResult`.

    The trade signature is ``entry_time->exit_time:pnl`` formatted with the same
    ten decimals as the frozen literals, so a moved fill, a moved exit reason or
    a moved fee shows up as a different string.
    """
    trades = result.trades  # type: ignore[attr-defined]
    return Baseline(
        n_trades=len(trades),
        final_equity=float(result.final_balance),  # type: ignore[attr-defined]
        equity_sum=float(result.equity_curve.sum()),  # type: ignore[attr-defined]
        pnl_sum=float(sum(trade.pnl for trade in trades)),
        trade_signature=tuple(
            f"{trade.entry_time.isoformat()}->{trade.exit_time.isoformat()}:{trade.pnl:.10f}"
            for trade in trades
        ),
    )


def assert_matches_baseline(actual: Baseline, expected: Baseline, *, label: str) -> None:
    """Assert ``actual`` equals the frozen ``expected`` digest, digit for digit.

    This is the **single** shared assertion used by the frozen-literal test and by
    the determinism test, so a reader sees "before == after" rather than two
    unrelated numbers. ``n_trades`` and the trade signature are exact;
    the three floats use a ``rel=1e-12`` tolerance (never a bare ``==`` on a
    float, never a tolerance loose enough to hide a real drift).
    """
    assert actual.n_trades == expected.n_trades, f"{label}: trade count moved"
    assert actual.trade_signature == expected.trade_signature, (
        f"{label}: trade signature moved\nactual  : {actual.trade_signature}\nexpected: "
        f"{expected.trade_signature}"
    )
    assert actual.final_equity == pytest.approx(expected.final_equity, rel=1e-12), (
        f"{label}: final_equity moved"
    )
    assert actual.equity_sum == pytest.approx(expected.equity_sum, rel=1e-12), (
        f"{label}: equity_sum moved"
    )
    assert actual.pnl_sum == pytest.approx(expected.pnl_sum, rel=1e-12), f"{label}: pnl_sum moved"


def run_fee_backtest(frame: pd.DataFrame) -> object:
    """Run the fee-bearing reference backtest on ``frame``."""
    return run_backtest(
        get_strategy("basic"),
        frame,
        symbol=SYMBOL,
        timeframe=TIMEFRAME,
        fee_rate=0.001,
        slippage=0.0005,
    )


def run_default_backtest(frame: pd.DataFrame) -> object:
    """Run the default reference backtest on ``frame`` (engine defaults)."""
    return run_backtest(get_strategy("basic"), frame, symbol=SYMBOL, timeframe=TIMEFRAME)


# ---------------------------------------------------------------------------
# 1. the frozen baseline
# ---------------------------------------------------------------------------


def test_the_basic_strategy_backtest_is_identical_to_the_frozen_baseline() -> None:
    """The two reference runs still produce the numbers measured at ``9b517fa``.

    FAILS the moment any engine, indicator or strategy change moves a number.
    """
    frame = load_fixture()

    assert_matches_baseline(digest(run_fee_backtest(frame)), FEE_BASELINE, label="fee run")
    assert_matches_baseline(
        digest(run_default_backtest(frame)), DEFAULT_BASELINE, label="default run"
    )


# ---------------------------------------------------------------------------
# 2. determinism
# ---------------------------------------------------------------------------


def test_the_same_backtest_run_twice_is_bit_for_bit_identical() -> None:
    """Two runs over the same frame share the equity curve bit for bit.

    The engine documents that it uses no randomness and no dict ordering: this
    test makes that a contract, and also proves the frozen digest is not the
    result of a hidden state carried between runs.
    """
    frame = load_fixture()

    first = run_fee_backtest(frame)
    second = run_fee_backtest(frame)

    pd.testing.assert_series_equal(first.equity_curve, second.equity_curve, check_exact=True)
    assert [(t.entry_time, t.exit_time, t.pnl) for t in first.trades] == [
        (t.entry_time, t.exit_time, t.pnl) for t in second.trades
    ]
    assert_matches_baseline(digest(second), FEE_BASELINE, label="second fee run")

    first_default = run_default_backtest(frame)
    second_default = run_default_backtest(frame)
    pd.testing.assert_series_equal(
        first_default.equity_curve, second_default.equity_curve, check_exact=True
    )
    assert_matches_baseline(digest(second_default), DEFAULT_BASELINE, label="second default run")


# ---------------------------------------------------------------------------
# 3. the architectural invariant
# ---------------------------------------------------------------------------


def test_the_backtest_layer_never_learns_about_the_catch_up_field() -> None:
    """The catch-up entry stays live-only, even if a future edit slips past the numbers.

    FAILS if anyone implements the catch-up by touching the strategy or the
    engine -- the exact failure mode the brief forbids.
    """
    for path in FROZEN_SOURCE_FILES:
        assert path.is_file(), f"frozen source file is missing: {path}"
        source = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            assert token not in source, (
                f"{path.relative_to(REPO_ROOT)} must stay free of the {token!r} token: the "
                "catch-up entry is a live-engine concern, never a strategy or engine concern"
            )

    assert LIVE_ONLY_FIELD in ProfileConfig.model_fields, (
        f"{LIVE_ONLY_FIELD} must exist on ProfileConfig: it is the live-only switch"
    )
    for model in BACKTEST_SIDE_MODELS:
        assert LIVE_ONLY_FIELD not in model.model_fields, (
            f"{LIVE_ONLY_FIELD} must not exist on {model.__name__}: the backtest path never "
            "reads a profile and must stay untouched"
        )


# ---------------------------------------------------------------------------
# 4. the same digest through the documented configuration surface
# ---------------------------------------------------------------------------


def test_the_backtest_digest_is_stable_across_the_documented_configuration_surface() -> None:
    """The digest recomputed from a configuration document equals the frozen baseline.

    The document's profiles section carries the live-only
    ``entry_lookback_candles`` field: the backtest engine never reads a profile,
    so the digest must be strictly unchanged. Nothing is written to disk -- the
    document is validated in process.
    """
    frame = load_fixture()
    profile_payload = {
        "id": "catch-up-evidence",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "strategy": "basic",
        LIVE_ONLY_FIELD: 3,
    }
    document = {
        "strategy": {"name": "basic"},
        "exchange": {"fee_rate": 0.001, "slippage": 0.0005},
        "profiles": [profile_payload],
    }
    # The live-only profile field is validated by the profile model...
    validated_profile = ProfileConfig.model_validate(profile_payload)
    assert validated_profile.id == "catch-up-evidence"
    assert getattr(validated_profile, LIVE_ONLY_FIELD) == 3

    # ...and the backtest configuration built from the very same document is
    # strict: the profile section is not part of it and changes no number.
    cfg = AppConfig.model_validate(
        {
            "strategy": document["strategy"],
            "exchange": document["exchange"],
        }
    )
    assert cfg.strategy.name == "basic"

    result = run_backtest_on_config(cfg, frame, symbol=SYMBOL)
    # The document's exchange settings are the fee-bearing reference run.
    assert_matches_baseline(digest(result), FEE_BASELINE, label="configured fee run")

    default_cfg = AppConfig.model_validate({"strategy": {"name": "basic"}})
    default_result = run_backtest_on_config(default_cfg, frame, symbol=SYMBOL)
    assert_matches_baseline(
        digest(default_result), DEFAULT_BASELINE, label="configured default run"
    )


# ---------------------------------------------------------------------------
# 5. the fixture guard
# ---------------------------------------------------------------------------


def test_the_fixture_is_the_one_the_baseline_was_measured_on() -> None:
    """The baseline is only meaningful on this exact fixture.

    A guard against a silently swapped fixture invalidating every number above.
    """
    assert FIXTURE.is_file(), f"missing fixture: {FIXTURE}"

    frame = load_fixture()
    assert len(frame) == FIXTURE_ROWS
    assert tuple(frame.columns) == FIXTURE_COLUMNS
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing
    assert not frame.isna().to_numpy().any()
    assert str(frame.index[0]) == "2023-01-01 00:00:00+00:00"
