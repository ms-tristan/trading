"""Contract test: the checkout really assembles the specified repository layout.

This module is a read-only tripwire. It imports nothing from ``trading_platform``,
nothing from ``freqtrade`` and nothing from the dashboard, it spawns no process
and it touches no network: every assertion below reads paths on disk. That keeps
the suite fast and makes it impossible for a syntax error in another work package
to break it.

Scope: the specification layout, the ``user_data/`` whitelist and the shape of
``config/``. Content of the configuration documents is pinned by
``test_config_documents.py``, the strategies by ``test_strategy_files.py``, the
frozen skeleton by ``test_deploy_skeleton.py`` and the coverage policy by
``test_frozen_policy.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The ten freqtrade strategy modules shipped by the platform.
STRATEGY_FILES: tuple[str, ...] = (
    "BasicStrategy.py",
    "BollingerStrategy.py",
    "DonchianStrategy.py",
    "DualThrustStrategy.py",
    "FaberStrategy.py",
    "KeltnerStrategy.py",
    "MacdStrategy.py",
    "MomentumStrategy.py",
    "RsiReversionStrategy.py",
    "SupertrendStrategy.py",
)

#: The three declarative documents of ``config/``; nothing else may live there.
CONFIG_DOCUMENTS: tuple[str, ...] = (
    "platform.json",
    "strategies.json",
    "profiles.json",
)

#: Every file the specification layout requires, as a repository-relative path.
SPECIFICATION_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Makefile",
    "README.md",
    ".dockerignore",
    ".gitignore",
    ".github/workflows/ci.yml",
    ".github/workflows/deploy.yml",
    "deploy/Dockerfile.realtime",
    "deploy/Dockerfile.dashboard",
    "deploy/docker-compose.yml",
    "deploy/README.md",
    "config/platform.json",
    "config/strategies.json",
    "config/profiles.json",
    "src/trading_platform/__init__.py",
    "src/trading_platform/__main__.py",
    "src/trading_platform/config.py",
    "src/trading_platform/models.py",
    "src/trading_platform/metrics.py",
    "src/trading_platform/logging_setup.py",
    "src/trading_platform/paths.py",
    "src/trading_platform/profiles/__init__.py",
    "src/trading_platform/profiles/catalogue.py",
    "src/trading_platform/profiles/store.py",
    "src/trading_platform/engine/__init__.py",
    "src/trading_platform/engine/config_builder.py",
    "src/trading_platform/engine/client.py",
    "src/trading_platform/engine/supervisor.py",
    "src/trading_platform/engine/poller.py",
    "src/trading_platform/api/__init__.py",
    "src/trading_platform/api/app.py",
    "src/trading_platform/api/routes.py",
    "src/trading_platform/api/schemas.py",
    "src/trading_platform/api/security.py",
    "user_data/README.md",
    "docs/testing-policy.md",
    "docs/architecture.md",
    "docs/strategies.md",
    "docs/operations.md",
    "dashboard/package.json",
    "dashboard/package-lock.json",
    "dashboard/next.config.ts",
    "dashboard/tsconfig.json",
    "dashboard/next-env.d.ts",
    "dashboard/eslint.config.mjs",
    "dashboard/postcss.config.mjs",
    "dashboard/vitest.config.ts",
) + tuple(f"user_data/strategies/{name}" for name in STRATEGY_FILES)

#: Every directory the specification layout requires.
SPECIFICATION_DIRECTORIES: tuple[str, ...] = (
    "tests",
    "dashboard/src",
)

#: Build artifacts that any developer machine may hold without violating the
#: layout: a ``__pycache__`` tree, a compiled module or a Finder file. They are
#: ignored by the whitelist checks so the suite does not fail on local detritus.
BUILD_ARTIFACT_NAMES = frozenset({"__pycache__", ".DS_Store"})


def _is_build_artifact(part: str) -> bool:
    return part in BUILD_ARTIFACT_NAMES or part.endswith((".pyc", ".pyo"))


def _entries_named(directory: Path, name: str) -> list[Path]:
    """Every entry of ``directory`` whose name matches ``name``, case-insensitively."""
    if not directory.is_dir():
        return []
    return sorted(entry for entry in directory.iterdir() if entry.name.lower() == name.lower())


def _tree_entries(root: Path) -> set[str]:
    """Repository-relative paths under ``root``, build artifacts excluded."""
    entries: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(_is_build_artifact(part) for part in relative.parts):
            continue
        entries.add(relative.as_posix())
    return entries


def test_specification_layout_has_no_duplicate_entries() -> None:
    """The frozen layout itself must list every path exactly once."""
    listed = list(SPECIFICATION_FILES) + list(SPECIFICATION_DIRECTORIES)
    duplicates = sorted({path for path in listed if listed.count(path) > 1})
    assert not duplicates, f"the specification layout lists these paths twice: {duplicates}"


@pytest.mark.parametrize("relative_path", SPECIFICATION_FILES)
def test_specification_path_exists_exactly_once(relative_path: str) -> None:
    path = REPO_ROOT / relative_path
    matches = _entries_named(path.parent, path.name)
    assert matches, f"missing from the specification layout: {relative_path}"
    assert [entry.name for entry in matches] == [path.name], (
        f"{relative_path} exists more than once (or with a different spelling) in "
        f"{path.parent}: {[entry.name for entry in matches]}"
    )


@pytest.mark.parametrize("relative_path", SPECIFICATION_FILES)
def test_specification_path_is_a_regular_file(relative_path: str) -> None:
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is not a regular file"


@pytest.mark.parametrize("relative_path", SPECIFICATION_DIRECTORIES)
def test_specification_directory_exists(relative_path: str) -> None:
    path = REPO_ROOT / relative_path
    assert path.is_dir(), f"{relative_path} is not a directory"


def test_user_data_holds_only_the_readme_and_the_ten_strategies() -> None:
    expected = {"README.md", "strategies"}
    expected |= {f"strategies/{name}" for name in STRATEGY_FILES}

    found = _tree_entries(REPO_ROOT / "user_data")

    assert found == expected, (
        "user_data/ must contain exactly README.md and the ten specified strategy "
        f"files; unexpected: {sorted(found - expected)}, missing: {sorted(expected - found)}"
    )


def test_user_data_readme_is_the_only_document_at_its_root() -> None:
    found = {path.name for path in (REPO_ROOT / "user_data").iterdir()}
    assert found == {"README.md", "strategies"}, (
        f"unexpected entries under user_data/: {sorted(found)}"
    )


def test_config_holds_only_the_three_json_documents() -> None:
    entries = sorted((REPO_ROOT / "config").iterdir())
    names = [entry.name for entry in entries]

    for entry in entries:
        assert entry.is_file(), f"config/{entry.name} is not a regular file"
        assert entry.suffix == ".json", (
            f"config/{entry.name} is not a *.json document; every config document must be a "
            "JSON file directly in config/"
        )

    assert names == sorted(CONFIG_DOCUMENTS), (
        f"config/ must hold exactly {sorted(CONFIG_DOCUMENTS)}; found {names}"
    )


def test_dashboard_scaffold_is_complete() -> None:
    scaffold = {
        "package.json",
        "package-lock.json",
        "next.config.ts",
        "tsconfig.json",
        "next-env.d.ts",
        "eslint.config.mjs",
        "postcss.config.mjs",
        "vitest.config.ts",
    }
    dashboard = REPO_ROOT / "dashboard"
    missing = sorted(name for name in scaffold if not (dashboard / name).is_file())
    assert not missing, f"missing from the dashboard scaffold: {missing}"

    app = dashboard / "src"
    assert app.is_dir(), "dashboard/src/ is not a directory"
    assert any(app.rglob("*.tsx")) or any(app.rglob("*.ts")), "dashboard/src/ holds no source file"


def test_test_suite_areas_exist() -> None:
    tests = REPO_ROOT / "tests"
    assert tests.is_dir(), "tests/ is not a directory"
    assert (tests / "conftest.py").is_file(), "tests/conftest.py is missing"
    assert any(tests.rglob("test_*.py")), "tests/ holds no test module"
