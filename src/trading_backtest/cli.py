"""Command line orchestration for the backtesting skeleton.

The CLI is the only place that knows about *every* layer: it loads the
configuration, acquires the data, runs the backtest and the statistical
validation, renders the report and prints either a rich summary or a single
machine-readable JSON object.

Import policy (frozen — the argument-validation tests and ``--help`` rely on it):
the module top level only imports the standard library, ``typer``/``rich`` and
the ``core``/``config``/``data`` layers.  ``trading_backtest.strategy``,
``.validation``, ``.metrics`` and ``.reporting`` are imported **inside the
command bodies**, so ``--help``, ``--version``, ``config show`` and
``config validate`` stay fast and never require the other layers to exist.

The ``realtime`` group (layer 8) obeys the very same rule, and it is mechanically
tested (``tests/test_cli_realtime.py``): ``trading_backtest.realtime`` (layer 6)
and ``trading_backtest.web`` (layer 7) are imported **inside the command bodies
and their helpers**, never at module import time.  Importing
``trading_backtest.cli`` therefore leaves neither of the two new layers in
``sys.modules``, which is what keeps ``--help`` free of the engine and of the
optional ``ccxt``/``freqtrade`` extras.  The three commands are:

* ``realtime run`` — starts the engine *and* the monitoring server (``--once``
  runs a single deterministic tick and exits, without starting any server);
* ``realtime serve`` — read-only monitoring over the persisted state, no engine;
* ``realtime check`` — static pre-flight: it validates the profiles document,
  the credential *presence*, the live gate, the risk limits and the writability
  of the state database; it never places an order, never needs the network, and
  exits ``1`` as soon as one profile cannot start.

Their payloads have their own key sets (documented verbatim in
``docs/usage.md`` and ``docs/realtime.md``): ``realtime-check`` carries
``state_db_writable`` plus one entry per profile, while ``realtime-run`` and
``realtime-serve`` carry ``profiles`` (snapshots), ``decisions`` and the
monitoring ``url``.  They travel through :func:`_emit_realtime` instead of
:func:`_payload` for that reason; stdout still carries exactly one JSON object
per invocation, and the startup URL is announced on **stderr** when ``--json``
is used.

Exit codes: ``0`` success, ``1`` domain error (any
:class:`~trading_backtest.core.errors.TradingBacktestError`), ``2`` usage error
(unknown command, missing argument, invalid choice...).

Every command supports ``--json``, which prints exactly one
``json.dumps(payload, indent=2, sort_keys=True, default=str)`` object on stdout
with the keys ``command, ok, symbol, timeframe, config_path, metrics, run,
reports, data_quality``.

The optional benchmark (:data:`_BENCHMARK_HELP`) never adds a payload key: the
gate travels *inside* ``run`` (under ``run["benchmark"]``) and as a dedicated
``Benchmark`` section of the written report, exactly like the other validation
payloads.  ``--benchmark-variant`` (:data:`_BENCHMARK_VARIANT_HELP`) selects
between ``buy_and_hold``, ``cash``, ``risk_free`` and ``random_entry`` -- exactly
one benchmark per run -- and ``--risk-free-rate`` (:data:`_RISK_FREE_HELP`) sets
the annual rate subtracted from the annualised mean return by
``sharpe_ratio``/``sortino_ratio`` (and compounded by the ``risk_free`` curve).
The random-entry skill test travels the very same way, but *inside*
``run["random_entry"]`` plus a ``random_entry`` report section: it is a
distribution, not a curve, so it has its own gate.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import typer
from rich.console import Console
from rich.table import Table

from trading_backtest import __version__
from trading_backtest.config import (
    AppConfig,
    MonitoringConfig,
    ProfileConfig,
    RealtimeConfig,
    default_realtime_config,
    load_config,
    load_monitoring_config,
    load_profiles,
    load_realtime_config,
)
from trading_backtest.core.constants import OHLCV_INDEX_NAME, UTC
from trading_backtest.core.errors import (
    ConfigError,
    FreqtradeConfigError,
    InsufficientDataError,
    MarketStreamError,
    MonitoringError,
    TradingBacktestError,
)
from trading_backtest.core.models import BacktestResult, RunnerFn
from trading_backtest.data import (
    DataQualityReport,
    OHLCVLoader,
    ensure_ohlcv,
    validate_ohlcv,
)
from trading_backtest.freqtrade.config import validate_freqtrade_config

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    import pandas as pd

    from trading_backtest.metrics import MetricSet
    from trading_backtest.validation import (
        BenchmarkGateResult,
        RandomEntryGateResult,
    )

__all__ = ["app", "config_app", "data_app", "main", "realtime_app"]

#: Name used in usage/help messages (the console script of ``pyproject.toml``).
PROG_NAME = "trading-backtest"

#: Symbol used when neither ``--symbol`` nor the data file name provides one.
DEFAULT_SYMBOL = "UNKNOWN/USDT"

#: Fallback JSON payload keys, in the documented order.
PAYLOAD_KEYS: tuple[str, ...] = (
    "command",
    "ok",
    "symbol",
    "timeframe",
    "config_path",
    "metrics",
    "run",
    "reports",
    "data_quality",
)

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# option types
# ---------------------------------------------------------------------------


class WindowMode(StrEnum):
    """Walk-forward window scheme (mirrors ``validation.WINDOW_MODES``)."""

    rolling = "rolling"
    anchored = "anchored"


class MonteCarloMethod(StrEnum):
    """Monte Carlo resampling scheme (mirrors ``validation.METHODS``)."""

    trade_resample = "trade_resample"
    bootstrap_equity = "bootstrap_equity"


#: Enum -> frozen validation-layer literal (keeps the CLI and the layer in sync).
WINDOW_MODES: dict[WindowMode, Literal["rolling", "anchored"]] = {
    WindowMode.rolling: "rolling",
    WindowMode.anchored: "anchored",
}

#: Enum -> frozen validation-layer literal (keeps the CLI and the layer in sync).
MONTE_CARLO_METHODS: dict[MonteCarloMethod, Literal["trade_resample", "bootstrap_equity"]] = {
    MonteCarloMethod.trade_resample: "trade_resample",
    MonteCarloMethod.bootstrap_equity: "bootstrap_equity",
}

#: Top-level keys that identify an :class:`AppConfig` document.
_APPCONFIG_MARKERS = frozenset({"project_name", "log_level", "backtest", "validation", "reporting"})

#: Top-level keys that identify a Freqtrade bot configuration.
_FREQTRADE_MARKERS = frozenset(
    {"max_open_trades", "stake_currency", "dry_run", "tradable_balance_ratio"}
)


# ---------------------------------------------------------------------------
# payload / output helpers
# ---------------------------------------------------------------------------


def _payload(
    *,
    command: str,
    ok: bool,
    symbol: str | None = None,
    timeframe: str | None = None,
    config_path: str | None = None,
    metrics: Mapping[str, Any] | None = None,
    run: Mapping[str, Any] | None = None,
    reports: Sequence[str] = (),
    data_quality: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON payload printed by every command (exactly :data:`PAYLOAD_KEYS`)."""
    payload: dict[str, Any] = {
        "command": command,
        "ok": bool(ok),
        "symbol": symbol,
        "timeframe": timeframe,
        "config_path": config_path,
        "metrics": None if metrics is None else dict(metrics),
        "run": None if run is None else dict(run),
        "reports": [str(path) for path in reports],
        "data_quality": None if data_quality is None else dict(data_quality),
    }
    return {key: payload[key] for key in PAYLOAD_KEYS}


def _format_value(value: Any) -> str:
    """Render one scalar for the human-readable summary."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:,.4f}"
    return str(value)


def _metric_values(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the ``{name: value}`` mapping of a ``MetricSet.to_dict()`` payload."""
    metrics = payload.get("metrics")
    if not isinstance(metrics, Mapping):
        return {}
    values = metrics.get("values")
    return dict(values) if isinstance(values, Mapping) else {}


#: Scalar keys printed as highlights under the metrics table, per command.
HIGHLIGHT_KEYS: dict[str, tuple[str, ...]] = {
    "backtest": ("strategy_name", "initial_balance", "final_balance"),
    "walk_forward": (
        "mode",
        "n_windows",
        "aggregate_oos_metric",
        "aggregate_is_metric",
        "efficiency",
        "is_consistent",
    ),
    "robustness": (
        "metric_name",
        "n_points",
        "metric_mean",
        "stability",
        "positive_ratio",
        "robust_ratio",
        "is_robust",
    ),
    "monte_carlo": (
        "method",
        "n_simulations",
        "mean_return",
        "median_return",
        "prob_profit",
        "var_95",
        "cvar_95",
    ),
    "data_download": ("rows", "cache_path"),
}

#: Commands whose human output is the raw payload (configuration inspection).
_RAW_PAYLOAD_COMMANDS = frozenset({"config_show", "config_validate"})


def _print_human(payload: Mapping[str, Any]) -> None:
    """Print the rich summary of a successful run (metrics table + report paths)."""
    command = str(payload.get("command") or "")
    run = payload.get("run")
    run_mapping: Mapping[str, Any] = run if isinstance(run, Mapping) else {}

    if command in _RAW_PAYLOAD_COMMANDS:
        # configuration inspection: the effective configuration *is* the output
        typer.echo(json.dumps(dict(run_mapping), indent=2, sort_keys=True, default=str))
        return

    symbol = payload.get("symbol") or "-"
    timeframe = payload.get("timeframe") or "-"
    console.print(f"[bold]{command}[/bold] {symbol} {timeframe}", highlight=False)

    values = _metric_values(payload)
    if values:
        table = Table(title="Metrics", header_style="bold")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        for name in sorted(values):
            table.add_row(str(name), _format_value(values[name]))
        console.print(table)

    for key in HIGHLIGHT_KEYS.get(command, ()):
        if key in run_mapping:
            console.print(f"  {key}: {_format_value(run_mapping[key])}", highlight=False)

    benchmark = run_mapping.get("benchmark")
    if isinstance(benchmark, Mapping):
        console.print(f"  benchmark: {benchmark.get('variant')}", highlight=False)
        for key in ("alpha", "beta", "correlation", "strategy_beats_benchmark"):
            value = benchmark.get(key)
            if value is not None:
                console.print(f"  {key}: {_format_value(value)}", highlight=False)

    trades = run_mapping.get("trades")
    if isinstance(trades, list):
        console.print(f"  n_trades: {len(trades)}", highlight=False)

    quality = payload.get("data_quality")
    if isinstance(quality, Mapping):
        status = "ok" if quality.get("ok") else "issues"
        console.print(f"  data: {quality.get('n_rows')} rows ({status})", highlight=False)

    reports = payload.get("reports") or []
    if reports:
        console.print("reports:", highlight=False)
        for path in reports:
            console.print(f"  {path}", highlight=False)


def _emit(payload: Mapping[str, Any], *, json_output: bool) -> None:
    """Print ``payload`` as the single JSON object, or as the rich summary."""
    if json_output:
        typer.echo(json.dumps(dict(payload), indent=2, sort_keys=True, default=str))
        return
    _print_human(payload)


@contextmanager
def _error_surface(command: str, *, json_output: bool) -> Iterator[None]:
    """Turn a domain error into a readable message and exit code ``1``.

    Nothing else is swallowed: a bug (``AttributeError``, ``KeyboardInterrupt``)
    still propagates with its traceback.
    """
    try:
        yield
    except TradingBacktestError as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    _payload(command=command, ok=False), indent=2, sort_keys=True, default=str
                )
            )
        err_console.print(f"error: {exc}", style="red", markup=False, highlight=False)
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# configuration helpers
# ---------------------------------------------------------------------------


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read the JSON object stored in ``path`` (``ConfigError`` when impossible)."""
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in configuration file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(
            f"configuration file {path} must contain a JSON object, got {type(payload).__name__}"
        )
    return payload


def _is_freqtrade_config(payload: Mapping[str, Any]) -> bool:
    """``True`` for a Freqtrade bot configuration, ``False`` for an :class:`AppConfig`."""
    if not _FREQTRADE_MARKERS & set(payload):
        return False
    return not (_APPCONFIG_MARKERS & set(payload))


def _parse_moment(value: str | datetime | None, *, field: str) -> pd.Timestamp | None:
    """Parse an ISO-8601 moment and normalise it to UTC (``None`` stays ``None``)."""
    if value is None:
        return None
    import pandas as pd  # local import: keeps the CLI startup lean

    try:
        moment = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid {field}: {value!r} is not a valid ISO-8601 timestamp") from exc
    if pd.isna(moment):
        raise ConfigError(f"invalid {field}: {value!r} is not a valid ISO-8601 timestamp")
    return moment.tz_localize(UTC) if moment.tz is None else moment.tz_convert(UTC)


def _appconfig_issues(cfg: AppConfig) -> list[str]:
    """Return the coherence issues the pydantic schema alone cannot express."""
    issues: list[str] = []
    start = _parse_moment(cfg.data.start, field="data.start")
    end = _parse_moment(cfg.data.end, field="data.end")
    if start is not None and end is not None and start >= end:
        issues.append(
            f"data.start ({start.isoformat()}) must be before data.end ({end.isoformat()})"
        )
    return issues


# ---------------------------------------------------------------------------
# data helpers
# ---------------------------------------------------------------------------


def _symbol_from_filename(path: Path) -> str | None:
    """Infer ``BTC/USDT`` from a ``BTC_USDT-1h.csv`` style fixture name."""
    stem = Path(path).name
    for suffix in (".csv", ".parquet"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    base = stem.rsplit("-", 1)[0] if "-" in stem else stem
    inferred = base.strip().replace("_", "/").upper()
    return inferred or None


def _resolve_symbol(symbol: str | None, data_file: Path | None) -> str:
    """Resolve the trading pair: ``--symbol``, the file name, then a generic default."""
    if symbol:
        return symbol
    if data_file is not None:
        inferred = _symbol_from_filename(Path(data_file))
        if inferred:
            return inferred
    return DEFAULT_SYMBOL


def _resolve_timeframe(timeframe: str | None, cfg: AppConfig) -> str:
    """Resolve the timeframe: ``--timeframe`` wins over ``data.timeframe``."""
    return timeframe or cfg.data.timeframe


def _read_local_csv(path: Path) -> pd.DataFrame:
    """Read a local OHLCV csv (never the network) and enforce the OHLCV contract."""
    import pandas as pd  # local import: keeps the CLI startup lean

    if not path.is_file():
        raise InsufficientDataError(f"data file not found: {path}")
    try:
        frame = pd.read_csv(path, index_col=OHLCV_INDEX_NAME, parse_dates=True)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise InsufficientDataError(f"cannot read OHLCV csv {path}: {exc}") from exc
    if frame.empty:
        raise InsufficientDataError(f"no candle in {path}")
    return ensure_ohlcv(frame, name=str(path))


def _build_loader(cfg: AppConfig, *, no_network: bool) -> OHLCVLoader:
    """Build the cache-first loader (a cache miss never downloads when offline).

    Network access requires both switches to allow it: ``data.allow_network`` in
    the configuration *and* the absence of ``--no-network`` on the command line.
    """
    return OHLCVLoader(
        cfg.exchange.name,
        Path(cfg.data.cache_dir),
        fmt=cfg.data.format,
        allow_network=bool(cfg.data.allow_network) and not no_network,
        validate=cfg.data.validate,
        max_gap_factor=cfg.data.max_gap_factor,
        max_missing_ratio=cfg.data.max_missing_ratio,
        provider=None,
    )


def _quality(frame: pd.DataFrame, cfg: AppConfig, *, timeframe: str) -> DataQualityReport:
    """Run the data-quality gate (raising when ``data.validate`` asks for it)."""
    return validate_ohlcv(
        frame,
        timeframe=timeframe,
        max_gap_factor=cfg.data.max_gap_factor,
        max_missing_ratio=cfg.data.max_missing_ratio,
        raise_on_error=cfg.data.validate,
    )


def _acquire_data(
    cfg: AppConfig,
    *,
    symbol: str,
    timeframe: str,
    start: str | datetime | None,
    end: str | datetime | None,
    data_file: Path | None,
    no_network: bool,
) -> pd.DataFrame:
    """Return the OHLCV window to analyse: a local csv, or the cache-first loader."""
    since = _parse_moment(start if start is not None else cfg.data.start, field="--start")
    until = _parse_moment(end if end is not None else cfg.data.end, field="--end")

    if data_file is not None:
        frame = _read_local_csv(Path(data_file))
        if since is not None:
            frame = frame.loc[frame.index >= since]
        if until is not None:
            frame = frame.loc[frame.index <= until]
        if frame.empty:
            raise InsufficientDataError(
                f"no candle for {symbol} {timeframe} in {data_file} inside the requested window"
            )
        return frame

    if since is None or until is None:
        raise ConfigError(
            "data.start and data.end are required when no --data-file is given: "
            "the requested window cannot be guessed"
        )
    return _build_loader(cfg, no_network=no_network).load(symbol, timeframe, since, until)


# ---------------------------------------------------------------------------
# run helpers (strategy / validation / metrics / reporting are imported lazily)
# ---------------------------------------------------------------------------


def _make_runner(cfg: AppConfig, *, symbol: str) -> RunnerFn:
    """Return the configured runner (imports :mod:`trading_backtest.strategy`)."""
    from trading_backtest.strategy import make_runner

    return make_runner(cfg, symbol=symbol)


def _compute_metrics(
    result: BacktestResult, *, timeframe: str, risk_free_rate: float = 0.0
) -> MetricSet:
    """Compute the metric set of ``result`` (imports :mod:`trading_backtest.metrics`).

    ``risk_free_rate`` is the **annual** rate forwarded to
    :func:`trading_backtest.metrics.compute_metrics`; it only moves
    ``sharpe_ratio``/``sortino_ratio`` (``0.0`` keeps the historical values).
    """
    from trading_backtest.metrics import compute_metrics

    return compute_metrics(result, timeframe=timeframe, risk_free_rate=risk_free_rate)


def _resolve_risk_free_rate(cfg: AppConfig, *, override: float | None) -> float:
    """Return the annual risk-free rate of this run: ``--risk-free-rate``, else the config.

    ``None`` follows ``benchmark.risk_free_rate`` (``0.0`` in the shipped
    configuration, which preserves the historical Sharpe/Sortino values).  An
    explicit override must be a finite fraction ``>= 0``: a negative or
    ``nan``/``inf`` rate is a typo, not a study, and is rejected with a
    :class:`~trading_backtest.core.errors.ConfigError` (exit code ``1``, no
    traceback).
    """
    if override is None:
        return float(cfg.benchmark.risk_free_rate)
    if override < 0.0 or not math.isfinite(override):
        raise ConfigError(f"--risk-free-rate must be a finite fraction >= 0, got {override!r}")
    return float(override)


def _resolve_benchmark_variant(
    cfg: AppConfig, *, enabled: bool | None, variant_override: str | None = None
) -> str:
    """Return the effective benchmark variant; ``'none'`` means: do not compute any benchmark.

    ``enabled`` is the tri-state ``--benchmark/--no-benchmark`` flag: ``None``
    follows ``benchmark.enabled``, ``True``/``False`` force the benchmark on/off.
    ``variant='none'`` in the configuration always wins over ``--benchmark``, and
    ``--no-benchmark`` disables the benchmark whatever the configured variant.

    ``--benchmark-variant`` wins over ``benchmark.variant`` **when the benchmark
    is active** (it can never resurrect a disabled benchmark) and is validated
    here, by hand, against the real
    :data:`trading_backtest.metrics.BENCHMARK_VARIANTS` tuple -- ``typer``'s
    ``StrEnum`` idiom does not cover that set without new plumbing.  Exactly one
    benchmark is ever computed per run.
    """
    active = cfg.benchmark.enabled if enabled is None else bool(enabled)
    if not active:
        return "none"
    if variant_override is None:
        return str(cfg.benchmark.variant)

    from trading_backtest.metrics import BENCHMARK_VARIANTS

    if variant_override not in BENCHMARK_VARIANTS:
        raise ConfigError(
            f"unknown benchmark variant: {variant_override!r}; available variants: "
            f"{', '.join(BENCHMARK_VARIANTS)}"
        )
    return str(variant_override)


def _benchmark_gate(
    result: BacktestResult,
    frame: pd.DataFrame,
    *,
    variant: str,
    cfg: AppConfig,
    risk_free_rate: float = 0.0,
) -> BenchmarkGateResult | None:
    """Gate ``result`` against its ``variant`` benchmark (lazy validation import).

    Returns ``None`` when no *curve* benchmark is requested: ``variant == 'none'``
    (nothing to compare against) and ``variant == 'random_entry'`` (a distribution
    comparison, handled by :func:`_random_entry_gate`).  Both short-circuits keep
    the "no import for a disabled benchmark" contract intact.

    ``risk_free_rate`` is forwarded to
    :func:`trading_backtest.validation.validate_benchmark`, so the
    ``sharpe_ratio``/``sortino_ratio`` of the strategy row and of the benchmark row
    use the very same annual rate.
    """
    if variant in {"none", "random_entry"}:
        return None

    from trading_backtest.validation import validate_benchmark

    return validate_benchmark(
        result,
        frame,
        variant=variant,
        fee_rate=cfg.exchange.fee_rate,
        slippage=cfg.exchange.slippage,
        risk_free_rate=risk_free_rate,
    )


def _random_entry_gate(
    result: BacktestResult,
    frame: pd.DataFrame,
    *,
    variant: str,
    cfg: AppConfig,
) -> RandomEntryGateResult | None:
    """Gate ``result`` against random entries (the skill test); ``None`` when not selected.

    Only ``variant == 'random_entry'`` runs the simulation: nothing is ever
    computed for a disabled benchmark, and the other variants are curve
    comparisons handled by :func:`_benchmark_gate`.

    ``n_simulations``/``random_seed`` come from the configuration
    (``benchmark.n_random_simulations`` / ``benchmark.random_entry_seed``), so a
    run is reproducible from its configuration alone; the seed is always explicit.
    ``risk_free_rate`` is deliberately **not** passed: the ranking is done on
    ``total_return``, which is risk-free-rate free by construction.
    """
    if variant != "random_entry":
        return None

    from trading_backtest.validation import validate_random_entry

    return validate_random_entry(
        result,
        frame,
        n_simulations=cfg.benchmark.n_random_simulations,
        random_seed=cfg.benchmark.random_entry_seed,
        fee_rate=cfg.exchange.fee_rate,
        slippage=cfg.exchange.slippage,
    )


def _parse_formats(value: str | None, cfg: AppConfig) -> list[str]:
    """Resolve the report formats: ``--formats`` wins, ``reporting.formats`` is the default."""
    if value is None:
        return [str(fmt) for fmt in cfg.reporting.formats]
    return [part.strip() for part in value.split(",") if part.strip()]


def _grid_size(grid: Mapping[str, Sequence[Any]]) -> int:
    """Number of parameter combinations of ``grid``."""
    size = 1
    for values in grid.values():
        size *= max(1, len(values))
    return size


def _trim_grid(
    grid: Mapping[str, Sequence[Any]],
    limit: int,
) -> dict[str, list[Any]]:
    """Shrink ``grid`` until it holds at most ``limit`` combinations.

    The longest list of values loses its last entry first (alphabetical order
    breaks ties), which keeps the result deterministic.  A parameter never
    shrinks below one value, so the trimmed grid is always sweepable.
    """
    trimmed: dict[str, list[Any]] = {key: list(values) for key, values in grid.items()}
    while _grid_size(trimmed) > limit:
        candidates = sorted(
            (key for key, values in trimmed.items() if len(values) > 1),
            key=lambda key: (-len(trimmed[key]), key),
        )
        if not candidates:
            break
        trimmed[candidates[0]] = trimmed[candidates[0]][:-1]
    return trimmed


def _robustness_grid(cfg: AppConfig, *, max_combinations: int) -> dict[str, list[Any]]:
    """Return the grid to sweep: ``validation.robustness_grid``, or the strategy space.

    An explicit grid is never silently trimmed — :func:`parameter_sweep` refuses a
    grid larger than ``max_combinations``, which is the documented safety ceiling.
    The *implicit* grid (the parameter space of ``strategy.name``) is trimmed to
    ``max_combinations`` so that ``--max-combinations`` always works.
    """
    from trading_backtest.strategy import strategy_param_space

    if max_combinations < 1:
        raise ConfigError(f"--max-combinations must be >= 1, got {max_combinations}")
    if cfg.validation.robustness_grid:
        return {key: list(values) for key, values in cfg.validation.robustness_grid.items()}
    return _trim_grid(strategy_param_space(cfg.strategy.name), max_combinations)


def _write_and_emit(
    command: str,
    *,
    cfg: AppConfig,
    result: BacktestResult,
    metrics: MetricSet,
    run: Mapping[str, Any],
    extras: Mapping[str, Mapping[str, Any]],
    quality: DataQualityReport,
    symbol: str,
    timeframe: str,
    config_path: Path,
    output_dir: Path | None,
    formats: str | None,
    json_output: bool,
    benchmark: BenchmarkGateResult | None = None,
    random_entry: RandomEntryGateResult | None = None,
) -> None:
    """Build the report, write it through ``write_report`` and emit the payload.

    ``benchmark`` is the optional curve gate (buy & hold, cash or risk free): its
    three side-by-side rows become the ``Benchmark`` report section and its
    scalars travel inside the ``run`` payload (never as a new top-level payload
    key).

    ``random_entry`` is the optional skill gate: its payload becomes the
    ``random_entry`` report section *and* ``run["random_entry"]``.  Both channels
    are handled here, once, so the report section and the run payload can never
    disagree and no command duplicates the logic.
    """
    from trading_backtest.reporting import build_report, write_report

    payloads: dict[str, Mapping[str, Any]] = dict(extras)
    if random_entry is not None:
        payloads["random_entry"] = random_entry.to_dict()
    report = build_report(
        result=result,
        metrics=metrics,
        extras=payloads,
        title=cfg.reporting.title,
        config_echo=cfg.model_dump(mode="json"),
        include_trades=cfg.reporting.include_trades,
        trade_limit=cfg.reporting.trade_limit,
        benchmark=None if benchmark is None else benchmark.comparison.comparison_rows(),
    )
    directory = Path(output_dir) if output_dir is not None else Path(cfg.reporting.output_dir)
    written = write_report(
        report,
        directory,
        formats=_parse_formats(formats, cfg),
        basename=cfg.reporting.basename,
    )
    run_payload = dict(run)
    if benchmark is not None:
        run_payload["benchmark"] = benchmark.to_dict()
    if random_entry is not None:
        run_payload["random_entry"] = random_entry.to_dict()
    _emit(
        _payload(
            command=command,
            ok=True,
            symbol=symbol,
            timeframe=timeframe,
            config_path=str(config_path),
            metrics=metrics.to_dict(),
            run=run_payload,
            reports=[str(path) for path in written],
            data_quality=quality.to_dict(),
        ),
        json_output=json_output,
    )


# ---------------------------------------------------------------------------
# typer application
# ---------------------------------------------------------------------------

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Backtest, validate and report on trading strategies (offline by default).",
)
data_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Acquire and cache market data.",
)
config_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Inspect and validate configuration files.",
)
realtime_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run and monitor N concurrent trading profiles in real time.",
)
app.add_typer(data_app, name="data")
app.add_typer(config_app, name="config")
app.add_typer(realtime_app, name="realtime")

_CONFIG_HELP = "Path to the JSON configuration file."
_SYMBOL_HELP = "Trading pair, e.g. 'BTC/USDT' (inferred from --data-file when omitted)."
_TIMEFRAME_HELP = "Candle timeframe: 1m, 5m, 15m, 30m, 1h, 4h or 1d."
_START_HELP = "ISO-8601 start of the window (defaults to data.start)."
_END_HELP = "ISO-8601 end of the window (defaults to data.end)."
_DATA_FILE_HELP = "Local OHLCV csv: used instead of the cache, never touches the network."
_OUTPUT_DIR_HELP = "Report directory (defaults to reporting.output_dir)."
_FORMATS_HELP = "Comma separated report formats (markdown,json). Default: reporting.formats."
_NO_NETWORK_HELP = "Never download: a cache miss is a hard error."
_BENCHMARK_HELP = "Compare the strategy to a buy & hold benchmark. Default: benchmark.enabled."
_RISK_FREE_HELP = (
    "Annualised risk-free rate as a fraction (0.05 = 5 %/yr); default: benchmark.risk_free_rate."
)
_BENCHMARK_VARIANT_HELP = (
    "Benchmark variant: buy_and_hold, cash, risk_free, random_entry or none; "
    "default: benchmark.variant."
)
_JSON_HELP = "Print one JSON object on stdout instead of the human summary."


def _version_callback(value: bool) -> None:
    """Eager ``--version``: print the package version and exit with code 0."""
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _root_callback(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Backtest, validate and report on trading strategies."""


@app.command()
def backtest(
    config: Path = typer.Option(..., "--config", "-c", help=_CONFIG_HELP),
    symbol: str | None = typer.Option(None, "--symbol", help=_SYMBOL_HELP),
    timeframe: str | None = typer.Option(None, "--timeframe", help=_TIMEFRAME_HELP),
    start: str | None = typer.Option(None, "--start", help=_START_HELP),
    end: str | None = typer.Option(None, "--end", help=_END_HELP),
    data_file: Path | None = typer.Option(None, "--data-file", help=_DATA_FILE_HELP),
    output_dir: Path | None = typer.Option(None, "--output-dir", help=_OUTPUT_DIR_HELP),
    formats: str | None = typer.Option(None, "--formats", help=_FORMATS_HELP),
    no_network: bool = typer.Option(False, "--no-network", help=_NO_NETWORK_HELP),
    benchmark: bool | None = typer.Option(None, "--benchmark/--no-benchmark", help=_BENCHMARK_HELP),
    risk_free_rate: float | None = typer.Option(None, "--risk-free-rate", help=_RISK_FREE_HELP),
    benchmark_variant: str | None = typer.Option(
        None, "--benchmark-variant", help=_BENCHMARK_VARIANT_HELP
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Backtest the configured strategy over one data window."""
    with _error_surface("backtest", json_output=json_output):
        cfg = load_config(config)
        resolved_symbol = _resolve_symbol(symbol, data_file)
        resolved_timeframe = _resolve_timeframe(timeframe, cfg)
        resolved_risk_free_rate = _resolve_risk_free_rate(cfg, override=risk_free_rate)
        variant = _resolve_benchmark_variant(
            cfg, enabled=benchmark, variant_override=benchmark_variant
        )
        frame = _acquire_data(
            cfg,
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            start=start,
            end=end,
            data_file=data_file,
            no_network=no_network,
        )
        quality = _quality(frame, cfg, timeframe=resolved_timeframe)
        result = _make_runner(cfg, symbol=resolved_symbol)(frame, None)
        metrics = _compute_metrics(
            result, timeframe=resolved_timeframe, risk_free_rate=resolved_risk_free_rate
        )
        gate = _benchmark_gate(
            result,
            frame,
            variant=variant,
            cfg=cfg,
            risk_free_rate=resolved_risk_free_rate,
        )
        random_gate = _random_entry_gate(result, frame, variant=variant, cfg=cfg)
        _write_and_emit(
            "backtest",
            cfg=cfg,
            result=result,
            metrics=metrics,
            run=result.to_dict(),
            extras={"data_quality": quality.to_dict()},
            quality=quality,
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            config_path=config,
            output_dir=output_dir,
            formats=formats,
            json_output=json_output,
            benchmark=gate,
            random_entry=random_gate,
        )


@app.command("walk-forward")
def walk_forward_command(
    config: Path = typer.Option(..., "--config", "-c", help=_CONFIG_HELP),
    symbol: str | None = typer.Option(None, "--symbol", help=_SYMBOL_HELP),
    timeframe: str | None = typer.Option(None, "--timeframe", help=_TIMEFRAME_HELP),
    start: str | None = typer.Option(None, "--start", help=_START_HELP),
    end: str | None = typer.Option(None, "--end", help=_END_HELP),
    data_file: Path | None = typer.Option(None, "--data-file", help=_DATA_FILE_HELP),
    windows: int | None = typer.Option(None, "--windows", help="Number of walk-forward windows."),
    is_ratio: float | None = typer.Option(
        None, "--is-ratio", help="In-sample share of every window (0 < ratio < 1)."
    ),
    mode: WindowMode | None = typer.Option(None, "--mode", help="rolling or anchored."),
    metric: str | None = typer.Option(None, "--metric", help="Metric used to score the windows."),
    output_dir: Path | None = typer.Option(None, "--output-dir", help=_OUTPUT_DIR_HELP),
    formats: str | None = typer.Option(None, "--formats", help=_FORMATS_HELP),
    no_network: bool = typer.Option(False, "--no-network", help=_NO_NETWORK_HELP),
    benchmark: bool | None = typer.Option(None, "--benchmark/--no-benchmark", help=_BENCHMARK_HELP),
    risk_free_rate: float | None = typer.Option(None, "--risk-free-rate", help=_RISK_FREE_HELP),
    benchmark_variant: str | None = typer.Option(
        None, "--benchmark-variant", help=_BENCHMARK_VARIANT_HELP
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Walk-forward analysis: in-sample / out-of-sample stability over time."""
    with _error_surface("walk_forward", json_output=json_output):
        from trading_backtest.validation import walk_forward

        cfg = load_config(config)
        resolved_symbol = _resolve_symbol(symbol, data_file)
        resolved_timeframe = _resolve_timeframe(timeframe, cfg)
        resolved_risk_free_rate = _resolve_risk_free_rate(cfg, override=risk_free_rate)
        variant = _resolve_benchmark_variant(
            cfg, enabled=benchmark, variant_override=benchmark_variant
        )
        frame = _acquire_data(
            cfg,
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            start=start,
            end=end,
            data_file=data_file,
            no_network=no_network,
        )
        quality = _quality(frame, cfg, timeframe=resolved_timeframe)
        runner = _make_runner(cfg, symbol=resolved_symbol)
        result = runner(frame, None)
        metrics = _compute_metrics(
            result, timeframe=resolved_timeframe, risk_free_rate=resolved_risk_free_rate
        )
        gate = _benchmark_gate(
            result,
            frame,
            variant=variant,
            cfg=cfg,
            risk_free_rate=resolved_risk_free_rate,
        )
        random_gate = _random_entry_gate(result, frame, variant=variant, cfg=cfg)
        analysis = walk_forward(
            runner,
            frame,
            metric=metric or cfg.validation.robustness_metric,
            n_windows=windows if windows is not None else cfg.validation.n_windows,
            in_sample_ratio=(is_ratio if is_ratio is not None else cfg.validation.in_sample_ratio),
            mode=(WINDOW_MODES[mode] if mode is not None else cfg.validation.mode),
            purge_candles=cfg.validation.purge_candles,
            initial_balance=cfg.backtest.initial_balance,
        )
        payload = analysis.to_dict()
        _write_and_emit(
            "walk_forward",
            cfg=cfg,
            result=result,
            metrics=metrics,
            run=payload,
            extras={"data_quality": quality.to_dict(), "walk_forward": payload},
            quality=quality,
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            config_path=config,
            output_dir=output_dir,
            formats=formats,
            json_output=json_output,
            benchmark=gate,
            random_entry=random_gate,
        )


@app.command()
def robustness(
    config: Path = typer.Option(..., "--config", "-c", help=_CONFIG_HELP),
    symbol: str | None = typer.Option(None, "--symbol", help=_SYMBOL_HELP),
    timeframe: str | None = typer.Option(None, "--timeframe", help=_TIMEFRAME_HELP),
    data_file: Path | None = typer.Option(None, "--data-file", help=_DATA_FILE_HELP),
    metric: str | None = typer.Option(None, "--metric", help="Metric used to score the grid."),
    max_combinations: int | None = typer.Option(
        None, "--max-combinations", help="Safety ceiling of the parameter sweep."
    ),
    output_dir: Path | None = typer.Option(None, "--output-dir", help=_OUTPUT_DIR_HELP),
    formats: str | None = typer.Option(None, "--formats", help=_FORMATS_HELP),
    no_network: bool = typer.Option(False, "--no-network", help=_NO_NETWORK_HELP),
    benchmark: bool | None = typer.Option(None, "--benchmark/--no-benchmark", help=_BENCHMARK_HELP),
    risk_free_rate: float | None = typer.Option(None, "--risk-free-rate", help=_RISK_FREE_HELP),
    benchmark_variant: str | None = typer.Option(
        None, "--benchmark-variant", help=_BENCHMARK_VARIANT_HELP
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Parametric robustness: sweep a grid and summarise its stability."""
    with _error_surface("robustness", json_output=json_output):
        from trading_backtest.validation import parameter_sweep

        cfg = load_config(config)
        resolved_symbol = _resolve_symbol(symbol, data_file)
        resolved_timeframe = _resolve_timeframe(timeframe, cfg)
        resolved_risk_free_rate = _resolve_risk_free_rate(cfg, override=risk_free_rate)
        variant = _resolve_benchmark_variant(
            cfg, enabled=benchmark, variant_override=benchmark_variant
        )
        limit = (
            max_combinations
            if max_combinations is not None
            else cfg.validation.robustness_max_combinations
        )
        grid = _robustness_grid(cfg, max_combinations=limit)
        frame = _acquire_data(
            cfg,
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            start=None,
            end=None,
            data_file=data_file,
            no_network=no_network,
        )
        quality = _quality(frame, cfg, timeframe=resolved_timeframe)
        runner = _make_runner(cfg, symbol=resolved_symbol)
        result = runner(frame, None)
        metrics = _compute_metrics(
            result, timeframe=resolved_timeframe, risk_free_rate=resolved_risk_free_rate
        )
        gate = _benchmark_gate(
            result,
            frame,
            variant=variant,
            cfg=cfg,
            risk_free_rate=resolved_risk_free_rate,
        )
        random_gate = _random_entry_gate(result, frame, variant=variant, cfg=cfg)
        sweep = parameter_sweep(
            runner,
            frame,
            grid,
            base_params=dict(cfg.strategy.params) or None,
            metric=metric or cfg.validation.robustness_metric,
            max_combinations=limit,
            initial_balance=cfg.backtest.initial_balance,
        )
        payload = sweep.to_dict()
        _write_and_emit(
            "robustness",
            cfg=cfg,
            result=result,
            metrics=metrics,
            run=payload,
            extras={"data_quality": quality.to_dict(), "robustness": payload},
            quality=quality,
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            config_path=config,
            output_dir=output_dir,
            formats=formats,
            json_output=json_output,
            benchmark=gate,
            random_entry=random_gate,
        )


@app.command("monte-carlo")
def monte_carlo_command(
    config: Path = typer.Option(..., "--config", "-c", help=_CONFIG_HELP),
    symbol: str | None = typer.Option(None, "--symbol", help=_SYMBOL_HELP),
    timeframe: str | None = typer.Option(None, "--timeframe", help=_TIMEFRAME_HELP),
    data_file: Path | None = typer.Option(None, "--data-file", help=_DATA_FILE_HELP),
    simulations: int | None = typer.Option(
        None, "--simulations", help="Number of simulated equity paths."
    ),
    method: MonteCarloMethod | None = typer.Option(
        None, "--method", help="trade_resample or bootstrap_equity."
    ),
    seed: int | None = typer.Option(None, "--seed", help="Random seed (determinism)."),
    output_dir: Path | None = typer.Option(None, "--output-dir", help=_OUTPUT_DIR_HELP),
    formats: str | None = typer.Option(None, "--formats", help=_FORMATS_HELP),
    benchmark: bool | None = typer.Option(None, "--benchmark/--no-benchmark", help=_BENCHMARK_HELP),
    risk_free_rate: float | None = typer.Option(None, "--risk-free-rate", help=_RISK_FREE_HELP),
    benchmark_variant: str | None = typer.Option(
        None, "--benchmark-variant", help=_BENCHMARK_VARIANT_HELP
    ),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Monte Carlo simulation of the trades produced by one backtest."""
    with _error_surface("monte_carlo", json_output=json_output):
        from trading_backtest.validation import monte_carlo

        cfg = load_config(config)
        resolved_symbol = _resolve_symbol(symbol, data_file)
        resolved_timeframe = _resolve_timeframe(timeframe, cfg)
        resolved_risk_free_rate = _resolve_risk_free_rate(cfg, override=risk_free_rate)
        variant = _resolve_benchmark_variant(
            cfg, enabled=benchmark, variant_override=benchmark_variant
        )
        frame = _acquire_data(
            cfg,
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            start=None,
            end=None,
            data_file=data_file,
            no_network=False,
        )
        quality = _quality(frame, cfg, timeframe=resolved_timeframe)
        result = _make_runner(cfg, symbol=resolved_symbol)(frame, None)
        metrics = _compute_metrics(
            result, timeframe=resolved_timeframe, risk_free_rate=resolved_risk_free_rate
        )
        gate = _benchmark_gate(
            result,
            frame,
            variant=variant,
            cfg=cfg,
            risk_free_rate=resolved_risk_free_rate,
        )
        random_gate = _random_entry_gate(result, frame, variant=variant, cfg=cfg)
        simulation = monte_carlo(
            result,
            n_simulations=(
                simulations if simulations is not None else cfg.validation.n_monte_carlo
            ),
            method=(
                MONTE_CARLO_METHODS[method]
                if method is not None
                else cfg.validation.monte_carlo_method
            ),
            random_seed=seed if seed is not None else cfg.validation.random_seed,
        )
        payload = simulation.to_dict()
        _write_and_emit(
            "monte_carlo",
            cfg=cfg,
            result=result,
            metrics=metrics,
            run=payload,
            extras={"data_quality": quality.to_dict(), "monte_carlo": payload},
            quality=quality,
            symbol=resolved_symbol,
            timeframe=resolved_timeframe,
            config_path=config,
            output_dir=output_dir,
            formats=formats,
            json_output=json_output,
            benchmark=gate,
            random_entry=random_gate,
        )


# ---------------------------------------------------------------------------
# data sub-commands
# ---------------------------------------------------------------------------


@data_app.command("download")
def data_download(
    config: Path = typer.Option(..., "--config", "-c", help=_CONFIG_HELP),
    symbol: str = typer.Option(..., "--symbol", help="Trading pair, e.g. 'BTC/USDT'."),
    timeframe: str = typer.Option(..., "--timeframe", help=_TIMEFRAME_HELP),
    start: str = typer.Option(..., "--start", help="ISO-8601 start of the window."),
    end: str = typer.Option(..., "--end", help="ISO-8601 end of the window."),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Fill the on-disk OHLCV cache (the only command that may use the network)."""
    with _error_surface("data_download", json_output=json_output):
        cfg = load_config(config)
        since = _parse_moment(start, field="--start")
        until = _parse_moment(end, field="--end")
        loader = _build_loader(cfg, no_network=False)
        frame = loader.load(symbol, timeframe, since, until)
        quality = _quality(frame, cfg, timeframe=timeframe)
        run = {
            "rows": len(frame),
            "start": str(frame.index[0].isoformat()),
            "end": str(frame.index[-1].isoformat()),
            "cache_path": str(loader.cache.path_for(cfg.exchange.name, symbol, timeframe)),
        }
        _emit(
            _payload(
                command="data_download",
                ok=True,
                symbol=symbol,
                timeframe=timeframe,
                config_path=str(config),
                run=run,
                data_quality=quality.to_dict(),
            ),
            json_output=json_output,
        )


# ---------------------------------------------------------------------------
# config sub-commands
# ---------------------------------------------------------------------------


@config_app.command("show")
def config_show(
    config: Path = typer.Option(..., "--config", "-c", help=_CONFIG_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Print the effective configuration (defaults and environment applied)."""
    with _error_surface("config_show", json_output=json_output):
        path = Path(config)
        raw = _read_json_object(path)
        run = raw if _is_freqtrade_config(raw) else load_config(path).model_dump(mode="json")
        _emit(
            _payload(command="config_show", ok=True, config_path=str(path), run=run),
            json_output=json_output,
        )


@config_app.command("validate")
def config_validate(
    config: Path = typer.Option(..., "--config", "-c", help=_CONFIG_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Validate a configuration file (``AppConfig`` or Freqtrade); exit 1 when invalid."""
    with _error_surface("config_show", json_output=json_output):
        path = Path(config)
        raw = _read_json_object(path)
        issues: list[str]
        if _is_freqtrade_config(raw):
            kind = "freqtrade"
            issues = validate_freqtrade_config(raw)
            if issues:
                raise FreqtradeConfigError(f"{path} is not a valid Freqtrade configuration", issues)
        else:
            kind = "appconfig"
            issues = _appconfig_issues(load_config(path))
            if issues:
                raise ConfigError(f"{path} is not a consistent AppConfig", issues)
        _emit(
            _payload(
                command="config_show",
                ok=True,
                config_path=str(path),
                run={"valid": True, "kind": kind, "issues": issues},
            ),
            json_output=json_output,
        )


# ---------------------------------------------------------------------------
# realtime engine and monitoring (layers 6 and 7, imported in the bodies)
# ---------------------------------------------------------------------------

#: Environment variable arming live trading (mirrors ``realtime.risk.LiveTradingGate``).
LIVE_TRADING_ENV = "TB_ALLOW_LIVE_TRADING"

#: The exact value :data:`LIVE_TRADING_ENV` must carry to arm live trading.
LIVE_TRADING_VALUE = "I_UNDERSTAND_THE_RISK"

_PROFILES_HELP = "Path to the JSON profiles file (profiles + realtime + monitoring)."
_HOST_HELP = "Monitoring bind host (default: monitoring.host)."
_PORT_HELP = "Monitoring port; 0 binds an ephemeral port (default: monitoring.port)."
_ONCE_HELP = "Run ONE deterministic engine tick over the polled candles and exit."

#: Credential variable names quoted by the pre-flight messages (never their values).
_CREDENTIAL_HELP = "set TB_LIVE_API_KEY/TB_LIVE_API_SECRET or TB_PROFILE_<ID>_API_KEY/_API_SECRET"

#: Bound of the extra join of the monitoring thread at shutdown, in seconds.
_SERVER_JOIN_TIMEOUT = 5.0


def _print_realtime_human(payload: Mapping[str, Any]) -> None:
    """Print the rich summary of a realtime payload (``realtime run|serve|check``)."""
    command = str(payload.get("command") or "")
    console.print(f"[bold]{command}[/bold]", highlight=False)
    for key in ("config_path", "state_db", "state_db_writable", "kill_switch", "url"):
        if key in payload and payload[key] is not None:
            console.print(f"  {key}: {_format_value(payload[key])}", highlight=False)

    issues = payload.get("issues")
    if isinstance(issues, list) and issues:
        console.print("  issues:", highlight=False)
        for issue in issues:
            console.print(f"    - {issue}", highlight=False)

    profiles = payload.get("profiles")
    if isinstance(profiles, list) and profiles:
        table = Table(title="Profiles", header_style="bold")
        for column in ("Profile", "Symbol", "Timeframe", "Mode", "State"):
            table.add_column(column)
        for entry in profiles:
            if not isinstance(entry, Mapping):
                continue
            state = entry.get("status")
            if state is None:
                state = "ok" if entry.get("ok") else "issues"
            entry_issues = entry.get("issues")
            if isinstance(entry_issues, list) and entry_issues:
                state = f"{state} ({len(entry_issues)} issue(s))"
            table.add_row(
                str(entry.get("profile_id") or entry.get("id") or "-"),
                str(entry.get("symbol") or "-"),
                str(entry.get("timeframe") or "-"),
                str(entry.get("mode") or "-"),
                str(state),
            )
        console.print(table)

    decisions = payload.get("decisions")
    if isinstance(decisions, list):
        console.print(f"  decisions: {len(decisions)}", highlight=False)
        for decision in decisions:
            if isinstance(decision, Mapping):
                console.print(
                    f"    {decision.get('profile_id')} {decision.get('action')} "
                    f"{decision.get('timestamp')}",
                    highlight=False,
                )


def _emit_realtime(payload: Mapping[str, Any], *, json_output: bool) -> None:
    """Print a realtime payload as the single JSON object, or as the rich summary."""
    if json_output:
        typer.echo(json.dumps(dict(payload), indent=2, sort_keys=True, default=str))
        return
    _print_realtime_human(payload)


def _announce(url: str, *, json_output: bool) -> None:
    """Announce the bound monitoring URL (on stderr in ``--json`` mode)."""
    typer.echo(f"monitoring: {url}", err=json_output)


def _realtime_logging(realtime: RealtimeConfig) -> None:
    """Install the structured JSON logs of a realtime command.

    The observability module owns the format; without this call the layer's events
    fall through to the interpreter's last-resort handler, which prints the bare
    event name at WARNING and above and drops every INFO event -- an operator then
    reads ``profile_crashed`` with no error, no profile and no symbol, and never
    sees a single ``candle_processed``.  The console sink goes to ``stderr`` and a
    durable ``realtime-YYYYMMDD.log`` is written under ``realtime.logs_dir`` so the
    reason survives a container restart.
    """
    from trading_backtest.realtime.observability import configure_logging, log_path

    level = os.environ.get("TB_LOG_LEVEL", "INFO").upper()
    configure_logging(level=level, log_file=log_path(Path(realtime.logs_dir)))


def _realtime_clock(realtime: RealtimeConfig) -> Any:
    """Build the time seam of a run.

    ``realtime.start_at`` is the deterministic replay/backfill anchor: when it is
    set the engine runs on a :class:`~trading_backtest.realtime.clock.ManualClock`
    anchored at that instant, which is what makes ``realtime run --once``
    reproducible; otherwise the engine reads the system clock.

    The manual clock is *yielding* on purpose.  The engine paces itself **only**
    through ``Clock.sleep`` (the stream's idle wait and the runner's pacing
    sleep), and ``ManualClock.sleep`` returns without suspending: the supervised
    profile loop then becomes one CPU-bound synchronous stretch inside a single
    task, the event loop never regains control, none of its timers fire (so no
    ``asyncio.wait_for`` bound does) and a ``SIGINT`` is never delivered -- the
    process spins at 100 % of one core and cannot be interrupted.  Adding a
    single ``asyncio.sleep(0)`` (which reads no wall clock) keeps the replay
    byte-identical while leaving the loop cooperative.  The trade-off is written
    down in ``docs/realtime.md``: an anchored continuous run replays as fast as
    the CPU allows instead of following wall time.
    """
    from trading_backtest.realtime.clock import ManualClock, SystemClock

    anchor = _parse_moment(realtime.start_at, field="realtime.start_at")
    if anchor is None:
        return SystemClock()

    class YieldingManualClock(ManualClock):
        """``ManualClock`` that hands the event loop back after every sleep."""

        async def sleep(self, seconds: float) -> None:
            """Advance the virtual time by ``seconds``, then yield once."""
            self.advance(seconds)
            await asyncio.sleep(0)

    return YieldingManualClock(anchor.to_pydatetime())


def _realtime_store(realtime: RealtimeConfig, clock: Any) -> Any:
    """Build the SQLite state store of a run (never initialized here)."""
    from trading_backtest.realtime.store import SqliteStateStore

    return SqliteStateStore(Path(realtime.state_db), clock=clock)


def _realtime_monitor(store: Any, *, clock: Any, realtime: RealtimeConfig) -> Any:
    """Build the read model over persisted state."""
    from trading_backtest.realtime.monitor import Monitor

    return Monitor(store, clock=clock, realtime=realtime)


def _realtime_stream_factory(realtime: RealtimeConfig, clock: Any) -> Any:
    """Build the per-profile market-stream factory of a run.

    With ``realtime.csv_dir`` set the engine polls a
    :class:`~trading_backtest.data.loader.CsvDataProvider` and never touches the
    network; otherwise it polls the cache-first
    :class:`~trading_backtest.data.loader.OHLCVLoader` of the profile's exchange,
    whose provider is only built (and therefore only needs the optional ``ccxt``
    extra) when that profile is actually wired.
    """
    from trading_backtest.data.loader import CsvDataProvider, OHLCVLoader
    from trading_backtest.realtime.stream import PollingMarketStream

    csv_dir = realtime.csv_dir

    def factory(profile: ProfileConfig) -> Any:
        provider: Any
        if csv_dir is not None:
            provider = CsvDataProvider(Path(csv_dir))
        else:
            provider = OHLCVLoader(
                str(profile.exchange),
                Path(realtime.cache_dir),
                fmt=str(realtime.format),
                allow_network=bool(realtime.allow_network),
                validate=True,
            ).provider
        return PollingMarketStream(
            provider,
            clock=clock,
            exchange=str(profile.exchange),
            history_candles=int(realtime.history_candles),
            poll_interval_seconds=float(profile.poll_interval_seconds),
            timeout_seconds=float(realtime.stream_poll_timeout_seconds),
            max_reconnects=int(realtime.max_stream_reconnects),
            reconnect_backoff_seconds=float(realtime.reconnect_backoff_seconds),
        )

    return factory


def _realtime_orchestrator(
    profiles: Sequence[ProfileConfig],
    realtime: RealtimeConfig,
    monitoring: MonitoringConfig,
    *,
    clock: Any,
    store: Any,
) -> Any:
    """Wire the engine: one orchestrator, N profiles, one shared state store."""
    from trading_backtest.realtime.orchestrator import RealtimeOrchestrator

    return RealtimeOrchestrator(
        profiles=profiles,
        store=store,
        clock=clock,
        realtime=realtime,
        monitoring=monitoring,
        stream_factory=_realtime_stream_factory(realtime, clock),
        environ=os.environ,
        version=__version__,
    )


def _realtime_profiles(orchestrator: Any) -> list[dict[str, Any]]:
    """Return the snapshot payload of every profile of a live orchestrator."""
    return [entry.to_dict() for entry in orchestrator.snapshot().profiles]


def _realtime_tick(
    profiles: Sequence[ProfileConfig],
    realtime: RealtimeConfig,
    monitoring: MonitoringConfig,
    *,
    clock: Any,
    store: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run exactly ONE deterministic engine tick.

    Returns the decisions of the tick and the profile snapshots **read before the
    store is closed** (a snapshot read after ``stop()`` would be reading a closed
    database).  The bound is explicit: the orchestrator ticks its profiles
    sequentially, so the budget is one stream timeout per profile plus one for
    the shutdown.
    """
    orchestrator = _realtime_orchestrator(profiles, realtime, monitoring, clock=clock, store=store)
    budget = float(realtime.stream_poll_timeout_seconds) * (len(profiles) + 1)
    decisions_payload: list[dict[str, Any]] = []
    profiles_payload: list[dict[str, Any]] = []
    try:
        try:
            decisions = asyncio.run(asyncio.wait_for(orchestrator.run_once(), timeout=budget))
        except TimeoutError as exc:
            # A tick that refuses to finish is a stream failure, not a traceback.
            raise MarketStreamError(
                f"the realtime engine did not finish one tick within {budget} s"
            ) from exc
        decisions_payload = [decision.to_dict() for decision in decisions]
        profiles_payload = _realtime_profiles(orchestrator)
    finally:
        asyncio.run(orchestrator.stop())
    return decisions_payload, profiles_payload


async def _engine_until_stopped(orchestrator: Any, sink: list[dict[str, Any]]) -> None:
    """Run the supervised platform, then stop it **on the very same event loop**.

    ``orchestrator.stop()`` cancels the profile tasks it created, so it must run
    on the loop that owns them: calling it from a fresh ``asyncio.run`` after a
    ``SIGINT`` closed the first loop raises ``RuntimeError: Event loop is closed``
    (the tasks of a dead loop cannot be cancelled).  Read the snapshots *before*
    ``stop()`` closes the store, hence the ``sink``: a snapshot read afterwards
    would query a closed database.

    ``SIGINT`` reaches this coroutine as an ``asyncio.CancelledError`` (CPython
    cancels the main task before re-raising ``KeyboardInterrupt``), so both the
    interrupted and the nominal path stop the platform the same way.
    """
    try:
        await orchestrator.run_forever()
    except asyncio.CancelledError:
        sink.extend(_realtime_profiles(orchestrator))
        await orchestrator.stop()
        raise
    except BaseException:  # a failing profile must still close the store
        await orchestrator.stop()
        raise
    sink.extend(_realtime_profiles(orchestrator))
    await orchestrator.stop()


def _realtime_engine(
    profiles: Sequence[ProfileConfig],
    realtime: RealtimeConfig,
    monitoring: MonitoringConfig,
    *,
    clock: Any,
    store: Any,
    host: str | None,
    port: int | None,
    json_output: bool,
) -> tuple[list[dict[str, Any]], str]:
    """Start the monitoring server, run the engine forever, then stop both.

    Returns the last profile snapshots and the monitoring URL.  ``SIGINT`` is a
    clean shutdown: the server is closed and the orchestrator stopped before the
    payload is emitted, and the exit code stays ``0``.
    """
    from trading_backtest.web.server import create_server, start_in_thread

    orchestrator = _realtime_orchestrator(profiles, realtime, monitoring, clock=clock, store=store)
    server = create_server(
        orchestrator,
        monitor=_realtime_monitor(store, clock=clock, realtime=realtime),
        config=monitoring,
        read_only=False,
        host=host,
        port=port,
        version=__version__,
    )
    thread = start_in_thread(server)
    url = f"http://{monitoring.host if host is None else host}:{server.port}/"
    _announce(url, json_output=json_output)
    snapshots: list[dict[str, Any]] = []
    try:
        asyncio.run(_engine_until_stopped(orchestrator, snapshots))
    except KeyboardInterrupt:
        typer.echo("interrupted: the platform is shut down and stopped", err=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=_SERVER_JOIN_TIMEOUT)
    return snapshots, url


def _state_db_writable(path: Path) -> tuple[bool, str]:
    """Probe the writability of the state-database directory.

    The probe creates the parent directory (``mkdir(parents=True, exist_ok=True)``)
    and then writes and deletes a temporary file next to the database: it proves
    the directory is usable **without ever creating the state database itself**,
    which is the documented guarantee of ``realtime check``.
    """
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / f".{path.name}.write-probe"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"the state-database directory {parent} is not writable: {exc}"
    return True, ""


def _realtime_profile_entry(
    profile: ProfileConfig, *, environ: Mapping[str, str], gate: Any
) -> dict[str, Any]:
    """Build the ``realtime check`` entry of one profile (no order, no network).

    The limits are *resolved* through ``RiskLimits.from_config`` -- their
    validation belongs to the pydantic configuration layer, which already ran --
    and the credentials are only ever probed for **presence**: neither a value nor
    a network call is involved.
    """
    from trading_backtest.realtime.credentials import credentials_from_env
    from trading_backtest.realtime.risk import RiskLimits

    issues: list[str] = []
    risk = RiskLimits.from_config(profile.risk).to_dict()

    configured = False
    try:
        credentials = credentials_from_env(
            str(profile.id), exchange=str(profile.exchange), environ=environ
        )
        configured = credentials is not None and bool(credentials.configured)
    except TradingBacktestError as exc:
        # Presence checking never raises: a half-written credential is a finding.
        issues.append(str(exc))

    allowed = bool(gate.allowed(profile))
    if str(profile.mode) == "live":
        if not allowed:
            issues.append(
                f"live profile {profile.id!r} is not armed: set "
                f"{LIVE_TRADING_ENV}={LIVE_TRADING_VALUE}"
            )
        if not configured:
            issues.append(
                f"live profile {profile.id!r} has no credentials in the environment: "
                f"{_CREDENTIAL_HELP}"
            )
    return {
        "id": str(profile.id),
        "symbol": str(profile.symbol),
        "timeframe": str(profile.timeframe),
        "strategy": str(profile.strategy),
        "mode": str(profile.mode),
        "ok": not issues,
        "issues": issues,
        "credentials_present": configured,
        "live_gate_allowed": allowed,
        "risk": risk,
    }


def _realtime_check_payload(
    path: Path,
    *,
    realtime: RealtimeConfig,
    entries: Sequence[Mapping[str, Any]],
    issues: Sequence[str],
    writable: bool,
    kill_switch: bool,
) -> dict[str, Any]:
    """Assemble the documented ``realtime-check`` payload.

    ``issues`` is the **platform-level** counterpart of the per-profile ``issues``
    list (an unreadable document, an unwritable state directory): the documented
    keys are always present, and that eighth key never carries a profile finding.
    """
    profile_entries = [dict(entry) for entry in entries]
    ok = (
        bool(profile_entries)
        and not issues
        and writable
        and all(bool(entry["ok"]) for entry in profile_entries)
    )
    return {
        "command": "realtime-check",
        "ok": ok,
        "config_path": str(path),
        "state_db": str(realtime.state_db),
        "state_db_writable": bool(writable),
        "kill_switch": bool(kill_switch),
        "profiles": profile_entries,
        "issues": [str(issue) for issue in issues],
    }


def _realtime_preflight(path: Path) -> dict[str, Any]:
    """Run the static pre-flight of a profiles document (offline, order-free)."""
    from trading_backtest.realtime.risk import KillSwitch, LiveTradingGate

    profiles = load_profiles(path)
    realtime = load_realtime_config(path)
    load_monitoring_config(path)

    gate = LiveTradingGate(os.environ)
    entries = [
        _realtime_profile_entry(profile, environ=os.environ, gate=gate) for profile in profiles
    ]

    issues: list[str] = []
    writable, problem = _state_db_writable(Path(realtime.state_db))
    if not writable:
        issues.append(problem)

    kill_switch = KillSwitch(
        clock=_realtime_clock(realtime),
        flag_path=realtime.kill_switch_file,
        environ=os.environ,
    ).engaged()
    return _realtime_check_payload(
        path,
        realtime=realtime,
        entries=entries,
        issues=issues,
        writable=writable,
        kill_switch=kill_switch,
    )


def _realtime_check_failure(path: Path, message: str) -> dict[str, Any]:
    """Build the documented ``realtime-check`` payload of an unreadable document."""
    return _realtime_check_payload(
        path,
        realtime=default_realtime_config(),
        entries=(),
        issues=[message],
        writable=False,
        kill_switch=False,
    )


@realtime_app.command("check")
def realtime_check(
    profiles: Path = typer.Option(..., "--profiles", "-p", help=_PROFILES_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Static pre-flight of a profiles file; exit 1 when a profile cannot start."""
    with _error_surface("realtime-check", json_output=json_output):
        path = Path(profiles)
        try:
            payload = _realtime_preflight(path)
        except ConfigError as exc:
            # An unreadable/invalid document is a pre-flight *finding*, not a crash:
            # it is reported through the documented check payload and its `issues`
            # list, and the reason is echoed on stderr exactly like `_error_surface`
            # would (there is no per-profile entry to carry it).
            payload = _realtime_check_failure(path, str(exc))
            _emit_realtime(payload, json_output=json_output)
            err_console.print(f"error: {exc}", style="red", markup=False, highlight=False)
            raise typer.Exit(code=1) from exc
        _emit_realtime(payload, json_output=json_output)
        if not payload["ok"]:
            raise typer.Exit(code=1)


@realtime_app.command("run")
def realtime_run(
    profiles: Path = typer.Option(..., "--profiles", "-p", help=_PROFILES_HELP),
    host: str | None = typer.Option(None, "--host", help=_HOST_HELP),
    port: int | None = typer.Option(None, "--port", help=_PORT_HELP),
    once: bool = typer.Option(False, "--once", help=_ONCE_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Run N concurrent profiles in real time, with the monitoring dashboard.

    ``--once`` executes a single deterministic tick over the polled candles,
    persists the state and exits without starting any HTTP server.
    """
    with _error_surface("realtime-run", json_output=json_output):
        path = Path(profiles)
        engine_profiles = load_profiles(path)
        realtime = load_realtime_config(path)
        monitoring = load_monitoring_config(path)
        _realtime_logging(realtime)
        clock = _realtime_clock(realtime)
        store = _realtime_store(realtime, clock)

        url: str | None = None
        if once:
            decisions, snapshots = _realtime_tick(
                engine_profiles, realtime, monitoring, clock=clock, store=store
            )
        else:
            decisions = []
            snapshots, url = _realtime_engine(
                engine_profiles,
                realtime,
                monitoring,
                clock=clock,
                store=store,
                host=host,
                port=port,
                json_output=json_output,
            )
        _emit_realtime(
            {
                "command": "realtime-run",
                "ok": True,
                "config_path": str(path),
                "state_db": str(realtime.state_db),
                "profiles": snapshots,
                "decisions": decisions,
                "url": url,
            },
            json_output=json_output,
        )


@realtime_app.command("serve")
def realtime_serve(
    profiles: Path = typer.Option(..., "--profiles", "-p", help=_PROFILES_HELP),
    host: str | None = typer.Option(None, "--host", help=_HOST_HELP),
    port: int | None = typer.Option(None, "--port", help=_PORT_HELP),
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Serve the dashboard read-only over the persisted state; no engine runs."""
    from trading_backtest.realtime.clock import SystemClock
    from trading_backtest.web.server import create_server, serve

    with _error_surface("realtime-serve", json_output=json_output):
        path = Path(profiles)
        load_profiles(path)
        realtime = load_realtime_config(path)
        monitoring = load_monitoring_config(path)
        _realtime_logging(realtime)
        clock = SystemClock()
        store = _realtime_store(realtime, clock)
        store.initialize()
        snapshots: list[dict[str, Any]] = []
        url = ""
        try:
            provider = _PersistedSnapshotProvider(store, clock=clock, realtime=realtime)
            server = create_server(
                provider,
                monitor=provider.monitor,
                config=monitoring,
                read_only=True,
                host=host,
                port=port,
                version=__version__,
            )
            url = f"http://{monitoring.host if host is None else host}:{server.port}/"
            _announce(url, json_output=json_output)
            try:
                serve(server, block=True)
            except KeyboardInterrupt:
                typer.echo("interrupted: stopping the monitoring server", err=True)
            finally:
                snapshots = list(provider.snapshot().to_dict()["profiles"])
                server.shutdown()
                server.server_close()
        finally:
            store.close()
        _emit_realtime(
            {
                "command": "realtime-serve",
                "ok": True,
                "config_path": str(path),
                "state_db": str(realtime.state_db),
                "profiles": snapshots,
                "url": url,
            },
            json_output=json_output,
        )


class _PersistedSnapshotProvider:
    """Read-only :class:`~trading_backtest.web.routes.SnapshotProvider` over SQLite.

    ``realtime serve`` runs no engine, so the web layer needs a provider that
    rebuilds the dashboard view from the persisted state only.  Every read is
    cheap (no metric is computed: ``/api/profiles/{id}/metrics`` is answered by
    the injected :class:`~trading_backtest.realtime.monitor.Monitor`), and the two
    mutating members refuse loudly -- the server is built with
    ``read_only=True``, so the transport already answers ``403`` before reaching
    them; implementing them keeps the provider structurally conformant instead of
    silently incomplete.
    """

    def __init__(self, store: Any, *, clock: Any, realtime: RealtimeConfig) -> None:
        from trading_backtest.realtime.risk import KillSwitch

        self._store = store
        self._clock = clock
        self.monitor = _realtime_monitor(store, clock=clock, realtime=realtime)
        self._kill_switch = KillSwitch(
            store,
            clock=clock,
            flag_path=realtime.kill_switch_file,
            environ=os.environ,
        )

    def profile_snapshot(self, profile_id: str) -> Any:
        """Return the persisted snapshot of one profile, or ``None`` when unknown."""
        from trading_backtest.realtime.models import ProfileSnapshot, ProfileStatus, RunMode

        specs = {str(profile.id): profile for profile in self._store.load_profiles()}
        spec = specs.get(str(profile_id))
        if spec is None:
            return None
        equity = self.monitor.equity(profile_id)
        positions = self.monitor.positions(profile_id)
        trades = self.monitor.trades(profile_id)
        health = self.monitor.health(profile_id)
        last = equity[-1] if equity else None
        initial = float(spec.initial_balance)
        cash = float(last.cash) if last is not None else initial
        position_value = float(last.position_value) if last is not None else 0.0
        equity_value = float(last.equity) if last is not None else initial
        state = self._store.profile_state(profile_id)
        return ProfileSnapshot(
            profile_id=str(profile_id),
            symbol=str(spec.symbol),
            timeframe=str(spec.timeframe),
            strategy=str(spec.strategy),
            mode=RunMode(str(spec.mode)),
            status=ProfileStatus(state.status),
            initial_balance=initial,
            equity=equity_value,
            cash=cash,
            position_value=position_value,
            total_return=(equity_value / initial - 1.0) if initial else 0.0,
            n_trades=len(trades),
            open_positions=len(positions),
            health=health,
            started_at=None,
            updated_at=state.updated_at,
        )

    def snapshot(self) -> Any:
        """Return the whole platform rebuilt from the persisted state."""
        import pandas as pd

        from trading_backtest.realtime.models import PlatformSnapshot

        profiles = [
            item
            for item in (
                self.profile_snapshot(str(profile.id)) for profile in self._store.load_profiles()
            )
            if item is not None
        ]
        state = self.kill_switch_state()
        return PlatformSnapshot(
            profiles=tuple(profiles),
            generated_at=pd.Timestamp(self._clock.now()),
            kill_switch=bool(state.engaged),
            kill_switch_reason=str(state.reason),
            kill_switch_changed_at=state.changed_at,
            version=__version__,
            started_at=None,
            uptime_seconds=0.0,
        )

    def health(self) -> dict[str, Any]:
        """Return the ``/api/health`` body of the persisted platform (no engine)."""
        snapshot = self.snapshot()
        return {
            "status": "degraded" if snapshot.kill_switch else "ok",
            "version": snapshot.version,
            "uptime_seconds": snapshot.uptime_seconds,
            "profiles_total": len(snapshot.profiles),
            "profiles_running": 0,
            "kill_switch": snapshot.kill_switch,
            "checked_at": self._clock.now().isoformat(),
        }

    def kill_switch_state(self) -> Any:
        """Return the effective kill-switch state (file, environment, store)."""
        return self._kill_switch.state()

    def engage_kill_switch(self, reason: str) -> Any:  # pragma: no cover - read-only server
        """Refuse: ``realtime serve`` never mutates the platform."""
        raise MonitoringError("this monitoring server is read-only: run `realtime run` instead")

    def release_kill_switch(self) -> Any:  # pragma: no cover - read-only server
        """Refuse: ``realtime serve`` never mutates the platform."""
        raise MonitoringError("this monitoring server is read-only: run `realtime run` instead")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its exit code (never raises for a usage/domain error).

    ``0`` on success, ``1`` for a :class:`~trading_backtest.core.errors.TradingBacktestError`
    (rendered on stderr as ``error: ...``), ``2`` for a typer/click usage error.
    Anything else propagates untouched.
    """
    args = None if argv is None else list(argv)
    try:
        result = app(args=args, prog_name=PROG_NAME, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code)
    except TradingBacktestError as exc:
        err_console.print(f"error: {exc}", style="red", markup=False, highlight=False)
        return 1
    except Exception as exc:
        # Usage errors are the only other failure the CLI owns: they carry an
        # ``exit_code`` (2 for a bad argument) and know how to render themselves.
        exit_code = getattr(exc, "exit_code", None)
        if not isinstance(exit_code, int):
            raise
        show = getattr(exc, "show", None)
        if callable(show):
            show()
        return int(exit_code)
    return result if isinstance(result, int) else 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
