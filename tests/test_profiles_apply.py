"""Contract tests of the catalogue apply path (work package wp-profile-catalogue-cli).

Every test drives a **fake transport**: no socket is ever opened, no server is
ever started, and the platform's answers are exact values this file owns.  That
is what makes the interesting cases -- a 409 on one row, a ledger that cannot fund
the catalogue, a live profile whose credentials are half-written -- deterministic
instead of dependent on a venue.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from trading_platform.core.errors import ConfigError, ProfileError
from trading_platform.profiles.apply import (
    PROVISION_COMMAND,
    ApplyPlan,
    ProfileApiClient,
    _urlopen_transport,
    apply_catalogue,
    ledger_capacity,
    live_credential_issues,
    plan_apply,
)
from trading_platform.profiles.catalogue import (
    CATALOGUE_BY_ID,
    PROFILE_CATALOGUE,
    paper_definitions,
    paper_total_initial_balance,
)

#: The frozen payload keys of one apply run.
PAYLOAD_KEYS = frozenset(
    {
        "command",
        "ok",
        "api_url",
        "dry_run",
        "paper_total_initial_balance",
        "ledger_capacity",
        "created",
        "skipped",
        "pruned",
        "failed",
        "refused_live",
    }
)

#: The identifiers the deployed state database already holds (never in the catalogue).
LEGACY_PROFILE_IDS = (
    "momentumarpa",
    "momentumbome",
    "momentumbtc",
    "momentumeth",
    "momentumsol",
)

#: A paper ledger that can fund the whole catalogue several times over.
RICH_LEDGER: dict[str, Any] = {
    "total_cash": 100_000.0,
    "positions_value": 0.0,
    "initial_balance": 100_000.0,
}

#: The environment that arms the single live entry of the catalogue.
LIVE_ENVIRON: dict[str, str] = {
    "TB_ALLOW_LIVE_TRADING": "I_UNDERSTAND_THE_RISK",
    "TB_LIVE_API_KEY": "live-key",
    "TB_LIVE_API_SECRET": "live-secret",
}

#: The one live row of the catalogue, and the variables its refusal must name.
LIVE_ID = "momentum-dot-1d-live"
LIVE_KEY_ENV = "TB_PROFILE_MOMENTUM_DOT_1D_LIVE_API_KEY"
LIVE_SECRET_ENV = "TB_PROFILE_MOMENTUM_DOT_1D_LIVE_API_SECRET"


class FakeApi:
    """An in-memory stand-in of the three profile routes of the monitoring API.

    ``GET /api/profiles`` answers the identifiers and the ledgers it is given;
    ``POST`` refuses a duplicate with the server's own ``409``; ``DELETE`` removes
    an identifier.  Two knobs make a refusal reproducible: ``post_status`` /
    ``delete_status`` force the status of one identifier, and ``raw_payload``
    replaces the whole ``GET`` answer (a malformed body, an older server...).
    """

    def __init__(
        self,
        *,
        ids: tuple[str, ...] = (),
        ledger: Mapping[str, Any] | None = RICH_LEDGER,
        legacy_wallet: Mapping[str, Any] | None = None,
        raw_payload: Mapping[str, Any] | None = None,
        post_status: Mapping[str, tuple[int, str]] | None = None,
        delete_status: Mapping[str, tuple[int, str]] | None = None,
    ) -> None:
        self.ids = list(ids)
        self.ledger = None if ledger is None else dict(ledger)
        self.legacy_wallet = legacy_wallet
        self.raw_payload = raw_payload
        self.post_status = dict(post_status or {})
        self.delete_status = dict(delete_status or {})
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    # -- transport ---------------------------------------------------------

    def transport(
        self, method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        """Answer one request exactly like the monitoring API would."""
        self.calls.append((method, url, body, dict(headers)))
        return getattr(self, f"_{method.lower()}")(url, body)

    def _get(self, url: str, body: bytes | None) -> tuple[int, bytes]:
        if self.raw_payload is not None:
            return 200, json.dumps(dict(self.raw_payload)).encode("utf-8")
        payload = {
            "profiles": [{"profile_id": identifier} for identifier in self.ids],
            "generated_at": "2024-01-01T00:00:00+00:00",
            "wallet": self.legacy_wallet,
            "wallets": {"paper": self.ledger, "live": None},
        }
        return 200, json.dumps(payload).encode("utf-8")

    def _post(self, url: str, body: bytes | None) -> tuple[int, bytes]:
        request = json.loads((body or b"{}").decode("utf-8"))
        identifier = str(request["profile_id"])
        forced = self.post_status.get(identifier)
        if forced is not None:
            return forced[0], json.dumps({"error": forced[1]}).encode("utf-8")
        if identifier in self.ids:
            return 409, json.dumps({"error": f"profile {identifier!r} already exists"}).encode(
                "utf-8"
            )
        self.ids.append(identifier)
        return 201, json.dumps({"profile": {"profile_id": identifier}}).encode("utf-8")

    def _delete(self, url: str, body: bytes | None) -> tuple[int, bytes]:
        identifier = url.rsplit("/", 1)[-1]
        forced = self.delete_status.get(identifier)
        if forced is not None:
            return forced[0], json.dumps({"error": forced[1]}).encode("utf-8")
        if identifier not in self.ids:
            return 404, json.dumps({"error": f"unknown profile: {identifier!r}"}).encode("utf-8")
        self.ids.remove(identifier)
        return 200, json.dumps({"profile_id": identifier, "deleted": True}).encode("utf-8")

    # -- assertions --------------------------------------------------------

    def requests(self, method: str) -> list[tuple[str, str, bytes | None, dict[str, str]]]:
        """Return every recorded call of ``method``."""
        return [call for call in self.calls if call[0] == method]

    def created_ids(self) -> list[str]:
        """Return the identifiers of every recorded ``POST``, in order."""
        return [
            str(json.loads((call[2] or b"{}").decode("utf-8"))["profile_id"])
            for call in self.requests("POST")
        ]

    def deleted_ids(self) -> list[str]:
        """Return the identifiers of every recorded ``DELETE``, in order."""
        return [call[1].rsplit("/", 1)[-1] for call in self.requests("DELETE")]


def _client(api: FakeApi, *, token: str | None = "operator-token") -> ProfileApiClient:
    """Return a client over ``api``'s transport (no socket, ever)."""
    return ProfileApiClient(base_url="http://api.test", token=token, transport=api.transport)


def _paper_ids() -> list[str]:
    """Return the identifiers of the 26 paper rows, in catalogue order."""
    return [definition.id for definition in paper_definitions()]


# ---------------------------------------------------------------------------
# 1. the plan
# ---------------------------------------------------------------------------


def test_the_plan_splits_create_skip_and_prune_in_catalogue_order() -> None:
    existing = ["momentumarpa", _paper_ids()[0], "donchian-btc-4h"]
    plan = plan_apply(PROFILE_CATALOGUE, existing)

    assert isinstance(plan, ApplyPlan)
    created = [definition.id for definition in plan.to_create]
    skipped = [definition.id for definition in plan.to_skip]
    assert skipped == [_paper_ids()[0], "donchian-btc-4h"]
    assert created == [
        definition.id for definition in PROFILE_CATALOGUE if definition.id not in skipped
    ]
    assert plan.prunable == ("momentumarpa",)
    assert plan.to_dict() == {
        "to_create": created,
        "to_skip": skipped,
        "prunable": ["momentumarpa"],
    }


def test_the_plan_of_an_empty_platform_holds_the_whole_catalogue() -> None:
    plan = plan_apply(PROFILE_CATALOGUE, [])
    assert len(plan.to_create) == 27
    assert plan.to_skip == ()
    assert plan.prunable == ()


# ---------------------------------------------------------------------------
# 2. the nominal run, and idempotence
# ---------------------------------------------------------------------------


def test_a_first_apply_creates_the_whole_catalogue() -> None:
    api = FakeApi()

    payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)

    assert set(payload) == PAYLOAD_KEYS
    assert payload["command"] == PROVISION_COMMAND
    assert payload["ok"] is True
    assert payload["api_url"] == "http://api.test"
    assert payload["dry_run"] is False
    assert payload["created"] == [definition.id for definition in PROFILE_CATALOGUE]
    assert payload["created"][-1] == LIVE_ID
    assert payload["skipped"] == []
    assert payload["pruned"] == []
    assert payload["failed"] == []
    assert payload["refused_live"] == []
    assert payload["paper_total_initial_balance"] == 26_000.0
    assert payload["ledger_capacity"] == 100_000.0
    assert sorted(api.ids) == sorted(payload["created"])


def test_a_second_apply_creates_nothing() -> None:
    api = FakeApi()
    apply_catalogue(_client(api), environ=LIVE_ENVIRON)
    before = list(api.ids)
    api.calls.clear()

    payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)

    assert payload["ok"] is True
    assert payload["created"] == []
    assert payload["skipped"] == [definition.id for definition in PROFILE_CATALOGUE]
    assert api.requests("POST") == []
    assert api.requests("DELETE") == []
    assert api.ids == before


def test_the_paper_ledger_is_read_once_and_the_total_compared() -> None:
    api = FakeApi()
    payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)
    assert len(api.requests("GET")) == 1
    assert payload["paper_total_initial_balance"] == paper_total_initial_balance()


# ---------------------------------------------------------------------------
# 3. dry run
# ---------------------------------------------------------------------------


def test_a_dry_run_mutates_nothing_and_reports_the_plan() -> None:
    api = FakeApi(ids=("momentumarpa", _paper_ids()[0]))

    payload = apply_catalogue(_client(api), dry_run=True, prune=True, environ=LIVE_ENVIRON)

    assert payload["dry_run"] is True
    assert api.requests("POST") == []
    assert api.requests("DELETE") == []
    assert api.ids == ["momentumarpa", _paper_ids()[0]]
    assert payload["created"] == [
        definition.id for definition in PROFILE_CATALOGUE if definition.id != _paper_ids()[0]
    ]
    assert payload["skipped"] == [_paper_ids()[0]]
    assert payload["pruned"] == ["momentumarpa"]
    assert payload["ok"] is True


def test_a_dry_run_reports_the_live_refusal_like_a_real_run() -> None:
    api = FakeApi()
    payload = apply_catalogue(_client(api), dry_run=True, environ={})
    assert [entry["id"] for entry in payload["refused_live"]] == [LIVE_ID]
    assert payload["ok"] is False
    assert api.requests("POST") == []


# ---------------------------------------------------------------------------
# 4. refusals are loud and never silent
# ---------------------------------------------------------------------------


def test_a_refused_create_becomes_a_failed_entry_with_the_server_text() -> None:
    target = "donchian-btc-4h"
    api = FakeApi(post_status={target: (400, "unsupported timeframe: '3m'")})

    payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)

    assert payload["ok"] is False
    assert payload["failed"] == [
        {
            "id": target,
            "error": "POST http://api.test/api/profiles failed with HTTP 400: unsupported timeframe: '3m'",
        }
    ]
    assert len(payload["created"]) == 26
    assert target not in api.ids


def test_every_create_is_attempted_even_when_one_is_refused() -> None:
    api = FakeApi(post_status={"macd-btc-4h": (409, "profile already exists")})
    payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)
    assert len(api.requests("POST")) == 27
    assert [entry["id"] for entry in payload["failed"]] == ["macd-btc-4h"]


def test_a_refused_delete_is_reported_too() -> None:
    api = FakeApi(
        ids=("momentumarpa",),
        delete_status={"momentumarpa": (403, "missing or invalid operator token")},
    )

    payload = apply_catalogue(_client(api), prune=True, environ=LIVE_ENVIRON)

    assert payload["pruned"] == []
    assert payload["failed"] == [
        {
            "id": "momentumarpa",
            "error": "DELETE http://api.test/api/profiles/momentumarpa failed with HTTP 403: "
            "missing or invalid operator token",
        }
    ]
    assert payload["ok"] is False


# ---------------------------------------------------------------------------
# 5. never destructive by default
# ---------------------------------------------------------------------------


def test_the_default_run_deletes_nothing() -> None:
    api = FakeApi(ids=LEGACY_PROFILE_IDS)

    payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)

    assert api.requests("DELETE") == []
    assert payload["pruned"] == []
    assert set(LEGACY_PROFILE_IDS) <= set(api.ids)


def test_prune_deletes_only_the_identifiers_the_catalogue_does_not_know() -> None:
    api = FakeApi(ids=(*LEGACY_PROFILE_IDS, _paper_ids()[0]))

    payload = apply_catalogue(_client(api), prune=True, environ=LIVE_ENVIRON)

    assert payload["pruned"] == sorted(LEGACY_PROFILE_IDS)
    assert api.deleted_ids() == sorted(LEGACY_PROFILE_IDS)
    assert _paper_ids()[0] in api.ids


# ---------------------------------------------------------------------------
# 6. the money check
# ---------------------------------------------------------------------------


def test_the_capacity_check_refuses_an_over_committing_catalogue() -> None:
    api = FakeApi(
        ledger={"total_cash": 10_000.0, "positions_value": 0.0, "initial_balance": 15_000.0}
    )

    payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)

    assert payload["ok"] is False
    assert payload["ledger_capacity"] == 10_000.0
    assert payload["created"] == []
    assert api.requests("POST") == []
    # the refusal aborts the whole plan: the 26 paper rows and the armed live row
    # are all reported, none of them is created, and none is skipped silently.
    assert [entry["id"] for entry in payload["failed"]] == [*_paper_ids(), LIVE_ID]
    assert payload["refused_live"] == []
    message = payload["failed"][0]["error"]
    assert "26000.00 USDT" in message
    assert "10000.00 USDT" in message
    assert "shortfall 16000.00 USDT" in message
    assert "--force" in message
    assert LIVE_ID not in api.ids


def test_force_bypasses_the_capacity_check() -> None:
    api = FakeApi(ledger={"total_cash": 10.0, "positions_value": 0.0})

    payload = apply_catalogue(_client(api), force=True, environ=LIVE_ENVIRON)

    assert payload["ok"] is True
    assert len(payload["created"]) == 27
    assert payload["ledger_capacity"] == 10.0


def test_an_unknown_capacity_warns_loudly_and_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeApi(ledger=None, legacy_wallet=None)

    with caplog.at_level(logging.WARNING, logger="trading_platform.profiles.apply"):
        payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)

    assert payload["ledger_capacity"] is None
    assert len(payload["created"]) == 27
    assert payload["ok"] is True
    assert any("capacity is unknown" in record.message for record in caplog.records)


def test_an_empty_catalogue_never_fails_on_the_ledger() -> None:
    """Nothing to create means nothing to fund: the check can only fail a create."""
    api = FakeApi(
        ids=tuple(definition.id for definition in PROFILE_CATALOGUE), ledger={"total_cash": 1.0}
    )

    payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)

    assert payload["ok"] is True
    assert payload["failed"] == []


# ---------------------------------------------------------------------------
# 7. the live row
# ---------------------------------------------------------------------------


def test_the_live_row_is_refused_without_the_gate_and_the_credentials() -> None:
    api = FakeApi()

    payload = apply_catalogue(_client(api), environ={})

    assert payload["ok"] is False
    assert [entry["id"] for entry in payload["refused_live"]] == [LIVE_ID]
    reason = payload["refused_live"][0]["reason"]
    assert "TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK" in reason
    assert LIVE_KEY_ENV in reason
    assert LIVE_SECRET_ENV in reason
    assert "TB_LIVE_API_KEY" in reason
    assert "TB_LIVE_API_SECRET" in reason
    assert LIVE_ID not in api.ids


def test_the_live_row_is_refused_when_only_the_gate_is_armed() -> None:
    api = FakeApi()
    payload = apply_catalogue(
        _client(api), environ={"TB_ALLOW_LIVE_TRADING": "I_UNDERSTAND_THE_RISK"}
    )
    reason = payload["refused_live"][0]["reason"]
    assert "no venue credentials" in reason
    assert LIVE_KEY_ENV in reason


def test_the_live_row_is_refused_when_only_the_credentials_are_present() -> None:
    api = FakeApi()
    payload = apply_catalogue(_client(api), environ=dict(LIVE_ENVIRON, TB_ALLOW_LIVE_TRADING=""))
    reason = payload["refused_live"][0]["reason"]
    assert "TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK" in reason


def test_a_half_written_credential_pair_is_reported_as_a_reason() -> None:
    api = FakeApi()
    environ = dict(LIVE_ENVIRON)
    environ.pop("TB_LIVE_API_SECRET")
    environ[LIVE_KEY_ENV] = "profile-key"

    payload = apply_catalogue(_client(api), environ=environ)

    reason = payload["refused_live"][0]["reason"]
    assert LIVE_SECRET_ENV in reason
    assert "complete the pair or remove it" in reason


def test_the_live_row_is_created_when_the_gate_and_the_credentials_are_present() -> None:
    api = FakeApi()

    payload = apply_catalogue(_client(api), environ=LIVE_ENVIRON)

    assert LIVE_ID in api.ids
    assert payload["refused_live"] == []


def test_live_credential_issues_is_empty_for_every_paper_row() -> None:
    for definition in paper_definitions():
        assert live_credential_issues(definition, {}) == []


def test_live_credential_issues_accepts_the_profile_scoped_pair() -> None:
    environ = {
        "TB_ALLOW_LIVE_TRADING": "I_UNDERSTAND_THE_RISK",
        LIVE_KEY_ENV: "key",
        LIVE_SECRET_ENV: "secret",
    }
    assert live_credential_issues(CATALOGUE_BY_ID[LIVE_ID], environ) == []


# ---------------------------------------------------------------------------
# 8. the ledger capacity reader
# ---------------------------------------------------------------------------


def test_ledger_capacity_reads_cash_plus_positions_then_the_initial_balance() -> None:
    wallets = {"wallets": {"paper": {"total_cash": 100.0, "positions_value": 25.0}}}
    assert ledger_capacity(wallets) == 125.0

    only_initial = {"wallets": {"paper": {"initial_balance": 15_000.0}}}
    assert ledger_capacity(only_initial) == 15_000.0

    non_finite = {
        "wallets": {"paper": {"total_cash": None, "positions_value": 10.0, "initial_balance": 42.0}}
    }
    assert ledger_capacity(non_finite) == 42.0


def test_ledger_capacity_falls_back_to_the_paper_only_wallet_key() -> None:
    legacy = {"wallet": {"total_cash": 7.0, "positions_value": 3.0}}
    assert ledger_capacity(legacy) == 10.0
    assert ledger_capacity({"wallet": {"initial_balance": 9.0}}) == 9.0


def test_ledger_capacity_is_unknown_rather_than_invented() -> None:
    assert ledger_capacity({}) is None
    assert ledger_capacity({"wallets": {"paper": None, "live": None}}) is None
    assert ledger_capacity({"wallets": ["not", "a", "mapping"]}) is None
    assert ledger_capacity({"wallets": {"paper": {"total_cash": "many"}}}) is None


# ---------------------------------------------------------------------------
# 9. the HTTP client
# ---------------------------------------------------------------------------


def test_the_client_normalises_the_base_url_and_rejects_an_obvious_typo() -> None:
    assert ProfileApiClient("http://api.test/", None).base_url == "http://api.test"
    assert ProfileApiClient("http://api.test", "  ").token is None
    assert ProfileApiClient("http://api.test", " token ").token == "token"
    with pytest.raises(ConfigError):
        ProfileApiClient("", None)
    with pytest.raises(ConfigError):
        ProfileApiClient("127.0.0.1:8080", None)


def test_the_operator_token_travels_on_mutations_only() -> None:
    api = FakeApi(ids=("momentumarpa",))

    apply_catalogue(_client(api), prune=True, dry_run=False, environ=LIVE_ENVIRON)

    get_headers, post_headers, delete_headers = (
        api.requests("GET")[0][3],
        api.requests("POST")[0][3],
        api.requests("DELETE")[0][3],
    )
    assert "X-Operator-Token" not in get_headers
    assert post_headers["X-Operator-Token"] == "operator-token"
    assert delete_headers["X-Operator-Token"] == "operator-token"


def test_a_client_without_a_token_sends_no_header_and_reports_the_server_refusal() -> None:
    api = FakeApi(post_status={_paper_ids()[0]: (403, "missing or invalid operator token")})

    payload = apply_catalogue(_client(api, token=None), environ=LIVE_ENVIRON)

    assert "X-Operator-Token" not in api.requests("POST")[0][3]
    assert "missing or invalid operator token" in payload["failed"][0]["error"]


def test_a_non_2xx_answer_is_loud_at_the_client_level() -> None:
    api = FakeApi(ids=("momentumarpa",), delete_status={"momentumarpa": (403, "read-only server")})
    client = _client(api)

    with pytest.raises(ProfileError, match="HTTP 403: read-only server"):
        client.delete_profile("momentumarpa")


def test_a_malformed_answer_is_loud() -> None:
    without_profiles = _client(FakeApi(raw_payload={"generated_at": None}))
    with pytest.raises(ProfileError, match="without a 'profiles' list"):
        apply_catalogue(without_profiles, environ=LIVE_ENVIRON)


def test_the_identifier_reader_ignores_garbage_entries_and_reads_both_key_names() -> None:
    api = FakeApi(
        raw_payload={
            "profiles": [
                {"profile_id": "momentumarpa"},
                "not-a-mapping",
                {"id": "momentumbome"},
                {"profile_id": ""},
                {},
                42,
            ]
        }
    )

    payload = apply_catalogue(_client(api), dry_run=True, prune=True, environ=LIVE_ENVIRON)

    assert payload["created"] == [definition.id for definition in PROFILE_CATALOGUE]
    assert payload["pruned"] == ["momentumarpa", "momentumbome"]


def test_a_200_answer_that_is_not_a_json_object_is_loud() -> None:
    def transport(
        method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        return 200, b"[1, 2, 3]"

    with pytest.raises(ProfileError, match="expected an object"):
        ProfileApiClient("http://api.test", None, transport=transport).list_profiles()

    def not_json(
        method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        return 200, b"<html>nope</html>"

    with pytest.raises(ProfileError, match="not JSON"):
        ProfileApiClient("http://api.test", None, transport=not_json).list_profiles()


def test_the_empty_answer_of_a_delete_is_accepted() -> None:
    def transport(
        method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        return 204, b""

    assert ProfileApiClient("http://api.test", "t", transport=transport).delete_profile("x") == {}


def test_an_error_body_that_is_not_json_is_echoed_as_text() -> None:
    def transport(
        method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        return 500, b"<html>\n  <body>boom</body>\n</html>"

    with pytest.raises(ProfileError, match="HTTP 500: <html> <body>boom</body> </html>"):
        ProfileApiClient("http://api.test", None, transport=transport).list_profiles()

    def empty(
        method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        return 502, b""

    with pytest.raises(ProfileError, match="no response body"):
        ProfileApiClient("http://api.test", None, transport=empty).list_profiles()

    def blank(
        method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        return 502, b"   \n"

    with pytest.raises(ProfileError, match="no response body"):
        ProfileApiClient("http://api.test", None, transport=blank).list_profiles()


# ---------------------------------------------------------------------------
# 10. the default (urllib) transport, without a socket
# ---------------------------------------------------------------------------


class _FakeResponse:
    """The two members :func:`_urlopen_transport` uses of a ``urlopen`` answer."""

    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        """Return the body."""
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_the_default_transport_returns_the_status_and_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: _FakeResponse(b'{"ok": true}')
    )

    assert _urlopen_transport("GET", "http://api.test/api/profiles", None, {}, timeout=1.0) == (
        200,
        b'{"ok": true}',
    )


def test_the_default_transport_returns_an_http_error_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            "http://api.test/api/profiles",
            403,
            "Forbidden",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"error": "missing or invalid operator token"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    status, raw = _urlopen_transport("POST", "http://api.test/api/profiles", b"{}", {}, timeout=1.0)
    assert status == 403
    assert b"operator token" in raw


def test_the_default_transport_is_loud_when_the_api_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(request: Any, timeout: float) -> Any:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(ProfileError, match="cannot reach the monitoring API"):
        _urlopen_transport("GET", "http://api.test/api/profiles", None, {}, timeout=1.0)


# ---------------------------------------------------------------------------
# 11. the module contract
# ---------------------------------------------------------------------------


def test_the_catalogue_module_is_never_imported_by_the_strategy_or_realtime_layers() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "trading_platform"
    offenders = [
        path.relative_to(root).as_posix()
        for layer in ("strategy", "realtime")
        for path in (root / layer).rglob("*.py")
        if "trading_platform.profiles" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
