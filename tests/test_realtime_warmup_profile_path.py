"""Where the profile is created and where it is fed: refusal, then delivery.

Two halves of the same contract live here.

*Refusal*: the profile control path -- the one the dashboard and ``realtime
check`` go through -- must **refuse** a profile whose strategy can never warm up
with the candles that profile asks the stream for (the silent no-op of the
incident), naming the strategy, the timeframe, the candles required and the
candles available, and listing the timeframes that would work instead.  A
profile that is merely incoherent (it asks for more candles than the stream
serves) is reported, never refused: refusing it would refuse working profiles.

*Delivery*: the per-profile ``history_candles`` override must reach the stream
the profile is really fed from -- the window ``PollingMarketStream`` asks its
provider for -- and an absent override must reproduce the realtime-level setting
exactly, byte for byte.

Offline and deterministic: the engine loop is a real thread running a real
asyncio loop (as ``tests/test_realtime_control.py`` does), the orchestrator is a
local fake, and the data provider is either an empty CSV directory or a
recording stub.  No network call, no wall clock, no fixture binary.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from trading_platform.config.models import ProfileConfig, RealtimeConfig
from trading_platform.core.constants import candle_delta
from trading_platform.core.errors import ConfigError, ProfileError
from trading_platform.data.synthetic import make_ohlcv
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.control import RuntimeProfileController
from trading_platform.realtime.observability import LOGGER_NAME
from trading_platform.realtime.stream import PollingMarketStream

#: How long a test may wait for the engine thread.
_WAIT = 5.0

SYMBOL = "BTC/USDT"

#: The defaults the incident's profile was created with.
DEFAULT_WARMUP = 200
DEFAULT_HISTORY = 300

#: Candles ``momentum`` needs on a 1m / 1h / 4h grid with default parameters.
REQUIRED_1M = 40321
REQUIRED_1H = 673
REQUIRED_4H = 169


def payload(**overrides: Any) -> dict[str, Any]:
    """Return the create body the dashboard sends, overridable field by field."""
    body: dict[str, Any] = {
        "profile_id": "momentum-1m",
        "symbol": SYMBOL,
        "timeframe": "1m",
        "strategy": "momentum",
        "mode": "paper",
        "initial_balance": 1000.0,
        "warmup_candles": DEFAULT_WARMUP,
    }
    body.update(overrides)
    return body


def profile(**overrides: Any) -> ProfileConfig:
    """Return a validated profile, overridable field by field."""
    body = payload(**overrides)
    body["id"] = body.pop("profile_id")
    return ProfileConfig.model_validate(body)


class EngineThread:
    """A real event loop owned by a background thread: the bridge's other side."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="warmup-engine-test-thread", daemon=True
        )

    def __enter__(self) -> EngineThread:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=_WAIT)
        self.loop.close()

    def bind(self, controller: RuntimeProfileController) -> None:
        """Bind the controller from *inside* the engine loop, like the CLI does."""
        future = asyncio.run_coroutine_threadsafe(self._bind(controller), self.loop)
        future.result(timeout=_WAIT)

    @staticmethod
    async def _bind(controller: RuntimeProfileController) -> None:
        controller.bind(asyncio.get_running_loop())


class FakeOrchestrator:
    """Local stand-in for the platform, recording every call it receives."""

    def __init__(self, *, declared: dict[str, ProfileConfig] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.profiles: dict[str, ProfileConfig] = dict(declared or {})

    async def add_profile(self, profile: ProfileConfig) -> Any:
        """Record a create and answer its identifier."""
        self.calls.append(("add_profile", profile))
        return str(profile.id)

    def profile_config(self, profile_id: str) -> ProfileConfig | None:
        """Return a declared profile, if the test declared one."""
        self.calls.append(("profile_config", profile_id))
        return self.profiles.get(str(profile_id))

    def names(self) -> list[str]:
        """Return the recorded call names, in order."""
        return [name for name, _argument in self.calls]


def controller(
    fake: FakeOrchestrator, *, history_candles: int | None = DEFAULT_HISTORY
) -> RuntimeProfileController:
    """Build a controller over one local fake platform."""
    return RuntimeProfileController(
        orchestrator=fake,  # type: ignore[arg-type]
        timeout_seconds=_WAIT,
        history_candles=history_candles,
    )


@pytest.fixture
def logs() -> Any:
    """Attach a capturing handler to the realtime logger for one test."""
    logger = logging.getLogger(LOGGER_NAME)
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def records_of(records: list[logging.LogRecord], event: str) -> list[logging.LogRecord]:
    """Return the captured records carrying one structured event."""
    return [record for record in records if getattr(record, "event", None) == event]


# ---------------------------------------------------------------------------
# 1. the create path refuses the impossible profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", ["1m", "15m", "1h"])
def test_the_create_path_refuses_a_momentum_profile_the_default_warmup_cannot_feed(
    tmp_path: Path, timeframe: str
) -> None:
    """1m, 15m and 1h with 200 warm-up candles: refused, with the arithmetic.

    Nothing is created and nothing reaches the engine loop: the refusal happens
    in the calling thread, before the command is marshalled.
    """
    fake = FakeOrchestrator()
    control = controller(fake)
    with EngineThread() as engine:
        engine.bind(control)
        with pytest.raises(ConfigError) as excinfo:
            control.create_profile(payload(timeframe=timeframe, profile_id="op-profile"))

    message = str(excinfo.value)
    assert "can never warm up" in message
    assert "'op-profile'" in message
    assert "'momentum'" in message
    assert f"'{timeframe}'" in message
    assert str(DEFAULT_WARMUP) in message
    assert "4h" in message and "1d" in message
    # The grid really is the grid of the timeframe the operator asked for.
    expected = {"1m": REQUIRED_1M, "15m": 2689, "1h": REQUIRED_1H}[timeframe]
    assert str(expected) in message
    assert fake.names() == []


def test_the_create_path_accepts_the_four_hour_grid() -> None:
    """4h needs 169 candles: the default 200-candle warm-up feeds it."""
    fake = FakeOrchestrator()
    control = controller(fake)
    with EngineThread() as engine:
        engine.bind(control)
        created = control.create_profile(
            payload(timeframe="4h", profile_id="momentum-4h", warmup_candles=DEFAULT_WARMUP)
        )

    assert created == "momentum-4h"
    assert fake.names() == ["profile_config", "add_profile"]
    forwarded = fake.calls[-1][1]
    assert isinstance(forwarded, ProfileConfig)
    assert forwarded.timeframe == "4h"
    assert forwarded.strategy == "momentum"
    assert forwarded.warmup_candles == DEFAULT_WARMUP
    assert forwarded.history_candles is None


def test_the_create_path_accepts_a_one_minute_profile_once_it_asks_for_enough() -> None:
    """The same 1m profile with ``warmup_candles=40321`` is created normally."""
    fake = FakeOrchestrator()
    control = controller(fake)
    with EngineThread() as engine:
        engine.bind(control)
        created = control.create_profile(payload(warmup_candles=REQUIRED_1M))

    assert created == "momentum-1m"
    forwarded = fake.calls[-1][1]
    assert forwarded.warmup_candles == REQUIRED_1M


def test_the_create_path_accepts_the_exact_boundary_inclusive() -> None:
    """``warmup_candles == required_candles`` is enough: the rule is inclusive."""
    fake = FakeOrchestrator()
    control = controller(fake)
    with EngineThread() as engine:
        engine.bind(control)
        assert control.create_profile(payload(timeframe="1h", warmup_candles=REQUIRED_1H)) == (
            "momentum-1m"
        )
    assert fake.names() == ["profile_config", "add_profile"]


def test_the_create_path_refuses_one_candle_short_of_the_boundary() -> None:
    """One candle below the requirement is still impossible."""
    fake = FakeOrchestrator()
    control = controller(fake)
    with EngineThread() as engine:
        engine.bind(control)
        with pytest.raises(ConfigError):
            control.create_profile(payload(timeframe="1h", warmup_candles=REQUIRED_1H - 1))
    assert fake.names() == []


def test_a_basic_profile_keeps_being_created_exactly_as_before() -> None:
    """The regression guard: ``basic`` declares no warm-up and is never refused.

    It is created on 1m with a 200-candle warm-up inside a 1-candle window --
    the seed of the existing ``realtime check`` scenario -- and the coherence
    mismatch must not turn a working profile into a failure.
    """
    fake = FakeOrchestrator()
    control = controller(fake, history_candles=1)
    with EngineThread() as engine:
        engine.bind(control)
        created = control.create_profile(
            payload(strategy="basic", timeframe="1m", profile_id="btc-paper")
        )

    assert created == "btc-paper"
    assert fake.names() == ["profile_config", "add_profile"]


def test_an_incoherent_profile_is_reported_and_still_created(logs: Any) -> None:
    """``warmup_candles`` 40321 inside a 300-candle window: a warning, not a refusal.

    The frame the strategy receives is bounded by ``warmup_candles``, so a
    smaller stream window does not by itself silence the profile -- and the
    operator's fix (the per-profile ``history_candles`` override) is named in the
    log record.
    """
    fake = FakeOrchestrator()
    control = controller(fake, history_candles=DEFAULT_HISTORY)
    with EngineThread() as engine:
        engine.bind(control)
        created = control.create_profile(payload(warmup_candles=REQUIRED_1M))

    assert created == "momentum-1m"
    reported = records_of(logs, "profile_warmup_coherence")
    assert len(reported) == 1
    assert reported[0].levelno == logging.WARNING
    context = dict(reported[0].context)
    assert context["warmup_candles"] == REQUIRED_1M
    assert context["history_candles"] == DEFAULT_HISTORY
    assert "history_candles" in context["message"]


def test_the_create_payload_carries_both_fields_into_the_profile() -> None:
    """``warmup_candles`` and ``history_candles`` are forwarded verbatim."""
    fake = FakeOrchestrator()
    control = controller(fake)
    with EngineThread() as engine:
        engine.bind(control)
        control.create_profile(
            payload(timeframe="1m", warmup_candles=REQUIRED_1M, history_candles=60000)
        )

    forwarded = fake.calls[-1][1]
    assert forwarded.warmup_candles == REQUIRED_1M
    assert forwarded.history_candles == 60000


def test_an_absent_override_is_not_invented_from_the_payload() -> None:
    """An omitted ``history_candles`` stays ``None``: the schema decides, not the path."""
    fake = FakeOrchestrator()
    control = controller(fake)
    with EngineThread() as engine:
        engine.bind(control)
        control.create_profile(payload(timeframe="4h"))
    forwarded = fake.calls[-1][1]
    assert forwarded.history_candles is None
    assert forwarded.effective_history_candles(DEFAULT_HISTORY) == DEFAULT_HISTORY


def test_the_controller_falls_back_to_the_realtime_history_setting(logs: Any) -> None:
    """A controller built without a window compares against the model default.

    The profile asks for 400 candles, the default realtime window is 300: the
    coherence finding fires against 300 -- not against the warm-up itself -- and
    the 4h profile is still created, because 169 candles are all it needs.
    """
    fake = FakeOrchestrator()
    control = controller(fake, history_candles=None)
    assert int(RealtimeConfig().history_candles) == DEFAULT_HISTORY
    with EngineThread() as engine:
        engine.bind(control)
        created = control.create_profile(payload(timeframe="4h", warmup_candles=400))

    assert created == "momentum-1m"
    reported = records_of(logs, "profile_warmup_coherence")
    assert len(reported) == 1
    assert dict(reported[0].context)["history_candles"] == DEFAULT_HISTORY
    assert dict(reported[0].context)["warmup_candles"] == 400


def test_a_duplicate_identifier_is_still_a_profile_error() -> None:
    """The warm-up check does not shadow the existing duplicate check.

    A feedable profile -- 4h, 200 candles -- that is already declared is refused
    with the historical :class:`ProfileError`, exactly as before.
    """
    declared = profile(timeframe="4h", profile_id="momentum-4h")
    fake = FakeOrchestrator(declared={"momentum-4h": declared})
    control = controller(fake)
    with EngineThread() as engine:
        engine.bind(control)
        with pytest.raises(ProfileError) as excinfo:
            control.create_profile(payload(timeframe="4h", profile_id="momentum-4h"))

    assert str(excinfo.value) == "profile already exists: 'momentum-4h'"
    assert fake.names() == ["profile_config"]


def test_the_warm_up_refusal_runs_before_the_engine_is_consulted() -> None:
    """An impossible profile never reaches the orchestrator, duplicate or not."""
    fake = FakeOrchestrator(declared={"momentum-1m": profile()})
    control = controller(fake)
    with EngineThread() as engine:
        engine.bind(control)
        with pytest.raises(ConfigError) as excinfo:
            control.create_profile(payload(profile_id="momentum-1m"))

    assert "can never warm up" in str(excinfo.value)
    assert fake.names() == []


# ---------------------------------------------------------------------------
# 2. the per-profile history override reaches the stream
# ---------------------------------------------------------------------------


def test_the_profile_history_override_reaches_the_stream(tmp_path: Path) -> None:
    """The window of the built stream is the profile's own, not a shared one."""
    from trading_platform.cli import _realtime_stream_factory

    realtime = RealtimeConfig(csv_dir=tmp_path, history_candles=DEFAULT_HISTORY)
    factory = _realtime_stream_factory(realtime, ManualClock(datetime(2024, 1, 2, tzinfo=UTC)))

    overridden = factory(profile(timeframe="1m", history_candles=42000))
    inherited = factory(profile(timeframe="4h", history_candles=None))

    assert isinstance(overridden, PollingMarketStream)
    assert overridden._history_candles == 42000
    assert inherited._history_candles == DEFAULT_HISTORY


def test_none_reproduces_the_realtime_setting_exactly(tmp_path: Path) -> None:
    """``history_candles=None`` is the previous behaviour, to the candle."""
    from trading_platform.cli import _realtime_stream_factory

    for setting in (1, 300, 777, 100_000):
        realtime = RealtimeConfig(csv_dir=tmp_path, history_candles=setting)
        factory = _realtime_stream_factory(realtime, ManualClock(datetime(2024, 1, 2, tzinfo=UTC)))
        stream = factory(profile(timeframe="1m", history_candles=None))
        assert stream._history_candles == setting


def test_two_profiles_of_the_same_factory_get_different_windows(tmp_path: Path) -> None:
    """The one shared realtime setting can no longer force every profile up."""
    from trading_platform.cli import _realtime_stream_factory

    realtime = RealtimeConfig(csv_dir=tmp_path, history_candles=DEFAULT_HISTORY)
    factory = _realtime_stream_factory(realtime, ManualClock(datetime(2024, 1, 2, tzinfo=UTC)))

    intraday = factory(profile(profile_id="momentum-1m", timeframe="1m", history_candles=42000))
    hourly = factory(profile(profile_id="btc-paper", timeframe="1h"))

    assert intraday._history_candles == 42000
    assert hourly._history_candles == DEFAULT_HISTORY


class RecordingProvider:
    """A market-data provider recording every window it is asked for."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[str, str, pd.Timestamp, pd.Timestamp]] = []

    def fetch_ohlcv(self, symbol: str, timeframe: str, since: Any, until: Any) -> pd.DataFrame:
        """Record the window and answer the prepared frame."""
        self.calls.append((symbol, timeframe, pd.Timestamp(since), pd.Timestamp(until)))
        return self.frame.copy()


def test_the_configured_window_is_what_the_provider_is_asked_for() -> None:
    """The private count is not a detail: it is the poll window the provider sees.

    With ``history_candles = 42000`` on a 1m grid, a poll of the stream covers
    exactly 42 000 minutes -- the window that lets an intraday profile warm up.
    """
    clock = ManualClock(datetime(2024, 1, 2, tzinfo=UTC))
    provider = RecordingProvider(make_ohlcv(2, start="2023-12-31T00:00:00Z", timeframe="1m"))
    stream = PollingMarketStream(
        provider,
        clock=clock,
        history_candles=42000,
        poll_interval_seconds=1.0,
        timeout_seconds=1.0,
        max_reconnects=1,
        reconnect_backoff_seconds=0.0,
    )

    asyncio.run(asyncio.wait_for(_poll(stream), timeout=_WAIT))

    assert len(provider.calls) == 1
    _symbol, _timeframe, since, until = provider.calls[0]
    assert until - since == candle_delta("1m") * 42000 == pd.Timedelta(minutes=42000)


async def _poll(stream: PollingMarketStream) -> Any:
    """Start the stream and take one poll, so the provider window is observed."""
    await stream.start()
    return await stream.next_candle(SYMBOL, "1m")


def test_the_default_window_stays_the_documented_three_hundred_candles() -> None:
    """Nothing about the existing default moved when the override was added."""
    assert RealtimeConfig().history_candles == 300
    assert ProfileConfig(id="x", symbol=SYMBOL).history_candles is None
    assert ProfileConfig(id="x", symbol=SYMBOL).effective_history_candles(300) == 300
