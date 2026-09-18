"""Translation of the house pydantic parameter models into native Freqtrade parameters.

**Layer 3** (``trading_platform.strategy``): this module imports
:mod:`trading_platform.core` and :mod:`trading_platform.strategy` only — never
``config``, ``data``, ``metrics``, ``reporting``, ``validation`` or ``cli``.
``freqtrade`` is an **optional extra** (``trading-platform[freqtrade]``) that is
imported **lazily**, inside :func:`freqtrade_parameter_attributes` only, so that
importing this module — and therefore collecting the whole test-suite — never
requires Freqtrade to be installed.  No new runtime dependency is declared:
``pyproject.toml`` is frozen, and ``requirements.txt`` keeps its 7 entries.

Why this module exists
----------------------
The package writes every strategy **once**: indicators and entry/exit rules live
in :mod:`trading_platform.strategy` and their parameters are validated by a
pydantic :class:`~trading_platform.strategy.base.StrategyParams` model.  Freqtrade
knows nothing about pydantic: it hyperopts ``IntParameter`` / ``DecimalParameter``
/ ``BooleanParameter`` / ``CategoricalParameter`` objects that must be *class*
attributes of an ``IStrategy`` subclass.  This module is the pure translation
seam between both worlds: it turns one parameter model into

* a tuple of :class:`FreqtradeParamSpec` — a Freqtrade-free, inert description
  (kind, bounds, default, categories) that unit tests can assert on offline; and
* a mapping ``{name: <real Freqtrade parameter>}`` ready to be injected into a
  generated strategy class as class attributes.

Mapping rules (deterministic, in ``ParamsModel.model_fields`` declaration order)
-------------------------------------------------------------------------------
======================  ============================================================
pydantic annotation     Freqtrade parameter
======================  ============================================================
``bool``                ``BooleanParameter(default=..., space="buy")``
``int``                 ``IntParameter(low, high, default=..., space="buy")``
``float``               ``DecimalParameter(low, high, default=..., decimals=3, ...)``
grid with >= 2 values   ``CategoricalParameter(categories=..., default=..., ...)``
anything else           **not mapped** — see "Documented limits" below
======================  ============================================================

Bounds are read by **duck-typing** the pydantic v2 ``FieldInfo.metadata`` objects
(``ge`` / ``gt`` / ``lt`` / ``le``): ``annotated_types`` is a transitive pydantic
dependency and is deliberately *not* imported, because ``pyproject.toml`` is
frozen and ``tests/test_packaging.py`` pins the number of declared runtime
dependencies.

* ``int`` — ``low = ge`` (else ``gt + 1``, else ``0``);
  ``high = le`` (else ``lt - 1``, else ``max(low + 1, int(default * 2.0))``).
* ``float`` — ``low = ge`` (else ``gt + 10**-decimals``, else ``0.0``);
  ``high = le`` (else ``lt - 10**-decimals``, else ``round(default * 2.0, decimals)``);
  ``decimals = FREQTRADE_DECIMALS``.

**Mandatory guard** — Freqtrade does *not* validate that the default lies inside
``[low, high]`` (verified against 2026.8: ``IntParameter(1, 18, default=20)`` is
accepted silently), so every numeric spec returned by this module is clamped
until ``low <= default <= high`` holds and ``low < high`` is guaranteed (a
zero-width range would make hyperopt sample a single point).

Documented limits
-----------------
(i) **Exclusive bounds are approximated.** ``gt`` / ``lt`` have no equivalent in
    Freqtrade (``low`` / ``high`` are inclusive), so they are turned into
    inclusive bounds shifted by ``10**-decimals`` (``1e-3`` with the default
    three decimals).  A parameter declared ``gt=0`` becomes ``low=0.001``: the
    admissible set is *narrowed*, never widened, and the house model keeps
    enforcing the exact rule.
(ii) **``extra="forbid"`` and cross-field invariants are not translated.** The
    ``@model_validator`` rules of the house strategies (``ema_slow > ema_fast``,
    ``rsi_min < rsi_max``) and the "unknown key" rejection have no native
    Freqtrade counterpart: Freqtrade parameters are independent, per-field
    objects.  They stay enforced at runtime by the house ``ParamsModel`` — the
    adapter validates its effective parameters through the model before running.
(iii) **Freqtrade parameters are class attributes.** A per-*instance* parameter
    set cannot be expressed on a generated class: the factory reads the
    *validated mapping* of the target model, so the generated class defaults
    reflect it, while a Freqtrade params JSON file (``--strategy-params`` /
    ``buy_params``) can still override them at bot start — that override bypasses
    the house model, hence the runtime re-validation of (ii).
(iv) **Unsupported annotations are silently omitted.** ``str``, ``Enum``,
    ``datetime`` and ``Sequence`` fields are *not* mapped (Freqtrade has no
    scalar parameter object for them): they are absent from the result and the
    house default applies at runtime.  :func:`freqtrade_param_specs` never
    raises for them; use ``overrides`` when such a field must be exposed (for
    instance as a ``CategoricalParameter``).

The manual path (``overrides``)
-------------------------------
``overrides`` is the documented escape hatch: after the automatic pass it
*replaces* a spec by name (same position) or *adds* a new one (appended, in the
order given).  Overridden specs go through the same numeric guard, so a
well-formed override is returned untouched while a malformed one (default
outside its bounds) is repaired instead of being emitted.

Determinism
-----------
The result depends only on the model, the grid and the overrides — no global
state, no dictionary ordering from ``set`` (pydantic metadata objects exist at
most once per bound) and no I/O.  ``grid`` is **never inferred**: pass
``Strategy.param_space()`` explicitly to opt into the categorical mapping.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic.fields import FieldInfo

from trading_platform.core.errors import StrategyError
from trading_platform.strategy.base import Strategy, StrategyParams

__all__ = [
    "FREQTRADE_DECIMALS",
    "FREQTRADE_PARAM_SPACE",
    "FREQTRADE_UNBOUNDED_HIGH_FACTOR",
    "FreqtradeParamSpec",
    "freqtrade_param_specs",
    "freqtrade_parameter_attributes",
    "startup_candle_count_for",
]

#: Single hyperopt space used for every mapped house parameter.
FREQTRADE_PARAM_SPACE: str = "buy"

#: Default number of decimals of a Freqtrade ``DecimalParameter``.
FREQTRADE_DECIMALS: int = 3

#: Factor applied to a default to build an upper bound when the model declares none.
FREQTRADE_UNBOUNDED_HIGH_FACTOR: float = 2.0

#: The four kinds of Freqtrade parameter this module can produce.
_Kind = Literal["bool", "categorical", "float", "int"]

#: The message raised when the optional ``freqtrade`` extra is missing.
_FREQTRADE_MISSING = (
    "freqtrade is not installed: install the optional extra with "
    "pip install 'trading-platform[freqtrade]'"
)


@dataclass(frozen=True)
class FreqtradeParamSpec:
    """Inert, Freqtrade-free description of one hyperoptable parameter.

    Instances are produced by :func:`freqtrade_param_specs` and consumed by
    :func:`freqtrade_parameter_attributes`.  They are frozen so that a spec can
    be shared (and asserted on) without fearing an accidental mutation.

    Attributes
    ----------
    name:
        Field name of the house ``ParamsModel``; the key of the resulting
        Freqtrade class attribute.
    kind:
        ``"bool"``, ``"int"``, ``"float"`` or ``"categorical"``.
    default:
        Default value, taken from the validated house parameters.
    low, high:
        Inclusive bounds of a numeric parameter (``None`` for the other kinds).
    decimals:
        Number of decimals of a ``float`` parameter (``None`` for the other kinds).
    categories:
        Allowed values of a ``categorical`` parameter (``None`` otherwise), in
        the order declared by the house grid.
    space:
        Hyperopt space; always :data:`FREQTRADE_PARAM_SPACE` (``"buy"``), because
        the house strategies express both directions through their own columns.
    """

    name: str
    kind: _Kind
    default: Any
    low: int | float | None = None
    high: int | float | None = None
    decimals: int | None = None
    categories: tuple[Any, ...] | None = None
    space: str = FREQTRADE_PARAM_SPACE


# ---------------------------------------------------------------------------
# internal helpers -- target resolution
# ---------------------------------------------------------------------------


def _resolve_target(
    target: type[StrategyParams] | Strategy | type[Strategy],
) -> tuple[type[StrategyParams], StrategyParams]:
    """Return the ``(ParamsModel, validated defaults)`` pair behind ``target``.

    Raises
    ------
    StrategyError
        If ``target`` is neither a :class:`StrategyParams` subclass, a
        :class:`Strategy` subclass, nor a :class:`Strategy` instance.
    """
    if isinstance(target, Strategy):
        model = type(target).ParamsModel
        return model, target.params
    if isinstance(target, type) and issubclass(target, Strategy):
        model = target.ParamsModel
        return model, model()
    if isinstance(target, type) and issubclass(target, StrategyParams):
        return target, target()
    raise StrategyError(
        "freqtrade_param_specs expects a StrategyParams subclass, a Strategy subclass "
        f"or a Strategy instance, got {type(target).__name__}"
    )


# ---------------------------------------------------------------------------
# internal helpers -- constraint reading (duck-typed, no annotated_types import)
# ---------------------------------------------------------------------------


def _bounds(info: FieldInfo) -> tuple[Any, Any, Any, Any]:
    """Return ``(ge, gt, le, lt)`` of a field, read without importing pydantic internals.

    Every constraint object of ``FieldInfo.metadata`` is duck-typed: ``ge`` /
    ``gt`` / ``le`` / ``lt`` are read with :func:`getattr` and a missing
    attribute leaves the accumulator unchanged.  ``annotated_types`` is *not*
    imported on purpose (see the module docstring).
    """
    ge: Any = None
    gt: Any = None
    le: Any = None
    lt: Any = None
    for meta in info.metadata:
        duck = cast("Any", meta)
        ge = getattr(duck, "ge", ge)
        gt = getattr(duck, "gt", gt)
        le = getattr(duck, "le", le)
        lt = getattr(duck, "lt", lt)
    return ge, gt, le, lt


def _annotation_kind(annotation: Any) -> Literal["bool", "float", "int"] | None:
    """Return the Freqtrade-scalar kind of ``annotation``, or ``None`` if unsupported.

    ``bool`` is a subclass of ``int`` in Python, so it is tested first; every
    other annotation (``str``, ``Enum``, ``datetime``, ``Sequence``, unions, …)
    is unsupported and therefore not mapped.
    """
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    return None


# ---------------------------------------------------------------------------
# internal helpers -- guards
# ---------------------------------------------------------------------------


def _guard_int(name: str, default: int, low: int, high: int) -> FreqtradeParamSpec:
    """Return an ``int`` spec clamped so that ``low <= default < high`` holds."""
    if low > default:
        low = default
    if high < default:
        high = default
    if low >= high:
        high = low + 1
    return FreqtradeParamSpec(name=name, kind="int", default=default, low=low, high=high)


def _guard_float(
    name: str, default: float, low: float, high: float, decimals: int
) -> FreqtradeParamSpec:
    """Return a ``float`` spec clamped so that ``low <= default <= high`` and ``low < high``."""
    epsilon = 10.0**-decimals
    if low > default:
        low = default
    if high < default:
        high = default
    if low >= high:
        widened = round(low + epsilon, decimals)
        high = widened if widened > low else math.nextafter(low, math.inf)
    return FreqtradeParamSpec(
        name=name, kind="float", default=default, low=low, high=high, decimals=decimals
    )


def _guarded(spec: FreqtradeParamSpec) -> FreqtradeParamSpec:
    """Apply the numeric guard to a hand-built spec; other kinds are returned as-is.

    This makes the guarantee *"the produced spec always satisfies
    ``low <= default <= high``"* unconditional: it covers the automatic pass,
    the overridden specs and the specs passed straight to
    :func:`freqtrade_parameter_attributes`.  A missing bound is filled with
    the same "unbounded" formula the automatic pass uses.
    """
    if spec.kind == "int":
        int_default = int(spec.default)
        int_low = int(spec.low) if spec.low is not None else 0
        int_high = (
            int(spec.high)
            if spec.high is not None
            else max(int_low + 1, int(int_default * FREQTRADE_UNBOUNDED_HIGH_FACTOR))
        )
        return _guard_int(spec.name, int_default, int_low, int_high)
    if spec.kind == "float":
        decimals = spec.decimals if spec.decimals is not None else FREQTRADE_DECIMALS
        float_default = float(spec.default)
        float_low = float(spec.low) if spec.low is not None else 0.0
        float_high = (
            float(spec.high)
            if spec.high is not None
            else round(float_default * FREQTRADE_UNBOUNDED_HIGH_FACTOR, decimals)
        )
        return _guard_float(spec.name, float_default, float_low, float_high, decimals)
    return spec


# ---------------------------------------------------------------------------
# internal helpers -- per-kind specs
# ---------------------------------------------------------------------------


def _int_spec(name: str, default: Any, info: FieldInfo) -> FreqtradeParamSpec:
    """Build the ``int`` spec of ``name`` from its pydantic constraints."""
    ge, gt, le, lt = _bounds(info)
    value = int(default)
    if ge is not None:
        low = int(ge)
    elif gt is not None:
        low = int(gt) + 1
    else:
        low = 0
    if le is not None:
        high = int(le)
    elif lt is not None:
        high = int(lt) - 1
    else:
        high = max(low + 1, int(value * FREQTRADE_UNBOUNDED_HIGH_FACTOR))
    return _guard_int(name, value, low, high)


def _float_spec(name: str, default: Any, info: FieldInfo) -> FreqtradeParamSpec:
    """Build the ``float`` spec of ``name`` from its pydantic constraints."""
    ge, gt, le, lt = _bounds(info)
    decimals = FREQTRADE_DECIMALS
    epsilon = 10.0**-decimals
    value = float(default)
    if ge is not None:
        low = float(ge)
    elif gt is not None:
        low = float(gt) + epsilon
    else:
        low = 0.0
    if le is not None:
        high = float(le)
    elif lt is not None:
        high = float(lt) - epsilon
    else:
        high = round(value * FREQTRADE_UNBOUNDED_HIGH_FACTOR, decimals)
    return _guard_float(name, value, low, high, decimals)


def _categorical_categories(
    name: str,
    default: Any,
    grid: Mapping[str, Sequence[float | int]] | None,
) -> tuple[Any, ...] | None:
    """Return the categorical candidates of ``name``, or ``None`` if it is not categorical.

    The grid wins only when it declares **at least two** candidates (Freqtrade
    raises ``OperationalException`` below two) *and* the default is one of them,
    compared with its exact type so that a boolean field is never turned into a
    categorical by an ``[0, 1]`` grid.
    """
    if grid is None:
        return None
    candidates = grid.get(name)
    if candidates is None:
        return None
    values = tuple(candidates)
    if len(values) < 2:
        return None
    if not any(candidate == default and type(candidate) is type(default) for candidate in values):
        return None
    return values


def _apply_overrides(
    specs: Sequence[FreqtradeParamSpec],
    overrides: Mapping[str, FreqtradeParamSpec] | None,
) -> tuple[FreqtradeParamSpec, ...]:
    """Replace/add ``overrides`` by name, then guard every spec of the result."""
    ordered = list(specs)
    if overrides:
        positions = {spec.name: index for index, spec in enumerate(ordered)}
        for name, override in overrides.items():
            if name in positions:
                ordered[positions[name]] = override
            else:
                positions[name] = len(ordered)
                ordered.append(override)
    return tuple(_guarded(spec) for spec in ordered)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def freqtrade_param_specs(
    params_model: type[StrategyParams] | Strategy | type[Strategy],
    *,
    grid: Mapping[str, Sequence[float | int]] | None = None,
    overrides: Mapping[str, FreqtradeParamSpec] | None = None,
) -> tuple[FreqtradeParamSpec, ...]:
    """Translate a house parameter model into Freqtrade-free parameter specs.

    Parameters
    ----------
    params_model:
        The pydantic model of a strategy (``BasicStrategyParams``), a
        :class:`~trading_platform.strategy.base.Strategy` subclass, or a
        :class:`~trading_platform.strategy.base.Strategy` **instance** — in which
        case the validated values of that instance become the defaults.
    grid:
        Optional hyperopt grid, typically ``strategy.param_space()``.  A field
        declared there with two candidates or more *and* whose default is one of
        them becomes ``categorical``; every other field keeps its numeric kind.
        The grid is never inferred from ``PARAM_SPACE``: pass it explicitly.
    overrides:
        Manual mapping applied after the automatic pass (replaces by name,
        otherwise appends).  See the module docstring.

    Returns
    -------
    tuple[FreqtradeParamSpec, ...]
        One spec per mapped field, in ``ParamsModel.model_fields`` declaration
        order, then the added overrides in their own order.  Fields with an
        unsupported annotation are omitted (limit (iv)).

    Raises
    ------
    StrategyError
        If ``params_model`` is not a model class, a strategy class or a strategy
        instance.

    Examples
    --------
    >>> from trading_platform.strategy.basic import BasicStrategyParams
    >>> [spec.name for spec in freqtrade_param_specs(BasicStrategyParams)]
    ['ema_fast', 'ema_slow', 'rsi_period', 'rsi_min', 'rsi_max', 'atr_period', 'atr_stop_multiplier', 'allow_short']
    >>> freqtrade_param_specs(BasicStrategyParams)[0]
    FreqtradeParamSpec(name='ema_fast', kind='int', default=9, low=1, high=18, decimals=None, categories=None, space='buy')
    >>> freqtrade_param_specs(BasicStrategyParams, grid={"ema_fast": [5, 9, 13]})[0].kind
    'categorical'
    """
    model, defaults = _resolve_target(params_model)
    specs: list[FreqtradeParamSpec] = []
    for name, info in model.model_fields.items():
        kind = _annotation_kind(info.annotation)
        if kind is None:
            continue
        default = getattr(defaults, name)
        categories = _categorical_categories(name, default, grid)
        if categories is not None:
            specs.append(
                FreqtradeParamSpec(
                    name=name, kind="categorical", default=default, categories=categories
                )
            )
        elif kind == "bool":
            specs.append(FreqtradeParamSpec(name=name, kind="bool", default=bool(default)))
        elif kind == "int":
            specs.append(_int_spec(name, default, info))
        else:
            specs.append(_float_spec(name, default, info))
    return _apply_overrides(specs, overrides)


def freqtrade_parameter_attributes(specs: Sequence[FreqtradeParamSpec]) -> dict[str, Any]:
    """Build the real Freqtrade parameter objects described by ``specs``.

    The result maps each parameter name to a ``IntParameter``,
    ``DecimalParameter``, ``BooleanParameter`` or ``CategoricalParameter``
    instance, ready to be set as a **class attribute** of a generated
    ``IStrategy`` subclass (see
    :mod:`trading_platform.strategy.freqtrade_adapter`).  Insertion order
    follows ``specs``.

    ``freqtrade`` is imported **inside this function** — never at module level —
    so the optional extra stays optional.

    Raises
    ------
    StrategyError
        If ``freqtrade`` is not installed.  The message names the optional extra
        to install; ``ImportError`` is never leaked to the caller.
    """
    try:
        from freqtrade.strategy import (
            BooleanParameter,
            CategoricalParameter,
            DecimalParameter,
            IntParameter,
        )
    except ImportError as error:
        raise StrategyError(_FREQTRADE_MISSING) from error

    attributes: dict[str, Any] = {}
    for raw in specs:
        spec = _guarded(raw)
        if spec.kind == "int":
            attributes[spec.name] = IntParameter(
                low=int(cast("int", spec.low)),
                high=int(cast("int", spec.high)),
                default=int(spec.default),
                space=spec.space,
            )
        elif spec.kind == "float":
            attributes[spec.name] = DecimalParameter(
                low=float(cast("float", spec.low)),
                high=float(cast("float", spec.high)),
                default=float(spec.default),
                decimals=spec.decimals if spec.decimals is not None else FREQTRADE_DECIMALS,
                space=spec.space,
            )
        elif spec.kind == "bool":
            attributes[spec.name] = BooleanParameter(default=bool(spec.default), space=spec.space)
        else:
            attributes[spec.name] = CategoricalParameter(
                categories=list(spec.categories or ()),
                default=spec.default,
                space=spec.space,
            )
    return attributes


def startup_candle_count_for(
    params_model: type[StrategyParams] | Strategy | type[Strategy],
) -> int:
    """Return the ``startup_candle_count`` implied by a parameter model.

    Freqtrade warms its indicators up over ``startup_candle_count`` candles; for
    the house strategies the longest look-back *is* the largest integer
    parameter (``ema_slow`` for ``basic``, hence ``21``), so the value is the
    maximum of the ``int`` fields of the model — booleans excluded, since
    ``bool`` is a subclass of ``int``.

    Returns ``0`` when the model declares no integer field (an empty model needs
    no warm-up at all).

    Raises
    ------
    StrategyError
        If ``params_model`` is not a model class, a strategy class or a strategy
        instance.
    """
    return max(
        (int(spec.default) for spec in freqtrade_param_specs(params_model) if spec.kind == "int"),
        default=0,
    )
