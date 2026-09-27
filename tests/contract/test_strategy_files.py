"""Contract test: the ten freqtrade strategy modules of ``user_data/strategies/``.

Every module is parsed with :mod:`ast` -- it is never imported, so no freqtrade,
pandas or TA-Lib dependency is pulled in and a broken strategy cannot make the
suite crash instead of fail. The checks mirror the class contract documented in
``user_data/README.md``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

STRATEGY_FILES: tuple[str, ...] = (
    "BasicStrategy.py",
    "BollingerStrategy.py",
    "DonchianStrategy.py",
    "DualThrustStrategy.py",
    "FaberStrategy.py",
    "KeltnerStrategy.py",
    "MacdStrategy.py",
    "MomentumStrategy.py",
    "RsiReversionStrategy.py",
    "SupertrendStrategy.py",
)

#: ``dataframe["volume"] > 0`` -- mandatory guard around every signal assignment.
VOLUME_GUARD = re.compile(r"""dataframe\[\s*["']volume["']\s*\]\s*>\s*0""")

#: The indicators must come from TA-Lib, never from a hand-rolled rolling window.
TALIB_IMPORT = "import talib.abstract as ta"


class _NotALiteral:
    """Sentinel for a class attribute whose value is not a literal expression."""

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return "<non-literal expression>"


NOT_A_LITERAL = _NotALiteral()


def _source(strategy_file: str) -> str:
    return (REPO_ROOT / "user_data" / "strategies" / strategy_file).read_text(encoding="utf-8")


def _module(strategy_file: str) -> ast.Module:
    return ast.parse(_source(strategy_file))


def _single_class(strategy_file: str) -> ast.ClassDef:
    classes = [node for node in _module(strategy_file).body if isinstance(node, ast.ClassDef)]
    assert len(classes) == 1, (
        f"{strategy_file}: a strategy module must define exactly one class, "
        f"found {[node.name for node in classes]}"
    )
    return classes[0]


def _class_attributes(strategy_file: str) -> dict[str, object]:
    """The literal class attributes of the strategy class, keyed by name."""
    attributes: dict[str, object] = {}
    for node in _single_class(strategy_file).body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        try:
            attributes[target.id] = ast.literal_eval(value)
        except ValueError:
            attributes[target.id] = NOT_A_LITERAL
    return attributes


def _declared(strategy_file: str, name: str) -> object:
    attributes = _class_attributes(strategy_file)
    assert name in attributes, f"{strategy_file}: the strategy class does not declare {name}"
    value = attributes[name]
    assert value is not NOT_A_LITERAL, (
        f"{strategy_file}: {name} must be a literal so the contract can be checked statically"
    )
    return value


def _catalogue_entry(strategy_file: str) -> dict:
    document = json.loads((REPO_ROOT / "config" / "strategies.json").read_text(encoding="utf-8"))
    matches = [entry for entry in document["strategies"] if entry["file"] == strategy_file]
    assert len(matches) == 1, (
        f"config/strategies.json must carry exactly one entry with file {strategy_file!r}, "
        f"found {len(matches)}"
    )
    return matches[0]


@pytest.mark.parametrize("strategy_file", STRATEGY_FILES)
def test_strategy_module_has_a_docstring(strategy_file: str) -> None:
    docstring = ast.get_docstring(_module(strategy_file))
    assert docstring and docstring.strip(), f"{strategy_file}: missing module docstring"


@pytest.mark.parametrize("strategy_file", STRATEGY_FILES)
def test_strategy_module_defines_exactly_one_class_named_like_the_file_stem(
    strategy_file: str,
) -> None:
    class_node = _single_class(strategy_file)
    stem = strategy_file.removesuffix(".py")
    assert class_node.name == stem, (
        f"{strategy_file}: the strategy class must be named {stem!r}, found {class_node.name!r}"
    )


@pytest.mark.parametrize("strategy_file", STRATEGY_FILES)
def test_strategy_class_pins_the_interface_contract(strategy_file: str) -> None:
    assert _declared(strategy_file, "INTERFACE_VERSION") == 3, (
        f"{strategy_file}: INTERFACE_VERSION must be 3"
    )
    assert _declared(strategy_file, "can_short") is False, (
        f"{strategy_file}: can_short must be False"
    )
    assert _declared(strategy_file, "process_only_new_candles") is True, (
        f"{strategy_file}: process_only_new_candles must be True"
    )
    assert _declared(strategy_file, "use_exit_signal") is True, (
        f"{strategy_file}: use_exit_signal must be True"
    )


@pytest.mark.parametrize("strategy_file", STRATEGY_FILES)
def test_strategy_class_declares_startup_count_stoploss_and_roi(strategy_file: str) -> None:
    startup_candle_count = _declared(strategy_file, "startup_candle_count")
    assert isinstance(startup_candle_count, int) and startup_candle_count > 0, (
        f"{strategy_file}: startup_candle_count must be an explicit positive integer, "
        f"found {startup_candle_count!r}"
    )

    stoploss = _declared(strategy_file, "stoploss")
    assert isinstance(stoploss, float) and stoploss < 0, (
        f"{strategy_file}: stoploss must be an explicit negative ratio, found {stoploss!r}"
    )

    minimal_roi = _declared(strategy_file, "minimal_roi")
    assert isinstance(minimal_roi, dict) and minimal_roi, (
        f"{strategy_file}: minimal_roi must be an explicit non-empty mapping, found {minimal_roi!r}"
    )


@pytest.mark.parametrize("strategy_file", STRATEGY_FILES)
def test_strategy_module_imports_talib_abstract(strategy_file: str) -> None:
    assert TALIB_IMPORT in _source(strategy_file), (
        f"{strategy_file}: missing {TALIB_IMPORT!r}; indicators must come from TA-Lib"
    )


@pytest.mark.parametrize("strategy_file", STRATEGY_FILES)
def test_strategy_module_guards_its_signals_with_the_volume_condition(strategy_file: str) -> None:
    source = _source(strategy_file)
    guards = VOLUME_GUARD.findall(source)
    assert guards, (
        f'{strategy_file}: every signal assignment must be guarded by dataframe["volume"] > 0'
    )


@pytest.mark.parametrize("strategy_file", STRATEGY_FILES)
def test_strategy_catalogue_entry_matches_the_file_stem(strategy_file: str) -> None:
    entry = _catalogue_entry(strategy_file)
    stem = strategy_file.removesuffix(".py")
    assert entry["class_name"] == stem, (
        f"config/strategies.json: the entry for {strategy_file} declares class_name "
        f"{entry['class_name']!r} instead of {stem!r}"
    )
