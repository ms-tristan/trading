"""Documentation and CI-workflow contract tests.

Pure file-content assertions: no network, no import of ``trading_backtest``
(see ``docs/testing-policy.md`` -- documentation is part of the delivered
surface and must stay consistent with the frozen interfaces).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"

ARCHITECTURE = DOCS_DIR / "architecture.md"
METHODOLOGY = DOCS_DIR / "backtesting-methodology.md"
USAGE = DOCS_DIR / "usage.md"
TESTING_POLICY = DOCS_DIR / "testing-policy.md"
README = REPO_ROOT / "README.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The FULL pytest command of docs/testing-policy.md section 3, byte for byte.
FULL_PYTEST_COMMAND = (
    "python -m pytest tests --cov=trading_backtest --cov-report=term-missing --cov-report=xml"
)

DOCUMENTED_PAGES = {
    "architecture": ARCHITECTURE,
    "backtesting-methodology": METHODOLOGY,
    "usage": USAGE,
    "testing-policy": TESTING_POLICY,
}

FROZEN_MODULES = (
    "trading_backtest.config",
    "trading_backtest.data",
    "trading_backtest.strategy",
    "trading_backtest.validation",
    "trading_backtest.metrics",
    "trading_backtest.reporting",
    "trading_backtest.cli",
)

CLI_COMMANDS = ("backtest", "walk-forward", "robustness", "monte-carlo")

_MD_LINK = re.compile(r"\]\(([^)\s]+)\)")


def read(path: Path) -> str:
    """Return the UTF-8 content of ``path``."""
    return path.read_text(encoding="utf-8")


def non_empty_lines(text: str) -> list[str]:
    """Return the stripped non-empty lines of ``text``."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def markdown_link_targets(text: str) -> list[str]:
    """Return every relative ``*.md`` link target found in ``text``."""
    targets: list[str] = []
    for raw in _MD_LINK.findall(text):
        target = raw.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.endswith(".md"):
            targets.append(target)
    return targets


# ---------------------------------------------------------------------------
# 1. The four documentation pages exist and are substantial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DOCUMENTED_PAGES))
def test_documentation_page_exists_and_is_substantial(name: str) -> None:
    path = DOCUMENTED_PAGES[name]

    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    lines = non_empty_lines(read(path))
    assert len(lines) >= 40, (
        f"{path.relative_to(REPO_ROOT)} only has {len(lines)} non-empty lines (>= 40 expected)"
    )
    assert len("\n".join(lines)) > 500


def test_readme_is_not_empty() -> None:
    assert len(non_empty_lines(read(README))) >= 10


# ---------------------------------------------------------------------------
# 2. README links every documentation page, and every link resolves on disk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(DOCUMENTED_PAGES))
def test_readme_links_every_documentation_page(name: str) -> None:
    readme = read(README)

    assert f"docs/{name}.md" in readme, f"README.md does not link docs/{name}.md"
    assert f"](docs/{name}.md)" in readme, f"README.md does not render docs/{name}.md as a link"


def test_every_referenced_markdown_path_exists() -> None:
    sources = [README, *DOCUMENTED_PAGES.values()]
    missing: list[str] = []

    for source in sources:
        for target in markdown_link_targets(read(source)):
            if not (source.parent / target).resolve().is_file():
                missing.append(f"{source.relative_to(REPO_ROOT)} -> {target}")

    assert not missing, f"broken documentation links: {missing}"

    # Every documented page must be reachable from the README.
    readme_targets = set(markdown_link_targets(read(README)))
    for name in DOCUMENTED_PAGES:
        assert f"docs/{name}.md" in readme_targets


def test_readme_keeps_the_documentation_section() -> None:
    readme = read(README)

    assert "## Documentation" in readme
    assert "## Démarrage rapide" in readme
    # The quickstart block stays copy-pasteable and offline.
    assert "--data-file" in readme
    assert "pip install -e" in readme


# ---------------------------------------------------------------------------
# 3. The testing policy stays the single source of truth
# ---------------------------------------------------------------------------


def test_testing_policy_pins_the_full_coverage_command() -> None:
    policy = read(TESTING_POLICY)

    assert FULL_PYTEST_COMMAND in policy
    assert FULL_PYTEST_COMMAND in non_empty_lines(policy) or any(
        FULL_PYTEST_COMMAND in line for line in policy.splitlines()
    )


def test_testing_policy_keeps_the_coverage_threshold_and_scope_words() -> None:
    policy = read(TESTING_POLICY)

    assert "85" in policy
    assert "scoped" in policy.lower()
    assert "--cov-fail-under" in policy


def test_usage_delegates_the_testing_policy_instead_of_duplicating_it() -> None:
    usage = read(USAGE)

    assert "testing-policy.md" in usage


# ---------------------------------------------------------------------------
# 4. CI workflow -- textual contract (always runs)
# ---------------------------------------------------------------------------


def test_ci_workflow_exists() -> None:
    assert CI_WORKFLOW.is_file(), ".github/workflows/ci.yml is missing"


def test_ci_workflow_text_contract() -> None:
    text = read(CI_WORKFLOW)

    required = (
        "name: CI",
        "push",
        "pull_request",
        "workflow_dispatch",
        "quality",
        "ubuntu-latest",
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "cache: pip",
        "python -m pip install --upgrade pip",
        'python -m pip install -e ".[dev]"',
        "python -m ruff check .",
        "python -m ruff format --check .",
        "python -m mypy src",
        FULL_PYTEST_COMMAND,
        "actions/upload-artifact@v4",
        "coverage.xml",
        "if: always()",
        "contents: read",
        "cancel-in-progress",
    )
    missing = [token for token in required if token not in text]
    assert not missing, f"ci.yml is missing: {missing}"

    # The coverage gate comes from pyproject.toml only -- never repeated in CI.
    run_lines = [line for line in text.splitlines() if line.strip().startswith("run:")]
    assert not [line for line in run_lines if "--cov-fail-under" in line]
    # Exactly one job, no network-dependent step.
    assert text.count("\n  quality:") == 1
    for forbidden in ("curl ", "wget ", "git clone", "apt-get"):
        assert forbidden not in text


def test_ci_workflow_steps_are_ordered() -> None:
    text = read(CI_WORKFLOW)

    ordered = (
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "pip install --upgrade pip",
        'pip install -e ".[dev]"',
        "ruff check .",
        "ruff format --check .",
        "mypy src",
        FULL_PYTEST_COMMAND,
        "actions/upload-artifact@v4",
    )
    positions = []
    for token in ordered:
        index = text.find(token)
        assert index != -1, f"ci.yml has no step containing {token!r}"
        positions.append(index)
    assert positions == sorted(positions), f"CI steps are out of order: {positions}"


def test_ci_workflow_declares_python_311() -> None:
    text = read(CI_WORKFLOW)

    assert "python-version" in text
    assert '"3.11"' in text or "'3.11'" in text or "3.11" in text


def test_ci_workflow_never_installs_optional_extras() -> None:
    text = read(CI_WORKFLOW)
    install_lines = [line for line in text.splitlines() if "pip install" in line]

    assert install_lines, "ci.yml declares no pip install step"
    for line in install_lines:
        lowered = line.lower()
        assert "freqtrade" not in lowered, f"CI must not install the freqtrade extra: {line!r}"
        assert "ccxt" not in lowered, f"CI must not install the exchange extra: {line!r}"
    assert any('install -e ".[dev]"' in line for line in install_lines)


# ---------------------------------------------------------------------------
# 5. CI workflow -- structural contract (needs PyYAML)
# ---------------------------------------------------------------------------


def _triggers(workflow: dict) -> dict:
    """Return the ``on:`` mapping (PyYAML resolves the bare key ``on`` to True)."""
    for key in ("on", True):
        if key in workflow:
            value = workflow[key]
            return value if isinstance(value, dict) else {}
    return {}


def _step_markers(steps: list) -> list[str]:
    markers: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        parts = [str(step.get(key, "")) for key in ("uses", "name", "run")]
        markers.append(" ".join(parts))
    return markers


def test_ci_workflow_parses_as_yaml_and_matches_the_contract() -> None:
    yaml = pytest.importorskip("yaml", reason="PyYAML is not part of the dev extra")

    workflow = yaml.safe_load(read(CI_WORKFLOW))

    assert isinstance(workflow, dict)
    assert workflow["name"] == "CI"

    triggers = _triggers(workflow)
    assert set(triggers) >= {"push", "pull_request", "workflow_dispatch"}

    assert workflow["permissions"] == {"contents": "read"}
    concurrency = workflow["concurrency"]
    assert "github.ref" in str(concurrency["group"])
    assert concurrency["cancel-in-progress"] is True

    jobs = workflow["jobs"]
    assert list(jobs) == ["quality"]
    job = jobs["quality"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["matrix"]["python-version"] == ["3.11"]

    markers = _step_markers(job["steps"])
    ordered = (
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "pip install --upgrade pip",
        'pip install -e ".[dev]"',
        "ruff check .",
        "ruff format --check .",
        "mypy src",
        FULL_PYTEST_COMMAND,
        "actions/upload-artifact@v4",
    )
    positions = []
    for token in ordered:
        match = next((i for i, marker in enumerate(markers) if token in marker), None)
        assert match is not None, f"ci.yml has no step containing {token!r}"
        positions.append(match)
    assert positions == sorted(positions), f"CI steps are out of order: {positions}"
    assert len(set(positions)) == len(positions), "CI steps are duplicated or merged"

    upload = next(step for step in job["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert upload["with"]["path"] == "coverage.xml"
    assert "always()" in str(upload["if"])

    install_steps = [
        str(step.get("run", ""))
        for step in job["steps"]
        if "pip install" in str(step.get("run", ""))
    ]
    assert install_steps
    for command in install_steps:
        assert "freqtrade" not in command.lower()
        assert "ccxt" not in command.lower()


# ---------------------------------------------------------------------------
# 6. The delivered documentation describes the frozen architecture
# ---------------------------------------------------------------------------


def test_architecture_documents_every_frozen_module() -> None:
    text = read(ARCHITECTURE)

    missing = [module for module in FROZEN_MODULES if module not in text]
    assert not missing, f"architecture.md does not mention: {missing}"


def test_architecture_documents_the_ohlcv_contract() -> None:
    text = read(ARCHITECTURE)

    assert "timestamp" in text
    assert "UTC" in text
    for column in ("open", "high", "low", "close", "volume"):
        assert column in text
    assert "NaN" in text


def test_architecture_documents_the_seams_and_models() -> None:
    text = read(ARCHITECTURE)

    for token in ("RunnerFn", "to_dict", "TradeRecord", "BacktestResult", "MetricSet"):
        assert token in text, f"architecture.md does not mention {token}"


def test_methodology_documents_the_validation_protocol() -> None:
    text = read(METHODOLOGY).lower()

    for token in ("walk-forward", "in-sample", "out-of-sample", "monte carlo", "var"):
        assert token in text, f"backtesting-methodology.md does not mention {token!r}"
    assert "60 %" in text
    assert "purge" in text


def test_usage_documents_every_cli_command() -> None:
    text = read(USAGE)

    for command in CLI_COMMANDS:
        assert command in text, f"usage.md does not mention the {command!r} command"
    assert "--data-file" in text
    assert "backtest_default.json" in text


def test_usage_documents_tooling_and_freqtrade_configs() -> None:
    text = read(USAGE)

    for token in ("docker build", "docker-test", "freqtrade_dryrun.json", "AppConfig"):
        assert token in text, f"usage.md does not mention {token!r}"
    assert "| Cible |" in text


# ---------------------------------------------------------------------------
# 7. The Freqtrade adapter is documented (write-once / expose-twice recipe)
# ---------------------------------------------------------------------------


def test_architecture_documents_the_freqtrade_adapter() -> None:
    text = read(ARCHITECTURE)

    for token in ("freqtrade_adapter", "make_freqtrade_strategy"):
        assert token in text, f"architecture.md does not mention {token!r}"


def test_architecture_documents_the_entry_long_rename() -> None:
    """The ``entry_long`` -> ``enter_long`` rename is the adapter's trap #1."""
    text = read(ARCHITECTURE)

    house = [match.start() for match in re.finditer("entry_long", text)]
    freqtrade = [match.start() for match in re.finditer("enter_long", text)]
    assert house, "architecture.md does not mention the house column 'entry_long'"
    assert freqtrade, "architecture.md does not mention the Freqtrade column 'enter_long'"

    # Both names must be documented *together* (as a translation), not merely listed
    # in two unrelated places.
    assert any(abs(left - right) <= 200 for left in house for right in freqtrade), (
        "architecture.md must document the entry_long -> enter_long rename"
    )


def test_architecture_documents_the_stoploss_mapping_and_the_shim() -> None:
    text = read(ARCHITECTURE)

    # The frozen symbol of the absolute-stop -> ratio translation.
    assert "stoploss_ratio_from_absolute" in text, (
        "architecture.md must document the frozen stoploss translation symbol"
    )
    # ... and the upstream Freqtrade helper it delegates to (verbatim token).
    assert "stoploss_from_absolute" in text, (
        "architecture.md must spell out the 'stoploss from absolute' translation"
    )
    assert "user_data/strategies/BasicStrategy.py" in text, (
        "architecture.md must document the shipped Freqtrade shim"
    )


def test_architecture_documents_the_execution_model_gap() -> None:
    text = read(ARCHITECTURE)

    # The shared convention, spelled out in both wordings: the frozen English
    # docstring of strategy/engine.py and the French side-by-side table.
    assert "close of candle" in text
    assert "open of candle" in text
    assert "clôture de `t`" in text
    assert "ouverture de `t+1`" in text

    # The conclusion must be written literally: the two backtests diverge.
    lowered = text.lower()
    for token in ("pas comparables", "pas les mêmes chiffres", "n'est pas un bug"):
        assert token in lowered, f"architecture.md does not state {token!r}"


def test_usage_documents_the_freqtrade_exposure_section() -> None:
    text = read(USAGE)

    # The optional extra, the naming convention, the shim and the escape hatch.
    assert 'pip install -e ".[freqtrade]"' in text
    assert "BasicStrategy" in text
    assert "BasicFreqtradeStrategy" in text
    assert "make_freqtrade_strategy" in text
    assert "--strategy-path" in text
    assert "user_data/strategies/BasicStrategy.py" in text
    assert "dry-run" in text
    # Paper and live commands are both spelled out.
    assert "freqtrade trade" in text
    assert "freqtrade_dryrun.json" in text
    assert "freqtrade_config.json" in text
