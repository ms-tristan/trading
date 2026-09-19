"""Configuration loading, merging, dumping and parameter overriding.

The loader accepts a JSON file (only), an optional mapping of overrides (nested
dicts and/or dotted keys) and validates the result through
:class:`~trading_platform.config.models.AppConfig`.  Every failure is normalised
to :class:`~trading_platform.core.errors.ConfigError`.

The same module owns the realtime documents: a *profiles file* is a single JSON
object whose three allowed root keys are ``profiles`` (a required, non-empty list
of :class:`~trading_platform.config.models.ProfileConfig`), ``realtime`` and
``monitoring``.  :func:`load_profiles`, :func:`load_realtime_config` and
:func:`load_monitoring_config` read that one file; each of them ignores the keys
it does not own, so a run, a monitoring-only server and a pre-flight check all
consume the same document.  :func:`save_profiles` owns the write side: it replaces
the ``profiles`` key and preserves every other root key, atomically.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from trading_platform.config.models import (
    AppConfig,
    MonitoringConfig,
    ProfileConfig,
    RealtimeConfig,
)
from trading_platform.core.errors import ConfigError

__all__ = [
    "default_config",
    "default_monitoring_config",
    "default_realtime_config",
    "dump_config",
    "load_config",
    "load_monitoring_config",
    "load_profiles",
    "load_realtime_config",
    "override_params",
    "save_profiles",
]

_JSON_SUFFIXES = (".json",)

#: The only keys accepted at the root of a profiles document.
_PROFILES_ROOT_KEYS: frozenset[str] = frozenset({"profiles", "realtime", "monitoring"})

#: How many names :func:`_open_temporary` tries before giving up.
_TEMPORARY_ATTEMPTS: int = 100

_SectionModel = TypeVar("_SectionModel", bound=BaseModel)


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
# profiles document: profiles + realtime + monitoring
# ---------------------------------------------------------------------------


def load_profiles(path: str | Path) -> list[ProfileConfig]:
    """Load every profile declared by the profiles document ``path``.

    The document is a JSON object whose root keys are limited to ``profiles``,
    ``realtime`` and ``monitoring``.  This function owns the ``profiles`` key and
    ignores the two others.

    Parameters
    ----------
    path:
        JSON profiles file (``.json`` only, like :func:`load_config`).

    Returns
    -------
    list[ProfileConfig]
        The declared profiles, in file order.  Profiles whose ``enabled`` flag is
        ``False`` are returned too: filtering is the caller's decision.

    Raises
    ------
    ConfigError
        Wrong extension, missing file, invalid JSON, non-object root, unknown root
        key, missing or empty ``profiles`` list, invalid profile entry or a
        duplicated profile id.
    """
    target = Path(path)
    payload = _read_payload(target)
    for key in payload:
        if key not in _PROFILES_ROOT_KEYS:
            allowed = ", ".join(sorted(_PROFILES_ROOT_KEYS))
            raise ConfigError(
                f"unknown key at the root of the profiles file {target}: {key!r} "
                f"(allowed: {allowed})"
            )
    if "profiles" not in payload:
        raise ConfigError(f"the profiles file {target} declares no 'profiles' key")
    raw_profiles = payload["profiles"]
    if not isinstance(raw_profiles, list):
        raise ConfigError(
            f"the 'profiles' key of {target} must be a JSON list, got {type(raw_profiles).__name__}"
        )
    if not raw_profiles:
        raise ConfigError("the profiles file declares no profile")

    profiles: list[ProfileConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_profiles):
        if not isinstance(entry, Mapping):
            raise ConfigError(
                f"invalid profile at index {index}: expected a JSON object, "
                f"got {type(entry).__name__}"
            )
        try:
            profile = ProfileConfig.model_validate(dict(entry))
        except ValidationError as exc:
            raise ConfigError(
                f"invalid profile at index {index}: {_format_validation_error(exc)}"
            ) from exc
        if profile.id in seen:
            raise ConfigError(f"duplicate profile id: {profile.id!r}")
        seen.add(profile.id)
        profiles.append(profile)
    return profiles


def _open_temporary(directory: Path, name: str, mode: int) -> tuple[int, str]:
    """Create a unique temporary file next to ``name``, with ``mode``.

    ``tempfile.mkstemp`` cannot be used here: it hard-codes 0o600, and the mode
    of the file that gets renamed onto the target is the one the target ends up
    with. ``O_EXCL`` keeps the creation atomic, so two concurrent callers can
    never pick the same name.
    """
    for _ in range(_TEMPORARY_ATTEMPTS):
        candidate = directory / f".{name}.{secrets.token_hex(6)}.tmp"
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        except FileExistsError:
            continue
        return descriptor, str(candidate)
    raise ConfigError(f"cannot create a temporary file next to {directory / name}")


def save_profiles(path: str | Path, profiles: Sequence[ProfileConfig]) -> Path:
    """Rewrite the ``profiles`` key of the profiles document ``path``, atomically.

    The profiles file is the on-disk **source of truth** of the running platform, so
    the create/delete routes rewrite it while the engine keeps running.  The document
    is therefore never truncated in place: the existing payload is read first, only
    its ``profiles`` key is replaced, and the result is written to a temporary file
    of the same directory which is then moved onto the target with :func:`os.replace`
    -- an atomic rename on every supported platform.  A crash between the two steps
    leaves the original file untouched, and a reader never observes a half-written
    document.

    Every other root key (``realtime``, ``monitoring``) is preserved **verbatim**: a
    caller that owns only the profile list must not silently drop the engine
    settings that share the document.

    Parameters
    ----------
    path:
        JSON profiles file (``.json`` only, like :func:`load_profiles`).  It must
        already exist: this function updates a document, it never invents one.
    profiles:
        The profiles to declare, in the order they must appear.  An empty sequence
        is written as an empty list; whether that is a legal platform is the
        caller's rule and :func:`load_profiles` still refuses to read it.

    Returns
    -------
    Path
        The written path.

    Raises
    ------
    ConfigError
        The document cannot be read (missing file, wrong extension, invalid JSON),
        or it cannot be written (unwritable directory, failing rename).  The
        original file is left untouched and the temporary file is removed.
    """
    target = Path(path)
    payload = _read_payload(target)
    payload["profiles"] = [profile.model_dump(mode="json") for profile in profiles]
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    # The mode has to be right at CREATION time. Some bind mounts -- Docker
    # Desktop's virtiofs on macOS, for one -- refuse chmod outright with EPERM,
    # and ``tempfile.mkstemp`` hard-codes 0o600: the rename would then hand the
    # target a mode that makes it unreadable to the uid the mount maps the owner
    # to, which is exactly how the container lost access to the config it had
    # just written.
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        mode = 0o644
    temporary: Path | None = None
    try:
        handle, name = _open_temporary(target.parent, target.name, mode)
        temporary = Path(name)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Best effort, for platforms where the umask stripped bits at creation.
        with contextlib.suppress(OSError):
            temporary.chmod(mode)
        # ``Path.replace`` *is* ``os.replace``: an atomic rename on every supported
        # platform, which is what makes the rewrite all-or-nothing for a reader.
        temporary.replace(target)
    except OSError as exc:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise ConfigError(f"cannot write profiles file {target}: {exc}") from exc
    return target


def _strip_section_prefix(overrides: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Drop the ``"<section>."`` prefix of dotted override keys.

    ``load_realtime_config(path, {"realtime.poll_interval_seconds": 1})`` and
    ``load_realtime_config(path, {"poll_interval_seconds": 1})`` therefore mean
    the same thing.  A key that does not start with the section name is left
    untouched, so an override targeting another section is still rejected loudly
    by ``extra="forbid"`` instead of being silently swallowed.
    """
    prefix = f"{key}."
    return {
        (override_key[len(prefix) :] if override_key.startswith(prefix) else override_key): value
        for override_key, value in overrides.items()
    }


def _load_section(
    model: type[_SectionModel],
    key: str,
    path: str | Path | None,
    overrides: Mapping[str, Any] | None,
) -> _SectionModel:
    """Load one ``realtime``/``monitoring`` section over the built-in defaults."""
    payload: dict[str, Any] = {}
    if path is not None:
        document = _read_payload(Path(path))
        section = document.get(key)
        if section is not None and not isinstance(section, Mapping):
            raise ConfigError(
                f"the {key!r} key of {path} must be a JSON object, got {type(section).__name__}"
            )
        payload = dict(section or {})
    merged = _deep_merge(model().model_dump(), payload)
    if overrides is not None:
        merged = _apply_overrides(merged, _strip_section_prefix(overrides, key))
    try:
        return model.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid {key} configuration: {_format_validation_error(exc)}") from exc


def load_realtime_config(
    path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> RealtimeConfig:
    """Build a :class:`RealtimeConfig` from the ``realtime`` key of a profiles file.

    ``path=None`` means "defaults only".  ``overrides`` accepts nested mappings and
    dotted keys: the section name prefix is optional, so both
    ``{"poll_interval_seconds": 1.0}`` and ``{"realtime.poll_interval_seconds": 1.0}``
    override the same field, and an unknown key is rejected with a
    :class:`~trading_platform.core.errors.ConfigError` (``extra="forbid"``).
    """
    return _load_section(RealtimeConfig, "realtime", path, overrides)


def load_monitoring_config(
    path: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> MonitoringConfig:
    """Build a :class:`MonitoringConfig` from the ``monitoring`` key of a profiles file.

    ``path=None`` means "defaults only"; ``overrides`` follows the same nested /
    dotted semantics as :func:`load_config`.
    """
    return _load_section(MonitoringConfig, "monitoring", path, overrides)
