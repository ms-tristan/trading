"""Tests for the random-entry (``random_entry``) section of the reporting layer.

The random-entry gate payload travels through the **existing** ``extras`` channel
of :func:`~trading_platform.reporting.builder.build_report`: no reporting API is
added for it, the section is titled exactly like its ``extras`` key and it is
inserted after the other sorted ``extras`` sections and before the
``Benchmark``/``Trades``/``Equity curve`` ones.

The payload is *opaque data* for the reporting layer, so these tests build it by
hand in the exact shape produced by
``trading_platform.validation.random_entry.RandomEntryGateResult.to_dict()``
(top-level scalars, a nested ``percentiles`` mapping and a nested ``distribution``
mapping holding the per-simulation ``returns`` list).  The validation and metrics
implementations are therefore never imported here and the tests stay offline,
deterministic and independent from the other work packages.
"""

from __future__ import annotations

import json
from datetime import UTC as DATETIME_UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from trading_platform.core.constants import OHLCV_INDEX_NAME, UTC
from trading_platform.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    TradeRecord,
)
from trading_platform.reporting import build_report, read_report, render_markdown, write_report

BASE_TIMESTAMP = pd.Timestamp("2024-01-01T00:00:00Z")
FIXED_TIME = datetime(2024, 6, 1, 12, 30, tzinfo=DATETIME_UTC)

#: Keys of ``Report.summary`` derived from the :class:`BacktestResult` itself.
RESULT_SUMMARY_KEYS = {
    "symbol",
    "timeframe",
    "strategy_name",
    "start",
    "end",
    "initial_balance",
    "final_balance",
    "n_trades",
}

#: The five per-simulation total returns of the synthetic distribution.
SIMULATED_RETURNS = [-0.03, 0.0, 0.012, 0.02, 0.09]


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


def random_entry_payload() -> dict[str, Any]:
    """Opaque ``random_entry`` payload, shaped like ``RandomEntryGateResult.to_dict()``.

    The nested ``percentiles`` mapping and the nested ``distribution`` mapping
    (which itself holds the per-simulation ``returns``/``final_balances`` lists)
    are the two shapes a JSON/markdown renderer must not flatten away.
    """
    return {
        "n_simulations": 5,
        "random_seed": 42,
        "n_trades": 2,
        "holding_periods": 1,
        "exposure": 0.25,
        "initial_balance": 1000.0,
        "timeframe": "1h",
        "strategy_total_return": 0.4712,
        "mean_return": 0.02,
        "median_return": 0.012,
        "std_return": 0.04,
        "percentile": 97.5,
        "p_value": 0.025,
        "min_p_value": 0.05,
        "strategy_beats_random": True,
        "percentiles": {"p05": -0.03, "p50": 0.012, "p95": 0.09},
        "distribution": {
            "variant": "random_entry",
            "n_simulations": 5,
            "random_seed": 42,
            "strategy_total_return": 0.4712,
            "returns": list(SIMULATED_RETURNS),
            "final_balances": [970.0, 1000.0, 1012.0, 1020.0, 1090.0],
            "percentiles": {"p05": -0.03, "p50": 0.012, "p95": 0.09},
            "percentile": 97.5,
            "p_value": 0.025,
        },
    }


def benchmark_rows() -> list[dict[str, object]]:
    """Minimal explicit benchmark payload (the dedicated 3-row table)."""
    return [
        {"variant": "strategy", "total_return": 0.4712},
        {"variant": "buy_and_hold", "total_return": 0.4},
        {"variant": "gap", "total_return": 0.0712},
    ]


def random_entry_report(**kwargs: object) -> Any:
    """``build_report`` on the synthetic result, with the random-entry payload."""
    options: dict[str, object] = {
        "metrics": {"total_return": 0.4712},
        "extras": {"random_entry": random_entry_payload()},
        "generated_at": FIXED_TIME,
    }
    options.update(kwargs)
    return build_report(result=make_result(), **options)  # type: ignore[arg-type]


# --- Section placement through the extras channel ---------------------------


def test_build_report_places_random_entry_after_sorted_extras_and_before_benchmark() -> None:
    report = random_entry_report(
        extras={"data_quality": {"n_gaps": 0}, "random_entry": random_entry_payload()},
        benchmark=benchmark_rows(),
    )

    assert report.section_titles == [
        "data_quality",
        "random_entry",
        "Benchmark",
        "Trades",
        "Equity curve",
    ]


def test_build_report_without_benchmark_keeps_random_entry_before_trades_and_equity() -> None:
    report = random_entry_report(benchmark=None)

    assert report.section_titles == ["random_entry", "Trades", "Equity curve"]
    assert report.get_section("Benchmark") is None


def test_random_entry_section_body_is_the_mapping_passed_in() -> None:
    payload = random_entry_payload()
    report = build_report(
        result=make_result(),
        metrics={"total_return": 0.4712},
        extras={"random_entry": payload},
        generated_at=FIXED_TIME,
    )

    section = report.get_section("random_entry")
    assert section is not None
    assert section.title == "random_entry"
    assert section.level == 2
    assert section.body == payload
    assert isinstance(section.body, dict)
    assert section.body["percentiles"] == {"p05": -0.03, "p50": 0.012, "p95": 0.09}
    assert section.body["distribution"]["returns"] == SIMULATED_RETURNS


def test_random_entry_section_copies_the_top_level_keys_defensively() -> None:
    payload = random_entry_payload()
    report = build_report(
        result=make_result(),
        metrics={"total_return": 0.4712},
        extras={"random_entry": payload},
        generated_at=FIXED_TIME,
    )
    payload["p_value"] = 0.99

    section = report.get_section("random_entry")
    assert section is not None
    body = section.body
    assert isinstance(body, dict)
    assert body["p_value"] == 0.025


def test_exactly_one_section_is_titled_benchmark_with_the_random_entry_payload() -> None:
    report = random_entry_report(benchmark=benchmark_rows())

    assert report.section_titles.count("Benchmark") == 1
    benchmark = report.get_section("Benchmark")
    assert benchmark is not None
    assert benchmark.body == benchmark_rows()
    assert len(benchmark.body) == 3


# --- Frozen summary ---------------------------------------------------------


def test_summary_is_unchanged_by_the_random_entry_section() -> None:
    report = random_entry_report(benchmark=benchmark_rows())

    assert "random_entry" not in report.summary
    assert set(report.summary) == {"total_return"} | RESULT_SUMMARY_KEYS


# --- JSON rendering / writer -------------------------------------------------


def test_to_json_keeps_the_random_entry_distribution() -> None:
    report = random_entry_report()
    payload = json.loads(report.to_json())

    sections = payload["sections"]
    assert [section["title"] for section in sections] == [
        "random_entry",
        "Trades",
        "Equity curve",
    ]
    random_entry = sections[0]
    assert random_entry["level"] == 2
    body = random_entry["body"]
    assert body["percentile"] == 97.5
    assert body["p_value"] == 0.025
    assert body["strategy_beats_random"] is True
    assert body["percentiles"] == {"p05": -0.03, "p50": 0.012, "p95": 0.09}
    assert body["distribution"]["returns"] == SIMULATED_RETURNS
    assert body["distribution"]["final_balances"] == [970.0, 1000.0, 1012.0, 1020.0, 1090.0]
    assert body["distribution"]["percentiles"]["p95"] == 0.09


def test_write_report_and_read_report_round_trip_the_random_entry_section(
    tmp_path: Path,
) -> None:
    written = write_report(random_entry_report(), tmp_path, formats=("markdown", "json"))

    assert [path.suffix for path in written] == [".md", ".json"]
    assert all(path.exists() for path in written)

    payload = read_report(tmp_path / "report.json")
    titles = [section["title"] for section in payload["sections"]]
    assert titles == ["random_entry", "Trades", "Equity curve"]
    body = payload["sections"][titles.index("random_entry")]["body"]
    assert body["percentile"] == 97.5
    assert body["p_value"] == 0.025
    assert body["strategy_beats_random"] is True
    assert body["percentiles"] == {"p05": -0.03, "p50": 0.012, "p95": 0.09}
    assert body["distribution"]["returns"] == SIMULATED_RETURNS


def test_to_dict_is_dumps_safe_with_the_random_entry_payload() -> None:
    report = random_entry_report()

    assert json.dumps(report.to_dict(), default=str)


# --- Markdown rendering ------------------------------------------------------


def test_render_markdown_contains_the_random_entry_section() -> None:
    markdown = render_markdown(random_entry_report())

    assert "## random_entry" in markdown
    assert "- percentile: 97.5000" in markdown
    assert "- p_value: 0.0250" in markdown
    assert "- min_p_value: 0.0500" in markdown
    assert "- strategy_beats_random: true" in markdown
    assert "- n_simulations: 5" in markdown


def test_render_markdown_renders_the_nested_distribution_and_returns_list() -> None:
    markdown = render_markdown(random_entry_report())

    # A nested mapping becomes an indented bullet block...
    assert "- distribution:" in markdown
    assert "  - returns: -0.0300, 0.0000, 0.0120, 0.0200, 0.0900" in markdown
    assert "  - percentiles: p05=-0.0300, p50=0.0120, p95=0.0900" in markdown
    assert "  - strategy_total_return: 0.4712" in markdown
    # ... and the top-level percentiles mapping is rendered inline.
    assert "- percentiles: p05=-0.0300, p50=0.0120, p95=0.0900" in markdown


def test_render_markdown_with_the_benchmark_still_shows_the_random_entry_section() -> None:
    markdown = render_markdown(random_entry_report(benchmark=benchmark_rows()))

    assert "## random_entry" in markdown
    assert "## Benchmark" in markdown
    assert markdown.index("## random_entry") < markdown.index("## Benchmark")
    assert markdown.index("## Benchmark") < markdown.index("## Trades")


def test_render_markdown_is_deterministic_with_the_random_entry_section() -> None:
    report = random_entry_report()

    assert render_markdown(report) == render_markdown(report)


def test_render_markdown_ends_with_one_newline_and_no_trailing_space() -> None:
    markdown = render_markdown(random_entry_report())

    assert markdown.endswith("\n")
    assert not markdown.endswith("\n\n")
    assert all(line == line.rstrip() for line in markdown.splitlines())
