"""Operator CLI: run the engine, provision the catalogue, read the fleet.

Four commands, all documented in ``README.md`` and ``docs/operations.md``:

* ``trading realtime run`` -- the production entry point of the realtime
  container: it owns the engine's lifecycle (bootstrap, start, snapshot polling,
  graceful stop through ``SIGTERM``/``SIGINT``) and serves the API with uvicorn;
* ``trading realtime provision`` -- applies ``config/profiles.json`` through
  ``POST /api/catalogue/apply``; the operator token is read from
  ``TB_OPERATOR_TOKEN`` in the **environment only**, never from ``argv``, so it
  cannot leak into a shell history or a process list;
* ``trading realtime status`` -- prints the health line and the ranked fleet of a
  running engine as a plain-text table;
* ``trading strategies list`` -- prints the discovered strategy catalogue.

Exit codes: ``0`` on success, ``1`` on a failure or a refusal, ``2`` for an
argparse usage error (the argparse default). No command ever prints a token or an
exchange credential.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from . import __version__
from .api.app import create_app
from .api.security import OPERATOR_TOKEN_HEADER
from .config import ENV_OPERATOR_TOKEN, PlatformSettings, live_trading_gate, operator_token
from .engine.poller import SnapshotPoller
from .engine.supervisor import Supervisor
from .logging_setup import configure_logging
from .models import SUPPORTED_TIMEFRAMES, ProfileConfig, finite_float
from .paths import resolve_state_db_path, state_dir_for
from .profiles.catalogue import StrategyCatalogue, load_profile_catalogue, load_strategy_catalogue
from .profiles.store import StateStore

__all__ = ["build_parser", "main"]

#: Host the local engine binds to by default (the container overrides both).
DEFAULT_HOST = "127.0.0.1"

#: Port the local engine binds to by default.
DEFAULT_PORT = 8080

#: Aggregated API a command talks to by default.
DEFAULT_API_URL = "http://127.0.0.1:8080"

#: Timeout of one CLI HTTP call, in seconds.
REQUEST_TIMEOUT_SECONDS = 10.0

#: Paths of the aggregated API used by the provision and status commands.
HEALTH_PATH = "/api/health"
PROFILES_PATH = "/api/profiles"
CATALOGUE_APPLY_PATH = "/api/catalogue/apply"

EXIT_OK = 0
EXIT_FAILURE = 1

#: Fields a dry run compares between the local catalogue and the served views.
#: ``enabled`` is not part of ``ProfileView`` and is therefore not comparable.
PLAN_FIELDS: tuple[str, ...] = (
    "name",
    "strategy",
    "timeframe",
    "mode",
    "exchange",
    "pairs",
    "initial_capital",
    "max_open_trades",
    "priority",
)


class _CommandError(RuntimeError):
    """A failure or a refusal the CLI reports as a one-line message and exit 1."""


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser of the whole CLI."""
    parser = argparse.ArgumentParser(
        prog="trading",
        description="Multi-profile crypto trading platform supervised by Freqtrade.",
    )
    parser.add_argument("--version", action="version", version=f"trading-platform {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    realtime = commands.add_parser("realtime", help="run and operate the trading engine")
    realtime_commands = realtime.add_subparsers(dest="realtime_command", required=True)

    run = realtime_commands.add_parser("run", help="run the supervisor and the aggregated API")
    run.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="SQLite state database (default: the resolved platform state database)",
    )
    run.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default: {DEFAULT_HOST})")
    run.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"bind port (default: {DEFAULT_PORT})"
    )
    run.set_defaults(handler=_run_command)

    provision = realtime_commands.add_parser(
        "provision", help="apply config/profiles.json to a running engine"
    )
    provision.add_argument("--api-url", default=DEFAULT_API_URL, help="base URL of the API")
    provision.add_argument(
        "--dry-run", action="store_true", help="print the plan without changing anything"
    )
    provision.add_argument(
        "--prune", action="store_true", help="also delete the catalogue-owned rows the file dropped"
    )
    provision.add_argument(
        "--force", action="store_true", help="apply even when the catalogue is incoherent"
    )
    provision.set_defaults(handler=_provision_command)

    status = realtime_commands.add_parser("status", help="print the health and the ranked fleet")
    status.add_argument("--api-url", default=DEFAULT_API_URL, help="base URL of the API")
    status.set_defaults(handler=_status_command)

    strategies = commands.add_parser("strategies", help="inspect the strategy catalogue")
    strategies_commands = strategies.add_subparsers(dest="strategies_command", required=True)
    strategies_list = strategies_commands.add_parser(
        "list", help="print the discovered strategies and their profile counts"
    )
    strategies_list.set_defaults(handler=_strategies_command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI command and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:  # pragma: no cover - argparse requires a sub-command
        parser.print_usage(sys.stderr)
        return 2
    try:
        return int(handler(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_FAILURE
    except _CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE


# ---------------------------------------------------------------------------
# realtime run
# ---------------------------------------------------------------------------
def _run_command(args: argparse.Namespace) -> int:
    """Build the engine over the requested state database and serve the API."""
    settings = PlatformSettings.load()
    state_db = resolve_state_db_path(args.state_db)
    state_dir = state_dir_for(state_db)
    state_dir.mkdir(parents=True, exist_ok=True)
    configure_logging()
    supervisor = Supervisor(
        store=StateStore(state_db),
        settings=settings,
        state_dir=state_dir,
    )
    app = create_app(
        supervisor=supervisor,
        settings=settings,
        state_dir=state_dir,
        start_engine=False,
    )
    server = _OperatorServer(uvicorn.Config(app, host=str(args.host), port=int(args.port)))
    return asyncio.run(_serve(supervisor, server))


class _OperatorServer(uvicorn.Server):
    """uvicorn server whose signals belong to the CLI.

    uvicorn would otherwise install its own ``SIGINT``/``SIGTERM`` handler and
    stop serving without telling the supervisor; the CLI owns both signals so a
    ``Ctrl-C`` reaches the engine exactly once, through the shutdown path that
    also stops the workers and closes the state store.
    """

    def install_signal_handlers(self) -> None:
        """Leave ``SIGINT`` and ``SIGTERM`` to the CLI."""
        return None


class _ShutdownRequest:
    """The signal handler of the serving command.

    The first ``SIGINT``/``SIGTERM`` asks for a graceful shutdown: the server
    stops accepting, the lifespan of the command closes the workers and the state
    store. A second one forces the exit, which is the escape hatch when a worker
    refuses to die inside its terminate grace period.
    """

    def __init__(self, server: uvicorn.Server) -> None:
        self._server = server
        self.requested = asyncio.Event()

    def __call__(self) -> None:
        """Record the request, or force the exit when one was already made."""
        if self.requested.is_set():
            self._server.force_exit = True
        else:
            self.requested.set()


async def _serve(supervisor: Supervisor, server: uvicorn.Server) -> int:
    """Serve the API until the server stops, then stop the engine gracefully."""
    supervisor.bootstrap()
    await supervisor.start()
    poller = SnapshotPoller(supervisor)
    poller_task = asyncio.create_task(poller.run_forever())

    shutdown = _ShutdownRequest(server)

    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:  # pragma: no cover - Windows has no SIGTERM
            continue
        try:
            loop.add_signal_handler(signum, shutdown)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-main thread
            with suppress(ValueError):
                signal.signal(signum, lambda *_: shutdown())
        else:
            installed.append(signum)

    watcher = asyncio.create_task(_watch_shutdown(shutdown.requested, server))
    try:
        await server.serve()
    finally:
        watcher.cancel()
        poller.stop()
        poller_task.cancel()
        await asyncio.gather(watcher, poller_task, return_exceptions=True)
        for signum in installed:
            with suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(signum)
        await supervisor.stop()
        supervisor.store.close()
    return EXIT_OK


async def _watch_shutdown(shutdown: asyncio.Event, server: uvicorn.Server) -> None:
    """Stop the HTTP server as soon as a shutdown signal was received."""
    await shutdown.wait()
    server.should_exit = True


# ---------------------------------------------------------------------------
# realtime provision
# ---------------------------------------------------------------------------
def _provision_command(args: argparse.Namespace) -> int:
    """Apply the declarative catalogue through the API, or print the plan."""
    profiles = load_profile_catalogue()
    problems = _coherence_problems(profiles, load_strategy_catalogue())
    if args.dry_run:
        for problem in problems:
            print(f"warning: {problem}", file=sys.stderr)
        return _print_plan(args, profiles)
    if problems and not args.force:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        raise _CommandError(
            "the profile catalogue is not coherent with the platform; fix it or re-run with --force"
        )
    token = operator_token()
    if not token:
        raise _CommandError(
            f"{ENV_OPERATOR_TOKEN} is not set: the catalogue cannot be applied "
            "without the operator token (it is never read from the arguments)"
        )
    with _api_client(str(args.api_url)) as client:
        answer = _request_json(
            client,
            CATALOGUE_APPLY_PATH,
            method="post",
            json={"prune": bool(args.prune)},
            headers={OPERATOR_TOKEN_HEADER: token},
        )
    print(f"catalogue applied through {args.api_url}")
    for key in ("created", "updated", "skipped", "pruned", "refused_live"):
        _print_ids(key, _id_list(answer.get(key)))
    return EXIT_OK


def _print_plan(args: argparse.Namespace, profiles: Sequence[ProfileConfig]) -> int:
    """Print what an apply would change, without changing anything."""
    with _api_client(str(args.api_url)) as client:
        listing = _request_json(client, PROFILES_PATH)
    served = {
        str(item.get("id")): item
        for item in listing.get("profiles", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    refused_live: list[str] = []
    for profile in profiles:
        existing = served.get(profile.id)
        if existing is None:
            if not _live_allowed(profile.mode):
                refused_live.append(profile.id)
            else:
                created.append(profile.id)
            continue
        if _plan_changes(existing, profile):
            updated.append(profile.id)
        else:
            skipped.append(profile.id)
    pruned = sorted(set(served) - {profile.id for profile in profiles}) if args.prune else []

    print(f"dry run against {args.api_url}: nothing was changed")
    _print_ids("created", created)
    _print_ids("updated", updated)
    _print_ids("skipped", skipped)
    _print_ids("pruned", pruned)
    _print_ids("refused_live", refused_live)
    print(
        "  note: prune candidates are the ids the catalogue does not declare; "
        "the API only deletes catalogue-owned rows"
    )
    return EXIT_OK


def _coherence_problems(
    profiles: Sequence[ProfileConfig],
    strategies: StrategyCatalogue,
) -> list[str]:
    """Return why the local catalogue could not be applied as it stands."""
    problems: list[str] = []
    seen: set[str] = set()
    for profile in profiles:
        if profile.id in seen:
            problems.append(f"duplicate profile id: {profile.id}")
        seen.add(profile.id)
        if strategies.get(profile.strategy) is None:
            problems.append(f"profile {profile.id}: unknown strategy: {profile.strategy}")
        if profile.timeframe not in SUPPORTED_TIMEFRAMES:
            problems.append(f"profile {profile.id}: unsupported timeframe: {profile.timeframe}")
    return problems


def _plan_changes(existing: Mapping[str, Any], profile: ProfileConfig) -> bool:
    """Whether a dry run would update the served profile."""
    for name in PLAN_FIELDS:
        if name not in existing:
            continue
        current = existing[name]
        wanted = getattr(profile, name)
        if isinstance(wanted, list) or isinstance(current, list):
            if list(current or []) != list(wanted):
                return True
        elif isinstance(wanted, float) or isinstance(current, float):
            if float(current) != float(wanted):
                return True
        elif current != wanted:
            return True
    return False


def _live_allowed(mode: str) -> bool:
    """Whether a profile in ``mode`` may run, according to the live-trading gate."""
    allowed, _reason = live_trading_gate(mode)
    return allowed


# ---------------------------------------------------------------------------
# realtime status
# ---------------------------------------------------------------------------
def _status_command(args: argparse.Namespace) -> int:
    """Print the health line and the ranked profiles of a running engine."""
    with _api_client(str(args.api_url)) as client:
        health = _request_json(client, HEALTH_PATH)
        listing = _request_json(client, PROFILES_PATH)
    profiles = [item for item in listing.get("profiles", []) if isinstance(item, Mapping)]
    print(_health_line(health))
    print(_profile_table(profiles))
    return EXIT_OK


def _health_line(health: Mapping[str, Any]) -> str:
    """Render the one-line summary of ``GET /api/health``."""
    return (
        f"status: {health.get('status', 'unknown')} | version: {health.get('version', '')} | "
        f"profiles: {_as_int(health.get('profiles_total'))} total, "
        f"{_as_int(health.get('profiles_running'))} running, "
        f"{_as_int(health.get('profiles_healthy'))} healthy | "
        f"kill switch: {'engaged' if health.get('kill_switch_engaged') else 'released'} | "
        f"uptime: {_format_duration(health.get('uptime_seconds'))}"
    )


def _profile_table(profiles: Sequence[Mapping[str, Any]]) -> str:
    """Render the ranked fleet as a plain-text table."""
    rows = [
        (
            str(_as_int(profile.get("rank")) or index),
            str(profile.get("id", "")),
            str(profile.get("mode", "")),
            str(profile.get("state", "")),
            f"{finite_float(profile.get('portfolio_value'), 0.0):,.2f}",
            f"{finite_float(profile.get('profit_pct'), 0.0) * 100.0:+.2f}%",
            f"{_as_int(profile.get('open_trades'))}/{_as_int(profile.get('closed_trades'))}",
        )
        for index, profile in enumerate(profiles, start=1)
    ]
    table = _table(("RANK", "PROFILE", "MODE", "STATE", "PORTFOLIO", "PROFIT", "TRADES"), rows)
    return table if rows else f"{table}\nno profiles"


# ---------------------------------------------------------------------------
# strategies list
# ---------------------------------------------------------------------------
def _strategies_command(args: argparse.Namespace) -> int:
    """Print every discovered strategy with the number of profiles using it."""
    catalogue = load_strategy_catalogue()
    counts = Counter(profile.strategy for profile in load_profile_catalogue())
    rows = [
        (
            meta.id,
            meta.class_name,
            meta.category,
            str(counts.get(meta.id, 0)),
            meta.title,
        )
        for meta in catalogue.all()
    ]
    table = _table(("ID", "CLASS", "CATEGORY", "PROFILES", "TITLE"), rows)
    print(table if rows else f"{table}\nno strategies discovered")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _api_client(api_url: str) -> httpx.Client:
    """Return an HTTP client for the aggregated API.

    This function is the seam the tests replace with an
    ``httpx.MockTransport``-backed client, so no test ever opens a socket.
    """
    return httpx.Client(base_url=api_url, timeout=REQUEST_TIMEOUT_SECONDS)


def _request_json(
    client: httpx.Client,
    path: str,
    *,
    method: str = "get",
    **kwargs: Any,
) -> dict[str, Any]:
    """Call one API path and return its JSON object, or raise :class:`_CommandError`."""
    try:
        response = client.request(method.upper(), path, **kwargs)
    except httpx.HTTPError as exc:
        raise _CommandError(f"{method.upper()} {path} failed: {exc}") from exc
    if response.status_code >= 400:
        raise _CommandError(
            f"{method.upper()} {path} answered HTTP {response.status_code}: "
            f"{_error_detail(response)}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise _CommandError(f"{method.upper()} {path} did not answer JSON") from exc
    if not isinstance(payload, dict):
        raise _CommandError(f"{method.upper()} {path} did not answer a JSON object")
    return payload


def _error_detail(response: httpx.Response) -> str:
    """Return the ``detail`` of an error answer, or a short body excerpt."""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:200] or "no body"
    if isinstance(payload, Mapping) and "detail" in payload:
        return str(payload["detail"])[:200]
    return str(payload)[:200]


def _id_list(value: Any) -> list[str]:
    """Coerce one id list of the apply answer."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value]


def _print_ids(label: str, ids: Sequence[str]) -> None:
    """Print one ``label: a, b`` line of the plan and of the apply answer."""
    print(f"  {label}: {', '.join(ids) if ids else '-'}")


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a fixed-width text table with a header rule."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip()
    ]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        for row in rows
    )
    return "\n".join(lines)


def _format_duration(seconds: Any) -> str:
    """Render a duration in seconds as ``2d 03h``, ``1h 05m``, ``42s``."""
    total = max(0, int(finite_float(seconds, 0.0)))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a counter to ``int`` (the API sends real integers, but be safe)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
