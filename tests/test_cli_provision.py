"""Contract tests of ``trading realtime provision`` (work package wp-profile-catalogue-cli).

The command is driven in-process through ``CliRunner``, and its API client is
replaced by a stub: the real apply path (the plan, the money check, the live gate,
the report) runs against it, so these tests pin the **CLI surface** -- the options,
the one JSON object on stdout, the exit codes and the frozen import policy --
without ever opening a socket.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from trading_platform.cli import app
from trading_platform.core.errors import ProfileError
from trading_platform.profiles import apply as profiles_apply
from trading_platform.profiles.catalogue import PROFILE_CATALOGUE

REPO_ROOT = Path(__file__).resolve().parents[1]

RUNNER = CliRunner()

#: ANSI SGR escape sequences emitted by rich when the CLI runs in a colour-forcing
#: environment (GitHub Actions sets ``GITHUB_ACTIONS``, which makes typer force the
#: terminal on even under ``CliRunner``).  Rich styles *inside* tokens -- ``--api-url``
#: comes out as ``\x1b[1m-\x1b[0m\x1b[1m-api\x1b[0m\x1b[1m-url\x1b[0m`` -- so a raw
#: substring check on ``result.output`` would only pass on a non-colourised local run.
#: The captured streams are therefore de-colourised, never the assertions relaxed.
_ANSI_SGR = re.compile(rb"\x1b\[[0-9;]*m")

#: Terminal width pinned for the ``--help`` assertions.  At the default 80 columns rich
#: folds a long option name mid-token, which hides it from a substring check for reasons
#: that have nothing to do with the command surface.
HELP_COLUMNS = "200"

#: The frozen payload keys of the ``realtime-provision`` command.
PAYLOAD_KEYS = frozenset(
    {
        "command",
        "ok",
        "api_url",
        "dry_run",
        "paper_total_initial_balance",
        "ledger_capacity",
        "created",
        "skipped",
        "pruned",
        "failed",
        "refused_live",
    }
)

#: Every identifier of the catalogue, in order.
CATALOGUE_IDS = tuple(definition.id for definition in PROFILE_CATALOGUE)

#: The five momentum profiles the deployed state database already holds.
LEGACY_PROFILE_IDS = (
    "momentumarpa",
    "momentumbome",
    "momentumbtc",
    "momentumeth",
    "momentumsol",
)

#: The single live row of the catalogue and the variables its arming needs.
LIVE_ID = "momentum-dot-1d-live"
LIVE_GATE_ENV = "TB_ALLOW_LIVE_TRADING"
LIVE_GATE_VALUE = "I_UNDERSTAND_THE_RISK"

#: A paper ledger that can fund the catalogue.
RICH_LEDGER: dict[str, Any] = {
    "total_cash": 100_000.0,
    "positions_value": 0.0,
    "initial_balance": 100_000.0,
}

#: The environment variables the tests always control (a real host may set them).
ENVIRONMENT_VARIABLES = (
    "TB_API_URL",
    "TB_OPERATOR_TOKEN",
    LIVE_GATE_ENV,
    "TB_LIVE_API_KEY",
    "TB_LIVE_API_SECRET",
    "TB_LIVE_API_PASSWORD",
    "TB_PROFILE_MOMENTUM_DOT_1D_LIVE_API_KEY",
    "TB_PROFILE_MOMENTUM_DOT_1D_LIVE_API_SECRET",
)


class StubClient:
    """The three profile routes the apply path calls, with a scripted outcome.

    ``ids``, ``ledger`` and ``fail_create`` are class attributes so a test can
    script the platform before invoking the command; every instance records the
    calls it received, which is how "no mutation happened" is asserted.
    """

    ids: ClassVar[tuple[str, ...]] = ()
    ledger: ClassVar[dict[str, Any] | None] = RICH_LEDGER
    fail_create: ClassVar[dict[str, str]] = {}
    instances: ClassVar[list[StubClient]] = []

    def __init__(self, base_url: str, token: str | None, **_ignored: Any) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.token = token
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.methods: list[str] = []
        StubClient.instances.append(self)

    def list_profiles(self) -> dict[str, Any]:
        """Answer ``GET /api/profiles`` with the scripted identifiers and ledger."""
        self.methods.append("GET")
        return {
            "profiles": [{"profile_id": identifier} for identifier in type(self).ids],
            "generated_at": "2024-01-01T00:00:00+00:00",
            "wallet": None,
            "wallets": {"paper": type(self).ledger, "live": None},
        }

    def create_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """Answer ``POST /api/profiles``: record the create, or fail it loudly."""
        identifier = str(body["profile_id"])
        self.methods.append("POST")
        error = type(self).fail_create.get(identifier)
        if error is not None:
            raise ProfileError(error)
        self.created.append(identifier)
        return {"profile": {"profile_id": identifier}}

    def delete_profile(self, profile_id: str) -> dict[str, Any]:
        """Answer ``DELETE /api/profiles/{id}``: record the deletion."""
        self.methods.append("DELETE")
        self.deleted.append(str(profile_id))
        return {"profile_id": str(profile_id), "deleted": True}


@pytest.fixture(autouse=True)
def stubbed_client(monkeypatch: pytest.MonkeyPatch) -> StubClient:
    """Reset the stub, clear the credentials from the environment and patch the client."""
    StubClient.ids = ()
    StubClient.ledger = dict(RICH_LEDGER)
    StubClient.fail_create = {}
    StubClient.instances = []
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(profiles_apply, "ProfileApiClient", StubClient)
    return StubClient


def invoke(*args: str, env: dict[str, str] | None = None) -> Any:
    """Run the CLI in-process and return the click result, without ANSI styling.

    ``env`` adds environment overrides for this one invocation: the ``--help`` tests
    pin a wide ``COLUMNS`` so rich never folds a long option name mid-token.  Stripping
    the styling keeps the run deterministic whatever the colour environment of the
    machine running the suite (docs/testing-policy.md section 1, rule 3).
    """
    result = RUNNER.invoke(app, list(args), env=env)
    result.stdout_bytes = _ANSI_SGR.sub(b"", result.stdout_bytes)
    result.stderr_bytes = _ANSI_SGR.sub(b"", result.stderr_bytes)
    result.output_bytes = _ANSI_SGR.sub(b"", result.output_bytes)
    return result


def last_client() -> StubClient:
    """Return the client the last invocation built."""
    assert StubClient.instances, "the command never built a client"
    return StubClient.instances[-1]


def arm_live_trading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm the live gate and the global credentials, as the engine would need them."""
    monkeypatch.setenv(LIVE_GATE_ENV, LIVE_GATE_VALUE)
    monkeypatch.setenv("TB_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("TB_LIVE_API_SECRET", "live-secret")


# ---------------------------------------------------------------------------
# 1. the command surface
# ---------------------------------------------------------------------------


def test_provision_help_lists_the_four_options() -> None:
    result = invoke("realtime", "provision", "--help", env={"COLUMNS": HELP_COLUMNS})

    assert result.exit_code == 0
    for option in ("--api-url", "--dry-run", "--prune", "--force", "--json"):
        assert option in result.output


def test_provision_help_never_offers_a_token_option() -> None:
    """The operator token travels through the environment, never through argv."""
    result = invoke("realtime", "provision", "--help", env={"COLUMNS": HELP_COLUMNS})

    assert "--token" not in result.output
    assert "TB_OPERATOR_TOKEN" in result.output


def test_the_realtime_group_still_lists_its_historical_commands() -> None:
    result = invoke("realtime", "--help", env={"COLUMNS": HELP_COLUMNS})

    assert result.exit_code == 0
    for command in ("run", "serve", "check", "provision"):
        assert command in result.output


# ---------------------------------------------------------------------------
# 2. the payload and the exit codes
# ---------------------------------------------------------------------------


def test_a_dry_run_prints_one_json_object_with_the_documented_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arm_live_trading(monkeypatch)

    result = invoke("realtime", "provision", "--dry-run", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == PAYLOAD_KEYS
    assert payload["command"] == "realtime-provision"
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["api_url"] == "http://127.0.0.1:8080"
    assert payload["created"] == list(CATALOGUE_IDS)
    assert payload["paper_total_initial_balance"] == 26_000.0
    assert payload["ledger_capacity"] == 100_000.0
    assert last_client().methods == ["GET"]


def test_a_dry_run_of_a_fully_provisioned_platform_has_nothing_to_do() -> None:
    StubClient.ids = CATALOGUE_IDS

    result = invoke("realtime", "provision", "--dry-run", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["created"] == []
    assert payload["skipped"] == list(CATALOGUE_IDS)
    assert payload["failed"] == []


def test_exit_code_is_one_when_a_create_is_refused() -> None:
    StubClient.fail_create = {"macd-btc-4h": "HTTP 400: unsupported timeframe: '3m'"}

    result = invoke("realtime", "provision", "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["failed"] == [
        {"id": "macd-btc-4h", "error": "HTTP 400: unsupported timeframe: '3m'"}
    ]
    assert "macd-btc-4h" in result.stderr
    assert "unsupported timeframe" in result.stderr


def test_exit_code_is_one_when_the_live_entry_is_refused() -> None:
    """Without the gate and the credentials no live profile is ever created."""
    result = invoke("realtime", "provision", "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert [entry["id"] for entry in payload["refused_live"]] == [LIVE_ID]
    assert LIVE_ID not in last_client().created
    assert LIVE_GATE_ENV in result.stderr
    assert "TB_PROFILE_MOMENTUM_DOT_1D_LIVE_API_KEY" in result.stderr


def test_the_live_entry_is_created_when_the_engine_is_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arm_live_trading(monkeypatch)

    result = invoke("realtime", "provision", "--json")

    assert result.exit_code == 0, result.output
    assert LIVE_ID in last_client().created
    assert json.loads(result.stdout)["refused_live"] == []


def test_the_human_output_names_the_refusals_on_stderr() -> None:
    result = invoke("realtime", "provision")

    assert result.exit_code == 1
    assert "realtime-provision" in result.stdout
    assert "26,000.0000" in result.stdout
    assert "error:" in result.stderr
    assert "TB_ALLOW_LIVE_TRADING" in result.stderr


def test_the_dry_run_human_output_says_would_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arm_live_trading(monkeypatch)

    result = invoke("realtime", "provision", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "would create: 27" in result.stdout
    assert "created: 27" not in result.stdout


def test_the_human_output_of_a_successful_run_lists_what_it_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arm_live_trading(monkeypatch)
    StubClient.ledger = None  # a platform that never booted: capacity unknown

    result = invoke("realtime", "provision")

    assert result.exit_code == 0, result.output
    assert "capacity of unknown" in result.stdout
    assert "created: momentum-btc-4h" in result.stdout
    assert "created: 27" in result.stdout


# ---------------------------------------------------------------------------
# 3. the options
# ---------------------------------------------------------------------------


def test_the_api_url_comes_from_the_option_then_the_environment_then_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = invoke(
        "realtime", "provision", "--dry-run", "--json", "--api-url", "http://explicit.test/"
    )
    assert last_client().base_url == "http://explicit.test"
    assert json.loads(explicit.stdout)["api_url"] == "http://explicit.test"

    monkeypatch.setenv("TB_API_URL", "http://from-env.test")
    from_env = invoke("realtime", "provision", "--dry-run", "--json")
    assert last_client().base_url == "http://from-env.test"
    assert json.loads(from_env.stdout)["api_url"] == "http://from-env.test"

    monkeypatch.setenv("TB_API_URL", "")
    from_default = invoke("realtime", "provision", "--dry-run", "--json")
    assert last_client().base_url == "http://127.0.0.1:8080"
    assert json.loads(from_default.stdout)["api_url"] == "http://127.0.0.1:8080"


def test_the_operator_token_is_read_from_the_environment_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TB_OPERATOR_TOKEN", "operator-token")
    invoke("realtime", "provision", "--dry-run", "--json")
    assert last_client().token == "operator-token"

    monkeypatch.setenv("TB_OPERATOR_TOKEN", "   ")
    invoke("realtime", "provision", "--dry-run", "--json")
    assert last_client().token is None


def test_the_default_run_deletes_nothing_and_prune_is_opt_in() -> None:
    StubClient.ids = LEGACY_PROFILE_IDS

    invoke("realtime", "provision", "--json")
    assert last_client().deleted == []
    assert last_client().methods.count("DELETE") == 0

    result = invoke("realtime", "provision", "--dry-run", "--prune", "--json")
    payload = json.loads(result.stdout)
    assert payload["pruned"] == sorted(LEGACY_PROFILE_IDS)
    assert last_client().deleted == []

    invoke("realtime", "provision", "--prune", "--json")
    assert last_client().deleted == sorted(LEGACY_PROFILE_IDS)


def test_force_bypasses_the_ledger_check(monkeypatch: pytest.MonkeyPatch) -> None:
    arm_live_trading(monkeypatch)
    StubClient.ledger = {"total_cash": 10.0, "positions_value": 0.0}

    refused = invoke("realtime", "provision", "--json")
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["created"] == []

    forced = invoke("realtime", "provision", "--force", "--json")
    assert forced.exit_code == 0, forced.output
    assert len(json.loads(forced.stdout)["created"]) == len(CATALOGUE_IDS)


# ---------------------------------------------------------------------------
# 4. the frozen import policy
# ---------------------------------------------------------------------------


def test_importing_the_cli_does_not_import_the_new_layers() -> None:
    """``trading_platform.profiles`` joins layers 6 and 7 as a body-only import."""
    script = (
        "import sys; import trading_platform.cli; "
        "print([name for name in ('trading_platform.realtime', 'trading_platform.web', "
        "'trading_platform.profiles') if name in sys.modules])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"
