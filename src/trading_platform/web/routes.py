"""Pure HTTP routing of the monitoring API (layer 7, work package wp9).

This module contains **no socket, no thread and no server**: :class:`Router`
maps ``(method, path)`` onto an :class:`HttpResponse` and nothing else.  The
transport (:mod:`trading_platform.web.server`) is a thin adapter on top of it,
which is what makes every route testable by calling one pure function -- the
whole JSON contract of section 4 of the delivery brief is enforced here.

JSON only
---------
Layer 7 is a **pure JSON API**: it serves no HTML document and no static asset.
``GET /`` and every ``/static/...`` path are unknown routes and answer the
documented JSON ``404`` (``{"error": "not found: <path>"}``) like any other
unknown path.  The dashboard is a separate application (``dashboard/``, a
Next.js server) that consumes this API from its own origin.

Data sources
------------
A route never reads SQLite, never imports the orchestrator and never touches the
filesystem at all:

* :class:`SnapshotProvider` supplies the *cheap* in-memory view (the 2-second
  dashboard poll): the platform snapshot, the health body, one profile snapshot,
  the kill-switch commands and the bounded candle history of one profile;
* :class:`~trading_platform.realtime.monitor.Monitor` supplies the *complete*
  persisted view of one profile (equity curve, trades, orders, positions and the
  metrics block, which delegates to ``metrics.compute_metrics``);
* :class:`CatalogProvider` supplies the four vocabularies of the dashboard
  pickers (symbols, strategies, timeframes, modes) and degrades to the static
  catalog of :func:`~trading_platform.realtime.catalog.default_catalog_body` on
  any failure -- that route never answers a ``500``;
* :class:`ProfileController` runs the lifecycle commands (pause, resume, delete,
  create) on the engine loop and reports the pause state of every profile.

The orchestrator class is deliberately **never** imported here: the CLI can
therefore serve the persisted state (``realtime serve``) without an engine, and
a test can serve a local fake.

Shared wallet (additive)
------------------------
``GET /api/health`` and ``GET /api/profiles`` carry one extra key, ``wallet``:
the platform-wide view of the **one** shared USDT wallet every profile funds its
orders from (``null`` when the provider reports none).  Nothing else changes:
every pre-existing key keeps its name and its type, and the per-profile payloads
gain the *attributed* figures (``allocation``, ``deployed``, ``realized_pnl``,
``unrealized_pnl`` and ``last_block_reason``) beside the ones they already had.

Orphaned positions (additive)
-----------------------------
``GET /api/health`` carries one more extra key, ``orphaned_positions``: the
report of the last startup safety sweep, which closes at the venue every durable
position whose profile is no longer loaded.  It is **always present** -- an
explicit ``null`` until the platform has been swept at least once -- and the very
same object is served by the dedicated ``GET /api/orphans`` route, so an operator
or a script can read the warning without parsing the health body.  A closure
never degrades the health status; a position that could **not** be closed does,
and is logged at ``ERROR`` as loudly as a success.

Profile lifecycle
-----------------
The four lifecycle routes require the operator token and are refused with the
documented ``403`` by a read-only server *and* by a server built without a
:class:`ProfileController` (``realtime serve`` attaches no controller).  Pausing
a profile closes its **entry** gate only: the open position stays managed, its
stop loss and every exit stay evaluated.  Deleting a profile closes every open
order and flattens the open position at market **before** anything is removed,
so a refused flattening leaves the profile exactly where it was.

Failure surface
---------------
:meth:`Router.handle` never raises.  A :class:`MonitoringError` (or any other
exception) raised by a data source is converted at this boundary into a
``500 {"error": "<ExceptionType>: <message>"}`` and logged; **no stack trace ever
reaches the wire**.

Honest limitation
-----------------
The transport is the standard library only (brief D2): HTTP polling, no
WebSocket, no ASGI push, and a single operator token resolved from
``TB_OPERATOR_TOKEN`` -- a trusted-LAN monitoring surface, not an
internet-facing one.  Both points are restated in ``docs/realtime.md``.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qs

from trading_platform.config.models import MonitoringConfig
from trading_platform.core.errors import ConfigError, MonitoringError, ProfileError
from trading_platform.realtime.catalog import default_catalog_body
from trading_platform.realtime.clock import Clock, SystemClock
from trading_platform.realtime.models import PlatformSnapshot, ProfileSnapshot, ProfileStatus
from trading_platform.realtime.monitor import Monitor

__all__ = [
    "CANDLE_ROUTE_DEFAULT_LIMIT",
    "CANDLE_ROUTE_MAX_LIMIT",
    "CatalogProvider",
    "HttpResponse",
    "ProfileController",
    "Router",
    "SnapshotProvider",
    "operator_token_from_env",
]

#: Logger of the whole web layer (the transport logs there as well).
LOGGER_NAME = "trading_platform.web"

#: Environment variable holding the single operator token (never logged).
OPERATOR_TOKEN_ENV = "TB_OPERATOR_TOKEN"

_LOGGER = logging.getLogger(LOGGER_NAME)

#: Header carrying the operator token on a mutating request.
OPERATOR_TOKEN_HEADER = "X-Operator-Token"

#: Content type of every JSON payload.
JSON_CONTENT_TYPE = "application/json; charset=utf-8"

#: Documented 403 body of a server that refuses every mutation.
MUTATIONS_DISABLED_ERROR = "mutations are disabled on this server"

#: Documented 403 body of a mutating request missing the operator token.
MISSING_TOKEN_ERROR = "missing or invalid operator token"

#: Documented 400 body of a malformed ``limit`` query parameter.
MALFORMED_LIMIT_ERROR = "malformed query parameter: 'limit' must be a positive integer"

#: Reason returned by ``GET /api/operator-token`` when the supplied token matches.
TOKEN_REASON_VALID = "valid operator token"

#: Reason returned when the request carries no token at all.
TOKEN_REASON_MISSING = "no operator token was supplied"

#: Reason returned when the supplied token does not match the configured one.
TOKEN_REASON_INVALID = "the supplied operator token does not match this server"

#: Reason returned when the server has no usable token configured (read-only, or
#: no token at all), so no token could ever authorise a mutation.
TOKEN_REASON_DISABLED = "no operator token is configured on this server"

#: Candles returned by ``GET /api/profiles/{id}/candles`` when ``limit`` is absent.
CANDLE_ROUTE_DEFAULT_LIMIT: int = 500

#: Hard cap of the ``limit`` query parameter of the candles route.
CANDLE_ROUTE_MAX_LIMIT: int = 1000

#: Profile identifier pattern, identical to ``config.models.ProfileConfig``.
_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

#: A strictly positive decimal integer (the accepted form of ``limit``).
_POSITIVE_INTEGER = re.compile(r"[0-9]+")

#: Leaf names of the per-profile read routes.
_PROFILE_LEAVES: frozenset[str] = frozenset(
    {"equity", "trades", "orders", "positions", "metrics", "candles"}
)

#: Leaf names of the per-profile action routes (mutating, ``POST`` only).
_PROFILE_ACTIONS: frozenset[str] = frozenset({"pause", "resume"})

#: Keys and modes the creation body accepts (any other key is refused).
_CREATE_FIELDS: frozenset[str] = frozenset(
    {"profile_id", "symbol", "timeframe", "strategy", "mode", "initial_balance", "params"}
)

#: Creation fields that must be present and carry a string.
_CREATE_STRINGS: tuple[str, ...] = ("profile_id", "symbol", "timeframe", "strategy")

#: The two documented run modes of a creation body.
_CREATE_MODES: frozenset[str] = frozenset({"paper", "live"})

#: JSON scalars accepted as the values of ``params``.
_SCALARS: tuple[type, ...] = (str, int, float, bool, type(None))

#: Methods answering a read-only route.
_READ_METHODS: tuple[str, ...] = ("GET", "HEAD")

#: Methods answering a read route whose answer depends on the request headers.
#: Such a route cannot answer a header-free ``HEAD`` honestly, so it is GET only.
_GET_ONLY_METHODS: tuple[str, ...] = ("GET",)

#: Methods answering a platform route that both reads and mutates.
_MUTATING_METHODS: tuple[str, ...] = ("GET", "HEAD", "POST")

#: Methods answering ``/api/profiles/{id}``: read, plus the destructive ``DELETE``.
_PROFILE_METHODS: tuple[str, ...] = ("GET", "HEAD", "DELETE")

#: Methods answering a per-profile action route (``pause``, ``resume``).
_ACTION_METHODS: tuple[str, ...] = ("POST",)


# ---------------------------------------------------------------------------
# the snapshot seam (D4) -- defined here, re-exported by web.server
# ---------------------------------------------------------------------------


class SnapshotProvider(Protocol):
    """Everything the web layer is allowed to know about the running platform.

    The protocol is defined in this module (and re-exported by
    :mod:`trading_platform.web.server`) so that the router can be typed without
    importing the server, which would be an import cycle.  The orchestrator
    satisfies it structurally; so does the read-only adapter the CLI builds over
    the persisted state.
    """

    def snapshot(self) -> PlatformSnapshot:
        """Return the whole platform as of now (cheap, in-memory)."""
        ...

    def health(self) -> dict[str, Any]:
        """Return the health body of the orchestrator."""
        ...

    def profile_snapshot(self, profile_id: str) -> ProfileSnapshot | None:
        """Return the snapshot of one profile, or ``None`` when unknown."""
        ...

    def orphan_report(self) -> Mapping[str, Any] | None:
        """Return the last orphan-position sweep report, or ``None``.

        ``None`` means the platform was **never swept**: the router renders that
        as an explicit JSON ``null``, never as a missing key, so a consumer can
        always tell "swept, nothing found" from "never swept".  A provider that
        cannot answer at all is tolerated through ``getattr``.
        """
        ...

    def engage_kill_switch(self, reason: str) -> Any:
        """Engage the global kill switch and return its new state."""
        ...

    def release_kill_switch(self) -> Any:
        """Release the global kill switch and return its new state."""
        ...

    def kill_switch_state(self) -> Any:
        """Return the effective kill-switch state."""
        ...

    def candle_series(self, profile_id: str, limit: int) -> Sequence[Any]:
        """Return the persisted candles of one profile, oldest first.

        Each row is read **duck-typed** (``timestamp``, ``open``, ``high``,
        ``low``, ``close``, ``volume``, ``closed`` and ``profile_id``), so the
        orchestrator answers from the running engine and the read-only adapter of
        ``realtime serve`` answers from the store, without this layer importing
        either.
        """
        ...


class CatalogProvider(Protocol):
    """The four vocabularies of the dashboard pickers, as one mapping.

    The only implementation the platform ships is
    :class:`~trading_platform.realtime.catalog.MarketCatalog`, which is
    TTL-cached, offline by default and never raises.  The router does not rely on
    that: it treats any exception (or any non-mapping answer) as "no catalog" and
    falls back to
    :func:`~trading_platform.realtime.catalog.default_catalog_body`, so
    ``GET /api/catalog`` can never answer a ``500``.
    """

    def catalog(self) -> Mapping[str, Any]:
        """Return ``{symbols, strategies, timeframes, modes}``."""
        ...


class ProfileController(Protocol):
    """Runtime control of the running engine, callable from a worker thread.

    The monitoring server answers every request in its own thread while the
    orchestrator is asynchronous: the controller is the **injected bridge**
    between the two (``realtime.control.RuntimeProfileController`` marshals every
    command onto the engine loop), which is what keeps this module free of
    ``asyncio`` and of any import of the engine.

    An **absent** controller (``realtime serve``, or a server built without one)
    makes every lifecycle route answer the documented ``403``: the web layer
    never guesses, and a test never needs an engine.
    """

    def pause_profile(self, profile_id: str) -> Any:
        """Close the entry gate of one profile and return its snapshot."""
        ...

    def resume_profile(self, profile_id: str) -> Any:
        """Open the entry gate of one profile again and return its snapshot."""
        ...

    def delete_profile(self, profile_id: str) -> Any:
        """Flatten one profile, remove it and return the removed identifier."""
        ...

    def create_profile(self, payload: Mapping[str, Any]) -> Any:
        """Validate one create request, start the profile and return its snapshot."""
        ...

    def control_state(self) -> Mapping[str, Any]:
        """Return ``{"profiles": [{profile_id, paused, running}, ...]}``."""
        ...


# ---------------------------------------------------------------------------
# the response object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    """One fully built HTTP response, transport-agnostic."""

    status: int
    body: bytes
    content_type: str = JSON_CONTENT_TYPE
    headers: tuple[tuple[str, str], ...] = ()


# ---------------------------------------------------------------------------
# small conversion helpers (no pandas, no datetime import: duck typing only)
# ---------------------------------------------------------------------------


def _normalise_token(token: str | None) -> str | None:
    """Return ``token`` stripped, or ``None`` when it is empty or missing."""
    if token is None:
        return None
    stripped = token.strip()
    return stripped or None


def operator_token_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the operator token of ``environ`` (defaults to ``os.environ``).

    An empty or whitespace-only value is normalised to ``None``, which the router
    reads as "no token configured" -- and therefore as "every mutation is
    refused".  The token is **never** logged, echoed in a payload or included in
    an error message.
    """
    source: Mapping[str, str] = _environ() if environ is None else environ
    return _normalise_token(source.get(OPERATOR_TOKEN_ENV))


def _environ() -> Mapping[str, str]:
    """Return ``os.environ`` (imported lazily to keep the module import small)."""
    import os

    return os.environ


def _iso(value: Any) -> str | None:
    """Render ``value`` as an ISO-8601 string without importing datetime/pandas.

    ``None`` stays ``None``; anything exposing ``isoformat()`` (``datetime``,
    ``pandas.Timestamp``) is rendered by it, anything else by ``str()``.
    """
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None``.

    ``None``, ``NaN`` and both infinities collapse to ``None``: no payload built
    here can carry a number ``json.dumps`` would refuse (the contract forbids
    ``NaN``/``Inf`` on the wire).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> Any:
    """Recursively rewrite ``value`` into something ``json.dumps`` always accepts.

    Mappings, sequences and scalars pass through unchanged whenever they are
    already JSON-safe; non-finite floats become ``None`` and an unknown object is
    rendered with ``str()``.  This is the belt-and-braces companion of
    :func:`_finite`: the wire can neither carry ``NaN``/``Infinity`` nor a raw
    object.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _finite(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def _state_field(state: Any, name: str, default: Any) -> Any:
    """Read ``name`` from a kill-switch state (a mapping, an object or a bool).

    The provider may hand back the ``KillSwitchState`` dataclass of
    ``realtime.risk``, its ``to_dict()`` payload, or a plain boolean; the router
    accepts all three without importing the risk layer.
    """
    if isinstance(state, bool):
        return state if name == "engaged" else default
    if isinstance(state, Mapping):
        return state.get(name, default)
    return getattr(state, name, default)


def _header_value(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Return the header ``name`` of ``headers``, case-insensitively."""
    if not headers:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _snapshot_payload(value: Any) -> Any:
    """Render the object a lifecycle command returned as its JSON mapping.

    A :class:`~trading_platform.realtime.models.ProfileSnapshot` (or any object
    carrying ``to_dict()``) is rendered by it, a mapping passes through, and
    anything else goes through the wire safety net.
    """
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return _json_safe(value)


def _positive_number(value: Any) -> float | None:
    """Return ``value`` as a strictly positive finite float, or ``None``.

    Booleans are refused (``True`` is not a balance) and so are strings, ``NaN``
    and both infinities -- ``json.loads`` accepts those three literals, so the
    check belongs here as much as in :func:`_finite`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _is_scalar_mapping(value: Any) -> bool:
    """Whether ``value`` is a mapping whose values are all JSON scalars."""
    return isinstance(value, Mapping) and all(isinstance(item, _SCALARS) for item in value.values())


def _not_found(path: str) -> HttpResponse:
    """Build the documented 404 payload of an unknown route."""
    return _json_response(404, {"error": f"not found: {path}"})


def _without_body(answer: HttpResponse) -> HttpResponse:
    """Return ``answer`` with an empty body (what ``HEAD`` always carries)."""
    return HttpResponse(
        status=answer.status,
        body=b"",
        content_type=answer.content_type,
        headers=answer.headers,
    )


def _json_response(
    status: int, payload: Any, headers: tuple[tuple[str, str], ...] = ()
) -> HttpResponse:
    """Encode ``payload`` as a JSON response body (finite numbers only)."""
    text = json.dumps(_json_safe(payload), allow_nan=False, sort_keys=False)
    return HttpResponse(status=status, body=text.encode("utf-8"), headers=headers)


# ---------------------------------------------------------------------------
# the router
# ---------------------------------------------------------------------------


class Router:
    """Pure request -> response mapping of the monitoring API.

    Parameters
    ----------
    provider:
        The snapshot seam (see :class:`SnapshotProvider`).  It is the only
        source of the platform-level data.
    monitor:
        The persisted read model of the realtime layer.  It is the only source of
        the per-profile detail payloads.
    config:
        Monitoring settings; ``config.max_request_bytes`` bounds a request body
        and ``config.host``/``config.port`` are used by the server factory.
    read_only:
        When ``True`` (the default, and the mode of ``realtime serve``), every
        mutating route answers ``403``.
    operator_token:
        The single operator token required by ``POST /api/kill-switch`` and by
        every lifecycle route.  Empty or ``None`` means "no token configured",
        which also refuses mutations.
    version:
        Version string rendered by ``GET /api/health``.
    clock:
        Time seam (D4) used for ``checked_at`` and for the ``changed_at``
        fallback of the kill switch.  Tests inject a ``ManualClock``.
    controller:
        The lifecycle seam (see :class:`ProfileController`).  ``None`` -- the
        default, and the mode of ``realtime serve`` -- keeps the read routes
        working and refuses every lifecycle route with the documented ``403``.
    catalog:
        The catalog seam (see :class:`CatalogProvider`).  ``None`` makes
        ``GET /api/catalog`` answer the static fallback catalog.
    """

    def __init__(
        self,
        provider: SnapshotProvider,
        *,
        monitor: Monitor,
        config: MonitoringConfig,
        read_only: bool = True,
        operator_token: str | None = None,
        version: str = "",
        clock: Clock | None = None,
        controller: ProfileController | None = None,
        catalog: CatalogProvider | None = None,
    ) -> None:
        self._provider = provider
        self._monitor = monitor
        self._config = config
        self._read_only = bool(read_only)
        self._operator_token = _normalise_token(operator_token)
        self._version = str(version)
        self._clock: Clock = SystemClock() if clock is None else clock
        self._controller = controller
        self._catalog = catalog

    # -- introspection ------------------------------------------------------

    @property
    def read_only(self) -> bool:
        """Whether the mutating routes of this router are refused."""
        return self._read_only

    @property
    def config(self) -> MonitoringConfig:
        """The monitoring configuration this router was built with."""
        return self._config

    def allowed_methods(self, path: str) -> tuple[str, ...]:
        """Return the methods answered by ``path``.

        ``('GET', 'HEAD')`` for a read route, ``('GET', 'HEAD', 'POST')`` for
        ``/api/profiles`` and ``/api/kill-switch``, ``('GET', 'HEAD', 'DELETE')``
        for ``/api/profiles/{id}``, ``('POST',)`` for ``.../pause`` and
        ``.../resume``, and ``()`` for an unknown path (a 404 carries no
        ``Allow`` header).
        """
        route, _argument = self._route(path)
        return self._methods_of(route)

    @staticmethod
    def _methods_of(route: str) -> tuple[str, ...]:
        """Return the methods of a resolved route name."""
        if route == "unknown":
            return ()
        if route in {"kill_switch", "profiles"}:
            return _MUTATING_METHODS
        if route == "profile":
            return _PROFILE_METHODS
        if route in _PROFILE_ACTIONS:
            return _ACTION_METHODS
        if route == "operator_token":
            # GET only: the answer depends on the request's token header, which a
            # header-free HEAD could not supply without lying (see `_get_answer`).
            return _GET_ONLY_METHODS
        return _READ_METHODS

    # -- entry point --------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Map one request onto one response.  Never raises.

        ``query`` is the raw query string of the request.  Only
        ``GET /api/profiles/{id}/candles`` reads it (its documented ``limit``
        parameter); every other route ignores it and is answered normally.
        """
        verb = (method or "").upper()
        try:
            return self._dispatch(verb, path, query=query, body=body, headers=headers)
        except MonitoringError as exc:
            _LOGGER.warning(
                "monitoring route failed: %s %s: %s: %s",
                verb,
                path,
                type(exc).__name__,
                exc,
            )
            return _json_response(500, {"error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:  # the transport boundary converts everything
            _LOGGER.exception("monitoring route crashed: %s %s", verb, path)
            return _json_response(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- dispatch -----------------------------------------------------------

    def _dispatch(
        self,
        verb: str,
        path: str,
        *,
        query: str,
        body: bytes,
        headers: Mapping[str, str] | None,
    ) -> HttpResponse:
        route, argument = self._route(path)
        allowed = self._methods_of(route)
        if route == "unknown":
            # An unknown path is a 404 for every verb -- ``HEAD`` included (with
            # the empty body ``HEAD`` always carries).
            answer = _not_found(path)
            return _without_body(answer) if verb == "HEAD" else answer
        if verb == "HEAD" and "HEAD" in allowed:
            # ``HEAD`` answers exactly like ``GET`` with an empty body -- but only
            # on a route that advertises it: ``HEAD`` on ``.../pause`` (a POST-only
            # route) is the documented 405, exactly like any other verb it does
            # not answer.
            return _without_body(self._get_answer(route, argument, query))
        if verb not in allowed:
            return _json_response(
                405,
                {"error": "method not allowed"},
                headers=(("Allow", ", ".join(allowed)),),
            )
        if route == "kill_switch":
            if verb == "POST":
                return self._kill_switch(body, headers)
            return self._kill_switch_read()
        if route == "pause":
            return self._lifecycle(
                argument,
                headers,
                lambda controller: controller.pause_profile(argument),
                paused=True,
            )
        if route == "resume":
            return self._lifecycle(
                argument,
                headers,
                lambda controller: controller.resume_profile(argument),
                paused=False,
            )
        if route == "profile" and verb == "DELETE":
            return self._delete(argument, headers)
        if route == "profiles" and verb == "POST":
            return self._create(body, headers)
        if route == "catalog":
            return self._catalog_response()
        if route == "control":
            return self._control_response()
        if route == "orphans":
            return self._orphans_response()
        if route == "operator_token":
            return self._operator_token_response(headers)
        return self._read(route, argument, query)

    def _get_answer(self, route: str, argument: str, query: str = "") -> HttpResponse:
        """Build the response ``GET`` would answer for ``route`` (never a 405).

        Only called for a **known** route (an unknown path is answered before),
        and only for a route advertising ``HEAD``.
        """
        if route == "kill_switch":
            return self._kill_switch_read()
        if route == "catalog":
            return self._catalog_response()
        if route == "control":
            return self._control_response()
        if route == "orphans":
            return self._orphans_response()
        if route == "operator_token":
            # A HEAD carries no request headers to verify in this path (the
            # helper is header-free by design), and answering "invalid" for a
            # HEAD would be a lie about the caller's token. The route therefore
            # advertises GET only (see `_methods_of`), and reaching here is a
            # 405 -- never a 404, which would claim the route does not exist.
            return _json_response(
                405,
                {"error": "method not allowed"},
                headers=(("Allow", "GET"),),
            )
        return self._read(route, argument, query)

    def _read(self, route: str, argument: str, query: str = "") -> HttpResponse:
        """Answer one read route (``route`` is never ``kill_switch``)."""
        if route == "health":
            return _json_response(200, self._health_payload())
        if route == "profiles":
            snapshot = self._provider.snapshot()
            # The shared wallet view is **additive**: a snapshot carrying no wallet
            # (a provider whose store holds no wallet row yet) -- or a provider
            # whose snapshot predates the wallet and carries no ``wallet`` attribute
            # at all -- is rendered as an explicit ``null`` instead of a missing key.
            wallet = getattr(snapshot, "wallet", None)
            return _json_response(
                200,
                {
                    "profiles": [profile.to_dict() for profile in snapshot.profiles],
                    "generated_at": _iso(snapshot.generated_at),
                    "wallet": None if wallet is None else _snapshot_payload(wallet),
                },
            )
        if route == "profile":
            known = self._provider.profile_snapshot(argument)
            if known is None:
                return self._unknown_profile(argument)
            return _json_response(200, known.to_dict())
        if self._provider.profile_snapshot(argument) is None:
            return self._unknown_profile(argument)
        return self._profile_detail(route, argument, query)

    def _profile_detail(self, route: str, profile_id: str, query: str = "") -> HttpResponse:
        """Answer a per-profile detail route of an existing profile."""
        if route == "candles":
            limit = self._parse_candle_limit(query)
            if isinstance(limit, HttpResponse):
                return limit
            candles = [
                self._candle_payload(row) for row in self._provider.candle_series(profile_id, limit)
            ]
            return _json_response(200, {"candles": candles, "count": len(candles)})
        if route == "equity":
            points = [
                {
                    "timestamp": _iso(point.timestamp),
                    "equity": _finite(point.equity),
                    "cash": _finite(point.cash),
                    "position_value": _finite(point.position_value),
                }
                for point in self._monitor.equity(profile_id)
            ]
            return _json_response(200, {"points": points})
        if route == "trades":
            trades = [trade.to_dict() for trade in self._monitor.trades(profile_id)]
            return _json_response(200, {"trades": trades, "count": len(trades)})
        if route == "orders":
            orders = [order.to_dict() for order in self._monitor.orders(profile_id)]
            return _json_response(200, {"orders": orders})
        if route == "positions":
            positions = [item.to_dict() for item in self._monitor.positions(profile_id)]
            return _json_response(200, {"positions": positions})
        return _json_response(
            200,
            {
                "metrics": dict(self._monitor.metrics(profile_id)),
                "benchmark": self._monitor.benchmark(profile_id),
                "generated_at": _iso(self._clock.now()),
            },
        )

    def _unknown_profile(self, profile_id: str) -> HttpResponse:
        """Build the documented 404 payload of an unknown profile."""
        return _json_response(404, {"error": f"unknown profile: {profile_id!r}"})

    # -- candle history -----------------------------------------------------

    @staticmethod
    def _candle_payload(row: Any) -> dict[str, Any]:
        """Render one persisted candle, duck-typed, onto its documented keys.

        ``open``/``high``/``low``/``close``/``volume`` go through :func:`_finite`,
        so a non-finite value is written as ``null`` instead of a ``NaN`` the JSON
        contract forbids; ``timestamp`` is rendered ISO-8601.
        """
        return {
            "profile_id": str(row.profile_id),
            "timestamp": _iso(row.timestamp),
            "open": _finite(row.open),
            "high": _finite(row.high),
            "low": _finite(row.low),
            "close": _finite(row.close),
            "volume": _finite(row.volume),
            "closed": bool(row.closed),
        }

    @staticmethod
    def _parse_candle_limit(query: str) -> int | HttpResponse:
        """Decode the documented ``limit`` parameter of the candles route.

        Absent means :data:`CANDLE_ROUTE_DEFAULT_LIMIT`; a strictly positive
        integer is honoured and clamped to :data:`CANDLE_ROUTE_MAX_LIMIT`; an
        empty, non-numeric, zero or negative value is the documented ``400``.
        Every other query parameter is ignored.
        """
        values = parse_qs(query or "", keep_blank_values=True).get("limit")
        if not values:
            return CANDLE_ROUTE_DEFAULT_LIMIT
        raw = values[0]
        if _POSITIVE_INTEGER.fullmatch(raw) is None:
            return _json_response(400, {"error": MALFORMED_LIMIT_ERROR})
        requested = int(raw)
        if requested <= 0:
            return _json_response(400, {"error": MALFORMED_LIMIT_ERROR})
        return min(requested, CANDLE_ROUTE_MAX_LIMIT)

    # -- catalog and runtime control ----------------------------------------

    def _catalog_response(self) -> HttpResponse:
        """Answer ``GET /api/catalog``.  This route **never** answers a 500.

        The injected catalog is asked first; an absent, raising or malformed one
        degrades to :func:`~trading_platform.realtime.catalog.default_catalog_body`
        -- the offline static table plus the registry's own vocabularies -- so the
        pickers of the dashboard always have something to render.
        """
        body: Mapping[str, Any] | None = None
        if self._catalog is not None:
            try:
                candidate = self._catalog.catalog()
            except Exception as exc:  # a venue, a cache or a plugin failure: degrade
                _LOGGER.warning("market catalog is unavailable (%s): using the static catalog", exc)
            else:
                if isinstance(candidate, Mapping):
                    body = candidate
                else:
                    _LOGGER.warning(
                        "market catalog returned %s: using the static catalog", type(candidate)
                    )
        if body is None:
            body = default_catalog_body()
        return _json_response(200, dict(body))

    def _control_response(self) -> HttpResponse:
        """Answer ``GET /api/control`` with the runtime state of the platform.

        ``engine_running`` says whether a lifecycle seam is attached at all,
        ``read_only`` mirrors the server mode and ``mutable`` is the conjunction
        the dashboard reads before enabling its controls.  A failing controller
        never turns this read into a ``500``: the profile list is then empty.
        """
        profiles: list[Any] = []
        if self._controller is not None:
            try:
                state = self._controller.control_state()
            except Exception as exc:
                _LOGGER.warning("profile control state is unavailable (%s): reporting none", exc)
            else:
                entries = state.get("profiles") if isinstance(state, Mapping) else None
                if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
                    profiles = list(entries)
        return _json_response(
            200,
            {
                "engine_running": self._controller is not None,
                "read_only": self._read_only,
                "mutable": bool(
                    not self._read_only and self._controller is not None and self._operator_token
                ),
                "profiles": _json_safe(profiles),
            },
        )

    # -- profile lifecycle --------------------------------------------------

    def _forbidden(self, message: str = "") -> HttpResponse:
        """Build the documented ``403`` body of a refused mutation."""
        return _json_response(403, {"error": message or self._refusal_message()})

    def _refusal_message(self) -> str:
        """Return the documented 403 message of a refused mutation."""
        if self._read_only or self._operator_token is None:
            return MUTATIONS_DISABLED_ERROR
        return MISSING_TOKEN_ERROR

    def _lifecycle_seam(
        self, headers: Mapping[str, str] | None
    ) -> ProfileController | HttpResponse:
        """Return the attached controller, or the documented 403 of the command.

        The token is checked first (the exact same bodies as ``POST
        /api/kill-switch``); an **absent controller** then refuses the command
        like a read-only server does, so ``realtime serve`` answers the exact
        same way whether or not it was given a token.
        """
        if not self._authorised(headers):
            return self._forbidden()
        if self._controller is None:
            return self._forbidden(MUTATIONS_DISABLED_ERROR)
        return self._controller

    def _lifecycle(
        self,
        profile_id: str,
        headers: Mapping[str, str] | None,
        command: Callable[[ProfileController], Any],
        *,
        paused: bool,
    ) -> HttpResponse:
        """Run one pause/resume command and answer ``{profile, paused}``."""
        seam = self._lifecycle_seam(headers)
        if isinstance(seam, HttpResponse):
            return seam
        if self._provider.profile_snapshot(profile_id) is None:
            return self._unknown_profile(profile_id)
        try:
            profile = command(seam)
        except (MonitoringError, ProfileError, ConfigError) as exc:
            return self._failed_mutation(exc)
        return _json_response(200, {"profile": _snapshot_payload(profile), "paused": paused})

    def _delete(self, profile_id: str, headers: Mapping[str, str] | None) -> HttpResponse:
        """Answer ``DELETE /api/profiles/{id}``: flatten, then remove."""
        seam = self._lifecycle_seam(headers)
        if isinstance(seam, HttpResponse):
            return seam
        if self._provider.profile_snapshot(profile_id) is None:
            return self._unknown_profile(profile_id)
        try:
            removed = seam.delete_profile(profile_id)
        except (MonitoringError, ProfileError, ConfigError) as exc:
            return self._failed_mutation(exc)
        identifier = removed if isinstance(removed, str) and removed else profile_id
        return _json_response(200, {"profile_id": identifier, "deleted": True})

    def _create(self, body: bytes, headers: Mapping[str, str] | None) -> HttpResponse:
        """Answer ``POST /api/profiles``: validate the body, then start it."""
        seam = self._lifecycle_seam(headers)
        if isinstance(seam, HttpResponse):
            return seam
        payload = self._parse_create_body(body)
        if isinstance(payload, HttpResponse):
            return payload
        try:
            profile = seam.create_profile(payload)
        except (MonitoringError, ProfileError, ConfigError) as exc:
            return self._failed_mutation(exc)
        return _json_response(201, {"profile": _snapshot_payload(profile)})

    @staticmethod
    def _failed_mutation(exc: Exception) -> HttpResponse:
        """Map a lifecycle failure onto its documented status code.

        ``MonitoringError`` (no engine, a timeout) is a ``503``, ``ProfileError``
        (a duplicate, a profile that is not running) a ``409`` and ``ConfigError``
        (an unknown strategy, an unsupported timeframe) a ``400``.  Any other
        exception keeps the existing ``500`` boundary of :meth:`handle`.
        """
        _LOGGER.warning("profile lifecycle command failed: %s: %s", type(exc).__name__, exc)
        if isinstance(exc, MonitoringError):
            return _json_response(503, {"error": str(exc)})
        if isinstance(exc, ProfileError):
            return _json_response(409, {"error": str(exc)})
        return _json_response(400, {"error": str(exc)})

    @staticmethod
    def _parse_create_body(body: bytes) -> dict[str, Any] | HttpResponse:
        """Decode and validate the body of ``POST /api/profiles``.

        The validation happens **before** the controller is called, so a rejected
        request never reaches the engine loop and every message is exactly the one
        the contract documents.
        """
        try:
            decoded = json.loads(body.decode("utf-8") if body else "null")
        except (UnicodeDecodeError, ValueError):
            return _json_response(400, {"error": "malformed request body: not valid JSON"})
        if not isinstance(decoded, dict):
            return _json_response(400, {"error": "malformed request body: expected a JSON object"})
        for key in decoded:
            if key not in _CREATE_FIELDS:
                return _json_response(
                    400, {"error": f"malformed request body: unexpected field {key!r}"}
                )
        for name in _CREATE_STRINGS:
            if not isinstance(decoded.get(name), str):
                return _json_response(
                    400, {"error": f"malformed request body: {name!r} must be a string"}
                )
        if decoded.get("mode") not in _CREATE_MODES:
            return _json_response(
                400, {"error": "malformed request body: 'mode' must be 'paper' or 'live'"}
            )
        payload: dict[str, Any] = {name: decoded[name] for name in _CREATE_STRINGS}
        payload["mode"] = decoded["mode"]
        if "initial_balance" in decoded:
            balance = _positive_number(decoded["initial_balance"])
            if balance is None:
                return _json_response(
                    400,
                    {
                        "error": "malformed request body: 'initial_balance' must be a positive number"
                    },
                )
            payload["initial_balance"] = balance
        if "params" in decoded:
            params = decoded["params"]
            if not _is_scalar_mapping(params):
                return _json_response(
                    400, {"error": "malformed request body: 'params' must be an object"}
                )
            payload["params"] = dict(params)
        return payload

    # -- health -------------------------------------------------------------

    def _health_payload(self) -> dict[str, Any]:
        """Build the ``GET /api/health`` body from the provider and the clock.

        The orchestrator's own ``health()`` body wins for the counters it
        provides; a missing counter falls back to the platform snapshot (profile
        counts, uptime), and ``version``/``checked_at`` come from the router
        configuration and the clock seam.  ``status`` is exactly ``'ok'`` or
        ``'degraded'`` -- degraded as soon as the kill switch is engaged.

        ``wallet`` is the shared platform wallet the reporting layer published
        (the very same object the orchestrator's ``health()`` body carries, and the
        persisted ledger for the read-only surface of ``realtime serve``).  A
        provider that reports no wallet answers ``null``: the key is always
        present, so a consumer never has to guess whether the wallet is missing
        or simply empty.

        ``orphaned_positions`` is the report of the last startup orphan sweep
        (:meth:`_orphan_payload`): ``null`` until the platform has been swept at
        least once, again always present.  A **closure** never degrades the status
        -- the sweep did its job -- but a position that could not be closed does,
        and is logged at ``ERROR`` so a failure is reported as loudly as a success.
        """
        raw = self._provider.health()
        reported: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        kill_switch = self._kill_switch_engaged()
        orphans = self._orphan_payload()
        failed = int(orphans.get("failed_count", 0)) if orphans is not None else 0

        profiles_total = reported.get("profiles_total")
        profiles_running = reported.get("profiles_running")
        uptime = reported.get("uptime_seconds")
        if profiles_total is None or profiles_running is None or uptime is None:
            snapshot = self._provider.snapshot()
            if profiles_total is None:
                profiles_total = len(snapshot.profiles)
            if profiles_running is None:
                profiles_running = sum(
                    1 for profile in snapshot.profiles if profile.status is ProfileStatus.RUNNING
                )
            if uptime is None:
                uptime = snapshot.uptime_seconds

        if failed:
            _LOGGER.error(
                "orphan_positions_failed: %d orphaned position(s) could not be closed",
                failed,
            )

        return {
            "status": "degraded" if kill_switch or failed else "ok",
            "version": self._version,
            "uptime_seconds": _finite(uptime),
            "profiles_total": int(profiles_total),
            "profiles_running": int(profiles_running),
            "kill_switch": kill_switch,
            "checked_at": _iso(self._clock.now()),
            "wallet": _snapshot_payload(reported.get("wallet")),
            "orphaned_positions": orphans,
        }

    # -- orphaned positions -------------------------------------------------

    def _orphan_payload(self) -> dict[str, Any] | None:
        """Return the last orphan sweep as the documented payload, or ``None``.

        The provider is asked through ``getattr`` so a local test fake written
        before this route existed keeps working: a provider that cannot answer is
        simply "never swept", which the API renders as an explicit ``null``.

        The reader is accepted **either as a method or as an attribute**: the two
        shipped providers of the platform disagree on that detail (the CLI surface
        declares ``orphan_report()``, the orchestrator publishes the report as a
        property), and the shape of the payload -- not the calling convention --
        is what this contract pins.  A callable attribute is called, anything else
        is read as the value.
        """
        reader = getattr(self._provider, "orphan_report", None)
        if reader is None:
            return None
        try:
            report = reader() if callable(reader) else reader
        except MonitoringError:
            raise
        except Exception as exc:  # a broken report must never break the health route
            _LOGGER.warning("orphan report unavailable: %s", exc)
            return None
        if report is None:
            return None
        payload = report if isinstance(report, Mapping) else _snapshot_payload(report)
        if not isinstance(payload, Mapping):
            return None
        return {
            "found": int(payload.get("found", 0)),
            "orphaned": int(payload.get("orphaned", 0)),
            "closed_count": int(payload.get("closed_count", 0)),
            "failed_count": int(payload.get("failed_count", 0)),
            "closed": [_json_safe(item) for item in payload.get("closed") or ()],
            "failed": [_json_safe(item) for item in payload.get("failed") or ()],
            "swept_at": _iso(payload.get("swept_at")),
        }

    def _orphans_response(self) -> HttpResponse:
        """Answer ``GET /api/orphans`` with the last sweep, plus its status.

        The route exists so an operator (or a script) reads the warning without
        parsing the health body; the object it returns is **exactly** the
        ``orphaned_positions`` value of ``/api/health``, so the two can never
        disagree.
        """
        orphans = self._orphan_payload()
        status = (
            "degraded" if orphans is not None and int(orphans.get("failed_count", 0)) > 0 else "ok"
        )
        body: dict[str, Any] = dict(orphans) if orphans is not None else {}
        if orphans is None:
            body = {
                "found": 0,
                "orphaned": 0,
                "closed_count": 0,
                "failed_count": 0,
                "closed": [],
                "failed": [],
                "swept_at": None,
            }
        body["status"] = status
        return _json_response(200, body)

    # -- kill switch --------------------------------------------------------

    def _kill_switch_engaged(self) -> bool:
        """Return whether any source currently forces the kill switch."""
        return bool(_state_field(self._provider.kill_switch_state(), "engaged", False))

    def _kill_switch_read(self) -> HttpResponse:
        """Answer ``GET /api/kill-switch`` with the current, effective state.

        The route is the read counterpart of the mutating one; it exists because
        :meth:`allowed_methods` advertises ``GET`` (and ``HEAD``) on that exact
        path, and it never needs the operator token.
        """
        state = self._provider.kill_switch_state()
        return _json_response(
            200,
            {
                "kill_switch": bool(_state_field(state, "engaged", False)),
                "reason": str(_state_field(state, "reason", "") or ""),
                "changed_at": _iso(_state_field(state, "changed_at", None)),
            },
        )

    def _kill_switch(self, body: bytes, headers: Mapping[str, str] | None) -> HttpResponse:
        """Answer ``POST /api/kill-switch`` (auth, then body, then the provider)."""
        if not self._authorised(headers):
            return self._forbidden()
        payload = self._parse_kill_switch_body(body)
        if isinstance(payload, HttpResponse):
            return payload
        engage = bool(payload["engage"])
        reason = str(payload["reason"])
        if engage:
            state = self._provider.engage_kill_switch(reason)
        else:
            state = self._provider.release_kill_switch()
        changed_at = _iso(_state_field(state, "changed_at", None)) or _iso(self._clock.now())
        return _json_response(
            200,
            {
                "kill_switch": bool(_state_field(state, "engaged", engage)),
                "reason": str(_state_field(state, "reason", reason) or ""),
                "changed_at": changed_at,
            },
        )

    def _authorised(self, headers: Mapping[str, str] | None) -> bool:
        """Whether this mutating request carries the configured operator token."""
        if self._read_only or self._operator_token is None:
            return False
        supplied = _header_value(headers, OPERATOR_TOKEN_HEADER)
        if supplied is None:
            return False
        return hmac.compare_digest(supplied, self._operator_token)

    def _operator_token_response(self, headers: Mapping[str, str] | None) -> HttpResponse:
        """Answer ``GET /api/operator-token``: does this token actually work?

        The operator otherwise learns that a token is wrong only by attempting a
        mutation and reading a 403 whose text ("missing or invalid operator
        token") does not say WHICH of the two it was.  This route answers that
        question directly, so the dashboard can validate what the operator
        pasted before relying on it.

        It is a **read**: it changes no state, which is why it is answered on
        ``GET`` without the mutating-request treatment, and it is deliberately
        *not* a 403 -- a wrong token is a successful answer to the question
        "is this token valid?", so it is reported as ``200`` with
        ``valid: false`` and a machine-readable ``reason``.  That keeps the
        distinction the operator needs, between a wrong token and a server that
        disables mutations altogether.

        Nothing about the configured token is ever echoed: the answer is a
        boolean plus one of four fixed reason strings, and the comparison stays
        constant-time (:func:`hmac.compare_digest`), exactly like
        :meth:`_authorised`.
        """
        if self._read_only or self._operator_token is None:
            return _json_response(
                200,
                {
                    "valid": False,
                    "reason": TOKEN_REASON_DISABLED,
                    "read_only": bool(self._read_only),
                },
            )
        supplied = _header_value(headers, OPERATOR_TOKEN_HEADER)
        if supplied is None:
            return _json_response(200, {"valid": False, "reason": TOKEN_REASON_MISSING})
        if hmac.compare_digest(supplied, self._operator_token):
            return _json_response(200, {"valid": True, "reason": TOKEN_REASON_VALID})
        return _json_response(200, {"valid": False, "reason": TOKEN_REASON_INVALID})

    @staticmethod
    def _parse_kill_switch_body(body: bytes) -> dict[str, Any] | HttpResponse:
        """Decode the kill-switch body, or return the documented 400 response.

        The body must be a JSON object carrying a **boolean** ``engage``; a
        missing ``reason`` defaults to the empty string, a non-string ``reason``
        is refused.
        """
        try:
            decoded = json.loads(body.decode("utf-8") if body else "null")
        except (UnicodeDecodeError, ValueError):
            return _json_response(400, {"error": "malformed request body: not valid JSON"})
        if not isinstance(decoded, dict):
            return _json_response(400, {"error": "malformed request body: expected a JSON object"})
        if not isinstance(decoded.get("engage"), bool):
            return _json_response(
                400, {"error": "malformed request body: 'engage' must be a boolean"}
            )
        reason = decoded.get("reason", "")
        if not isinstance(reason, str):
            return _json_response(
                400, {"error": "malformed request body: 'reason' must be a string"}
            )
        return {"engage": decoded["engage"], "reason": reason}

    # -- routing table ------------------------------------------------------

    @staticmethod
    def _route(path: str) -> tuple[str, str]:
        """Resolve ``path`` onto ``(route_name, argument)``.

        An unknown path -- ``/`` and every ``/static/...`` path included, since
        layer 7 is a pure JSON API -- resolves to ``('unknown', '')`` so the
        caller answers the documented 404.  Matching is **exact**: empty
        segments, a trailing slash or a percent-encoded segment never resolve,
        which is what makes ``/api/profiles//equity`` and
        ``/static/..%2Froutes.py`` a 404 instead of a traversal.
        """
        segments = path.split("/")
        head = segments[1] if len(segments) > 1 else ""
        if len(segments) < 3 or head != "api":
            return ("unknown", "")
        name = segments[2]
        if len(segments) == 3:
            if name == "health":
                return ("health", "")
            if name == "profiles":
                return ("profiles", "")
            if name == "kill-switch":
                return ("kill_switch", "")
            if name == "catalog":
                return ("catalog", "")
            if name == "control":
                return ("control", "")
            if name == "orphans":
                return ("orphans", "")
            if name == "operator-token":
                return ("operator_token", "")
            return ("unknown", "")
        if name != "profiles":
            return ("unknown", "")
        profile_id = segments[3]
        if len(segments) == 4:
            return ("profile", profile_id) if _PROFILE_ID.fullmatch(profile_id) else ("unknown", "")
        if len(segments) == 5 and _PROFILE_ID.fullmatch(profile_id):
            leaf = segments[4]
            if leaf in _PROFILE_LEAVES or leaf in _PROFILE_ACTIONS:
                return (leaf, profile_id)
        return ("unknown", "")
