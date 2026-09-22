"""Contract tests of the two committed ``momentum`` backtest configurations.

``config/backtest_momentum.json`` is the long/short configuration used for the
research validation and ``config/backtest_momentum_spot.json`` is its spot-only
twin: the two files differ by exactly the short-selling switch (plus the names
that keep their reports apart).  Both are offline documents — loading them never
touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trading_platform.config import AppConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "backtest_default.json"
REFERENCE_CONFIG_PATH = CONFIG_DIR / "backtest_btc_benchmark_e2e.json"

MOMENTUM_CONFIG_PATH = CONFIG_DIR / "backtest_momentum.json"
MOMENTUM_SPOT_CONFIG_PATH = CONFIG_DIR / "backtest_momentum_spot.json"

#: The sections every momentum configuration copies verbatim from the committed
#: E2E reference: only the strategy, data, backtest and reporting sections are
#: specialised, the rest of the deployment stays identical.
COPIED_SECTIONS = (
    "exchange",
    "log_level",
    "monitoring",
    "project_name",
    "realtime",
    "validation",
)

#: ``(path, allow_short, basename, output_dir, title)`` of both configurations.
MOMENTUM_CONFIGS = (
    pytest.param(
        MOMENTUM_CONFIG_PATH,
        True,
        "momentum",
        "reports/momentum",
        "Momentum 4h long/short (2017-2026)",
        id="long-short",
    ),
    pytest.param(
        MOMENTUM_SPOT_CONFIG_PATH,
        False,
        "momentum_spot",
        "reports/momentum_spot",
        "Momentum 4h long-only spot (2017-2026)",
        id="spot-long-only",
    ),
)


def _expected_params(allow_short: bool) -> dict[str, Any]:
    """Return the frozen research parameters, short switch included."""
    return {
        "allow_short": allow_short,
        "atr_stop_multiplier": 4.0,
        "enter_score": 0.6,
        "exit_score": 0.0,
        "fast_days": 7,
        "mid_days": 14,
        "slow_days": 28,
    }


def _raw(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the documents themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [MOMENTUM_CONFIG_PATH, MOMENTUM_SPOT_CONFIG_PATH])
def test_momentum_config_is_canonical_json(path: Path) -> None:
    """Both files are written the way ``dump_config`` writes them."""
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)

    assert text == json.dumps(payload, indent=2, sort_keys=True) + "\n"
    assert list(payload) == sorted(payload)
    assert list(payload["strategy"]) == sorted(payload["strategy"])
    assert list(payload["strategy"]["params"]) == sorted(payload["strategy"]["params"])


@pytest.mark.parametrize("path", [MOMENTUM_CONFIG_PATH, MOMENTUM_SPOT_CONFIG_PATH])
def test_momentum_config_loads_as_an_appconfig(path: Path) -> None:
    assert isinstance(load_config(path), AppConfig)


@pytest.mark.parametrize("path", [MOMENTUM_CONFIG_PATH, MOMENTUM_SPOT_CONFIG_PATH])
def test_momentum_config_leaves_the_reference_sections_untouched(path: Path) -> None:
    """Exchange, monitoring, realtime and validation stay byte-identical."""
    reference = _raw(REFERENCE_CONFIG_PATH)
    payload = _raw(path)

    assert set(payload) == set(reference)
    for section in COPIED_SECTIONS:
        assert payload[section] == reference[section], section


def test_the_two_momentum_configs_differ_only_by_the_short_switch() -> None:
    long_short = _raw(MOMENTUM_CONFIG_PATH)
    spot = _raw(MOMENTUM_SPOT_CONFIG_PATH)

    differ = {key for key in long_short if long_short[key] != spot[key]}
    assert differ == {"backtest", "reporting", "strategy"}
    assert long_short["strategy"]["params"]["allow_short"] is True
    assert spot["strategy"]["params"]["allow_short"] is False


# ---------------------------------------------------------------------------
# the strategy section
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "allow_short", "basename", "output_dir", "title"),
    MOMENTUM_CONFIGS,
)
def test_momentum_config_selects_the_momentum_strategy(
    path: Path, allow_short: bool, basename: str, output_dir: str, title: str
) -> None:
    strategy = load_config(path).strategy

    assert strategy.name == "momentum"
    assert strategy.timeframe == "4h"
    assert strategy.params == _expected_params(allow_short)


@pytest.mark.parametrize(
    ("path", "allow_short", "basename", "output_dir", "title"),
    MOMENTUM_CONFIGS,
)
def test_momentum_config_uses_the_same_timeframe_on_both_sides(
    path: Path, allow_short: bool, basename: str, output_dir: str, title: str
) -> None:
    """The strategy and the loaded candles must agree, or the grid is a lie."""
    cfg = load_config(path)

    assert cfg.strategy.timeframe == cfg.data.timeframe == "4h"
    assert _raw(path)["strategy"]["timeframe"] == _raw(path)["data"]["timeframe"]


@pytest.mark.parametrize(
    ("path", "allow_short", "basename", "output_dir", "title"),
    MOMENTUM_CONFIGS,
)
def test_momentum_config_agrees_with_the_engine_on_shorting(
    path: Path, allow_short: bool, basename: str, output_dir: str, title: str
) -> None:
    """The strategy only emits shorts when the engine is allowed to take them."""
    cfg = load_config(path)

    assert cfg.backtest.allow_short is allow_short
    assert cfg.strategy.params["allow_short"] is allow_short


@pytest.mark.parametrize(
    ("path", "allow_short", "basename", "output_dir", "title"),
    MOMENTUM_CONFIGS,
)
def test_momentum_config_pins_the_research_panel_window(
    path: Path, allow_short: bool, basename: str, output_dir: str, title: str
) -> None:
    data = load_config(path).data

    assert data.start is not None and data.start.isoformat() == "2017-08-17T00:00:00+00:00"
    assert data.end is not None and data.end.isoformat() == "2026-08-31T00:00:00+00:00"
    # A nine-year real series contains exchange outages: the 3.0 default gap
    # factor means "at most three missing candles in a row", which is 12 hours on
    # a 4h grid and would reject the panel.
    assert data.max_gap_factor == 30.0
    assert data.max_missing_ratio == 0.01
    assert data.allow_network is True


# ---------------------------------------------------------------------------
# the execution, benchmark and reporting sections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "allow_short", "basename", "output_dir", "title"),
    MOMENTUM_CONFIGS,
)
def test_momentum_config_pins_the_execution_costs(
    path: Path, allow_short: bool, basename: str, output_dir: str, title: str
) -> None:
    """10 bp per side plus 5 bp of slippage, the assumptions of the research."""
    backtest = load_config(path).backtest

    assert backtest.fee_rate == 0.001
    assert backtest.slippage == 0.0005
    assert backtest.initial_balance == 10000.0
    assert backtest.max_open_trades == 1
    assert backtest.stake_amount is None
    assert backtest.compute_metrics is True


@pytest.mark.parametrize(
    ("path", "allow_short", "basename", "output_dir", "title"),
    MOMENTUM_CONFIGS,
)
def test_momentum_config_pins_the_benchmark(
    path: Path, allow_short: bool, basename: str, output_dir: str, title: str
) -> None:
    benchmark = load_config(path).benchmark

    assert benchmark.enabled is True
    assert benchmark.variant == "buy_and_hold"
    assert benchmark.risk_free_rate == 0.05
    assert benchmark.n_random_simulations == 1000
    assert benchmark.random_entry_seed == 42


@pytest.mark.parametrize(
    ("path", "allow_short", "basename", "output_dir", "title"),
    MOMENTUM_CONFIGS,
)
def test_momentum_config_pins_the_reporting(
    path: Path, allow_short: bool, basename: str, output_dir: str, title: str
) -> None:
    cfg = load_config(path)

    assert cfg.reporting.basename == basename
    assert cfg.reporting.output_dir == Path(output_dir)
    assert cfg.reporting.title == title
    assert cfg.reporting.formats == ["markdown", "json"]
    assert cfg.reporting.include_trades is True
    assert cfg.reporting.trade_limit == 50


@pytest.mark.parametrize(
    ("path", "allow_short", "basename", "output_dir", "title"),
    MOMENTUM_CONFIGS,
)
def test_momentum_config_pins_the_exchange_fee(
    path: Path, allow_short: bool, basename: str, output_dir: str, title: str
) -> None:
    """The venue section charges the same 10 bp per side as the engine does."""
    cfg = load_config(path)

    assert cfg.exchange.fee_rate == 0.001
    assert cfg.exchange.slippage == 0.0005
    assert cfg.exchange.fee_rate == cfg.backtest.fee_rate
    assert cfg.exchange.slippage == cfg.backtest.slippage


# ---------------------------------------------------------------------------
# the pinned default configuration
# ---------------------------------------------------------------------------


def test_the_default_config_still_declares_the_basic_strategy() -> None:
    """``backtest_default.json`` is pinned: no momentum run may repoint it."""
    payload = _raw(DEFAULT_CONFIG_PATH)
    assert payload["strategy"] == {"name": "basic", "params": {}, "timeframe": "1h"}
    assert load_config(DEFAULT_CONFIG_PATH).strategy.name == "basic"
