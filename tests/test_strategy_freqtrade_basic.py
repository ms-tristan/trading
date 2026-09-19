"""Tests of the concrete Freqtrade exposure: ``freqtrade_basic``, the shim and the re-exports.

The module under test is the *concrete* half of the Freqtrade adapter: it is the
only place where a Freqtrade-facing class is built **at import time**, and it is
paired with ``user_data/strategies/BasicStrategy.py``, the file Freqtrade
resolves by class name.

Two groups, and the split mirrors ``docs/testing-policy.md`` §1.2:

* the **offline guards** — the re-export contract of
  ``trading_platform.strategy``, the "``freqtrade_basic`` is never imported from
  there" proof, the literal ``class`` statement of the shim and the ``.gitignore``
  rules that let the shim be tracked — need **no** Freqtrade at all, and they
  **must run on the CI**, which installs the ``.[dev]`` extra only;
* everything that builds or resolves a real ``IStrategy`` calls
  ``pytest.importorskip("freqtrade", ...)`` **inside the test body**.

Why the guard is inside the test and not at module level: a module-level
``importorskip`` skips the *whole* module, which would also skip the offline
guards above — that is, precisely the assertions that make this file useful on a
checkout without the optional extra.  Guarding per test gives the same green CI
and keeps those assertions running there.

The last test is the integration one: it resolves ``BasicStrategy`` through the
**real** ``StrategyResolver`` (the shipped ``user_data/strategies`` shim, no
network) and cross-checks its signals candle by candle against the house strategy
on the real BTC/USDT 1h cache.  The equality of ``enter_long``/``exit_long`` with
``entry_long``/``exit_long`` is the proof that the ``entry_*`` -> ``enter_*``
translation survives the trip through Freqtrade's own loader.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.constants import DEFAULT_TIMEFRAME
from trading_platform.freqtrade import DEFAULT_STRATEGY_NAME
from trading_platform.strategy import registry

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The concrete exposure module — read as text by the docstring/AST tests.
MODULE_PATH = REPO_ROOT / "src" / "trading_platform" / "strategy" / "freqtrade_basic.py"

#: The public namespace of the strategy layer (the re-export contract).
INIT_PATH = REPO_ROOT / "src" / "trading_platform" / "strategy" / "__init__.py"

#: The Freqtrade entry point: the file ``StrategyResolver`` scans.
SHIM_PATH = REPO_ROOT / "user_data" / "strategies" / "BasicStrategy.py"

#: The rules this work package appends to ``.gitignore``.
GITIGNORE_PATH = REPO_ROOT / ".gitignore"

#: The configurations that declare the strategy name Freqtrade looks up.
FREQTRADE_CONFIGS = (
    REPO_ROOT / "config" / "freqtrade_config.json",
    REPO_ROOT / "config" / "freqtrade_dryrun.json",
)

#: Real Binance cache used by the integration test (git-ignored: the test skips
#: when it is absent, so a fresh clone stays green).
CACHE_PATH = REPO_ROOT / "data" / "cache" / "binance" / "BTC_USDT" / "1h.parquet"

#: The three arguments ``populate_*`` receives from Freqtrade.
METADATA: dict[str, object] = {"pair": "BTC/USDT", "timeframe": "1h", "dataframe": None}

#: The comment the shim must keep: a factory alias resolves to ``(None, None)``.
SHIM_CLASS_STATEMENT = "class BasicStrategy(BasicFreqtradeStrategy):"

#: ``.gitignore`` lines appended by this work package, in order.
GITIGNORE_APPENDED = (
    "!user_data/strategies/",
    "user_data/strategies/*",
    "!user_data/strategies/BasicStrategy.py",
)

#: The public API of ``trading_platform.strategy``, name for name.
EXPECTED_PUBLIC_API = [
    "BOOL_SIGNAL_COLUMNS",
    "ENGINE_VERSION",
    "FREQTRADE_INTERFACE_VERSION",
    "FREQTRADE_ORDER_COLUMNS",
    "SIGNAL_TO_FREQTRADE_COLUMNS",
    "STRATEGIES",
    "BasicStrategy",
    "BasicStrategyParams",
    "FeatureBundle",
    "Strategy",
    "StrategyParams",
    "TimesFMForecastParams",
    "TimesFMForecastStrategy",
    "atr",
    "attach_features",
    "ema",
    "ensure_signal_frame",
    "freqtrade_adapter",
    "freqtrade_available",
    "get_strategy",
    "indicators",
    "make_freqtrade_strategy",
    "make_runner",
    "register_strategy",
    "require_ohlcv_frame",
    "resolve_features",
    "rsi",
    "run_backtest",
    "run_backtest_on_config",
    "strategy_names",
    "strategy_param_space",
    "true_range",
    "validate_freqtrade_adapter_class",
]

#: The names this module re-exports from the adapter (its own public contract).
ADAPTER_REEXPORTS = (
    "FREQTRADE_INTERFACE_VERSION",
    "FREQTRADE_ORDER_COLUMNS",
    "SIGNAL_TO_FREQTRADE_COLUMNS",
    "freqtrade_available",
    "make_freqtrade_strategy",
    "validate_freqtrade_adapter_class",
)

#: Subprocess proof that the strategy namespace survives a checkout without the
#: optional extra: every ``freqtrade`` import raises the ``ModuleNotFoundError`` a
#: ``.[dev]``-only environment raises (``pytest.importorskip`` only skips on it).
_BLOCKED_FREQTRADE_CODE = """
import sys


class _FreqtradeBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "freqtrade" or fullname.startswith("freqtrade."):
            raise ModuleNotFoundError("No module named 'freqtrade' (simulated dev-only checkout)")
        return None


sys.meta_path.insert(0, _FreqtradeBlocker())
import trading_platform
import trading_platform.strategy as strategy

print("OK", "freqtrade" in sys.modules, hasattr(strategy, "make_freqtrade_strategy"))
"""

# ---------------------------------------------------------------------------
# module-scoped hygiene: deliberately none
# ---------------------------------------------------------------------------
#
# This module imports the real library (through ``StrategyResolver``), and it must
# **not** purge ``freqtrade*`` from ``sys.modules`` on the way out.  Purging makes
# the next ``import freqtrade.strategy`` re-execute the module and build a
# **second** ``IStrategy`` class object; every class built on the first one — the
# generated adapter classes, and ``user_data/strategies/BasicStrategy.py`` as
# ``StrategyResolver`` resolves it — then fails Freqtrade's own
# ``issubclass(obj, IStrategy)`` check, and ``isinstance`` checks in a module that
# imported the first generation at collection time fail too.
#
# The failure-path tests of the sibling modules do not need anyone to clean up
# after this module: each one installs its own simulation
# (``_simulate_missing_freqtrade``, or a subprocess with a meta-path blocker) and
# purges the cache *inside* ``monkeypatch``, which puts the very same module
# objects back at teardown.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _freqtrade_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return ``frame`` the way Freqtrade hands it to ``populate_*``.

    A positional ``RangeIndex`` plus the timestamps in a ``date`` column — not the
    house OHLCV contract, which is exactly why the adapter translates it back.
    """
    out = frame.copy()
    out.index = pd.RangeIndex(len(out))
    out["date"] = frame.index.to_numpy()
    return out


def _house_signals(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the house signal frame of ``frame`` for the ``basic`` strategy."""
    strategy = registry.get_strategy("basic")
    return strategy.signals(strategy.prepare(frame))


def _resolved_shim_class() -> tuple[type, Path]:
    """Return ``(class, path)`` for ``BasicStrategy`` as Freqtrade resolves it.

    The real ``StrategyResolver._search_object`` is used on the *shipped*
    ``user_data/strategies`` directory: no network, no configuration file, no
    running bot.
    """
    from freqtrade.resolvers.strategy_resolver import StrategyResolver

    cls, path = StrategyResolver._search_object(
        REPO_ROOT / "user_data" / "strategies", object_name="BasicStrategy"
    )
    assert cls is not None, "user_data/strategies/BasicStrategy.py does not resolve"
    assert path is not None
    return cls, path


# ---------------------------------------------------------------------------
# offline guards -- these run on the CI, where freqtrade is absent
# ---------------------------------------------------------------------------


def test_strategy_namespace_reexports_the_adapter_api() -> None:
    """``trading_platform.strategy`` exposes the adapter, and only the agreed names."""
    import trading_platform.strategy as strategy

    assert strategy.__all__ == EXPECTED_PUBLIC_API
    for name in EXPECTED_PUBLIC_API:
        assert hasattr(strategy, name), f"trading_platform.strategy.{name} is missing"
    for name in ADAPTER_REEXPORTS:
        assert getattr(strategy, name) is getattr(strategy.freqtrade_adapter, name)
    assert strategy.freqtrade_adapter.__name__ == "trading_platform.strategy.freqtrade_adapter"


def test_strategy_namespace_never_imports_freqtrade_basic() -> None:
    """The concrete module is never imported here: it needs the optional extra.

    ``freqtrade_basic`` builds its class at import time, so a top-level import of
    it would make ``import trading_platform`` fail on a ``.[dev]``-only checkout.
    The check is an AST one — a plain substring search would also match the
    docstring that *documents* the rule — plus a proof that the only textual
    mentions of the name live in that docstring.
    """
    source = INIT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
    assert imported, "the namespace is expected to import its public objects"
    assert not [name for name in imported if "freqtrade_basic" in name]

    docstring = ast.get_docstring(tree) or ""
    assert "freqtrade_basic" in docstring
    assert source.count("freqtrade_basic") == docstring.count("freqtrade_basic")


def test_importing_the_strategy_namespace_survives_a_missing_freqtrade() -> None:
    """End-to-end proof: ``import trading_platform.strategy`` works without the extra."""
    result = subprocess.run(
        [sys.executable, "-c", _BLOCKED_FREQTRADE_CODE],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src")),
    )
    assert result.stdout.strip() == "OK False True"


def test_the_shim_carries_a_literal_class_statement() -> None:
    """Freqtrade's resolver needs the literal statement, not a factory alias.

    ``StrategyResolver._search_object`` skips any file whose text lacks
    ``class <Name>(`` and ``_get_valid_object`` additionally requires
    ``__module__ == file stem``; an alias such as
    ``BasicStrategy = make_freqtrade_strategy("basic")`` therefore resolves to
    ``(None, None)``.  The dedicated negative control is
    :func:`test_a_factory_alias_file_cannot_be_resolved`.
    """
    source = SHIM_PATH.read_text(encoding="utf-8")
    assert SHIM_CLASS_STATEMENT in source
    assert "from trading_platform.strategy.freqtrade_basic import BasicFreqtradeStrategy" in source
    assert sorted(path.name for path in SHIM_PATH.parent.glob("*.py")) == ["BasicStrategy.py"]
    # No rule is recopied in the shim: the class body is the docstring only.
    tree = ast.parse(source)
    shim_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BasicStrategy"
    )
    assert [child for child in shim_class.body if not isinstance(child, ast.Expr)] == [], (
        "the shim must stay an empty subclass"
    )


def test_gitignore_allows_the_shim_and_keeps_the_readme_rule() -> None:
    """``user_data/*`` is negated for one file only, right after the README rule."""
    lines = GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    assert "user_data/*" in lines
    readme = lines.index("!user_data/README.md")
    assert lines[readme + 1 : readme + 1 + len(GITIGNORE_APPENDED)] == list(GITIGNORE_APPENDED)


# ---------------------------------------------------------------------------
# the shipped class -- real Freqtrade interface
# ---------------------------------------------------------------------------


def test_module_documents_the_optional_extra_and_the_name_resolution() -> None:
    """The module docstring states the two facts a user needs, and its API is frozen."""
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from trading_platform.strategy import freqtrade_basic

    docstring = freqtrade_basic.__doc__ or ""
    assert "freqtrade" in docstring and "extra" in docstring
    assert "class name" in docstring
    assert "user_data/strategies" in docstring
    assert freqtrade_basic.__all__ == [
        "BASIC_FREQTRADE_STRATEGY_NAME",
        "BasicFreqtradeStrategy",
    ]


def test_basic_strategy_name_is_the_freqtrade_layer_name() -> None:
    """``BASIC_FREQTRADE_STRATEGY_NAME`` is the single source of truth of the name."""
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from trading_platform.strategy.freqtrade_basic import BASIC_FREQTRADE_STRATEGY_NAME

    assert BASIC_FREQTRADE_STRATEGY_NAME == DEFAULT_STRATEGY_NAME == "BasicStrategy"


def test_the_shipped_class_name_is_the_one_the_configurations_declare() -> None:
    """The generated class name matches ``config/freqtrade*.json`` (read-only check)."""
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from trading_platform.strategy.freqtrade_basic import BasicFreqtradeStrategy

    for path in FREQTRADE_CONFIGS:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["strategy"] == BasicFreqtradeStrategy.__name__


def test_the_shipped_class_is_a_valid_freqtrade_istrategy() -> None:
    """Every attribute Freqtrade reads is present, with the documented value."""
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from freqtrade.strategy import IStrategy

    from trading_platform.strategy.freqtrade_adapter import validate_freqtrade_adapter_class
    from trading_platform.strategy.freqtrade_basic import BasicFreqtradeStrategy

    assert issubclass(BasicFreqtradeStrategy, IStrategy)
    assert BasicFreqtradeStrategy.INTERFACE_VERSION == 3
    assert BasicFreqtradeStrategy.__name__ == "BasicStrategy" == DEFAULT_STRATEGY_NAME
    assert BasicFreqtradeStrategy.timeframe == DEFAULT_TIMEFRAME == "1h"
    assert BasicFreqtradeStrategy.stoploss == -0.99
    assert BasicFreqtradeStrategy.use_custom_stoploss is True
    assert BasicFreqtradeStrategy.minimal_roi == {"0": 100.0}
    assert BasicFreqtradeStrategy.can_short is False
    assert BasicFreqtradeStrategy.startup_candle_count == 21
    assert BasicFreqtradeStrategy.house_strategy_name == "basic"
    assert validate_freqtrade_adapter_class(BasicFreqtradeStrategy) is None


def test_the_shipped_class_instantiates_with_the_empty_config() -> None:
    """Freqtrade's own ``{}`` config is enough to build an instance."""
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from freqtrade.strategy import IStrategy

    from trading_platform.strategy.freqtrade_basic import BasicFreqtradeStrategy

    instance = BasicFreqtradeStrategy({})
    assert isinstance(instance, IStrategy)
    assert instance.timeframe == DEFAULT_TIMEFRAME
    assert instance.house_strategy_name == "basic"
    assert instance._entry_stops == {}


def test_the_three_populate_methods_render_the_freqtrade_signal_columns(
    ohlcv_frame: pd.DataFrame,
) -> None:
    """``populate_indicators`` / ``_entry_trend`` / ``_exit_trend`` produce Freqtrade's names."""
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from trading_platform.strategy.freqtrade_adapter import FREQTRADE_ORDER_COLUMNS
    from trading_platform.strategy.freqtrade_basic import BasicFreqtradeStrategy

    instance = BasicFreqtradeStrategy({})
    frame = _freqtrade_frame(ohlcv_frame)

    indicators = instance.populate_indicators(frame, METADATA)
    for column in ("ema_fast", "ema_slow", "rsi", "atr"):
        assert column in indicators.columns
    entries = instance.populate_entry_trend(indicators, METADATA)
    exits = instance.populate_exit_trend(entries, METADATA)

    for column in FREQTRADE_ORDER_COLUMNS:
        assert column in exits.columns
    assert "entry_long" not in exits.columns
    assert "entry_short" not in exits.columns
    assert len(exits) == len(ohlcv_frame)
    assert exits["date"].tolist() == frame["date"].tolist()
    assert exits["close"].tolist() == frame["close"].tolist()
    assert exits["enter_long"].dtype == bool


# ---------------------------------------------------------------------------
# load-by-name -- the reason the shim exists
# ---------------------------------------------------------------------------


def test_the_strategy_resolver_loads_the_shim_by_name() -> None:
    """``--strategy BasicStrategy`` resolves: the class carries the right ``__module__``."""
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from freqtrade.strategy import IStrategy

    cls, path = _resolved_shim_class()
    assert path == SHIM_PATH
    assert cls.__module__ == "BasicStrategy"
    assert cls.__name__ == "BasicStrategy" == DEFAULT_STRATEGY_NAME
    assert issubclass(cls, IStrategy)
    instance = cls({})
    assert isinstance(instance, IStrategy)
    assert instance.house_strategy_name == "basic"
    assert instance._entry_stops == {}


def test_a_factory_alias_file_cannot_be_resolved(tmp_path: Path) -> None:
    """Negative control: an alias file resolves to ``(None, None)`` — hence the shim.

    The resolved object must be *defined* in the scanned file (``__module__``
    equals the file stem), which an assignment to the factory's result never
    satisfies.  This is what makes ``user_data/strategies/BasicStrategy.py`` a
    literal subclass instead of a one-line export.
    """
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from freqtrade.resolvers.strategy_resolver import StrategyResolver

    alias = tmp_path / "AliasStrategy.py"
    alias.write_text(
        "from trading_platform.strategy.freqtrade_adapter import make_freqtrade_strategy\n"
        "\n"
        'AliasStrategy = make_freqtrade_strategy("basic", class_name="AliasStrategy")\n',
        encoding="utf-8",
    )
    assert StrategyResolver._search_object(tmp_path, object_name="AliasStrategy") == (None, None)


# ---------------------------------------------------------------------------
# integration -- the shim on the real BTC/USDT cache
# ---------------------------------------------------------------------------


def test_the_resolved_shim_renders_the_house_signals_on_the_real_cache() -> None:
    """Integration: the class Freqtrade loads renders **exactly** the house signals.

    Cross-check of the two engines on ``data/cache/binance/BTC_USDT/1h.parquet``
    (17543 candles, 2023-01-01 .. 2025-01-01).  With the shipped defaults the
    house ``basic`` strategy produces **395** ``entry_long`` and **400**
    ``exit_long`` signals; the resolved shim must produce the same counts, and the
    same booleans candle by candle, under Freqtrade's names (``enter_long`` /
    ``exit_long``).  That equality is the proof that the ``entry_*`` ->
    ``enter_*`` translation survives Freqtrade's own loader and that the shim adds
    no logic of its own.  The PnL of the two engines is a different matter and is
    **not** compared (``docs/architecture.md`` §4.9.4).
    """
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    if not CACHE_PATH.exists():
        pytest.skip(f"real market data cache is absent: {CACHE_PATH}")

    data = pd.read_parquet(CACHE_PATH)
    frame = _freqtrade_frame(data)
    cls, _ = _resolved_shim_class()
    instance = cls({})

    out = instance.populate_indicators(frame, METADATA)
    out = instance.populate_entry_trend(out, METADATA)
    out = instance.populate_exit_trend(out, METADATA)
    assert len(out) == len(data)

    signals = _house_signals(data)
    np.testing.assert_array_equal(out["enter_long"].to_numpy(), signals["entry_long"].to_numpy())
    np.testing.assert_array_equal(out["exit_long"].to_numpy(), signals["exit_long"].to_numpy())
    np.testing.assert_array_equal(out["enter_short"].to_numpy(), signals["entry_short"].to_numpy())
    np.testing.assert_array_equal(out["exit_short"].to_numpy(), signals["exit_short"].to_numpy())

    entries = int(out["enter_long"].sum())
    exits = int(out["exit_long"].sum())
    assert entries == int(signals["entry_long"].sum())
    assert exits == int(signals["exit_long"].sum())
    assert entries > 0
    assert exits > 0
    if len(data) == 17543:  # the shipped cache: the counts quoted in the docstring
        assert entries == 395
        assert exits == 400
    assert "entry_long" not in out.columns
