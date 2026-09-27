"""Tests for the filesystem layout resolution."""

from __future__ import annotations

from pathlib import Path

from trading_platform import paths


def test_repo_root_points_at_the_checkout(repo_root: Path) -> None:
    assert repo_root == paths.REPO_ROOT
    assert (paths.REPO_ROOT / "pyproject.toml").is_file()


def test_container_root_constant() -> None:
    assert Path("/app") == paths.CONTAINER_APP_DIR


def test_environment_variable_names() -> None:
    assert paths.ENV_CONFIG_DIR == "TB_CONFIG_DIR"
    assert paths.ENV_STRATEGIES_DIR == "TB_STRATEGIES_DIR"
    assert paths.ENV_STATE_DB == "TB_REALTIME_STATE_DB"


def test_module_constants_match_the_resolvers_without_overrides() -> None:
    assert paths.resolve_config_dir({}) == paths.CONFIG_DIR
    assert paths.resolve_strategies_dir({}) == paths.STRATEGIES_DIR
    assert paths.STRATEGIES_DIR.parent == paths.USER_DATA_DIR
    assert paths.resolve_state_db_path(env={}) == paths.DEFAULT_STATE_DB
    assert paths.DEFAULT_STATE_DB.parent == paths.DATA_DIR


def test_config_dir_falls_back_to_the_container_then_the_checkout() -> None:
    container_config = paths.CONTAINER_APP_DIR / "config"
    if container_config.is_dir():
        assert paths.resolve_config_dir({}) == container_config
    else:
        assert paths.resolve_config_dir({}) == paths.REPO_ROOT / "config"


def test_config_dir_prefers_an_existing_container_directory(monkeypatch, tmp_path: Path) -> None:
    container = tmp_path / "app" / "config"
    container.mkdir(parents=True)
    monkeypatch.setattr(paths, "CONTAINER_CONFIG_DIR", container)
    assert paths.resolve_config_dir({}) == container
    override = tmp_path / "override"
    assert paths.resolve_config_dir({paths.ENV_CONFIG_DIR: str(override)}) == override


def test_strategies_dir_prefers_an_existing_container_directory(
    monkeypatch, tmp_path: Path
) -> None:
    container = tmp_path / "app" / "user_data" / "strategies"
    container.mkdir(parents=True)
    monkeypatch.setattr(paths, "CONTAINER_STRATEGIES_DIR", container)
    assert paths.resolve_strategies_dir({}) == container
    override = tmp_path / "override"
    assert paths.resolve_strategies_dir({paths.ENV_STRATEGIES_DIR: str(override)}) == override


def test_strategies_dir_falls_back_to_the_container_then_the_checkout() -> None:
    container_strategies = paths.CONTAINER_APP_DIR / "user_data" / "strategies"
    if container_strategies.is_dir():
        assert paths.resolve_strategies_dir({}) == container_strategies
    else:
        assert paths.resolve_strategies_dir({}) == (paths.REPO_ROOT / "user_data" / "strategies")


def test_resolve_config_dir_prefers_the_environment(tmp_path: Path) -> None:
    env = {paths.ENV_CONFIG_DIR: str(tmp_path)}
    assert paths.resolve_config_dir(env) == tmp_path


def test_resolve_config_dir_ignores_blank_values() -> None:
    env = {paths.ENV_CONFIG_DIR: "   "}
    assert paths.resolve_config_dir(env) == paths.resolve_config_dir({})


def test_resolve_config_dir_expands_the_home_directory() -> None:
    env = {paths.ENV_CONFIG_DIR: "~/trading-config"}
    assert paths.resolve_config_dir(env) == Path("~/trading-config").expanduser()


def test_resolve_strategies_dir_prefers_the_environment(tmp_path: Path) -> None:
    env = {paths.ENV_STRATEGIES_DIR: str(tmp_path / "strategies")}
    assert paths.resolve_strategies_dir(env) == tmp_path / "strategies"


def test_resolve_strategies_dir_ignores_blank_values() -> None:
    env = {paths.ENV_STRATEGIES_DIR: ""}
    assert paths.resolve_strategies_dir(env) == paths.resolve_strategies_dir({})


def test_resolve_state_db_path_prefers_the_explicit_argument(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.db"
    env = {paths.ENV_STATE_DB: str(tmp_path / "from-env.db")}
    assert paths.resolve_state_db_path(explicit, env) == explicit


def test_resolve_state_db_path_prefers_the_environment(tmp_path: Path) -> None:
    env = {paths.ENV_STATE_DB: str(tmp_path / "from-env.db")}
    assert paths.resolve_state_db_path(None, env) == tmp_path / "from-env.db"


def test_resolve_state_db_path_falls_back_to_the_checkout() -> None:
    assert paths.resolve_state_db_path(env={}) == (
        paths.REPO_ROOT / "data" / "realtime" / "state.db"
    )


def test_resolve_state_db_path_ignores_blank_values() -> None:
    assert paths.resolve_state_db_path(
        env={paths.ENV_STATE_DB: "  "}
    ) == paths.resolve_state_db_path(env={})


def test_resolvers_read_the_process_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(paths.ENV_CONFIG_DIR, str(tmp_path / "config"))
    monkeypatch.setenv(paths.ENV_STRATEGIES_DIR, str(tmp_path / "strategies"))
    monkeypatch.setenv(paths.ENV_STATE_DB, str(tmp_path / "state.db"))
    assert paths.resolve_config_dir() == tmp_path / "config"
    assert paths.resolve_strategies_dir() == tmp_path / "strategies"
    assert paths.resolve_state_db_path() == tmp_path / "state.db"


def test_state_dir_for_returns_the_resolved_parent(tmp_state_dir: Path) -> None:
    state_db = tmp_state_dir / "state.db"
    assert paths.state_dir_for(state_db) == tmp_state_dir.resolve()
    assert paths.state_dir_for(str(state_db)) == tmp_state_dir.resolve()
