"""Idempotent, dry-runnable, non-destructive apply path of the profile catalogue.

The catalogue declares what the platform *should* hold; this module reconciles
that declaration with what the running platform *does* hold, over the platform's
own HTTP API and nothing else.  It is the engine behind
``trading realtime provision``, and every one of its properties is a safety
property an operator relies on:

idempotent
    the plan is ``catalogue - existing``: a second run on a platform that already
    holds every identifier creates **nothing** and deletes nothing;
dry-runnable
    ``dry_run=True`` performs **no mutation at all** -- no ``POST``, no ``DELETE``
    -- while reporting exactly what a real run would do, so the first command an
    operator types is always a safe one;
non-destructive by default
    nothing is ever deleted unless ``prune=True`` is passed explicitly, and then
    only the identifiers the catalogue does not know about (the five legacy
    momentum profiles of the deployed state database are exactly that);
loud
    every non-2xx answer of the API becomes an error carrying the status **and**
    the server's own error text.  A refusal is reported, never skipped;
honest about live trading
    a live row is refused unless the venue credentials *and* the live gate are
    present in the engine environment, because a live profile the broker cannot
    authenticate is quarantined as a boot failure.  No live profile is fabricated;
honest about money
    one shared USDT ledger funds every paper order, so the catalogue's committed
    total is compared to the ledger's own capacity before anything is created,
    and an over-commitment is refused unless ``force=True``.

Testability
-----------
The client takes an injected ``transport`` -- a plain
``(method, url, body, headers) -> (status, bytes)`` callable -- so the whole apply
path is exercised with **zero sockets**: every test drives a fake API, and the
default transport (``urllib.request``, imported inside its own body) is the only
thing that ever opens a connection.

Layer direction (frozen)
------------------------
This module consumes ``config`` through the catalogue, the pure ``realtime``
seams (``credentials``, ``risk``) and the standard library.  It imports no part of
``web``, and ``trading_platform.cli`` imports it **inside the command body**.
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from trading_platform.core.errors import ConfigError, ProfileError, TradingBacktestError
from trading_platform.profiles.catalogue import (
    PROFILE_CATALOGUE,
    ProfileDefinition,
    paper_total_initial_balance,
)
from trading_platform.realtime.credentials import (
    ENV_LIVE_API_KEY,
    ENV_LIVE_API_SECRET,
    credentials_from_env,
    profile_env_prefix,
)
from trading_platform.realtime.risk import LiveTradingGate

__all__ = [
    "PROVISION_COMMAND",
    "ApplyPlan",
    "ProfileApiClient",
    "Transport",
    "apply_catalogue",
    "ledger_capacity",
    "live_credential_issues",
    "plan_apply",
]

#: Logger of the apply path; the loud warnings of a blind run travel through it.
LOGGER = logging.getLogger(__name__)

#: The ``command`` key of the payload, i.e. the name of the CLI surface.
PROVISION_COMMAND = "realtime-provision"

#: Header carrying the operator token on a mutating request.
#:
#: It mirrors :data:`trading_platform.web.routes.OPERATOR_TOKEN_HEADER` exactly and
#: deliberately does **not** import it: ``profiles`` must never depend on the web
#: layer (the direction is frozen), and the header name is part of the documented
#: HTTP contract, not of the web implementation.
OPERATOR_TOKEN_HEADER = "X-Operator-Token"

#: Environment variable the operator token is read from (mirrors ``web.routes``).
OPERATOR_TOKEN_ENV = "TB_OPERATOR_TOKEN"

#: The credential vocabulary the CLI already uses for its pre-flight messages.
#:
#: The wording is mirrored verbatim from ``cli._CREDENTIAL_HELP``: the two modules
#: must not import each other (``cli`` imports ``profiles`` inside its command
#: body), and an operator who read the sentence once must recognise it everywhere.
CREDENTIAL_HELP = "set TB_LIVE_API_KEY/TB_LIVE_API_SECRET or TB_PROFILE_<ID>_API_KEY/_API_SECRET"

#: Longest server error text echoed in a message (a whole HTML page helps nobody).
_MAX_ERROR_TEXT = 500

#: One HTTP round trip: ``(method, url, body, headers) -> (status, body bytes)``.
Transport = Callable[[str, str, "bytes | None", Mapping[str, str]], "tuple[int, bytes]"]


@dataclass(frozen=True)
class ApplyPlan:
    """What applying a catalogue against a platform's identifiers would do.

    Attributes
    ----------
    to_create:
        Catalogue rows whose identifier is unknown to the platform, in catalogue
        order.
    to_skip:
        Catalogue rows the platform already holds, in catalogue order.
    prunable:
        Identifiers the platform holds but the catalogue does not know about,
        sorted ascending (the ``--prune`` set, and nothing else).
    """

    to_create: tuple[ProfileDefinition, ...]
    to_skip: tuple[ProfileDefinition, ...]
    prunable: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the plan as the JSON-ready ``{to_create, to_skip, prunable}`` mapping."""
        return {
            "to_create": [definition.id for definition in self.to_create],
            "to_skip": [definition.id for definition in self.to_skip],
            "prunable": list(self.prunable),
        }


def plan_apply(catalogue: Iterable[ProfileDefinition], existing_ids: Iterable[str]) -> ApplyPlan:
    """Split ``catalogue`` against ``existing_ids`` into create / skip / prune.

    Pure: it reads nothing, writes nothing and knows no clock.  ``catalogue`` is
    ordered as given (the caller passes
    :data:`~trading_platform.profiles.catalogue.PROFILE_CATALOGUE`, so the report
    of a run is the catalogue's own order), and ``prunable`` is sorted ascending
    because a set carries no order of its own.
    """
    definitions = tuple(catalogue)
    known = {str(identifier) for identifier in existing_ids}
    catalogue_ids = {definition.id for definition in definitions}
    return ApplyPlan(
        to_create=tuple(definition for definition in definitions if definition.id not in known),
        to_skip=tuple(definition for definition in definitions if definition.id in known),
        prunable=tuple(sorted(known - catalogue_ids)),
    )


class ProfileApiClient:
    """The profile lifecycle routes of the monitoring API, over an injectable transport.

    Parameters
    ----------
    base_url:
        Root of the monitoring API, e.g. ``http://127.0.0.1:8080``.  A trailing
        slash is accepted; a value that carries no scheme is rejected with a
        :class:`~trading_platform.core.errors.ConfigError`, because it is always a
        typo.
    token:
        Operator token sent as :data:`OPERATOR_TOKEN_HEADER` on every mutating
        call.  ``None`` (or a blank value) sends no header at all: the API then
        answers its own ``403``, which is reported like any other refusal.
    timeout:
        Seconds before one request is abandoned; ignored when ``transport`` is
        injected.
    transport:
        ``(method, url, body, headers) -> (status, body bytes)``; the default is
        :func:`_urlopen_transport`.  Tests inject a fake here, which is what keeps
        the whole apply path socket-free.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        timeout: float = 10.0,
        transport: Transport | None = None,
    ) -> None:
        self._base_url = _normalise_base_url(base_url)
        # The server strips its own token before comparing (``web.routes._normalise_token``)
        # and reads a blank value as "no token configured": mirroring that here is what
        # keeps a trailing newline of an env file from turning into a mysterious 403.
        self._token = None if token is None or not str(token).strip() else str(token).strip()
        self._timeout = float(timeout)
        self._transport: Transport = (
            partial(_urlopen_transport, timeout=self._timeout) if transport is None else transport
        )

    @property
    def base_url(self) -> str:
        """Return the normalised root URL every request is built from."""
        return self._base_url

    @property
    def token(self) -> str | None:
        """Return the operator token (``None`` when the environment carries none)."""
        return self._token

    def list_profiles(self) -> dict[str, Any]:
        """Answer ``GET /api/profiles`` as the decoded JSON object.

        Read-only, so no operator token is sent: the route is open on a trusted
        LAN, and the token only ever guards a mutation.
        """
        return self._request("GET", "/api/profiles", None, mutating=False)

    def create_profile(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Answer ``POST /api/profiles`` with ``body`` (the token is mandatory there).

        ``body`` is the nine-key mapping of
        :meth:`~trading_platform.profiles.catalogue.ProfileDefinition.to_create_body`.
        """
        encoded = json.dumps(dict(body)).encode("utf-8")
        return self._request("POST", "/api/profiles", encoded, mutating=True)

    def delete_profile(self, profile_id: str) -> dict[str, Any]:
        """Answer ``DELETE /api/profiles/{id}`` (the destructive route)."""
        path = "/api/profiles/" + urllib.parse.quote(str(profile_id), safe="")
        return self._request("DELETE", path, None, mutating=True)

    # -- internals ---------------------------------------------------------

    def _request(
        self, method: str, path: str, body: bytes | None, *, mutating: bool
    ) -> dict[str, Any]:
        """Perform one request and decode its JSON answer, loudly on any failure.

        Raises
        ------
        ProfileError
            On a non-2xx answer (the message carries the status and the server's
            own error text) or on a body that is not a JSON object.
        """
        url = self._base_url + path
        headers: dict[str, str] = {"Accept": "application/json"}
        if mutating:
            headers["Content-Type"] = "application/json"
            if self._token is not None:
                headers[OPERATOR_TOKEN_HEADER] = self._token
        status, raw = self._transport(method, url, body, headers)
        if not 200 <= int(status) < 300:
            raise ProfileError(f"{method} {url} failed with HTTP {int(status)}: {_error_text(raw)}")
        if not raw:
            return {}
        try:
            decoded: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProfileError(
                f"{method} {url} answered a body that is not JSON: {_error_text(raw)}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ProfileError(
                f"{method} {url} answered a JSON {type(decoded).__name__}, expected an object"
            )
        return decoded


def ledger_capacity(profiles_payload: Mapping[str, Any]) -> float | None:
    """Return the paper ledger's capacity in USDT, or ``None`` when it is unknown.

    The capacity is what the shared paper ledger can still fund, read from the
    **same** ``GET /api/profiles`` answer the plan is built from -- there is no
    second request and no second source of truth:

    * ``wallets.paper.total_cash + wallets.paper.positions_value`` when both are
      finite: the cash the ledger can still deploy plus the mark-to-market value
      of what it already holds;
    * otherwise ``wallets.paper.initial_balance``: a ledger that has no row yet is
      reported by its seed;
    * otherwise the paper-only ``wallet`` key of a server written before the
      per-mode ledgers existed (the very same object, so the same two rules);
    * otherwise ``None`` -- "capacity unknown", which the apply path reports
      loudly instead of inventing a number.

    A non-finite value (``NaN``, ``Infinity``, a string) is never a capacity: the
    route renders such a value as ``null`` and this function treats it as absent.
    """
    wallet = _paper_wallet(profiles_payload)
    if wallet is None:
        return None
    total_cash = _finite(wallet.get("total_cash"))
    positions_value = _finite(wallet.get("positions_value"))
    if total_cash is not None and positions_value is not None:
        return total_cash + positions_value
    return _finite(wallet.get("initial_balance"))


def live_credential_issues(
    definition: ProfileDefinition, environ: Mapping[str, str] | None = None
) -> list[str]:
    """Return the reasons ``definition`` may **not** be created live, empty when it may.

    A paper definition always passes: paper trading needs no arming and no venue
    credentials.  A live definition must satisfy both halves of the platform's own
    live path, and this function reuses them instead of restating the rules:

    * :class:`~trading_platform.realtime.risk.LiveTradingGate` -- the environment
      must carry ``TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK``;
    * :func:`~trading_platform.realtime.credentials.credentials_from_env` -- the
      per-profile pair (``TB_PROFILE_<UPPER_ID>_API_KEY``/``_API_SECRET``) or the
      global pair (``TB_LIVE_API_KEY``/``TB_LIVE_API_SECRET``) must be present.
      A half-written pair is a :class:`~trading_platform.core.errors.ProfileError`
      there, and it is reported here as a reason like any other: the answer must
      stay total, and a refusal is exactly what it produces.

    The sentences name the exact variables to set, so the operator never has to
    guess: a live profile the broker cannot authenticate would be quarantined as a
    boot failure, and creating one is therefore never an option.
    """
    if definition.mode != "live":
        return []

    profile = definition.to_profile_config()
    prefix = profile_env_prefix(definition.id)
    issues: list[str] = []
    if not LiveTradingGate(environ).allowed(profile):
        issues.append(
            f"live trading is not armed: set "
            f"{LiveTradingGate.ENV_VAR}={LiveTradingGate.REQUIRED_VALUE}"
        )
    try:
        credentials = credentials_from_env(
            definition.id, exchange=str(profile.exchange), environ=environ
        )
    except ProfileError as exc:
        issues.append(f"{exc}; complete the pair or remove it ({CREDENTIAL_HELP})")
    else:
        if credentials is None:
            issues.append(
                f"no venue credentials for live profile {definition.id!r}: set "
                f"{prefix}_API_KEY/{prefix}_API_SECRET or "
                f"{ENV_LIVE_API_KEY}/{ENV_LIVE_API_SECRET} in the engine environment "
                f"({CREDENTIAL_HELP})"
            )
    return issues


def apply_catalogue(
    client: ProfileApiClient,
    *,
    dry_run: bool = False,
    prune: bool = False,
    force: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Reconcile the catalogue with the platform behind ``client`` and report the run.

    The order is frozen and each step is observable in the returned payload:

    1. ``GET /api/profiles`` -- the existing identifiers and the shared paper
       ledger, read once (a failure here raises: a plan built on nothing would be
       a guess);
    2. the money check -- ``paper_total_initial_balance()`` against
       :func:`ledger_capacity`.  An over-commitment **aborts the whole plan**: no
       row of the catalogue is created (paper *or* live -- a partially applied
       catalogue is harder to reason about than no catalogue at all), and every
       blocked row is reported with an actionable message naming both numbers and
       the shortfall, unless ``force`` is set.  An **unknown** capacity (a platform
       that never booted has no ledger row) is logged at ``WARNING`` and the run
       proceeds: refusing on a missing number would block the very first
       provisioning of a fresh platform.  Both cases are documented here because
       they are exactly the two an operator will meet;
    3. the live rows -- each one is refused into ``refused_live`` with the reasons
       of :func:`live_credential_issues`, or kept as eligible when the gate and the
       credentials are present;
    4. the creations -- one ``POST`` per eligible row, in **catalogue order** (a
       live row the gate armed is created exactly like a paper one, and a refused
       live row never is).  A refusal of the API becomes one ``failed`` entry (the
       server's own text included) and never stops the others, so one bad row
       cannot hide the state of the remaining twenty-five;
    5. ``--prune`` -- the identifiers of :func:`plan_apply`'s ``prunable`` set,
       deleted only when ``prune`` is set.

    ``dry_run`` performs **no mutation at all**: no ``POST``, no ``DELETE``.  The
    payload keeps its exact shape and its lists then describe what a real run
    *would* do (the ``dry_run`` key says so), which is what makes the first
    command of an operator a safe dry run.

    Returns
    -------
    dict[str, Any]
        The frozen payload: ``command``, ``ok``, ``api_url``, ``dry_run``,
        ``paper_total_initial_balance``, ``ledger_capacity``, ``created``,
        ``skipped``, ``pruned``, ``failed`` and ``refused_live``.  ``ok`` is true
        exactly when neither ``failed`` nor ``refused_live`` holds an entry.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    payload = client.list_profiles()
    plan = plan_apply(PROFILE_CATALOGUE, _profile_ids(payload))
    capacity = ledger_capacity(payload)
    total = paper_total_initial_balance()

    created: list[str] = []
    failed: list[dict[str, str]] = []
    refused_live: list[dict[str, str]] = []
    pruned: list[str] = []

    pending_paper = [definition for definition in plan.to_create if definition.mode == "paper"]
    pending_live = [definition for definition in plan.to_create if definition.mode != "paper"]

    # 1. the money check: one shared ledger funds every paper order of the mode.
    money_error: str | None = None
    if pending_paper and capacity is None:
        LOGGER.warning(
            "paper ledger capacity is unknown (the platform has no ledger row yet): "
            "committing %.2f USDT of catalogue capital without a funding check",
            total,
        )
    elif pending_paper and capacity is not None and total > capacity and not force:
        money_error = _over_commit_message(total=total, capacity=capacity, count=len(pending_paper))
        LOGGER.error("%s", money_error)

    # 2. the live rows: never created while the venue cannot authenticate them.
    eligible_live: set[str] = set()
    for definition in pending_live:
        issues = live_credential_issues(definition, env)
        if issues:
            LOGGER.warning("refusing live profile %s: %s", definition.id, " ".join(issues))
            refused_live.append({"id": definition.id, "reason": " ".join(issues)})
        else:
            eligible_live.add(definition.id)

    # 3. the creations, in catalogue order, so the report reads like the table.  A
    #    money refusal aborts the whole plan -- a partially applied catalogue is
    #    harder to reason about than no catalogue at all -- and every row it blocked
    #    is reported with the reason, so nothing is ever skipped silently.
    creatable: set[str] = set()
    if money_error is None:
        creatable = set(eligible_live) | {definition.id for definition in pending_paper}
    else:
        failed.extend(
            {"id": definition.id, "error": money_error}
            for definition in plan.to_create
            if definition.mode == "paper" or definition.id in eligible_live
        )
    for definition in plan.to_create:
        if definition.id not in creatable:
            continue
        if dry_run:
            created.append(definition.id)
            continue
        _create(client, definition, created=created, failed=failed)

    # 4. the prune, last and only when explicitly asked for.
    if prune:
        for identifier in plan.prunable:
            if dry_run:
                pruned.append(identifier)
                continue
            try:
                client.delete_profile(identifier)
            except TradingBacktestError as exc:
                failed.append({"id": identifier, "error": str(exc)})
            else:
                pruned.append(identifier)

    return {
        "command": PROVISION_COMMAND,
        "ok": not failed and not refused_live,
        "api_url": client.base_url,
        "dry_run": bool(dry_run),
        "paper_total_initial_balance": total,
        "ledger_capacity": capacity,
        "created": created,
        "skipped": [definition.id for definition in plan.to_skip],
        "pruned": pruned,
        "failed": failed,
        "refused_live": refused_live,
    }


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _create(
    client: ProfileApiClient,
    definition: ProfileDefinition,
    *,
    created: list[str],
    failed: list[dict[str, str]],
) -> None:
    """Create one profile, recording the outcome in ``created`` or ``failed``.

    A refused create never stops the run: the payload reports it, the CLI exits
    ``1`` and the remaining rows still get their chance -- one malformed row must
    not hide the state of the other twenty-five.
    """
    try:
        client.create_profile(definition.to_create_body())
    except TradingBacktestError as exc:
        LOGGER.error("creating profile %s failed: %s", definition.id, exc)
        failed.append({"id": definition.id, "error": str(exc)})
    else:
        created.append(definition.id)


def _profile_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the identifiers of a ``GET /api/profiles`` payload.

    Raises
    ------
    ProfileError
        When the payload carries no ``profiles`` list: an answer that cannot be
        read is never treated as "the platform holds nothing" -- that would
        create twenty-seven duplicates.
    """
    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        raise ProfileError(
            "GET /api/profiles answered without a 'profiles' list: cannot plan an apply on it"
        )
    identifiers: list[str] = []
    for entry in profiles:
        if not isinstance(entry, Mapping):
            continue
        identifier = entry.get("profile_id") or entry.get("id")
        if isinstance(identifier, str) and identifier:
            identifiers.append(identifier)
    return tuple(identifiers)


def _paper_wallet(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the paper ledger object of a profiles payload, or ``None``.

    ``wallets.paper`` is the documented location; the paper-only ``wallet`` key of
    a server written before the per-mode ledgers existed is the fallback.  A
    ``null`` (no ledger row yet) is **not** an older server: it stays ``None``.
    """
    wallets = payload.get("wallets")
    if isinstance(wallets, Mapping):
        candidate = wallets.get("paper")
        if isinstance(candidate, Mapping):
            return candidate
    legacy = payload.get("wallet")
    return legacy if isinstance(legacy, Mapping) else None


def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _over_commit_message(*, total: float, capacity: float, count: int) -> str:
    """Render the frozen "the catalogue does not fit the ledger" sentence.

    It names what is refused (``count`` profiles), what the catalogue commits, what
    the ledger can cover and the exact shortfall, plus the two ways out: lower the
    catalogue's ``initial_balance`` values or pass ``--force``.
    """
    return (
        f"refusing to create {count} paper profile(s): the catalogue commits {total:.2f} USDT "
        f"but the shared paper ledger can only cover {capacity:.2f} USDT "
        f"(shortfall {total - capacity:.2f} USDT); lower the catalogue's initial_balance "
        "values or raise the platform ledger, or pass --force to commit anyway"
    )


def _normalise_base_url(base_url: str) -> str:
    """Return ``base_url`` without its trailing slash, rejecting an obvious typo.

    Raises
    ------
    ConfigError
        When the value is empty or carries no scheme (``127.0.0.1:8080`` is by far
        the most common mistake, and it would otherwise fail much later, inside
        the transport, with an unrelated message).
    """
    text = str(base_url).strip().rstrip("/")
    if not text:
        raise ConfigError("the monitoring API URL must not be empty")
    if "://" not in text:
        raise ConfigError(
            f"invalid monitoring API URL: {base_url!r} (expected a scheme, "
            "e.g. http://127.0.0.1:8080)"
        )
    return text


def _error_text(raw: bytes) -> str:
    """Return the server's own error text of a failed answer.

    The documented error bodies are JSON objects carrying an ``error`` string; a
    body that is not JSON (an HTML page from a proxy, an empty answer) is echoed
    as text.  The result is always a non-empty, single-line, bounded string, so a
    message never ends in a dangling colon.
    """
    if not raw:
        return "no response body"
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return "no response body"
    try:
        decoded = json.loads(text)
    except ValueError:
        pass
    else:
        if isinstance(decoded, Mapping):
            error = decoded.get("error")
            if isinstance(error, str) and error.strip():
                text = error.strip()
    text = " ".join(text.split())
    return text[:_MAX_ERROR_TEXT]


def _urlopen_transport(
    method: str,
    url: str,
    body: bytes | None,
    headers: Mapping[str, str],
    *,
    timeout: float,
) -> tuple[int, bytes]:
    """Perform one request with ``urllib.request`` (imported here, never at import time).

    An HTTP error status is **returned**, not raised: only a transport-level
    failure (DNS, refused connection, timeout) raises a
    :class:`~trading_platform.core.errors.ProfileError`, and the caller turns every
    status into the one documented message shape.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise ProfileError(f"cannot reach the monitoring API at {url}: {exc.reason}") from exc
    except OSError as exc:  # pragma: no cover - a socket failure is not deterministic
        raise ProfileError(f"cannot reach the monitoring API at {url}: {exc}") from exc
