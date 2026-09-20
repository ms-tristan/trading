"""Platform settings persisted in SQLite (single source of truth).

The engine settings used to live in a committed JSON document
(``deploy/profiles.json``) that the monitoring API rewrote on every profile
change.  Because the deployment rebuilt that file from git, every change made
through the dashboard was silently destroyed by the next deploy.  This module
removes that failure mode: the **state store is the authoritative store** for the
engine settings, exactly as it already is for the profiles.

Storage
-------
The settings are one JSON document written into the existing ``meta`` key/value
table under :data:`SETTINGS_META_KEY`, through the existing
:meth:`~trading_platform.realtime.store.StateStore.set_meta` /
:meth:`~trading_platform.realtime.store.StateStore.get_meta` pair.  No second
mechanism is introduced, no table is added and the schema version does not move:
the migration is *nothing at all*.

Bootstrap surface
-----------------
The path of the database cannot live inside the database, so a minimal surface
must be known before the store can be opened: :data:`BOOTSTRAP_FIELDS`
(``state_db`` and ``logs_dir``) come from the small configuration file, the CLI
options and the environment variables.  Every other field is seeded into SQLite
at first initialisation and read back from SQLite on every later boot.  The
stored document therefore never carries the bootstrap fields (and, since the
models carry no credential field at all, never carries a secret either): on a
read, they are inherited from the bootstrap surface, so a host that never
customises anything behaves exactly as it did before this module existed.

Typical use
-----------
``settings = settings_from_store(store, bootstrap=PlatformSettings.seed())``
seeds the store on the very first boot and returns what the store remembers on
every later one.  :func:`update_settings` is the runtime-update path: the change
is visible to the very next read, with no file edited.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trading_platform.config.models import MonitoringConfig, RealtimeConfig
from trading_platform.core.errors import StateStoreError
from trading_platform.realtime.store import StateStore

__all__ = [
    "BOOTSTRAP_FIELDS",
    "SETTINGS_META_KEY",
    "SETTINGS_SECTIONS",
    "SETTINGS_VERSION",
    "PlatformSettings",
    "SettingsError",
    "save_settings",
    "settings_from_store",
    "update_settings",
]

logger = logging.getLogger(__name__)

#: ``meta`` key holding the persisted settings document (the single source of truth).
SETTINGS_META_KEY: str = "platform_settings"

#: Version of the stored settings envelope (not of the schema).
#:
#: A document written by a *newer* build is refused rather than silently mangled,
#: which is the same rule the store applies to its own ``schema_version``.
SETTINGS_VERSION: int = 1

#: Fields that must be known *before* the store can be opened, and are therefore
#: never persisted: the path of the database cannot live inside the database.
BOOTSTRAP_FIELDS: tuple[str, ...] = ("state_db", "logs_dir")

#: Persisted sections of the settings document.
SETTINGS_SECTIONS: tuple[str, ...] = ("realtime", "monitoring")


class SettingsError(StateStoreError):
    """The persisted platform settings are missing, unusable or were refused.

    Subclassing :class:`~trading_platform.core.errors.StateStoreError` keeps the
    domain-error alias: a caller that already catches the store's error catches
    this one too, while the dedicated type still lets the boot path name the
    settings surface explicitly.
    """


@dataclass(frozen=True)
class PlatformSettings:
    """The engine settings of the platform: the realtime and monitoring sections.

    A frozen value object, so a reader always observes a complete, self-consistent
    pair of models.  It is built from the store (:func:`settings_from_store`) and
    written to the store (:func:`save_settings`); it is never written to a file.
    """

    realtime: RealtimeConfig
    monitoring: MonitoringConfig

    def to_dict(self) -> dict[str, Any]:
        """Return the storage envelope of these settings.

        The ``realtime`` section is ``model_dump(mode="json")`` **without** the
        bootstrap fields (:data:`BOOTSTRAP_FIELDS`): the path of the state database
        and the log directory are known before the store exists and are inherited
        from the bootstrap surface on every read, so storing them would be both
        circular and misleading.  The models forbid unknown keys and declare no
        credential field, so the produced document can carry neither an unknown
        setting nor a secret.
        """
        realtime = self.realtime.model_dump(mode="json")
        for field in BOOTSTRAP_FIELDS:
            realtime.pop(field, None)
        return {
            "version": SETTINGS_VERSION,
            "realtime": realtime,
            "monitoring": self.monitoring.model_dump(mode="json"),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PlatformSettings:
        """Rebuild the settings from a storage envelope.

        Raises
        ------
        SettingsError
            If the payload is not a mapping, declares a newer version, or does not
            validate against the configuration models.
        """
        if not isinstance(payload, Mapping):
            raise SettingsError(
                f"invalid platform settings: expected a JSON object, got {type(payload).__name__}"
            )
        version = payload.get("version", SETTINGS_VERSION)
        if not isinstance(version, int) or isinstance(version, bool):
            raise SettingsError(
                f"invalid platform settings: version must be an integer, got {version!r}"
            )
        if version > SETTINGS_VERSION:
            raise SettingsError(
                f"platform settings use version {version}, newer than the supported "
                f"{SETTINGS_VERSION}"
            )
        sections: dict[str, Any] = {}
        for section in SETTINGS_SECTIONS:
            value = payload.get(section, {})
            if not isinstance(value, Mapping):
                raise SettingsError(
                    f"invalid platform settings: section {section!r} must be a JSON object, "
                    f"got {type(value).__name__}"
                )
            sections[section] = dict(value)
        try:
            return cls(
                realtime=RealtimeConfig.model_validate(sections["realtime"]),
                monitoring=MonitoringConfig.model_validate(sections["monitoring"]),
            )
        except ValueError as exc:
            raise SettingsError(f"invalid platform settings: {exc}") from exc

    @classmethod
    def from_defaults(cls) -> PlatformSettings:
        """Return the settings a host that never customises anything runs with.

        The defaults of the configuration models themselves, so the seeded store
        reproduces the exact behaviour of the configuration file that this module
        replaces.
        """
        return cls(realtime=RealtimeConfig(), monitoring=MonitoringConfig())

    @classmethod
    def seed(cls) -> PlatformSettings:
        """Return the settings written at the first initialisation of a store.

        Named separately from :meth:`from_defaults` so the call site reads as the
        decision it is: *these* are the values a brand new state database starts
        with, and they are written once and never re-applied.
        """
        return cls.from_defaults()


def _dumps(settings: PlatformSettings) -> str:
    """Serialise the envelope with the store's stable ``sort_keys=True`` convention."""
    return json.dumps(settings.to_dict(), sort_keys=True)


def _deep_merge(base: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``base`` deep-merged with ``incoming`` (incoming wins, leaf by leaf).

    Nested mappings are merged key by key, so a partial document -- the bootstrap
    surface, or a future build that adds one nested field -- never erases the
    sibling keys that only the other side carries.  Any other value replaces the
    base value wholesale, which is what makes an explicit ``null`` meaningful.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def settings_from_store(store: StateStore, *, bootstrap: PlatformSettings) -> PlatformSettings:
    """Return the settings of ``store``, seeding it on the very first call.

    The read path of the whole platform: the persisted document wins, and the
    bootstrap surface (``state_db``/``logs_dir``, plus any field a future build
    adds that this document predates) is inherited from ``bootstrap``.

    Parameters
    ----------
    store:
        An initialized state store.  Only its ``meta`` key/value surface is used.
    bootstrap:
        The minimal settings known before the store could be opened.  Written
        verbatim when the store remembers nothing.

    Returns
    -------
    PlatformSettings
        The settings a reader must use.

    Raises
    ------
    SettingsError
        If the stored document is not valid JSON, does not validate against the
        configuration models, or was written by a newer settings version.  A
        corrupted document is never silently replaced by the defaults.
    """
    try:
        raw = store.get_meta(SETTINGS_META_KEY)
    except StateStoreError as exc:
        raise SettingsError(f"cannot read the platform settings: {exc}") from exc
    if raw is None:
        logger.info(
            "no platform settings in the state store yet: seeding the configured defaults "
            "(the state database is now the source of truth)",
        )
        save_settings(store, bootstrap)
        return bootstrap
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise SettingsError(
            f"the platform settings stored in the state database are not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise SettingsError(
            "the platform settings stored in the state database must be a JSON object, "
            f"got {type(payload).__name__}"
        )
    stored = PlatformSettings.from_dict(payload)
    merged = _deep_merge(bootstrap.to_dict(), stored.to_dict())
    merged["version"] = SETTINGS_VERSION
    resolved = PlatformSettings.from_dict(merged)
    for field in BOOTSTRAP_FIELDS:
        value = getattr(bootstrap.realtime, field)
        if getattr(resolved.realtime, field) != value:
            resolved = PlatformSettings(
                realtime=resolved.realtime.model_copy(update={field: value}),
                monitoring=resolved.monitoring,
            )
    return resolved


def save_settings(store: StateStore, settings: PlatformSettings) -> None:
    """Persist ``settings`` into the store's ``meta`` table.

    Raises
    ------
    SettingsError
        If the write fails, so a caller never believes a setting was persisted
        when it was not.
    """
    try:
        store.set_meta(SETTINGS_META_KEY, _dumps(settings))
    except StateStoreError as exc:
        raise SettingsError(f"cannot persist the platform settings: {exc}") from exc


def update_settings(
    store: StateStore,
    *,
    current: PlatformSettings,
    realtime: Mapping[str, Any] | None = None,
    monitoring: Mapping[str, Any] | None = None,
) -> PlatformSettings:
    """Update a subset of the settings and persist the result.

    The partial mappings are deep-merged over the current values and the result is
    validated through :class:`~trading_platform.config.models.RealtimeConfig` /
    :class:`~trading_platform.config.models.MonitoringConfig`, so ``extra="forbid"``
    still refuses an unknown key and a badly typed value is rejected loudly.  A
    rejected update writes **nothing**: the stored document is left byte-identical.

    The bootstrap fields are re-injected from ``current`` verbatim before the write,
    so the persisted document can never drift into carrying the path of the database
    it lives in.

    Parameters
    ----------
    store:
        An initialized state store.
    current:
        The settings as the caller knows them (typically the last
        :func:`settings_from_store` result).  Its ``state_db``/``logs_dir`` are
        re-applied to the updated object.
    realtime:
        Partial ``realtime`` section, merged over the current one.
    monitoring:
        Partial ``monitoring`` section, merged over the current one.

    Returns
    -------
    PlatformSettings
        The **new** settings, also visible to the very next
        :func:`settings_from_store` call of any reader.

    Raises
    ------
    SettingsError
        If a value is rejected or the write fails.
    """
    envelope = current.to_dict()
    payload = {
        "version": SETTINGS_VERSION,
        "realtime": _deep_merge(
            envelope["realtime"],
            {} if realtime is None else dict(realtime),
        ),
        "monitoring": _deep_merge(
            envelope["monitoring"],
            {} if monitoring is None else dict(monitoring),
        ),
    }
    try:
        updated = PlatformSettings.from_dict(payload)
    except SettingsError:
        logger.warning("refused a platform settings update: the new values are invalid")
        raise
    for field in BOOTSTRAP_FIELDS:
        value = getattr(current.realtime, field)
        if getattr(updated.realtime, field) != value:
            updated = PlatformSettings(
                realtime=updated.realtime.model_copy(update={field: value}),
                monitoring=updated.monitoring,
            )
    save_settings(store, updated)
    logger.info("platform settings updated in the state store")
    return updated
