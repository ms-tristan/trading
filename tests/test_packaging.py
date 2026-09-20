"""Packaging and tooling contract tests (work package wp-5).

Pure file-content assertions -- no network, no exchange, no market data:

* ``pyproject.toml`` is the single source of truth for the version, the console
  script and the coverage gate;
* ``requirements*.txt`` mirror the dependency floors declared there;
* ``Makefile`` / ``Dockerfile`` / ``.dockerignore`` expose the documented tasks;
* the bootstrap assets of the forecast flow (the two committed Freqtrade
  documents and the ``forecast-bootstrap*`` targets that build their artifacts)
  ship with the checkout, while the retired JSON profile documents do **not**
  (the profile set is a table of the SQLite state store);
* ``docs/testing-policy.md`` pins the full coverage command and the 85 % gate.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import trading_platform

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
POLICY = REPO_ROOT / "docs" / "testing-policy.md"
MAKEFILE = REPO_ROOT / "Makefile"
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
REQUIREMENTS_DEV = REPO_ROOT / "requirements-dev.txt"
REQUIREMENTS_FREQTRADE = REPO_ROOT / "requirements-freqtrade.txt"

#: The FULL pytest command of docs/testing-policy.md section 3, byte for byte.
COVERAGE_FLAGS = "--cov=trading_platform --cov-report=term-missing --cov-report=xml"

#: Coverage gate enforced by pyproject.toml and docker.
COVERAGE_THRESHOLD = 85

#: Every target `make help` must advertise.
REQUIRED_MAKE_TARGETS = (
    "help",
    "venv",
    "install",
    "install-dev",
    "lint",
    "format",
    "type-check",
    "test",
    "test-cov",
    "cov",
    "check",
    "backtest",
    "walk-forward",
    "robustness",
    "monte-carlo",
    "docker-build",
    "docker-test",
    "clean",
)

#: Accepted requirement form: ``name>=floor``, optionally with extras.
_DEPENDENCY = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?>=(?P<floor>[^\s;#]+)$"
)


def read(path: Path) -> str:
    """Return the UTF-8 content of ``path``."""
    return path.read_text(encoding="utf-8")


def pyproject() -> dict:
    """Parse ``pyproject.toml`` with the standard library."""
    return tomllib.loads(read(PYPROJECT))


def plain_lines(path: Path) -> list[str]:
    """Return the non-empty, non-comment lines of a requirement/Makefile file."""
    return [
        line.strip()
        for line in read(path).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def parse_dependency(text: str) -> tuple[str, str]:
    """Split ``pandas>=2.1`` (or ``coverage[toml]>=7.5``) into ``(name, floor)``."""
    match = _DEPENDENCY.match(text.strip())
    assert match, f"{text!r} does not match the 'name>=floor' form"
    return match.group("name").lower(), match.group("floor")


def parse_requirements(path: Path) -> dict[str, str]:
    """Return ``{distribution: floor}`` for the pinned lines of a requirements file."""
    entries: dict[str, str] = {}
    for line in plain_lines(path):
        if line.startswith("-r "):
            continue
        name, floor = parse_dependency(line)
        entries[name] = floor
    return entries


def makefile_text() -> str:
    """Return the content of the Makefile."""
    return read(MAKEFILE)


# ---------------------------------------------------------------------------
# 1. pyproject.toml -- version, console script, coverage gate
# ---------------------------------------------------------------------------


def test_pyproject_parses_and_version_matches_the_package() -> None:
    document = pyproject()

    assert document["project"]["name"] == "trading-platform"
    assert document["project"]["version"] == trading_platform.__version__
    assert document["project"]["requires-python"].startswith(">=3.11")


def test_console_script_points_at_the_cli_main() -> None:
    document = pyproject()

    assert document["project"]["scripts"]["trading-backtest"] == "trading_platform.cli:main"


def test_coverage_gate_is_wired_and_equals_85() -> None:
    document = pyproject()

    assert document["tool"]["coverage"]["report"]["fail_under"] == COVERAGE_THRESHOLD
    assert (
        f"--cov-fail-under={COVERAGE_THRESHOLD}"
        in document["tool"]["pytest"]["ini_options"]["addopts"]
    )
    assert document["tool"]["coverage"]["run"]["branch"] is False
    assert document["tool"]["coverage"]["run"]["source"] == ["trading_platform"]


def test_testing_policy_pins_the_full_command_and_the_threshold() -> None:
    policy = read(POLICY)

    assert COVERAGE_FLAGS in policy
    assert "85" in policy
    assert f"fail_under = {COVERAGE_THRESHOLD}" in policy


# ---------------------------------------------------------------------------
# 2. requirements*.txt -- consistent with pyproject.toml
# ---------------------------------------------------------------------------


def test_requirements_files_only_use_name_ge_floor_lines() -> None:
    for path in (REQUIREMENTS, REQUIREMENTS_DEV, REQUIREMENTS_FREQTRADE):
        assert parse_requirements(path), f"{path.name} declares no dependency"


def test_requirements_txt_matches_the_runtime_dependencies() -> None:
    declared = dict(parse_dependency(entry) for entry in pyproject()["project"]["dependencies"])

    assert parse_requirements(REQUIREMENTS) == declared
    assert len(declared) == 7


def test_requirements_dev_includes_the_runtime_file_and_the_dev_extra() -> None:
    lines = plain_lines(REQUIREMENTS_DEV)
    extras = dict(
        parse_dependency(entry) for entry in pyproject()["project"]["optional-dependencies"]["dev"]
    )

    assert lines[0] == "-r requirements.txt"
    assert parse_requirements(REQUIREMENTS_DEV) == extras


def test_requirements_freqtrade_includes_the_runtime_file_and_the_extra() -> None:
    lines = plain_lines(REQUIREMENTS_FREQTRADE)
    freqtrade = parse_requirements(REQUIREMENTS_FREQTRADE)

    # the runtime dependencies are pulled in through the -r include (asserted above
    # for the dev file as well): never duplicated by hand
    assert lines[0] == "-r requirements.txt"
    assert freqtrade == {"freqtrade": "2024.4"}
    assert set(parse_requirements(REQUIREMENTS)) & set(freqtrade) == set()


def test_heavy_optional_dependencies_stay_out_of_the_runtime_requirements() -> None:
    runtime = set(parse_requirements(REQUIREMENTS))

    assert "freqtrade" not in runtime
    assert "ccxt" not in runtime


# ---------------------------------------------------------------------------
# 3. Makefile
# ---------------------------------------------------------------------------


def test_makefile_declares_every_required_target() -> None:
    text = makefile_text()
    missing = [
        target
        for target in REQUIRED_MAKE_TARGETS
        if not re.search(rf"^{re.escape(target)}:", text, flags=re.MULTILINE)
    ]

    assert not missing, f"the Makefile has no rule for: {missing}"


def test_makefile_help_lists_every_required_target() -> None:
    text = makefile_text()
    missing = [target for target in REQUIRED_MAKE_TARGETS if f"make {target}" not in text]

    assert not missing, f"`make help` does not mention: {missing}"


def test_makefile_default_goal_and_interpreter_fallback() -> None:
    text = makefile_text()

    assert ".DEFAULT_GOAL := help" in text
    assert (
        "PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python;"
        " else echo python3.11; fi)" in text
    )
    assert ".PHONY:" in text


def test_makefile_test_cov_runs_the_documented_command() -> None:
    text = makefile_text()

    assert f"$(PYTHON) -m pytest tests {COVERAGE_FLAGS}" in text
    assert COVERAGE_FLAGS in read(POLICY)
    assert "$(PYTHON) -m ruff check ." in text
    assert "$(PYTHON) -m ruff format --check ." in text
    assert "$(PYTHON) -m mypy src" in text
    assert "$(PYTHON) -m pytest tests -q" in text
    assert "config/backtest_default.json" in text


def test_makefile_pipeline_targets_use_the_cli() -> None:
    text = makefile_text()

    for command in ("backtest", "walk-forward", "robustness", "monte-carlo"):
        assert f"$(PYTHON) -m trading_platform {command} --config $(CONFIG)" in text


#: The bootstrap assets the wheel and the documented flow depend on: the two
#: committed Freqtrade documents.  The profile documents were **removed**: the
#: profile set lives in the SQLite state store (``realtime.state_db``), so a
#: JSON profile file would be read by no one.
BOOTSTRAP_CONFIG_ASSETS = (
    "config/freqtrade_config.json",
    "config/freqtrade_dryrun.json",
)

#: The JSON profile documents the platform used to read, and no longer ships
#: under any name: no importer, no migration, no replacement.
DELETED_CONFIG_ASSETS = (
    "config/profiles.example.json",
    "config/profiles.timesfm.example.json",
    "deploy/profiles.json",
)


def test_the_shipped_configuration_assets_are_present_and_parsable() -> None:
    """Every bootstrap asset ships in the checkout and parses as JSON."""
    for relative in BOOTSTRAP_CONFIG_ASSETS:
        path = REPO_ROOT / relative

        assert path.is_file(), f"{relative} is missing from the checkout"
        assert json.loads(read(path)), f"{relative} is empty"


def test_the_bootstrap_assets_carry_no_profile_document() -> None:
    """The shipped configuration assets are Freqtrade documents and nothing else.

    A profile document back in this list would re-introduce the failure the
    state store removed: a committed file that looks authoritative, is rebuilt
    from git on every deployment, and silently disagrees with the running
    platform.
    """
    profiled = [relative for relative in BOOTSTRAP_CONFIG_ASSETS if "profiles" in relative]

    assert not profiled, f"the bootstrap assets still list profile documents: {profiled}"


def test_the_json_profile_documents_are_deleted() -> None:
    """Every retired profile document is gone from the checkout."""
    surviving = [relative for relative in DELETED_CONFIG_ASSETS if (REPO_ROOT / relative).exists()]

    assert not surviving, f"these JSON profile documents were not deleted: {surviving}"


def test_the_makefile_bootstraps_a_forecast_profile() -> None:
    """The three-command flow: download, build the artifact, build for a profile."""
    text = makefile_text()

    for target in ("data-download", "forecast-bootstrap", "forecast-bootstrap-seasonal"):
        assert re.search(rf"^{re.escape(target)}:", text, flags=re.MULTILINE), target
        assert f"make {target}" in text, f"`make help` does not mention {target!r}"

    for variable in ("SYMBOL ?=", "TIMEFRAME ?=", "FORECAST_DIR ?="):
        assert variable in text, f"the Makefile does not declare {variable!r}"

    # the bootstrap targets are offline predictions: the offline backends only
    assert "--backend naive" in text
    assert "--backend seasonal" in text


# ---------------------------------------------------------------------------
# 4. Docker
# ---------------------------------------------------------------------------


def test_dockerfile_contract() -> None:
    text = read(DOCKERFILE)

    assert "python:3.11-slim" in text
    assert re.search(r"^FROM\s+\S+\s+AS\s+base$", text, flags=re.MULTILINE)
    assert re.search(r"^FROM\s+base\s+AS\s+test$", text, flags=re.MULTILINE)
    assert "requirements.txt" in text
    assert "requirements-dev.txt" in text
    assert "pip install --no-cache-dir ." in text
    assert (
        "RUN python -m pytest tests --cov=trading_platform --cov-report=term-missing "
        f"--cov-fail-under={COVERAGE_THRESHOLD}" in text
    )
    assert "PYTHONDONTWRITEBYTECODE=1" in text
    assert "PYTHONUNBUFFERED=1" in text
    assert re.search(r"^USER appuser$", text, flags=re.MULTILINE)
    # no OS package installation: pip is the only network-using step
    assert not re.search(r"^RUN\s+.*(apt-get|apt\s+install|curl|wget)", text, flags=re.MULTILINE)


def test_dockerfile_ends_with_the_cli_entrypoint() -> None:
    lines = [line for line in read(DOCKERFILE).splitlines() if line.strip()]

    assert lines[-1] == 'ENTRYPOINT ["python", "-m", "trading_platform"]'


def test_dockerfile_test_stage_copies_the_suite() -> None:
    text = read(DOCKERFILE)

    assert "COPY src ./src" in text
    assert "COPY tests ./tests" in text
    assert "COPY config ./config" in text


def test_dockerignore_excludes_build_noise() -> None:
    entries = {
        line.strip()
        for line in read(DOCKERIGNORE).splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    for required in (
        ".git",
        ".venv",
        ".uv-cache",
        "data",
        "reports",
        "htmlcov",
        "coverage.xml",
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        ".scratch",
    ):
        assert required in entries, f".dockerignore does not exclude {required!r}"


def test_dockerignore_keeps_what_the_suite_needs() -> None:
    entries = {
        line.strip()
        for line in read(DOCKERIGNORE).splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    # tests/test_docs.py and tests/test_packaging.py read these inside the image
    for kept in ("docs", "Makefile", "Dockerfile", "config", "tests"):
        assert kept not in entries, f".dockerignore excludes {kept!r}, needed by the suite"
