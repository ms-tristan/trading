"""Configuration loading, merging, dumping and parameter overriding.

The loader accepts a JSON file (only), an optional mapping of overrides (nested
dicts and/or dotted keys) and validates the result through
:class:`~trading_platform.config.models.AppConfig`.  Every failure is normalised
to :class:`~trading_platform.core.errors.ConfigError`.

The realtime platform has **no configuration document any more**.  Its profiles
and its engine settings live in the SQLite state store, which is their single
source of truth (see :mod:`trading_platform.realtime.settings`): a JSON document
rebuilt from git on every deployment silently destroyed every change an operator
made through the dashboard, so the document was removed rather than patched.

What remains here is the **bootstrap surface**, and it is deliberately tiny: the
path of the state database cannot live inside the database it locates, so
:func:`load_bootstrap_realtime_config` resolves the two storage keys that must be
known *before* the store can be opened -- ``state_db`` and ``logs_dir`` -- from
the environment variables ``TB_REALTIME_STATE_DB``/``TB_REALTIME_LOGS_DIR`` and
from explicit keyword arguments.  Every other field of the returned
:class:`~trading_platform.config.models.RealtimeConfig` is a placeholder that the
store overwrites at boot.

:func:`load_realtime_config` and :func:`load_monitoring_config` are kept for call
compatibility and now return the built-in defaults plus optional overrides: they
never read a document, whatever ``path`` they are handed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from trading_platform.config.models import (
    AppConfig,
    MonitoringConfig,
    RealtimeConfig,
)
from trading_platform.core.errors import ConfigError

__all__ = [
    "default_config",
    "default_monitoring_config",
    "default_realtime_config",
    "dump_config",
    "load_bootstrap_realtime_config",
    "load_config",
    "load_monitoring_config",
    "load_realtime_config",
    "override_params",
]

_JSON_SUFFIXES = (".json",)

_SectionModel = TypeVar("_SectionModel", bound=BaseModel)

#: Environment variable carrying the bootstrap state-database path.
STATE_DB_ENV = "TB_REALTIME_STATE_DB"

#: Environment variable carrying the bootstrap log-directory path.
LOGS_DIR_ENV = "TB_REALTIME_LOGS_DIR"

#: The storage keys that must be known *before* the state store can be opened.
#:
#: They are the only fields of :class:`RealtimeConfig` that keep a
#: file/environment surface: the database path cannot be stored in the database
#: it locates.  Everything else is seeded into SQLite on first initialisation and
#: read back from SQLite on every later boot.
BOOTSTRAP_FIELDS: tuple[str, ...] = ("state_db", "logs_dir")


def default_config() -> AppConfig:
    """Return the built-in configuration (no file, environment still applies)."""
    return AppConfig()


def default_realtime_config() -> RealtimeConfig:
    """Return the built-in realtime engine configuration."""
    return RealtimeConfig()


def default_monitoring_config() -> MonitoringConfig:
    """Return the built-in monitoring server configuration."""
    return MonitoringConfig()


def _read_payload(path: Path) -> dict[str, Any]:
    """Read and JSON-decode ``path``, raising :class:`ConfigError` on any problem."""
    if path.suffix.lower() not in _JSON_SUFFIXES:
        raise ConfigError(
            f"unsupported configuration file format: {path.suffix or '<none>'!r} (expected .json)"
        )
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    if not path.is_file():
        raise ConfigError(f"configuration path is not a file: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unreadable file / permissions
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in configuration file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(
            f"configuration file {path} must contain a JSON object, got {type(payload).__name__}"
        )
    return payload


def _deep_merge(base: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``incoming`` over ``base`` (``incoming`` wins)."""
    merged: dict[str, Any] = dict(base)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _set_dotted(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    """Assign ``value`` at ``dotted_key`` (e.g. ``"data.timeframe"``) inside ``target``."""
    parts = [part for part in dotted_key.split(".") if part]
    if not parts:
        raise ConfigError(f"invalid override key: {dotted_key!r}")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _apply_overrides(payload: dict[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Merge nested overrides first, then dotted keys (dotted keys always win)."""
    nested: dict[str, Any] = {}
    dotted: dict[str, Any] = {}
    for key, value in overrides.items():
        if "." in key:
            dotted[key] = value
        else:
            nested[key] = value
    merged = _deep_merge(payload, nested)
    for key, value in dotted.items():
        _set_dotted(merged, key, value)
    return merged


def _format_validation_error(exc: ValidationError) -> str:
    """Render a pydantic error with explicit, dot-separated field paths."""
    details = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        details.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "; ".join(details)


def load_config(
    path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> AppConfig:
    """Build an :class:`AppConfig` from an optional JSON file and overrides.

    Parameters
    ----------
    path:
        JSON configuration file. ``None`` means "use the defaults only" so that
        ``load_config()`` is equivalent to :func:`default_config`.
    overrides:
        Deep-merged over the file payload.  Both nested mappings
        (``{"data": {"timeframe": "1h"}}``) and dotted keys
        (``{"data.timeframe": "1h"}``) are accepted; dotted keys win.

    Raises
    ------
    ConfigError
        Wrong file extension, missing file, invalid JSON, unknown key or any
        pydantic validation failure.  The message always contains the offending
        field path.
    """
    payload: dict[str, Any] = {}
    if path is not None:
        payload = _read_payload(Path(path))
    if overrides is not None:
        payload = _apply_overrides(payload, overrides)
    try:
        return AppConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {_format_validation_error(exc)}") from exc
    except TypeError as exc:  # pragma: no cover - defensive: pydantic raises ValidationError
        raise ConfigError(f"invalid configuration: {exc}") from exc


def dump_config(cfg: AppConfig, path: str | Path) -> Path:
    """Write ``cfg`` as pretty JSON and return the written path.

    The document is produced with ``model_dump(mode="json")``, so it can be fed
    back to :func:`load_config` to obtain an equal configuration.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = cfg.model_dump(mode="json")
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unwritable destination
        raise ConfigError(f"cannot write configuration file {target}: {exc}") from exc
    return target


def override_params(cfg: AppConfig, params: Mapping[str, Any]) -> AppConfig:
    """Return a deep copy of ``cfg`` whose ``strategy.params`` are updated with ``params``.

    The input configuration is never mutated.
    """
    updated = cfg.model_copy(deep=True)
    merged = dict(updated.strategy.params)
    merged.update(params)
    updated.strategy.params = merged
    return updated


# ---------------------------------------------------------------------------
# realtime: the bootstrap surface, and the store as the source of truth
# ---------------------------------------------------------------------------


def _bootstrap_value(
    name: str,
    explicit: str | Path | bool | None,
    environ: Mapping[str, str],
    env_name: str,
) -> str | Path | bool | None:
    """Resolve one bootstrap key, applying "argument > environment > default".

    ``None`` is the explicit "not set" marker, so a caller that passes nothing
    never overrides the environment, and the model default applies last.  A
    boolean is passed through untouched: ``allow_network=False`` is a decision,
    not a missing value.
    """
    if explicit is None:
        raw = environ.get(env_name)
        return None if raw is None else raw
    return explicit


def _python_literal(raw: str) -> Any:
    """Return ``raw`` as a boolean when it spells one, else the string itself.

    ``TB_REALTIME_*`` variables are read through the same ``bool`` fields as the
    CLI options, so ``TB_REALTIME_ALLOW_NETWORK=false`` has to mean ``False``
    rather than the truthy string ``"false"``.  Anything else stays a string and
    is validated by the model (a path, a number).
    """
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return raw


def load_bootstrap_realtime_config(
    *,
    state_db: str | Path | None = None,
    logs_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
    csv_dir: str | Path | None = None,
    allow_network: bool | None = None,
    environ: Mapping[str, str] | None = None,
) -> RealtimeConfig:
    """Resolve the **bootstrap** realtime configuration, before any store exists.

    Precedence is *explicit argument > environment variable > model default*::

        state_db  <- argument, else TB_REALTIME_STATE_DB,  else data/realtime/state.db
        logs_dir  <- argument, else TB_REALTIME_LOGS_DIR,  else data/realtime/logs

    The returned object is a **bootstrap**, not the platform configuration.  Only
    :data:`BOOTSTRAP_FIELDS` (``state_db`` and ``logs_dir``) are authoritative:
    the path of the database cannot live inside the database it locates, so those
    two keys are resolved here and inherited verbatim on every later read.  Every
    other field carried by the returned object is a *placeholder* that the SQLite
    settings overwrite at boot -- the store is the single source of truth for the
    engine settings, exactly as it already is for the profiles.

    Parameters
    ----------
    state_db:
        Path of the SQLite state database.  The historical ``--profiles`` option
        of the realtime commands is an alias of the option that lands here.
    logs_dir:
        Directory of the durable JSON logs.
    data_dir:
        Offline data directory, when the caller wants it bootstrapped rather than
        stored.
    csv_dir:
        Offline candle directory polled instead of the venue.
    allow_network:
        ``False`` refuses every network call of the market-stream factory.
    environ:
        Environment mapping to read; defaults to the process environment.

    Returns
    -------
    RealtimeConfig
        Validated by the configuration model itself, so the same bounds,
        literals and ``extra="forbid"`` rules apply as everywhere else.

    Raises
    ------
    ConfigError
        The resolved payload does not validate against
        :class:`~trading_platform.config.models.RealtimeConfig`.
    """
    import os

    source = os.environ if environ is None else environ
    payload: dict[str, Any] = {}
    candidates: tuple[tuple[str, str | Path | bool | None, str], ...] = (
        ("state_db", state_db, STATE_DB_ENV),
        ("logs_dir", logs_dir, LOGS_DIR_ENV),
        ("data_dir", data_dir, "TB_REALTIME_DATA_DIR"),
        ("csv_dir", csv_dir, "TB_REALTIME_CSV_DIR"),
        ("allow_network", allow_network, "TB_REALTIME_ALLOW_NETWORK"),
    )
    for name, explicit, env_name in candidates:
        value = _bootstrap_value(name, explicit, source, env_name)
        if value is None:
            continue
        payload[name] = _python_literal(value) if isinstance(value, str) else value
    try:
        return RealtimeConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(
            f"invalid realtime bootstrap configuration: {_format_validation_error(exc)}"
        ) from exc


def _load_defaults(
    model: type[_SectionModel],
    key: str,
    overrides: Mapping[str, Any] | None,
) -> _SectionModel:
    """Build one section over its own built-in defaults (never reads a document)."""
    merged = model().model_dump()
    if overrides is not None:
        merged = _apply_overrides(merged, _strip_section_prefix(overrides, key))
    try:
        return model.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid {key} configuration: {_format_validation_error(exc)}") from exc


def _strip_section_prefix(overrides: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Drop the ``"<section>."`` prefix of dotted override keys.

    ``load_realtime_config({"realtime.poll_interval_seconds": 1})`` and
    ``load_realtime_config({"poll_interval_seconds": 1})`` therefore mean the
    same thing.  A key that does not start with the section name is left
    untouched, so an override targeting another section is still rejected loudly
    by ``extra="forbid"`` instead of being silently swallowed.
    """
    prefix = f"{key}."
    return {
        (override_key[len(prefix) :] if override_key.startswith(prefix) else override_key): value
        for override_key, value in overrides.items()
    }


def load_realtime_config(
    path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> RealtimeConfig:
    """Build a :class:`RealtimeConfig` from the built-in defaults and ``overrides``.

    ``path`` is **ignored** and kept only for call compatibility: there is no
    profiles document any more, and this function never reads a file.  The engine
    settings live in the SQLite state store, and the bootstrap fields
    (``state_db``/``logs_dir``) are resolved by
    :func:`load_bootstrap_realtime_config`.

    ``overrides`` accepts nested mappings and dotted keys: the section name
    prefix is optional, so both ``{"poll_interval_seconds": 1.0}`` and
    ``{"realtime.poll_interval_seconds": 1.0}`` override the same field, and an
    unknown key is rejected with a
    :class:`~trading_platform.core.errors.ConfigError` (``extra="forbid"``).
    """
    return _load_defaults(RealtimeConfig, "realtime", overrides)


def load_monitoring_config(
    path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> MonitoringConfig:
    """Build a :class:`MonitoringConfig` from the built-in defaults and ``overrides``.

    ``path`` is **ignored** and kept only for call compatibility: this function
    never reads a file.  The monitoring settings live in the SQLite state store;
    ``--host``/``--port`` are per-invocation overrides that are never persisted.
    """
    return _load_defaults(MonitoringConfig, "monitoring", overrides)
