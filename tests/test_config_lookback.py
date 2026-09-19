"""The ``entry_lookback_candles`` profile field and its documentation contract.

This module pins REQUIREMENT 9 of the catch-up-entry feature: the configuration
compatibility trap.  ``tests/test_realtime_models.py`` asserts an exact set
equality between ``ProfileConfig.model_fields`` and the keys of every profile of
``config/profiles.example.json``, so adding the field *requires* extending the
shipped example -- never weakening the assertion.

Every test here fails as soon as the guard it pins disappears: the field, its
``ge``/``le`` bounds or the extended example document.

Deliberate surface update (the optional ``forecast`` profile field)
-------------------------------------------------------------------
The realtime forecast integration appends one more optional field to
``ProfileConfig``: ``forecast: Path | None``, the path of the offline forecast
artifact a forecast-driven strategy consumes.  It is declared as the **absolute
last** field so the field order of every pre-existing configuration never moves,
which pushes the two positions this module used to assert
(``[-1] == "entry_lookback_candles"``, ``[-2] == "risk"``) one slot down.  The
assertions were therefore updated to the new documented order
(``[-1] == "forecast"``, ``[-2] == "entry_lookback_candles"``, ``[-3] ==
"risk"``) and the example-profile test was extended to require the new key.
Nothing else changed: every bound, every credential/endpoint scan and the
legacy-document test are untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from trading_platform.config import load_profiles
from trading_platform.config.models import (
    MAX_ENTRY_LOOKBACK_CANDLES,
    MonitoringConfig,
    ProfileConfig,
    RealtimeConfig,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROFILES = REPO_ROOT / "config" / "profiles.example.json"

#: Credential-ish keys the shipped example must never carry.
FORBIDDEN_KEYS = ("api_key", "api_secret", "password", "secret", "token")

#: Endpoint schemes the shipped example must never carry either.
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
    # the forecast field is appended after it, so the catch-up window is now
    # last-but-one -- the order of every pre-existing field is unchanged
    assert list(ProfileConfig.model_fields)[-2] == "entry_lookback_candles"
    # positional construction of the pre-existing fields never moved
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
# 4. the shipped example declares the key on every profile
# ---------------------------------------------------------------------------


def test_example_profiles_declare_entry_lookback() -> None:
    payload = json.loads(EXAMPLE_PROFILES.read_text(encoding="utf-8"))
    declared: list[int] = []

    for profile in payload["profiles"]:
        assert "entry_lookback_candles" in profile, (
            f"profile {profile.get('id')!r} does not declare entry_lookback_candles"
        )
        value = profile["entry_lookback_candles"]
        assert isinstance(value, int) and not isinstance(value, bool)
        assert 0 <= value <= MAX_ENTRY_LOOKBACK_CANDLES
        declared.append(value)
        # the optional forecast field is part of the model surface: the shipped
        # example declares it on every profile, explicitly ``null`` by default
        assert "forecast" in profile, f"profile {profile.get('id')!r} declares no forecast key"
        assert profile["forecast"] is None

    loaded = load_profiles(EXAMPLE_PROFILES)

    assert [profile.entry_lookback_candles for profile in loaded] == declared
    assert all(profile.forecast is None for profile in loaded)


# ---------------------------------------------------------------------------
# 4b. the optional ``forecast`` profile field (realtime forecast integration)
# ---------------------------------------------------------------------------


def test_forecast_profile_field_is_last_optional_and_defaults_to_none() -> None:
    """The artifact path is optional, declared last, and ``None`` by default."""
    payload = json.loads(EXAMPLE_PROFILES.read_text(encoding="utf-8"))

    assert list(ProfileConfig.model_fields)[-1] == "forecast"
    assert ProfileConfig.model_fields["forecast"].default is None

    omitted = ProfileConfig(id="x", symbol="BTC/USDT")

    assert omitted.forecast is None
    assert omitted.forecast_artifact is None
    assert omitted.strategy_params() == {}
    # a document that omits the key keeps the historical behaviour
    assert omitted.model_dump()["forecast"] is None
    assert "forecast" in payload["profiles"][0]

    declared = ProfileConfig(id="x", symbol="BTC/USDT", forecast="a.parquet")

    assert declared.forecast == Path("a.parquet")
    assert declared.forecast_artifact == Path("a.parquet")
    assert declared.strategy_params() == {"artifact": "a.parquet"}


def test_strategy_params_never_mutates_the_profile_params() -> None:
    profile = ProfileConfig(id="x", symbol="BTC/USDT", params={"ema_fast": 9}, forecast="a.parquet")

    resolved = profile.strategy_params()

    assert resolved == {"ema_fast": 9, "artifact": "a.parquet"}
    assert profile.params == {"ema_fast": 9}
    resolved["ema_fast"] = 100
    assert profile.params["ema_fast"] == 9


def test_forecast_refuses_an_explicitly_unknown_key() -> None:
    """``extra="forbid"`` still applies to the appended field's neighbourhood."""
    with pytest.raises(ValidationError):
        ProfileConfig(id="x", symbol="BTC/USDT", forecast_artifact="a.parquet")


# ---------------------------------------------------------------------------
# 5. the same invariant tests/test_realtime_models.py asserts, pinned here too
# ---------------------------------------------------------------------------


def test_example_profile_keys_equal_the_model_fields() -> None:
    payload = json.loads(EXAMPLE_PROFILES.read_text(encoding="utf-8"))
    profile_keys = set(ProfileConfig.model_fields)
    risk_keys = set(ProfileConfig.model_fields["risk"].annotation.model_fields)

    for profile in payload["profiles"]:
        assert set(profile) == profile_keys, f"profile {profile.get('id')!r} drifted"
        assert set(profile["risk"]) == risk_keys
    assert set(payload["realtime"]) == set(RealtimeConfig.model_fields)
    assert set(payload["monitoring"]) == set(MonitoringConfig.model_fields)


# ---------------------------------------------------------------------------
# 6. an old document that omits the key keeps the historical behaviour
# ---------------------------------------------------------------------------


def test_a_document_that_omits_the_key_keeps_the_historical_behaviour(tmp_path: Path) -> None:
    document = {
        "profiles": [
            {"id": "legacy-btc", "symbol": "BTC/USDT"},
            {"id": "legacy-eth", "symbol": "ETH/USDT", "warmup_candles": 300},
        ]
    }
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    profiles = load_profiles(path)

    assert [profile.entry_lookback_candles for profile in profiles] == [0, 0]
    for profile in profiles:
        assert profile.model_dump()["entry_lookback_candles"] == 0


# ---------------------------------------------------------------------------
# 7. the new key is neither a credential nor an endpoint
# ---------------------------------------------------------------------------


def test_the_new_key_is_not_a_credential_and_adds_no_endpoint() -> None:
    text = EXAMPLE_PROFILES.read_text(encoding="utf-8")
    lowered = text.lower()

    for key in FORBIDDEN_KEYS:
        assert key not in lowered, f"the example profiles file contains {key!r}"
    for scheme in FORBIDDEN_SCHEMES:
        assert scheme not in lowered, f"the example profiles file contains {scheme!r}"
    assert "entry_lookback_candles" in text
