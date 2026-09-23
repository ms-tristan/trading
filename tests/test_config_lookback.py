"""The ``entry_lookback_candles`` profile field and its documentation contract.

This module pins REQUIREMENT 9 of the catch-up-entry feature: the configuration
compatibility trap.  ``tests/test_realtime_models.py`` asserts an exact set
equality between ``ProfileConfig.model_fields`` and the keys of every profile of
the declared profile set, so adding the field *requires* extending that set --
never weakening the assertion.

The declared profile set is no longer a committed JSON document: the SQLite state
store is the single source of truth for the profiles.  The declared set pinned
here is therefore ``DECLARED_PROFILES`` below, written into a real
:class:`~trading_platform.realtime.store.SqliteStateStore` -- the shipped tree
declares no profile at all, and a host starts empty on purpose.

Every test here fails as soon as the guard it pins disappears: the field, its
``ge``/``le`` bounds or the declared profile set.

"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trading_platform.config.models import (
    MAX_ENTRY_LOOKBACK_CANDLES,
    MonitoringConfig,
    ProfileConfig,
    RealtimeConfig,
)
from trading_platform.realtime.settings import BOOTSTRAP_FIELDS, PlatformSettings
from trading_platform.realtime.store import SqliteStateStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _declared_profile_payload(**overrides: Any) -> dict[str, Any]:
    """Return a full profile payload: every model field, plus ``overrides``.

    Built from ``model_dump(mode="json")`` of the model itself, so the declared
    set can never drift from the field surface: a field added to
    :class:`ProfileConfig` appears here without anyone editing this module, and
    ``test_declared_profile_keys_equal_the_model_fields`` then pins it.
    """
    payload = ProfileConfig(id="x", symbol="BTC/USDT").model_dump(mode="json")
    payload.update(overrides)
    return payload


#: The declared profile set this module pins, as it is stored in the state
#: database.  It is written into a real store (see :func:`declared_profiles`) so
#: the compatibility trap is exercised against the exact persistence path the
#: platform uses -- there is no configuration document any more.  The shipped
#: tree declares no profile at all: a host starts empty on purpose.
DECLARED_PROFILES: tuple[dict[str, Any], ...] = (
    _declared_profile_payload(
        id="btc-paper",
        symbol="BTC/USDT",
        entry_lookback_candles=0,
    ),
    _declared_profile_payload(
        id="eth-paper",
        symbol="ETH/USDT",
        entry_lookback_candles=3,
    ),
)


def declared_profiles(tmp_path: Path) -> list[ProfileConfig]:
    """Return every declared profile, read back from a real state store.

    The store round-trip is the point: a key the persistence layer silently drops
    would be invisible to a test that kept the payload in memory.
    """
    store = SqliteStateStore(tmp_path / "declared.db")
    store.initialize()
    try:
        for payload in DECLARED_PROFILES:
            store.save_profile(ProfileConfig.model_validate(payload))
        return list(store.load_profiles())
    finally:
        store.close()


#: Credential-ish keys the declared profile set must never carry.
FORBIDDEN_KEYS = ("api_key", "api_secret", "password", "secret", "token")

#: Endpoint schemes the declared profile set must never carry either.
FORBIDDEN_SCHEMES = ("http://", "https://", "wss://")


def _minimal_profile_payload(**overrides: Any) -> dict[str, Any]:
    """Return the smallest valid profile object, plus ``overrides``."""
    payload: dict[str, Any] = {"id": "x", "symbol": "BTC/USDT"}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. the default is 0: an existing configuration keeps its exact behaviour
# ---------------------------------------------------------------------------


def test_entry_lookback_defaults_to_zero() -> None:
    profile = ProfileConfig(id="x", symbol="BTC/USDT")

    assert profile.entry_lookback_candles == 0
    assert ProfileConfig.model_fields["entry_lookback_candles"].default == 0


# ---------------------------------------------------------------------------
# 2. the field is optional, last, and accepts the documented bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 3, MAX_ENTRY_LOOKBACK_CANDLES])
def test_entry_lookback_accepts_the_documented_bounds(value: int) -> None:
    profile = ProfileConfig(**_minimal_profile_payload(entry_lookback_candles=value))

    assert profile.entry_lookback_candles == value
    assert ProfileConfig.model_fields["entry_lookback_candles"].default == 0
    # the catch-up window stays where it was declared, and the per-profile history
    # override is appended after it, so the order of every pre-existing field never
    # moves and no serialised profile changes shape
    assert list(ProfileConfig.model_fields)[-2:] == ["entry_lookback_candles", "history_candles"]
    assert list(ProfileConfig.model_fields)[-3] == "risk"


# ---------------------------------------------------------------------------
# 3. negative and absurd values are refused loudly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-1, -100, 201, 10_000, 2**31])
def test_entry_lookback_refuses_negative_and_absurd(value: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ProfileConfig(**_minimal_profile_payload(entry_lookback_candles=value))

    assert "entry_lookback_candles" in str(excinfo.value)


@pytest.mark.parametrize("value", ["three", 1.5, None, [3]])
def test_entry_lookback_refuses_a_non_integer(value: Any) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ProfileConfig(**_minimal_profile_payload(entry_lookback_candles=value))

    assert "entry_lookback_candles" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. the declared profile set carries the key on every profile
# ---------------------------------------------------------------------------


def test_declared_profiles_carry_entry_lookback(tmp_path: Path) -> None:
    declared: list[int] = []

    for payload in DECLARED_PROFILES:
        assert "entry_lookback_candles" in payload, (
            f"profile {payload.get('id')!r} does not declare entry_lookback_candles"
        )
        value = payload["entry_lookback_candles"]
        assert isinstance(value, int) and not isinstance(value, bool)
        assert 0 <= value <= MAX_ENTRY_LOOKBACK_CANDLES
        declared.append(value)

    loaded = declared_profiles(tmp_path)

    assert [profile.entry_lookback_candles for profile in loaded] == declared


# ---------------------------------------------------------------------------
# 5. the same invariant tests/test_realtime_models.py asserts, pinned here too
# ---------------------------------------------------------------------------


def test_declared_profile_keys_equal_the_model_fields(tmp_path: Path) -> None:
    profile_keys = set(ProfileConfig.model_fields)
    risk_keys = set(ProfileConfig.model_fields["risk"].annotation.model_fields)

    for payload in DECLARED_PROFILES:
        assert set(payload) == profile_keys, f"profile {payload.get('id')!r} drifted"
        assert set(payload["risk"]) == risk_keys

    # the store round-trip preserves the exact key set the model declares
    for profile in declared_profiles(tmp_path):
        restored = profile.model_dump(mode="json")
        assert set(restored) == profile_keys
        assert set(restored["risk"]) == risk_keys

    # the engine settings live in SQLite too, seeded from the model defaults
    settings = PlatformSettings.from_defaults()

    assert set(settings.to_dict()["realtime"]) | set(BOOTSTRAP_FIELDS) == set(
        RealtimeConfig.model_fields
    )
    assert set(settings.to_dict()["monitoring"]) == set(MonitoringConfig.model_fields)


# ---------------------------------------------------------------------------
# 6. an old document that omits the key keeps the historical behaviour
# ---------------------------------------------------------------------------


def test_a_profile_row_that_omits_the_key_keeps_the_historical_behaviour(
    tmp_path: Path,
) -> None:
    """A profile stored without the key behaves exactly as it always did."""
    store = SqliteStateStore(tmp_path / "state.db")
    store.initialize()
    try:
        store.save_profile(ProfileConfig(id="legacy-btc", symbol="BTC/USDT"))
        store.save_profile(ProfileConfig(id="legacy-eth", symbol="ETH/USDT", warmup_candles=300))
        profiles = store.load_profiles()
    finally:
        store.close()

    assert [profile.entry_lookback_candles for profile in profiles] == [0, 0]
    for profile in profiles:
        assert profile.model_dump()["entry_lookback_candles"] == 0


# ---------------------------------------------------------------------------
# 7. the new key is neither a credential nor an endpoint
# ---------------------------------------------------------------------------


def test_the_new_key_is_not_a_credential_and_adds_no_endpoint() -> None:
    text = json.dumps(DECLARED_PROFILES, sort_keys=True)
    lowered = text.lower()

    for key in FORBIDDEN_KEYS:
        assert key not in lowered, f"the declared profiles contain {key!r}"
    for scheme in FORBIDDEN_SCHEMES:
        assert scheme not in lowered, f"the declared profiles contain {scheme!r}"
    assert "entry_lookback_candles" in text
