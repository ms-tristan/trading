"""Contract test: the coverage policy and the files that mechanically enforce it.

``docs/testing-policy.md`` is the only description of the gates, and the gates
themselves live in ``pyproject.toml`` (Python, coverage >= 80 %) and in
``dashboard/vitest.config.ts`` (dashboard, 70/70/70 and branches 60). This module
pins all three read-only: it parses TOML and text, imports nothing from the
package and runs no command of the gate itself.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

POLICY_DOCUMENT = "docs/testing-policy.md"
PYTHON_GATE_FILE = "pyproject.toml"
DASHBOARD_GATE_FILE = "dashboard/vitest.config.ts"

PYTHON_COVERAGE_THRESHOLD = 80
DASHBOARD_THRESHOLDS = {"lines": 70, "statements": 70, "functions": 70, "branches": 60}

#: The files that could silently take the Python gate away from pyproject.toml.
ALTERNATIVE_PYTHON_GATE_FILES = ("pytest.ini", "setup.cfg", "tox.ini")


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _normalised(relative_path: str) -> str:
    """The document with every run of whitespace collapsed, for prose assertions."""
    return " ".join(_read(relative_path).split())


def _pyproject() -> dict:
    return tomllib.loads(_read(PYTHON_GATE_FILE))


def test_policy_document_documents_the_python_gate() -> None:
    document = _normalised(POLICY_DOCUMENT)
    assert ".venv/bin/python -m pytest" in document, (
        "docs/testing-policy.md must document the full Python gate command"
    )
    assert "--cov-fail-under=80" in document, (
        "docs/testing-policy.md must document the coverage threshold flag"
    )
    assert f"{PYTHON_COVERAGE_THRESHOLD} %" in document, (
        "docs/testing-policy.md must state the 80 % coverage threshold of the Python gate"
    )


def test_policy_document_documents_the_scoped_no_cov_run() -> None:
    document = _normalised(POLICY_DOCUMENT)
    assert "--no-cov" in document, (
        "docs/testing-policy.md must document the scoped --no-cov run of a work package"
    )
    assert "tests/<area> -q --no-cov" in document, (
        "docs/testing-policy.md must document the scoped form `.venv/bin/python -m pytest "
        "tests/<area> -q --no-cov`"
    )


def test_policy_document_names_the_files_that_enforce_the_gates() -> None:
    document = _normalised(POLICY_DOCUMENT)
    assert PYTHON_GATE_FILE in document, (
        f"docs/testing-policy.md must name {PYTHON_GATE_FILE} as the Python gate file"
    )
    assert DASHBOARD_GATE_FILE in document, (
        f"docs/testing-policy.md must name {DASHBOARD_GATE_FILE} as the dashboard gate file"
    )
    assert "[tool.pytest.ini_options]" in document and "addopts" in document, (
        "docs/testing-policy.md must point at the addopts entry that carries the coverage gate"
    )


def test_pyproject_addopts_carries_the_three_coverage_flags() -> None:
    addopts = _pyproject()["tool"]["pytest"]["ini_options"]["addopts"]
    tokens = addopts.split()

    for flag in ("--cov=trading_platform", "--cov-report=term-missing", "--cov-fail-under=80"):
        assert flag in tokens, f"{PYTHON_GATE_FILE} addopts must carry {flag}, found {addopts!r}"

    coverage_sources = _pyproject()["tool"]["coverage"]["run"]["source"]
    assert coverage_sources == ["trading_platform"], (
        f"[tool.coverage.run] must measure trading_platform only, found {coverage_sources}"
    )


def test_policy_document_quotes_the_addopts_pyproject_enforces() -> None:
    quoted = re.search(r'addopts\s*=\s*"([^"]*)"', _read(POLICY_DOCUMENT))
    assert quoted is not None, "docs/testing-policy.md must quote the addopts line it describes"

    enforced = _pyproject()["tool"]["pytest"]["ini_options"]["addopts"]
    assert quoted.group(1).split() == enforced.split(), (
        "docs/testing-policy.md quotes an addopts line that pyproject.toml does not enforce: "
        f"{quoted.group(1)!r} != {enforced!r}"
    )


def test_pyproject_is_the_only_python_gate_file() -> None:
    present = [name for name in ALTERNATIVE_PYTHON_GATE_FILES if (REPO_ROOT / name).exists()]
    assert not present, (
        f"the Python gate lives in {PYTHON_GATE_FILE}; remove the competing gate file(s) {present}"
    )


def test_dashboard_gate_declares_the_coverage_thresholds() -> None:
    document = _read(DASHBOARD_GATE_FILE)
    block = re.search(r"thresholds:\s*\{(?P<body>[^}]*)\}", document, re.DOTALL)
    assert block is not None, f"{DASHBOARD_GATE_FILE} must declare a coverage thresholds block"

    for key, value in DASHBOARD_THRESHOLDS.items():
        assert re.search(rf"\b{key}:\s*{value}\b", block.group("body")), (
            f"{DASHBOARD_GATE_FILE} must declare the {key} threshold at {value}"
        )


def test_policy_document_and_dashboard_gate_agree_on_the_thresholds() -> None:
    document = _normalised(POLICY_DOCUMENT)
    for key, value in DASHBOARD_THRESHOLDS.items():
        assert f"{key} >= {value} %" in document, (
            f"docs/testing-policy.md must document the {key} threshold as `{key} >= {value} %`"
        )


def test_policy_document_names_the_dashboard_coverage_command() -> None:
    document = _normalised(POLICY_DOCUMENT)
    assert "npm run test:coverage" in document, (
        "docs/testing-policy.md must document the dashboard coverage command"
    )
