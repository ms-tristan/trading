"""Tests for the dedicated ``Benchmark`` section of the reporting layer.

The benchmark payload is *opaque data* kept by the reporting layer: these tests
never import the benchmark/metrics implementation, only the reporting builder,
renderer and writer, so they stay offline, deterministic and independent from the
other work packages.
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
