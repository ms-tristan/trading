"""The operator CLI: argument parsing, provisioning, status and strategy listing.

No test opens a socket or starts a process: every HTTP call goes through an
``httpx.MockTransport`` installed over the CLI's own client factory, and the
serving command is driven with a patched ``asyncio.run`` that only inspects the
coroutine it was handed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from trading_platform import __main__ as cli
from trading_platform.api.security import OPERATOR_TOKEN_HEADER
from trading_platform.config import ENV_OPERATOR_TOKEN
from trading_platform.models import ProfileConfig, StrategyMeta
from trading_platform.profiles.catalogue import StrategyCatalogue

TOKEN = "operator-token-of-the-test"


# ---------------------------------------------------------------------------
# Doubles and helpers
# ---------------------------------------------------------------------------
def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Route every CLI HTTP call to ``handler`` and record the requests."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)

    def factory(api_url: str) -> httpx.Client:
        return httpx.Client(transport=transport, base_url=api_url)

    monkeypatch.setattr(cli, "_api_client", factory)
    return seen


def _json_handler(payloads: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Answer each path with the payload registered for it."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = payloads.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"detail": f"no stub for {request.url.path}"})
        return httpx.Response(200, json=body)

    return handler


def _catalogue(*strategies: str) -> StrategyCatalogue:
    return StrategyCatalogue(
        [
            StrategyMeta(id=name, class_name=f"{name.title()}Strategy", title=name)
            for name in strategies
        ]
    )


def _profile(profile_id: str, **overrides: Any) -> ProfileConfig:
    fields: dict[str, Any] = {
        "id": profile_id,
        "name": profile_id,
        "strategy": "basic",
        "timeframe": "1h",
        "pairs": ["BTC/USDT"],
        "initial_capital": 1000.0,
    }
    fields.update(overrides)
    return ProfileConfig(**fields)


def _served_profile(profile_id: str, **overrides: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": profile_id,
        "name": profile_id,
        "strategy": "basic",
        "timeframe": "1h",
        "mode": "paper",
        "exchange": "binance",
        "pairs": ["BTC/USDT"],
        "initial_capital": 1000.0,
        "max_open_trades": 2,
        "priority": 100,
        "rank": 1,
        "state": "running",
        "portfolio_value": 1100.0,
        "profit_pct": 0.1,
        "open_trades": 1,
        "closed_trades": 4,
    }
    view.update(overrides)
    return view


@pytest.fixture(autouse=True)
def _configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OPERATOR_TOKEN, TOKEN)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def test_realtime_run_defaults() -> None:
    args = cli.build_parser().parse_args(["realtime", "run"])

    assert args.command == "realtime"
    assert args.realtime_command == "run"
    assert args.state_db is None
    assert args.host == "127.0.0.1"
    assert args.port == 8080


def test_realtime_run_options() -> None:
    args = cli.build_parser().parse_args(
        [
            "realtime",
            "run",
            "--state-db",
            "/tmp/state.db",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
        ]
    )

    assert args.state_db == Path("/tmp/state.db")
    assert (args.host, args.port) == ("0.0.0.0", 9000)


def test_realtime_provision_defaults() -> None:
    args = cli.build_parser().parse_args(["realtime", "provision"])

    assert args.api_url == "http://127.0.0.1:8080"
    assert (args.dry_run, args.prune, args.force) == (False, False, False)


def test_realtime_status_defaults() -> None:
    args = cli.build_parser().parse_args(["realtime", "status"])

    assert args.api_url == "http://127.0.0.1:8080"


def test_strategies_list_is_a_command() -> None:
    args = cli.build_parser().parse_args(["strategies", "list"])

    assert args.strategies_command == "list"


@pytest.mark.parametrize("argv", [[], ["realtime"], ["strategies"], ["unknown"]])
def test_usage_errors_keep_the_argparse_exit_code(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)

    assert excinfo.value.code == 2


def test_the_token_can_never_be_passed_as_an_argument() -> None:
    """There is no ``--token`` option: the token only ever comes from the env."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["realtime", "provision", "--token", TOKEN])

    assert excinfo.value.code == 2


def test_version_is_printed() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# realtime provision
# ---------------------------------------------------------------------------
def test_provision_posts_the_apply_request_with_the_environment_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_profile_catalogue", lambda: [_profile("basic-btc-1h")])
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))
    seen = _install_transport(
        monkeypatch,
        _json_handler(
            {
                "/api/catalogue/apply": {
                    "created": ["basic-btc-1h"],
                    "updated": [],
                    "skipped": [],
                    "pruned": [],
                    "refused_live": [],
                }
            }
        ),
    )

    exit_code = cli.main(["realtime", "provision", "--api-url", "http://api.test", "--prune"])

    assert exit_code == 0
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/catalogue/apply"
    assert request.headers[OPERATOR_TOKEN_HEADER] == TOKEN
    assert request.content == b'{"prune":true}'
    printed = capsys.readouterr().out
    assert "catalogue applied through http://api.test" in printed
    assert "created: basic-btc-1h" in printed
    assert "refused_live: -" in printed


def test_provision_refuses_without_a_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(ENV_OPERATOR_TOKEN, raising=False)
    monkeypatch.setattr(cli, "load_profile_catalogue", lambda: [_profile("basic-btc-1h")])
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))
    seen = _install_transport(monkeypatch, _json_handler({}))

    exit_code = cli.main(["realtime", "provision", "--api-url", "http://api.test"])

    assert exit_code == 1
    assert seen == []
    assert ENV_OPERATOR_TOKEN in capsys.readouterr().err


def test_provision_dry_run_prints_the_plan_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "load_profile_catalogue",
        lambda: [
            _profile("already-there"),
            _profile("renamed", name="Renamed"),
            _profile("repaired", pairs=["SOL/USDT"]),
            _profile("resized", initial_capital=2500.0),
            _profile("new-one"),
            _profile("live-one", mode="live"),
        ],
    )
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))
    monkeypatch.delenv("TB_ALLOW_LIVE_TRADING", raising=False)
    served = {
        "already-there": _served_profile("already-there"),
        "renamed": _served_profile("renamed"),
        "repaired": _served_profile("repaired"),
        "resized": _served_profile("resized"),
        "orphan": _served_profile("orphan"),
    }
    seen = _install_transport(
        monkeypatch, _json_handler({"/api/profiles": {"profiles": list(served.values())}})
    )

    exit_code = cli.main(
        ["realtime", "provision", "--api-url", "http://api.test", "--dry-run", "--prune"]
    )

    assert exit_code == 0
    assert [request.method for request in seen] == ["GET"]
    printed = capsys.readouterr().out
    assert "dry run against http://api.test: nothing was changed" in printed
    assert "created: new-one" in printed
    assert "updated: renamed, repaired, resized" in printed
    assert "skipped: already-there" in printed
    assert "pruned: orphan" in printed
    assert "refused_live: live-one" in printed
    assert "the API only deletes catalogue-owned rows" in printed


def test_provision_dry_run_warns_about_an_incoherent_catalogue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "load_profile_catalogue",
        lambda: [_profile("broken", strategy="missing", timeframe="7m")],
    )
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))
    seen = _install_transport(monkeypatch, _json_handler({"/api/profiles": {"profiles": []}}))

    exit_code = cli.main(["realtime", "provision", "--dry-run"])

    assert exit_code == 0
    assert [request.method for request in seen] == ["GET"]
    captured = capsys.readouterr()
    assert "unknown strategy: missing" in captured.err
    assert "unsupported timeframe: 7m" in captured.err


def test_provision_refuses_an_incoherent_catalogue_without_force(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "load_profile_catalogue", lambda: [_profile("broken", strategy="missing")]
    )
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))
    seen = _install_transport(monkeypatch, _json_handler({}))

    exit_code = cli.main(["realtime", "provision", "--api-url", "http://api.test"])

    assert exit_code == 1
    assert seen == []
    captured = capsys.readouterr()
    assert "unknown strategy: missing" in captured.err
    assert "--force" in captured.err


def test_provision_force_applies_an_incoherent_catalogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli, "load_profile_catalogue", lambda: [_profile("broken", strategy="missing")]
    )
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))
    seen = _install_transport(
        monkeypatch,
        _json_handler({"/api/catalogue/apply": {"created": ["broken"]}}),
    )

    exit_code = cli.main(["realtime", "provision", "--api-url", "http://api.test", "--force"])

    assert exit_code == 0
    assert [request.method for request in seen] == ["POST"]


def test_provision_refuses_a_duplicated_profile_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "load_profile_catalogue",
        lambda: [_profile("twice"), _profile("twice")],
    )
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))
    seen = _install_transport(monkeypatch, _json_handler({}))

    exit_code = cli.main(["realtime", "provision"])

    assert exit_code == 1
    assert seen == []
    assert "duplicate profile id: twice" in capsys.readouterr().err


def test_provision_tolerates_an_answer_without_id_lists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed apply answer is printed as \"no ids\" instead of crashing."""
    monkeypatch.setattr(cli, "load_profile_catalogue", lambda: [_profile("basic-btc-1h")])
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))
    _install_transport(
        monkeypatch,
        _json_handler({"/api/catalogue/apply": {"created": "basic-btc-1h"}}),
    )

    exit_code = cli.main(["realtime", "provision", "--api-url", "http://api.test"])

    assert exit_code == 0
    printed = capsys.readouterr().out
    assert "created: -" in printed


def test_provision_reports_an_api_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_profile_catalogue", lambda: [_profile("basic-btc-1h")])
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(403, json={"detail": "invalid operator token"}),
    )

    exit_code = cli.main(["realtime", "provision", "--api-url", "http://api.test"])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "HTTP 403" in error
    assert "invalid operator token" in error


def test_provision_reports_an_unreachable_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_profile_catalogue", lambda: [_profile("basic-btc-1h")])
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: _catalogue("basic"))

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_transport(monkeypatch, boom)

    exit_code = cli.main(["realtime", "provision", "--api-url", "http://api.test"])

    assert exit_code == 1
    assert "failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# realtime status
# ---------------------------------------------------------------------------
def test_status_prints_the_health_line_and_the_ranked_fleet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_transport(
        monkeypatch,
        _json_handler(
            {
                "/api/health": {
                    "status": "ok",
                    "version": "1.0.0",
                    "profiles_total": 2,
                    "profiles_running": 1,
                    "profiles_healthy": 1,
                    "profiles_queued": 0,
                    "kill_switch_engaged": False,
                    "uptime_seconds": 3720,
                },
                "/api/profiles": {
                    "profiles": [
                        _served_profile("momentum-btc-1h", rank=1, portfolio_value=1250.5),
                        _served_profile(
                            "basic-eth-4h",
                            rank=2,
                            mode="live",
                            state="blocked",
                            portfolio_value=980.0,
                            profit_pct=-0.02,
                            open_trades=0,
                            closed_trades=7,
                        ),
                    ],
                    "total": 2,
                },
            }
        ),
    )

    exit_code = cli.main(["realtime", "status", "--api-url", "http://api.test"])

    assert exit_code == 0
    printed = capsys.readouterr().out
    assert "status: ok" in printed
    assert "slots" not in printed
    assert "queued" not in printed
    assert "kill switch: released" in printed
    assert "uptime: 1h 02m" in printed
    assert "RANK" in printed
    assert "PROFILE" in printed
    assert "TRADES" in printed
    assert "momentum-btc-1h" in printed
    assert "1,250.50" in printed
    assert "+10.00%" in printed
    assert "basic-eth-4h" in printed
    assert "-2.00%" in printed
    assert "0/7" in printed


def test_status_reports_an_unreachable_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_transport(
        monkeypatch, lambda request: httpx.Response(500, json={"detail": "engine exploded"})
    )

    exit_code = cli.main(["realtime", "status", "--api-url", "http://api.test"])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "HTTP 500" in error
    assert "engine exploded" in error


def test_status_reports_an_error_body_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(502, text="<html>bad gateway</html>"),
    )

    exit_code = cli.main(["realtime", "status"])

    assert exit_code == 1
    assert "bad gateway" in capsys.readouterr().err


def test_status_reports_an_error_body_without_a_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(500, json=["boom"]))

    exit_code = cli.main(["realtime", "status"])

    assert exit_code == 1
    assert "boom" in capsys.readouterr().err


def test_status_reports_an_answer_that_is_not_a_json_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=["health"]))

    exit_code = cli.main(["realtime", "status"])

    assert exit_code == 1
    assert "did not answer a JSON object" in capsys.readouterr().err


def test_status_reports_an_answer_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(200, text="not json"))

    exit_code = cli.main(["realtime", "status"])

    assert exit_code == 1
    assert "did not answer JSON" in capsys.readouterr().err


def test_status_says_so_when_the_fleet_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_transport(
        monkeypatch,
        _json_handler(
            {
                "/api/health": {"status": "degraded", "profiles_running": 0},
                "/api/profiles": {"profiles": []},
            }
        ),
    )

    exit_code = cli.main(["realtime", "status"])

    assert exit_code == 0
    printed = capsys.readouterr().out
    assert "status: degraded" in printed
    assert "no profiles" in printed


# ---------------------------------------------------------------------------
# strategies list
# ---------------------------------------------------------------------------
def test_strategies_list_prints_the_catalogue_with_profile_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "load_strategy_catalogue",
        lambda: StrategyCatalogue(
            [
                StrategyMeta(
                    id="basic",
                    class_name="BasicStrategy",
                    title="EMA cross baseline",
                    category="baseline",
                ),
                StrategyMeta(
                    id="faber",
                    class_name="FaberStrategy",
                    title="Faber trend",
                    category="trend",
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        cli,
        "load_profile_catalogue",
        lambda: [_profile("a", strategy="basic"), _profile("b", strategy="basic")],
    )

    exit_code = cli.main(["strategies", "list"])

    assert exit_code == 0
    printed = capsys.readouterr().out
    assert "ID" in printed
    assert "CLASS" in printed
    assert "PROFILES" in printed
    assert "BasicStrategy" in printed
    assert "EMA cross baseline" in printed
    assert "FaberStrategy" in printed
    lines = {line.split()[0]: line for line in printed.splitlines() if line.strip()}
    assert lines["basic"].split()[3] == "2"
    assert lines["faber"].split()[3] == "0"


def test_strategies_list_without_strategies_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_strategy_catalogue", lambda: StrategyCatalogue([]))
    monkeypatch.setattr(cli, "load_profile_catalogue", list)

    exit_code = cli.main(["strategies", "list"])

    assert exit_code == 0
    assert "no strategies discovered" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# realtime run
# ---------------------------------------------------------------------------
def test_run_builds_the_engine_over_the_requested_state_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(coroutine: Any) -> int:
        captured["coroutine"] = coroutine
        coroutine.close()
        return 0

    def fake_create_app(**kwargs: Any) -> str:
        captured["app_kwargs"] = kwargs
        return "the-application"

    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    monkeypatch.setattr(cli, "create_app", fake_create_app)
    state_db = tmp_path / "realtime" / "state.db"

    exit_code = cli.main(
        [
            "realtime",
            "run",
            "--state-db",
            str(state_db),
            "--host",
            "0.0.0.0",
            "--port",
            "8099",
        ]
    )

    assert exit_code == 0
    assert state_db.parent.is_dir()
    kwargs = captured["app_kwargs"]
    assert kwargs["start_engine"] is False
    assert kwargs["state_dir"] == state_db.parent
    supervisor = kwargs["supervisor"]
    assert supervisor.store.path == state_db
    assert supervisor.settings.snapshot_interval_seconds == 60


def test_run_reports_a_broken_state_db_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A path that cannot be a database directory is a failure, not a traceback."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    exit_code = cli.main(["realtime", "run", "--state-db", str(blocker / "state.db")])

    assert exit_code == 1
    assert "error:" in capsys.readouterr().err


def test_an_interrupted_command_exits_with_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted() -> list[ProfileConfig]:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "load_strategy_catalogue", interrupted)

    exit_code = cli.main(["strategies", "list"])

    assert exit_code == 1
    assert "interrupted" in capsys.readouterr().err


async def test_shutdown_watcher_stops_the_server() -> None:
    class _Server:
        should_exit = False

    server = _Server()
    shutdown = asyncio.Event()
    shutdown.set()

    await cli._watch_shutdown(shutdown, server)  # type: ignore[arg-type]

    assert server.should_exit is True


def test_the_server_leaves_the_signals_to_the_cli() -> None:
    """uvicorn must not install its own SIGINT/SIGTERM handler here."""
    server = cli._OperatorServer.__new__(cli._OperatorServer)

    assert server.install_signal_handlers() is None


def test_a_first_signal_asks_for_a_graceful_shutdown() -> None:
    class _Server:
        should_exit = False
        force_exit = False

    server = _Server()
    shutdown = cli._ShutdownRequest(server)  # type: ignore[arg-type]

    shutdown()

    assert shutdown.requested.is_set() is True
    assert server.force_exit is False


def test_a_second_signal_forces_the_exit() -> None:
    """A worker that ignores SIGTERM must not be able to block a shutdown."""

    class _Server:
        should_exit = False
        force_exit = False

    server = _Server()
    shutdown = cli._ShutdownRequest(server)  # type: ignore[arg-type]

    shutdown()
    shutdown()

    assert server.force_exit is True


async def test_serve_boots_the_engine_polls_and_stops_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole lifecycle runs without a socket: a fake server returns at once."""
    events: list[str] = []

    class _Store:
        closed = False

        def close(self) -> None:
            self.closed = True

    class _Supervisor:
        def __init__(self) -> None:
            self.store = _Store()

        def bootstrap(self) -> None:
            events.append("bootstrap")

        async def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    class _Poller:
        def __init__(self, supervisor: Any) -> None:
            self.supervisor = supervisor
            events.append("poller-created")

        async def run_forever(self) -> None:
            events.append("polling")
            await asyncio.Event().wait()

        def stop(self) -> None:
            events.append("poller-stopped")

    class _Server:
        should_exit = False
        force_exit = False

        async def serve(self) -> None:
            events.append("serving")

    monkeypatch.setattr(cli, "SnapshotPoller", _Poller)
    supervisor = _Supervisor()
    server = _Server()

    exit_code = await cli._serve(supervisor, server)  # type: ignore[arg-type]

    assert exit_code == 0
    assert events[:3] == ["bootstrap", "start", "poller-created"]
    assert "serving" in events
    assert events[-2:] == ["poller-stopped", "stop"]
    assert supervisor.store.closed is True


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (42, "42s"), (90, "1m 30s"), (3720, "1h 02m"), (183600, "2d 03h")],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert cli._format_duration(seconds) == expected


@pytest.mark.parametrize("value", [None, "many", float("nan")])
def test_format_duration_survives_a_broken_value(value: Any) -> None:
    assert cli._format_duration(value) == "0s"


def test_table_pads_every_column() -> None:
    table = cli._table(("A", "BB"), [("1", "2"), ("longer", "x")])

    lines: Sequence[str] = table.splitlines()
    assert lines[0].startswith("A")
    assert set(lines[1]) == {"-", " "}
    assert lines[2].startswith("1")
    assert "longer" in lines[3]


# ---------------------------------------------------------------------------
# The planning and transport seams
# ---------------------------------------------------------------------------
def test_plan_changes_compares_only_the_fields_the_api_serves() -> None:
    """A field the served view does not carry cannot make a plan update."""
    assert cli._plan_changes({"name": "same"}, _profile("same", pairs=["SOL/USDT"])) is False
    assert cli._plan_changes({"name": "other"}, _profile("same")) is True
    assert cli._plan_changes({"pairs": ["BTC/USDT"]}, _profile("same")) is False


def test_api_client_targets_the_requested_url() -> None:
    with cli._api_client("http://api.test:1234") as client:
        assert str(client.base_url) == "http://api.test:1234"
