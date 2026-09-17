"""End-to-end tests of the command line interface (work package wp-5).

Every test is **offline** and deterministic: the backtest data comes from
``tests/fixtures/BTC_USDT-1h.csv`` (200 synthetic candles), the cache path is a
``tmp_path`` and a cache miss is asserted instead of a download.  No test is
marked ``network`` -- there is nothing to download.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import trading_backtest
from trading_backtest.cli import app
from trading_backtest.config import dump_config, load_config
from trading_backtest.data import OHLCVCache
from trading_backtest.data.synthetic import make_trending_ohlcv
from trading_backtest.reporting import read_report

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = str(REPO_ROOT / "config" / "backtest_default.json")
FIXTURE = str(REPO_ROOT / "tests" / "fixtures" / "BTC_USDT-1h.csv")
FIXTURE_ROWS = 200

#: The documented JSON payload keys, in the documented order.
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

#: Commands that never need the network (monte-carlo has no --no-network flag).
OFFLINE_FLAGGED_COMMANDS = ("backtest", "walk-forward", "robustness")

runner = CliRunner()


def invoke(*args: str):
    """Invoke the CLI in the isolated CliRunner environment."""
    return runner.invoke(app, list(args))


def json_payload(result) -> dict:
    """Parse the single JSON object printed on stdout."""
    return json.loads(result.stdout)


def assert_payload_shape(payload: dict) -> None:
    """Assert the payload exposes exactly the documented keys."""
    assert sorted(payload) == sorted(PAYLOAD_KEYS)


def offline_config(tmp_path: Path, *, cache_dir: Path | None = None) -> Path:
    """Write a copy of the default configuration pointing at a tmp cache."""
    cfg = load_config(CONFIG)
    cfg.data.cache_dir = cache_dir if cache_dir is not None else tmp_path / "cache"
    cfg.data.allow_network = True
    return dump_config(cfg, tmp_path / "config.json")


# ---------------------------------------------------------------------------
# help / version / usage errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("--help",),
        ("backtest", "--help"),
        ("walk-forward", "--help"),
        ("robustness", "--help"),
        ("monte-carlo", "--help"),
        ("data", "--help"),
        ("data", "download", "--help"),
        ("config", "--help"),
        ("config", "show", "--help"),
        ("config", "validate", "--help"),
    ],
)
def test_help_exits_zero(args: tuple[str, ...]) -> None:
    result = invoke(*args)

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_no_arguments_prints_the_help_and_fails() -> None:
    result = invoke()

    assert result.exit_code == 2
    assert "Usage:" in result.output


def test_version_prints_the_package_version() -> None:
    result = invoke("--version")

    assert result.exit_code == 0
    assert result.output.strip() == trading_backtest.__version__


def test_unknown_subcommand_is_a_usage_error() -> None:
    result = invoke("does-not-exist")

    assert result.exit_code == 2
    assert "Traceback" not in result.stderr


def test_missing_required_option_is_a_usage_error() -> None:
    result = invoke("backtest")

    assert result.exit_code == 2
    assert "--config" in result.stderr


def test_invalid_enum_choice_is_a_usage_error() -> None:
    result = invoke("walk-forward", "--config", CONFIG, "--mode", "sideways")

    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# config show / validate
# ---------------------------------------------------------------------------


def test_config_show_json() -> None:
    result = invoke("config", "show", "--config", CONFIG, "--json")

    assert result.exit_code == 0, result.output
    payload = json_payload(result)
    assert_payload_shape(payload)
    assert payload["command"] == "config_show"
    assert payload["ok"] is True
    assert payload["config_path"] == CONFIG
    assert payload["metrics"] is None
    assert payload["reports"] == []
    assert payload["run"]["data"]["timeframe"] == "1h"
    assert payload["run"]["data"]["allow_network"] is True
    assert payload["run"]["reporting"]["formats"] == ["markdown", "json"]


def test_config_show_human_prints_the_configuration() -> None:
    result = invoke("config", "show", "--config", CONFIG)

    assert result.exit_code == 0, result.output
    assert '"project_name": "trading-backtest"' in result.output


@pytest.mark.parametrize(
    "relative_path",
    [
        "config/backtest_default.json",
        "config/freqtrade_config.json",
        "config/freqtrade_dryrun.json",
    ],
)
def test_config_validate_accepts_every_committed_config(relative_path: str) -> None:
    result = invoke("config", "validate", "--config", str(REPO_ROOT / relative_path), "--json")

    assert result.exit_code == 0, result.output
    payload = json_payload(result)
    assert payload["command"] == "config_show"  # documented command key for both sub-commands
    assert payload["run"]["valid"] is True
    assert payload["run"]["issues"] == []


def test_config_validate_rejects_a_corrupt_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text('{"project_name": "x",,}', encoding="utf-8")

    result = invoke("config", "validate", "--config", str(broken))

    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "invalid JSON" in result.stderr.replace("\n", "")
    assert "Traceback" not in result.stderr


def test_config_validate_rejects_a_missing_file(tmp_path: Path) -> None:
    result = invoke("config", "validate", "--config", str(tmp_path / "missing.json"))

    assert result.exit_code == 1
    assert "not found" in result.stderr.replace("\n", "")


def test_config_validate_rejects_an_inconsistent_window(tmp_path: Path) -> None:
    payload = json.loads((REPO_ROOT / "config" / "backtest_default.json").read_text("utf-8"))
    payload["data"]["start"] = "2024-01-02T00:00:00Z"
    payload["data"]["end"] = "2024-01-01T00:00:00Z"
    path = tmp_path / "inconsistent.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = invoke("config", "validate", "--config", str(path))

    assert result.exit_code == 1
    assert "data.start" in result.stderr.replace("\n", "")


def test_config_validate_reports_freqtrade_issues(tmp_path: Path) -> None:
    payload = json.loads((REPO_ROOT / "config" / "freqtrade_dryrun.json").read_text("utf-8"))
    payload["max_open_trades"] = 0
    path = tmp_path / "freqtrade.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = invoke("config", "validate", "--config", str(path), "--json")

    assert result.exit_code == 1
    assert "max_open_trades" in result.stderr.replace("\n", "")
    failed = json_payload(result)
    assert failed["ok"] is False
    assert_payload_shape(failed)


def test_config_show_rejects_a_json_document_that_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    result = invoke("config", "show", "--config", str(path))

    assert result.exit_code == 1
    assert "must contain a JSON object" in result.stderr.replace("\n", "")


def test_config_show_reports_an_unreadable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")

    def unreadable(*args: object, **kwargs: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", unreadable)

    result = invoke("config", "show", "--config", str(path))

    assert result.exit_code == 1
    assert "cannot read configuration file" in result.stderr.replace("\n", "")


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


def test_backtest_json_end_to_end(tmp_path: Path) -> None:
    result = invoke(
        "backtest",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--json",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    payload = json_payload(result)
    assert_payload_shape(payload)
    assert payload["command"] == "backtest"
    assert payload["ok"] is True
    assert payload["symbol"] == "BTC/USDT"  # inferred from the fixture name
    assert payload["timeframe"] == "1h"
    assert payload["config_path"] == CONFIG
    assert payload["data_quality"]["ok"] is True
    assert payload["data_quality"]["n_rows"] == FIXTURE_ROWS

    values = payload["metrics"]["values"]
    assert {"total_return", "sharpe_ratio", "max_drawdown", "n_trades"} <= set(values)
    assert payload["run"]["final_balance"] == pytest.approx(values["final_balance"])
    assert payload["run"]["strategy_name"] == "basic"

    written = [Path(path) for path in payload["reports"]]
    assert len(written) == 2
    for path in written:
        assert path.parent.resolve() == tmp_path.resolve()
        assert path.is_file()
    json_report = next(path for path in written if path.suffix == ".json")
    markdown_report = next(path for path in written if path.suffix == ".md")

    reparsed = read_report(json_report)
    assert reparsed["title"] == "Backtest Report"
    assert reparsed["summary"]["final_balance"] == pytest.approx(values["final_balance"])
    assert reparsed["summary"]["n_trades"] == values["n_trades"]
    assert reparsed["summary"]["symbol"] == "BTC/USDT"
    assert {section["title"] for section in reparsed["sections"]} >= {
        "data_quality",
        "Trades",
        "Equity curve",
    }
    assert markdown_report.read_text(encoding="utf-8").startswith("# Backtest Report")


def test_backtest_human_mode_prints_the_table_and_the_paths(tmp_path: Path) -> None:
    result = invoke(
        "backtest",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "Metrics" in result.output
    assert "sharpe_ratio" in result.output
    assert "reports:" in result.output
    # the report paths are printed (rich wraps long paths, hence the unfolding)
    unfolded = result.output.replace("\n", "")
    assert "report.md" in unfolded
    assert "report.json" in unfolded
    assert (tmp_path / "report.md").is_file()


def test_backtest_honours_a_single_output_format(tmp_path: Path) -> None:
    result = invoke(
        "backtest",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--json",
        "--formats",
        "json",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    payload = json_payload(result)
    assert len(payload["reports"]) == 1
    assert payload["reports"][0].endswith(".json")


def test_backtest_rejects_an_unknown_output_format(tmp_path: Path) -> None:
    result = invoke(
        "backtest",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--formats",
        "pdf",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 1
    assert "pdf" in result.stderr.replace("\n", "")
    assert "Traceback" not in result.stderr


def test_backtest_rejects_a_missing_data_file(tmp_path: Path) -> None:
    result = invoke(
        "backtest",
        "--config",
        CONFIG,
        "--data-file",
        str(tmp_path / "nope.csv"),
        "--no-network",
    )

    assert result.exit_code == 1
    assert "not found" in result.stderr.replace("\n", "")


def test_backtest_slices_the_window_of_a_local_csv(tmp_path: Path) -> None:
    result = invoke(
        "backtest",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--json",
        "--start",
        "2023-01-02T00:00:00Z",
        "--end",
        "2023-01-03T00:00:00Z",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    # 25 hourly candles of the 200-row fixture survive the slice
    assert json_payload(result)["data_quality"]["n_rows"] == 25


def test_backtest_rejects_an_empty_window(tmp_path: Path) -> None:
    result = invoke(
        "backtest",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--start",
        "2030-01-01T00:00:00Z",
        "--end",
        "2030-01-02T00:00:00Z",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 1
    assert "inside the requested window" in result.stderr.replace("\n", "")


def test_backtest_rejects_an_empty_csv(tmp_path: Path) -> None:
    empty = tmp_path / "BTC_USDT-1h.csv"
    empty.write_text("timestamp,open,high,low,close,volume\n", encoding="utf-8")

    result = invoke("backtest", "--config", CONFIG, "--data-file", str(empty), "--no-network")

    assert result.exit_code == 1
    assert "no candle" in result.stderr.replace("\n", "")


def test_backtest_rejects_an_invalid_timestamp() -> None:
    result = invoke(
        "backtest",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--start",
        "not-a-date",
    )

    assert result.exit_code == 1
    assert "ISO-8601" in result.stderr.replace("\n", "")


def test_backtest_without_data_file_and_no_network_fails_readably(tmp_path: Path) -> None:
    config_path = offline_config(tmp_path)  # empty cache directory

    result = invoke(
        "backtest",
        "--config",
        str(config_path),
        "--symbol",
        "BTC/USDT",
        "--timeframe",
        "1h",
        "--start",
        "2023-01-01T00:00:00Z",
        "--end",
        "2023-01-05T00:00:00Z",
        "--no-network",
    )

    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "network" in result.stderr.replace("\n", "")
    assert "Traceback" not in result.stderr


def test_backtest_without_data_file_needs_a_window(tmp_path: Path) -> None:
    config_path = offline_config(tmp_path)

    result = invoke("backtest", "--config", str(config_path), "--no-network", "--json")

    assert result.exit_code == 1
    assert "data.start" in result.stderr.replace("\n", "")
    failed = json_payload(result)
    assert_payload_shape(failed)
    assert failed["ok"] is False
    assert failed["metrics"] is None
    assert failed["reports"] == []


# ---------------------------------------------------------------------------
# walk-forward / robustness / monte-carlo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "extra", "payload_command", "run_keys"),
    [
        ("walk-forward", ("--windows", "2"), "walk_forward", ("windows", "n_windows", "mode")),
        ("robustness", ("--max-combinations", "4"), "robustness", ("points", "n_points")),
        ("monte-carlo", ("--simulations", "50"), "monte_carlo", ("returns", "n_simulations")),
    ],
)
def test_validation_commands_end_to_end(
    tmp_path: Path,
    command: str,
    extra: tuple[str, ...],
    payload_command: str,
    run_keys: tuple[str, ...],
) -> None:
    args = [
        command,
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--json",
        "--output-dir",
        str(tmp_path),
        *extra,
    ]
    if command in OFFLINE_FLAGGED_COMMANDS:
        args.append("--no-network")

    result = invoke(*args)

    assert result.exit_code == 0, result.output
    payload = json_payload(result)
    assert_payload_shape(payload)
    assert payload["command"] == payload_command
    assert payload["ok"] is True
    assert payload["metrics"] is not None
    assert payload["data_quality"]["ok"] is True
    for key in run_keys:
        assert key in payload["run"], f"{payload_command} payload has no {key!r}"

    written = [Path(path) for path in payload["reports"]]
    assert len(written) == 2
    assert all(path.is_file() for path in written)


def test_walk_forward_uses_the_requested_number_of_windows(tmp_path: Path) -> None:
    result = invoke(
        "walk-forward",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--json",
        "--windows",
        "2",
        "--is-ratio",
        "0.75",
        "--mode",
        "anchored",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    run = json_payload(result)["run"]
    assert run["n_windows"] == 2
    assert run["mode"] == "anchored"
    assert run["in_sample_ratio"] == pytest.approx(0.75)
    assert len(run["windows"]) == 2


def test_robustness_respects_the_combination_ceiling(tmp_path: Path) -> None:
    result = invoke(
        "robustness",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--json",
        "--max-combinations",
        "4",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    run = json_payload(result)["run"]
    assert run["n_points"] == len(run["points"])
    assert 1 <= run["n_points"] <= 4


def test_robustness_rejects_a_zero_ceiling(tmp_path: Path) -> None:
    result = invoke(
        "robustness",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--max-combinations",
        "0",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 1
    assert "--max-combinations" in result.stderr.replace("\n", "")
    assert "Traceback" not in result.stderr


def test_robustness_refuses_an_explicit_grid_larger_than_the_ceiling(tmp_path: Path) -> None:
    payload = json.loads((REPO_ROOT / "config" / "backtest_default.json").read_text("utf-8"))
    payload["validation"]["robustness_grid"] = {"ema_fast": [5, 9, 13], "ema_slow": [21, 34, 55]}
    path = tmp_path / "grid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = invoke(
        "robustness",
        "--config",
        str(path),
        "--data-file",
        FIXTURE,
        "--no-network",
        "--max-combinations",
        "4",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 1
    assert "max_combinations" in result.stderr.replace("\n", "")


def test_monte_carlo_is_deterministic_with_the_same_seed(tmp_path: Path) -> None:
    args = (
        "monte-carlo",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--simulations",
        "50",
        "--seed",
        "7",
        "--json",
        "--output-dir",
        str(tmp_path),
    )

    first = invoke(*args)
    second = invoke(*args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert json_payload(first)["run"]["returns"] == json_payload(second)["run"]["returns"]
    assert len(json_payload(first)["run"]["returns"]) == 50


def test_walk_forward_human_mode_prints_counts_and_flags(tmp_path: Path) -> None:
    result = invoke(
        "walk-forward",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--no-network",
        "--windows",
        "2",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "n_windows: 2" in result.output
    assert "is_consistent:" in result.output
    assert "Metrics" in result.output


def test_monte_carlo_method_bootstrap_equity(tmp_path: Path) -> None:
    result = invoke(
        "monte-carlo",
        "--config",
        CONFIG,
        "--data-file",
        FIXTURE,
        "--simulations",
        "20",
        "--method",
        "bootstrap_equity",
        "--seed",
        "3",
        "--json",
        "--output-dir",
        str(tmp_path),
    )

    assert result.exit_code == 0, result.output
    run = json_payload(result)["run"]
    assert run["method"] == "bootstrap_equity"
    assert run["n_simulations"] == 20


# ---------------------------------------------------------------------------
# data download (offline: the cache already covers the window)
# ---------------------------------------------------------------------------


def test_data_download_serves_a_covered_cache_without_network(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    frame = make_trending_ohlcv(
        n=FIXTURE_ROWS, period=45, amplitude=8.0, seed=7, start="2023-01-01T00:00:00Z"
    )
    OHLCVCache(cache_dir).write("binance", "BTC/USDT", "1h", frame)
    config_path = offline_config(tmp_path, cache_dir=cache_dir)

    result = invoke(
        "data",
        "download",
        "--config",
        str(config_path),
        "--symbol",
        "BTC/USDT",
        "--timeframe",
        "1h",
        "--start",
        "2023-01-01T00:00:00Z",
        "--end",
        "2023-01-04T00:00:00Z",
        "--json",
    )

    assert result.exit_code == 0, result.output
    payload = json_payload(result)
    assert_payload_shape(payload)
    assert payload["command"] == "data_download"
    assert payload["ok"] is True
    assert payload["reports"] == []
    assert payload["run"]["rows"] == 73  # 3 days of hourly candles, bounds inclusive
    assert Path(payload["run"]["cache_path"]).is_file()
    assert payload["data_quality"]["n_rows"] == 73


def test_data_download_human_mode_prints_the_row_count(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    frame = make_trending_ohlcv(
        n=FIXTURE_ROWS, period=45, amplitude=8.0, seed=7, start="2023-01-01T00:00:00Z"
    )
    OHLCVCache(cache_dir).write("binance", "BTC/USDT", "1h", frame)
    config_path = offline_config(tmp_path, cache_dir=cache_dir)

    result = invoke(
        "data",
        "download",
        "--config",
        str(config_path),
        "--symbol",
        "BTC/USDT",
        "--timeframe",
        "1h",
        "--start",
        "2023-01-01T00:00:00Z",
        "--end",
        "2023-01-04T00:00:00Z",
    )

    assert result.exit_code == 0, result.output
    assert "data_download BTC/USDT 1h" in result.output
    assert "rows: 73" in result.output
    assert "cache_path:" in result.output


def test_data_download_requires_symbol_timeframe_and_window() -> None:
    result = invoke("data", "download", "--config", CONFIG)

    assert result.exit_code == 2
    assert "Missing option" in result.stderr


# ---------------------------------------------------------------------------
# module surface: `python -m`, console script, lazy imports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["trading_backtest", "trading_backtest.cli"])
def test_python_dash_m_entry_points(module: str) -> None:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    completed = subprocess.run(
        [sys.executable, "-m", module, "--version"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == trading_backtest.__version__


def test_python_dash_m_help() -> None:
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    completed = subprocess.run(
        [sys.executable, "-m", "trading_backtest", "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Usage:" in completed.stdout
    assert "backtest" in completed.stdout


def test_console_script_entry_point() -> None:
    script = shutil.which("trading-backtest")
    if script is None:  # pragma: no cover - the package is not installed here
        pytest.skip("the console script is not installed in this environment")

    completed = subprocess.run([script, "--version"], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == trading_backtest.__version__


def test_cli_module_imports_without_the_heavy_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "trading_backtest.strategy",
        "trading_backtest.validation",
        "trading_backtest.metrics",
        "trading_backtest.reporting",
    ):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, "trading_backtest.cli", raising=False)

    module = importlib.import_module("trading_backtest.cli")

    assert module.app is not None
    assert list(module.PAYLOAD_KEYS) == list(PAYLOAD_KEYS)


def test_cli_enums_mirror_the_validation_layer() -> None:
    from trading_backtest.cli import MONTE_CARLO_METHODS, WINDOW_MODES
    from trading_backtest.validation import METHODS
    from trading_backtest.validation import WINDOW_MODES as LAYER_WINDOW_MODES

    assert sorted(mode.value for mode in WINDOW_MODES) == sorted(LAYER_WINDOW_MODES)
    assert sorted(method.value for method in MONTE_CARLO_METHODS) == sorted(METHODS)


# ---------------------------------------------------------------------------
# main(): the entry point called by the console script and by __main__
# ---------------------------------------------------------------------------


def test_main_returns_the_exit_codes(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    from trading_backtest.cli import main

    assert main(["--help"]) == 0
    assert main(["--version"]) == 0
    assert main(["config", "show", "--config", CONFIG, "--json"]) == 0
    assert main(["does-not-exist"]) == 2
    success = main(
        [
            "backtest",
            "--config",
            CONFIG,
            "--data-file",
            FIXTURE,
            "--no-network",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert success == 0
    capsys.readouterr()

    exit_code = main(
        [
            "backtest",
            "--config",
            CONFIG,
            "--data-file",
            str(tmp_path / "missing.csv"),
            "--no-network",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "error:" in captured.err
    assert "not found" in captured.err.replace("\n", "")
    assert "Traceback" not in captured.err


def test_main_swallows_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_backtest import cli

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "load_config", boom)

    with pytest.raises(RuntimeError):
        cli.main(["config", "show", "--config", CONFIG, "--json"])


def test_main_renders_a_domain_error_escaping_a_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The safety net of ``main``: a TradingBacktestError outside a command body."""
    from collections.abc import Iterator
    from contextlib import contextmanager

    from trading_backtest import cli

    @contextmanager
    def pass_through(command: str, *, json_output: bool) -> Iterator[None]:
        yield

    monkeypatch.setattr(cli, "_error_surface", pass_through)

    exit_code = cli.main(["config", "show", "--config", str(tmp_path / "missing.json")])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "error:" in captured.err
    assert "not found" in captured.err.replace("\n", "")
    assert "Traceback" not in captured.err
