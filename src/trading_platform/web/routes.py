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
  dashboard poll): the platform snapshot, the health body, one profile snapshot
  and the kill-switch commands;
* :class:`~trading_platform.realtime.monitor.Monitor` supplies the *complete*
  persisted view of one profile (equity curve, trades, orders, positions and the
  metrics block, which delegates to ``metrics.compute_metrics``).

The orchestrator class is deliberately **never** imported here: the CLI can
therefore serve the persisted state (``realtime serve``) without an engine, and
a test can serve a local fake.

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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from trading_platform.config.models import MonitoringConfig
from trading_platform.core.errors import MonitoringError
from trading_platform.realtime.clock import Clock, SystemClock
from trading_platform.realtime.models import PlatformSnapshot, ProfileSnapshot, ProfileStatus
from trading_platform.realtime.monitor import Monitor

__all__ = ["HttpResponse", "Router", "SnapshotProvider", "operator_token_from_env"]

#: Logger of the whole web layer (the transport logs there as well).
LOGGER_NAME = "trading_platform.web"

#: Environment variable holding the single operator token (never logged).
OPERATOR_TOKEN_ENV = "TB_OPERATOR_TOKEN"

_LOGGER = logging.getLogger(LOGGER_NAME)

#: Header carrying the operator token on a mutating request.
OPERATOR_TOKEN_HEADER = "X-Operator-Token"

#: Content type of every JSON payload.
JSON_CONTENT_TYPE = "application/json; charset=utf-8"

#: Profile identifier pattern, identical to ``config.models.ProfileConfig``.
_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

#: Leaf names of the per-profile read routes.
_PROFILE_LEAVES: frozenset[str] = frozenset({"equity", "trades", "orders", "positions", "metrics"})

#: Methods answering a read-only route.
_READ_METHODS: tuple[str, ...] = ("GET", "HEAD")

#: Methods answering the (single) mutating route.
_MUTATING_METHODS: tuple[str, ...] = ("GET", "HEAD", "POST")


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

    def engage_kill_switch(self, reason: str) -> Any:
        """Engage the global kill switch and return its new state."""
        ...

    def release_kill_switch(self) -> Any:
        """Release the global kill switch and return its new state."""
        ...

    def kill_switch_state(self) -> Any:
        """Return the effective kill-switch state."""
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


def _not_found(path: str) -> HttpResponse:
    """Build the documented 404 payload of an unknown route."""
    return _json_response(404, {"error": f"not found: {path}"})


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
        The single operator token required by ``POST /api/kill-switch``.  Empty
        or ``None`` means "no token configured", which also refuses mutations.
    version:
        Version string rendered by ``GET /api/health``.
    clock:
        Time seam (D4) used for ``checked_at`` and for the ``changed_at``
        fallback of the kill switch.  Tests inject a ``ManualClock``.
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
    ) -> None:
        self._provider = provider
        self._monitor = monitor
        self._config = config
        self._read_only = bool(read_only)
        self._operator_token = _normalise_token(operator_token)
        self._version = str(version)
        self._clock: Clock = SystemClock() if clock is None else clock

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
        ``/api/kill-switch`` and ``()`` for an unknown path (a 404 carries no
        ``Allow`` header).
        """
        route, _argument = self._route(path)
        if route == "unknown":
            return ()
        return _MUTATING_METHODS if route == "kill_switch" else _READ_METHODS

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

        ``query`` is accepted for transport symmetry: no documented route of
        section 4 takes a query parameter, so it is ignored (and a request
        carrying one is answered normally).
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
        if verb == "HEAD":
            # ``HEAD`` answers exactly like ``GET`` with an empty body -- on an
            # unknown path (a 404 has no body either) as on a known one.
            answer = self._get_answer(route, argument, path)
            return HttpResponse(
                status=answer.status,
                body=b"",
                content_type=answer.content_type,
                headers=answer.headers,
            )
        if route == "unknown":
            return _not_found(path)
        allowed = _MUTATING_METHODS if route == "kill_switch" else _READ_METHODS
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
        return self._read(route, argument)

    def _get_answer(self, route: str, argument: str, path: str) -> HttpResponse:
        """Build the response ``GET`` would answer for ``route`` (never a 405)."""
        if route == "unknown":
            return _not_found(path)
        if route == "kill_switch":
            return self._kill_switch_read()
        return self._read(route, argument)

    def _read(self, route: str, argument: str) -> HttpResponse:
        """Answer one read route (``route`` is never ``kill_switch``)."""
        if route == "health":
            return _json_response(200, self._health_payload())
        if route == "profiles":
            snapshot = self._provider.snapshot()
            return _json_response(
                200,
                {
                    "profiles": [profile.to_dict() for profile in snapshot.profiles],
                    "generated_at": _iso(snapshot.generated_at),
                },
            )
        if route == "profile":
            known = self._provider.profile_snapshot(argument)
            if known is None:
                return self._unknown_profile(argument)
            return _json_response(200, known.to_dict())
        if self._provider.profile_snapshot(argument) is None:
            return self._unknown_profile(argument)
        return self._profile_detail(route, argument)

    def _profile_detail(self, route: str, profile_id: str) -> HttpResponse:
        """Answer a per-profile detail route of an existing profile."""
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

    # -- health -------------------------------------------------------------

    def _health_payload(self) -> dict[str, Any]:
        """Build the ``GET /api/health`` body from the provider and the clock.

        The orchestrator's own ``health()`` body wins for the counters it
        provides; a missing counter falls back to the platform snapshot (profile
        counts, uptime), and ``version``/``checked_at`` come from the router
        configuration and the clock seam.  ``status`` is exactly ``'ok'`` or
        ``'degraded'`` -- degraded as soon as the kill switch is engaged.
        """
        raw = self._provider.health()
        reported: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        kill_switch = self._kill_switch_engaged()

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

        return {
            "status": "degraded" if kill_switch else "ok",
            "version": self._version,
            "uptime_seconds": _finite(uptime),
            "profiles_total": int(profiles_total),
            "profiles_running": int(profiles_running),
            "kill_switch": kill_switch,
            "checked_at": _iso(self._clock.now()),
        }

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
            return _json_response(
                403,
                {
                    "error": (
                        "mutations are disabled on this server"
                        if self._read_only or self._operator_token is None
                        else "missing or invalid operator token"
                    )
                },
            )
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
            return ("unknown", "")
        if name != "profiles":
            return ("unknown", "")
        profile_id = segments[3]
        if len(segments) == 4:
            return ("profile", profile_id) if _PROFILE_ID.fullmatch(profile_id) else ("unknown", "")
        if (
            len(segments) == 5
            and segments[4] in _PROFILE_LEAVES
            and _PROFILE_ID.fullmatch(profile_id)
        ):
            return (segments[4], profile_id)
        return ("unknown", "")
