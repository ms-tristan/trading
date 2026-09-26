"""Thread -> event-loop bridge for the runtime control of the running engine.

The monitoring server is a threaded ``ThreadingHTTPServer`` (standard library
only): every request is answered by a worker thread that has **no** event loop.
The orchestrator, on the other hand, is asynchronous -- ``start``/``stop``/``pause``
are coroutines that mutate the profile registry from inside the engine loop, which
is precisely what makes a runtime command safe against a candle being processed at
the same moment.

:class:`RuntimeProfileController` is the injected seam between the two worlds.  It
is the *only* object the web layer knows about:

* it is bound to the engine loop once, from inside the engine coroutine
  (:meth:`RuntimeProfileController.bind`);
* every mutating call marshals its coroutine onto that loop with
  :func:`asyncio.run_coroutine_threadsafe` and blocks the HTTP thread until the
  coroutine completed, with a bounded timeout;
* the failure surface is preserved: a :class:`~trading_platform.core.errors.ProfileError`
  raised inside the loop is re-raised unchanged in the HTTP thread, so the router
  keeps mapping it to its documented status code.

Nothing here imports the HTTP layer, and nothing in the HTTP layer imports
:mod:`asyncio`, the orchestrator module or the loop: an unbound controller (a
read-only ``realtime serve``, or a unit test) simply refuses the mutating calls with
:class:`~trading_platform.core.errors.MonitoringError`, which the router maps to the
documented ``403``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import ValidationError

from trading_platform.config.loader import _format_validation_error
from trading_platform.config.models import ProfileConfig, RealtimeConfig
from trading_platform.core.constants import SUPPORTED_TIMEFRAMES, timeframe_minutes
from trading_platform.core.errors import ConfigError, MonitoringError, ProfileError
from trading_platform.realtime.models import ProfileSnapshot, RunMode
from trading_platform.realtime.observability import LOGGER_NAME, log_event
from trading_platform.realtime.warmup import (
    SEVERITY_ERROR,
    effective_warmup_candles,
    profile_warmup_findings,
)
from trading_platform.strategy.registry import strategy_names

if TYPE_CHECKING:
    from trading_platform.realtime.orchestrator import RealtimeOrchestrator

__all__ = ["RuntimeProfileController"]

_LOGGER = logging.getLogger(LOGGER_NAME)

_T = TypeVar("_T")

#: Profile identifier pattern, identical to ``config.models.ProfileConfig``.
_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: The supported timeframes, shortest first (the order a picker must display).
#:
#: Derived from :data:`~trading_platform.core.constants.SUPPORTED_TIMEFRAMES` rather
#: than written out, so adding a timeframe to the engine is enough for the error
#: message -- and the catalog route -- to carry it.
_TIMEFRAME_ORDER: tuple[str, ...] = tuple(sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes))

#: Message returned by every mutating call of an unbound controller.
UNBOUND_MESSAGE = "the engine is not running: profile control is unavailable"


class RuntimeProfileController:
    """Run the lifecycle commands of the engine from any (non-async) thread.

    Parameters
    ----------
    orchestrator:
        The running platform.  It is injected, never imported by the web layer: a
        test passes a local fake and the router stays importable without an engine.
        The profile set it owns is the SQLite state store's, so this controller
        carries no path: there is no configuration document to rewrite any more.
    timeout_seconds:
        How long a command may block the calling thread before it is abandoned with
        a :class:`~trading_platform.core.errors.MonitoringError`.  The bound is what
        keeps an HTTP worker from waiting for ever on a stuck engine.
    history_candles:
        The history window the engine serves a profile by default -- the realtime
        setting ``realtime.history_candles`` of the running platform.  It is used
        by the create-time warm-up check to tell "this profile can never warm up"
        (refused) from "this profile asks for more than the stream serves"
        (reported).  ``None`` falls back to the model default, so a controller
        built without it keeps the documented behaviour.
    """

    def __init__(
        self,
        *,
        orchestrator: RealtimeOrchestrator,
        timeout_seconds: float = 10.0,
        history_candles: int | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._timeout_seconds = float(timeout_seconds)
        self._history_candles = (
            int(RealtimeConfig().history_candles)
            if history_candles is None
            else max(1, int(history_candles))
        )
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def bound(self) -> bool:
        """Return whether the controller is bound to the engine loop."""
        return self._loop is not None

    def __repr__(self) -> str:
        """Return a short, secret-free representation of the controller."""
        return f"RuntimeProfileController(bound={self.bound}, timeout={self._timeout_seconds})"

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the controller to the engine loop. Idempotent.

        Called from inside the engine coroutine, where the running loop *is* the loop
        every mutation must be marshalled onto.  Binding twice is a no-op: a
        re-entered ``start`` must not lose the loop reference.
        """
        if self._loop is loop:
            return
        self._loop = loop
        log_event(_LOGGER, "profile_control_bound", timeout=self._timeout_seconds)

    # -- lifecycle commands -------------------------------------------------

    def pause_profile(self, profile_id: str) -> ProfileSnapshot:
        """Pause one profile: no new position, the open one stays managed."""
        return self._call(lambda: self._orchestrator.pause_profile(profile_id))

    def resume_profile(self, profile_id: str) -> ProfileSnapshot:
        """Resume one profile: its entry gate is open again."""
        return self._call(lambda: self._orchestrator.resume_profile(profile_id))

    def delete_profile(self, profile_id: str) -> str:
        """Delete one profile after flattening it; return the removed identifier."""
        return self._call(lambda: self._orchestrator.delete_profile(profile_id))

    def create_profile(self, payload: Mapping[str, Any]) -> ProfileSnapshot:
        """Validate one raw create request and start the profile it describes.

        The mapping is the decoded JSON body of ``POST /api/profiles``.  Validation
        happens here -- in the calling thread, before anything is marshalled -- so a
        rejected request never reaches the engine loop, and the messages are the
        ones the API documents.

        Raises
        ------
        ConfigError
            Invalid identifier, unknown strategy, unsupported timeframe, any field
            :class:`~trading_platform.config.models.ProfileConfig` rejects (mode,
            balance, parameters) -- or a profile whose strategy can **never** warm
            up with the candles the profile asks the stream for: the platform must
            refuse a profile it can never feed instead of running it for ever with
            zero signals.
        ProfileError
            The identifier is already declared by the state store or by the running
            registry.
        MonitoringError
            The engine is not running, or the command timed out.
        """
        identifier = payload.get("profile_id", payload.get("id"))
        if not isinstance(identifier, str) or not _PROFILE_ID.match(identifier):
            raise ConfigError(
                f"invalid profile id: {identifier!r} (expected ^[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}$)"
            )
        names = strategy_names()
        strategy = payload.get("strategy")
        if strategy not in names:
            raise ConfigError(f"unknown strategy: {strategy!r} (available: {', '.join(names)})")
        timeframe = payload.get("timeframe")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            supported = ", ".join(_TIMEFRAME_ORDER)
            raise ConfigError(f"unsupported timeframe: {timeframe!r} (supported: {supported})")
        profile = self._build_profile(
            identifier, strategy=strategy, timeframe=timeframe, payload=payload
        )
        self._check_warmup(profile)
        if self._orchestrator.profile_config(identifier) is not None:
            raise ProfileError(f"profile already exists: {identifier!r}")
        return self._call(lambda: self._orchestrator.add_profile(profile))

    def _check_warmup(self, profile: ProfileConfig) -> None:
        """Refuse an impossible profile, report an incoherent one.

        The arithmetic is the platform's single authority
        (:mod:`trading_platform.realtime.warmup`), applied here -- in the calling
        thread, before anything reaches the engine loop -- so a profile that could
        never warm up is refused at the very place it is created, with the message
        the API documents:

        * an **error** finding (``required_candles > warmup_candles``, both compared
          on the **resolved** warm-up) raises
          :class:`~trading_platform.core.errors.ConfigError`, which
          ``web/routes.py::_failed_mutation`` maps onto the documented ``400``:
          the profile is never created and never started.  A profile that overrides
          nothing is served its strategy's own requirement and this branch is
          unreachable for it: it is reached **only** by an explicit
          ``warmup_candles`` below the requirement;
        * a **warning** finding (``warmup_candles > history_candles``) is logged
          and the profile *is* created: the frame the strategy receives is bounded
          by ``warmup_candles``, so a smaller stream window does not by itself
          silence the profile and refusing it would refuse working configurations.
        """
        for finding in profile_warmup_findings(profile, history_candles=self._history_candles):
            if finding.severity == SEVERITY_ERROR:
                raise ConfigError(finding.message)
            log_event(
                _LOGGER,
                "profile_warmup_coherence",
                level=logging.WARNING,
                profile_id=str(profile.id),
                symbol=str(profile.symbol),
                timeframe=str(profile.timeframe),
                warmup_candles=effective_warmup_candles(profile),
                history_candles=self._history_candles,
                message=finding.message,
            )

    @staticmethod
    def _build_profile(
        identifier: str,
        *,
        strategy: str,
        timeframe: str,
        payload: Mapping[str, Any],
    ) -> ProfileConfig:
        """Build the validated :class:`ProfileConfig` behind a create request.

        Only the fields the API documents are read; ``mode`` defaults to ``paper``
        and the balance to the model default when the body omits them.
        ``warmup_candles`` and ``history_candles`` are forwarded only when the body
        carries them, so an absent override is left **absent**: ``warmup_candles``
        then means "the strategy's own requirement on the profile's timeframe" and
        ``history_candles`` "the realtime-level window".  Resolving either one here
        would create a second authority beside
        :func:`~trading_platform.realtime.warmup.effective_warmup_candles` and
        ``ProfileConfig.effective_history_candles`` -- the create-time verdict and
        the start-time verdict must be taken from the same rule.
        """
        fields: dict[str, Any] = {
            "id": identifier,
            "symbol": payload.get("symbol"),
            "timeframe": timeframe,
            "strategy": strategy,
            "mode": payload.get("mode", RunMode.PAPER.value),
            "params": payload.get("params") or {},
        }
        if payload.get("initial_balance") is not None:
            fields["initial_balance"] = payload["initial_balance"]
        if payload.get("warmup_candles") is not None:
            fields["warmup_candles"] = payload["warmup_candles"]
        if payload.get("history_candles") is not None:
            fields["history_candles"] = payload["history_candles"]
        try:
            return ProfileConfig.model_validate(fields)
        except ValidationError as exc:
            raise ConfigError(f"invalid profile: {_format_validation_error(exc)}") from exc

    def control_state(self) -> Mapping[str, Any]:
        """Return the pause/run state of every profile of the platform.

        A pure synchronous read of the in-memory registry: it needs no loop and it
        never mutates anything, so it answers even from an unbound controller.
        """
        return self._orchestrator.control_state()

    # -- the bridge ---------------------------------------------------------

    def _call(self, factory: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        """Run ``factory()`` on the engine loop and return its result.

        The coroutine is built **after** the bound check, so an unbound controller
        never leaves an un-awaited coroutine behind.  A timeout cancels the pending
        future -- the engine stops caring about a command nobody waits for any more --
        and reports the bound that was exceeded.
        """
        loop = self._loop
        if loop is None:
            raise MonitoringError(UNBOUND_MESSAGE)
        future = asyncio.run_coroutine_threadsafe(factory(), loop)
        try:
            return future.result(timeout=self._timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise MonitoringError(
                f"profile control timed out after {self._timeout_seconds}s"
            ) from exc
