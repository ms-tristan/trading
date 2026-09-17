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

Exit codes: ``0`` success, ``1`` domain error (any
:class:`~trading_backtest.core.errors.TradingBacktestError`), ``2`` usage error
(unknown command, missing argument, invalid choice...).

Every command supports ``--json``, which prints exactly one
``json.dumps(payload, indent=2, sort_keys=True, default=str)`` object on stdout
with the keys ``command, ok, symbol, timeframe, config_path, metrics, run,
reports, data_quality``.
"""

from __future__ import annotations

import json
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
from trading_backtest.config import AppConfig, load_config
from trading_backtest.core.constants import OHLCV_INDEX_NAME, UTC
from trading_backtest.core.errors import (
    ConfigError,
    FreqtradeConfigError,
    InsufficientDataError,
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

__all__ = ["app", "config_app", "data_app", "main"]

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


def _compute_metrics(result: BacktestResult, *, timeframe: str) -> MetricSet:
    """Compute the metric set of ``result`` (imports :mod:`trading_backtest.metrics`)."""
    from trading_backtest.metrics import compute_metrics

    return compute_metrics(result, timeframe=timeframe)


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
) -> None:
    """Build the report, write it through ``write_report`` and emit the payload."""
    from trading_backtest.reporting import build_report, write_report

    report = build_report(
        result=result,
        metrics=metrics,
        extras=extras,
        title=cfg.reporting.title,
        config_echo=cfg.model_dump(mode="json"),
        include_trades=cfg.reporting.include_trades,
        trade_limit=cfg.reporting.trade_limit,
    )
    directory = Path(output_dir) if output_dir is not None else Path(cfg.reporting.output_dir)
    written = write_report(
        report,
        directory,
        formats=_parse_formats(formats, cfg),
        basename=cfg.reporting.basename,
    )
    _emit(
        _payload(
            command=command,
            ok=True,
            symbol=symbol,
            timeframe=timeframe,
            config_path=str(config_path),
            metrics=metrics.to_dict(),
            run=run,
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
app.add_typer(data_app, name="data")
app.add_typer(config_app, name="config")

_CONFIG_HELP = "Path to the JSON configuration file."
_SYMBOL_HELP = "Trading pair, e.g. 'BTC/USDT' (inferred from --data-file when omitted)."
_TIMEFRAME_HELP = "Candle timeframe: 1m, 5m, 15m, 30m, 1h, 4h or 1d."
_START_HELP = "ISO-8601 start of the window (defaults to data.start)."
_END_HELP = "ISO-8601 end of the window (defaults to data.end)."
_DATA_FILE_HELP = "Local OHLCV csv: used instead of the cache, never touches the network."
_OUTPUT_DIR_HELP = "Report directory (defaults to reporting.output_dir)."
_FORMATS_HELP = "Comma separated report formats (markdown,json). Default: reporting.formats."
_NO_NETWORK_HELP = "Never download: a cache miss is a hard error."
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
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Backtest the configured strategy over one data window."""
    with _error_surface("backtest", json_output=json_output):
        cfg = load_config(config)
        resolved_symbol = _resolve_symbol(symbol, data_file)
        resolved_timeframe = _resolve_timeframe(timeframe, cfg)
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
        metrics = _compute_metrics(result, timeframe=resolved_timeframe)
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
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Walk-forward analysis: in-sample / out-of-sample stability over time."""
    with _error_surface("walk_forward", json_output=json_output):
        from trading_backtest.validation import walk_forward

        cfg = load_config(config)
        resolved_symbol = _resolve_symbol(symbol, data_file)
        resolved_timeframe = _resolve_timeframe(timeframe, cfg)
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
        metrics = _compute_metrics(result, timeframe=resolved_timeframe)
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
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Parametric robustness: sweep a grid and summarise its stability."""
    with _error_surface("robustness", json_output=json_output):
        from trading_backtest.validation import parameter_sweep

        cfg = load_config(config)
        resolved_symbol = _resolve_symbol(symbol, data_file)
        resolved_timeframe = _resolve_timeframe(timeframe, cfg)
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
        metrics = _compute_metrics(result, timeframe=resolved_timeframe)
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
    json_output: bool = typer.Option(False, "--json", help=_JSON_HELP),
) -> None:
    """Monte Carlo simulation of the trades produced by one backtest."""
    with _error_surface("monte_carlo", json_output=json_output):
        from trading_backtest.validation import monte_carlo

        cfg = load_config(config)
        resolved_symbol = _resolve_symbol(symbol, data_file)
        resolved_timeframe = _resolve_timeframe(timeframe, cfg)
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
        metrics = _compute_metrics(result, timeframe=resolved_timeframe)
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
