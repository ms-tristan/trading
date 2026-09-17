"""Tests for the dedicated ``Benchmark`` section of the reporting layer.

The benchmark payload is *opaque data* kept by the reporting layer: these tests
never import the benchmark/metrics implementation, only the reporting builder,
renderer and writer, so they stay offline, deterministic and independent from the
other work packages.  The single exception is the frozen
:data:`trading_backtest.metrics.BENCHMARK_METRIC_NAMES` constant: the rows of the
``risk_free`` variant below are keyed by it so the side-by-side table is pinned
against the very names the metrics layer emits, not against a copy of them.
"""

from __future__ import annotations

import json
from datetime import UTC as DATETIME_UTC
from datetime import datetime
from pathlib import Path

import pandas as pd

from trading_backtest.core.constants import OHLCV_INDEX_NAME, UTC
from trading_backtest.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    TradeRecord,
)
from trading_backtest.metrics import BENCHMARK_METRIC_NAMES
from trading_backtest.reporting import (
    ReportBuilder,
    build_report,
    read_report,
    render_markdown,
    write_report,
)
from trading_backtest.reporting.builder import BENCHMARK_SECTION_TITLE

BASE_TIMESTAMP = pd.Timestamp("2024-01-01T00:00:00Z")
FIXED_TIME = datetime(2024, 6, 1, 12, 30, tzinfo=DATETIME_UTC)

#: The eight metric columns rendered next to the ``variant`` label column.
BENCHMARK_COLUMNS = [
    "alpha",
    "beta",
    "cagr",
    "max_drawdown",
    "max_drawdown_duration",
    "sharpe",
    "total_return",
    "volatility",
]


def make_equity(values: list[float]) -> pd.Series:
    """Hourly equity curve starting 2024-01-01T00:00:00Z."""
    index = pd.date_range(
        BASE_TIMESTAMP,
        periods=len(values),
        freq="h",
        tz=UTC,
        name=OHLCV_INDEX_NAME,
    )
    return pd.Series(
        [float(value) for value in values], index=index, name="equity", dtype="float64"
    )


def make_result(*, n_trades: int = 2, points: int = 26) -> BacktestResult:
    """Synthetic backtest result with ``n_trades`` trades and a rising equity curve."""
    values = [1000.0 + 5.0 * step for step in range(points)]
    trades = [
        TradeRecord(
            entry_time=BASE_TIMESTAMP + pd.Timedelta(hours=step),
            exit_time=BASE_TIMESTAMP + pd.Timedelta(hours=step + 1),
            entry_price=100.0,
            exit_price=100.0 + (10.0 if step % 2 == 0 else -5.0),
            size=1.0,
            direction=Direction.LONG,
            pnl=10.0 if step % 2 == 0 else -5.0,
            pnl_pct=0.01,
            fees=0.25,
            exit_reason=ExitReason.SIGNAL,
            duration_minutes=60.0,
        )
        for step in range(min(n_trades, points - 1))
    ]
    equity = make_equity(values)
    index = pd.DatetimeIndex(equity.index)
    return BacktestResult(
        strategy_name="basic",
        symbol="BTC/USDT",
        timeframe="1h",
        start=index[0],
        end=index[-1],
        initial_balance=1000.0,
        final_balance=float(values[-1]),
        trades=trades,
        equity_curve=equity,
        params={"ema_fast": 12},
        metadata={},
    )


def benchmark_rows() -> list[dict[str, object]]:
    """Three opaque benchmark rows: strategy, buy & hold, and the gap between them."""
    return [
        {
            "variant": "strategy",
            "total_return": 0.4712,
            "cagr": 0.13721,
            "volatility": 0.42,
            "sharpe": 0.78123,
            "max_drawdown": -0.31254,
            "max_drawdown_duration": 4380,
            "alpha": 0.07123,
            "beta": 0.91234,
        },
        {
            "variant": "buy_and_hold",
            "total_return": 0.4,
            "cagr": 0.1183,
            "volatility": 0.5,
            "sharpe": 0.6,
            "max_drawdown": -0.4,
            "max_drawdown_duration": 5200,
            "alpha": None,
            "beta": None,
        },
        {
            "variant": "gap",
            "total_return": 0.0712,
            "alpha": 0.07123,
            "beta": 0.91234,
        },
    ]


def fixed_benchmark_report(**kwargs: object) -> object:
    """``build_report`` on the synthetic result, with the three benchmark rows."""
    options: dict[str, object] = {"metrics": {"total_return": 0.4712}, "generated_at": FIXED_TIME}
    options.update(kwargs)
    return build_report(result=make_result(), benchmark=benchmark_rows(), **options)  # type: ignore[arg-type]


# --- Builder ----------------------------------------------------------------


def test_add_benchmark_is_fluent_and_uses_the_dedicated_title() -> None:
    builder = ReportBuilder(generated_at=FIXED_TIME)
    rows = benchmark_rows()

    assert builder.add_benchmark(rows) is builder
    report = builder.build()
    section = report.get_section(BENCHMARK_SECTION_TITLE)
    assert report.section_titles == ["Benchmark"]
    assert section is not None
    assert section.title == "Benchmark"
    assert section.level == 2
    assert section.body == rows


def test_add_benchmark_accepts_a_mapping_body_and_a_custom_level() -> None:
    report = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_benchmark({"variant": "cash", "total_return": 0.0}, level=3)
        .build()
    )

    section = report.get_section("Benchmark")
    assert section is not None
    assert section.level == 3
    assert section.body == {"variant": "cash", "total_return": 0.0}


def test_add_benchmark_copies_the_rows_defensively() -> None:
    rows = benchmark_rows()
    report = ReportBuilder(generated_at=FIXED_TIME).add_benchmark(rows).build()
    rows[0]["variant"] = "mutated"

    section = report.get_section("Benchmark")
    assert section is not None
    assert section.body[0]["variant"] == "strategy"


# --- build_report wiring ----------------------------------------------------


def test_build_report_places_benchmark_before_trades_and_equity() -> None:
    report = fixed_benchmark_report()

    assert report.section_titles == ["Benchmark", "Trades", "Equity curve"]
    section = report.get_section("Benchmark")
    assert section is not None
    assert section.body == benchmark_rows()
    assert section.level == 2


def test_build_report_places_benchmark_after_the_sorted_extras() -> None:
    report = fixed_benchmark_report(extras={"walk_forward": {"n_windows": 3}})

    assert report.section_titles == ["walk_forward", "Benchmark", "Trades", "Equity curve"]


def test_build_report_without_benchmark_keeps_the_legacy_order() -> None:
    report = build_report(
        result=make_result(),
        metrics={"total_return": 0.4712},
        extras={"walk_forward": {"n_windows": 3}},
        benchmark=None,
    )

    assert report.section_titles == ["walk_forward", "Trades", "Equity curve"]
    assert report.get_section("Benchmark") is None


def test_build_report_benchmark_with_toggles_disabled() -> None:
    report = fixed_benchmark_report(include_trades=False, include_equity=False)

    assert report.section_titles == ["Benchmark"]


def test_build_report_benchmark_rows_are_not_routed_through_extras() -> None:
    report = fixed_benchmark_report(extras={"walk_forward": {"n_windows": 3}})

    assert "Benchmark" not in report.summary
    assert set(report.summary) == {"total_return"} | {
        "symbol",
        "timeframe",
        "strategy_name",
        "start",
        "end",
        "initial_balance",
        "final_balance",
        "n_trades",
    }


# --- Markdown rendering -----------------------------------------------------


def test_render_markdown_benchmark_table() -> None:
    markdown = render_markdown(fixed_benchmark_report())

    assert "## Benchmark" in markdown
    columns = sorted([*BENCHMARK_COLUMNS, "variant"])
    header = "| " + " | ".join(columns) + " |"
    assert header in markdown
    assert "| variant |" in header
    assert len(columns) == 9
    assert "| " + " | ".join("---" for _ in range(9)) + " |" in markdown

    lines = markdown.splitlines()
    start = lines.index(header)
    assert lines[start + 1] == "| " + " | ".join("---" for _ in range(9)) + " |"
    data_rows = lines[start + 2 : start + 5]
    assert len(data_rows) == 3
    assert all(line.startswith("| ") and line.endswith(" |") for line in data_rows)

    strategy_row = lines[start + 2]
    assert strategy_row == " | ".join(
        [
            "| 0.0712",
            "0.9123",
            "0.1372",
            "-0.3125",
            "4380",
            "0.7812",
            "0.4712",
            "strategy",
            "0.4200 |",
        ]
    )
    # Absent cells of the "gap" row are rendered as empty cells.
    assert lines[start + 4] == "| 0.0712 | 0.9123 |  |  |  |  | 0.0712 | gap |  |"


def test_render_markdown_benchmark_mapping_body_is_bullets() -> None:
    report = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_benchmark({"variant": "buy_and_hold", "total_return": 0.4})
        .build()
    )

    assert "- total_return: 0.4000" in render_markdown(report)
    assert "- variant: buy_and_hold" in render_markdown(report)


def test_render_markdown_benchmark_empty_sequence_is_the_placeholder() -> None:
    report = ReportBuilder(generated_at=FIXED_TIME).add_benchmark([]).build()

    assert "_(empty)_" in render_markdown(report)


def test_render_markdown_is_deterministic_across_calls() -> None:
    report = fixed_benchmark_report()

    assert render_markdown(report) == render_markdown(report)  # type: ignore[arg-type]


# --- JSON rendering / writer ------------------------------------------------


def test_to_json_contains_the_benchmark_section() -> None:
    report = fixed_benchmark_report()
    payload = json.loads(report.to_json())  # type: ignore[attr-defined]

    sections = payload["sections"]
    benchmark = [section for section in sections if section["title"] == "Benchmark"]
    assert len(benchmark) == 1
    assert benchmark[0]["level"] == 2
    assert benchmark[0]["body"] == [dict(row) for row in benchmark_rows()]
    assert [section["title"] for section in sections] == [
        "Benchmark",
        "Trades",
        "Equity curve",
    ]


def test_to_dict_is_dumps_safe() -> None:
    report = fixed_benchmark_report()

    assert json.dumps(report.to_dict(), default=str)  # type: ignore[attr-defined]


def test_write_report_and_read_report_round_trip_the_benchmark(tmp_path: Path) -> None:
    written = write_report(fixed_benchmark_report(), tmp_path, formats=("markdown", "json"))  # type: ignore[arg-type]

    assert [path.suffix for path in written] == [".md", ".json"]
    assert all(path.exists() for path in written)

    payload = read_report(tmp_path / "report.json")
    titles = [section["title"] for section in payload["sections"]]
    assert "Benchmark" in titles
    benchmark = payload["sections"][titles.index("Benchmark")]
    assert benchmark["body"] == [dict(row) for row in benchmark_rows()]

    assert "## Benchmark" in (tmp_path / "report.md").read_text(encoding="utf-8")


# --- risk_free variant (side-by-side rows) ----------------------------------

#: Metrics of the strategy side of the ``risk_free`` comparison.
RISK_FREE_STRATEGY_METRICS: dict[str, float] = {
    "total_return": 0.4712,
    "cagr": 0.13721,
    "volatility": 0.42,
    "sharpe_ratio": 0.78123,
    "sortino_ratio": 0.91234,
    "max_drawdown": -0.31254,
    "max_drawdown_duration": 4380.0,
    "final_balance": 1471.2,
}

#: Metrics of the ``risk_free`` side: a riskless rate compounded over the window.
#: The curve is deterministic, hence a volatility of exactly ``0.0`` and no
#: drawdown, which is what makes ``beta`` and ``correlation`` undefined for that
#: side (there is no benchmark variance to regress the strategy returns against).
RISK_FREE_BENCHMARK_METRICS: dict[str, float] = {
    "total_return": 0.1038,
    "cagr": 0.05,
    "volatility": 0.0,
    "sharpe_ratio": 0.0,
    "sortino_ratio": 0.0,
    "max_drawdown": 0.0,
    "max_drawdown_duration": 0.0,
    "final_balance": 1103.8,
}


def risk_free_rows() -> list[dict[str, object]]:
    """The 3 side-by-side rows of a ``risk_free`` comparison, in render order.

    Mirrors ``BenchmarkComparison.comparison_rows()`` for ``variant='risk_free'``:
    ``strategy`` first, then the variant label itself, then ``gap`` -- each row
    keyed by the frozen :data:`BENCHMARK_METRIC_NAMES`.  ``beta``/``correlation``
    are ``None`` on the risk-free side (zero variance) and therefore on the gap
    row too.
    """
    gap = {
        name: RISK_FREE_STRATEGY_METRICS[name] - RISK_FREE_BENCHMARK_METRICS[name]
        for name in BENCHMARK_METRIC_NAMES
    }
    return [
        {
            "variant": "strategy",
            **RISK_FREE_STRATEGY_METRICS,
            "beta": 0.91234,
            "correlation": 0.8712,
        },
        {"variant": "risk_free", **RISK_FREE_BENCHMARK_METRICS, "beta": None, "correlation": None},
        {"variant": "gap", **gap, "beta": None, "correlation": None},
    ]


def fixed_risk_free_report(**kwargs: object) -> object:
    """``build_report`` on the synthetic result, with the risk_free rows."""
    options: dict[str, object] = {"metrics": {"total_return": 0.4712}, "generated_at": FIXED_TIME}
    options.update(kwargs)
    return build_report(result=make_result(), benchmark=risk_free_rows(), **options)  # type: ignore[arg-type]


def test_build_report_risk_free_rows_are_the_side_by_side_triple() -> None:
    report = fixed_risk_free_report()

    assert report.section_titles == ["Benchmark", "Trades", "Equity curve"]  # type: ignore[attr-defined]
    section = report.get_section("Benchmark")  # type: ignore[attr-defined]
    assert section is not None
    assert [row["variant"] for row in section.body] == ["strategy", "risk_free", "gap"]
    assert len(BENCHMARK_METRIC_NAMES) == 8
    for row in section.body:
        assert set(BENCHMARK_METRIC_NAMES) <= set(row)


def test_render_markdown_risk_free_table_has_the_frozen_metric_columns() -> None:
    markdown = render_markdown(fixed_risk_free_report())  # type: ignore[arg-type]

    columns = sorted([*BENCHMARK_METRIC_NAMES, "beta", "correlation", "variant"])
    header = "| " + " | ".join(columns) + " |"
    assert "## Benchmark" in markdown
    assert header in markdown
    assert "| " + " | ".join("---" for _ in columns) + " |" in markdown

    lines = markdown.splitlines()
    start = lines.index(header)
    data_rows = lines[start + 2 : start + 5]
    assert len(data_rows) == 3
    assert "| risk_free |" in data_rows[1]
    assert "| gap |" in data_rows[2]


def test_risk_free_zero_variance_keeps_beta_and_correlation_as_json_null() -> None:
    report = fixed_risk_free_report()
    payload = json.loads(report.to_json())  # type: ignore[attr-defined]  # type: ignore[attr-defined]

    body = payload["sections"][0]["body"]
    rows = {row["variant"]: row for row in body}
    assert rows["strategy"]["beta"] == 0.91234
    assert rows["strategy"]["correlation"] == 0.8712
    assert rows["risk_free"]["beta"] is None
    assert rows["risk_free"]["correlation"] is None
    assert rows["gap"]["beta"] is None
    assert rows["gap"]["correlation"] is None
    # ``None`` really leaves as JSON ``null`` (never as a string or a NaN).
    json_text = report.to_json()  # type: ignore[attr-defined]
    assert json_text.count('"beta": null') == 2
    assert json_text.count('"correlation": null') == 2


def test_render_markdown_risk_free_null_regression_cells_are_empty() -> None:
    markdown = render_markdown(fixed_risk_free_report())  # type: ignore[arg-type]

    columns = sorted([*BENCHMARK_METRIC_NAMES, "beta", "correlation", "variant"])
    lines = markdown.splitlines()
    header = "| " + " | ".join(columns) + " |"
    data_rows = lines[lines.index(header) + 2 : lines.index(header) + 5]
    beta_index = columns.index("beta")
    correlation_index = columns.index("correlation")

    def cells(row: str) -> list[str]:
        return row.removeprefix("| ").removesuffix(" |").split(" | ")

    risk_free_cells = cells(data_rows[1])
    assert len(risk_free_cells) == len(columns)
    assert risk_free_cells[beta_index] == ""
    assert risk_free_cells[correlation_index] == ""
    assert cells(data_rows[0])[beta_index] == "0.9123"
    assert cells(data_rows[0])[correlation_index] == "0.8712"


def test_write_report_round_trips_the_risk_free_rows(tmp_path: Path) -> None:
    written = write_report(fixed_risk_free_report(), tmp_path, formats=("markdown", "json"))  # type: ignore[arg-type]

    assert [path.suffix for path in written] == [".md", ".json"]
    payload = read_report(tmp_path / "report.json")
    titles = [section["title"] for section in payload["sections"]]
    assert titles.count("Benchmark") == 1
    body = payload["sections"][titles.index("Benchmark")]["body"]
    assert [row["variant"] for row in body] == ["strategy", "risk_free", "gap"]
    assert all(set(BENCHMARK_METRIC_NAMES) <= set(row) for row in body)
    assert "| risk_free |" in (tmp_path / "report.md").read_text(encoding="utf-8")
