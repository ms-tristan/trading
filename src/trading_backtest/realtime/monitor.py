"""Read model of the realtime layer (delivery brief D6 and D10).

The monitor is the **only** place that turns the persisted state of a profile
back into the domain objects the rest of the framework already understands:

* :meth:`Monitor.build_result` rebuilds a
  :class:`~trading_backtest.core.models.BacktestResult` from the SQLite rows
  alone (equity curve, round-trips, profile header), so a live profile can be
  reported by the very same metrics layer as a backtest;
* :meth:`Monitor.metrics` **delegates** to
  :func:`trading_backtest.metrics.compute_metrics` -- not a single formula is
  recomputed here (D6);
* :meth:`Monitor.benchmark` delegates to
  :func:`trading_backtest.metrics.compare_benchmark` (or, for the
  ``random_entry`` variant, to
  :func:`trading_backtest.validation.random_entry.validate_random_entry`);
* :meth:`Monitor.gate` delegates to
  :func:`trading_backtest.validation.benchmark.validate_benchmark`.

Both validation modules are imported **inside the method body** that needs them,
which keeps the module importable with the dev extra only and preserves the
layer direction (validation sits above metrics, metrics above core).

Honest limitation -- the benchmark block is ``None`` until the caller supplies an
OHLCV frame
------------------------------------------------------------------------------
The state store deliberately persists no candles: it keeps orders, fills,
positions, equity points, round-trips and profile status, nothing else.  A
benchmark curve, on the other hand, needs prices.  :meth:`Monitor.benchmark`
therefore answers ``None`` when no frame is passed (the HTTP contract renders it
as ``"benchmark": null``) and only builds the comparison when the caller -- the
CLI, the orchestrator or a test -- hands it the OHLCV frame of the profile.  That
is a documented consequence, not a bug, and it is one of the limitations listed
in ``docs/realtime.md``.

Failure surface
---------------
Every persistence failure is converted at this boundary into
:class:`~trading_backtest.core.errors.MonitoringError`, naming the profile, so
the web layer never has to know about ``sqlite3`` or about the store's own error
branch.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from trading_backtest.config.models import ProfileConfig, RealtimeConfig
from trading_backtest.core.constants import (
    DEFAULT_FEE_RATE,
    DEFAULT_INITIAL_BALANCE,
    DEFAULT_SLIPPAGE,
    DEFAULT_TIMEFRAME,
    OHLCV_INDEX_NAME,
    UTC,
)
from trading_backtest.core.errors import MonitoringError, RealtimeError
from trading_backtest.core.models import BacktestResult, TradeRecord
from trading_backtest.metrics import compare_benchmark, compute_metrics
from trading_backtest.realtime.clock import Clock
from trading_backtest.realtime.models import (
    EngineCounters,
    EquityPoint,
    Order,
    Position,
    ProfileHealth,
    ProfileState,
    ProfileStatus,
    RunMode,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from trading_backtest.metrics import BenchmarkVariant
    from trading_backtest.realtime.store import StateStore

__all__ = ["Monitor", "ProfileReport"]

#: Default benchmark variant, mirroring :data:`trading_backtest.metrics.DEFAULT_BENCHMARK_VARIANT`.
_DEFAULT_BENCHMARK_VARIANT: str = "buy_and_hold"

#: Maximum number of orders :meth:`Monitor.orders` returns when not told otherwise.
_DEFAULT_ORDER_LIMIT: int = 100


# ---------------------------------------------------------------------------
# JSON hygiene helpers
# ---------------------------------------------------------------------------


def _finite_or_none(value: float | None) -> float | None:
    """Return ``value`` as a finite ``float``, or ``None``.

    ``None``, ``NaN`` and both infinities collapse to ``None``: no payload built
    by this module can ever carry a number ``json.dumps`` would refuse (the web
    contract forbids ``NaN``/``Inf`` on the wire).
    """
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _sanitise(value: Any) -> Any:
    """Recursively replace every non-finite float of ``value`` with ``None``.

    Scalars, mappings and sequences coming from another module (the metrics
    layer, the validation layer) pass through untouched whenever they are already
    finite, so a payload this module returns is equal to the payload it was built
    from -- only the non-JSON-safe numbers are rewritten.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _finite_or_none(value)
    if isinstance(value, Mapping):
        return {str(key): _sanitise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitise(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# the read model payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileReport:
    """Everything the monitoring surface knows about one profile.

    The report is the read-model counterpart of
    :class:`~trading_backtest.realtime.models.ProfileSnapshot`: the snapshot is
    the *cheap* view the orchestrator keeps in memory for a 2-second poll, the
    report is the *complete* view rebuilt from the persisted state (metrics,
    benchmark, full equity curve, trades, positions, health).
    """

    profile_id: str
    symbol: str
    timeframe: str
    strategy: str
    mode: RunMode
    status: ProfileStatus
    initial_balance: float
    final_balance: float
    total_return: float
    n_trades: int
    metrics: dict[str, float]
    benchmark: dict[str, Any] | None
    equity: tuple[EquityPoint, ...]
    trades: tuple[TradeRecord, ...]
    positions: tuple[Position, ...]
    health: ProfileHealth
    generated_at: pd.Timestamp

    def to_dict(self) -> dict[str, Any]:
        """Return the fully JSON-safe payload of the report.

        Every float is either finite or ``None`` (see :func:`_finite_or_none`),
        every timestamp is an ISO-8601 UTC string and the equity curve is a flat
        list of ``{"timestamp", "equity", "cash", "position_value"}`` objects, so
        the payload can be written to the wire as-is.
        """
        return {
            "profile_id": str(self.profile_id),
            "symbol": str(self.symbol),
            "timeframe": str(self.timeframe),
            "strategy": str(self.strategy),
            "mode": RunMode(self.mode).value,
            "status": ProfileStatus(self.status).value,
            "initial_balance": _finite_or_none(self.initial_balance),
            "final_balance": _finite_or_none(self.final_balance),
            "total_return": _finite_or_none(self.total_return),
            "n_trades": int(self.n_trades),
            "metrics": _sanitise(dict(self.metrics)),
            "benchmark": None if self.benchmark is None else _sanitise(self.benchmark),
            "equity": [
                {
                    "timestamp": pd.Timestamp(point.timestamp).isoformat(),
                    "equity": _finite_or_none(point.equity),
                    "cash": _finite_or_none(point.cash),
                    "position_value": _finite_or_none(point.position_value),
                }
                for point in self.equity
            ],
            "trades": [trade.to_dict() for trade in self.trades],
            "positions": [position.to_dict() for position in self.positions],
            "health": self.health.to_dict(),
            "generated_at": pd.Timestamp(self.generated_at).isoformat(),
        }


@dataclass(frozen=True)
class _Persisted:
    """One consistent read of a profile's persisted state."""

    profile: ProfileConfig | None
    equity: tuple[EquityPoint, ...]
    trades: tuple[TradeRecord, ...]
    positions: tuple[Position, ...]
    state: ProfileState


# ---------------------------------------------------------------------------
# series / window helpers
# ---------------------------------------------------------------------------


def _equity_series(points: Sequence[EquityPoint]) -> pd.Series:
    """Return the equity curve of ``points`` as a ``float64`` :class:`pandas.Series`.

    The series is indexed by a tz-aware UTC :class:`pandas.DatetimeIndex` named
    ``"timestamp"`` and named ``"equity"``, exactly like the curve of a
    :class:`~trading_backtest.core.models.BacktestResult`.  An empty input yields
    an empty ``float64`` series with a UTC index -- never an exception.
    """
    if not points:
        return pd.Series(
            dtype="float64",
            index=pd.DatetimeIndex([], tz=UTC, name=OHLCV_INDEX_NAME),
            name="equity",
        )
    index = pd.DatetimeIndex(
        [pd.Timestamp(point.timestamp) for point in points], name=OHLCV_INDEX_NAME
    )
    index = index.tz_localize(UTC) if index.tz is None else index.tz_convert(UTC)
    return pd.Series(
        [float(point.equity) for point in points],
        index=index,
        dtype="float64",
        name="equity",
    )


def _window(
    points: Sequence[EquityPoint],
    trades: Sequence[TradeRecord],
    clock: Clock,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return the ``(start, end)`` window covered by the persisted state.

    The equity curve wins when it exists; a profile that only traded (its curve
    was pruned, or it was never sampled) is covered by the earliest entry and the
    latest exit; a profile with nothing at all is dated at *now* by the injected
    clock, so even an empty report carries a deterministic, testable window.
    """
    if points:
        return pd.Timestamp(points[0].timestamp), pd.Timestamp(points[-1].timestamp)
    if trades:
        entries = [pd.Timestamp(trade.entry_time) for trade in trades]
        exits = [pd.Timestamp(trade.exit_time) for trade in trades]
        return min(entries), max(exits)
    moment = pd.Timestamp(clock.now())
    return moment, moment


def _resolve_benchmark_variant(explicit: str, config: RealtimeConfig | None) -> str:
    """Return the effective benchmark variant of this monitor.

    An explicitly provided argument wins; the injected
    :class:`~trading_backtest.config.models.RealtimeConfig` only *seeds* the
    default, i.e. it is honoured when the caller left the argument at its
    documented default (``"buy_and_hold"``).
    """
    if config is not None and str(explicit) == _DEFAULT_BENCHMARK_VARIANT:
        return str(config.benchmark_variant)
    return str(explicit)


def _resolve_risk_free_rate(explicit: float, config: RealtimeConfig | None) -> float:
    """Return the effective annual risk-free rate of this monitor.

    Same precedence rule as :func:`_resolve_benchmark_variant`: the explicit
    argument wins, the injected configuration seeds the default.
    """
    if config is not None and float(explicit) == 0.0:
        return float(config.risk_free_rate)
    return float(explicit)


# ---------------------------------------------------------------------------
# the monitor
# ---------------------------------------------------------------------------


class Monitor:
    """Read model built on top of an injected :class:`StateStore`.

    The monitor posts nothing, knows nothing about the engine and only ever
    *reads*: it is what the CLI, the HTTP server and the tests use to render a
    profile after the fact (or after a restart).

    Parameters
    ----------
    store:
        Durable state seam (``trading_backtest.realtime.store.StateStore``).
        Only the read methods are exercised.
    clock:
        Time seam; it dates the reports (:attr:`ProfileReport.generated_at`) and
        the window of an empty profile.  Tests inject a ``ManualClock``.
    realtime:
        Optional engine configuration.  It seeds the two ``benchmark_variant`` /
        ``risk_free_rate`` defaults so the CLI can build a monitor straight from
        a configuration file; an explicitly provided argument always wins.
    benchmark_variant:
        Benchmark used by :meth:`benchmark` and :meth:`gate`.  ``"none"``
        disables both.
    risk_free_rate:
        **Annual** risk-free rate as a fraction (``0.05`` = 5 %/year), forwarded
        untouched to the metrics and validation layers.
    fee_rate, slippage:
        Costs applied to the benchmark side of the comparison, identical to the
        backtest engine's.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        clock: Clock,
        realtime: RealtimeConfig | None = None,
        benchmark_variant: str = _DEFAULT_BENCHMARK_VARIANT,
        risk_free_rate: float = 0.0,
        fee_rate: float = DEFAULT_FEE_RATE,
        slippage: float = DEFAULT_SLIPPAGE,
    ) -> None:
        self._store = store
        self._clock = clock
        self._realtime = realtime
        self._benchmark_variant = _resolve_benchmark_variant(benchmark_variant, realtime)
        self._risk_free_rate = _resolve_risk_free_rate(risk_free_rate, realtime)
        self._fee_rate = float(fee_rate)
        self._slippage = float(slippage)

    # -- introspection ------------------------------------------------------

    @property
    def benchmark_variant(self) -> str:
        """Effective benchmark variant of this monitor."""
        return self._benchmark_variant

    @property
    def risk_free_rate(self) -> float:
        """Effective annual risk-free rate of this monitor."""
        return self._risk_free_rate

    def __repr__(self) -> str:
        """Return a short, secret-free representation of the read model."""
        return (
            f"Monitor(benchmark_variant={self._benchmark_variant!r}, "
            f"risk_free_rate={self._risk_free_rate!r}, "
            f"fee_rate={self._fee_rate!r}, slippage={self._slippage!r})"
        )

    # -- error boundary -----------------------------------------------------

    @contextmanager
    def _guard(self, profile_id: str) -> Iterator[None]:
        """Convert a persistence failure into a :class:`MonitoringError`.

        A :class:`MonitoringError` raised deeper in the call stack travels
        untouched (it is already the right error for this boundary).
        """
        try:
            yield
        except MonitoringError:
            raise
        except RealtimeError as exc:
            raise MonitoringError(
                f"cannot build the read model of profile {profile_id!r}: {exc}"
            ) from exc

    # -- persisted state ----------------------------------------------------

    def _profile(self, profile_id: str) -> ProfileConfig | None:
        """Return the persisted configuration of ``profile_id``, or ``None``."""
        for spec in self._store.load_profiles():
            if str(spec.id) == profile_id:
                return spec
        return None

    def _read(self, profile_id: str) -> _Persisted:
        """Read every persisted slice of ``profile_id`` used by the read model."""
        return _Persisted(
            profile=self._profile(profile_id),
            equity=tuple(self._store.equity_curve(profile_id)),
            trades=tuple(self._store.list_trades(profile_id)),
            positions=tuple(self._store.list_positions(profile_id)),
            state=self._store.profile_state(profile_id),
        )

    # -- profile header -----------------------------------------------------

    def initial_balance(self, profile_id: str) -> float:
        """Return the initial balance of a profile.

        The value comes from the persisted
        :class:`~trading_backtest.config.models.ProfileConfig` when the profile
        was ever saved and falls back to
        :data:`~trading_backtest.core.constants.DEFAULT_INITIAL_BALANCE`
        otherwise.
        """
        with self._guard(profile_id):
            spec = self._profile(profile_id)
        if spec is None:
            return float(DEFAULT_INITIAL_BALANCE)
        return float(spec.initial_balance)

    def _symbol(self, profile_id: str, data: _Persisted) -> str:
        """Return the symbol of a profile, falling back to its first position."""
        if data.profile is not None:
            return str(data.profile.symbol)
        for position in data.positions:
            return str(position.symbol)
        return ""

    def _mode(self, data: _Persisted) -> RunMode:
        """Return the run mode recorded by the configuration, else the persisted one."""
        if data.profile is not None:
            return RunMode(str(data.profile.mode))
        return RunMode(data.state.mode)

    # -- backtest result ----------------------------------------------------

    def build_result(self, profile_id: str) -> BacktestResult:
        """Rebuild a :class:`BacktestResult` from the persisted state only.

        The produced result is what the metrics, benchmark and validation layers
        consume, which is what makes a live profile comparable with a backtest
        run of the same strategy: same initial capital, same trades, same equity
        curve.  An empty profile yields an empty ``float64`` curve with a UTC
        :class:`pandas.DatetimeIndex` (never a crash) and a window dated by the
        injected clock.

        Raises
        ------
        MonitoringError
            If the persistent state cannot be read.
        """
        with self._guard(profile_id):
            data = self._read(profile_id)
        return self._result_from(profile_id, data)

    def _result_from(self, profile_id: str, data: _Persisted) -> BacktestResult:
        """Build the :class:`BacktestResult` of an already-read profile."""
        spec = data.profile
        balance = float(DEFAULT_INITIAL_BALANCE if spec is None else spec.initial_balance)
        curve = _equity_series(data.equity)
        final_balance = float(curve.iloc[-1]) if len(curve) else balance
        if not math.isfinite(final_balance):
            # ``to_dict()`` renders a non-finite number as ``None``; the scalar
            # carried by the result itself falls back to the initial capital so
            # no downstream formula is fed a NaN.
            final_balance = balance
        start, end = _window(data.equity, data.trades, self._clock)
        timeframe = DEFAULT_TIMEFRAME if spec is None else str(spec.timeframe)
        return BacktestResult(
            strategy_name="" if spec is None else str(spec.strategy),
            symbol=self._symbol(profile_id, data),
            timeframe=timeframe,
            start=start,
            end=end,
            initial_balance=balance,
            final_balance=final_balance,
            trades=list(data.trades),
            equity_curve=curve,
            params={} if spec is None else dict(spec.params),
            metadata={"source": "realtime", "profile_id": str(profile_id)},
        )

    # -- metrics, benchmark and gate ---------------------------------------

    def metrics(self, profile_id: str) -> dict[str, float]:
        """Return the frozen metric set of a profile.

        The numbers are **exactly** what
        :func:`trading_backtest.metrics.compute_metrics` returns for the rebuilt
        result with the profile's timeframe and this monitor's risk-free rate:
        the metrics layer owns every formula (D6), nothing is recomputed here.
        """
        result = self.build_result(profile_id)
        return self._metrics_of(result)

    def _metrics_of(self, result: BacktestResult) -> dict[str, float]:
        """Return ``compute_metrics(result, ...).as_dict()`` verbatim."""
        metric_set = compute_metrics(
            result,
            timeframe=str(result.timeframe),
            risk_free_rate=self._risk_free_rate,
        )
        return dict(metric_set.as_dict())

    def benchmark(
        self, profile_id: str, ohlcv: pd.DataFrame | None = None
    ) -> dict[str, Any] | None:
        """Return the benchmark comparison of a profile, or ``None``.

        ``None`` is returned when no OHLCV frame is supplied, when the frame has
        fewer than two rows, and when the configured variant is ``"none"``.  The
        ``"random_entry"`` variant has no curve: it is answered by
        :func:`trading_backtest.validation.random_entry.validate_random_entry`,
        imported inside this method body so the module stays importable without
        it.

        Raises
        ------
        MetricsError
            Propagated from the metrics layer for an unknown variant.
        MonitoringError
            If the persistent state cannot be read.
        """
        if ohlcv is None or len(ohlcv) < 2:
            return None
        if self._benchmark_variant == "none":
            return None
        result = self.build_result(profile_id)
        return self._benchmark_of(result, ohlcv)

    def _benchmark_of(
        self, result: BacktestResult, ohlcv: pd.DataFrame | None
    ) -> dict[str, Any] | None:
        """Build the benchmark block of an already-rebuilt result."""
        if ohlcv is None or len(ohlcv) < 2 or self._benchmark_variant == "none":
            return None
        if self._benchmark_variant == "random_entry":
            # Lazy on purpose: the validation layer sits above the metrics layer.
            from trading_backtest.validation.random_entry import validate_random_entry

            distribution = validate_random_entry(
                result,
                ohlcv,
                fee_rate=self._fee_rate,
                slippage=self._slippage,
            )
            return dict(distribution.to_dict())
        comparison = compare_benchmark(
            result,
            ohlcv,
            variant=cast("BenchmarkVariant", self._benchmark_variant),
            initial_balance=result.initial_balance,
            fee_rate=self._fee_rate,
            slippage=self._slippage,
            risk_free_rate=self._risk_free_rate,
        )
        return None if comparison is None else dict(comparison.to_dict())

    def gate(self, profile_id: str, ohlcv: pd.DataFrame) -> dict[str, Any] | None:
        """Return the benchmark gate verdict of a profile, or ``None``.

        The verdict is the payload of
        :func:`trading_backtest.validation.benchmark.validate_benchmark`
        (imported inside this method body); ``None`` means the benchmark is
        disabled (variant ``"none"``) or produced no comparison.

        Raises
        ------
        MonitoringError
            If the persistent state cannot be read.
        ValidationLayerError
            Propagated from the validation layer.
        """
        # Lazy on purpose: the validation layer sits above the metrics layer.
        from trading_backtest.validation.benchmark import validate_benchmark

        result = self.build_result(profile_id)
        verdict = validate_benchmark(
            result,
            ohlcv,
            variant=self._benchmark_variant,
            fee_rate=self._fee_rate,
            slippage=self._slippage,
            risk_free_rate=self._risk_free_rate,
        )
        return None if verdict is None else dict(verdict.to_dict())

    # -- persisted collections ---------------------------------------------

    def equity(self, profile_id: str) -> tuple[EquityPoint, ...]:
        """Return the whole equity curve of a profile, oldest first.

        Raises
        ------
        MonitoringError
            If the persistent state cannot be read.
        """
        with self._guard(profile_id):
            return tuple(self._store.equity_curve(profile_id))

    def trades(self, profile_id: str) -> tuple[TradeRecord, ...]:
        """Return the closed round-trips of a profile, oldest exit first.

        Raises
        ------
        MonitoringError
            If the persistent state cannot be read.
        """
        with self._guard(profile_id):
            return tuple(self._store.list_trades(profile_id))

    def orders(self, profile_id: str, *, limit: int = _DEFAULT_ORDER_LIMIT) -> tuple[Order, ...]:
        """Return the most recent orders of a profile, most recent first.

        Raises
        ------
        MonitoringError
            If the persistent state cannot be read.
        """
        with self._guard(profile_id):
            return tuple(self._store.list_orders(profile_id, limit=limit))

    def positions(self, profile_id: str) -> tuple[Position, ...]:
        """Return the open positions of a profile, ordered by symbol.

        Raises
        ------
        MonitoringError
            If the persistent state cannot be read.
        """
        with self._guard(profile_id):
            return tuple(self._store.list_positions(profile_id))

    def health(self, profile_id: str) -> ProfileHealth:
        """Return the health block of a profile.

        An unknown profile never raises: the store answers its documented stopped
        default, so the dashboard renders a ``stopped`` badge instead of a 500.
        The counters are the in-process values of a *running* engine and are
        therefore empty here -- the read model only sees what was persisted.

        Raises
        ------
        MonitoringError
            If the persistent state cannot be read.
        """
        with self._guard(profile_id):
            state = self._store.profile_state(profile_id)
        return self._health_from(profile_id, state)

    def _health_from(self, profile_id: str, state: ProfileState) -> ProfileHealth:
        """Build the health block of an already-read persisted state."""
        return ProfileHealth(
            profile_id=str(profile_id),
            status=ProfileStatus(state.status),
            last_candle_at=state.last_candle_at,
            lag_seconds=float(state.lag_seconds),
            last_error=state.last_error,
            reconnect_count=int(state.reconnect_count),
            counters=EngineCounters(),
        )

    # -- reports ------------------------------------------------------------

    def report(self, profile_id: str, *, ohlcv: pd.DataFrame | None = None) -> ProfileReport:
        """Return the complete report of one profile.

        Parameters
        ----------
        profile_id:
            Profile to read.  An unknown profile is not an error: it produces an
            empty report with the stopped default health.
        ohlcv:
            Optional OHLCV frame feeding the benchmark block; ``None`` leaves
            ``benchmark`` at ``None``.

        Raises
        ------
        MonitoringError
            If the persistent state cannot be read.
        """
        with self._guard(profile_id):
            data = self._read(profile_id)
        result = self._result_from(profile_id, data)
        metrics = self._metrics_of(result)
        return ProfileReport(
            profile_id=str(profile_id),
            symbol=self._symbol(profile_id, data),
            timeframe=str(result.timeframe),
            strategy=str(result.strategy_name),
            mode=self._mode(data),
            status=ProfileStatus(data.state.status),
            initial_balance=float(result.initial_balance),
            final_balance=float(result.final_balance),
            total_return=float(metrics.get("total_return", 0.0)),
            n_trades=len(data.trades),
            metrics=metrics,
            benchmark=self._benchmark_of(result, ohlcv),
            equity=data.equity,
            trades=data.trades,
            positions=data.positions,
            health=self._health_from(profile_id, data.state),
            generated_at=pd.Timestamp(self._clock.now()),
        )

    def reports(self, *, ohlcv: Mapping[str, pd.DataFrame] | None = None) -> list[ProfileReport]:
        """Return one report per stored profile, sorted by ``profile_id``.

        Parameters
        ----------
        ohlcv:
            Optional mapping of ``profile_id`` to the OHLCV frame used for the
            benchmark block.  A profile absent from the mapping keeps a ``None``
            benchmark.
        """
        frames: Mapping[str, pd.DataFrame] = {} if ohlcv is None else ohlcv
        return [self.report(pid, ohlcv=frames.get(pid)) for pid in self.profile_ids()]

    def profile_ids(self) -> list[str]:
        """Return the identifiers of every persisted profile, sorted.

        Raises
        ------
        MonitoringError
            If the persistent state cannot be read.
        """
        try:
            specs = self._store.load_profiles()
        except MonitoringError:
            raise
        except RealtimeError as exc:
            raise MonitoringError(f"cannot list the profiles of the read model: {exc}") from exc
        return sorted({str(spec.id) for spec in specs})
