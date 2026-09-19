"""Strategy abstraction: typed parameters, OHLCV contract, signal contract.

A strategy is a small, stateless-in-spirit object with two pure methods:

``prepare(data)``
    Validate the OHLCV contract and return a **new** frame carrying the input
    columns plus the strategy's indicator columns (the input is never mutated).

``signals(prepared)``
    Turn a prepared frame into a signal frame indexed exactly like the input and
    carrying **exactly** the columns of
    :data:`~trading_platform.core.constants.SIGNAL_COLUMNS`
    (``entry_long``, ``exit_long``, ``entry_short``, ``exit_short``,
    ``stop_loss``); the four order columns are ``bool`` with no ``NaN`` and
    ``stop_loss`` is ``float64`` with ``NaN`` allowed ("no stop").

Parameters are validated by a pydantic model (``ParamsModel``), which makes
every strategy self-documenting, immutable and JSON-serialisable through
:meth:`pydantic.BaseModel.model_dump` — the engine stores that dump in
``BacktestResult.params``.

Every contract violation is reported as
:class:`~trading_platform.core.errors.StrategyError`.

Both methods stay **pure and I/O-free**.  A strategy that needs an external
input (a pre-computed forecast artifact, for example) receives it through
:meth:`Strategy.set_feature_bundle` — the seam implemented by
:mod:`trading_platform.strategy.features` — and never reads it back from disk
inside ``prepare`` or ``signals``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, ClassVar

import pandas as pd
from pydantic import BaseModel, ConfigDict, ValidationError

from trading_platform.core.constants import REQUIRED_OHLCV_COLUMNS, SIGNAL_COLUMNS
from trading_platform.core.errors import StrategyError

__all__ = [
    "Strategy",
    "StrategyParams",
    "ensure_signal_frame",
    "require_ohlcv_frame",
]

#: The four boolean order columns of a signal frame (every column but ``stop_loss``).
BOOL_SIGNAL_COLUMNS: tuple[str, ...] = tuple(
    column for column in SIGNAL_COLUMNS if column != "stop_loss"
)


# ---------------------------------------------------------------------------
# frame contracts
# ---------------------------------------------------------------------------


def require_ohlcv_frame(data: pd.DataFrame, *, name: str = "data") -> pd.DataFrame:
    """Return a copy of ``data`` that satisfies the OHLCV contract.

    The returned frame is a deep copy whose required columns are ``float64`` and
    whose index is preserved **exactly** as given (it is not re-sampled, sorted
    nor localised).  The input frame is never mutated.

    Raises
    ------
    StrategyError
        If ``data`` is not a :class:`pandas.DataFrame`, is empty, is not indexed
        by a :class:`pandas.DatetimeIndex`, misses one of the
        :data:`~trading_platform.core.constants.REQUIRED_OHLCV_COLUMNS` or holds
        a required column that cannot be cast to ``float64``.
    """
    if not isinstance(data, pd.DataFrame):
        raise StrategyError(f"{name} must be a pandas DataFrame, got {type(data).__name__}")

    issues: list[str] = []
    missing = [column for column in REQUIRED_OHLCV_COLUMNS if column not in data.columns]
    if missing:
        issues.append(f"missing required column(s): {', '.join(missing)}")
    if not isinstance(data.index, pd.DatetimeIndex):
        issues.append(f"index must be a DatetimeIndex, got {type(data.index).__name__}")
    if data.empty:
        issues.append("empty frame: no candle to work with")
    if issues:
        raise StrategyError(f"{name} does not satisfy the OHLCV contract", issues)

    frame = data.copy(deep=True)
    unconvertible: list[str] = []
    for column in REQUIRED_OHLCV_COLUMNS:
        try:
            frame[column] = pd.to_numeric(frame[column], errors="raise").astype("float64")
        except (TypeError, ValueError):
            unconvertible.append(column)
    if unconvertible:
        raise StrategyError(
            f"{name} does not satisfy the OHLCV contract",
            [f"column(s) not convertible to float64: {', '.join(unconvertible)}"],
        )
    return frame


def ensure_signal_frame(signals: pd.DataFrame, index: pd.Index) -> pd.DataFrame:
    """Validate and coerce a signal frame against the frozen signal contract.

    Guarantees on the returned frame (a new object; ``signals`` is not mutated):

    * exactly the :data:`~trading_platform.core.constants.SIGNAL_COLUMNS`, in
      that order, and no duplicate column;
    * indexed exactly like ``index`` (same length, same values);
    * the four order columns are ``bool`` — any missing value is coerced to
      ``False`` (a ``NaN`` comparison is never a signal);
    * ``stop_loss`` is ``float64``, ``NaN`` meaning "no stop".

    Raises
    ------
    StrategyError
        If ``signals`` is not a DataFrame, misses or adds a column, is indexed
        differently from ``index``, or carries a non-numeric ``stop_loss``.
    """
    if not isinstance(signals, pd.DataFrame):
        raise StrategyError(f"signals must be a pandas DataFrame, got {type(signals).__name__}")

    issues: list[str] = []
    duplicated = [column for column in signals.columns if list(signals.columns).count(column) > 1]
    if duplicated:
        issues.append(f"duplicated column(s): {', '.join(sorted(set(duplicated)))}")
    missing = [column for column in SIGNAL_COLUMNS if column not in signals.columns]
    if missing:
        issues.append(f"missing signal column(s): {', '.join(missing)}")
    unexpected = [column for column in signals.columns if column not in SIGNAL_COLUMNS]
    if unexpected:
        issues.append(f"unexpected signal column(s): {', '.join(map(str, unexpected))}")
    if not issues and not signals.index.equals(index):
        issues.append("signals must be indexed exactly like the input data")
    if issues:
        raise StrategyError(f"signals must have exactly the columns {list(SIGNAL_COLUMNS)}", issues)

    frame = pd.DataFrame(index=signals.index)
    for column in BOOL_SIGNAL_COLUMNS:
        values = signals[column]
        frame[column] = values.fillna(False).astype(bool)
    try:
        frame["stop_loss"] = pd.to_numeric(signals["stop_loss"], errors="raise").astype("float64")
    except (TypeError, ValueError):
        raise StrategyError("signal column 'stop_loss' must be numeric") from None
    return frame[list(SIGNAL_COLUMNS)]


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


class StrategyParams(BaseModel):
    """Base class of every strategy parameter model.

    Frozen (immutable) and forbidding unknown keys, so that a typo in a
    configuration file is reported instead of silently ignored.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


def _validation_details(error: ValidationError) -> list[str]:
    """Render one ``field: message`` line per pydantic error."""
    details: list[str] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<model>"
        details.append(f"{location}: {item['msg']}")
    return details


def _error_fields(error: ValidationError) -> str:
    """Comma separated, de-duplicated list of the fields pydantic rejected."""
    fields = sorted({str(item["loc"][0]) for item in error.errors() if item["loc"]})
    return ", ".join(fields) if fields else "<model>"


# ---------------------------------------------------------------------------
# strategy
# ---------------------------------------------------------------------------


class Strategy(ABC):
    """Abstract base class of every strategy of the package.

    Subclasses must set :attr:`name`, :attr:`ParamsModel` and implement
    :meth:`prepare` and :meth:`signals` (and may declare :attr:`PARAM_SPACE` for
    the robustness layer).
    """

    #: Identifier used by the registry, the CLI and ``BacktestResult``.
    name: ClassVar[str] = ""
    #: Pydantic model validating the parameters of this strategy.
    ParamsModel: ClassVar[type[StrategyParams]] = StrategyParams
    #: Optional grid used by the parametric robustness analysis.
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {}

    def __init__(self, params: StrategyParams | Mapping[str, Any] | None = None) -> None:
        """Build a strategy from ``params``.

        Parameters
        ----------
        params:
            ``None`` (use the defaults), a mapping validated through
            :attr:`ParamsModel`, or an instance of :attr:`ParamsModel`.

        Raises
        ------
        StrategyError
            If ``params`` is a mapping that :attr:`ParamsModel` rejects (the
            message names the failing field), if it is a model of the wrong type
            or if it is neither a mapping nor a model.
        """
        if params is None:
            self._params: StrategyParams = self.validate_params(None)
        elif isinstance(params, self.ParamsModel):
            self._params = params
        elif isinstance(params, StrategyParams):
            raise StrategyError(
                f"params must be an instance of {self.ParamsModel.__name__}, "
                f"got {type(params).__name__}"
            )
        elif isinstance(params, Mapping):
            self._params = self.validate_params(params)
        else:
            raise StrategyError(
                f"params must be a mapping or a {self.ParamsModel.__name__} instance, "
                f"got {type(params).__name__}"
            )
        #: External, pre-resolved inputs (see
        #: :func:`trading_platform.strategy.features.attach_features`).  ``None``
        #: means "this strategy has no external input"; the base class never reads
        #: it, so a strategy with no external dependency is unaffected.
        self._feature_bundle: object | None = None

    @property
    def params(self) -> StrategyParams:
        """The validated, immutable parameters of this strategy."""
        return self._params

    def set_feature_bundle(self, features: object | None = None) -> None:
        """Attach the external features of this run (documented no-op hook).

        The base implementation accepts **any** value — including ``None`` — and
        simply stores it: a strategy that consumes no external input (such as
        :class:`~trading_platform.strategy.basic.BasicStrategy`) therefore keeps
        its exact previous behaviour whether or not the engine injects a bundle.
        A strategy that does consume external features overrides this method,
        narrows the argument by validation and reports a wrong type as
        :class:`~trading_platform.core.errors.StrategyError`.

        Parameters
        ----------
        features:
            The resolved features.  ``None`` clears them.
        """
        self._feature_bundle = features

    @property
    def feature_bundle(self) -> object | None:
        """The external features attached to this instance, or ``None``."""
        return self._feature_bundle

    def param_space(self) -> dict[str, list[float | int]]:
        """Return a **copy** of the parameter grid of this strategy."""
        return {key: list(values) for key, values in self.PARAM_SPACE.items()}

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        """Return the default parameters as a plain, mutable ``dict`` copy."""
        return cls.ParamsModel().model_dump()

    @classmethod
    def validate_params(cls, params: Mapping[str, Any] | None) -> StrategyParams:
        """Validate ``params`` through :attr:`ParamsModel`.

        Raises
        ------
        StrategyError
            If ``params`` is not a mapping, or if validation fails; the error
            message contains the name of every failing field.
        """
        if params is None:
            payload: dict[str, Any] = {}
        elif isinstance(params, Mapping):
            payload = dict(params)
        else:
            raise StrategyError(f"params must be a mapping or None, got {type(params).__name__}")
        try:
            return cls.ParamsModel(**payload)
        except ValidationError as error:
            raise StrategyError(
                f"invalid parameters for strategy {cls.name!r}: {_error_fields(error)}",
                _validation_details(error),
            ) from error

    @abstractmethod
    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return a new frame with the OHLCV columns plus the indicator columns.

        The input must satisfy the OHLCV contract (see
        :func:`require_ohlcv_frame`) and is never mutated; the returned frame
        keeps the exact same index.
        """

    @abstractmethod
    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a *prepared* frame.

        The result is indexed exactly like ``data`` and carries exactly the
        :data:`~trading_platform.core.constants.SIGNAL_COLUMNS`.  The method is
        pure: calling it twice on the same input returns the same frame and
        leaves the input untouched.
        """

    def run(self, data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Return ``(prepared, signals)`` for ``data`` — the engine entry point."""
        prepared = self.prepare(data)
        return prepared, self.signals(prepared)
