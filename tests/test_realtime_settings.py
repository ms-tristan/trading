"""Tests of the platform settings persisted in the state store (work-package WP1).

The state database is the **single source of truth** for the engine settings: this
module pins the seeding rule, the bootstrap surface, the runtime-update path and
every refusal.  Everything here is offline and deterministic: each test owns a
``tmp_path`` database and no test ever touches the network or the real
``deploy/profiles.json`` document (which no longer exists).

Grouping:

1. the storage envelope: shape, contents, absent bootstrap fields, no secret;
2. first initialisation: the store is seeded exactly once;
3. the bootstrap surface: ``state_db``/``logs_dir`` are inherited, never stored;
4. the runtime-update path: a change is visible to the next reader;
5. refusals: corrupted document, newer version, unknown key, bad type, write error.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from trading_platform.config.models import MonitoringConfig, RealtimeConfig
from trading_platform.core.errors import StateStoreError
from trading_platform.realtime.settings import (
    BOOTSTRAP_FIELDS,
    SETTINGS_META_KEY,
    SETTINGS_SECTIONS,
    SETTINGS_VERSION,
    PlatformSettings,
    SettingsError,
    save_settings,
    settings_from_store,
    update_settings,
)
from trading_platform.realtime.store import SqliteStateStore

#: The keys that must never appear in the persisted document: secrets and the
#: bootstrap surface that cannot live inside the database it locates.
FORBIDDEN_KEYS = ("api_key", "api_secret", "password", "secret", "token", "state_db", "logs_dir")


def stored_document(store: SqliteStateStore) -> str:
    """Return the raw JSON text the store holds for the settings key."""
    raw = store.get_meta(SETTINGS_META_KEY)
    assert raw is not None, "the settings document must have been written"
    return raw


@contextmanager
def raw_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a second connection, used to break the schema or read the file directly."""
    connection = sqlite3.connect(db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path of the database owned by one test (the ``settings_store`` database)."""
    return tmp_path / "realtime" / "state.db"


def stored_payload(store: SqliteStateStore) -> dict[str, Any]:
    """Return the decoded settings document held by the store."""
    payload = json.loads(stored_document(store))
    assert isinstance(payload, dict)
    return payload


def every_key(value: Any) -> list[str]:
    """Return every key of a nested JSON structure (mappings only)."""
    if isinstance(value, dict):
        keys: list[str] = []
        for key, item in value.items():
            keys.append(str(key))
            keys.extend(every_key(item))
        return keys
    if isinstance(value, list):
        return [key for item in value for key in every_key(item)]
    return []


# ---------------------------------------------------------------------------
# 1. the storage envelope
# ---------------------------------------------------------------------------


def test_the_meta_key_and_version_are_pinned() -> None:
    """The storage surface is a contract: one key, one version, two sections."""
    assert SETTINGS_META_KEY == "platform_settings"
    assert SETTINGS_VERSION == 1
    assert BOOTSTRAP_FIELDS == ("state_db", "logs_dir")
    assert SETTINGS_SECTIONS == ("realtime", "monitoring")


def test_the_document_persists_exactly_the_model_defaults(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """A store seeded from defaults holds the default dumps, minus the bootstrap.

    This is the backward-compatibility rule of the whole change: a host that never
    customises anything runs the exact values the configuration models declare.
    """
    settings_from_store(settings_store, bootstrap=bootstrap_settings)

    payload = stored_payload(settings_store)
    expected_realtime = RealtimeConfig().model_dump(mode="json")
    for field in BOOTSTRAP_FIELDS:
        expected_realtime.pop(field)
    assert payload == {
        "version": SETTINGS_VERSION,
        "realtime": expected_realtime,
        "monitoring": MonitoringConfig().model_dump(mode="json"),
    }


def test_the_document_is_serialised_with_sorted_keys(settings_store: SqliteStateStore) -> None:
    """The store's own ``sort_keys=True`` convention: the text is byte-stable."""
    settings_from_store(settings_store, bootstrap=PlatformSettings.from_dict({"version": 1}))

    raw = stored_document(settings_store)
    assert raw == json.dumps(json.loads(raw), sort_keys=True)


def test_the_document_holds_no_secret_and_no_bootstrap_field(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """Nothing secret, and never the path of the database it lives in."""
    settings_from_store(settings_store, bootstrap=bootstrap_settings)

    keys = every_key(stored_payload(settings_store))
    assert keys, "the document must not be empty"
    for forbidden in FORBIDDEN_KEYS:
        assert forbidden not in keys
    assert "token" not in stored_document(settings_store)


def test_platform_settings_is_a_frozen_value_object() -> None:
    """A reader always observes a complete, non-mutable pair of models."""
    settings = PlatformSettings.from_defaults()
    with pytest.raises(FrozenInstanceError):
        settings.realtime = RealtimeConfig(poll_interval_seconds=1.0)  # type: ignore[misc]


def test_from_defaults_is_the_seed() -> None:
    """``seed()`` is named for the decision it expresses, not for a second value."""
    assert PlatformSettings.seed() == PlatformSettings.from_defaults()
    assert PlatformSettings.seed().to_dict()["version"] == SETTINGS_VERSION


# ---------------------------------------------------------------------------
# 2. first initialisation: the store is seeded exactly once
# ---------------------------------------------------------------------------


def test_a_fresh_store_is_seeded_with_the_bootstrap_settings(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """The first read of an empty store writes the configured settings and answers them."""
    assert settings_store.get_meta(SETTINGS_META_KEY) is None

    settings = settings_from_store(settings_store, bootstrap=bootstrap_settings)

    assert settings == bootstrap_settings
    assert settings_store.get_meta(SETTINGS_META_KEY) is not None


def test_a_second_read_does_not_re_seed_the_store(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore, tmp_path: Path
) -> None:
    """A mutated value survives a reopen: the bootstrap is a seed, never a reset."""
    settings_from_store(settings_store, bootstrap=bootstrap_settings)
    settings_from_store(settings_store, bootstrap=bootstrap_settings)
    current = update_settings(
        settings_store, current=bootstrap_settings, realtime={"poll_interval_seconds": 1.25}
    )
    assert current.realtime.poll_interval_seconds == pytest.approx(1.25)
    settings_store.close()

    # a *different* bootstrap (a host that restarted with another Python process)
    # must not overwrite what the store remembers: only the first initialisation
    # seeds.  Only the bootstrap surface follows the new process.
    other = PlatformSettings(
        realtime=RealtimeConfig(state_db=tmp_path / "realtime" / "state.db", logs_dir=tmp_path),
        monitoring=MonitoringConfig(port=9999),
    )
    reopened = SqliteStateStore(tmp_path / "realtime" / "state.db")
    reopened.initialize()
    try:
        settings = settings_from_store(reopened, bootstrap=other)
        assert settings.realtime.poll_interval_seconds == pytest.approx(1.25)
        assert settings.realtime.state_db == other.realtime.state_db
        # the monitoring section is a stored *setting*: the store wins over the
        # newly constructed bootstrap, which only ever seeds the very first boot
        assert settings.monitoring.port == 8080
    finally:
        reopened.close()


def test_the_seed_is_written_once_only(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """Every later read reads the store; it never rewrites the seeded document."""
    settings_from_store(settings_store, bootstrap=bootstrap_settings)
    before = stored_document(settings_store)

    settings_from_store(settings_store, bootstrap=bootstrap_settings)

    assert stored_document(settings_store) == before


# ---------------------------------------------------------------------------
# 3. the bootstrap surface
# ---------------------------------------------------------------------------


def test_the_bootstrap_fields_are_inherited_and_never_stored(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """``state_db``/``logs_dir`` come from the bootstrap surface, on every read."""
    seeded = settings_from_store(settings_store, bootstrap=bootstrap_settings)

    assert seeded.realtime.state_db == bootstrap_settings.realtime.state_db
    assert seeded.realtime.logs_dir == bootstrap_settings.realtime.logs_dir
    assert "state_db" not in stored_payload(settings_store)["realtime"]
    assert "logs_dir" not in stored_payload(settings_store)["realtime"]


def test_the_bootstrap_fields_win_over_a_document_that_carries_them(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore, tmp_path: Path
) -> None:
    """A hand-written document cannot move the database out from under the process.

    The deep merge lets the stored document win for every *setting*, but the
    bootstrap surface is re-applied afterwards: the path of the store in use always
    answers the path the process was started with.
    """
    settings_from_store(settings_store, bootstrap=bootstrap_settings)
    payload = stored_payload(settings_store)
    payload["realtime"]["state_db"] = "/somewhere/else.db"
    payload["realtime"]["logs_dir"] = "/somewhere/else-logs"
    settings_store.set_meta(SETTINGS_META_KEY, json.dumps(payload, sort_keys=True))

    settings = settings_from_store(settings_store, bootstrap=bootstrap_settings)

    assert settings.realtime.state_db == bootstrap_settings.realtime.state_db
    assert settings.realtime.logs_dir == bootstrap_settings.realtime.logs_dir


def test_an_absent_field_of_a_stored_document_is_inherited_from_the_bootstrap(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """The deep merge is what makes a future build's new field boot cleanly."""
    settings_from_store(settings_store, bootstrap=bootstrap_settings)
    payload = stored_payload(settings_store)
    payload["realtime"].pop("reconcile_interval_seconds")
    settings_store.set_meta(SETTINGS_META_KEY, json.dumps(payload, sort_keys=True))

    settings = settings_from_store(settings_store, bootstrap=bootstrap_settings)

    assert settings.realtime.reconcile_interval_seconds == pytest.approx(
        bootstrap_settings.realtime.reconcile_interval_seconds
    )


# ---------------------------------------------------------------------------
# 4. the runtime-update path
# ---------------------------------------------------------------------------


def test_update_settings_is_visible_to_the_next_reader(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """A change made through the store is seen by a reader, with no file edited."""
    current = settings_from_store(settings_store, bootstrap=bootstrap_settings)

    updated = update_settings(
        settings_store,
        current=current,
        realtime={"history_candles": 777, "allow_network": False},
        monitoring={"port": 9099, "refresh_seconds": 7.5},
    )

    assert updated.realtime.history_candles == 777
    assert updated.realtime.allow_network is False
    assert updated.monitoring.port == 9099
    assert updated.monitoring.refresh_seconds == pytest.approx(7.5)
    reread = settings_from_store(settings_store, bootstrap=bootstrap_settings)
    assert reread == updated
    assert reread.realtime.history_candles == 777
    assert reread.monitoring.port == 9099


def test_update_settings_keeps_every_untouched_field(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """A partial update is partial: a sibling setting never moves."""
    current = settings_from_store(settings_store, bootstrap=bootstrap_settings)

    updated = update_settings(settings_store, current=current, realtime={"history_candles": 42})

    assert updated.realtime.history_candles == 42
    assert updated.realtime.poll_interval_seconds == current.realtime.poll_interval_seconds
    assert updated.monitoring == current.monitoring


def test_update_settings_never_persists_the_bootstrap_fields(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """The stored document still cannot carry the path of the database."""
    current = settings_from_store(settings_store, bootstrap=bootstrap_settings)

    update_settings(
        settings_store,
        current=current,
        realtime={"state_db": "/tmp/not-mine.db", "logs_dir": "/tmp/not-mine-logs"},
    )

    payload = stored_payload(settings_store)
    assert "state_db" not in payload["realtime"]
    assert "logs_dir" not in payload["realtime"]
    reread = settings_from_store(settings_store, bootstrap=bootstrap_settings)
    assert reread.realtime.state_db == current.realtime.state_db
    assert reread.realtime.logs_dir == current.realtime.logs_dir


def test_a_rejected_update_leaves_the_document_byte_identical(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """A refused value writes nothing: an unknown key and a bad type are both refused."""
    current = settings_from_store(settings_store, bootstrap=bootstrap_settings)
    before = stored_document(settings_store)

    with pytest.raises(SettingsError, match="invalid platform settings"):
        update_settings(settings_store, current=current, realtime={"not_a_setting": 1})
    with pytest.raises(SettingsError, match="invalid platform settings"):
        update_settings(settings_store, current=current, realtime={"history_candles": "many"})
    with pytest.raises(SettingsError, match="invalid platform settings"):
        update_settings(settings_store, current=current, realtime={"history_candles": 0})
    with pytest.raises(SettingsError, match="invalid platform settings"):
        update_settings(settings_store, current=current, monitoring={"port": 70000})

    assert stored_document(settings_store) == before
    assert settings_from_store(settings_store, bootstrap=bootstrap_settings) == current


def test_save_settings_reports_a_failing_write(bootstrap_settings: PlatformSettings) -> None:
    """A write that is refused is reported, never silently assumed to have landed."""
    store = SqliteStateStore(bootstrap_settings.realtime.state_db)
    with pytest.raises(SettingsError, match="cannot persist the platform settings"):
        save_settings(store, bootstrap_settings)


def test_settings_from_store_reports_a_failing_seed(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore, db_path: Path
) -> None:
    """The first initialisation is a write too: it fails loudly on an unusable store."""
    assert settings_store.get_meta(SETTINGS_META_KEY) is None
    with raw_connection(db_path) as conn:
        conn.execute("DROP TABLE meta")

    with pytest.raises(SettingsError, match="cannot read the platform settings"):
        settings_from_store(settings_store, bootstrap=bootstrap_settings)


def test_settings_error_is_a_state_store_error() -> None:
    """The domain-error alias is preserved: an existing handler still catches it."""
    assert issubclass(SettingsError, StateStoreError)


# ---------------------------------------------------------------------------
# 5. refusals of an unusable document
# ---------------------------------------------------------------------------


def test_a_corrupted_document_raises_a_settings_error(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """Corruption is never answered with silent defaults."""
    settings_store.set_meta(SETTINGS_META_KEY, "{not json")

    with pytest.raises(SettingsError, match="not valid JSON"):
        settings_from_store(settings_store, bootstrap=bootstrap_settings)


def test_a_document_that_is_not_an_object_raises_a_settings_error(
    settings_store: SqliteStateStore,
) -> None:
    settings_store.set_meta(SETTINGS_META_KEY, json.dumps([1, 2, 3]))

    with pytest.raises(SettingsError, match="must be a JSON object"):
        settings_from_store(settings_store, bootstrap=PlatformSettings.seed())


def test_a_document_with_an_invalid_field_raises_a_settings_error(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """A document that does not validate is refused, not repaired from the defaults."""
    payload = bootstrap_settings.to_dict()
    payload["monitoring"]["port"] = 70_000
    settings_store.set_meta(SETTINGS_META_KEY, json.dumps(payload, sort_keys=True))

    with pytest.raises(SettingsError, match="invalid platform settings"):
        settings_from_store(settings_store, bootstrap=bootstrap_settings)


def test_a_document_with_an_unknown_key_raises_a_settings_error(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """``extra="forbid"`` still applies to a hand-edited document."""
    payload = bootstrap_settings.to_dict()
    payload["realtime"]["unknown_setting"] = 1
    settings_store.set_meta(SETTINGS_META_KEY, json.dumps(payload, sort_keys=True))

    with pytest.raises(SettingsError, match="invalid platform settings"):
        settings_from_store(settings_store, bootstrap=bootstrap_settings)


def test_a_section_that_is_not_an_object_raises_a_settings_error(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    payload = bootstrap_settings.to_dict()
    payload["realtime"] = "everything is fine"
    settings_store.set_meta(SETTINGS_META_KEY, json.dumps(payload, sort_keys=True))

    with pytest.raises(SettingsError, match="section 'realtime' must be a JSON object"):
        settings_from_store(settings_store, bootstrap=bootstrap_settings)


def test_a_newer_document_version_is_refused(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """A document written by a more recent build is refused, never mangled."""
    payload = bootstrap_settings.to_dict()
    payload["version"] = SETTINGS_VERSION + 1
    settings_store.set_meta(SETTINGS_META_KEY, json.dumps(payload, sort_keys=True))

    with pytest.raises(SettingsError, match=f"version {SETTINGS_VERSION + 1}"):
        settings_from_store(settings_store, bootstrap=bootstrap_settings)
    # the refused document is left exactly as it was
    assert stored_payload(settings_store)["version"] == SETTINGS_VERSION + 1


def test_a_non_integer_version_is_refused(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    payload = bootstrap_settings.to_dict()
    payload["version"] = "one"
    settings_store.set_meta(SETTINGS_META_KEY, json.dumps(payload, sort_keys=True))

    with pytest.raises(SettingsError, match="version must be an integer"):
        settings_from_store(settings_store, bootstrap=bootstrap_settings)


def test_from_dict_refuses_a_payload_that_is_not_a_mapping() -> None:
    with pytest.raises(SettingsError, match="expected a JSON object"):
        PlatformSettings.from_dict([("realtime", {})])  # type: ignore[arg-type]


def test_the_stored_document_round_trips_through_from_dict(
    bootstrap_settings: PlatformSettings, settings_store: SqliteStateStore
) -> None:
    """The envelope is a stable, documented shape."""
    settings_from_store(settings_store, bootstrap=bootstrap_settings)

    payload = stored_payload(settings_store)
    assert list(payload) == ["monitoring", "realtime", "version"]
    rebuilt = PlatformSettings.from_dict(payload)
    assert rebuilt.monitoring == MonitoringConfig()
    assert rebuilt.realtime.poll_interval_seconds == RealtimeConfig().poll_interval_seconds


def test_the_settings_survive_a_close_and_reopen(tmp_path: Path) -> None:
    """A restart reads the store: the settings are durable state, not process state."""
    path = tmp_path / "realtime" / "state.db"
    first = SqliteStateStore(path)
    first.initialize()
    try:
        seeded = settings_from_store(first, bootstrap=PlatformSettings.seed())
        update_settings(first, current=seeded, realtime={"kill_switch_file": path / "stop.flag"})
    finally:
        first.close()

    second = SqliteStateStore(path)
    second.initialize()
    try:
        settings = settings_from_store(second, bootstrap=PlatformSettings.seed())
        assert settings.realtime.kill_switch_file == path / "stop.flag"
    finally:
        second.close()
