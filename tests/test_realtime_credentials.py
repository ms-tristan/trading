"""Contract tests of the environment-only credential resolver (work package wp4).

Covers, offline and deterministically:

* the naming scheme of ``profile_env_prefix`` and the two credential sets;
* ``credentials_from_env`` (precedence, ``None``, the dangling-key failure);
* ``known_secrets`` and ``redact``;
* the **secret-leakage** proof required by the delivery brief (D9): a sentinel
  value present in the environment appears in none of ``repr()``, ``str()``,
  ``to_dict()``, a JSON dump, a captured log record, or the raw bytes of a
  SQLite file written the way the state store writes it.

No network, no wall-clock dependency, no shared fixture: every helper is local
to this module and every environment is an explicit mapping -- nothing is ever
patched globally, so the tests cannot interfere with the rest of the suite.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from trading_backtest.core.errors import ProfileError
from trading_backtest.realtime.credentials import (
    ENV_LIVE_API_KEY,
    ENV_LIVE_API_PASSWORD,
    ENV_LIVE_API_SECRET,
    PROFILE_ENV_PREFIX,
    REDACTED,
    ExchangeCredentials,
    credentials_from_env,
    known_secrets,
    profile_env_prefix,
    redact,
)

#: The value the leakage proof tracks: obviously synthetic, easy to grep for.
SENTINEL = "SENTINEL_SECRET_42"
#: A second, unrelated value that must never be redacted by accident.
OTHER_VALUE = "another-value"


def live_environ(**overrides: str) -> dict[str, str]:
    """Return a global-only environment holding the sentinel in every field."""
    environ = {
        ENV_LIVE_API_KEY: SENTINEL,
        ENV_LIVE_API_SECRET: SENTINEL,
        ENV_LIVE_API_PASSWORD: SENTINEL,
    }
    environ.update(overrides)
    return environ


def write_state_like_store(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` the way the SQLite state store writes its rows.

    The helper deliberately mirrors the store's storage discipline (WAL journal,
    JSON column, one transaction, explicit checkpoint on close) without importing
    the store itself: wp4 must be testable before -- and independently of -- the
    persistence package.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS orders ("
            "client_order_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO orders (client_order_id, payload) VALUES (?, ?)",
                (str(payload["client_order_id"]), json.dumps(payload, sort_keys=True)),
            )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def state_bytes(path: Path) -> bytes:
    """Return the bytes of the database file and of its sidecar files."""
    data = b""
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            data += candidate.read_bytes()
    return data


# ---------------------------------------------------------------------------
# 1. naming and constants
# ---------------------------------------------------------------------------


def test_environment_variable_names_match_the_delivery_brief() -> None:
    assert ENV_LIVE_API_KEY == "TB_LIVE_API_KEY"
    assert ENV_LIVE_API_SECRET == "TB_LIVE_API_SECRET"
    assert ENV_LIVE_API_PASSWORD == "TB_LIVE_API_PASSWORD"
    assert PROFILE_ENV_PREFIX == "TB_PROFILE_"
    assert REDACTED == "***"


def test_profile_env_prefix_sanitises_every_unsafe_character() -> None:
    assert profile_env_prefix("btc-paper") == "TB_PROFILE_BTC_PAPER"
    assert profile_env_prefix("btc.usdt-live") == "TB_PROFILE_BTC_USDT_LIVE"
    assert profile_env_prefix("BTC/USDT") == "TB_PROFILE_BTC_USDT"
    assert profile_env_prefix("eth 4h paper") == "TB_PROFILE_ETH_4H_PAPER"
    assert profile_env_prefix("") == PROFILE_ENV_PREFIX


def test_module_all_is_the_frozen_public_surface() -> None:
    from trading_backtest.realtime import credentials as module

    assert sorted(module.__all__) == sorted(
        [
            "ENV_LIVE_API_KEY",
            "ENV_LIVE_API_PASSWORD",
            "ENV_LIVE_API_SECRET",
            "PROFILE_ENV_PREFIX",
            "REDACTED",
            "ExchangeCredentials",
            "credentials_from_env",
            "known_secrets",
            "profile_env_prefix",
            "redact",
        ]
    )
    assert len(module.__all__) == 10


# ---------------------------------------------------------------------------
# 2. resolution
# ---------------------------------------------------------------------------


def test_credentials_from_env_returns_none_when_nothing_is_set() -> None:
    assert credentials_from_env(environ={}) is None
    assert credentials_from_env("btc-paper", environ={}) is None
    # A secret without a key is not a credential either: nothing to report.
    assert credentials_from_env(environ={ENV_LIVE_API_SECRET: SENTINEL}) is None


def test_credentials_from_env_reads_the_global_pair() -> None:
    credentials = credentials_from_env(exchange="kraken", environ=live_environ())
    assert credentials is not None
    assert credentials.api_key == SENTINEL
    assert credentials.api_secret == SENTINEL
    assert credentials.password == SENTINEL
    assert credentials.exchange == "kraken"
    assert credentials.profile_id == ""
    assert credentials.source == "live-env"
    assert credentials.configured is True


def test_credentials_from_env_reads_the_per_profile_pair() -> None:
    environ = {
        f"{PROFILE_ENV_PREFIX}BTC_PAPER_API_KEY": "profile-key",
        f"{PROFILE_ENV_PREFIX}BTC_PAPER_API_SECRET": "profile-secret",
    }
    credentials = credentials_from_env("btc-paper", environ=environ)
    assert credentials is not None
    assert credentials.api_key == "profile-key"
    assert credentials.api_secret == "profile-secret"
    assert credentials.password == ""
    assert credentials.profile_id == "btc-paper"
    assert credentials.source == "profile-env"


def test_per_profile_variables_win_over_the_global_pair() -> None:
    environ = live_environ(
        **{
            f"{PROFILE_ENV_PREFIX}BTC_PAPER_API_KEY": "profile-key",
            f"{PROFILE_ENV_PREFIX}BTC_PAPER_API_SECRET": "profile-secret",
            f"{PROFILE_ENV_PREFIX}BTC_PAPER_API_PASSWORD": "profile-password",
        }
    )
    credentials = credentials_from_env("btc-paper", environ=environ)
    assert credentials is not None
    assert (credentials.api_key, credentials.api_secret) == ("profile-key", "profile-secret")
    assert credentials.password == "profile-password"
    assert credentials.source == "profile-env"


def test_per_profile_key_wins_even_when_the_global_pair_is_incomplete() -> None:
    environ = {
        ENV_LIVE_API_KEY: "global-key",  # no global secret: the global pair is broken
        f"{PROFILE_ENV_PREFIX}ETH_LIVE_API_KEY": "profile-key",
        f"{PROFILE_ENV_PREFIX}ETH_LIVE_API_SECRET": "profile-secret",
    }
    credentials = credentials_from_env("eth-live", environ=environ)
    assert credentials is not None
    assert credentials.source == "profile-env"
    # Without a per-profile key the broken global pair is reported, not silently used.
    with pytest.raises(ProfileError, match=ENV_LIVE_API_SECRET):
        credentials_from_env("btc-paper", environ=environ)


def test_a_key_without_its_secret_fails_loudly_per_profile() -> None:
    environ = {f"{PROFILE_ENV_PREFIX}BTC_PAPER_API_KEY": "profile-key"}
    with pytest.raises(ProfileError) as error:
        credentials_from_env("btc-paper", environ=environ)
    message = str(error.value)
    assert "btc-paper" in message
    assert "TB_PROFILE_BTC_PAPER_API_KEY" in message
    assert "TB_PROFILE_BTC_PAPER_API_SECRET" in message


def test_a_key_without_its_secret_fails_loudly_globally() -> None:
    with pytest.raises(ProfileError) as error:
        credentials_from_env(environ={ENV_LIVE_API_KEY: SENTINEL})
    message = str(error.value)
    assert ENV_LIVE_API_KEY in message
    assert ENV_LIVE_API_SECRET in message


def test_credentials_from_env_never_writes_to_the_process_environment() -> None:
    environ = live_environ()
    os.environ.pop(ENV_LIVE_API_KEY, None)
    credentials_from_env("btc-paper", environ=environ)
    assert ENV_LIVE_API_KEY not in os.environ
    assert SENTINEL not in os.environ.values()
    # The mapping handed in is only read, never mutated.
    assert set(environ) == {ENV_LIVE_API_KEY, ENV_LIVE_API_SECRET, ENV_LIVE_API_PASSWORD}


# ---------------------------------------------------------------------------
# 3. known_secrets and redact
# ---------------------------------------------------------------------------


def test_known_secrets_collects_both_schemes_and_skips_empty_values() -> None:
    environ = live_environ(
        **{
            ENV_LIVE_API_PASSWORD: "",  # empty: skipped
            f"{PROFILE_ENV_PREFIX}BTC_PAPER_API_KEY": "profile-key",
            f"{PROFILE_ENV_PREFIX}BTC_PAPER_API_SECRET": "profile-key",  # duplicate: once
            f"{PROFILE_ENV_PREFIX}ETH_LIVE_API_KEY": "",
            "UNRELATED_VARIABLE": "not-a-secret",
        }
    )
    secrets = known_secrets(environ)
    assert secrets == tuple(sorted({SENTINEL, "profile-key"}))
    assert "not-a-secret" not in secrets
    assert "" not in secrets


def test_known_secrets_of_an_empty_environment_is_empty() -> None:
    assert known_secrets({}) == ()


def test_redact_replaces_every_known_secret() -> None:
    secrets = known_secrets(live_environ())
    text = f"key={SENTINEL} and again {SENTINEL}, plus {OTHER_VALUE}"
    redacted = redact(text, secrets=secrets)
    assert SENTINEL not in redacted
    assert OTHER_VALUE in redacted
    assert redacted.count(REDACTED) == 2


def test_redact_maps_none_to_the_empty_string_and_ignores_empty_secrets() -> None:
    assert redact(None) == ""
    assert redact(None, secrets=["whatever"]) == ""
    assert redact("plain", secrets=[""]) == "plain"
    assert redact("plain", secrets=()) == "plain"


def test_redact_without_secrets_is_the_identity() -> None:
    assert redact(SENTINEL) == SENTINEL


# ---------------------------------------------------------------------------
# 4. redaction of every ExchangeCredentials surface
# ---------------------------------------------------------------------------


def make_credentials(**overrides: Any) -> ExchangeCredentials:
    """Build a fully populated credential object holding the sentinel."""
    values: dict[str, Any] = {
        "api_key": SENTINEL,
        "api_secret": SENTINEL,
        "password": SENTINEL,
        "exchange": "binance",
        "profile_id": "btc-live",
        "source": "profile-env",
    }
    values.update(overrides)
    return ExchangeCredentials(**values)


def test_exchange_credentials_is_frozen_and_describes_itself() -> None:
    credentials = make_credentials()
    assert credentials.configured is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        credentials.api_key = "other"  # type: ignore[misc]


def test_to_dict_is_always_redacted_and_has_no_unsafe_parameter() -> None:
    credentials = make_credentials()
    payload = credentials.to_dict()
    assert payload == {
        "api_key": REDACTED,
        "api_secret": REDACTED,
        "password": REDACTED,
        "exchange": "binance",
        "profile_id": "btc-live",
        "configured": True,
        "source": "profile-env",
    }
    import inspect

    assert list(inspect.signature(ExchangeCredentials.to_dict).parameters) == ["self"]


def test_to_dict_leaves_an_absent_secret_empty_rather_than_redacted() -> None:
    payload = ExchangeCredentials(api_key="k", api_secret="s").to_dict()
    assert payload["password"] == ""
    assert payload["api_key"] == REDACTED
    assert payload["api_secret"] == REDACTED
    assert payload["profile_id"] == ""
    assert payload["source"] == ""


def test_repr_str_and_json_never_carry_the_secret() -> None:
    credentials = make_credentials()
    surfaces = [
        repr(credentials),
        str(credentials),
        f"{credentials}",
        f"{credentials!r}",
        json.dumps(credentials.to_dict()),
        json.dumps(credentials.to_dict(), indent=2),
    ]
    for surface in surfaces:
        assert SENTINEL not in surface
    assert REDACTED in repr(credentials)


def test_an_unconfigured_credential_reports_itself_unconfigured() -> None:
    credentials = ExchangeCredentials(api_key="", api_secret="")
    assert credentials.configured is False
    assert credentials.to_dict()["configured"] is False
    assert REDACTED not in repr(credentials)


# ---------------------------------------------------------------------------
# 5. the leakage proof (brief D9)
# ---------------------------------------------------------------------------


def test_no_secret_ever_reaches_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    environ = live_environ()
    assert SENTINEL in environ.values()  # the proof is not vacuous
    credentials = credentials_from_env("btc-live", environ=environ)
    assert credentials is not None
    secrets = known_secrets(environ)

    with caplog.at_level(logging.DEBUG, logger="trading_backtest.realtime"):
        logger = logging.getLogger("trading_backtest.realtime.broker")
        logger.info("credentials=%r", credentials)
        logger.info("payload=%s", json.dumps(credentials.to_dict()))
        logger.info("raw=%s", redact(repr(environ), secrets=secrets))
        logger.debug("source=%s profile=%s", credentials.source, credentials.profile_id)

    assert caplog.records, "the write cycle must actually log something"
    assert SENTINEL not in caplog.text
    assert REDACTED in caplog.text


def test_no_secret_ever_reaches_the_bytes_of_a_state_database(tmp_path: Path) -> None:
    environ = live_environ()
    credentials = credentials_from_env("btc-live", environ=environ)
    assert credentials is not None

    payload = {
        "client_order_id": "btc-live-BTC_USDT-20240101T000000Z-0000",
        "profile_id": "btc-live",
        "mode": "live",
        "credentials": credentials.to_dict(),
        "credentials_repr": repr(credentials),
    }
    database = tmp_path / "state.db"
    write_state_like_store(database, payload)
    raw = state_bytes(database)

    assert b"btc-live-BTC_USDT" in raw, "the payload really was persisted"
    assert REDACTED.encode() in raw
    assert SENTINEL not in raw.decode("utf-8", errors="replace")

    # Control: the very same check does detect the sentinel when it is written,
    # so the assertion above is not vacuously true.
    control = tmp_path / "control.db"
    control_payload = dict(payload, credentials_repr=SENTINEL)
    write_state_like_store(control, control_payload)
    assert SENTINEL in state_bytes(control).decode("utf-8", errors="replace")


def test_the_resolver_itself_never_returns_the_environment_mapping() -> None:
    environ = live_environ()
    credentials = credentials_from_env(environ=environ)
    assert credentials is not None
    assert not isinstance(credentials, dict)
    assert set(dataclasses.asdict(credentials)) == {
        "api_key",
        "api_secret",
        "password",
        "exchange",
        "profile_id",
        "source",
    }
