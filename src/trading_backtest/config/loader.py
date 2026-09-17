"""Configuration loading, merging, dumping and parameter overriding.

The loader accepts a JSON file (only), an optional mapping of overrides (nested
dicts and/or dotted keys) and validates the result through
:class:`~trading_backtest.config.models.AppConfig`.  Every failure is normalised
to :class:`~trading_backtest.core.errors.ConfigError`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trading_backtest.config.models import AppConfig
from trading_backtest.core.errors import ConfigError

__all__ = [
    "default_config",
    "dump_config",
    "load_config",
    "override_params",
]

_JSON_SUFFIXES = (".json",)


def default_config() -> AppConfig:
    """Return the built-in configuration (no file, environment still applies)."""
    return AppConfig()


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
