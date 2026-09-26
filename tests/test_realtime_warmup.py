"""The arithmetic authority of the warm-up contract: pure, total, and frozen.

:mod:`trading_platform.realtime.warmup` is the single place that compares the
three numbers of the contract -- what a strategy needs, what a profile asks the
stream for, and how long a window the stream serves.  Every other layer (the
create refusal, ``realtime check``, the runner's start-time check) reads its
verdicts here, so this file pins the module's public vocabulary, its two rules
and the totality that lets a validation path ask "is this profile feedable?"
without adding a failure mode of its own.

Offline and deterministic: every value is a pure function of its arguments, and
nothing here touches a clock, a file or the network.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from trading_platform.config.models import ProfileConfig
from trading_platform.core.constants import SUPPORTED_TIMEFRAMES, timeframe_minutes
from trading_platform.core.errors import ConfigError, StrategyError
from trading_platform.realtime import warmup as warmup_module
from trading_platform.realtime.warmup import (
    DEFAULT_WARMUP_CANDLES,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    WARMUP_CODE_COHERENCE,
    WARMUP_CODE_IMPOSSIBLE,
    WarmupFinding,
    candles_per_day,
    effective_warmup_candles,
    profile_warmup_findings,
    required_candles_for,
    warmup_report,
    working_timeframes,
)

#: The frozen public surface of the module.
FROZEN_EXPORTS = {
    "DEFAULT_WARMUP_CANDLES",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "WARMUP_CODE_COHERENCE",
    "WARMUP_CODE_IMPOSSIBLE",
    "WarmupFinding",
    "candles_per_day",
    "effective_warmup_candles",
    "profile_warmup_findings",
    "required_candles_for",
    "warmup_report",
    "working_timeframes",
}

#: The severity vocabulary is frozen: no new severity may appear.
FROZEN_SEVERITIES = {"error", "warning"}

#: Candles per 24-hour day of every supported timeframe.
CANDLES_PER_DAY: dict[str, float] = {
    "1m": 1440.0,
    "5m": 288.0,
    "15m": 96.0,
    "30m": 48.0,
    "1h": 24.0,
    "4h": 6.0,
    "1d": 1.0,
}

#: Candles ``momentum`` needs with its default parameters, per timeframe.
REQUIRED_BY_TIMEFRAME: dict[str, int] = {
    "1m": 40321,
    "5m": 8065,
    "15m": 2689,
    "30m": 1345,
    "1h": 673,
    "4h": 169,
    "1d": 29,
}

#: The very profile the operator created through the dashboard, with **no**
#: explicit warm-up override: the shape that used to be a silent no-op and is now
#: resolved to the strategy's own requirement.
INCIDENT_PROFILE: dict[str, Any] = {
    "id": "momentum-1m",
    "symbol": "BTC/USDT",
    "timeframe": "1m",
    "strategy": "momentum",
}

#: The explicit override the operator asks for when they really mean 200 candles.
EXPLICIT_WARMUP = 200

#: The incident sentence, pinned byte-for-byte: an explicit override below the
#: requirement is refused with exactly this message.
INCIDENT_MESSAGE = (
    "profile 'momentum-1m' can never warm up: strategy 'momentum' on timeframe '1m' "
    "needs 40321 candles (1440 candles/day, longest lookback 40320 candles + 1) but "
    "the profile only ever asks the stream for 200 candles (warmup_candles=200); "
    "timeframes that would work with these parameters: 4h (169), 1d (29); "
    "raise warmup_candles to at least 40321"
)

#: The coherence sentence, pinned byte-for-byte.
COHERENCE_MESSAGE = (
    "profile 'momentum-1m' asks for 1000 warm-up candles (warmup_candles=1000) but "
    "the live stream is configured to serve a 300-candle window (history_candles=300); "
    "raise history_candles to at least 1000 (realtime setting or the per-profile "
    "override) or lower warmup_candles"
)


def profile(**overrides: Any) -> ProfileConfig:
    """Return the incident profile, overridable field by field."""
    return ProfileConfig.model_validate({**INCIDENT_PROFILE, **overrides})


# ---------------------------------------------------------------------------
# 1. the frozen vocabulary
# ---------------------------------------------------------------------------


def test_the_module_exports_exactly_the_frozen_surface() -> None:
    """The vocabulary is part of the contract: callers pin these names."""
    assert set(warmup_module.__all__) == FROZEN_EXPORTS
    assert len(warmup_module.__all__) == len(FROZEN_EXPORTS)


def test_the_default_warmup_candles_keeps_its_historical_value() -> None:
    """``200`` is the value the optional field used to default to: it stays pinned.

    It is the fallback of :func:`effective_warmup_candles` for an unbuildable
    strategy, and the value ``MAX_ENTRY_LOOKBACK_CANDLES`` was pinned to.
    """
    assert DEFAULT_WARMUP_CANDLES == 200
    assert isinstance(DEFAULT_WARMUP_CANDLES, int)


def test_only_the_two_documented_severities_exist() -> None:
    """The severity set is frozen: ``error`` and ``warning``, nothing else."""
    assert {SEVERITY_ERROR, SEVERITY_WARNING} == FROZEN_SEVERITIES


def test_the_severities_and_codes_have_their_frozen_values() -> None:
    """Stable machine-readable values: they travel in payloads and in tests."""
    assert SEVERITY_ERROR == "error"
    assert SEVERITY_WARNING == "warning"
    assert WARMUP_CODE_IMPOSSIBLE == "strategy-warmup-impossible"
    assert WARMUP_CODE_COHERENCE == "warmup-exceeds-history"


def test_a_finding_is_a_frozen_record_of_three_strings() -> None:
    """``WarmupFinding`` is immutable and serialises to its documented mapping."""
    finding = WarmupFinding(code=WARMUP_CODE_IMPOSSIBLE, severity=SEVERITY_ERROR, message="m")
    assert (finding.code, finding.severity, finding.message) == (
        WARMUP_CODE_IMPOSSIBLE,
        SEVERITY_ERROR,
        "m",
    )
    assert finding.to_dict() == {
        "code": WARMUP_CODE_IMPOSSIBLE,
        "severity": SEVERITY_ERROR,
        "message": "m",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.severity = SEVERITY_WARNING  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. the grid and the requirement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes))
def test_candles_per_day_is_the_grid_of_the_timeframe(timeframe: str) -> None:
    """``1440 / minutes`` -- the arithmetic the data, strategy and stream agree on."""
    assert candles_per_day(timeframe) == pytest.approx(CANDLES_PER_DAY[timeframe])


@pytest.mark.parametrize("timeframe", ["7m", "", "1H", "hourly"])
def test_candles_per_day_refuses_an_unsupported_timeframe(timeframe: str) -> None:
    """This is the module's only raising function, and it stays loud."""
    with pytest.raises(ConfigError):
        candles_per_day(timeframe)


@pytest.mark.parametrize("timeframe", sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes))
def test_required_candles_for_reads_the_strategy_the_profile_will_run(timeframe: str) -> None:
    """The requirement is the strategy's own declaration on the profile's grid."""
    assert required_candles_for(profile(timeframe=timeframe)) == REQUIRED_BY_TIMEFRAME[timeframe]


def test_required_candles_for_is_zero_for_a_strategy_without_warmup() -> None:
    """``basic`` declares nothing, so every grid answers ``0``."""
    for timeframe in SUPPORTED_TIMEFRAMES:
        assert required_candles_for(profile(strategy="basic", timeframe=timeframe)) == 0


def test_required_candles_for_propagates_an_unbuildable_strategy() -> None:
    """A profile nobody can build stays loud on the strict reader."""
    with pytest.raises(StrategyError):
        required_candles_for(profile(strategy="does-not-exist"))
    with pytest.raises(StrategyError):
        required_candles_for(
            profile(strategy="momentum", params={"fast_days": 10, "mid_days": 5, "slow_days": 3})
        )


# ---------------------------------------------------------------------------
# 2b. the resolved warm-up: the optional override
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(SUPPORTED_TIMEFRAMES, key=timeframe_minutes))
def test_effective_warmup_agrees_with_the_requirement_without_an_override(
    timeframe: str,
) -> None:
    """No override means "the strategy's own requirement on the profile's grid"."""
    target = profile(timeframe=timeframe)
    assert target.warmup_candles is None
    assert effective_warmup_candles(target) == required_candles_for(target)
    assert effective_warmup_candles(target) == REQUIRED_BY_TIMEFRAME[timeframe]


def test_an_explicit_override_wins_over_the_requirement() -> None:
    """A set value is a deliberate override and is returned as-is."""
    assert effective_warmup_candles(profile(warmup_candles=7)) == 7
    assert effective_warmup_candles(profile(timeframe="4h", warmup_candles=200)) == 200


def test_effective_warmup_is_never_below_one() -> None:
    """The engine is never asked for a zero-candle frame."""
    # ``basic`` declares no warm-up at all, so the raw requirement is 0.
    assert required_candles_for(profile(strategy="basic")) == 0
    assert effective_warmup_candles(profile(strategy="basic")) == 1


def test_effective_warmup_is_total_for_an_unbuildable_strategy() -> None:
    """A validation path must never gain a second failure mode: it answers 200."""
    for target in (
        profile(strategy="does-not-exist"),
        profile(strategy="momentum", params={"fast_days": 10, "mid_days": 5, "slow_days": 3}),
        # An unsupported timeframe is refused by the model, so the only way to
        # reach the ConfigError half of the guard is to construct around it.
        ProfileConfig.model_construct(
            id="broken", symbol="BTC/USDT", timeframe="7m", strategy="momentum"
        ),
    ):
        assert effective_warmup_candles(target) == DEFAULT_WARMUP_CANDLES


def test_effective_warmup_never_raises_where_the_strict_reader_does() -> None:
    """The pair is deliberate: the strict reader raises, the resolution helper does not."""
    target = profile(strategy="does-not-exist")
    with pytest.raises(StrategyError):
        required_candles_for(target)
    assert effective_warmup_candles(target) == DEFAULT_WARMUP_CANDLES


# ---------------------------------------------------------------------------
# 3. the two rules
# ---------------------------------------------------------------------------


def test_a_healthy_profile_has_no_finding_at_all() -> None:
    """4h with no override resolves to 169 candles inside a 300-candle window."""
    assert profile_warmup_findings(profile(timeframe="4h"), history_candles=300) == []


def test_the_incident_profile_without_an_override_is_never_impossible() -> None:
    """THE INCIDENT SHAPE, fixed: no override resolves to 40321, so the silent
    no-op is gone by construction -- the operator gets a coherent profile."""
    target = profile()
    assert effective_warmup_candles(target) == 40321
    report = warmup_report(target, history_candles=300)
    assert report["warmup_candles"] == 40321
    codes = [finding["code"] for finding in report["findings"]]
    assert WARMUP_CODE_IMPOSSIBLE not in codes


def test_the_incident_profile_with_an_explicit_override_is_refused_verbatim() -> None:
    """THE INCIDENT SHAPE, still refused where the operator asked for it.

    ``warmup_candles=200`` on ``momentum``/``1m`` needs 40321 candles: the silent
    no-op must stay an error, with the frozen sentence.
    """
    findings = profile_warmup_findings(profile(warmup_candles=EXPLICIT_WARMUP), history_candles=300)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == WARMUP_CODE_IMPOSSIBLE
    assert finding.severity == SEVERITY_ERROR
    assert finding.message == INCIDENT_MESSAGE
    # and the sentence still carries the shapes the operator reads
    message = finding.message
    assert "can never warm up" in message
    assert "'momentum-1m'" in message
    assert "'momentum'" in message
    assert "'1m'" in message
    assert "40321" in message
    assert "(warmup_candles=200)" in message
    # the timeframes that WOULD work with these parameters
    assert "4h" in message
    assert "1d" in message
    assert "169" in message
    assert "29" in message


@pytest.mark.parametrize("timeframe", ["1m", "15m", "1h"])
def test_an_explicit_undersized_warmup_is_an_error_on_every_intraday_grid(
    timeframe: str,
) -> None:
    """The refusal the dashboard needed, now reachable only through an override."""
    findings = profile_warmup_findings(
        profile(timeframe=timeframe, warmup_candles=EXPLICIT_WARMUP), history_candles=300
    )
    assert [finding.severity for finding in findings] == [SEVERITY_ERROR]
    assert findings[0].code == WARMUP_CODE_IMPOSSIBLE


def test_the_same_profile_on_four_hours_is_accepted() -> None:
    """The grid that really works at the default warm-up produces no error."""
    findings = profile_warmup_findings(profile(timeframe="4h"), history_candles=300)
    assert [finding.severity for finding in findings] == []


def test_a_default_profile_can_never_be_impossible() -> None:
    """With no override ``required > warmup`` is false by construction, on every grid."""
    for timeframe in SUPPORTED_TIMEFRAMES:
        findings = profile_warmup_findings(profile(timeframe=timeframe), history_candles=10**9)
        assert [finding.code for finding in findings] == [], timeframe


def test_equal_requirement_and_warmup_is_not_an_error() -> None:
    """The boundary is inclusive: exactly enough candles is enough."""
    findings = profile_warmup_findings(
        profile(timeframe="4h", warmup_candles=REQUIRED_BY_TIMEFRAME["4h"]), history_candles=300
    )
    assert findings == []
    findings = profile_warmup_findings(
        profile(timeframe="4h", warmup_candles=REQUIRED_BY_TIMEFRAME["4h"] - 1),
        history_candles=300,
    )
    assert [finding.code for finding in findings] == [WARMUP_CODE_IMPOSSIBLE]


def test_a_warmup_wider_than_the_stream_window_is_a_warning() -> None:
    """The profile asks for more candles than the engine will serve: reported."""
    findings = profile_warmup_findings(
        profile(timeframe="1h", warmup_candles=1000), history_candles=300
    )
    assert [finding.code for finding in findings] == [WARMUP_CODE_COHERENCE]
    finding = findings[0]
    assert finding.severity == SEVERITY_WARNING
    assert finding.message == COHERENCE_MESSAGE
    assert "1000" in finding.message
    assert "300" in finding.message
    assert "history_candles" in finding.message


def test_the_resolved_default_warmup_is_coherent_with_the_default_window() -> None:
    """A default profile is fed its own requirement inside the default window.

    This is the end-to-end property of the new default: resolution happens first,
    so the coherence rule compares the resolved number, not an absent field.
    """
    target = profile(timeframe="4h")
    findings = profile_warmup_findings(target, history_candles=300)
    assert findings == []
    assert effective_warmup_candles(target) == REQUIRED_BY_TIMEFRAME["4h"]


def test_equal_warmup_and_history_is_coherent() -> None:
    """The coherence boundary is inclusive too."""
    findings = profile_warmup_findings(
        profile(timeframe="1h", warmup_candles=673), history_candles=673
    )
    assert findings == []


def test_a_profile_can_be_both_impossible_and_incoherent() -> None:
    """The two rules are independent, and both are reported -- each once.

    Both at once now requires an explicit override above the window: the resolved
    warm-up is what each rule is compared against.
    """
    findings = profile_warmup_findings(profile(warmup_candles=400), history_candles=300)
    assert [finding.code for finding in findings] == [
        WARMUP_CODE_IMPOSSIBLE,
        WARMUP_CODE_COHERENCE,
    ]
    assert [finding.severity for finding in findings] == [SEVERITY_ERROR, SEVERITY_WARNING]


def test_a_basic_profile_wider_than_its_window_is_only_a_warning() -> None:
    """``basic`` declares no warm-up, so the seeded check scenario stays ``ok``.

    This is the exact shape ``realtime check`` is seeded with (``warmup_candles``
    40, ``history_candles`` 1): the coherence mismatch must be reported *without*
    turning a working profile into a failure.
    """
    findings = profile_warmup_findings(
        profile(strategy="basic", timeframe="1h", warmup_candles=40), history_candles=1
    )
    assert [finding.code for finding in findings] == [WARMUP_CODE_COHERENCE]
    assert findings[0].severity == SEVERITY_WARNING


# ---------------------------------------------------------------------------
# 4. totality: a validation path can always ask
# ---------------------------------------------------------------------------


def test_an_unbuildable_strategy_is_never_a_finding() -> None:
    """The finding helpers swallow what ``resolve_strategy`` already reports."""
    assert profile_warmup_findings(profile(strategy="does-not-exist"), history_candles=1) == []
    assert (
        profile_warmup_findings(
            profile(strategy="momentum", params={"fast_days": 10, "mid_days": 5, "slow_days": 3}),
            history_candles=1,
        )
        == []
    )


def test_an_unbuildable_strategy_reports_zero_required_candles() -> None:
    """The report stays total and never invents a requirement it cannot compute."""
    report = warmup_report(profile(strategy="does-not-exist"), history_candles=300)
    assert report["required_candles"] == 0
    assert report["findings"] == []


# ---------------------------------------------------------------------------
# 5. the report and the alternatives
# ---------------------------------------------------------------------------


def test_the_report_carries_exactly_the_five_documented_keys() -> None:
    """``realtime check`` publishes this mapping under its additive key.

    ``warmup_candles`` is the **resolved** warm-up: a plain ``int >= 1``, never
    ``None``, so a consumer that reads the key directly sees the window the engine
    really asks the stream for.
    """
    report = warmup_report(profile(), history_candles=300)
    assert set(report) == {
        "candles_per_day",
        "required_candles",
        "warmup_candles",
        "history_candles",
        "findings",
    }
    assert report["candles_per_day"] == pytest.approx(1440.0)
    assert report["required_candles"] == 40321
    # no override -> the strategy's own requirement, and never ``None``
    assert report["warmup_candles"] == 40321
    assert isinstance(report["warmup_candles"], int)
    assert report["history_candles"] == 300
    assert [finding["code"] for finding in report["findings"]] == [WARMUP_CODE_COHERENCE]
    assert set(report["findings"][0]) == {"code", "severity", "message"}


def test_the_report_publishes_an_explicit_override_unchanged() -> None:
    """An explicit value is reported as-is -- and the incident sentence comes with it."""
    report = warmup_report(profile(warmup_candles=EXPLICIT_WARMUP), history_candles=300)

    assert report["warmup_candles"] == EXPLICIT_WARMUP
    assert report["required_candles"] == 40321
    assert [finding["code"] for finding in report["findings"]] == [WARMUP_CODE_IMPOSSIBLE]
    assert report["findings"][0]["message"] == INCIDENT_MESSAGE


def test_the_report_never_publishes_a_null_warmup() -> None:
    """Even for an unbuildable strategy the key is a usable int, never ``None``."""
    report = warmup_report(profile(strategy="does-not-exist"), history_candles=300)

    assert report["warmup_candles"] == DEFAULT_WARMUP_CANDLES
    assert report["required_candles"] == 0
    assert report["findings"] == []


def test_the_report_is_a_plain_json_native_mapping() -> None:
    """No dataclass, no numpy scalar: the check payload serialises as it stands."""
    report = warmup_report(profile(strategy="basic", warmup_candles=40), history_candles=1)
    assert json.loads(json.dumps(report)) == report


def test_working_timeframes_lists_the_alternative_grids_shortest_first() -> None:
    """The refusal must name what WOULD work, cheapest grid first."""
    assert working_timeframes("momentum", {}, warmup_candles=200) == ["4h", "1d"]
    assert working_timeframes("momentum", {}, warmup_candles=28) == []
    assert working_timeframes("momentum", {}, warmup_candles=29) == ["1d"]
    assert working_timeframes("momentum", {}, warmup_candles=40321) == sorted(
        SUPPORTED_TIMEFRAMES, key=timeframe_minutes
    )


def test_working_timeframes_is_total() -> None:
    """An unknown strategy or rejected parameters answer "nothing works"."""
    assert working_timeframes("does-not-exist", {}, warmup_candles=10**9) == []
    assert (
        working_timeframes(
            "momentum", {"fast_days": 10, "mid_days": 5, "slow_days": 3}, warmup_candles=10**9
        )
        == []
    )


def test_every_public_answer_is_pure_and_repeatable() -> None:
    """Same inputs, same outputs: no clock, no cache, no hidden state."""
    target = profile()
    first = (
        candles_per_day("1m"),
        required_candles_for(target),
        tuple(
            finding.to_dict() for finding in profile_warmup_findings(target, history_candles=300)
        ),
        warmup_report(target, history_candles=300),
        tuple(working_timeframes("momentum", {}, warmup_candles=200)),
    )
    second = (
        candles_per_day("1m"),
        required_candles_for(target),
        tuple(
            finding.to_dict() for finding in profile_warmup_findings(target, history_candles=300)
        ),
        warmup_report(target, history_candles=300),
        tuple(working_timeframes("momentum", {}, warmup_candles=200)),
    )
    assert first == second
    # and the profile itself is never mutated by any of it
    assert target.model_dump() == profile().model_dump()
