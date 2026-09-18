"""Tests for the reporting layer (builder, Markdown rendering, writer).

The reports are assembled from :class:`BacktestResult` / :class:`TradeRecord`
objects built in this module: no strategy, validation or engine module is
imported, so the tests stay offline and independent from the other packages.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import textwrap
from datetime import UTC as DATETIME_UTC
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import trading_platform.reporting as reporting_package
from trading_platform.core.constants import OHLCV_INDEX_NAME, UTC
from trading_platform.core.errors import ReportingError
from trading_platform.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    TradeRecord,
)
from trading_platform.metrics import METRIC_NAMES, MetricSet, compute_metrics
from trading_platform.reporting import (
    Report,
    ReportBuilder,
    ReportSection,
    build_report,
    read_report,
    render_markdown,
    render_summary_table,
    write_report,
)

BASE_TIMESTAMP = pd.Timestamp("2024-01-01T00:00:00Z")
FIXED_TIME = datetime(2024, 6, 1, 12, 30, tzinfo=DATETIME_UTC)


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


def make_trade(index: int, *, pnl: float = 10.0, pnl_pct: float = 0.01) -> TradeRecord:
    """Deterministic one-hour trade built on a synthetic hourly timeline."""
    return TradeRecord(
        entry_time=BASE_TIMESTAMP + pd.Timedelta(hours=index),
        exit_time=BASE_TIMESTAMP + pd.Timedelta(hours=index + 1),
        entry_price=100.0,
        exit_price=100.0 + pnl,
        size=1.0,
        direction=Direction.LONG if pnl >= 0 else Direction.SHORT,
        pnl=float(pnl),
        pnl_pct=float(pnl_pct),
        fees=0.25,
        exit_reason=ExitReason.SIGNAL if pnl >= 0 else ExitReason.STOP_LOSS,
        duration_minutes=60.0,
    )


def make_result(*, n_trades: int = 2, points: int = 26) -> BacktestResult:
    """Synthetic backtest result with ``n_trades`` trades and a rising equity curve."""
    values = [1000.0 + 5.0 * step for step in range(points)]
    trades = [
        make_trade(step, pnl=10.0 if step % 2 == 0 else -5.0)
        for step in range(min(n_trades, points - 1))
    ]
    index = pd.DatetimeIndex(make_equity(values).index)
    return BacktestResult(
        strategy_name="basic",
        symbol="BTC/USDT",
        timeframe="1h",
        start=index[0],
        end=index[-1],
        initial_balance=1000.0,
        final_balance=float(values[-1]),
        trades=trades,
        equity_curve=make_equity(values),
        params={"ema_fast": 12},
        metadata={},
    )


def fixed_report() -> Report:
    """Report with a fixed ``generated_at`` used by the rendering assertions."""
    builder = ReportBuilder(title="Backtest Report", generated_at=FIXED_TIME)
    builder.add_result(make_result(n_trades=2))
    builder.add_metrics({"total_return": 0.25, "n_trades": 2.0, "profit_factor": float("inf")})
    builder.add_validation("walk_forward", {"n_windows": 3, "mean_return": 0.0123456789})
    builder.add_section("rows", [{"b": 2.0, "a": 1.0}, {"a": 3.0, "c": "x"}])
    builder.add_section("empty", {})
    return builder.build()


# --- ReportBuilder ----------------------------------------------------------


def test_builder_methods_are_fluent_and_return_self() -> None:
    builder = ReportBuilder(generated_at=FIXED_TIME)
    result = make_result()

    assert builder.add_metrics({"a": 1.0}) is builder
    assert builder.add_result(result) is builder
    assert builder.add_trades(result.trades) is builder
    assert builder.add_equity(result.equity_curve) is builder
    assert builder.add_validation("walk_forward", {"n_windows": 1}) is builder
    assert builder.add_section("extra", {"x": 1}) is builder
    assert builder.add_note("hello") is builder


def test_builder_section_titles_follow_insertion_order() -> None:
    result = make_result()
    report = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_result(result)
        .add_validation("walk_forward", {"n_windows": 2})
        .add_trades(result.trades, limit=1)
        .add_equity(result.equity_curve)
        .build()
    )

    assert report.section_titles == ["walk_forward", "Trades", "Equity curve"]
    assert report.get_section("Trades") is not None
    assert report.get_section("missing") is None


def test_add_metrics_accepts_a_metric_set_and_a_plain_mapping() -> None:
    metrics = compute_metrics(make_result())
    report = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_metrics(metrics)
        .add_metrics({"total_return": 0.5})
        .build()
    )

    assert set(report.summary) == set(METRIC_NAMES)
    assert report.summary["total_return"] == 0.5
    assert report.summary["n_trades"] == 2.0
    assert isinstance(report.summary["sharpe_ratio"], float)


def test_add_metrics_accepts_a_bare_metric_set_value_mapping() -> None:
    builder = ReportBuilder(generated_at=FIXED_TIME)

    builder.add_metrics(MetricSet(values={"zzz": 1.0, "aaa": 2.0}))

    assert builder.build().summary == {"aaa": 2.0, "zzz": 1.0}


def test_add_result_populates_the_expected_summary_keys_and_stores_trades() -> None:
    result = make_result(n_trades=3)
    builder = ReportBuilder(generated_at=FIXED_TIME)

    builder.add_result(result)

    summary = builder.build().summary
    assert set(summary) == {
        "symbol",
        "timeframe",
        "strategy_name",
        "start",
        "end",
        "initial_balance",
        "final_balance",
        "n_trades",
    }
    assert summary["symbol"] == "BTC/USDT"
    assert summary["timeframe"] == "1h"
    assert summary["strategy_name"] == "basic"
    assert summary["start"] == result.start.isoformat()
    assert summary["end"] == result.end.isoformat()
    assert summary["initial_balance"] == 1000.0
    assert summary["final_balance"] == result.final_balance
    assert summary["n_trades"] == 3
    assert builder.trades == list(result.trades)


def test_add_trades_respects_the_limit() -> None:
    result = make_result(n_trades=25)
    section = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_trades(result.trades, limit=10)
        .build()
        .get_section("Trades")
    )

    assert section is not None
    assert len(section.body) == 10
    assert set(section.body[0]) == {
        "entry_time",
        "exit_time",
        "entry_price",
        "exit_price",
        "size",
        "direction",
        "pnl",
        "pnl_pct",
        "exit_reason",
        "duration_minutes",
    }
    assert section.body[0]["direction"] == "long"
    assert section.body[0]["entry_time"] == BASE_TIMESTAMP.isoformat()


def test_add_trades_default_limit_is_fifty() -> None:
    trades = [make_trade(step) for step in range(60)]
    section = (
        ReportBuilder(generated_at=FIXED_TIME).add_trades(trades).build().get_section("Trades")
    )

    assert section is not None
    assert len(section.body) == 50


def test_add_equity_downsamples_keeping_first_and_last_points() -> None:
    equity = make_equity([1000.0 + step for step in range(500)])
    section = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_equity(equity, max_points=200)
        .build()
        .get_section("Equity curve")
    )

    assert section is not None
    body = section.body
    assert len(body["timestamps"]) == 200
    assert len(body["values"]) == 200
    assert body["timestamps"][0] == equity.index[0].isoformat()
    assert body["timestamps"][-1] == equity.index[-1].isoformat()
    assert body["values"][0] == 1000.0
    assert body["values"][-1] == 1499.0
    assert body["values"] == sorted(body["values"])


def test_add_equity_on_an_empty_curve_and_with_disabled_downsampling() -> None:
    index = pd.DatetimeIndex([], tz=UTC, name=OHLCV_INDEX_NAME)
    empty = pd.Series([], index=index, name="equity", dtype="float64")
    builder = ReportBuilder(generated_at=FIXED_TIME)

    builder.add_equity(empty)
    builder.add_equity(make_equity([1000.0, 1001.0, 1002.0, 1003.0]), max_points=0)
    report = builder.build()

    empty_section = report.get_section("Equity curve")
    assert empty_section is not None
    assert empty_section.body == {"timestamps": [], "values": []}
    assert len(report.sections) == 2
    assert report.sections[1].body["values"] == [1000.0, 1001.0, 1002.0, 1003.0]


def test_add_equity_keeps_every_point_when_the_curve_is_short_enough() -> None:
    equity = make_equity([1000.0, 1010.0, 1020.0])
    section = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_equity(equity, max_points=200)
        .build()
        .get_section("Equity curve")
    )

    assert section is not None
    assert section.body["timestamps"] == [stamp.isoformat() for stamp in equity.index]
    assert section.body["values"] == [1000.0, 1010.0, 1020.0]


def test_add_validation_creates_a_section_titled_exactly_like_the_payload_name() -> None:
    payload = {"n_windows": 4, "mean_return": 0.0123}
    section = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_validation("walk_forward", payload)
        .build()
        .get_section("walk_forward")
    )

    assert section is not None
    assert section.title == "walk_forward"
    assert section.body == payload
    assert section.level == 2


def test_add_section_supports_sequence_bodies_and_level_three() -> None:
    builder = ReportBuilder(generated_at=FIXED_TIME)

    builder.add_section("detail", [{"a": 1.0}], level=3)

    section = builder.build().get_section("detail")
    assert section is not None
    assert section.level == 3
    assert section.body == [{"a": 1.0}]


def test_add_note_creates_a_notes_section() -> None:
    report = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_validation("walk_forward", {"n_windows": 1})
        .add_note("first note")
        .add_note("second note")
        .build()
    )

    assert report.section_titles == ["walk_forward", "Notes"]
    notes = report.get_section("Notes")
    assert notes is not None
    assert notes.body == {"notes": ["first note", "second note"]}


# --- build_report -----------------------------------------------------------


def test_build_report_with_extras_produces_sections_in_sorted_key_order() -> None:
    result = make_result(n_trades=4)
    walk_forward = {"n_windows": 3, "mean_return": 0.01}
    monte_carlo = {"n_simulations": 100, "p_value": 0.05}

    report = build_report(
        result=result,
        extras={"walk_forward": walk_forward, "monte_carlo": monte_carlo},
        generated_at=FIXED_TIME,
    )

    assert report.section_titles == ["monte_carlo", "walk_forward", "Trades", "Equity curve"]
    assert report.get_section("walk_forward").body == walk_forward
    assert report.get_section("monte_carlo").body == monte_carlo
    assert report.summary["symbol"] == "BTC/USDT"
    assert set(METRIC_NAMES).issubset(set(report.summary))


def test_build_report_flags_disable_trades_and_equity_sections() -> None:
    report = build_report(
        result=make_result(),
        extras={"walk_forward": {"n_windows": 2}},
        include_trades=False,
        include_equity=False,
        generated_at=FIXED_TIME,
    )

    assert report.section_titles == ["walk_forward"]


def test_build_report_accepts_precomputed_metrics_and_a_trade_limit() -> None:
    result = make_result(n_trades=8)
    metrics = compute_metrics(result)

    report = build_report(
        result=result,
        metrics=metrics,
        trade_limit=3,
        generated_at=FIXED_TIME,
    )

    assert report.summary["sharpe_ratio"] == pytest.approx(metrics["sharpe_ratio"])
    trades = report.get_section("Trades")
    assert trades is not None
    assert len(trades.body) == 3


def test_build_report_passes_the_config_echo_into_the_metadata() -> None:
    report = build_report(
        result=make_result(),
        config_echo={"symbol": "BTC/USDT", "timeframe": "1h"},
        generated_at=FIXED_TIME,
    )

    assert report.metadata == {"config": {"symbol": "BTC/USDT", "timeframe": "1h"}}


def test_build_report_uses_the_result_timeframe_for_annualisation() -> None:
    result = make_result(points=3)
    assert result.timeframe == "1h"

    report = build_report(result=result, generated_at=FIXED_TIME)

    assert report.summary["volatility"] == pytest.approx(compute_metrics(result)["volatility"])


# --- Report model -----------------------------------------------------------


def test_report_section_to_dict_and_report_to_dict() -> None:
    section = ReportSection(
        title="t", level=3, body=[{"b": 1, "a": pd.Timestamp("2024-01-01T00:00:00Z")}]
    )

    assert section.to_dict() == {
        "title": "t",
        "level": 3,
        "body": [{"a": "2024-01-01T00:00:00+00:00", "b": 1}],
    }


def test_report_to_dict_coerces_numpy_scalars_enums_and_opaque_objects() -> None:
    report = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_section(
            "exotic",
            {
                "count": np.int64(7),
                "ratio": np.float32(0.5),
                "direction": Direction.LONG,
                "opaque": object(),
            },
        )
        .build()
    )

    body = report.to_dict()["sections"][0]["body"]

    assert body["count"] == 7
    assert isinstance(body["count"], int)
    assert body["ratio"] == pytest.approx(0.5)
    assert isinstance(body["ratio"], float)
    assert body["direction"] == "long"
    assert isinstance(body["opaque"], str)
    assert json.dumps(report.to_dict())


def test_report_to_dict_is_json_serialisable_without_a_default_encoder() -> None:
    report = build_report(
        result=make_result(),
        extras={
            "walk_forward": {
                "n_windows": 3,
                "start": BASE_TIMESTAMP,
                "window_returns": [0.01, 0.02],
            }
        },
        config_echo={"cache_dir": Path("/tmp/cache"), "symbol": "BTC/USDT"},
        generated_at=FIXED_TIME,
    )

    payload = report.to_dict()

    assert payload["title"] == "Backtest Report"
    assert payload["generated_at"] == FIXED_TIME.isoformat()
    assert json.dumps(payload)  # no default=str: everything must already be JSON-native
    assert payload["metadata"]["config"]["cache_dir"] == "/tmp/cache"
    assert payload["sections"][0]["body"]["start"] == BASE_TIMESTAMP.isoformat()


def test_report_generated_at_is_always_utc_aware() -> None:
    naive = ReportBuilder(title="x", generated_at=datetime(2024, 6, 1, 12, 30)).build()
    offset = ReportBuilder(
        title="x",
        generated_at=datetime(2024, 6, 1, 14, 30, tzinfo=timezone(timedelta(hours=2))),
    ).build()

    assert naive.generated_at == FIXED_TIME
    assert naive.generated_at.tzinfo is not None
    assert offset.generated_at == FIXED_TIME
    assert offset.generated_at.isoformat() == "2024-06-01T12:30:00+00:00"


def test_report_to_json_round_trips_through_json_loads() -> None:
    report = fixed_report()

    payload = json.loads(report.to_json())

    assert payload == report.to_dict()
    assert payload["title"] == "Backtest Report"


# --- Markdown rendering -----------------------------------------------------


def test_markdown_starts_with_the_title_and_the_generation_line() -> None:
    markdown = render_markdown(fixed_report())

    assert markdown.startswith("# Backtest Report\n")
    assert "\n_Generated at 2024-06-01T12:30:00+00:00_\n" in markdown


def test_markdown_contains_the_summary_table_header_and_sorted_metrics() -> None:
    markdown = render_markdown(fixed_report())

    assert "## Summary\n\n| Metric | Value |\n| --- | --- |" in markdown
    assert "| n_trades | 2 |" in markdown
    assert "| total_return | 0.2500 |" in markdown
    assert "| profit_factor | inf |" in markdown
    assert "| symbol | BTC/USDT |" in markdown
    metrics_block = markdown.split("## Summary", 1)[1].split("## ", 1)[0]
    rows = [line for line in metrics_block.splitlines() if line.startswith("| ")][2:]
    names = [row.split("|")[1].strip() for row in rows]
    assert names == sorted(names)


def test_render_summary_table_formats_counts_floats_and_infinities() -> None:
    table = render_summary_table(
        {"b": 2.5, "n_trades": 4.0, "profit_factor": float("inf"), "a": 1.0}
    )

    assert table == "\n".join(
        [
            "| Metric | Value |",
            "| --- | --- |",
            "| a | 1.0000 |",
            "| b | 2.5000 |",
            "| n_trades | 4 |",
            "| profit_factor | inf |",
        ]
    )


def test_render_summary_table_on_an_empty_mapping_is_just_the_header() -> None:
    assert render_summary_table({}) == "| Metric | Value |\n| --- | --- |"


def test_markdown_renders_mapping_bodies_as_bullets_with_one_nested_level() -> None:
    markdown = render_markdown(fixed_report())

    assert "## walk_forward\n\n- mean_return: 0.0123\n- n_windows: 3" in markdown


def test_markdown_renders_nested_mappings_indented() -> None:
    report = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_section("nested", {"outer": {"inner": 1.5}, "flat": 2})
        .build()
    )

    markdown = render_markdown(report)

    assert "## nested\n\n- flat: 2\n- outer:\n  - inner: 1.5000" in markdown


def test_markdown_renders_sequence_bodies_as_tables_with_sorted_columns() -> None:
    markdown = render_markdown(fixed_report())

    assert "## rows\n\n| a | b | c |\n| --- | --- | --- |" in markdown
    assert "| 1.0000 | 2.0000 |  |" in markdown
    assert "| 3.0000 |  | x |" in markdown


def test_markdown_renders_an_empty_body_as_the_placeholder() -> None:
    markdown = render_markdown(fixed_report())

    assert "## empty\n\n_(empty)_" in markdown


def test_markdown_renders_empty_sequence_and_keyless_row_bodies_as_the_placeholder() -> None:
    report = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_section("no_rows", [])
        .add_section("blank_rows", [{}])
        .build()
    )

    markdown = render_markdown(report)

    assert "## no_rows\n\n_(empty)_" in markdown
    assert "## blank_rows\n\n_(empty)_" in markdown


def test_markdown_handles_booleans_sets_nan_and_deeply_nested_mappings() -> None:
    report = (
        ReportBuilder(generated_at=FIXED_TIME)
        .add_section(
            "exotic",
            {
                "flag": False,
                "tags": {"b", "a"},
                "ratio": float("nan"),
                "outer": {"inner": {"x": 1.0}},
            },
        )
        .build()
    )

    markdown = render_markdown(report)

    assert "- flag: false" in markdown
    assert "- tags: a, b" in markdown
    assert "- ratio: nan" in markdown
    assert "- outer:\n  - inner: x=1.0000" in markdown


def test_markdown_renders_a_level_three_section_as_a_third_level_heading() -> None:
    report = ReportBuilder(generated_at=FIXED_TIME).add_section("deep", {"x": 1}, level=3).build()

    assert "### deep\n\n- x: 1" in render_markdown(report)


def test_markdown_renders_metadata_as_bullets_when_present() -> None:
    report = build_report(
        result=make_result(),
        config_echo={"symbol": "BTC/USDT"},
        generated_at=FIXED_TIME,
    )

    markdown = render_markdown(report)

    assert "## Metadata\n\n- config:\n  - symbol: BTC/USDT" in markdown


def test_markdown_has_no_trailing_whitespace_and_exactly_one_trailing_newline() -> None:
    markdown = render_markdown(fixed_report())

    assert markdown.endswith("\n")
    assert not markdown.endswith("\n\n")
    for line in markdown.splitlines():
        assert line == line.rstrip(), repr(line)


def test_markdown_is_byte_identical_across_two_calls() -> None:
    report = fixed_report()

    assert render_markdown(report) == render_markdown(report)
    assert report.to_markdown() == report.to_markdown()


def test_markdown_of_a_report_without_sections_or_metadata_is_still_valid() -> None:
    markdown = render_markdown(Report(title="Empty", generated_at=FIXED_TIME))

    assert markdown == (
        "# Empty\n\n_Generated at 2024-06-01T12:30:00+00:00_\n\n## Summary\n\n"
        "| Metric | Value |\n| --- | --- |\n"
    )


# --- writer -----------------------------------------------------------------


def test_write_report_default_writes_markdown_then_json(tmp_path: Path) -> None:
    report = fixed_report()

    paths = write_report(report, tmp_path / "reports")

    assert [path.name for path in paths] == ["report.md", "report.json"]
    assert all(path.exists() for path in paths)
    assert paths[0].read_text(encoding="utf-8") == report.to_markdown()
    assert json.loads(paths[1].read_text(encoding="utf-8")) == report.to_dict()


def test_write_report_single_format_and_custom_basename(tmp_path: Path) -> None:
    report = fixed_report()

    markdown_paths = write_report(report, tmp_path, formats=("markdown",), basename="run-1")
    json_paths = write_report(report, tmp_path, formats=("json",), basename="run-2")

    assert [path.name for path in markdown_paths] == ["run-1.md"]
    assert [path.name for path in json_paths] == ["run-2.json"]
    assert json_paths[0].read_text(encoding="utf-8") == report.to_json()


def test_write_report_respects_the_requested_order(tmp_path: Path) -> None:
    paths = write_report(fixed_report(), tmp_path, formats=("json", "markdown"))

    assert [path.suffix for path in paths] == [".json", ".md"]


def test_write_report_overwrites_existing_files(tmp_path: Path) -> None:
    (tmp_path / "report.md").write_text("stale", encoding="utf-8")
    report = fixed_report()

    paths = write_report(report, tmp_path, formats=("markdown",))

    assert paths[0].read_text(encoding="utf-8") == report.to_markdown()


def test_write_report_rejects_an_unknown_format_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "never-created"

    with pytest.raises(ReportingError) as error:
        write_report(fixed_report(), target, formats=("markdown", "pdf"))

    assert "pdf" in str(error.value)
    assert not target.exists()


def test_write_report_rejects_an_empty_format_list_without_creating_the_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "never-created"

    with pytest.raises(ReportingError):
        write_report(fixed_report(), target, formats=())

    assert not target.exists()


def test_read_report_round_trips_a_written_json_file(tmp_path: Path) -> None:
    report = fixed_report()
    paths = write_report(report, tmp_path, formats=("json",))

    payload = read_report(paths[0])

    assert payload == json.loads(report.to_json())
    assert read_report(str(paths[0])) == payload


def test_read_report_on_a_missing_file_raises_reporting_error(tmp_path: Path) -> None:
    with pytest.raises(ReportingError):
        read_report(tmp_path / "missing.json")


def test_read_report_on_a_corrupt_file_raises_reporting_error(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")

    with pytest.raises(ReportingError):
        read_report(corrupt)


def test_read_report_on_a_non_object_payload_raises_reporting_error(tmp_path: Path) -> None:
    array = tmp_path / "array.json"
    array.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ReportingError):
        read_report(array)


# --- end-to-end smoke test --------------------------------------------------


def test_full_report_smoke_test_on_twenty_five_trades_is_json_serialisable(tmp_path: Path) -> None:
    result = make_result(n_trades=25, points=101)

    report = build_report(
        result=result,
        extras={"walk_forward": {"n_windows": 5}, "monte_carlo": [{"p_value": 0.05}]},
        config_echo={"symbol": "BTC/USDT"},
        generated_at=FIXED_TIME,
    )

    assert report.summary["n_trades"] == 25.0
    trades = report.get_section("Trades")
    assert trades is not None
    assert len(trades.body) == 25

    payload = json.loads(report.to_json())
    assert json.dumps(report.to_dict())
    assert payload["summary"]["symbol"] == "BTC/USDT"

    paths = write_report(report, tmp_path)
    assert read_report(paths[1]) == payload
    markdown = paths[0].read_text(encoding="utf-8")
    assert markdown.startswith("# Backtest Report\n")
    assert "| Metric | Value |" in markdown
    assert markdown.endswith("\n")

    metrics = compute_metrics(result)
    assert math.isfinite(report.summary["sharpe_ratio"])
    assert report.summary["sharpe_ratio"] == pytest.approx(metrics["sharpe_ratio"])


# --- layer independence -----------------------------------------------------


def test_reporting_sources_never_import_the_validation_or_strategy_layers() -> None:
    package_dir = Path(reporting_package.__file__).parent

    for module in sorted(package_dir.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        assert "trading_platform.validation" not in source, module.name
        assert "trading_platform.strategy" not in source, module.name


def test_build_report_runs_in_a_fresh_interpreter_without_the_validation_layer(
    tmp_path: Path,
) -> None:
    source_root = Path(reporting_package.__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import sys
        from datetime import datetime, timezone
        from pathlib import Path

        import pandas as pd

        from trading_platform.core.models import BacktestResult, TradeRecord, Direction, ExitReason
        from trading_platform.reporting import build_report, write_report

        index = pd.date_range("2024-01-01T00:00:00Z", periods=3, freq="h", tz="UTC")
        equity = pd.Series([1000.0, 1010.0, 1020.0], index=index, name="equity")
        trade = TradeRecord(
            entry_time=index[0],
            exit_time=index[1],
            entry_price=100.0,
            exit_price=110.0,
            size=1.0,
            direction=Direction.LONG,
            pnl=10.0,
            pnl_pct=0.01,
            fees=0.1,
            exit_reason=ExitReason.SIGNAL,
            duration_minutes=60.0,
        )
        result = BacktestResult(
            strategy_name="basic",
            symbol="BTC/USDT",
            timeframe="1h",
            start=index[0],
            end=index[-1],
            initial_balance=1000.0,
            final_balance=1020.0,
            trades=[trade],
            equity_curve=equity,
            params={},
            metadata={},
        )
        report = build_report(
            result=result,
            extras={"walk_forward": {"n_windows": 2}, "monte_carlo": {"n_simulations": 10}},
            generated_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        )
        paths = write_report(report, Path(sys.argv[1]))
        assert [path.name for path in paths] == ["report.md", "report.json"]
        assert "trading_platform.validation" not in sys.modules
        assert "trading_platform.strategy" not in sys.modules
        print("ok")
        """
    )
    env = {**os.environ, "PYTHONPATH": str(source_root)}

    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout
