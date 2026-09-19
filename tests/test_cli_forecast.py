"""End-to-end tests of the forecasting commands (work package wp-5).

Everything here is **offline** and deterministic: the candle frames come from
:mod:`trading_platform.data.synthetic`, every artifact is written under
``tmp_path`` (never inside the repository tree) and the backends exercised are
the pure ``numpy`` ones (``naive``, ``seasonal``).  ``torch`` and ``timesfm``
are never imported: the two tests that cover the model-backed backend pin the
*unavailable* path, which is the only one a ``.[dev]`` installation can reach.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from trading_platform.cli import app
from trading_platform.core.constants import OHLCV_INDEX_NAME
from trading_platform.data.synthetic import make_trending_ohlcv
from trading_platform.forecast.artifact import ForecastStore
from trading_platform.forecast.backends.naive import NaiveBackend
from trading_platform.forecast.backends.timesfm import DEFAULT_MODEL_ID
from trading_platform.forecast.registry import BACKENDS, register_backend

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = str(REPO_ROOT / "config" / "backtest_default.json")

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

#: Scalars ``forecast-build`` publishes in ``metrics`` (all of them floats).
BUILD_METRIC_KEYS = ("context_length", "horizon", "n_origins", "n_quantiles", "stride")

#: Scalars ``forecast-skill`` publishes in ``metrics``.
SKILL_METRIC_KEYS = (
    "rmse",
    "mae",
    "baseline_rmse",
    "baseline_mae",
    "rmse_skill_score",
    "mae_skill_score",
    "mase",
    "directional_accuracy",
    "directional_accuracy_horizon",
    "coverage_error_mean",
    # What a strategy is actually paid on: the same measurements against the REAL
    # price move, which keeps the seasonal component the target transform removes.
    "real_rmse",
    "real_baseline_rmse",
    "real_rmse_skill_score",
    "real_directional_accuracy_horizon",
)

#: Context/horizon/stride used by every artifact of this module.
CONTEXT = 64
HORIZON = 12
STRIDE = 12

#: Registry name of the stub backend that pretends its extra is missing.
UNAVAILABLE_BACKEND = "stub-unavailable"

#: Registry name of the stub backend that publishes a licence note.
LICENSED_BACKEND = "stub-licensed"

#: Licence a model-backed backend records in the artifact metadata.
STUB_LICENSE = "Apache-2.0"

#: Checkpoint identifier the licensed stub backend pretends to load.
STUB_MODEL_ID = "google/stub-checkpoint"

runner = CliRunner()

#: ANSI SGR escape sequences emitted by rich when the CLI runs in a colour-forcing
#: environment (CI sets ``FORCE_COLOR``/``TERM``).  The captured streams are
#: de-colourised, never the assertions relaxed.
_ANSI_SGR = re.compile(rb"\x1b\[[0-9;]*m")


def invoke(*args: str) -> Any:
    """Invoke the CLI in the isolated CliRunner environment, without ANSI styling."""
    result = runner.invoke(app, list(args))
    result.stdout_bytes = _ANSI_SGR.sub(b"", result.stdout_bytes)
    result.stderr_bytes = _ANSI_SGR.sub(b"", result.stderr_bytes)
    result.output_bytes = _ANSI_SGR.sub(b"", result.output_bytes)
    return result


def single_json_object(text: str) -> dict[str, Any]:
    """Assert that ``text`` holds exactly one JSON object and return it."""
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(text.lstrip())
    assert text.lstrip()[end:].strip() == "", "stdout carries more than one JSON value"
    assert isinstance(payload, dict)
    return payload


def json_payload(result: Any) -> dict[str, Any]:
    """Parse and validate the single JSON object a ``--json`` run prints on stdout."""
    assert result.exit_code == 0, result.output
    payload = single_json_object(result.stdout)
    assert sorted(payload) == sorted(PAYLOAD_KEYS)
    return payload


def assert_domain_error(result: Any, *fragments: str) -> None:
    """Assert a command failed cleanly: exit code ``1``, ``error:`` on stderr, no bug."""
    assert result.exit_code == 1, result.output
    assert "error:" in result.stderr, result.stderr
    exception = result.exception
    assert exception is None or isinstance(exception, SystemExit), repr(exception)
    assert "Traceback" not in result.output
    for fragment in fragments:
        assert fragment in result.stderr, result.stderr


def parquet_of(frame: Any, path: Path) -> Path:
    """Write ``frame`` as a parquet candle file and return its path."""
    frame.to_parquet(path)
    return path


def build_args(data_file: Path, out: Path, *extra: str) -> list[str]:
    """Return the ``forecast-build`` arguments of one offline build."""
    return [
        "forecast-build",
        "--config",
        CONFIG,
        "--data-file",
        str(data_file),
        "--out",
        str(out),
        "--context",
        str(CONTEXT),
        "--horizon",
        str(HORIZON),
        "--reforecast-every",
        str(STRIDE),
        *extra,
    ]


def build_artifact(tmp_path: Path, data_file: Path, *extra: str) -> Path:
    """Build an artifact from ``data_file`` with the ``naive`` backend and return it."""
    out = tmp_path / "artifact.parquet"
    result = invoke(*build_args(data_file, out, "--backend", "naive", "--json", *extra))
    assert result.exit_code == 0, result.output
    return out


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def unavailable_backend() -> Iterator[str]:
    """Register a stub backend whose optional extra is missing, then clean up."""

    class _UnavailableBackend:
        """Backend that is registered but cannot run in this environment."""

        name = UNAVAILABLE_BACKEND

        def is_available(self) -> bool:
            """Report the backend as uninstalled, like a missing extra would."""
            return False

        def predict(self, requests: Any, *, horizon: int) -> Any:
            """Never called: an unavailable backend is rejected before forecasting."""
            raise AssertionError("predict must not be called on an unavailable backend")

    register_backend(UNAVAILABLE_BACKEND, lambda **options: _UnavailableBackend())
    try:
        yield UNAVAILABLE_BACKEND
    finally:
        BACKENDS.pop(UNAVAILABLE_BACKEND, None)


@pytest.fixture
def licensed_backend() -> Iterator[str]:
    """Register a stub backend that publishes a licence note, then clean up.

    The stub delegates forecasting to :class:`NaiveBackend`, so the CLI path it
    exercises (licence lookup, ``backend_options`` forwarding, artifact build) is
    the real one, without ``torch`` or a model download.
    """

    class _LicensedBackend(NaiveBackend):
        """Offline backend that advertises the licence of its (fake) weights."""

        name = LICENSED_BACKEND

        def __init__(self, *, model_id: str = DEFAULT_MODEL_ID, **options: Any) -> None:
            super().__init__(**options)
            self._model_id = model_id

        @property
        def model_id(self) -> str:
            """Checkpoint this backend pretends to have loaded."""
            return self._model_id

        def license_note(self) -> str:
            """Return the licence recorded in the artifact metadata."""
            return STUB_LICENSE

    register_backend(LICENSED_BACKEND, lambda **options: _LicensedBackend(**options))
    try:
        yield LICENSED_BACKEND
    finally:
        BACKENDS.pop(LICENSED_BACKEND, None)


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------


def test_help_lists_the_three_forecast_commands() -> None:
    root = invoke("--help")

    assert root.exit_code == 0, root.output
    for command in ("forecast-build", "forecast-skill", "forecast-info"):
        assert command in root.output, f"--help does not list {command}"


@pytest.mark.parametrize("command", ["forecast-build", "forecast-skill", "forecast-info"])
def test_forecast_help_documents_the_frozen_options(command: str) -> None:
    result = invoke(command, "--help")

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_forecast_build_help_lists_every_documented_option() -> None:
    result = invoke("forecast-build", "--help")

    assert result.exit_code == 0, result.output
    for option in (
        "--config",
        "--data-file",
        "--out",
        "--backend",
        "--context",
        "--horizon",
        "--reforecast-every",
        "--seasonal-period",
        "--seasonal-window",
        "--model-id",
        "--symbol",
        "--timeframe",
        "--json",
    ):
        assert option in result.output, f"forecast-build --help hides {option}"


# ---------------------------------------------------------------------------
# forecast-build
# ---------------------------------------------------------------------------


def test_forecast_build_reads_a_csv_and_emits_the_frozen_payload(
    tmp_path: Path, synthetic_csv: Path
) -> None:
    out = tmp_path / "nested" / "btc.parquet"

    result = invoke(*build_args(synthetic_csv, out, "--backend", "naive", "--json"))

    payload = json_payload(result)
    assert payload["command"] == "forecast_build"
    assert payload["ok"] is True
    assert payload["symbol"] == "BTC/USDT"
    assert payload["timeframe"] == "1h"
    assert payload["config_path"] == CONFIG
    assert payload["reports"] == [str(out)]
    assert sorted(payload["metrics"]) == sorted(BUILD_METRIC_KEYS)
    assert all(isinstance(value, float) for value in payload["metrics"].values())
    assert payload["run"]["backend"] == "naive"
    assert payload["run"]["model_id"] is None
    assert payload["run"]["license"] == ""
    assert payload["data_quality"] is not None
    assert payload["data_quality"]["n_rows"] == 600

    # the parquet and its sidecar exist, and the store can read both back
    assert out.is_file()
    assert Path(f"{out}.meta.json").is_file()
    store = ForecastStore.load(out)
    assert store.metadata.to_dict() == payload["run"]
    assert payload["metrics"]["n_origins"] == float(len(store.origins()))
    assert payload["metrics"]["n_quantiles"] == float(len(store.quantile_levels))


def test_forecast_build_reads_a_parquet_data_file(tmp_path: Path, trending_frame: Any) -> None:
    data_file = parquet_of(trending_frame, tmp_path / "BTC_USDT-1h.parquet")
    out = tmp_path / "from_parquet.parquet"

    result = invoke(*build_args(data_file, out, "--backend", "naive", "--json"))

    payload = json_payload(result)
    assert payload["symbol"] == "BTC/USDT"
    assert payload["run"]["n_origins"] == payload["metrics"]["n_origins"]
    assert ForecastStore.load(out).horizon == HORIZON


def test_forecast_build_supports_the_seasonal_backend(tmp_path: Path, synthetic_csv: Path) -> None:
    out = tmp_path / "seasonal.parquet"

    result = invoke(*build_args(synthetic_csv, out, "--backend", "seasonal", "--json"))

    payload = json_payload(result)
    assert payload["run"]["backend"] == "seasonal"
    assert payload["run"]["license"] == ""
    assert ForecastStore.load(out).metadata.backend == "seasonal"


def test_forecast_build_defaults_are_the_documented_ones(tmp_path: Path) -> None:
    """No option beyond the three required paths: naive, context 512, horizon/stride 24."""
    data_file = tmp_path / "BTC_USDT-1h.csv"
    make_trending_ohlcv(700).to_csv(data_file, index_label=OHLCV_INDEX_NAME)
    out = tmp_path / "defaults.parquet"

    result = invoke(
        "forecast-build",
        "--config",
        CONFIG,
        "--data-file",
        str(data_file),
        "--out",
        str(out),
        "--json",
    )

    payload = json_payload(result)
    assert payload["metrics"] == {
        "context_length": 512.0,
        "horizon": 24.0,
        "n_origins": float(len(ForecastStore.load(out).origins())),
        "n_quantiles": 9.0,
        "stride": 24.0,
    }
    assert payload["run"]["backend"] == "naive"
    assert payload["run"]["seasonal_period"] == 24
    assert payload["run"]["seasonal_window"] == 168


def test_forecast_build_records_the_licence_note_of_the_backend(
    tmp_path: Path, synthetic_csv: Path, licensed_backend: str
) -> None:
    """A model-backed backend publishes a licence and keeps its checkpoint id.

    ``--model-id`` is forwarded to the backend factory *and* recorded in the
    metadata, so the artifact can never claim one checkpoint while another one
    produced the paths.
    """
    out = tmp_path / "licensed.parquet"

    result = invoke(
        *build_args(
            synthetic_csv, out, "--backend", licensed_backend, "--model-id", STUB_MODEL_ID, "--json"
        )
    )

    payload = json_payload(result)
    assert payload["run"]["backend"] == licensed_backend
    assert payload["run"]["model_id"] == STUB_MODEL_ID
    assert payload["run"]["license"] == STUB_LICENSE
    store = ForecastStore.load(out)
    assert store.metadata.model_id == STUB_MODEL_ID
    assert store.metadata.license == STUB_LICENSE


def test_forecast_build_honours_the_seasonal_options(tmp_path: Path, synthetic_csv: Path) -> None:
    out = tmp_path / "seasonal_options.parquet"

    result = invoke(
        *build_args(
            synthetic_csv,
            out,
            "--backend",
            "seasonal",
            "--seasonal-period",
            "12",
            "--seasonal-window",
            "48",
            "--json",
        )
    )

    payload = json_payload(result)
    assert payload["run"]["seasonal_period"] == 12
    assert payload["run"]["seasonal_window"] == 48
    assert payload["run"]["deseasonalized"] is True


def test_forecast_build_unknown_backend_exits_one(tmp_path: Path, synthetic_csv: Path) -> None:
    result = invoke(
        *build_args(synthetic_csv, tmp_path / "artifact.parquet", "--backend", "does-not-exist")
    )

    assert_domain_error(result, "unknown forecast backend")


def test_forecast_build_unknown_backend_with_json_still_prints_one_payload(
    tmp_path: Path, synthetic_csv: Path
) -> None:
    result = invoke(
        *build_args(
            synthetic_csv, tmp_path / "artifact.parquet", "--backend", "does-not-exist", "--json"
        )
    )

    assert result.exit_code == 1, result.output
    payload = single_json_object(result.stdout)
    assert sorted(payload) == sorted(PAYLOAD_KEYS)
    assert payload["command"] == "forecast_build"
    assert payload["ok"] is False
    assert "error:" in result.stderr


def test_forecast_build_unavailable_backend_names_the_missing_extra(
    tmp_path: Path, synthetic_csv: Path, unavailable_backend: str
) -> None:
    result = invoke(
        *build_args(synthetic_csv, tmp_path / "artifact.parquet", "--backend", unavailable_backend)
    )

    assert_domain_error(result, "is not available")


@pytest.mark.skipif(
    importlib.util.find_spec("torch") is not None,
    reason="the optional [timesfm] extra is installed",
)
def test_forecast_build_timesfm_without_the_extra_names_the_install_hint(
    tmp_path: Path, synthetic_csv: Path
) -> None:
    result = invoke(
        *build_args(synthetic_csv, tmp_path / "artifact.parquet", "--backend", "timesfm")
    )

    assert_domain_error(result, "[timesfm]", "pip install")


def test_forecast_build_rejects_a_model_id_on_a_pure_backend(
    tmp_path: Path, synthetic_csv: Path
) -> None:
    result = invoke(
        *build_args(
            synthetic_csv,
            tmp_path / "artifact.parquet",
            "--backend",
            "naive",
            "--model-id",
            "google/timesfm-2.5-200m-pytorch",
        )
    )

    assert_domain_error(result, "--model-id")


def test_forecast_build_missing_data_file_exits_one(tmp_path: Path) -> None:
    result = invoke(*build_args(tmp_path / "absent.csv", tmp_path / "artifact.parquet"))

    assert_domain_error(result, "data file not found")


def test_forecast_build_missing_parquet_data_file_exits_one(tmp_path: Path) -> None:
    result = invoke(*build_args(tmp_path / "absent.parquet", tmp_path / "artifact.parquet"))

    assert_domain_error(result, "data file not found")


def test_forecast_build_corrupt_parquet_exits_one(tmp_path: Path) -> None:
    corrupt = tmp_path / "BTC_USDT-1h.parquet"
    corrupt.write_bytes(b"this is not a parquet file")

    result = invoke(*build_args(corrupt, tmp_path / "artifact.parquet"))

    assert_domain_error(result, "cannot read OHLCV parquet")


def test_forecast_build_empty_parquet_exits_one(tmp_path: Path) -> None:
    empty = tmp_path / "BTC_USDT-1h.parquet"
    make_trending_ohlcv(0).to_parquet(empty)

    result = invoke(*build_args(empty, tmp_path / "artifact.parquet"))

    assert_domain_error(result, "no candle in")


def test_forecast_build_unsupported_suffix_exits_one(tmp_path: Path, synthetic_csv: Path) -> None:
    unsupported = tmp_path / "candles.txt"
    unsupported.write_text(synthetic_csv.read_text(encoding="utf-8"), encoding="utf-8")

    result = invoke(*build_args(unsupported, tmp_path / "artifact.parquet"))

    assert_domain_error(result, "unsupported data file format")


def test_forecast_build_missing_config_exits_one(tmp_path: Path, synthetic_csv: Path) -> None:
    result = invoke(
        "forecast-build",
        "--config",
        str(tmp_path / "absent.json"),
        "--data-file",
        str(synthetic_csv),
        "--out",
        str(tmp_path / "artifact.parquet"),
    )

    assert_domain_error(result, "configuration file not found")


def test_forecast_build_runs_the_data_quality_gate(tmp_path: Path) -> None:
    """``data.validate`` keeps its normal behaviour: invalid candles stop the build."""
    frame = make_trending_ohlcv(300)
    frame.loc[frame.index[10], "close"] = float("nan")
    data_file = tmp_path / "BTC_USDT-1h.csv"
    frame.to_csv(data_file, index_label=OHLCV_INDEX_NAME)
    out = tmp_path / "artifact.parquet"

    result = invoke(*build_args(data_file, out, "--backend", "naive", "--json"))

    assert_domain_error(result)
    assert not out.exists()


def test_forecast_build_rejects_an_impossible_context(tmp_path: Path, synthetic_csv: Path) -> None:
    """A context below the backend floor is a domain error, not a traceback."""
    out = tmp_path / "artifact.parquet"

    result = invoke(
        "forecast-build",
        "--config",
        CONFIG,
        "--data-file",
        str(synthetic_csv),
        "--out",
        str(out),
        "--context",
        "8",
    )

    assert_domain_error(result, "min_context")
    assert not out.exists()


# ---------------------------------------------------------------------------
# forecast-info
# ---------------------------------------------------------------------------


def test_forecast_info_prints_the_metadata_verbatim(tmp_path: Path, synthetic_csv: Path) -> None:
    artifact = build_artifact(tmp_path, synthetic_csv)
    metadata = ForecastStore.load(artifact).metadata.to_dict()

    result = invoke("forecast-info", "--artifact", str(artifact), "--json")

    payload = json_payload(result)
    assert payload["command"] == "forecast_info"
    assert payload["run"] == metadata
    assert payload["metrics"] is None
    assert payload["reports"] == []
    assert payload["config_path"] is None
    assert payload["symbol"] == metadata["symbol"]
    assert payload["timeframe"] == metadata["timeframe"]


def test_forecast_info_human_output_is_the_raw_metadata(
    tmp_path: Path, synthetic_csv: Path
) -> None:
    artifact = build_artifact(tmp_path, synthetic_csv)

    result = invoke("forecast-info", "--artifact", str(artifact))

    assert result.exit_code == 0, result.output
    assert single_json_object(result.stdout) == ForecastStore.load(artifact).metadata.to_dict()


def test_forecast_info_missing_artifact_exits_one(tmp_path: Path) -> None:
    result = invoke("forecast-info", "--artifact", str(tmp_path / "absent.parquet"))

    assert_domain_error(result, "forecast artifact not found")


def test_forecast_info_corrupt_sidecar_exits_one(tmp_path: Path, synthetic_csv: Path) -> None:
    artifact = build_artifact(tmp_path, synthetic_csv)
    Path(f"{artifact}.meta.json").write_text("{not json", encoding="utf-8")

    result = invoke("forecast-info", "--artifact", str(artifact))

    assert_domain_error(result, "cannot read the forecast artifact metadata")


# ---------------------------------------------------------------------------
# forecast-skill
# ---------------------------------------------------------------------------


def test_forecast_skill_reports_the_documented_metrics(tmp_path: Path, synthetic_csv: Path) -> None:
    artifact = build_artifact(tmp_path, synthetic_csv)

    result = invoke(
        "forecast-skill", "--artifact", str(artifact), "--data-file", str(synthetic_csv), "--json"
    )

    payload = json_payload(result)
    assert payload["command"] == "forecast_skill"
    assert payload["reports"] == []
    assert sorted(payload["metrics"]) == sorted(SKILL_METRIC_KEYS)
    assert payload["run"]["metrics"] == payload["metrics"]
    assert payload["run"]["metadata"]["backend"] == "naive"
    assert sorted(payload["run"]["coverage"]) == [f"q{level}" for level in range(10, 100, 10)]


def test_forecast_skill_honours_the_explicit_symbol_and_timeframe(
    tmp_path: Path, synthetic_csv: Path
) -> None:
    artifact = build_artifact(tmp_path, synthetic_csv)

    result = invoke(
        "forecast-skill",
        "--artifact",
        str(artifact),
        "--data-file",
        str(synthetic_csv),
        "--symbol",
        "ETH/USDT",
        "--timeframe",
        "4h",
        "--json",
    )

    payload = json_payload(result)
    assert payload["symbol"] == "ETH/USDT"
    assert payload["timeframe"] == "4h"


def test_forecast_skill_human_output_highlights_the_skill_metrics(
    tmp_path: Path, synthetic_csv: Path
) -> None:
    artifact = build_artifact(tmp_path, synthetic_csv)

    result = invoke(
        "forecast-skill", "--artifact", str(artifact), "--data-file", str(synthetic_csv)
    )

    assert result.exit_code == 0, result.output
    for key in ("rmse:", "mae:", "mase:", "rmse_skill_score:", "coverage_error_mean:"):
        assert key in result.output, f"the human summary hides {key}"


def test_forecast_skill_missing_artifact_exits_one(tmp_path: Path, synthetic_csv: Path) -> None:
    result = invoke(
        "forecast-skill",
        "--artifact",
        str(tmp_path / "absent.parquet"),
        "--data-file",
        str(synthetic_csv),
    )

    assert_domain_error(result, "forecast artifact not found")


def test_forecast_skill_missing_data_file_exits_one(tmp_path: Path, synthetic_csv: Path) -> None:
    artifact = build_artifact(tmp_path, synthetic_csv)

    result = invoke(
        "forecast-skill",
        "--artifact",
        str(artifact),
        "--data-file",
        str(tmp_path / "absent.csv"),
    )

    assert_domain_error(result, "data file not found")


def test_forecast_skill_on_uncovered_candles_reports_an_empty_evaluation(
    tmp_path: Path, synthetic_csv: Path
) -> None:
    """Candles that end before the first origin are reported, never guessed."""
    artifact = build_artifact(tmp_path, synthetic_csv)
    disjoint = tmp_path / "later.csv"
    make_trending_ohlcv(300, start="2024-06-01T00:00:00Z").to_csv(
        disjoint, index_label=OHLCV_INDEX_NAME
    )

    result = invoke(
        "forecast-skill", "--artifact", str(artifact), "--data-file", str(disjoint), "--json"
    )

    payload = json_payload(result)
    assert payload["run"]["metrics"]["rmse"] != payload["run"]["metrics"]["rmse"]  # NaN


# ---------------------------------------------------------------------------
# JSON contract and import policy
# ---------------------------------------------------------------------------


def test_forecast_commands_emit_exactly_one_json_object(
    tmp_path: Path, synthetic_csv: Path
) -> None:
    artifact = build_artifact(tmp_path, synthetic_csv)

    for args in (
        build_args(synthetic_csv, tmp_path / "one_more.parquet", "--json"),
        ["forecast-info", "--artifact", str(artifact), "--json"],
        [
            "forecast-skill",
            "--artifact",
            str(artifact),
            "--data-file",
            str(synthetic_csv),
            "--json",
        ],
    ):
        result = invoke(*args)
        assert result.exit_code == 0, result.output
        assert sorted(single_json_object(result.stdout)) == sorted(PAYLOAD_KEYS)


def test_cli_import_does_not_pull_the_forecast_or_strategy_layers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh ``import trading_platform.cli`` must not import the heavy layers.

    Both packages are removed from the module cache, so a module-level import of
    either one inside ``cli.py`` would put them straight back.
    """
    for name in ("trading_platform.strategy", "trading_platform.forecast"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "trading_platform.cli", raising=False)

    module = importlib.import_module("trading_platform.cli")

    assert module.app is not None
    assert "trading_platform.forecast" not in sys.modules
    assert "trading_platform.strategy" not in sys.modules
    assert list(module.PAYLOAD_KEYS) == list(PAYLOAD_KEYS)


def test_cli_module_imports_without_the_heavy_layers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same check as ``tests/test_cli.py``, extended to the forecasting layer."""
    for name in ("trading_platform.strategy", "trading_platform.forecast"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, "trading_platform.cli", raising=False)

    module = importlib.import_module("trading_platform.cli")

    assert module.app is not None
    assert sorted(module.PAYLOAD_KEYS) == sorted(PAYLOAD_KEYS)
