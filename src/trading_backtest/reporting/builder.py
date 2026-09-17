"""Report model and fluent builder.

The reporting layer is the natural bridge between the numeric layers and the
CLI: it knows how to turn a :class:`~trading_backtest.core.models.BacktestResult`,
a :class:`~trading_backtest.metrics.performance.MetricSet` and the opaque
``to_dict()`` payloads produced by the validation layer into one JSON-safe
:class:`Report`.

Dependency direction (frozen): ``reporting`` imports ``core`` and ``metrics`` --
never ``validation`` and never ``strategy``.  That is what keeps the four
implementation work packages independent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from numbers import Integral, Real
from pathlib import PurePath
from typing import Any

import numpy as np
import pandas as pd

from trading_backtest.core.models import BacktestResult, TradeRecord
from trading_backtest.metrics import MetricSet

__all__ = [
    "Report",
    "ReportBuilder",
    "ReportSection",
    "build_report",
]

#: Title of the section holding the trades table and the equity curve section.
TRADES_SECTION_TITLE = "Trades"
EQUITY_SECTION_TITLE = "Equity curve"
NOTES_SECTION_TITLE = "Notes"

#: Title of the section comparing the strategy against its benchmark.
BENCHMARK_SECTION_TITLE = "Benchmark"


def _jsonify(value: Any) -> Any:
    """Recursively coerce ``value`` into JSON-native Python types.

    Mappings become dictionaries with string keys, sequences become lists,
    timestamps/enums/paths become strings and numeric scalars become ``int`` or
    ``float``.  Anything else is rendered with :func:`str` so that
    :meth:`Report.to_dict` is always serialisable by ``json.dumps``.
    """
    if isinstance(value, Enum):
        return _jsonify(value.value)
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonify(item) for item in value]
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        return float(value)
    return str(value)


def _downsample_positions(count: int, max_points: int) -> list[int]:
    """Return ``max_points`` evenly spaced positions covering first and last index.

    ``max_points <= 0`` (or a curve already short enough) means "keep everything".
    """
    if count <= 0:
        return []
    if max_points <= 0 or count <= max_points:
        return list(range(count))
    positions = np.unique(np.rint(np.linspace(0.0, float(count - 1), max_points)).astype("int64"))
    return [int(position) for position in positions.tolist()]


def _trade_row(trade: TradeRecord) -> dict[str, Any]:
    """Return the compact trade row rendered in the trades section."""
    return {
        "entry_time": pd.Timestamp(trade.entry_time).isoformat(),
        "exit_time": pd.Timestamp(trade.exit_time).isoformat(),
        "entry_price": float(trade.entry_price),
        "exit_price": float(trade.exit_price),
        "size": float(trade.size),
        "direction": str(getattr(trade.direction, "value", trade.direction)),
        "pnl": float(trade.pnl),
        "pnl_pct": float(trade.pnl_pct),
        "exit_reason": str(getattr(trade.exit_reason, "value", trade.exit_reason)),
        "duration_minutes": float(trade.duration_minutes),
    }


def _normalise_body(
    body: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, Any] | list[dict[str, Any]]:
    """Return a defensive copy of a section body (mapping or row sequence)."""
    if isinstance(body, Mapping):
        return {str(key): value for key, value in body.items()}
    return [dict(row) for row in body]


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` as a timezone-aware UTC datetime (naive input -> UTC)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class ReportSection:
    """One titled block of a :class:`Report`.

    ``body`` is either a mapping (rendered as bullets) or a sequence of mappings
    (rendered as a table whose columns are the union of the row keys).
    """

    title: str
    level: int = 2
    body: dict[str, Any] | list[dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable mapping ``{"title", "level", "body"}``."""
        if isinstance(self.body, Mapping):
            body: Any = _jsonify(dict(self.body))
        else:
            body = [_jsonify(dict(row)) for row in self.body]
        return {"title": self.title, "level": self.level, "body": body}


@dataclass(frozen=True)
class Report:
    """A complete, renderable backtest report."""

    title: str
    generated_at: datetime
    summary: dict[str, Any] = field(default_factory=dict)
    sections: list[ReportSection] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def section_titles(self) -> list[str]:
        """Titles of the sections, in insertion order."""
        return [section.title for section in self.sections]

    def get_section(self, title: str) -> ReportSection | None:
        """Return the first section titled ``title`` (or ``None``)."""
        for section in self.sections:
            if section.title == title:
                return section
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the whole report."""
        return {
            "title": self.title,
            "generated_at": _as_utc(self.generated_at).isoformat(),
            "summary": _jsonify(dict(self.summary)),
            "sections": [section.to_dict() for section in self.sections],
            "metadata": _jsonify(dict(self.metadata)),
        }

    def to_markdown(self) -> str:
        """Render the report as Markdown (deterministic)."""
        from trading_backtest.reporting.markdown import render_markdown

        return render_markdown(self)

    def to_json(self) -> str:
        """Render the report as indented, key-sorted JSON."""
        import json

        return json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str)


class ReportBuilder:
    """Fluent builder assembling a :class:`Report` section by section."""

    def __init__(
        self,
        *,
        title: str = "Backtest Report",
        config_echo: Mapping[str, Any] | None = None,
        generated_at: datetime | None = None,
    ) -> None:
        self.title = title
        self.generated_at = _as_utc(generated_at if generated_at is not None else datetime.now(UTC))
        self.summary: dict[str, Any] = {}
        self.sections: list[ReportSection] = []
        self.metadata: dict[str, Any] = {}
        if config_echo is not None:
            self.metadata["config"] = dict(config_echo)
        self._trades: list[TradeRecord] = []
        self._notes: list[str] = []

    # -- summary ---------------------------------------------------------
    def add_metrics(self, metrics: MetricSet | Mapping[str, float]) -> ReportBuilder:
        """Merge metric values into the summary (sorted by metric name)."""
        values = metrics.as_dict() if isinstance(metrics, MetricSet) else dict(metrics)
        for name in sorted(values):
            self.summary[str(name)] = float(values[name])
        return self

    def add_result(self, result: BacktestResult) -> ReportBuilder:
        """Add the identity/balance keys of ``result`` to the summary."""
        self.summary["symbol"] = str(result.symbol)
        self.summary["timeframe"] = str(result.timeframe)
        self.summary["strategy_name"] = str(result.strategy_name)
        self.summary["start"] = pd.Timestamp(result.start).isoformat()
        self.summary["end"] = pd.Timestamp(result.end).isoformat()
        self.summary["initial_balance"] = float(result.initial_balance)
        self.summary["final_balance"] = float(result.final_balance)
        self.summary["n_trades"] = int(result.n_trades)
        self._trades = list(result.trades)
        return self

    # -- sections --------------------------------------------------------
    def add_trades(self, trades: Sequence[TradeRecord], *, limit: int = 50) -> ReportBuilder:
        """Add the ``Trades`` section with at most ``limit`` compact rows."""
        count = max(int(limit), 0)
        rows = [_trade_row(trade) for trade in list(trades)[:count]]
        self.sections.append(ReportSection(title=TRADES_SECTION_TITLE, body=rows))
        return self

    def add_equity(self, equity: pd.Series, *, max_points: int = 200) -> ReportBuilder:
        """Add the ``Equity curve`` section, evenly downsampled to ``max_points``."""
        series = (
            equity.astype("float64")
            if isinstance(equity, pd.Series)
            else pd.Series(equity, dtype="float64")
        )
        index = pd.DatetimeIndex(series.index)
        positions = _downsample_positions(len(series), int(max_points))
        body: dict[str, Any] = {
            "timestamps": [index[position].isoformat() for position in positions],
            "values": [float(series.iloc[position]) for position in positions],
        }
        self.sections.append(ReportSection(title=EQUITY_SECTION_TITLE, body=body))
        return self

    def add_benchmark(
        self,
        body: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        level: int = 2,
    ) -> ReportBuilder:
        """Add the ``Benchmark`` section with an opaque benchmark payload.

        ``body`` is deliberately opaque data: a mapping (rendered as bullets) or a
        sequence of row mappings (rendered as a table, typically one row per
        variant plus the strategy/buy-and-hold gap).  The reporting layer never
        imports the metrics/benchmark layer, exactly like the opaque validation
        payloads handled by :meth:`add_validation`.
        """
        self.sections.append(
            ReportSection(
                title=BENCHMARK_SECTION_TITLE, level=int(level), body=_normalise_body(body)
            )
        )
        return self

    def add_validation(self, name: str, payload: Mapping[str, Any]) -> ReportBuilder:
        """Add an opaque validation payload (walk-forward, robustness, ...)."""
        return self.add_section(name, payload)

    def add_section(
        self,
        title: str,
        body: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        level: int = 2,
    ) -> ReportBuilder:
        """Append a free-form section with an explicit heading level."""
        self.sections.append(
            ReportSection(title=str(title), level=int(level), body=_normalise_body(body))
        )
        return self

    def add_note(self, text: str) -> ReportBuilder:
        """Record a free-form note; notes are rendered in a trailing ``Notes`` section."""
        self._notes.append(str(text))
        return self

    # -- accessors / build -----------------------------------------------
    @property
    def trades(self) -> list[TradeRecord]:
        """Trades stored by the last :meth:`add_result` call."""
        return list(self._trades)

    def build(self) -> Report:
        """Return the immutable :class:`Report` assembled so far."""
        sections = list(self.sections)
        if self._notes:
            sections.append(
                ReportSection(
                    title=NOTES_SECTION_TITLE,
                    level=2,
                    body={"notes": list(self._notes)},
                )
            )
        return Report(
            title=self.title,
            generated_at=self.generated_at,
            summary=dict(self.summary),
            sections=sections,
            metadata=dict(self.metadata),
        )


def build_report(
    *,
    result: BacktestResult,
    metrics: MetricSet | Mapping[str, float] | None = None,
    extras: Mapping[str, Mapping[str, Any]] | None = None,
    title: str = "Backtest Report",
    config_echo: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
    include_trades: bool = True,
    trade_limit: int = 50,
    include_equity: bool = True,
    benchmark: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> Report:
    """Assemble the complete report of one backtest run (one-stop entry point).

    Metrics are computed lazily through :func:`trading_backtest.metrics.compute_metrics`
    when ``metrics`` is not supplied, using the timeframe of ``result``.  ``extras``
    payloads (typically the ``to_dict()`` output of the validation layer) each
    become a section titled exactly like their key, in sorted-key order.

    ``benchmark`` is an optional opaque payload (mapping or sequence of row
    mappings) added as a :data:`BENCHMARK_SECTION_TITLE` section when it is not
    ``None``.  It is inserted after the sorted ``extras`` sections and before the
    ``Trades``/``Equity curve`` sections, giving ``[sorted extras..., 'Benchmark',
    'Trades', 'Equity curve']``.  Because it accepts a sequence of rows it is a
    dedicated parameter and is never routed through ``extras`` (whose values are
    mappings and are titled after their key).  Leaving it at ``None`` produces a
    report byte-identical to the previous behaviour.
    """
    from trading_backtest.metrics import compute_metrics

    builder = ReportBuilder(title=title, config_echo=config_echo, generated_at=generated_at)
    builder.add_result(result)
    builder.add_metrics(
        metrics if metrics is not None else compute_metrics(result, timeframe=result.timeframe)
    )
    payloads = extras or {}
    for name in sorted(payloads):
        builder.add_validation(name, payloads[name])
    if benchmark is not None:
        builder.add_benchmark(benchmark)
    if include_trades:
        builder.add_trades(builder.trades, limit=trade_limit)
    if include_equity:
        builder.add_equity(result.equity_curve)
    return builder.build()
