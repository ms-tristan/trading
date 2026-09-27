"""Tests of the Freqtrade exposure of the eight new house strategies.

One parametrized file covers the whole catalogue growth, because the eight
exposures are **mechanical by construction**: each one is a copy of
``trading_platform.strategy.freqtrade_momentum`` with a different house name, a
different frozen grid and its own justification paragraph, paired with a literal
``class`` statement in ``user_data/strategies``.  Only the *table* below differs
from one strategy to the next, so a single parametrized module pins the contract
of all eight instead of eight near-identical files.

Two groups, and the split mirrors ``docs/testing-policy.md`` §1.2:

* the **offline guards** — the concrete module exists, it is never imported by
  the public namespace (asserted against ``sys.modules`` in a child interpreter,
  the only way to prove it without depending on what the current session already
  imported), the shim carries the literal ``class`` statement Freqtrade's
  resolver needs, ``.gitignore`` keeps every shim tracked, and the shared
  Freqtrade defaults are untouched — need **no** Freqtrade at all and must run on
  the CI, which installs the ``.[dev]`` extra only;
* everything that builds or resolves a real ``IStrategy`` calls
  ``pytest.importorskip("freqtrade", ...)`` **inside the test body**.

Why the guard is inside the test and not at module level: a module-level
``importorskip`` skips the *whole* module, which would also skip the offline
guards above — that is, precisely the assertions that make this file useful on a
checkout without the optional extra.

References: ``tests/test_strategy_freqtrade_basic.py`` (the idioms this file
mirrors: the AST/text assertions on the shim, the ``.gitignore`` check, the
``_search_object`` resolution), :mod:`trading_platform.strategy.freqtrade_adapter`
(the single translation seam) and :mod:`trading_platform.strategy.freqtrade_momentum`
(the reference exposure copied eight times).
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The package holding the concrete exposures and their shims.
STRATEGY_PACKAGE = REPO_ROOT / "src" / "trading_platform" / "strategy"

#: The public namespace of the strategy layer (the "never import" contract).
INIT_PATH = STRATEGY_PACKAGE / "__init__.py"

#: The directory ``StrategyResolver`` scans for a class name.
STRATEGIES_DIR = REPO_ROOT / "user_data" / "strategies"

#: The rules that keep the eight shims tracked while ``user_data/strategies/*`` stays ignored.
GITIGNORE_PATH = REPO_ROOT / ".gitignore"

#: The eight exposures: ``(house name, Freqtrade class name, module path, shim path, frozen grid)``.
EXPOSURES: tuple[tuple[str, str, Path, Path, str], ...] = (
    (
        "bollinger",
        "BollingerStrategy",
        STRATEGY_PACKAGE / "freqtrade_bollinger.py",
        STRATEGIES_DIR / "BollingerStrategy.py",
        "1h",
    ),
    (
        "donchian",
        "DonchianStrategy",
        STRATEGY_PACKAGE / "freqtrade_donchian.py",
        STRATEGIES_DIR / "DonchianStrategy.py",
        "4h",
    ),
    (
        "dual_thrust",
        "DualThrustStrategy",
        STRATEGY_PACKAGE / "freqtrade_dual_thrust.py",
        STRATEGIES_DIR / "DualThrustStrategy.py",
        "1h",
    ),
    (
        "faber",
        "FaberStrategy",
        STRATEGY_PACKAGE / "freqtrade_faber.py",
        STRATEGIES_DIR / "FaberStrategy.py",
        "1d",
    ),
    (
        "keltner",
        "KeltnerStrategy",
        STRATEGY_PACKAGE / "freqtrade_keltner.py",
        STRATEGIES_DIR / "KeltnerStrategy.py",
        "4h",
    ),
    (
        "macd",
        "MacdStrategy",
        STRATEGY_PACKAGE / "freqtrade_macd.py",
        STRATEGIES_DIR / "MacdStrategy.py",
        "4h",
    ),
    (
        "rsi_reversion",
        "RsiReversionStrategy",
        STRATEGY_PACKAGE / "freqtrade_rsi_reversion.py",
        STRATEGIES_DIR / "RsiReversionStrategy.py",
        "1d",
    ),
    (
        "supertrend",
        "SupertrendStrategy",
        STRATEGY_PACKAGE / "freqtrade_supertrend.py",
        STRATEGIES_DIR / "SupertrendStrategy.py",
        "4h",
    ),
)

#: Pytest ids: the house name of each exposure.
IDS = [house for house, *_ in EXPOSURES]

#: The ``startup_candle_count`` the adapter derives for each exposure: the largest
#: integer **default** of the house parameter model.  The adapter computes it with
#: :func:`~trading_platform.strategy.freqtrade_parameters.startup_candle_count_for`;
#: the guarded test recomputes it independently from the model (see
#: :func:`_max_int_default`) and pins both against this table.
STARTUP_CANDLE_COUNTS: dict[str, int] = {
    "bollinger": 20,  # period
    "donchian": 20,  # entry_period
    "dual_thrust": 14,  # atr_period
    "faber": 200,  # sma_period
    "keltner": 100,  # trend_ema_period
    "macd": 26,  # slow_period
    "rsi_reversion": 200,  # trend_ema_period
    "supertrend": 10,  # atr_period
}

#: ``.gitignore`` lines the catalogue growth appends to the allow-list block of
#: ``user_data/strategies``: the four legacy rules, then the eight new shims.
EXPECTED_ALLOW_LIST: tuple[str, ...] = (
    "!user_data/strategies/",
    "user_data/strategies/*",
    "!user_data/strategies/BasicStrategy.py",
    "!user_data/strategies/MomentumStrategy.py",
    *(f"!user_data/strategies/{class_name}.py" for _, class_name, _, _, _ in EXPOSURES),
)

#: The three arguments ``populate_*`` receives from Freqtrade.
METADATA: dict[str, object] = {"pair": "BTC/USDT", "timeframe": "1h", "dataframe": None}

#: Child interpreter proving that importing the public namespace never pulls a
#: concrete exposure in.  ``{concrete}`` is replaced by the tuple of the eight
#: module stems before the snippet is executed.
_BLOCKED_FREQTRADE_CODE = """
import json
import sys

import trading_platform
import trading_platform.strategy as strategy

concrete = {concrete}
prefix = "trading_platform.strategy."
leaked = sorted(
    name for name in sys.modules if name.startswith(prefix) and name[len(prefix) :] in concrete
)
print(
    json.dumps(
        {
            "leaked": leaked,
            "namespace_loaded": hasattr(strategy, "STRATEGIES"),
            "freqtrade_imported": "freqtrade" in sys.modules,
        }
    )
)
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _imported_modules(path: Path) -> list[str]:
    """Return every module name the source of ``path`` imports.

    Reading and parsing the shipped source works on a checkout without the
    optional extra, which is exactly what the offline guards need.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
    return imported


def _max_int_default(model: type[Any]) -> int:
    """Return the largest ``int`` default of a pydantic parameter model.

    Booleans are excluded on purpose (``bool`` is a subclass of ``int``, but a
    boolean is a switch, never a look-back).  This is the independent
    recomputation of what the adapter derives through
    :func:`~trading_platform.strategy.freqtrade_parameters.startup_candle_count_for`.
    """
    values = [
        int(spec.default)
        for spec in model.model_fields.values()
        if spec.annotation is int and isinstance(spec.default, int)
    ]
    return max(values, default=0)


def _freqtrade_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return ``frame`` the way Freqtrade hands it to ``populate_*``.

    A positional ``RangeIndex`` plus the timestamps in a ``date`` column — not the
    house OHLCV contract, which is exactly why the adapter translates it back.
    """
    out = frame.copy()
    out.index = pd.RangeIndex(len(out))
    out["date"] = frame.index.to_numpy()
    return out


def _shim_class_statement(class_name: str) -> str:
    """Return the literal statement Freqtrade's resolver searches for."""
    return f"class {class_name}("


def _adapter_class_name(class_name: str) -> str:
    """Return the concrete adapter class name of the Freqtrade class ``class_name``.

    ``"BollingerStrategy"`` -> ``"BollingerFreqtradeStrategy"``: the shim carries
    the name Freqtrade looks up, the concrete module carries the house class the
    adapter builds.  Keeping the two spellings apart in one helper is what makes
    the table above readable.
    """
    return f"{class_name.removesuffix('Strategy')}FreqtradeStrategy"


# ---------------------------------------------------------------------------
# offline guards -- these run on the CI, where freqtrade is absent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("house_name", "class_name", "module_path", "shim_path", "grid"), EXPOSURES, ids=IDS
)
def test_the_concrete_module_exists(
    house_name: str, class_name: str, module_path: Path, shim_path: Path, grid: str
) -> None:
    """Offline: the file exists and documents the four facts a reader needs.

    The docstring must state the optional extra, the module-level factory call,
    the ``class name`` resolution rule of ``StrategyResolver`` and the directory
    it scans — and, for this catalogue growth, the grid it freezes.
    """
    source = module_path.read_text(encoding="utf-8")
    assert "freqtrade" in source and "extra" in source
    assert "class name" in source
    assert "user_data/strategies" in source
    assert f'make_freqtrade_strategy("{house_name}", timeframe="{grid}")' in source
    assert f'__all__ = [\n    "{house_name.upper()}_FREQTRADE_STRATEGY_NAME",' in source
    assert _adapter_class_name(class_name) in source


@pytest.mark.parametrize(
    ("house_name", "class_name", "module_path", "shim_path", "grid"), EXPOSURES, ids=IDS
)
def test_the_shim_carries_a_literal_class_statement(
    house_name: str, class_name: str, module_path: Path, shim_path: Path, grid: str
) -> None:
    """Offline: the text and the AST of the shim are what ``StrategyResolver`` reads.

    ``StrategyResolver._search_object`` skips any file whose text lacks
    ``class <Name>(`` and ``_get_valid_object`` additionally requires
    ``__module__ == file stem``; a factory alias would resolve to
    ``(None, None)``.  The offline half proves the file is shaped correctly; the
    guarded half proves the real resolver actually finds it.
    """
    assert shim_path.exists(), f"missing Freqtrade entry point: {shim_path.name}"
    source = shim_path.read_text(encoding="utf-8")
    assert _shim_class_statement(class_name) in source
    assert (
        f"from trading_platform.strategy.freqtrade_{house_name} import "
        f"{_adapter_class_name(class_name)}" in source
    )

    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in classes] == [class_name]
    # No indicator and no rule is recopied in the shim: the body is the docstring only.
    assert [child for child in classes[0].body if not isinstance(child, ast.Expr)] == [], (
        "the shim must stay an empty subclass"
    )
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
    assert [node.module for node in imports] == [
        f"trading_platform.strategy.freqtrade_{house_name}"
    ]


def test_every_python_file_of_the_strategy_directory_is_a_shim() -> None:
    """Offline: ``user_data/strategies`` holds exactly the ten shipped shims.

    The directory Freqtrade scans is a *shipped* surface: every module in it is
    read on every class-name lookup, so a stray file there is venue-facing debt.
    The catalogue growth takes the expected listing from two files to ten;
    spelling the ten names out is what keeps the next addition deliberate.
    ``tests/test_strategy_freqtrade_basic.py`` pins the two legacy names inside
    its own shim test — that assertion predates this catalogue and must be
    extended to this same ten-name list.
    """
    expected = sorted(
        [
            "BasicStrategy.py",
            "MomentumStrategy.py",
            *(f"{class_name}.py" for _, class_name, _, _, _ in EXPOSURES),
        ]
    )
    assert sorted(path.name for path in STRATEGIES_DIR.glob("*.py")) == expected


@pytest.mark.parametrize(
    ("house_name", "class_name", "module_path", "shim_path", "grid"), EXPOSURES, ids=IDS
)
def test_the_concrete_module_is_not_reachable_from_the_public_namespace(
    house_name: str, class_name: str, module_path: Path, shim_path: Path, grid: str
) -> None:
    """Offline: the concrete module is neither imported nor re-exported by the namespace.

    ``freqtrade_<name>`` builds its class at import time, so a top-level import of
    it would make ``import trading_platform`` fail on a ``.[dev]``-only checkout.
    The check is an AST one plus a source scan — a plain substring search alone
    would be confused by a docstring that documents the rule.
    """
    import trading_platform.strategy as strategy

    assert not hasattr(strategy, f"freqtrade_{house_name}")
    stem = f"freqtrade_{house_name}"
    assert not [name for name in _imported_modules(INIT_PATH) if stem in name]
    assert stem not in INIT_PATH.read_text(encoding="utf-8")


def test_importing_the_namespace_never_imports_a_concrete_exposure() -> None:
    """Offline: a clean interpreter proves it against ``sys.modules``.

    A child process is the only honest way to assert on ``sys.modules``: inside
    this session another test may already have imported a concrete exposure on
    purpose (the guarded half does).  The child imports the public namespace and
    reports every concrete module that leaked into ``sys.modules`` — the answer
    must be "none" — plus the two facts that make the run meaningful (the
    namespace really loaded, and Freqtrade itself was never imported).
    """
    concrete = ", ".join(f'"freqtrade_{house}"' for house, *_ in EXPOSURES)
    result = subprocess.run(
        [sys.executable, "-c", _BLOCKED_FREQTRADE_CODE.replace("{concrete}", f"({concrete},)")],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src")),
    )
    payload = json.loads(result.stdout.strip())
    assert payload["leaked"] == []
    assert payload["namespace_loaded"] is True
    assert payload["freqtrade_imported"] is False


@pytest.mark.parametrize(
    ("house_name", "class_name", "module_path", "shim_path", "grid"), EXPOSURES, ids=IDS
)
def test_gitignore_keeps_the_shim_tracked(
    house_name: str, class_name: str, module_path: Path, shim_path: Path, grid: str
) -> None:
    """Offline: ``user_data/strategies/*`` is negated for every shipped shim.

    The allow-list is asserted as a whole block, right after the README rule:
    a negation that lands before ``user_data/strategies/*`` (or after a later
    re-inclusion of the directory) would silently stop tracking the file, and a
    shim that is not committed is a strategy Freqtrade cannot load on the
    deployed stack.
    """
    lines = GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    assert "user_data/*" in lines
    readme = lines.index("!user_data/README.md")
    block = lines[readme + 1 : readme + 1 + len(EXPECTED_ALLOW_LIST)]
    assert block == list(EXPECTED_ALLOW_LIST)
    assert f"!user_data/strategies/{class_name}.py" in lines


def test_the_shared_freqtrade_defaults_are_untouched() -> None:
    """Offline: growing the catalogue changed no shared Freqtrade default.

    The eight exposures are *additions*: the classifier constants of the
    Freqtrade layer (the default strategy class name and dry-run wallet of
    ``config/freqtrade*.json``) and the frozen adapter constants (interface
    version, the ``enter_*`` spelling, the disabled ROI, the global stop-loss
    bound) must all read exactly as before.
    """
    from trading_platform.freqtrade import (
        DEFAULT_DRY_RUN_WALLET,
        DEFAULT_STRATEGY_NAME,
        FREQTRADE_REQUIRED_KEYS,
    )
    from trading_platform.strategy.freqtrade_adapter import (
        FREQTRADE_DISABLED_ROI,
        FREQTRADE_INTERFACE_VERSION,
        FREQTRADE_ORDER_COLUMNS,
        SIGNAL_TO_FREQTRADE_COLUMNS,
    )
    from trading_platform.strategy.freqtrade_stoploss import DEFAULT_FREQTRADE_STOPLOSS

    assert DEFAULT_STRATEGY_NAME == "BasicStrategy"
    assert DEFAULT_DRY_RUN_WALLET == 1000.0
    assert "max_open_trades" in FREQTRADE_REQUIRED_KEYS
    assert FREQTRADE_INTERFACE_VERSION == 3
    assert FREQTRADE_ORDER_COLUMNS == ("enter_long", "exit_long", "enter_short", "exit_short")
    assert SIGNAL_TO_FREQTRADE_COLUMNS == {
        "entry_long": "enter_long",
        "exit_long": "exit_long",
        "entry_short": "enter_short",
        "exit_short": "exit_short",
    }
    assert FREQTRADE_DISABLED_ROI == {"0": 100.0}
    assert DEFAULT_FREQTRADE_STOPLOSS == -0.99


def test_the_catalogue_covers_every_new_house_strategy() -> None:
    """Offline: the eight exposures are exactly the eight new registry entries.

    The table above is the contract of this package; the registry is the single
    source of truth of the catalogue.  Both are compared, minus the two
    strategies that were exposed before this delivery.
    """
    from trading_platform.strategy.registry import strategy_names

    exposed = {house for house, *_ in EXPOSURES}
    assert set(strategy_names()) - {"basic", "momentum"} == exposed


# ---------------------------------------------------------------------------
# the shipped classes -- real Freqtrade interface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("house_name", "class_name", "module_path", "shim_path", "grid"), EXPOSURES, ids=IDS
)
def test_the_concrete_module_builds_the_expected_class(
    house_name: str, class_name: str, module_path: Path, shim_path: Path, grid: str
) -> None:
    """Guarded: the generated class carries the frozen name, grid and warm-up.

    ``startup_candle_count`` is derived by the adapter from the house parameter
    model; it is recomputed here from the model itself (never from the adapter
    helper, which would make the assertion a tautology) and cross-checked against
    :data:`STARTUP_CANDLE_COUNTS`, the documented value of this package.
    """
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from freqtrade.strategy import IStrategy

    from trading_platform.strategy.freqtrade_adapter import validate_freqtrade_adapter_class
    from trading_platform.strategy.registry import STRATEGIES

    module = importlib.import_module(f"trading_platform.strategy.freqtrade_{house_name}")
    expected_api = [
        f"{house_name.upper()}_FREQTRADE_STRATEGY_NAME",
        _adapter_class_name(class_name),
    ]
    assert module.__all__ == expected_api
    assert getattr(module, expected_api[0]) == class_name

    cls = getattr(module, expected_api[1])
    assert cls.__name__ == class_name
    assert issubclass(cls, IStrategy)
    assert cls.timeframe == grid
    assert cls.house_strategy_name == house_name
    assert cls.stoploss == -0.99
    assert cls.use_custom_stoploss is True
    assert cls.minimal_roi == {"0": 100.0}
    assert cls.can_short is False
    derived = _max_int_default(STRATEGIES[house_name].ParamsModel)
    assert derived == STARTUP_CANDLE_COUNTS[house_name] > 0
    assert cls.startup_candle_count == derived
    assert validate_freqtrade_adapter_class(cls) is None


@pytest.mark.parametrize(
    ("house_name", "class_name", "module_path", "shim_path", "grid"), EXPOSURES, ids=IDS
)
def test_the_concrete_class_instantiates_with_the_empty_config(
    house_name: str, class_name: str, module_path: Path, shim_path: Path, grid: str
) -> None:
    """Guarded: Freqtrade's own ``{}`` config is enough to build an instance."""
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from freqtrade.strategy import IStrategy

    cls = getattr(
        importlib.import_module(f"trading_platform.strategy.freqtrade_{house_name}"),
        _adapter_class_name(class_name),
    )
    instance = cls({})
    assert isinstance(instance, IStrategy)
    assert instance.timeframe == grid
    assert instance.house_strategy_name == house_name
    assert instance._entry_stops == {}


@pytest.mark.parametrize(
    ("house_name", "class_name", "module_path", "shim_path", "grid"), EXPOSURES, ids=IDS
)
def test_the_strategy_resolver_loads_the_shim_by_name(
    house_name: str, class_name: str, module_path: Path, shim_path: Path, grid: str
) -> None:
    """Guarded: the real ``StrategyResolver`` resolves ``--strategy <ClassName>``.

    Both halves of ``_search_object`` are exercised on the shipped directory: the
    file text contains ``class <Name>(`` and the class it keeps has
    ``__module__ == <file stem>``.  No network, no configuration file, no bot.
    """
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from freqtrade.resolvers.strategy_resolver import StrategyResolver
    from freqtrade.strategy import IStrategy

    cls, path = StrategyResolver._search_object(STRATEGIES_DIR, object_name=class_name)
    assert cls is not None, f"the shim {shim_path.name} does not resolve"
    assert path == shim_path.resolve()
    assert cls.__module__ == class_name == shim_path.stem
    assert issubclass(cls, IStrategy)
    instance = cls({})
    assert instance.timeframe == grid
    assert instance.house_strategy_name == house_name


@pytest.mark.parametrize(
    ("house_name", "class_name", "module_path", "shim_path", "grid"), EXPOSURES, ids=IDS
)
def test_the_populate_methods_render_the_house_columns(
    house_name: str,
    class_name: str,
    module_path: Path,
    shim_path: Path,
    grid: str,
    ohlcv_frame: pd.DataFrame,
) -> None:
    """Guarded: the three ``populate_*`` translate the house columns, and never rename them.

    The frame handed in is Freqtrade's (positional index plus a ``date`` column)
    and the frame handed back must keep its shape while gaining the house
    indicator columns and Freqtrade's four order columns.  ``entry_long`` /
    ``entry_short`` must be **absent**: Freqtrade does not read them, and their
    presence would be the classic silent bug of this contract.
    """
    pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")
    from trading_platform.strategy.freqtrade_adapter import FREQTRADE_ORDER_COLUMNS

    house_module = importlib.import_module(f"trading_platform.strategy.{house_name}")
    cls = getattr(
        importlib.import_module(f"trading_platform.strategy.freqtrade_{house_name}"),
        _adapter_class_name(class_name),
    )
    instance = cls({})
    metadata = {**METADATA, "timeframe": grid, "pair": f"{house_name.upper()}/USDT"}
    frame = _freqtrade_frame(ohlcv_frame)

    indicators = instance.populate_indicators(frame, metadata)
    for column in house_module.INDICATOR_COLUMNS:
        assert column in indicators.columns, f"{column} is missing from populate_indicators"
    entries = instance.populate_entry_trend(indicators, metadata)
    exits = instance.populate_exit_trend(entries, metadata)

    for column in FREQTRADE_ORDER_COLUMNS:
        assert column in exits.columns
    assert "entry_long" not in exits.columns
    assert "entry_short" not in exits.columns
    assert len(exits) == len(ohlcv_frame)
    assert exits["date"].tolist() == frame["date"].tolist()
    assert exits["close"].tolist() == frame["close"].tolist()
    for column in FREQTRADE_ORDER_COLUMNS:
        assert exits[column].dtype == bool
    # The adapter records the absolute stops of the analysed pair, NaN being "no stop".
    assert {pair for pair, _ in instance._entry_stops} <= {metadata["pair"]}
