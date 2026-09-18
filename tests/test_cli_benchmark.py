"""End-to-end tests of the benchmark wiring of the CLI (work package wp-5).

Every test is **offline** and deterministic: the OHLCV window comes from
``tests/fixtures/BTC_USDT-1h.csv`` (200 synthetic, rising candles: 100.00 ->
107.86), the report directory is a ``tmp_path`` and no test ever touches the
network.  The ``PAYLOAD_KEYS`` contract is re-declared here on purpose so that
this file stays independent from ``tests/test_cli.py``.

The two flags added by this work package are covered here as well:
``--risk-free-rate`` (the annual rate subtracted from the annualised mean return
by ``sharpe_ratio``/``sortino_ratio`` and compounded by the ``risk_free`` curve)
and ``--benchmark-variant`` (``buy_and_hold``, ``cash``, ``risk_free``,
``random_entry`` or ``none``).  Every random-entry test runs on a temporary
configuration holding ``benchmark.n_random_simulations = 50`` so that the suite
stays fast; the seed stays the configured ``benchmark.random_entry_seed`` (42),
which is what makes the distribution reproducible.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trading_platform.cli import app
from trading_platform.config import dump_config, load_config
from trading_platform.reporting import read_report

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = str(REPO_ROOT / "config" / "backtest_default.json")
FIXTURE = str(REPO_ROOT / "tests" / "fixtures" / "BTC_USDT-1h.csv")
FIXTURE_ROWS = 200

#: The documented JSON payload keys, in the documented order (re-declared here:
#: ``tests/test_cli.py`` asserts the very same tuple verbatim).
PAYLOAD_KEYS = (
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

#: Commands whose human output is the metrics table plus the run highlights.
RUN_COMMANDS = ("backtest", "walk-forward", "robustness", "monte-carlo")

#: Run commands exposing ``--no-network`` (monte-carlo has no such flag; it reads
#: ``--data-file`` directly and never touches the network either).
NO_NETWORK_COMMANDS = frozenset({"backtest", "walk-forward", "robustness"})

#: Run keys owned by each validation command -- none of them may disappear.
RUN_KEYS: dict[str, tuple[str, ...]] = {
    "walk-forward": ("windows", "n_windows", "mode"),
    "robustness": ("points", "n_points"),
    "monte-carlo": ("returns", "n_simulations"),
}

#: Metric columns of the side-by-side benchmark table.
BENCHMARK_METRICS = (
    "total_return",
    "cagr",
    "volatility",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "max_drawdown_duration",
    "final_balance",
)

#: Scalars every ``run["random_entry"]`` payload carries (the validation gate
#: contract: ``RandomEntryGateResult.to_dict()`` plus the full distribution).
RANDOM_ENTRY_KEYS = (
    "n_simulations",
    "random_seed",
    "n_trades",
    "holding_periods",
    "exposure",
    "initial_balance",
    "timeframe",
    "strategy_total_return",
    "mean_return",
    "median_return",
    "std_return",
    "percentiles",
    "percentile",
    "p_value",
    "min_p_value",
    "strategy_beats_random",
    "distribution",
)

#: Percentile labels of the simulated-return distribution.
PERCENTILE_LABELS = ("p05", "p25", "p50", "p75", "p95")

#: Simulations used by the random-entry tests: small enough to keep the suite fast,
#: large enough for the percentiles to be meaningful.
TEST_SIMULATIONS = 50

runner = CliRunner()

#: ANSI SGR escape sequences emitted by rich when the CLI runs in a colour-forcing
#: environment.  The captured streams are de-colourised, never the assertions relaxed.
_ANSI_SGR = re.compile(rb"\x1b\[[0-9;]*m")


def invoke(*args: str, env: dict[str, str] | None = None):
    """Invoke the CLI in the isolated ``CliRunner`` environment, without ANSI styling.

    ``env`` adds environment overrides for this one invocation: the ``--help``
    tests set a wide ``COLUMNS`` so that rich never elides a long option name.
    """
    result = runner.invoke(app, list(args), env=env)
    result.stdout_bytes = _ANSI_SGR.sub(b"", result.stdout_bytes)
    result.stderr_bytes = _ANSI_SGR.sub(b"", result.stderr_bytes)
    result.output_bytes = _ANSI_SGR.sub(b"", result.output_bytes)
    return result


def json_payload(result) -> dict[str, Any]:
    """Parse the single JSON object printed on stdout."""
    return json.loads(result.stdout)


def write_config(
    tmp_path: Path,
    *,
    variant: str | None = None,
    enabled: bool | None = None,
    risk_free_rate: float | None = None,
    n_random_simulations: int | None = None,
    name: str = "config.json",
) -> str:
    """Write the default configuration with an overridden ``benchmark`` section."""
    cfg = load_config(CONFIG)
    if variant is not None:
        cfg.benchmark.variant = variant  # type: ignore[assignment]
    if enabled is not None:
        cfg.benchmark.enabled = enabled
    if risk_free_rate is not None:
        cfg.benchmark.risk_free_rate = risk_free_rate
    if n_random_simulations is not None:
        cfg.benchmark.n_random_simulations = n_random_simulations
    return str(dump_config(cfg, tmp_path / name))


def run_json(*args: str, tmp_path: Path) -> dict[str, Any]:
    """Run one command with ``--json`` and return its parsed payload."""
    result = invoke(*args, "--json", "--output-dir", str(tmp_path))
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.stderr
    payload = json_payload(result)
    assert sorted(payload) == sorted(PAYLOAD_KEYS)  # no new top-level payload key
    assert payload["ok"] is True
    return payload


def written_reports(payload: dict[str, Any]) -> tuple[Path, Path]:
    """Return the ``(json, markdown)`` report paths written by one run."""
    written = [Path(path) for path in payload["reports"]]
    assert len(written) == 2
    for path in written:
        assert path.is_file()
    return (
        next(path for path in written if path.suffix == ".json"),
        next(path for path in written if path.suffix == ".md"),
    )


def benchmark_body(payload: dict[str, Any]) -> Any:
    """Return the body of the (unique) ``Benchmark`` section of the JSON report."""
    json_report, _ = written_reports(payload)
    report = read_report(json_report)
    sections = [section for section in report["sections"] if section["title"] == "Benchmark"]
    assert len(sections) == 1
    return sections[0]["body"]


def assert_benchmark_section(payload: dict[str, Any], *, variant: str) -> list[dict[str, Any]]:
    """Assert the JSON/markdown reports hold the side-by-side benchmark table."""
    body = benchmark_body(payload)
    assert isinstance(body, list)
    assert [row["variant"] for row in body] == ["strategy", variant, "gap"]
    for row in body:
        assert set(BENCHMARK_METRICS) <= set(row)
    _, markdown_report = written_reports(payload)
    markdown = markdown_report.read_text(encoding="utf-8")
    assert "## Benchmark" in markdown
    assert "| variant |" in markdown
    assert "| --- |" in markdown
    return body


def section_titles(payload: dict[str, Any]) -> list[str]:
    """Return the titles of the report sections, in written order."""
    json_report, _ = written_reports(payload)
    return [str(section["title"]) for section in read_report(json_report)["sections"]]


def error_text(result) -> str:
    """The message rendered on stderr, with every run of whitespace collapsed.

    ``rich`` wraps long messages at the console width, so a raw substring check
    would depend on the terminal: the captured text is normalised, never the
    assertion relaxed.
    """
    return " ".join(result.stderr.split())


def backtest_args(data_file: str = FIXTURE) -> list[str]:
    """The offline ``backtest`` arguments shared by most tests."""
    return ["backtest", "--config", CONFIG, "--data-file", data_file, "--no-network"]


def run_args(command: str, *extra: str) -> list[str]:
    """The offline arguments of ``command``, plus ``extra``."""
    args = [command, "--config", CONFIG, "--data-file", FIXTURE]
    if command in NO_NETWORK_COMMANDS:
        args.append("--no-network")
    return [*args, *extra]


# ---------------------------------------------------------------------------
# payload: the benchmark travels inside ``run``
# ---------------------------------------------------------------------------


def test_backtest_json_exposes_the_benchmark_inside_run(tmp_path: Path) -> None:
    payload = run_json(*backtest_args(), tmp_path=tmp_path)

    benchmark = payload["run"]["benchmark"]
    assert benchmark["variant"] == "buy_and_hold"
    assert isinstance(benchmark["strategy_beats_benchmark"], bool)
    assert isinstance(benchmark["alpha"], float)
    assert isinstance(benchmark["beta"], float)
    assert isinstance(benchmark["correlation"], float)
    assert benchmark["initial_balance"] == pytest.approx(payload["run"]["initial_balance"])
    assert benchmark["fee_rate"] == pytest.approx(load_config(CONFIG).exchange.fee_rate)


def test_backtest_writes_a_benchmark_section_in_both_reports(tmp_path: Path) -> None:
    payload = run_json(*backtest_args(), tmp_path=tmp_path)

    assert_benchmark_section(payload, variant="buy_and_hold")


def test_benchmark_flags_are_documented_in_every_run_command_help() -> None:
    for command in RUN_COMMANDS:
        result = invoke(command, "--help")
        # the help columns are wrapped (and long tokens elided) by rich: only the
        # flag names themselves are stable across terminal widths.
        compact = "".join(result.output.split())

        assert result.exit_code == 0, result.output
        assert "--benchmark" in compact
        assert "--no-benchmark" in compact
        assert "benchmark" in compact.lower()


def test_the_two_new_flags_are_documented_in_every_run_command_help() -> None:
    """``--risk-free-rate`` and ``--benchmark-variant`` exist on the four run commands."""
    for command in RUN_COMMANDS:
        # a wide terminal keeps rich from eliding the longer option name to
        # ``--risk-free-ra…`` (which it does at the default 80 columns)
        result = invoke(command, "--help", env={"COLUMNS": "200"})
        compact = "".join(result.output.split())

        assert result.exit_code == 0, result.output
        assert "--risk-free-rate" in compact
        assert "--benchmark-variant" in compact
        assert "risk_free" in compact.replace("\u2026", "")
        assert "random_entry" in compact.replace("\u2026", "")


# ---------------------------------------------------------------------------
# tri-state flag and configuration precedence
# ---------------------------------------------------------------------------


def test_no_benchmark_flag_disables_the_benchmark(tmp_path: Path) -> None:
    payload = run_json(*backtest_args(), "--no-benchmark", tmp_path=tmp_path)

    assert "benchmark" not in payload["run"]
    json_report, markdown_report = written_reports(payload)
    titles = {section["title"] for section in read_report(json_report)["sections"]}
    assert "Benchmark" not in titles
    assert "## Benchmark" not in markdown_report.read_text(encoding="utf-8")


def test_benchmark_flag_forces_the_benchmark_on_when_the_config_disables_it(
    tmp_path: Path,
) -> None:
    disabled = write_config(tmp_path, enabled=False)
    args = ["backtest", "--config", disabled, "--data-file", FIXTURE, "--no-network"]

    assert "benchmark" not in run_json(*args, tmp_path=tmp_path)["run"]
    assert "benchmark" in run_json(*args, "--benchmark", tmp_path=tmp_path)["run"]


def test_config_enabled_false_disables_the_benchmark(tmp_path: Path) -> None:
    disabled = write_config(tmp_path, enabled=False)

    payload = run_json(
        "backtest", "--config", disabled, "--data-file", FIXTURE, "--no-network", tmp_path=tmp_path
    )

    assert "benchmark" not in payload["run"]


def test_config_variant_none_wins_over_the_benchmark_flag(tmp_path: Path) -> None:
    """``variant='none'`` always wins: ``--benchmark`` cannot resurrect it."""
    no_variant = write_config(tmp_path, variant="none")

    payload = run_json(
        "backtest",
        "--config",
        no_variant,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--benchmark",
        tmp_path=tmp_path,
    )

    assert "benchmark" not in payload["run"]


def test_cash_variant_labels_the_row_and_carries_no_beta(tmp_path: Path) -> None:
    cash = write_config(tmp_path, variant="cash")

    payload = run_json(
        "backtest", "--config", cash, "--data-file", FIXTURE, "--no-network", tmp_path=tmp_path
    )

    assert payload["run"]["benchmark"]["variant"] == "cash"
    assert payload["run"]["benchmark"]["beta"] is None
    assert payload["run"]["benchmark"]["correlation"] is None
    body = assert_benchmark_section(payload, variant="cash")
    assert body[1]["variant"] == "cash"


def test_cash_variant_human_summary_hides_beta_and_correlation(tmp_path: Path) -> None:
    cash = write_config(tmp_path, variant="cash")

    result = invoke(
        "backtest",
        "--config",
        cash,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "  benchmark: cash" in result.output
    assert "beta" not in result.output
    assert "correlation" not in result.output


def test_invalid_benchmark_variant_is_rejected_at_load_time(tmp_path: Path) -> None:
    broken = write_config(tmp_path, variant="momentum", name="broken.json")

    result = invoke("backtest", "--config", broken, "--json", "--output-dir", str(tmp_path))

    assert result.exit_code == 1
    assert "benchmark.variant" in result.stderr.replace("\n", "")
    assert "Traceback" not in result.stderr
    failed = json_payload(result)
    assert sorted(failed) == sorted(PAYLOAD_KEYS)
    assert failed["ok"] is False
    assert failed["run"] is None


# ---------------------------------------------------------------------------
# --risk-free-rate
# ---------------------------------------------------------------------------


def test_risk_free_rate_zero_matches_the_default_configuration(tmp_path: Path) -> None:
    """The configuration default is ``0.0``: an explicit ``0.0`` changes nothing."""
    explicit = run_json(*backtest_args(), "--risk-free-rate", "0.0", tmp_path=tmp_path)
    implicit = run_json(*backtest_args(), tmp_path=tmp_path)

    assert explicit["metrics"]["values"] == implicit["metrics"]["values"]
    assert explicit["run"]["benchmark"] == implicit["run"]["benchmark"]


def test_risk_free_rate_lowers_the_sharpe_and_sortino_ratios(tmp_path: Path) -> None:
    zero = run_json(*backtest_args(), "--risk-free-rate", "0.0", tmp_path=tmp_path)
    five = run_json(*backtest_args(), "--risk-free-rate", "0.05", tmp_path=tmp_path)

    assert five["metrics"]["values"]["sharpe_ratio"] < zero["metrics"]["values"]["sharpe_ratio"]
    assert five["metrics"]["values"]["sortino_ratio"] < zero["metrics"]["values"]["sortino_ratio"]
    # ... and nothing else moves: total_return is a risk-free-rate-free quantity.
    assert five["metrics"]["values"]["total_return"] == zero["metrics"]["values"]["total_return"]


def test_risk_free_rate_leaves_alpha_bit_identical(tmp_path: Path) -> None:
    """``alpha`` is a total-return difference: the riskless rate cannot move it."""
    zero = run_json(*backtest_args(), "--risk-free-rate", "0.0", tmp_path=tmp_path)
    five = run_json(*backtest_args(), "--risk-free-rate", "0.05", tmp_path=tmp_path)

    assert five["run"]["benchmark"]["alpha"] == zero["run"]["benchmark"]["alpha"]
    assert five["run"]["benchmark"]["strategy_total_return"] == pytest.approx(
        zero["run"]["benchmark"]["strategy_total_return"]
    )


def test_the_configured_risk_free_rate_moves_the_metrics_without_a_flag(tmp_path: Path) -> None:
    """``benchmark.risk_free_rate`` is the default of ``--risk-free-rate``."""
    configured = write_config(tmp_path, risk_free_rate=0.05)
    flagged = run_json(*backtest_args(), "--risk-free-rate", "0.05", tmp_path=tmp_path)
    from_config = run_json(
        "backtest",
        "--config",
        configured,
        "--data-file",
        FIXTURE,
        "--no-network",
        tmp_path=tmp_path,
    )

    assert from_config["metrics"]["values"] == flagged["metrics"]["values"]


def test_a_negative_risk_free_rate_is_rejected(tmp_path: Path) -> None:
    result = invoke(
        *backtest_args(), "--risk-free-rate", "-0.01", "--json", "--output-dir", str(tmp_path)
    )

    assert result.exit_code == 1
    assert "--risk-free-rate must be a finite fraction >= 0, got -0.01" in error_text(result)
    assert "Traceback" not in result.stderr
    failed = json_payload(result)
    assert sorted(failed) == sorted(PAYLOAD_KEYS)
    assert failed["ok"] is False
    assert failed["run"] is None


def test_the_risk_free_rate_is_rejected_on_every_run_command(tmp_path: Path) -> None:
    for command in RUN_COMMANDS:
        result = invoke(
            *run_args(command, "--risk-free-rate", "-1.0", "--json"),
            "--output-dir",
            str(tmp_path),
        )

        assert result.exit_code == 1, result.output
        assert "--risk-free-rate must be a finite fraction >= 0" in error_text(result)
        assert "Traceback" not in result.stderr


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_a_non_finite_risk_free_rate_is_rejected(value: str, tmp_path: Path) -> None:
    result = invoke(
        *backtest_args(), "--risk-free-rate", value, "--json", "--output-dir", str(tmp_path)
    )

    assert result.exit_code == 1
    assert "--risk-free-rate must be a finite fraction >= 0" in error_text(result)
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# --benchmark-variant
# ---------------------------------------------------------------------------


def test_benchmark_variant_override_selects_the_risk_free_curve(tmp_path: Path) -> None:
    """``--benchmark-variant risk_free`` wins over the configured ``buy_and_hold``."""
    payload = run_json(
        *backtest_args(),
        "--benchmark-variant",
        "risk_free",
        "--risk-free-rate",
        "0.05",
        tmp_path=tmp_path,
    )

    assert payload["run"]["benchmark"]["variant"] == "risk_free"
    body = assert_benchmark_section(payload, variant="risk_free")
    assert body[1]["variant"] == "risk_free"
    # compounding 5 %/yr over the 200 hourly candles of the fixture really grows
    assert body[1]["total_return"] > 0.0
    assert body[1]["max_drawdown"] == pytest.approx(0.0)
    # the "gap" row is the strategy minus the risk-free placement, not a new metric
    assert body[2]["variant"] == "gap"
    # read before the next invocation overwrites the report files of this directory
    assert "Benchmark" in section_titles(payload)


def test_the_risk_free_and_cash_variants_are_distinct(tmp_path: Path) -> None:
    """``cash`` earns nothing, ``risk_free`` earns the configured rate."""
    risk_free = run_json(
        *backtest_args(),
        "--benchmark-variant",
        "risk_free",
        "--risk-free-rate",
        "0.05",
        tmp_path=tmp_path,
    )
    risk_free_body = assert_benchmark_section(risk_free, variant="risk_free")
    json_report, _ = written_reports(risk_free)
    config_echo = read_report(json_report)["metadata"]["config"]
    # the CLI override never rewrites the configuration echo
    assert config_echo["benchmark"]["variant"] == "buy_and_hold"
    assert config_echo["benchmark"]["risk_free_rate"] == 0.0

    cash = run_json(*backtest_args(), "--benchmark-variant", "cash", tmp_path=tmp_path)
    cash_body = assert_benchmark_section(cash, variant="cash")

    assert cash["run"]["benchmark"]["variant"] == "cash"
    assert cash_body[1]["total_return"] == pytest.approx(0.0)
    assert risk_free_body[1]["total_return"] > cash_body[1]["total_return"]


def test_an_unknown_benchmark_variant_override_is_rejected(tmp_path: Path) -> None:
    result = invoke(
        *backtest_args(), "--benchmark-variant", "hodl", "--json", "--output-dir", str(tmp_path)
    )

    message = error_text(result)
    assert result.exit_code == 1
    assert "unknown benchmark variant: 'hodl'" in message
    assert "available variants" in message
    assert "risk_free" in message
    assert "random_entry" in message
    assert "Traceback" not in result.stderr
    failed = json_payload(result)
    assert sorted(failed) == sorted(PAYLOAD_KEYS)
    assert failed["ok"] is False


def test_no_benchmark_beats_the_variant_override(tmp_path: Path) -> None:
    """``--no-benchmark`` can never be resurrected by ``--benchmark-variant``."""
    payload = run_json(
        *backtest_args(), "--no-benchmark", "--benchmark-variant", "risk_free", tmp_path=tmp_path
    )

    assert "benchmark" not in payload["run"]
    assert "Benchmark" not in section_titles(payload)


# ---------------------------------------------------------------------------
# --benchmark-variant random_entry (the skill test)
# ---------------------------------------------------------------------------


def random_entry_config(tmp_path: Path) -> str:
    """A configuration running :data:`TEST_SIMULATIONS` random-entry simulations."""
    return write_config(tmp_path, n_random_simulations=TEST_SIMULATIONS, name="random_entry.json")


def test_random_entry_variant_exposes_the_distribution_and_the_verdict(tmp_path: Path) -> None:
    config = random_entry_config(tmp_path)

    payload = run_json(
        "backtest",
        "--config",
        config,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--benchmark-variant",
        "random_entry",
        tmp_path=tmp_path,
    )

    run = payload["run"]
    assert "benchmark" not in run  # exactly one benchmark per run
    gate = run["random_entry"]
    assert set(RANDOM_ENTRY_KEYS) <= set(gate)
    assert gate["n_simulations"] == TEST_SIMULATIONS
    assert gate["random_seed"] == load_config(CONFIG).benchmark.random_entry_seed
    assert gate["n_trades"] == len(run["trades"]) > 0
    assert gate["holding_periods"] >= 1
    assert gate["timeframe"] == "1h"
    assert gate["initial_balance"] == pytest.approx(run["initial_balance"])
    assert set(gate["percentiles"]) == set(PERCENTILE_LABELS)
    assert 0.0 <= gate["percentile"] <= 100.0
    assert 0.0 <= gate["p_value"] <= 1.0
    assert gate["strategy_beats_random"] is (gate["p_value"] < gate["min_p_value"])

    distribution = gate["distribution"]
    assert distribution["n_simulations"] == TEST_SIMULATIONS
    assert len(distribution["returns"]) == TEST_SIMULATIONS
    assert len(distribution["final_balances"]) == TEST_SIMULATIONS
    assert distribution["mean_return"] == pytest.approx(gate["mean_return"])
    assert distribution["strategy_total_return"] == pytest.approx(gate["strategy_total_return"])
    assert distribution["percentile"] == pytest.approx(gate["percentile"])
    assert distribution["p_value"] == pytest.approx(gate["p_value"])


def test_random_entry_writes_one_report_section_and_no_benchmark_section(tmp_path: Path) -> None:
    config = random_entry_config(tmp_path)

    payload = run_json(
        "backtest",
        "--config",
        config,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--benchmark-variant",
        "random_entry",
        tmp_path=tmp_path,
    )

    titles = section_titles(payload)
    assert titles.count("random_entry") == 1
    assert "Benchmark" not in titles
    json_report, markdown_report = written_reports(payload)
    sections = [
        section
        for section in read_report(json_report)["sections"]
        if section["title"] == "random_entry"
    ]
    assert sections[0]["body"]["n_simulations"] == TEST_SIMULATIONS
    assert "## random_entry" in markdown_report.read_text(encoding="utf-8")


def test_random_entry_is_deterministic_across_two_invocations(tmp_path: Path) -> None:
    """The seed comes from ``benchmark.random_entry_seed``: the run is reproducible."""
    config = random_entry_config(tmp_path)
    args = [
        "backtest",
        "--config",
        config,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--benchmark-variant",
        "random_entry",
    ]

    first = run_json(*args, tmp_path=tmp_path)
    second = run_json(*args, tmp_path=tmp_path)

    dumped_first = json.dumps(first["run"]["random_entry"], sort_keys=True)
    dumped_second = json.dumps(second["run"]["random_entry"], sort_keys=True)
    assert dumped_first == dumped_second
    assert first["run"]["random_entry"]["random_seed"] == 42


def test_random_entry_is_not_computed_when_another_variant_is_selected(tmp_path: Path) -> None:
    config = random_entry_config(tmp_path)

    payload = run_json(
        "backtest",
        "--config",
        config,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--benchmark-variant",
        "buy_and_hold",
        tmp_path=tmp_path,
    )

    assert payload["run"]["benchmark"]["variant"] == "buy_and_hold"
    assert "random_entry" not in payload["run"]
    assert "random_entry" not in section_titles(payload)


def test_the_variant_can_be_selected_from_the_configuration_alone(tmp_path: Path) -> None:
    config = write_config(
        tmp_path, variant="random_entry", n_random_simulations=TEST_SIMULATIONS, name="re.json"
    )

    payload = run_json(
        "backtest", "--config", config, "--data-file", FIXTURE, "--no-network", tmp_path=tmp_path
    )

    assert payload["run"]["random_entry"]["n_simulations"] == TEST_SIMULATIONS
    assert "benchmark" not in payload["run"]


# ---------------------------------------------------------------------------
# human summary
# ---------------------------------------------------------------------------


def test_human_mode_prints_the_benchmark_block(tmp_path: Path) -> None:
    result = invoke(*backtest_args(), "--output-dir", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert "  benchmark: buy_and_hold" in result.output
    assert "  alpha:" in result.output
    assert "  beta:" in result.output
    assert "  correlation:" in result.output
    assert "  strategy_beats_benchmark:" in result.output


def test_human_mode_omits_the_benchmark_block_when_disabled(tmp_path: Path) -> None:
    result = invoke(*backtest_args(), "--no-benchmark", "--output-dir", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert "benchmark:" not in result.output
    assert "alpha:" not in result.output


# ---------------------------------------------------------------------------
# the verdict itself
# ---------------------------------------------------------------------------


def test_an_underperforming_strategy_does_not_beat_buy_and_hold_on_a_rising_frame(
    tmp_path: Path,
) -> None:
    """The fixture rises (+7.9%): a profitable-looking run can still destroy value."""
    payload = run_json(*backtest_args(), tmp_path=tmp_path)

    benchmark = payload["run"]["benchmark"]
    body = assert_benchmark_section(payload, variant="buy_and_hold")
    buy_and_hold_return = body[1]["total_return"]

    assert buy_and_hold_return > 0.0  # the frame really rises: doing nothing is profitable
    assert payload["metrics"]["values"]["total_return"] > 0.0  # ... and so is the strategy
    assert benchmark["alpha"] < 0.0
    assert benchmark["strategy_beats_benchmark"] is False


def test_a_strategy_beating_buy_and_hold_is_reported_as_such(tmp_path: Path) -> None:
    """The ``cash`` benchmark on a rising frame is beaten: the flag is not hard-coded."""
    cash = write_config(tmp_path, variant="cash")

    payload = run_json(
        "backtest", "--config", cash, "--data-file", FIXTURE, "--no-network", tmp_path=tmp_path
    )

    assert payload["run"]["benchmark"]["alpha"] > 0.0
    assert payload["run"]["benchmark"]["strategy_beats_benchmark"] is True


# ---------------------------------------------------------------------------
# the other run commands
# ---------------------------------------------------------------------------


def test_walk_forward_exposes_the_benchmark_and_keeps_its_own_run(tmp_path: Path) -> None:
    payload = run_json(*run_args("walk-forward", "--windows", "2"), tmp_path=tmp_path)

    run = payload["run"]
    assert set(RUN_KEYS["walk-forward"]) <= set(run)
    assert run["n_windows"] == 2
    assert len(run["windows"]) == 2
    assert run["benchmark"]["variant"] == "buy_and_hold"
    assert_benchmark_section(payload, variant="buy_and_hold")


def test_robustness_exposes_the_benchmark_and_keeps_its_own_run(tmp_path: Path) -> None:
    payload = run_json(*run_args("robustness", "--max-combinations", "4"), tmp_path=tmp_path)

    run = payload["run"]
    assert set(RUN_KEYS["robustness"]) <= set(run)
    assert run["n_points"] == len(run["points"])
    assert 1 <= run["n_points"] <= 4
    assert run["benchmark"]["variant"] == "buy_and_hold"
    assert_benchmark_section(payload, variant="buy_and_hold")


def test_monte_carlo_exposes_the_benchmark_and_keeps_its_own_run(tmp_path: Path) -> None:
    payload = run_json(*run_args("monte-carlo", "--simulations", "20"), tmp_path=tmp_path)

    run = payload["run"]
    assert set(RUN_KEYS["monte-carlo"]) <= set(run)
    assert run["n_simulations"] == 20
    assert len(run["returns"]) == 20
    assert run["benchmark"]["variant"] == "buy_and_hold"
    assert_benchmark_section(payload, variant="buy_and_hold")


@pytest.mark.parametrize("command", RUN_COMMANDS)
def test_every_run_command_threads_the_risk_free_variant_and_rate(
    command: str, tmp_path: Path
) -> None:
    """The two new flags are wired into the four run commands, not only ``backtest``."""
    payload = run_json(
        *run_args(command, "--benchmark-variant", "risk_free", "--risk-free-rate", "0.05"),
        tmp_path=tmp_path,
    )

    assert payload["run"]["benchmark"]["variant"] == "risk_free"
    assert_benchmark_section(payload, variant="risk_free")


def test_walk_forward_exposes_the_random_entry_gate(tmp_path: Path) -> None:
    """The random-entry gate travels inside ``run`` for the validation commands too."""
    config = random_entry_config(tmp_path)

    payload = run_json(
        "walk-forward",
        "--config",
        config,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--windows",
        "2",
        "--benchmark-variant",
        "random_entry",
        tmp_path=tmp_path,
    )

    run = payload["run"]
    assert set(RUN_KEYS["walk-forward"]) <= set(run)
    assert run["random_entry"]["n_simulations"] == TEST_SIMULATIONS
    assert "benchmark" not in run
    assert "random_entry" in section_titles(payload)


@pytest.mark.parametrize("command", RUN_COMMANDS)
def test_the_payload_and_exit_code_contract_of_every_run_command_is_unchanged(
    command: str, tmp_path: Path
) -> None:
    """One successful run: exit 0, the documented payload keys, metrics, 2 reports."""
    payload = run_json(*run_args(command), tmp_path=tmp_path)

    assert payload["command"] == command.replace("-", "_")
    assert payload["symbol"] == "BTC/USDT"
    assert payload["timeframe"] == "1h"
    assert payload["config_path"] == CONFIG
    assert payload["data_quality"]["n_rows"] == FIXTURE_ROWS
    assert payload["metrics"]["values"]
    assert {"total_return", "sharpe_ratio", "max_drawdown"} <= set(payload["metrics"]["values"])
    assert len(payload["reports"]) == 2


def test_the_commands_still_work_without_the_benchmark(tmp_path: Path) -> None:
    """``--no-benchmark`` changes nothing else: exit 0, metrics, 2 reports, no section."""
    for command in RUN_COMMANDS:
        payload = run_json(*run_args(command, "--no-benchmark"), tmp_path=tmp_path)

        assert payload["command"] == command.replace("-", "_")
        assert payload["metrics"]["values"]
        assert "benchmark" not in payload["run"]
        assert "random_entry" not in payload["run"]
        assert len(payload["reports"]) == 2
        json_report, _ = written_reports(payload)
        titles = {section["title"] for section in read_report(json_report)["sections"]}
        assert "Benchmark" not in titles
        assert "random_entry" not in titles
