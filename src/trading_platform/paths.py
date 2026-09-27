"""Filesystem layout of the trading platform.

One module owns every path the platform reads or writes, so that a host
checkout, an editable install and the realtime container resolve the same
locations. The precedence is always:

1. an explicit argument (when the function accepts one);
2. the documented environment variable;
3. the container directory, when it exists;
4. the repository-relative default.

The module-level constants (:data:`CONFIG_DIR`, :data:`DATA_DIR`, ...) are
resolved once at import time against the ambient environment. Call the
``resolve_*`` functions when a caller needs a value computed from an explicit
environment mapping.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "CONFIG_DIR",
    "CONTAINER_APP_DIR",
    "DATA_DIR",
    "DEFAULT_STATE_DB",
    "ENV_CONFIG_DIR",
    "ENV_STATE_DB",
    "ENV_STRATEGIES_DIR",
    "REPO_ROOT",
    "STRATEGIES_DIR",
    "USER_DATA_DIR",
    "resolve_config_dir",
    "resolve_state_db_path",
    "resolve_strategies_dir",
    "state_dir_for",
]

#: Root of the checkout (or of the installed application inside the container).
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: Root of the realtime container image.
CONTAINER_APP_DIR = Path("/app")

ENV_CONFIG_DIR = "TB_CONFIG_DIR"
ENV_STRATEGIES_DIR = "TB_STRATEGIES_DIR"
ENV_STATE_DB = "TB_REALTIME_STATE_DB"

CONTAINER_CONFIG_DIR = CONTAINER_APP_DIR / "config"
CONTAINER_STRATEGIES_DIR = CONTAINER_APP_DIR / "user_data" / "strategies"


def _environment(env: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return the environment mapping to read from (``os.environ`` by default)."""
    return os.environ if env is None else env


def _env_path(env: Mapping[str, str] | None, name: str) -> Path | None:
    """Return the path stored in ``name``, or ``None`` when unset or blank."""
    raw = _environment(env).get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return Path(text).expanduser()


def resolve_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the directory holding the platform JSON documents."""
    from_env = _env_path(env, ENV_CONFIG_DIR)
    if from_env is not None:
        return from_env
    if CONTAINER_CONFIG_DIR.is_dir():
        return CONTAINER_CONFIG_DIR
    return REPO_ROOT / "config"


def resolve_strategies_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the directory holding the Freqtrade strategy files."""
    from_env = _env_path(env, ENV_STRATEGIES_DIR)
    if from_env is not None:
        return from_env
    if CONTAINER_STRATEGIES_DIR.is_dir():
        return CONTAINER_STRATEGIES_DIR
    return REPO_ROOT / "user_data" / "strategies"


def resolve_state_db_path(
    explicit: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the SQLite file holding the realtime state."""
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = _env_path(env, ENV_STATE_DB)
    if from_env is not None:
        return from_env
    return REPO_ROOT / "data" / "realtime" / "state.db"


def state_dir_for(state_db: str | os.PathLike[str]) -> Path:
    """Return the directory that contains ``state_db`` (created by the caller)."""
    return Path(state_db).expanduser().resolve().parent


#: Resolved once, against the ambient environment.
CONFIG_DIR: Path = resolve_config_dir()
USER_DATA_DIR: Path = resolve_strategies_dir().parent
STRATEGIES_DIR: Path = resolve_strategies_dir()
DATA_DIR: Path = resolve_state_db_path().parent
DEFAULT_STATE_DB: Path = resolve_state_db_path()
