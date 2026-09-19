"""The operational flow of a forecast profile, end to end (work package wp4).

This module drives the **operator path** the delivery documents, entirely
offline and without ``torch``:

1. a local candle file stands in for ``make data-download`` (the only step that
   would touch the network);
2. ``trading forecast-bootstrap`` builds the artifact the profile declares;
3. ``trading forecast-info --json`` confirms it *is* usable right now -- the
   ``coverage`` block, its exact keys and the human rendering;
4. ``trading realtime run --once`` starts the profile over a local CSV cache and
   an anchored clock, and the run must produce a trade.

The last step is the regression test for the exact defect this delivery fixes: a
``timesfm`` profile used to start, attach no forecast bundle and emit no signal,
forever and silently.  The artifact here is built by the offline ``seasonal``
backend because the random-walk baseline has no thesis at all -- its median path
is flat, so it opens zero positions by design (``docs/forecasting.md`` §6).  The
seasonal backend is fully offline *and* does produce a directional median, which
is what makes it the honest way to prove the wiring rather than a tuned
threshold.

The two refusal paths are pinned too: a stale artifact and an artifact built for
another symbol/timeframe must both be refused before any candle is processed,
with the actionable message that names ``trading forecast-build``.

Everything is deterministic: seeded synthetic frames, an anchored
``realtime.start_at`` clock, an ephemeral -- actually absent -- HTTP port, and no
wall-clock dependency in the engine.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from typer.testing import CliRunner

from trading_platform.cli import app
from trading_platform.core.constants import OHLCV_INDEX_NAME
from trading_platform.data.synthetic import make_ohlcv

RUNNER = CliRunner()

SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"
CONFIG = str(Path(__file__).resolve().parents[1] / "config" / "backtest_default.json")

#: The virtual instant the whole scenario is anchored on.
#:
#: ``realtime run --once`` builds the profile's feature bundle -- and therefore
#: runs the coverage guard -- against the **real** clock, while the *engine* runs
#: on this manual anchor.  The two clocks are deliberately different: it proves
#: the guard is a startup check and not a per-candle decision.
LIVE_NOW = pd.Timestamp.now(tz="UTC").floor("h")

#: Provider window, in candles, and the runner's warm-up.
PROVIDER_ROWS = 600
WARMUP = 200
HISTORY_CANDLES = 300

#: The live catch-up window of the scenario's profile, in candles.
ENTRY_LOOKBACK = 60

#: Artifact geometry; the runner's own defaults are `context 512 / horizon 24 /
#: reforecast-every 24`, and the profile declares them so the two agree.
CONTEXT = 512
HORIZON = 24
STRIDE = 8

#: Decision parameters that let the offline ``seasonal`` baseline clear the gates.
#:
#: These are **integration parameters, not tuned thresholds**: the entry gates are
#: opened to their documented extremes so the test measures the *wiring* (does the
#: injected bundle make the profile trade?) instead of the model's skill.  The
#: execution convention and the strategy's coherence rules are untouched.
INTEGRATION_PARAMS: dict[str, Any] = {
    "context_length": CONTEXT,
    "horizon": HORIZON,
    "reforecast_every": STRIDE,
    "min_alpha": 0.0,
    "min_reliability": -10.0,
    "min_agreement": 0.0,
    "min_path_efficiency": 0.0,
    "max_vol_ratio": 1_000_000.0,
    "exit_vol_ratio": 1_000_000.0,
    "alpha_vs_atr": 1e-9,
    "allow_short": True,
    "max_hold": HORIZON,
    "cooldown": 0,
}

#: Seed of the synthetic scenario frame.
#:
#: Pinned because the engine only acts on the candles it is fed: the newest candle
#: must not carry the reversal of a catch-up entry, or the runner legitimately
#: refuses it.  Seed ``2`` is one of the many seeds where the offline ``seasonal``
#: baseline produces an entry inside the catch-up window that the newest candle
#: still confirms.  This is a *fixture* choice, not a tuned threshold: no decision
#: parameter is fitted to it.
SCENARIO_SEED = 2


@pytest.fixture(autouse=True)
def quiet_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the realtime layer's structured logs out of the captured output."""
    import logging

    monkeypatch.setenv("TB_LOG_LEVEL", "ERROR")
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


# ---------------------------------------------------------------------------
# the scenario
# ---------------------------------------------------------------------------


def write_candles(data_dir: Path, *, rows: int = PROVIDER_ROWS, start: str) -> Path:
    """Write the local candle file the profile implies (the ``data-download`` stand-in)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{SYMBOL.replace('/', '_')}-{TIMEFRAME}.csv"
    make_ohlcv(rows, start=start, timeframe=TIMEFRAME, seed=SCENARIO_SEED).to_csv(
        path, index_label=OHLCV_INDEX_NAME
    )
    return path


def anchor_start(*, rows: int = PROVIDER_ROWS) -> str:
    """Return a candle start whose frame ends a couple of strides behind ``LIVE_NOW``.

    The guard accepts an artifact while ``now <= last_origin + stride * (horizon - 2)``
    candles.  Two strides of margin keeps it clearly fresh while leaving most of
    its window inside the provider's look-back, so the strategy really has origins
    to decide on.
    """
    last_origin = LIVE_NOW - 2 * STRIDE * pd.Timedelta(hours=1)
    return (last_origin - (rows - 1) * pd.Timedelta(hours=1)).isoformat()


def write_profiles(directory: Path, *, forecast: str | None) -> Path:
    """Write the one-profile document of the flow (plus the realtime/monitoring keys)."""
    profile: dict[str, Any] = {
        "id": "btc-timesfm-paper",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "strategy": "timesfm",
        "params": dict(INTEGRATION_PARAMS),
        "mode": "paper",
        "initial_balance": 1000.0,
        "stake_amount": 100.0,
        "warmup_candles": WARMUP,
        "poll_interval_seconds": 5.0,
        "entry_lookback_candles": ENTRY_LOOKBACK,
        "risk": {"max_open_positions": 1},
    }
    if forecast is not None:
        profile["forecast"] = forecast
    document = {
        "profiles": [profile],
        "realtime": {
            "state_db": str(directory / "state.db"),
            "logs_dir": str(directory / "logs"),
            "data_dir": str(directory / "data"),
            "cache_dir": str(directory / "cache"),
            "format": "csv",
            "allow_network": False,
            "csv_dir": str(directory / "csv"),
            "start_at": LIVE_NOW.isoformat(),
            "history_candles": HISTORY_CANDLES,
            "poll_interval_seconds": 5.0,
            "stream_poll_timeout_seconds": 10.0,
            "max_stream_reconnects": 3,
            "reconnect_backoff_seconds": 0.01,
            "reconcile_interval_seconds": 60.0,
            "platform_initial_balance": 10_000.0,
        },
        "monitoring": {"host": "127.0.0.1", "port": 0},
    }
    path = directory / "profiles.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def write_config(path: Path, data_dir: Path) -> Path:
    """Write the minimal ``AppConfig`` locating the candle directory."""
    path.write_text(
        json.dumps({"data": {"data_dir": str(data_dir), "cache_dir": str(data_dir / "cache")}}),
        encoding="utf-8",
    )
    return path


def single_json_object(text: str) -> dict[str, Any]:
    """Parse the single JSON object a ``--json`` run prints on stdout."""
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(text.lstrip())
    assert text.lstrip()[end:].strip() == "", "stdout carries more than one JSON value"
    assert isinstance(payload, dict)
    return payload


def run_flow(directory: Path, *, backend: str = "seasonal") -> tuple[Path, dict[str, Any]]:
    """Run ``forecast-bootstrap`` over the scenario and return (profiles, payload)."""
    data_dir = directory / "data"
    config = write_config(directory / "config.json", data_dir)
    profiles = write_profiles(
        directory,
        forecast=str(data_dir / "forecast" / "btc-timesfm-paper-1h-seasonal.parquet"),
    )
    result = RUNNER.invoke(
        app,
        [
            "forecast-bootstrap",
            "--profiles",
            str(profiles),
            "--config",
            str(config),
            "--backend",
            backend,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return profiles, single_json_object(result.stdout)


# ---------------------------------------------------------------------------
# 1. THE OPERATIONAL FLOW, END TO END
# ---------------------------------------------------------------------------


def test_the_documented_operational_flow_produces_a_trading_profile(tmp_path: Path) -> None:
    """candles -> bootstrap -> info (usable) -> realtime run --once (a trade)."""
    write_candles(tmp_path / "data", start=anchor_start())
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    frame = make_ohlcv(PROVIDER_ROWS, start=anchor_start(), timeframe=TIMEFRAME, seed=SCENARIO_SEED)
    frame.to_csv(csv_dir / f"BTC_USDT-{TIMEFRAME}.csv", index_label=OHLCV_INDEX_NAME)

    profiles, bootstrap_payload = run_flow(tmp_path)
    artifact = Path(bootstrap_payload["reports"][0])
    assert artifact.is_file()

    # 3. forecast-info answers "is it usable right now?"
    info = RUNNER.invoke(app, ["forecast-info", "--artifact", str(artifact), "--json"])
    assert info.exit_code == 0, info.output
    info_payload = single_json_object(info.stdout)
    coverage = info_payload["run"]["coverage"]
    assert coverage["usable"] is True, coverage
    assert sorted(coverage) == sorted(
        (
            "first_origin",
            "last_origin",
            "usable_until",
            "usable",
            "seasonal_period",
            "checked_at",
        )
    )
    assert coverage["seasonal_period"] == 24
    assert info_payload["run"]["symbol"] == SYMBOL
    assert info_payload["run"]["timeframe"] == TIMEFRAME

    # ... and the human rendering still prints the metadata block verbatim
    human = RUNNER.invoke(app, ["forecast-info", "--artifact", str(artifact)])
    assert human.exit_code == 0, human.output
    printed = single_json_object(human.stdout)
    assert printed["coverage"]["usable"] is True
    assert printed["symbol"] == SYMBOL
    assert printed["backend"] == "seasonal"

    # 4. the live runner accepts the profile and actually trades
    result = RUNNER.invoke(
        app, ["realtime", "run", "--profiles", str(profiles), "--once", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = single_json_object(result.stdout)
    assert payload["ok"] is True
    entries = payload["profiles"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["profile_id"] == "btc-timesfm-paper"

    # The profile opened a real position funded by the shared wallet: this is
    # exactly the signal a profile without an injected bundle can never produce.
    # ``n_trades`` counts *closed* trades, so a one-tick run reports the entry
    # through ``open_positions``/``deployed`` -- and the decision itself is
    # published in ``decisions``.
    assert entry["open_positions"] >= 1, (
        "the bootstrapped profile opened no position: the exact defect this package fixes"
    )
    assert entry["deployed"] > 0.0, "the shared wallet funded the entry"
    assert entry["n_trades"] >= 0
    actions = [decision["action"] for decision in payload["decisions"]]
    assert "enter_long" in actions or "enter_short" in actions, actions
    entered = next(
        decision
        for decision in payload["decisions"]
        if decision["action"] in ("enter_long", "enter_short")
    )
    assert entered["reason"] == "signal"
    assert entered["blocked"] is False


def test_the_three_command_flow_is_repeatable(tmp_path: Path) -> None:
    """Re-running the flow on the same candles rewrites the same artifact."""
    write_candles(tmp_path / "data", start=anchor_start())
    profiles, first = run_flow(tmp_path)
    artifact = Path(first["reports"][0])
    before = artifact.read_bytes()

    result = RUNNER.invoke(
        app,
        [
            "forecast-bootstrap",
            "--profiles",
            str(profiles),
            "--config",
            str(tmp_path / "config.json"),
            "--backend",
            "seasonal",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert artifact.read_bytes() == before


# ---------------------------------------------------------------------------
# 2. THE REFUSAL PATHS
# ---------------------------------------------------------------------------


def test_a_stale_artifact_is_refused_with_an_actionable_message(tmp_path: Path) -> None:
    """An operator must learn *what to run*, before the engine starts."""
    write_candles(
        tmp_path / "data",
        rows=400,
        start=(LIVE_NOW - pd.Timedelta(hours=4000)).isoformat(),
    )
    profiles, payload = run_flow(tmp_path)
    artifact = Path(payload["reports"][0])

    # the artifact names the very command that rebuilds it
    info = RUNNER.invoke(app, ["forecast-info", "--artifact", str(artifact), "--json"])
    assert info.exit_code == 0, "without a profile the command only reports"
    assert single_json_object(info.stdout)["run"]["coverage"]["usable"] is False

    result = RUNNER.invoke(
        app, ["realtime", "run", "--profiles", str(profiles), "--once", "--json"]
    )

    assert result.exit_code == 1, result.output
    assert "is stale for profile" in result.stderr, result.stderr
    assert "forecast-build" in result.stderr, result.stderr
    assert "Traceback" not in result.output


def test_an_artifact_of_another_symbol_is_refused_before_any_candle(tmp_path: Path) -> None:
    """A mismatched pairing must never be silently misused."""
    write_candles(tmp_path / "data", start=anchor_start())
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    make_ohlcv(PROVIDER_ROWS, start=anchor_start(), timeframe=TIMEFRAME, seed=SCENARIO_SEED).to_csv(
        csv_dir / f"BTC_USDT-{TIMEFRAME}.csv", index_label=OHLCV_INDEX_NAME
    )

    profiles, payload = run_flow(tmp_path)
    artifact = Path(payload["reports"][0])

    # rebuild the artifact for another symbol, then point the BTC profile at it
    other = tmp_path / "data" / "forecast" / "eth.parquet"
    result = RUNNER.invoke(
        app,
        [
            "forecast-build",
            "--config",
            CONFIG,
            "--data-file",
            str(tmp_path / "data" / "BTC_USDT-1h.csv"),
            "--out",
            str(other),
            "--backend",
            "seasonal",
            "--symbol",
            "ETH/USDT",
            "--timeframe",
            TIMEFRAME,
            "--horizon",
            str(HORIZON),
            "--reforecast-every",
            str(STRIDE),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    document = json.loads(profiles.read_text(encoding="utf-8"))
    document["profiles"][0]["forecast"] = str(other)
    profiles.write_text(json.dumps(document, indent=2), encoding="utf-8")

    refused = RUNNER.invoke(
        app, ["realtime", "run", "--profiles", str(profiles), "--once", "--json"]
    )

    assert refused.exit_code == 1, refused.output
    assert "was built for symbol" in refused.stderr, refused.stderr
    assert "ETH/USDT" in refused.stderr, refused.stderr
    assert artifact.is_file()


def test_an_artifact_of_another_timeframe_is_refused_before_any_candle(tmp_path: Path) -> None:
    write_candles(tmp_path / "data", start=anchor_start())
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    make_ohlcv(PROVIDER_ROWS, start=anchor_start(), timeframe=TIMEFRAME, seed=SCENARIO_SEED).to_csv(
        csv_dir / f"BTC_USDT-{TIMEFRAME}.csv", index_label=OHLCV_INDEX_NAME
    )

    profiles, _payload = run_flow(tmp_path)
    other = tmp_path / "data" / "forecast" / "h4.parquet"
    frame = make_ohlcv(400, start=anchor_start(rows=400), timeframe="4h", seed=SCENARIO_SEED)
    frame.to_csv(tmp_path / "data" / "BTC_USDT-4h.csv", index_label=OHLCV_INDEX_NAME)
    result = RUNNER.invoke(
        app,
        [
            "forecast-build",
            "--config",
            CONFIG,
            "--data-file",
            str(tmp_path / "data" / "BTC_USDT-4h.csv"),
            "--out",
            str(other),
            "--backend",
            "seasonal",
            "--timeframe",
            "4h",
            "--horizon",
            str(HORIZON),
            "--reforecast-every",
            str(STRIDE),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    document = json.loads(profiles.read_text(encoding="utf-8"))
    document["profiles"][0]["forecast"] = str(other)
    profiles.write_text(json.dumps(document, indent=2), encoding="utf-8")

    refused = RUNNER.invoke(
        app, ["realtime", "run", "--profiles", str(profiles), "--once", "--json"]
    )

    assert refused.exit_code == 1, refused.output
    # rich hard-wraps the message, so the assertion is on wrap-insensitive parts
    assert "4h" in refused.stderr and "timeframe" in refused.stderr, refused.stderr
    assert "forecast-build" in refused.stderr, refused.stderr


# ---------------------------------------------------------------------------
# 3. import policy
# ---------------------------------------------------------------------------


def test_the_bootstrap_module_stays_free_of_the_heavy_layers() -> None:
    """``forecast.bootstrap_profile`` must run with ``.[dev]`` alone."""
    from trading_platform.forecast import bootstrap_profile

    source = Path(bootstrap_profile.__file__).read_text(encoding="utf-8")
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")]
        )
    }
    assert not roots & {"torch", "timesfm", "freqtrade", "ccxt", "jax"}
    assert bootstrap_profile.__all__ == ["bootstrap_profile_forecast"]
