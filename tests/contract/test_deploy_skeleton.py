"""Contract test: the frozen deployment skeleton and the CI/deploy workflows.

These files are shared infrastructure: several work packages build against them
and none of them may edit them. The module therefore checks them read-only, with
text inspection only -- no YAML or Docker import, no process, no network:

* ``deploy/docker-compose.yml`` (services, publishes, volumes, ``env_file``);
* ``deploy/Dockerfile.realtime`` and ``deploy/Dockerfile.dashboard``;
* ``.github/workflows/deploy.yml`` (frozen: smoke assertions plus a content digest);
* ``.github/workflows/ci.yml`` (triggers, runner labels, concurrency);
* ``dashboard/next.config.ts`` (standalone output and the ``/api`` rewrite).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``deploy.yml`` is part of the deployment skeleton and is frozen: pulling a
#: change into it (especially a second test/build gate, which belongs to ci.yml)
#: is a contract violation, not a refactoring. If the specification ever
#: re-freezes this file, this digest is the single constant to update.
FROZEN_DEPLOY_WORKFLOW_SHA256 = "4093548b755bd373242b8fa614fbb9a01f925a2607a711b4ac255e275c6579aa"

REALTIME_BASE_IMAGE = "freqtradeorg/freqtrade:2026.8"
REALTIME_ENTRYPOINT = ["python", "-m", "trading_platform"]
REALTIME_CMD = [
    "realtime",
    "run",
    "--state-db",
    "/app/data/realtime/state.db",
    "--host",
    "0.0.0.0",
    "--port",
    "8080",
]

DASHBOARD_BASE_IMAGE = "node:24-alpine"
DASHBOARD_CMD = ["node", "server.js"]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _body_lines(document: str) -> list[str]:
    """The lines of a text file, blank lines and comments removed."""
    return [
        line.rstrip()
        for line in document.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _yaml_block(document: str, key: str) -> list[str]:
    """The raw lines of the top-level YAML block introduced by ``key:``."""
    lines = document.splitlines()
    start = next((index for index, line in enumerate(lines) if line.rstrip() == f"{key}:"), None)
    assert start is not None, f"missing the top-level {key}: block"

    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and _indent(line) == 0:
            break
        block.append(line)
    return block


def _mapping_keys(block: list[str]) -> list[str]:
    """The keys of a YAML mapping block, comments and blank lines ignored."""
    return [
        line.strip()[:-1]
        for line in block
        if line.strip()
        and not line.lstrip().startswith("#")
        and _indent(line) == 2
        and line.strip().endswith(":")
    ]


def _compose_service(document: str, service: str) -> list[str]:
    """The raw lines of one service of a compose document."""
    lines = document.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.rstrip() == f"  {service}:"), None
    )
    assert start is not None, f"deploy/docker-compose.yml no longer declares the service {service}"

    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and _indent(line) < 4 and not line.lstrip().startswith("#"):
            break
        block.append(line)
    return block


def _dockerfile_instructions(document: str) -> list[str]:
    """The instructions of a Dockerfile, comments removed and continuations joined."""
    instructions: list[str] = []
    pending = ""
    for raw_line in document.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].strip()
            continue
        instructions.append(pending)
        pending = ""
    if pending:
        instructions.append(pending)
    return instructions


def _exec_form(instruction: str) -> list[str]:
    """The JSON argument vector of a ``CMD``/``ENTRYPOINT`` exec-form instruction."""
    start = instruction.index("[")
    return json.loads(instruction[start:])


def _from_images(instructions: list[str]) -> list[str]:
    images: list[str] = []
    for instruction in instructions:
        parts = instruction.split()
        if parts and parts[0].upper() == "FROM":
            images.append(parts[1])
    return images


def _run_instructions(instructions: list[str]) -> list[str]:
    return [instruction for instruction in instructions if instruction.upper().startswith("RUN ")]


# ---------------------------------------------------------------------------
# deploy/docker-compose.yml
# ---------------------------------------------------------------------------
def test_compose_declares_exactly_the_two_frozen_services() -> None:
    document = _read("deploy/docker-compose.yml")
    services = _mapping_keys(_yaml_block(document, "services"))
    assert services == ["trading-realtime", "trading-dashboard"], (
        "deploy/docker-compose.yml must declare exactly trading-realtime and "
        f"trading-dashboard, found {services}"
    )


def test_compose_publishes_both_ports_on_the_loopback_interface() -> None:
    document = _read("deploy/docker-compose.yml")
    realtime = _body_lines("\n".join(_compose_service(document, "trading-realtime")))
    dashboard = _body_lines("\n".join(_compose_service(document, "trading-dashboard")))

    assert any("127.0.0.1:3030:8080" in line for line in realtime), (
        "trading-realtime must publish 127.0.0.1:3030:8080"
    )
    assert any("127.0.0.1:3031:3000" in line for line in dashboard), (
        "trading-dashboard must publish 127.0.0.1:3031:3000"
    )


def test_compose_mounts_the_two_named_volumes_on_the_realtime_service() -> None:
    document = _read("deploy/docker-compose.yml")
    realtime = _body_lines("\n".join(_compose_service(document, "trading-realtime")))

    assert any("trading-state:/app/data/realtime" in line for line in realtime), (
        "trading-realtime must mount trading-state:/app/data/realtime"
    )
    assert any("trading-cache:/app/data/cache" in line for line in realtime), (
        "trading-realtime must mount trading-cache:/app/data/cache"
    )

    top_level_volumes = _mapping_keys(_yaml_block(document, "volumes"))
    for volume in ("trading-state", "trading-cache"):
        assert volume in top_level_volumes, f"the named volume {volume} is no longer declared"


def test_compose_reads_the_realtime_secrets_from_the_env_file() -> None:
    document = _read("deploy/docker-compose.yml")
    block = _body_lines("\n".join(_compose_service(document, "trading-realtime")))

    env_file = next(
        (index for index, line in enumerate(block) if line.strip() == "env_file:"), None
    )
    assert env_file is not None, "trading-realtime must declare env_file:"
    assert block[env_file + 1].strip() == "- .env", (
        "trading-realtime must load its secrets with `env_file: - .env`, found "
        f"{block[env_file + 1].strip()!r}"
    )


# ---------------------------------------------------------------------------
# deploy/Dockerfile.realtime
# ---------------------------------------------------------------------------
def test_realtime_image_builds_on_the_frozen_freqtrade_base_image() -> None:
    instructions = _dockerfile_instructions(_read("deploy/Dockerfile.realtime"))
    assert REALTIME_BASE_IMAGE in _from_images(instructions), (
        f"deploy/Dockerfile.realtime must build FROM {REALTIME_BASE_IMAGE}"
    )


def test_realtime_image_installs_the_package_without_resolving_dependencies() -> None:
    instructions = _dockerfile_instructions(_read("deploy/Dockerfile.realtime"))
    installs = [run for run in _run_instructions(instructions) if "--no-deps" in run]
    assert installs, (
        "deploy/Dockerfile.realtime must install the package with --no-deps so the base "
        "image's own pins win"
    )


def test_realtime_image_never_runs_as_root() -> None:
    instructions = _dockerfile_instructions(_read("deploy/Dockerfile.realtime"))
    assert "USER ftuser" in instructions, (
        "deploy/Dockerfile.realtime must drop to the base image user with `USER ftuser`"
    )


def test_realtime_image_healthchecks_the_aggregated_api() -> None:
    instructions = _dockerfile_instructions(_read("deploy/Dockerfile.realtime"))
    healthchecks = [
        instruction for instruction in instructions if instruction.upper().startswith("HEALTHCHECK")
    ]
    assert len(healthchecks) == 1, (
        "deploy/Dockerfile.realtime must declare exactly one HEALTHCHECK, found "
        f"{len(healthchecks)}"
    )
    healthcheck = healthchecks[0]
    assert "urllib" in healthcheck and "http://127.0.0.1:8080/api/health" in healthcheck, (
        "the realtime healthcheck must probe http://127.0.0.1:8080/api/health with urllib: "
        f"{healthcheck}"
    )


def test_realtime_image_entrypoint_and_cmd_are_the_specified_ones() -> None:
    instructions = _dockerfile_instructions(_read("deploy/Dockerfile.realtime"))

    entrypoints = [
        instruction for instruction in instructions if instruction.upper().startswith("ENTRYPOINT")
    ]
    assert len(entrypoints) == 1, "deploy/Dockerfile.realtime must declare exactly one ENTRYPOINT"
    assert _exec_form(entrypoints[0]) == REALTIME_ENTRYPOINT, (
        f"the realtime ENTRYPOINT must be {REALTIME_ENTRYPOINT}, found {_exec_form(entrypoints[0])}"
    )

    commands = [
        instruction for instruction in instructions if instruction.upper().startswith("CMD ")
    ]
    assert len(commands) == 1, "deploy/Dockerfile.realtime must declare exactly one CMD"
    assert _exec_form(commands[0]) == REALTIME_CMD, (
        f"the realtime CMD must be {REALTIME_CMD}, found {_exec_form(commands[0])}"
    )


# ---------------------------------------------------------------------------
# deploy/Dockerfile.dashboard
# ---------------------------------------------------------------------------
def test_dashboard_image_builds_and_runs_the_standalone_server() -> None:
    instructions = _dockerfile_instructions(_read("deploy/Dockerfile.dashboard"))

    images = _from_images(instructions)
    assert images and set(images) == {DASHBOARD_BASE_IMAGE}, (
        f"deploy/Dockerfile.dashboard must build on {DASHBOARD_BASE_IMAGE}, found {images}"
    )

    installs = [run for run in _run_instructions(instructions) if run.split()[-2:] == ["npm", "ci"]]
    assert installs, "deploy/Dockerfile.dashboard must install the lockfile with `npm ci`"

    copies = [i for i in instructions if i.upper().startswith("COPY ")]
    assert any("/app/.next/standalone" in instruction for instruction in copies), (
        "deploy/Dockerfile.dashboard must copy the .next/standalone output"
    )

    commands = [
        instruction for instruction in instructions if instruction.upper().startswith("CMD ")
    ]
    assert len(commands) == 1, "deploy/Dockerfile.dashboard must declare exactly one CMD"
    assert _exec_form(commands[0]) == DASHBOARD_CMD, (
        f"the dashboard CMD must be {DASHBOARD_CMD}, found {_exec_form(commands[0])}"
    )


# ---------------------------------------------------------------------------
# .github/workflows/deploy.yml (frozen)
# ---------------------------------------------------------------------------
def test_deploy_workflow_is_frozen() -> None:
    digest = hashlib.sha256(
        (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_bytes()
    ).hexdigest()
    assert digest == FROZEN_DEPLOY_WORKFLOW_SHA256, (
        ".github/workflows/deploy.yml is part of the frozen deployment skeleton and must not be "
        f"edited (expected sha256 {FROZEN_DEPLOY_WORKFLOW_SHA256}, found {digest})"
    )


def test_deploy_workflow_smoke_test_asserts_a_booted_engine() -> None:
    document = _read(".github/workflows/deploy.yml")
    body = "\n".join(_body_lines(document))
    assert 'h["status"]=="ok"' in body, 'the deploy smoke test must assert h["status"]=="ok"'
    assert 'h["profiles_running"]>0' in body, (
        'the deploy smoke test must assert h["profiles_running"]>0'
    )


def test_deploy_workflow_still_runs_on_a_push_to_main() -> None:
    document = _read(".github/workflows/deploy.yml")
    triggers = _mapping_keys(_yaml_block(document, "on"))
    assert triggers == ["push", "workflow_dispatch"], (
        f"deploy.yml must keep its push + workflow_dispatch triggers, found {triggers}"
    )
    assert "branches: [main]" in "\n".join(_yaml_block(document, "on")), (
        "deploy.yml must keep deploying on a push to main"
    )


# ---------------------------------------------------------------------------
# .github/workflows/ci.yml
# ---------------------------------------------------------------------------
def test_ci_workflow_never_runs_on_a_push() -> None:
    document = _read(".github/workflows/ci.yml")
    block = _yaml_block(document, "on")
    triggers = _mapping_keys(block)
    assert triggers == ["pull_request", "workflow_dispatch"], (
        "ci.yml must trigger on pull_request + workflow_dispatch only, found "
        f"{triggers}; the push trigger belongs to deploy.yml"
    )
    assert not [line for line in block if re.match(r"\s*push\s*:", line)], (
        "ci.yml must never declare a push trigger"
    )


def test_ci_workflow_runs_on_the_self_hosted_arm64_runner() -> None:
    document = _read(".github/workflows/ci.yml")
    assert re.search(r"runs-on:\s*\[self-hosted,\s*macOS,\s*ARM64\]", document), (
        "ci.yml must run on [self-hosted, macOS, ARM64]"
    )


def test_ci_workflow_cancels_the_previous_run_of_the_same_ref() -> None:
    document = _read(".github/workflows/ci.yml")
    concurrency = "\n".join(_body_lines("\n".join(_yaml_block(document, "concurrency"))))
    assert re.search(r"group:\s*ci-\$\{\{\s*github\.ref\s*\}\}", concurrency), (
        "ci.yml must group its runs with `ci-${{ github.ref }}`"
    )
    assert re.search(r"cancel-in-progress:\s*true", concurrency), (
        "ci.yml must set cancel-in-progress: true"
    )


# ---------------------------------------------------------------------------
# dashboard/next.config.ts
# ---------------------------------------------------------------------------
def test_dashboard_config_emits_the_standalone_output() -> None:
    document = _read("dashboard/next.config.ts")
    assert re.search(r"""output:\s*["']standalone["']""", document), (
        'dashboard/next.config.ts must declare output: "standalone"'
    )


def test_dashboard_config_rewrites_api_calls_to_the_api_origin() -> None:
    document = _read("dashboard/next.config.ts")
    assert '"/api/:path*"' in document, (
        "dashboard/next.config.ts must rewrite the /api/:path* source"
    )
    assert re.search(r"\$\{\s*apiOrigin\s*\}\s*/\s*api/\s*:\s*path\*", document), (
        "the /api/:path* rewrite must target `${apiOrigin}/api/:path*`"
    )
    assert "process.env.API_ORIGIN" in document, (
        "the API origin must be configurable through the API_ORIGIN environment variable"
    )
