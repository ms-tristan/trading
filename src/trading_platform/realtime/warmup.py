"""The warm-up contract: the single arithmetic authority of the platform.

A strategy declares how many candles a frame must hold before it can emit **any**
signal (:meth:`trading_platform.strategy.base.Strategy.required_candles`).  A
profile declares how many candles it asks the stream for
(``ProfileConfig.warmup_candles``, an **optional override**) and how many rows the
engine's window holds (``realtime.history_candles``, or the profile's own
``history_candles`` override).  Those three numbers are the whole contract, and
this module is the **only** place that compares them:

* :func:`candles_per_day` -- the candle grid of a timeframe;
* :func:`required_candles_for` -- what a profile's strategy needs on that grid;
* :func:`effective_warmup_candles` -- the warm-up a profile is *actually* served:
  its explicit override, or -- the default -- the strategy's own requirement, so
  "no override" can never mean "warm up for ever";
* :func:`profile_warmup_findings` -- the impossible profile (``required >
  warmup``: it can **never** warm up, the silent no-op of the incident) and the
  incoherent one (``warmup > usable_closed_candles(history_candles)``: the window,
  once its still-forming row is discounted, cannot serve the CLOSED candles the
  warm-up needs);
* :func:`warmup_report` -- the same numbers and findings as a JSON-ready mapping,
  consumed by ``realtime check``;
* :func:`working_timeframes` -- the timeframes that *would* work with the same
  parameters, so a refusal can tell the operator what to do instead.

The headroom rule (the whole of it)
-----------------------------------
**To serve N CLOSED candles the stream must be asked for N +
CLOSED_CANDLE_HEADROOM rows, and every finding below reasons about CLOSED rows.**
A live venue appends the candle it is still forming to every answer it returns, so
a requested window of ``N`` rows holds only ``N - CLOSED_CANDLE_HEADROOM`` closed
ones: :func:`history_request_candles` and :func:`usable_closed_candles` are that
arithmetic, they are the only place it may be restated, and a warm-up -- a count
of CLOSED candles -- must never be compared against a raw requested row count.

Every comparison below is made on the **resolved** warm-up
(:func:`effective_warmup_candles`), never on the raw field: a profile that
overrides nothing can never be **impossible** -- it is served the strategy's own
requirement -- and :data:`WARMUP_CODE_IMPOSSIBLE` stays reachable **only** when an
operator explicitly asks for fewer candles than the strategy needs.  A defaulted
profile can still be *incoherent* (its window may be narrower than its strategy's
requirement), and that is reported, never refused.

Layer direction (frozen)
------------------------
This module lives in the **realtime** layer.  It may import ``config``, ``core``
and ``strategy`` -- and the realtime strategy bridge
(:func:`trading_platform.realtime.strategies.resolve_strategy`) -- and it must
**never** import ``web`` or ``cli``: the web layer and the CLI consume this
module, never the other way round.  It imports no I/O, no clock and no network,
so every value it returns is a pure function of its arguments.

Ownership arbitration (binding)
-------------------------------
``cli.py`` is owned by the **wallet/mode** work package, not by this one: it is
needed by both the warm-up report and the wallet view, and a file may have only
one owner.  To make that split possible this module exposes the warm-up API in a
shape ``cli.py`` can use **without any change**: :func:`profile_warmup_findings`
and :func:`warmup_report` take a :class:`ProfileConfig` and ``history_candles``
only -- no new argument, no new import.  A caller that already imports them picks
the optional-override semantics up for free, because the resolution happens
*inside* these two functions (:func:`effective_warmup_candles`).  Nothing here may
be moved into ``cli.py``.

Severity semantics (frozen)
---------------------------
``error``
    The profile can **never** warm up: its strategy needs more candles than the
    profile ever asks the stream for.  This is the silent no-op -- refused where
    the profile is created and an ``ERROR`` at start.  It requires an **explicit**
    ``warmup_candles`` override below the requirement: without one, the profile is
    served the requirement itself and this severity is unreachable.
``warning``
    The configured window cannot serve the CLOSED candles the warm-up needs: one
    of its rows is the still-forming candle.  It is a real misconfiguration worth
    naming, but not a refusal: the frame the strategy receives is bounded by
    ``warmup_candles``, so a smaller window does not by itself silence the
    profile -- but a window of exactly ``warmup_candles`` rows is structurally one
    closed candle short, which is the off-by-one of the incident.

Every public function of this module is **total**: it never raises for a profile
whose strategy cannot be built (that failure is reported by ``resolve_strategy``
where the profile is resolved), so a validation path can always ask "is this
profile feedable?" without adding a second failure mode to its own answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trading_platform.config.models import ProfileConfig
from trading_platform.core.constants import SUPPORTED_TIMEFRAMES, timeframe_minutes
from trading_platform.core.errors import ConfigError, StrategyError
from trading_platform.realtime.strategies import resolve_strategy
from trading_platform.strategy.registry import get_strategy

__all__ = [
    "CLOSED_CANDLE_HEADROOM",
    "DEFAULT_WARMUP_CANDLES",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "WARMUP_CODE_COHERENCE",
    "WARMUP_CODE_IMPOSSIBLE",
    "WarmupFinding",
    "candles_per_day",
    "effective_warmup_candles",
    "history_request_candles",
    "profile_warmup_findings",
    "required_candles_for",
    "usable_closed_candles",
    "warmup_report",
    "working_timeframes",
]

#: Warm-up the contract falls back to when the strategy cannot be built at all.
#:
#: ``200`` is not an arbitrary number: it is the value
#: ``ProfileConfig.warmup_candles`` used to default to before it became optional,
#: and the value :data:`~trading_platform.config.models.MAX_ENTRY_LOOKBACK_CANDLES`
#: was pinned to for exactly that reason -- the maximum catch-up window a profile
#: may declare is satisfiable by this warm-up out of the box.  It is reached only
#: by :func:`effective_warmup_candles`, and only for a profile whose strategy
#: cannot be built (unknown name, rejected parameters, unsupported timeframe): a
#: validation path must answer *something* usable rather than raise a second
#: failure mode of its own.
DEFAULT_WARMUP_CANDLES: int = 200

#: Rows of a live provider window that can **never** be used: the still-forming candle.
#:
#: A live venue appends the candle it is currently building to every kline answer,
#: and that row's close time is in the future -- it is not a closed candle and no
#: strategy may read it.  A requested window of ``N`` rows therefore serves at most
#: ``N - CLOSED_CANDLE_HEADROOM`` closed candles.  Measured on the live incident:
#: the runner asked Binance for 169 4h klines and got 169 rows back, whose last close
#: time (1790452799999) was still ahead of the clock (1790448040610) -- 168 closed
#: candles for a strategy that needs 169, so the frame held 168 rows on every tick
#: and the strategy was never reached.
#:
#: Any comparison against a **requested window** must go through
#: :func:`history_request_candles` (or :func:`usable_closed_candles`) instead of
#: comparing against the raw row count.
CLOSED_CANDLE_HEADROOM: int = 1

#: Severity of a finding that must stop the profile (it can never warm up).
SEVERITY_ERROR = "error"

#: Severity of a finding that must be reported but never stops the profile.
SEVERITY_WARNING = "warning"

#: Code of the "the strategy needs more candles than the profile asks for" finding.
WARMUP_CODE_IMPOSSIBLE = "strategy-warmup-impossible"

#: Code of the "the configured window cannot serve the warm-up's CLOSED rows" finding.
WARMUP_CODE_COHERENCE = "warmup-exceeds-history"

#: Minutes of one 24-hour day, the numerator of the candle grid.
_MINUTES_PER_DAY = 1440.0


@dataclass(frozen=True)
class WarmupFinding:
    """One warm-up verdict about one profile.

    Attributes
    ----------
    code:
        Stable machine-readable identifier (:data:`WARMUP_CODE_IMPOSSIBLE` or
        :data:`WARMUP_CODE_COHERENCE`).
    severity:
        :data:`SEVERITY_ERROR` (the profile can never warm up) or
        :data:`SEVERITY_WARNING` (it is misconfigured but still runs).
    message:
        The operator-facing sentence, self-contained: it names the profile and the
        numbers behind the verdict.  For an error: the strategy, the timeframe, the
        candles required and the candles the profile asks for, plus the timeframes
        that would work instead.  For a warning: the resolved warm-up, the
        configured window, the CLOSED candles that window can serve and the minimum
        window to configure.
    """

    code: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        """Return the finding as the documented ``{code, severity, message}`` mapping."""
        return {"code": self.code, "severity": self.severity, "message": self.message}


def candles_per_day(timeframe: str) -> float:
    """Return how many candles of ``timeframe`` fit in one 24-hour day.

    The grid is derived from :data:`~trading_platform.core.constants.TIMEFRAME_MINUTES`,
    so it is the very grid the data layer, the strategy and the stream agree on:
    ``1m`` yields ``1440.0``, ``5m`` ``288.0``, ``15m`` ``96.0``, ``30m`` ``48.0``,
    ``1h`` ``24.0``, ``4h`` ``6.0`` and ``1d`` ``1.0``.

    Raises
    ------
    ConfigError
        If ``timeframe`` is not supported.  This is the only raising function of
        the module and it is the caller's business: a :class:`ProfileConfig` has
        already validated its timeframe by the time it reaches here.
    """
    return _MINUTES_PER_DAY / float(timeframe_minutes(timeframe))


def required_candles_for(profile: ProfileConfig) -> int:
    """Return the candles ``profile``'s strategy needs before it can emit a signal.

    The strategy is built through the realtime layer's single construction path
    (:func:`~trading_platform.realtime.strategies.resolve_strategy`), so the
    requirement is read from the very class the runner will use, with the very
    parameters the profile declares, on the profile's own candle grid.

    Raises
    ------
    StrategyError
        If the strategy name is unknown or its parameters are rejected.  Letting
        it propagate is deliberate: that failure is reported by the profile
        resolution path, and swallowing it here would answer ``0`` for a profile
        nobody can build.  The *finding* helpers below do swallow it, because a
        validation path must stay total.
    ConfigError
        If the profile's timeframe is not supported.
    """
    strategy = resolve_strategy(profile)
    return int(strategy.required_candles(candles_per_day(str(profile.timeframe))))


def effective_warmup_candles(profile: ProfileConfig) -> int:
    """Return the warm-up ``profile`` is **actually** served, as a plain ``int >= 1``.

    The number is a count of **CLOSED** candles -- the candles the strategy may
    read, and the ones :func:`history_request_candles` turns into the rows the
    stream must be asked for.  ``ProfileConfig.warmup_candles`` is optional,
    exactly like its sibling ``history_candles``, and this is its resolution helper
    -- the mirror of
    :meth:`~trading_platform.config.models.ProfileConfig.effective_history_candles`.
    The rule is the whole point of the field being optional:

    * an **explicit** value is a deliberate override and is returned as-is;
    * ``None`` -- "not overridden" -- resolves to the strategy's **own**
      requirement on the profile's timeframe (:func:`required_candles_for`), so a
      profile can never be created with a frame that silently never warms up.

    The function is **total**, unlike :func:`required_candles_for`: when the
    strategy cannot be built (unknown name, rejected parameters, unsupported
    timeframe) it answers :data:`DEFAULT_WARMUP_CANDLES` instead of raising,
    because a validation path must never gain a second failure mode -- the
    unbuildable strategy is already reported by ``resolve_strategy`` where the
    profile is resolved.  The same ``(ConfigError, StrategyError)`` pair is
    swallowed as in :func:`profile_warmup_findings`, and deliberately so: the two
    must agree on what "unbuildable" means.
    """
    if profile.warmup_candles is not None:
        return int(profile.warmup_candles)
    try:
        required = required_candles_for(profile)
    except (ConfigError, StrategyError):
        return DEFAULT_WARMUP_CANDLES
    # A strategy may legitimately declare no warm-up at all (``basic`` answers
    # ``0``); the warm-up the engine is asked for is never below one candle.
    return max(1, int(required))


def history_request_candles(warmup_candles: int) -> int:
    """Return the rows to ask the stream for so ``warmup_candles`` CLOSED candles are served.

    A live provider window always ends with the candle the venue is still forming
    (:data:`CLOSED_CANDLE_HEADROOM`, a row whose close time is in the future), so
    asking for the warm-up itself is always exactly one row short: the frame the
    runner builds then holds one closed candle fewer than the strategy requires and
    ``run_once`` logs ``warmup_incomplete`` for ever instead of running it.  This
    helper is the arithmetic authority for that request -- the runner asks the
    stream for ``history_request_candles(warmup)`` rows, never ``warmup``.

    Any check that compares something against a **requested window** -- a warm-up
    matrix, a catalogue rule, a create-time guard -- must go through this helper or
    :func:`usable_closed_candles`.  A warm-up is a count of CLOSED candles, so
    ``required_candles <= warmup_candles`` is the correct rule and a comparison of
    ``required_candles`` against a raw row count is one row short by construction.

    The function is **total**: it never raises and always answers at least
    ``1 + CLOSED_CANDLE_HEADROOM``.  A non-number-like argument is read as ``0``,
    exactly like the other total helpers of this module, so a validation path can
    always ask "how many rows would this need?".
    """
    return max(1, _as_int(warmup_candles)) + CLOSED_CANDLE_HEADROOM


def usable_closed_candles(window_candles: int) -> int:
    """Return the CLOSED candles a window of ``window_candles`` requested rows can serve.

    The inverse of :func:`history_request_candles` for every value that helper can
    produce: ``usable_closed_candles(history_request_candles(n)) == max(1, n)``.  A
    window narrower than the headroom serves nothing at all.

    The function is **total**: it never raises and never answers a negative number,
    so a comparison against a warm-up is always well defined -- even for the
    nonsense a hand-edited configuration may hold.
    """
    return max(0, _as_int(window_candles) - CLOSED_CANDLE_HEADROOM)


def profile_warmup_findings(profile: ProfileConfig, *, history_candles: int) -> list[WarmupFinding]:
    """Return the warm-up verdicts about ``profile`` -- at most one per severity.

    ``history_candles`` is the window the engine is configured to ask the stream
    for this profile (``RealtimeConfig.history_candles``, or the profile's own
    ``history_candles`` override -- see
    :meth:`~trading_platform.config.models.ProfileConfig.effective_history_candles`):
    a count of **requested rows**, one of which the venue is still forming, so it
    holds ``usable_closed_candles(history_candles)`` closed candles at most.

    The two rules, and there are exactly two -- both stated on the **resolved**
    warm-up (``warmup = effective_warmup_candles(profile)``, never the raw optional
    field) and both reasoned about in **CLOSED** candles:

    * :data:`WARMUP_CODE_IMPOSSIBLE` (**error**) iff
      ``required_candles_for(profile) > warmup``: the frame the runner builds can
      never reach the strategy's warm-up, so the profile would run for ever with
      zero signals.  ``warmup`` counts **CLOSED** candles, so
      ``required_candles <= warmup_candles`` is the **correct** rule for any
      external check (a catalogue, a warm-up matrix, a create-time guard): a
      comparison of ``required_candles`` against a raw requested window -- a
      provider row count, which carries the still-forming candle -- is one row
      short by construction and must go through :func:`history_request_candles` or
      :func:`usable_closed_candles` instead.  With no explicit override ``warmup``
      **is** the requirement, so this is false by construction: the finding stays
      reachable only when an operator deliberately sets ``warmup_candles`` below it;
    * :data:`WARMUP_CODE_COHERENCE` (**warning**) iff
      ``history_request_candles(warmup) > history_candles`` -- equivalently
      ``warmup > usable_closed_candles(history_candles)``: the configured window
      cannot serve the CLOSED rows the warm-up needs, because one of its rows is
      the still-forming candle.  ``warmup == history_candles`` is therefore **not**
      coherent: that window serves ``warmup - 1`` closed candles, exactly the
      one-row shortfall that silenced every profile of the incident.

    Both can hold at once, and each is reported at most once.  The function is
    **total and never raises**: a profile whose strategy cannot be built (unknown
    name, rejected parameters, unsupported timeframe) answers ``[]``, because that
    failure is already reported by :func:`required_candles_for` /
    ``resolve_strategy`` and this helper must never add a second one.
    """
    try:
        per_day = candles_per_day(str(profile.timeframe))
        required = required_candles_for(profile)
    except (ConfigError, StrategyError):
        return []

    warmup = effective_warmup_candles(profile)
    window = _as_int(history_candles)
    findings: list[WarmupFinding] = []
    if required > warmup:
        findings.append(
            WarmupFinding(
                WARMUP_CODE_IMPOSSIBLE,
                SEVERITY_ERROR,
                _impossible_message(profile, required=required, per_day=per_day, warmup=warmup),
            )
        )
    if history_request_candles(warmup) > window:
        findings.append(
            WarmupFinding(
                WARMUP_CODE_COHERENCE,
                SEVERITY_WARNING,
                _coherence_message(profile, warmup=warmup, window=window),
            )
        )
    return findings


def warmup_report(profile: ProfileConfig, *, history_candles: int) -> dict[str, Any]:
    """Return the warm-up numbers and findings of ``profile`` as a JSON-ready mapping.

    The result carries exactly five keys -- ``candles_per_day``,
    ``required_candles``, ``warmup_candles``, ``history_candles`` and ``findings``
    -- so ``realtime check`` can publish it under its additive ``warmup`` key
    without inventing a second vocabulary.

    ``warmup_candles`` is the **resolved** warm-up
    (:func:`effective_warmup_candles`): a plain ``int >= 1``, never ``None``, so a
    consumer that reads the key directly (the CLI report, the dashboard) sees the
    number the engine really asks the stream for instead of an absent override.

    The function is **total and never raises**: when the strategy cannot be built,
    ``required_candles`` is ``0`` and ``findings`` is ``[]`` (the strategy failure
    is reported by the profile resolution, not here), and the same holds for a
    profile whose timeframe is not supported, in which case ``candles_per_day`` is
    ``0.0`` -- "no grid at all" -- rather than a misleading number.
    """
    window = _as_int(history_candles)
    try:
        per_day = candles_per_day(str(profile.timeframe))
    except ConfigError:  # pragma: no cover - a ProfileConfig validates its timeframe
        per_day = 0.0
    try:
        required = required_candles_for(profile)
    except (ConfigError, StrategyError):
        required = 0
    return {
        "candles_per_day": per_day,
        "required_candles": required,
        "warmup_candles": effective_warmup_candles(profile),
        "history_candles": window,
        "findings": [
            finding.to_dict()
            for finding in profile_warmup_findings(profile, history_candles=window)
        ],
    }


def working_timeframes(
    strategy: str, params: Mapping[str, Any], *, warmup_candles: int
) -> list[str]:
    """Return the supported timeframes that ``warmup_candles`` can actually feed.

    The timeframes are the ones whose requirement is ``<= warmup_candles`` for the
    very same strategy and parameters, sorted **shortest first** (by
    :func:`~trading_platform.core.constants.timeframe_minutes`): the order a
    refusal must list them in, so the cheapest grid comes first.  The strategy is
    built through the registry with its own parameter model, exactly like
    :func:`required_candles_for` builds the profile's.

    The comparison is exact because ``warmup_candles`` here counts **CLOSED**
    candles -- the same unit as the strategy's requirement -- and not provider rows:
    the rows a stream would have to be asked for are
    ``history_request_candles(warmup_candles)`` (:data:`CLOSED_CANDLE_HEADROOM`
    more).  A caller that compares a warm-up against a **requested window** must
    convert it through :func:`usable_closed_candles` first.

    The function is **total and never raises**: ``[]`` means "nothing works" --
    the strategy is unknown, its parameters are rejected, or no supported
    timeframe can satisfy the warm-up with the candles available.
    """
    return [
        timeframe
        for timeframe, _required in _requirements(
            strategy, params, warmup_candles=_as_int(warmup_candles)
        )
    ]


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _requirements(
    strategy: str, params: Mapping[str, Any], *, warmup_candles: int
) -> list[tuple[str, int]]:
    """Return the ``(timeframe, required_candles)`` pairs ``warmup_candles`` can feed.

    Shortest timeframe first.  An unknown strategy or a rejected parameter set
    answers ``[]`` instead of raising: this feeds an error *message*, which must
    never be the thing that fails.
    """
    try:
        built = get_strategy(strategy, params)
    except (ConfigError, StrategyError):
        return []
    pairs: list[tuple[str, int]] = []
    for timeframe in sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes):
        required = int(built.required_candles(candles_per_day(timeframe)))
        if required <= warmup_candles:
            pairs.append((timeframe, required))
    return pairs


def _impossible_message(
    profile: ProfileConfig, *, required: int, per_day: float, warmup: int
) -> str:
    """Render the frozen "this profile can never warm up" sentence.

    The wording is part of the contract: the create refusal, the CLI finding and
    the runner's start-time error all carry **the very same sentence**, so an
    operator who saw it once recognises it everywhere.  It names the strategy, the
    timeframe, the candles required (with the day lookback it comes from), the
    candles the profile asks for, and the timeframes that would work instead.
    """
    pairs = _requirements(str(profile.strategy), profile.strategy_params(), warmup_candles=warmup)
    if pairs:
        alternatives = ", ".join(f"{timeframe} ({needed})" for timeframe, needed in pairs)
        tail = (
            f"timeframes that would work with these parameters: {alternatives}; "
            f"raise warmup_candles to at least {required}"
        )
    else:
        tail = (
            "none of the supported timeframes would work with these parameters; "
            f"raise warmup_candles to at least {required} or lower the day lookbacks"
        )
    return (
        f"profile {str(profile.id)!r} can never warm up: strategy {str(profile.strategy)!r} "
        f"on timeframe {str(profile.timeframe)!r} needs {required} candles "
        f"({per_day:g} candles/day, longest lookback {required - 1} candles + 1) but the "
        f"profile only ever asks the stream for {warmup} candles (warmup_candles={warmup}); "
        f"{tail}"
    )


def _coherence_message(profile: ProfileConfig, *, warmup: int, window: int) -> str:
    """Render the frozen "the window cannot serve the warm-up" sentence.

    A warning, never a refusal: the frame the strategy receives is bounded by
    ``warmup_candles``, so a smaller stream window does not by itself silence the
    profile -- but the operator asks for more CLOSED candles than the window can
    serve once its still-forming row is discounted, and that has to be visible.

    The sentence names the resolved warm-up, the configured window, the closed rows
    that window can serve and the minimum window (in rows) to configure -- which is
    the warm-up plus the headroom, never the warm-up itself.
    """
    return (
        f"profile {str(profile.id)!r} asks for {warmup} warm-up candles "
        f"(warmup_candles={warmup}) but the live stream is configured to serve a "
        f"{window}-candle window (history_candles={window}), which holds only "
        f"{usable_closed_candles(window)} closed candles (the venue always appends one "
        "still-forming candle); raise history_candles to at least "
        f"{history_request_candles(warmup)} (realtime setting or the per-profile "
        "override) or lower warmup_candles"
    )


def _as_int(value: Any) -> int:
    """Return ``value`` as an ``int``, or ``0`` when it is not number-like."""
    try:
        return int(value)
    except (TypeError, ValueError):  # pragma: no cover - the fields are typed ints
        return 0
