"""Tests of the realtime safety layer: limits, kill switch and the live gate.

Everything here is offline and deterministic: the clock is a
:class:`~trading_platform.realtime.clock.ManualClock` advanced explicitly, the
meta store is a local in-memory fake (this package never imports
``realtime.store``) and no test touches the network or the wall clock.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from trading_platform.config.models import (
    ProfileConfig,
    RealtimeConfig,
    RiskLimitsConfig,
    resolve_platform_initial_balance,
)
from trading_platform.core.errors import KillSwitchActiveError, LiveTradingForbiddenError
from trading_platform.realtime.clock import ManualClock
from trading_platform.realtime.models import OrderRequest, OrderSide, OrderType, RunMode
from trading_platform.realtime.risk import (
    ENV_KILL_SWITCH,
    KILL_SWITCH_CHANGED_META_KEY,
    KILL_SWITCH_META_KEY,
    KILL_SWITCH_REASON_META_KEY,
    KillSwitch,
    KillSwitchState,
    LiveTradingGate,
    MetaStore,
    PlatformRiskLimits,
    PlatformRiskState,
    RiskDecision,
    RiskLimits,
    RiskManager,
)

LOGGER_NAME = "trading_platform.realtime.risk"
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The profiles the shipped example used to declare, as an inline literal.  The
#: JSON document that carried them is deleted -- the profile set is a table of
#: the SQLite state store, and no importer exists -- so the wallet and platform
#: cap behaviour it pinned is pinned against the configuration models directly.
EXAMPLE_PROFILE_PAYLOADS: tuple[dict[str, Any], ...] = (
    {
        "id": "btc-paper",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "strategy": "basic",
        "mode": "paper",
        "initial_balance": 10_000.0,
        "stake_amount": 1_000.0,
        "exchange": "binance",
        "warmup_candles": 200,
        "poll_interval_seconds": 5.0,
        "allocation": 10_000.0,
        "risk": {
            "max_position_notional": 5_000.0,
            "max_order_notional": 1_000.0,
            "max_open_positions": 1,
            "max_daily_loss": 500.0,
            "max_drawdown_pct": 0.25,
            "max_daily_trades": 10,
        },
    },
    {
        "id": "eth-paper",
        "symbol": "ETH/USDT",
        "timeframe": "4h",
        "strategy": "basic",
        "mode": "paper",
        "initial_balance": 5_000.0,
        "stake_amount": 500.0,
        "exchange": "binance",
        "warmup_candles": 300,
        "poll_interval_seconds": 10.0,
        "allocation": 5_000.0,
        "risk": {
            "max_position_notional": 2_500.0,
            "max_order_notional": 500.0,
            "max_open_positions": 1,
            "max_daily_loss": 250.0,
            "max_drawdown_pct": 0.2,
            "max_daily_trades": 6,
        },
    },
)

#: The engine settings the same example declared, as a plain keyword mapping.
EXAMPLE_REALTIME_SETTINGS: dict[str, Any] = {
    "state_db": "data/realtime/state.db",
    "logs_dir": "data/realtime/logs",
    "data_dir": "data",
    "cache_dir": "data/cache",
    "format": "parquet",
    "allow_network": True,
    "csv_dir": None,
    "start_at": "2024-01-01T00:00:00+00:00",
    "history_candles": 300,
    "poll_interval_seconds": 5.0,
    "stream_poll_timeout_seconds": 10.0,
    "max_stream_reconnects": 5,
    "reconnect_backoff_seconds": 1.0,
    "reconcile_interval_seconds": 60.0,
    "risk_free_rate": 0.0,
    "benchmark_variant": "buy_and_hold",
    "kill_switch_file": "data/realtime/KILL_SWITCH",
    "platform_initial_balance": 15_000.0,
    "platform_max_total_notional": 12_000.0,
    "platform_max_daily_loss": 1_000.0,
}


# ---------------------------------------------------------------------------
# local fakes -- never import the persistence module (wp3) from here
# ---------------------------------------------------------------------------


class FakeMetaStore:
    """Two-line in-memory :class:`MetaStore` satisfying the protocol structurally."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []

    def get_meta(self, key: str) -> str | None:
        return self.rows.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.rows[key] = str(value)
        self.writes.append((key, str(value)))


class RecordingWallet:
    """Local stand-in of the shared wallet: the risk layer may only read it.

    It deliberately implements ``spendable`` **and nothing else**: the risk layer
    funds an order by asking the wallet what is spendable, it never debits it, so
    any other attribute access is turned into an explicit assertion failure.
    """

    def __init__(self, available: float) -> None:
        self._available = float(available)
        self.calls: list[str] = []

    def spendable(self) -> float:
        self.calls.append("spendable")
        return self._available

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"the risk layer must only read spendable(), touched {name!r}")


class LiveVenueWallet:
    """Local stand-in of the live wallet: the venue owns the cash, read-only here.

    ``spendable`` answers with the balance the venue reported, while the local
    ledger stays where it is: a live funding check reads the account, it never
    debits anything locally.
    """

    def __init__(self, venue_balance: float, *, local_cash: float = 0.0) -> None:
        self.venue_balance = float(venue_balance)
        self.cash = float(local_cash)
        self.reads = 0

    def spendable(self) -> float:
        self.reads += 1
        return self.venue_balance


def _clock() -> ManualClock:
    """Return a deterministic clock anchored on the default manual instant."""
    return ManualClock()


def _limits(**overrides: Any) -> RiskLimits:
    """Return permissive limits with only ``overrides`` enforced."""
    base: dict[str, Any] = {
        "max_position_notional": None,
        "max_order_notional": None,
        "max_open_positions": 10,
        "max_daily_loss": None,
        "max_drawdown_pct": None,
        "max_daily_trades": None,
    }
    base.update(overrides)
    return RiskLimits(**base)


def _request(quantity: float = 1.0, *, profile_id: str = "btc-paper") -> OrderRequest:
    """Return a market buy request used by every risk assertion."""
    return OrderRequest(
        profile_id=profile_id,
        client_order_id="btc-paper-BTCUSDT-1704067200000-1",
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        quantity=quantity,
        mode=RunMode.PAPER,
    )


def _verdict(
    manager: RiskManager,
    *,
    quantity: float = 1.0,
    reference_price: float = 100.0,
    equity: float = 1000.0,
    open_positions: int = 0,
    position_notional: float = 0.0,
    daily_pnl: float = 0.0,
    daily_trades: int = 0,
    peak_equity: float = 1000.0,
    closes_position: bool = False,
) -> RiskDecision:
    """Run one order through an existing manager and return its verdict."""
    return manager.check_order(
        _request(quantity),
        reference_price=reference_price,
        equity=equity,
        open_positions=open_positions,
        position_notional=position_notional,
        daily_pnl=daily_pnl,
        daily_trades=daily_trades,
        peak_equity=peak_equity,
        closes_position=closes_position,
    )


def _decision(
    limits: RiskLimits,
    *,
    quantity: float = 1.0,
    reference_price: float = 100.0,
    equity: float = 1000.0,
    open_positions: int = 0,
    position_notional: float = 0.0,
    daily_pnl: float = 0.0,
    daily_trades: int = 0,
    peak_equity: float = 1000.0,
    closes_position: bool = False,
    kill_switch: KillSwitch | None = None,
    platform: PlatformRiskLimits | None = None,
    wallet: Any = None,
    state: PlatformRiskState | None = None,
) -> RiskDecision:
    """Run one order through a fresh manager and return its verdict."""
    manager = RiskManager(
        limits,
        clock=_clock(),
        kill_switch=kill_switch,
        platform=platform,
        wallet=wallet,
        state=state,
    )
    return _verdict(
        manager,
        quantity=quantity,
        reference_price=reference_price,
        equity=equity,
        open_positions=open_positions,
        position_notional=position_notional,
        daily_pnl=daily_pnl,
        daily_trades=daily_trades,
        peak_equity=peak_equity,
        closes_position=closes_position,
    )


# ---------------------------------------------------------------------------
# 1. one test per limit kind
# ---------------------------------------------------------------------------


def test_max_order_notional_rejects_and_names_the_limit() -> None:
    decision = _decision(_limits(max_order_notional=100.0), quantity=2.0, reference_price=100.0)
    assert decision.allowed is False
    assert decision.limit == "max_order_notional"
    assert "200.00" in decision.reason
    assert "100.00" in decision.reason


def test_max_position_notional_rejects_and_names_the_limit() -> None:
    decision = _decision(
        _limits(max_position_notional=100.0),
        quantity=1.0,
        reference_price=100.0,
        position_notional=50.0,
    )
    assert decision.allowed is False
    assert decision.limit == "max_position_notional"
    assert "150.00" in decision.reason
    assert "100.00" in decision.reason


def test_max_open_positions_rejects_and_names_the_limit() -> None:
    decision = _decision(_limits(max_open_positions=1), open_positions=1)
    assert decision.allowed is False
    assert decision.limit == "max_open_positions"
    assert "open positions 2" in decision.reason
    assert "max_open_positions 1" in decision.reason


def test_max_daily_loss_rejects_and_names_the_limit() -> None:
    decision = _decision(_limits(max_daily_loss=50.0), daily_pnl=-75.0)
    assert decision.allowed is False
    assert decision.limit == "max_daily_loss"
    assert "75.00" in decision.reason
    assert "50.00" in decision.reason


def test_max_drawdown_pct_rejects_and_names_the_limit() -> None:
    decision = _decision(
        _limits(max_drawdown_pct=0.1), peak_equity=1000.0, equity=800.0, daily_pnl=0.0
    )
    assert decision.allowed is False
    assert decision.limit == "max_drawdown_pct"
    assert "0.2000" in decision.reason
    assert "0.1000" in decision.reason


def test_max_daily_trades_rejects_and_names_the_limit() -> None:
    decision = _decision(_limits(max_daily_trades=3), daily_trades=3)
    assert decision.allowed is False
    assert decision.limit == "max_daily_trades"
    assert "daily trades 3" in decision.reason
    assert "max_daily_trades 3" in decision.reason


# ---------------------------------------------------------------------------
# 2. frozen precedence and reachability
# ---------------------------------------------------------------------------


def test_nominal_request_is_allowed() -> None:
    decision = _decision(_limits())
    assert decision.allowed is True
    assert decision.limit == ""
    assert decision.reason == ""


def test_kill_switch_precedes_every_notional_limit() -> None:
    kill_switch = KillSwitch(None, clock=_clock())
    kill_switch.engage("operator halt")
    decision = _decision(
        _limits(max_order_notional=1.0, max_daily_trades=0),
        quantity=10.0,
        daily_trades=99,
        kill_switch=kill_switch,
    )
    assert decision.allowed is False
    assert decision.limit == "kill_switch"
    assert "operator halt" in decision.reason


def test_order_notional_precedes_position_and_count_limits() -> None:
    decision = _decision(
        _limits(max_order_notional=10.0, max_position_notional=10.0, max_open_positions=0),
        quantity=5.0,
        reference_price=100.0,
        open_positions=7,
        position_notional=900.0,
    )
    assert decision.limit == "max_order_notional"


def test_position_notional_precedes_open_positions() -> None:
    decision = _decision(
        _limits(max_position_notional=100.0, max_open_positions=0),
        quantity=5.0,
        reference_price=100.0,
        open_positions=7,
        position_notional=0.0,
    )
    assert decision.limit == "max_position_notional"


def test_open_positions_precedes_the_daily_budget_limits() -> None:
    decision = _decision(
        _limits(
            max_open_positions=0, max_daily_loss=1.0, max_drawdown_pct=0.001, max_daily_trades=0
        ),
        daily_pnl=-500.0,
        daily_trades=4,
    )
    assert decision.limit == "max_open_positions"


def test_daily_loss_precedes_drawdown_and_daily_trades() -> None:
    decision = _decision(
        _limits(max_daily_loss=1.0, max_drawdown_pct=0.001, max_daily_trades=0),
        equity=1.0,
        peak_equity=1000.0,
        daily_pnl=-500.0,
        daily_trades=4,
    )
    assert decision.limit == "max_daily_loss"


def test_drawdown_precedes_daily_trades() -> None:
    decision = _decision(
        _limits(max_drawdown_pct=0.001, max_daily_trades=0),
        equity=1.0,
        peak_equity=1000.0,
        daily_trades=4,
    )
    assert decision.limit == "max_drawdown_pct"


def test_closing_an_order_bypasses_the_position_caps() -> None:
    decision = _decision(
        _limits(max_position_notional=1.0, max_open_positions=0),
        quantity=10.0,
        reference_price=100.0,
        open_positions=5,
        position_notional=5000.0,
        closes_position=True,
    )
    assert decision.allowed is True


@pytest.mark.parametrize(
    ("limits", "context", "expected_limit"),
    [
        (_limits(max_order_notional=10.0), {"quantity": 5.0}, "max_order_notional"),
        (_limits(max_daily_loss=1.0), {"daily_pnl": -500.0}, "max_daily_loss"),
        (
            _limits(max_drawdown_pct=0.001),
            {"equity": 1.0, "peak_equity": 1000.0},
            "max_drawdown_pct",
        ),
        (_limits(max_daily_trades=0), {"daily_trades": 0}, "max_daily_trades"),
    ],
)
def test_closing_a_position_still_honours_the_other_limits(
    limits: RiskLimits, context: dict[str, Any], expected_limit: str
) -> None:
    decision = _decision(limits, closes_position=True, **context)
    assert decision.allowed is False
    assert decision.limit == expected_limit


# ---------------------------------------------------------------------------
# 3. boundary values
# ---------------------------------------------------------------------------


def test_notional_exactly_at_the_limit_is_allowed() -> None:
    assert _decision(_limits(max_order_notional=100.0), quantity=1.0).allowed is True
    assert (
        _decision(_limits(max_position_notional=100.0), quantity=1.0, position_notional=0.0).allowed
        is True
    )


def test_open_positions_exactly_one_below_the_cap_is_allowed() -> None:
    assert _decision(_limits(max_open_positions=2), open_positions=1).allowed is True


def test_zero_open_positions_cap_refuses_any_entry() -> None:
    decision = _decision(_limits(max_open_positions=0), open_positions=0)
    assert decision.allowed is False
    assert decision.limit == "max_open_positions"


def test_zero_peak_equity_does_not_divide_by_zero() -> None:
    decision = _decision(
        _limits(max_drawdown_pct=0.1), peak_equity=0.0, equity=0.0, reference_price=100.0
    )
    assert decision.allowed is True


def test_equity_above_the_peak_is_not_a_drawdown() -> None:
    assert _decision(_limits(max_drawdown_pct=0.1), peak_equity=1000.0, equity=1500.0).allowed


def test_daily_loss_exactly_at_the_limit_is_allowed() -> None:
    assert _decision(_limits(max_daily_loss=50.0), daily_pnl=-50.0).allowed is True


def test_daily_profit_never_trips_the_daily_loss_limit() -> None:
    assert _decision(_limits(max_daily_loss=50.0), daily_pnl=500.0).allowed is True


# ---------------------------------------------------------------------------
# 4. kill switch
# ---------------------------------------------------------------------------


def test_local_fake_store_satisfies_the_meta_store_protocol() -> None:
    assert isinstance(FakeMetaStore(), MetaStore)


def test_engage_engages_and_persists_the_three_rows() -> None:
    store = FakeMetaStore()
    clock = _clock()
    kill_switch = KillSwitch(store, clock=clock)

    state = kill_switch.engage("operator halt")

    assert state.engaged is True
    assert state.reason == "operator halt"
    assert state.source == "store"
    assert state.changed_at is not None
    assert kill_switch.engaged() is True
    assert store.rows[KILL_SWITCH_META_KEY] == "1"
    assert store.rows[KILL_SWITCH_REASON_META_KEY] == "operator halt"
    assert store.rows[KILL_SWITCH_CHANGED_META_KEY] == clock.now().isoformat()
    assert kill_switch.state().to_dict()["engaged"] is True


def test_engagement_without_a_store_stays_in_process() -> None:
    kill_switch = KillSwitch(None, clock=_clock())
    assert kill_switch.engaged() is False
    assert kill_switch.state().source == "none"

    state = kill_switch.engage("no store here", source="cli")
    assert state.engaged is True
    assert state.source == "cli"
    assert state.reason == "no store here"


def test_engagement_survives_a_restart() -> None:
    store = FakeMetaStore()
    KillSwitch(store, clock=_clock()).engage("crash survived")

    restarted = KillSwitch(store, clock=ManualClock())
    assert restarted.engaged() is True
    assert restarted.state().reason == "crash survived"
    assert restarted.state().source == "store"


def test_release_clears_the_store_and_survives_a_restart() -> None:
    store = FakeMetaStore()
    kill_switch = KillSwitch(store, clock=_clock())
    kill_switch.engage("operator halt")

    state = kill_switch.release()

    assert state.engaged is False
    assert state.source == "none"
    assert store.rows[KILL_SWITCH_META_KEY] == "0"
    assert store.rows[KILL_SWITCH_REASON_META_KEY] == ""
    assert KillSwitch(store, clock=_clock()).engaged() is False


def test_release_without_a_store_clears_the_in_process_engagement() -> None:
    kill_switch = KillSwitch(None, clock=_clock())
    kill_switch.engage("operator halt")
    assert kill_switch.release().engaged is False
    assert kill_switch.engaged() is False


def test_file_beats_env_and_store(tmp_path: Path) -> None:
    store = FakeMetaStore()
    store.set_meta(KILL_SWITCH_META_KEY, "1")
    store.set_meta(KILL_SWITCH_REASON_META_KEY, "store reason")
    flag = tmp_path / "KILL"
    flag.write_text("halt from the file\n", encoding="utf-8")

    state = KillSwitch(
        store, clock=_clock(), flag_path=flag, environ={ENV_KILL_SWITCH: "1"}
    ).state()

    assert state.engaged is True
    assert state.source == "file"
    assert state.reason == "halt from the file"


def test_empty_flag_file_reports_the_default_reason(tmp_path: Path) -> None:
    flag = tmp_path / "KILL"
    flag.write_text("\n", encoding="utf-8")
    state = KillSwitch(None, clock=_clock(), flag_path=flag, environ={}).state()
    assert state.source == "file"
    assert state.reason == "kill switch file present"


def test_env_beats_store(tmp_path: Path) -> None:
    store = FakeMetaStore()
    store.set_meta(KILL_SWITCH_META_KEY, "1")
    store.set_meta(KILL_SWITCH_REASON_META_KEY, "store reason")

    state = KillSwitch(
        store,
        clock=_clock(),
        flag_path=tmp_path / "absent",
        environ={ENV_KILL_SWITCH: "YeS"},
    ).state()

    assert state.engaged is True
    assert state.source == "env"
    assert state.reason == f"{ENV_KILL_SWITCH} is set"


def test_store_is_the_last_resort(tmp_path: Path) -> None:
    store = FakeMetaStore()
    store.set_meta(KILL_SWITCH_META_KEY, "1")
    store.set_meta(KILL_SWITCH_REASON_META_KEY, "store reason")

    state = KillSwitch(
        store, clock=_clock(), flag_path=tmp_path / "absent", environ={ENV_KILL_SWITCH: "0"}
    ).state()

    assert state.source == "store"
    assert state.reason == "store reason"


def test_release_refuses_while_the_file_forces_it(tmp_path: Path) -> None:
    flag = tmp_path / "KILL"
    flag.write_text("halt\n", encoding="utf-8")
    kill_switch = KillSwitch(FakeMetaStore(), clock=_clock(), flag_path=flag, environ={})
    with pytest.raises(KillSwitchActiveError) as excinfo:
        kill_switch.release()
    assert "forced by the file" in str(excinfo.value)


def test_release_refuses_while_the_environment_forces_it() -> None:
    kill_switch = KillSwitch(None, clock=_clock(), environ={ENV_KILL_SWITCH: "on"})
    with pytest.raises(KillSwitchActiveError) as excinfo:
        kill_switch.release()
    assert "forced by the env" in str(excinfo.value)


def test_require_inactive_raises_with_the_reason() -> None:
    kill_switch = KillSwitch(FakeMetaStore(), clock=_clock())
    assert kill_switch.require_inactive() is None
    kill_switch.engage("halt for maintenance")
    with pytest.raises(KillSwitchActiveError) as excinfo:
        kill_switch.require_inactive()
    assert str(excinfo.value) == "halt for maintenance"


def test_kill_switch_state_is_frozen_and_serialisable() -> None:
    state = KillSwitchState(engaged=True, reason="halt", changed_at=None, source="api")
    with pytest.raises(FrozenInstanceError):
        state.engaged = False  # type: ignore[misc]
    assert json.loads(json.dumps(state.to_dict())) == {
        "engaged": True,
        "reason": "halt",
        "changed_at": None,
        "source": "api",
    }


# ---------------------------------------------------------------------------
# 5. an injected kill switch rejects through the risk manager
# ---------------------------------------------------------------------------


def test_injected_kill_switch_rejects_with_the_kill_switch_limit() -> None:
    kill_switch = KillSwitch(FakeMetaStore(), clock=_clock())
    kill_switch.engage("global halt")
    decision = _decision(
        _limits(max_order_notional=1.0, max_open_positions=0), quantity=5.0, kill_switch=kill_switch
    )
    assert decision.allowed is False
    assert decision.limit == "kill_switch"
    assert decision.reason == "global kill switch engaged: global halt"


def test_inactive_kill_switch_does_not_change_the_verdict() -> None:
    kill_switch = KillSwitch(FakeMetaStore(), clock=_clock())
    decision = _decision(_limits(), kill_switch=kill_switch)
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# 6. live trading gate
# ---------------------------------------------------------------------------


def _profile(mode: str) -> ProfileConfig:
    return ProfileConfig(id="btc-core", symbol="BTC/USDT", strategy="basic", mode=mode)  # type: ignore[arg-type]


def test_paper_profile_is_allowed_and_check_is_a_noop() -> None:
    gate = LiveTradingGate(environ={})
    profile = _profile("paper")
    assert gate.allowed(profile) is True
    assert gate.check(profile) is None


def test_live_profile_is_forbidden_without_the_opt_in() -> None:
    gate = LiveTradingGate(environ={})
    profile = _profile("live")
    assert gate.allowed(profile) is False
    with pytest.raises(LiveTradingForbiddenError) as excinfo:
        gate.check(profile)
    message = str(excinfo.value)
    assert "btc-core" in message
    assert LiveTradingGate.ENV_VAR in message
    assert LiveTradingGate.REQUIRED_VALUE in message


@pytest.mark.parametrize("value", ["", "i_understand_the_risk", "yes", "1", "I_UNDERSTAND"])
def test_live_profile_is_forbidden_with_a_wrong_value(value: str) -> None:
    gate = LiveTradingGate(environ={LiveTradingGate.ENV_VAR: value})
    assert gate.allowed(_profile("live")) is False
    with pytest.raises(LiveTradingForbiddenError):
        gate.check(_profile("live"))


def test_live_profile_is_allowed_with_the_exact_opt_in() -> None:
    gate = LiveTradingGate(environ={LiveTradingGate.ENV_VAR: LiveTradingGate.REQUIRED_VALUE})
    profile = _profile("live")
    assert gate.allowed(profile) is True
    assert gate.check(profile) is None


# ---------------------------------------------------------------------------
# 7. no-secret rule
# ---------------------------------------------------------------------------


def test_rejection_log_carries_only_the_limit_and_the_numbers(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "SUPER-SECRET-API-KEY-0123456789"
    monkeypatch.setenv("TB_LIVE_API_KEY", secret)
    monkeypatch.setenv("TB_LIVE_API_SECRET", "SUPER-SECRET-API-SECRET-0123456789")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        decision = _decision(_limits(max_order_notional=100.0), quantity=2.0)

    assert decision.allowed is False
    records = [record for record in caplog.records if record.name == LOGGER_NAME]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert message == (
        "risk limit max_order_notional: order notional 200.00 exceeds max_order_notional 100.00"
    )
    assert secret not in message
    assert "SECRET" not in message


def test_an_allowed_order_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert _decision(_limits()).allowed is True
    assert [record for record in caplog.records if record.name == LOGGER_NAME] == []


# ---------------------------------------------------------------------------
# payloads and configuration mapping
# ---------------------------------------------------------------------------


def test_limits_from_config_and_to_dict_round_trip() -> None:
    cfg = RiskLimitsConfig(
        max_position_notional=500.0,
        max_order_notional=100.0,
        max_open_positions=3,
        max_daily_loss=50.0,
        max_drawdown_pct=0.2,
        max_daily_trades=10,
    )
    limits = RiskLimits.from_config(cfg)
    assert limits.to_dict() == {
        "max_position_notional": 500.0,
        "max_order_notional": 100.0,
        "max_open_positions": 3,
        "max_daily_loss": 50.0,
        "max_drawdown_pct": 0.2,
        "max_daily_trades": 10,
    }
    assert json.loads(json.dumps(limits.to_dict())) == limits.to_dict()


def test_disabled_limits_stay_none_in_the_payload() -> None:
    payload = RiskLimits.from_config(RiskLimitsConfig()).to_dict()
    assert payload["max_position_notional"] is None
    assert payload["max_order_notional"] is None
    assert payload["max_daily_loss"] is None
    assert payload["max_drawdown_pct"] is None
    assert payload["max_daily_trades"] is None
    assert payload["max_open_positions"] == 1


def test_manager_payload_reports_the_kill_switch() -> None:
    free = RiskManager(_limits(max_order_notional=100.0), clock=_clock())
    assert free.limits.max_order_notional == 100.0
    assert free.to_dict()["kill_switch"] is False
    assert free.to_dict()["max_order_notional"] == 100.0

    kill_switch = KillSwitch(FakeMetaStore(), clock=_clock())
    kill_switch.engage("halt")
    guarded = RiskManager(_limits(), clock=_clock(), kill_switch=kill_switch)
    assert guarded.to_dict()["kill_switch"] is True


def test_decisions_are_frozen_and_serialisable() -> None:
    allowed = RiskDecision.allow()
    assert allowed.to_dict() == {"allowed": True, "reason": "", "limit": ""}
    rejected = RiskDecision.reject("max_daily_trades", "daily trades 3 reaches max_daily_trades 3")
    assert rejected.to_dict()["limit"] == "max_daily_trades"
    with pytest.raises(FrozenInstanceError):
        allowed.allowed = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        RiskLimits().max_open_positions = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 8. the shared platform wallet: the funding check
# ---------------------------------------------------------------------------


def test_check_order_keeps_its_per_profile_signature() -> None:
    """The platform checks read the injected objects, never a new parameter."""
    parameters = inspect.signature(RiskManager.check_order).parameters
    assert list(parameters) == [
        "self",
        "request",
        "reference_price",
        "equity",
        "open_positions",
        "position_notional",
        "daily_pnl",
        "daily_trades",
        "peak_equity",
        "closes_position",
    ]
    assert parameters["closes_position"].default is False


def test_constructor_seams_are_keyword_only_and_appended_after_the_kill_switch() -> None:
    parameters = inspect.signature(RiskManager.__init__).parameters
    assert list(parameters) == [
        "self",
        "limits",
        "clock",
        "kill_switch",
        "platform",
        "wallet",
        "state",
    ]
    assert parameters["clock"].default is inspect.Parameter.empty
    for name in ("clock", "kill_switch", "platform", "wallet", "state"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    for name in ("platform", "wallet", "state"):
        assert parameters[name].default is None


def test_funding_is_refused_when_the_notional_exceeds_the_wallet_cash() -> None:
    wallet = RecordingWallet(99.99)
    decision = _decision(_limits(), wallet=wallet)
    assert decision.allowed is False
    assert decision.limit == "platform_wallet"
    assert decision.reason == (
        "platform wallet cannot fund order: requires 100.00 USDT, available 99.99 USDT"
    )
    assert wallet.calls == ["spendable"]


def test_funding_is_allowed_when_the_notional_equals_the_wallet_cash() -> None:
    wallet = RecordingWallet(100.0)
    assert _decision(_limits(), wallet=wallet).allowed is True
    assert wallet.calls == ["spendable"]


def test_an_empty_wallet_refuses_every_entry() -> None:
    wallet = RecordingWallet(0.0)
    decision = _decision(_limits(), wallet=wallet)
    assert decision.limit == "platform_wallet"
    assert decision.reason == (
        "platform wallet cannot fund order: requires 100.00 USDT, available 0.00 USDT"
    )


def test_the_funding_check_precedes_every_per_profile_limit() -> None:
    """The wallet is consulted first: an unfundable order never reaches a limit."""
    wallet = RecordingWallet(10.0)
    decision = _decision(
        _limits(
            max_order_notional=1.0,
            max_position_notional=1.0,
            max_open_positions=0,
            max_daily_loss=1.0,
            max_drawdown_pct=0.001,
            max_daily_trades=0,
        ),
        quantity=10.0,
        reference_price=100.0,
        open_positions=5,
        position_notional=900.0,
        daily_pnl=-500.0,
        daily_trades=4,
        equity=1.0,
        peak_equity=1000.0,
        wallet=wallet,
    )
    assert decision.limit == "platform_wallet"
    assert wallet.calls == ["spendable"]


def test_closing_orders_are_never_funding_checked() -> None:
    """An exit releases cash, it never needs cash: the wallet is not even read."""
    wallet = RecordingWallet(0.0)
    decision = _decision(_limits(), wallet=wallet, closes_position=True)
    assert decision.allowed is True
    assert wallet.calls == []


def test_a_live_wallet_is_read_only_and_funded_from_the_venue() -> None:
    """Live mode: the shared wallet mirrors the venue account, nothing is debited."""
    wallet = LiveVenueWallet(250.0)
    refused = _decision(_limits(), quantity=3.0, reference_price=100.0, wallet=wallet)
    assert refused.limit == "platform_wallet"
    assert refused.reason == (
        "platform wallet cannot fund order: requires 300.00 USDT, available 250.00 USDT"
    )
    assert _decision(_limits(), quantity=2.5, reference_price=100.0, wallet=wallet).allowed is True
    assert wallet.reads == 2
    # The local ledger is never the funding source of a live account, so the
    # checks above moved nothing.
    assert wallet.cash == 0.0


def test_a_live_wallet_refuses_once_the_venue_reports_an_empty_account() -> None:
    wallet = LiveVenueWallet(0.0)
    decision = _decision(_limits(), wallet=wallet)
    assert decision.limit == "platform_wallet"
    assert "available 0.00 USDT" in decision.reason


def test_a_funding_refusal_is_a_plain_risk_decision() -> None:
    """It maps to the very same exception as a per-profile refusal (limit != kill_switch)."""
    wallet = RecordingWallet(0.0)
    decision = _decision(_limits(), wallet=wallet)
    assert decision.to_dict() == {
        "allowed": False,
        "reason": "platform wallet cannot fund order: requires 100.00 USDT, available 0.00 USDT",
        "limit": "platform_wallet",
    }
    assert decision.limit != "kill_switch"


def test_a_funding_refusal_is_logged_like_a_per_profile_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        _decision(_limits(), wallet=RecordingWallet(0.0))

    records = [record for record in caplog.records if record.name == LOGGER_NAME]
    assert [record.getMessage() for record in records] == [
        "risk limit platform_wallet: platform wallet cannot fund order: "
        "requires 100.00 USDT, available 0.00 USDT"
    ]


# ---------------------------------------------------------------------------
# 9. platform-wide risk caps
# ---------------------------------------------------------------------------


def test_platform_limits_from_config_and_to_dict_round_trip() -> None:
    cfg = RealtimeConfig(
        platform_initial_balance=15_000.0,
        platform_max_total_notional=12_000.0,
        platform_max_daily_loss=1_000.0,
    )
    limits = PlatformRiskLimits.from_config(cfg)
    assert limits.max_total_notional == 12_000.0
    assert limits.max_daily_loss == 1_000.0
    assert limits.to_dict() == {"max_total_notional": 12_000.0, "max_daily_loss": 1_000.0}
    assert json.loads(json.dumps(limits.to_dict())) == limits.to_dict()
    with pytest.raises(FrozenInstanceError):
        limits.max_daily_loss = 1.0  # type: ignore[misc]


def test_platform_limits_are_absent_by_default() -> None:
    """An untouched configuration declares no platform cap at all."""
    limits = PlatformRiskLimits.from_config(RealtimeConfig())
    assert limits.max_total_notional is None
    assert limits.max_daily_loss is None
    assert limits.to_dict() == {"max_total_notional": None, "max_daily_loss": None}
    assert limits == PlatformRiskLimits()
    assert RiskManager(_limits(), clock=_clock()).platform_limits is None


def test_platform_total_notional_refusal_names_the_cap() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("eth-paper", exposure=500.0, daily_pnl=0.0)
    decision = _decision(
        _limits(), platform=PlatformRiskLimits(max_total_notional=550.0), state=state
    )
    assert decision.allowed is False
    assert decision.limit == "platform_max_total_notional"
    assert decision.reason == (
        "platform exposure 600.00 exceeds platform_max_total_notional 550.00"
    )


def test_platform_total_notional_exactly_at_the_cap_is_allowed() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("eth-paper", exposure=500.0, daily_pnl=0.0)
    decision = _decision(
        _limits(), platform=PlatformRiskLimits(max_total_notional=600.0), state=state
    )
    assert decision.allowed is True


def test_platform_daily_loss_refusal_names_the_cap() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=0.0, daily_pnl=-400.0)
    state.update("eth-paper", exposure=0.0, daily_pnl=-600.0)
    decision = _decision(_limits(), platform=PlatformRiskLimits(max_daily_loss=999.0), state=state)
    assert decision.allowed is False
    assert decision.limit == "platform_max_daily_loss"
    assert decision.reason == "platform daily loss 1000.00 exceeds platform_max_daily_loss 999.00"


def test_platform_daily_loss_exactly_at_the_cap_is_allowed() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=0.0, daily_pnl=-1000.0)
    decision = _decision(_limits(), platform=PlatformRiskLimits(max_daily_loss=1000.0), state=state)
    assert decision.allowed is True


def test_platform_profit_never_trips_the_platform_daily_loss() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=0.0, daily_pnl=5000.0)
    decision = _decision(_limits(), platform=PlatformRiskLimits(max_daily_loss=1000.0), state=state)
    assert decision.allowed is True


def test_platform_caps_are_inert_without_configuration() -> None:
    """No platform limits, no wallet: an untouched configuration trades as before."""
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=10_000_000.0, daily_pnl=-10_000_000.0)
    decision = _decision(_limits(), state=state)
    assert decision.allowed is True
    assert decision.limit == ""


def test_platform_caps_are_enforced_without_any_published_state() -> None:
    """A configured cap reads as "nothing published yet", which is a zero."""
    over_notional = _decision(_limits(), platform=PlatformRiskLimits(max_total_notional=50.0))
    assert over_notional.limit == "platform_max_total_notional"
    assert (
        over_notional.reason == "platform exposure 100.00 exceeds platform_max_total_notional 50.00"
    )
    assert _decision(_limits(), platform=PlatformRiskLimits(max_daily_loss=50.0)).allowed is True


def test_the_platform_state_is_never_reached_through_a_global() -> None:
    """An aggregation the manager was not given cannot influence a verdict."""
    foreign = PlatformRiskState(clock=_clock())
    foreign.update("btc-paper", exposure=1e9, daily_pnl=-1e9)
    manager = RiskManager(
        # Both caps are wide enough for the 100.00 USDT order on their own: only
        # the aggregation the manager never received could have refused it.
        _limits(),
        clock=_clock(),
        platform=PlatformRiskLimits(max_total_notional=1000.0, max_daily_loss=1000.0),
    )
    assert _verdict(manager).allowed is True
    assert foreign.total_exposure == 1e9
    assert foreign.total_daily_pnl == -1e9


def test_closing_orders_skip_the_platform_notional_cap() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=1_000_000.0, daily_pnl=0.0)
    decision = _decision(
        _limits(),
        platform=PlatformRiskLimits(max_total_notional=1.0),
        state=state,
        closes_position=True,
    )
    assert decision.allowed is True


def test_closing_orders_still_honour_the_platform_daily_loss() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=0.0, daily_pnl=-500.0)
    decision = _decision(
        _limits(),
        platform=PlatformRiskLimits(max_daily_loss=100.0),
        state=state,
        closes_position=True,
    )
    assert decision.allowed is False
    assert decision.limit == "platform_max_daily_loss"


def test_manager_payload_is_unchanged_by_the_platform_seams() -> None:
    """``to_dict`` keeps reporting exactly the per-profile limits plus the switch."""
    manager = RiskManager(
        _limits(max_order_notional=100.0),
        clock=_clock(),
        platform=PlatformRiskLimits(max_total_notional=1.0, max_daily_loss=2.0),
        wallet=RecordingWallet(0.0),
        state=PlatformRiskState(clock=_clock()),
    )
    assert manager.platform_limits == PlatformRiskLimits(max_total_notional=1.0, max_daily_loss=2.0)
    assert manager.to_dict() == {
        "max_position_notional": None,
        "max_order_notional": 100.0,
        "max_open_positions": 10,
        "max_daily_loss": None,
        "max_drawdown_pct": None,
        "max_daily_trades": None,
        "kill_switch": False,
    }


# ---------------------------------------------------------------------------
# 10. the frozen evaluation order, platform seams included
# ---------------------------------------------------------------------------


def test_kill_switch_precedes_the_platform_checks() -> None:
    kill_switch = KillSwitch(None, clock=_clock())
    kill_switch.engage("platform-wide halt")
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=1e6, daily_pnl=-1e6)
    decision = _decision(
        _limits(max_order_notional=1.0, max_open_positions=0),
        quantity=10.0,
        reference_price=100.0,
        kill_switch=kill_switch,
        platform=PlatformRiskLimits(max_total_notional=1.0, max_daily_loss=1.0),
        wallet=RecordingWallet(0.0),
        state=state,
    )
    assert decision.allowed is False
    assert decision.limit == "kill_switch"
    assert decision.reason == "global kill switch engaged: platform-wide halt"


def test_platform_wallet_precedes_the_platform_caps_and_every_per_profile_limit() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=1e6, daily_pnl=-1e6)
    decision = _decision(
        _limits(
            max_order_notional=1.0,
            max_position_notional=1.0,
            max_open_positions=0,
            max_daily_loss=1.0,
            max_drawdown_pct=0.001,
            max_daily_trades=0,
        ),
        quantity=10.0,
        reference_price=100.0,
        open_positions=5,
        position_notional=900.0,
        daily_pnl=-500.0,
        daily_trades=4,
        equity=1.0,
        peak_equity=1000.0,
        platform=PlatformRiskLimits(max_total_notional=1.0, max_daily_loss=1.0),
        wallet=RecordingWallet(10.0),
        state=state,
    )
    assert decision.allowed is False
    assert decision.limit == "platform_wallet"


def test_platform_total_notional_precedes_the_platform_daily_loss() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=1e6, daily_pnl=-1e6)
    decision = _decision(
        _limits(max_order_notional=1.0, max_open_positions=0),
        platform=PlatformRiskLimits(max_total_notional=1.0, max_daily_loss=1.0),
        wallet=RecordingWallet(1e9),
        state=state,
    )
    assert decision.allowed is False
    assert decision.limit == "platform_max_total_notional"


def test_platform_daily_loss_precedes_every_per_profile_limit() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("btc-paper", exposure=0.0, daily_pnl=-1e6)
    decision = _decision(
        _limits(
            max_order_notional=1.0,
            max_position_notional=1.0,
            max_open_positions=0,
            max_daily_loss=1.0,
            max_drawdown_pct=0.001,
            max_daily_trades=0,
        ),
        quantity=10.0,
        reference_price=100.0,
        open_positions=5,
        position_notional=900.0,
        daily_pnl=-500.0,
        daily_trades=4,
        equity=1.0,
        peak_equity=1000.0,
        platform=PlatformRiskLimits(max_total_notional=1e12, max_daily_loss=1.0),
        wallet=RecordingWallet(1e12),
        state=state,
    )
    assert decision.allowed is False
    assert decision.limit == "platform_max_daily_loss"


def test_a_funded_order_inside_every_cap_is_allowed() -> None:
    state = PlatformRiskState(clock=_clock())
    state.update("eth-paper", exposure=500.0, daily_pnl=-5.0)
    decision = _decision(
        _limits(max_order_notional=1000.0, max_position_notional=1000.0, max_open_positions=5),
        platform=PlatformRiskLimits(max_total_notional=12_000.0, max_daily_loss=1_000.0),
        wallet=RecordingWallet(15_000.0),
        state=state,
    )
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# 11. the platform aggregation
# ---------------------------------------------------------------------------


def test_platform_state_totals_start_at_zero() -> None:
    state = PlatformRiskState(clock=_clock())
    assert state.total_exposure == 0.0
    assert state.total_daily_pnl == 0.0
    assert state.to_dict() == {"total_exposure": 0.0, "total_daily_pnl": 0.0, "profiles": {}}
    # The clock seam is optional: the aggregation itself is time-free.
    assert PlatformRiskState().to_dict() == state.to_dict()


def test_platform_state_update_keeps_the_last_value_of_each_profile() -> None:
    state = PlatformRiskState()
    state.update("btc-paper", exposure=100.0, daily_pnl=-10.0)
    state.update("btc-paper", exposure=250.0, daily_pnl=-15.0)
    assert state.total_exposure == 250.0
    assert state.total_daily_pnl == -15.0

    state.update("eth-paper", exposure=50.0, daily_pnl=5.0)
    assert state.to_dict() == {
        "total_exposure": 300.0,
        "total_daily_pnl": -10.0,
        "profiles": {
            "btc-paper": {"exposure": 250.0, "daily_pnl": -15.0},
            "eth-paper": {"exposure": 50.0, "daily_pnl": 5.0},
        },
    }
    assert json.loads(json.dumps(state.to_dict())) == state.to_dict()


def test_platform_state_forget_drops_one_profile() -> None:
    state = PlatformRiskState()
    state.update("btc-paper", exposure=100.0, daily_pnl=-10.0)
    state.update("eth-paper", exposure=50.0, daily_pnl=5.0)

    state.forget("btc-paper")

    assert state.total_exposure == 50.0
    assert state.total_daily_pnl == 5.0
    assert state.to_dict()["profiles"] == {"eth-paper": {"exposure": 50.0, "daily_pnl": 5.0}}
    assert state.forget("unknown-profile") is None
    assert state.total_exposure == 50.0


def test_platform_state_updates_are_atomic_under_concurrency() -> None:
    """Eight profiles publishing at once, one reader watching every snapshot."""
    state = PlatformRiskState(clock=_clock())
    profiles = [f"profile-{index}" for index in range(8)]
    rounds = 200
    barrier = threading.Barrier(len(profiles))
    stop = threading.Event()
    violations: list[dict[str, Any]] = []

    def publish(profile_id: str) -> None:
        barrier.wait()
        for _ in range(rounds):
            state.update(profile_id, exposure=100.0, daily_pnl=-1.0)

    def read() -> None:
        while not stop.is_set():
            snapshot = state.to_dict()
            if snapshot["total_exposure"] / 100.0 != -snapshot["total_daily_pnl"]:
                violations.append(snapshot)
            if len(snapshot["profiles"]) > len(profiles):
                violations.append(snapshot)

    writers = [threading.Thread(target=publish, args=(profile_id,)) for profile_id in profiles]
    reader = threading.Thread(target=read)
    reader.start()
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    stop.set()
    reader.join()

    assert violations == []
    assert state.total_exposure == 100.0 * len(profiles)
    assert state.total_daily_pnl == -float(len(profiles))
    assert set(state.to_dict()["profiles"]) == set(profiles)


# ---------------------------------------------------------------------------
# 12. publishing into the aggregation
# ---------------------------------------------------------------------------


def test_publish_platform_state_feeds_the_aggregation() -> None:
    state = PlatformRiskState(clock=_clock())
    manager = RiskManager(_limits(), clock=_clock(), state=state)

    manager.publish_platform_state("btc-paper", exposure=250.0, daily_pnl=-10.0)
    manager.publish_platform_state("eth-paper", exposure=100.0, daily_pnl=5.0)

    assert state.total_exposure == 350.0
    assert state.total_daily_pnl == -5.0


def test_publish_platform_state_replaces_the_previous_figures() -> None:
    state = PlatformRiskState(clock=_clock())
    manager = RiskManager(_limits(), clock=_clock(), state=state)

    manager.publish_platform_state("btc-paper", exposure=250.0, daily_pnl=-10.0)
    manager.publish_platform_state("btc-paper", exposure=300.0, daily_pnl=-20.0)

    assert state.total_exposure == 300.0
    assert state.total_daily_pnl == -20.0
    assert state.to_dict()["profiles"] == {"btc-paper": {"exposure": 300.0, "daily_pnl": -20.0}}


def test_publish_platform_state_without_a_state_is_a_noop() -> None:
    manager = RiskManager(_limits(), clock=_clock())
    assert manager.publish_platform_state("btc-paper", exposure=250.0, daily_pnl=-10.0) is None
    assert manager.publish_platform_state("", exposure=1.0, daily_pnl=1.0) is None


def test_published_figures_drive_the_platform_caps() -> None:
    """The runner publishes, the next order is measured against the aggregation."""
    state = PlatformRiskState(clock=_clock())
    manager = RiskManager(
        _limits(),
        clock=_clock(),
        platform=PlatformRiskLimits(max_total_notional=500.0, max_daily_loss=50.0),
        state=state,
    )
    manager.publish_platform_state("eth-paper", exposure=450.0, daily_pnl=0.0)
    assert manager.publish_platform_state("btc-paper", exposure=0.0, daily_pnl=0.0) is None

    over_exposure = _verdict(manager)
    assert over_exposure.limit == "platform_max_total_notional"

    manager.publish_platform_state("eth-paper", exposure=0.0, daily_pnl=-60.0)
    over_loss = _verdict(manager)
    assert over_loss.limit == "platform_max_daily_loss"
    assert over_loss.reason == "platform daily loss 60.00 exceeds platform_max_daily_loss 50.00"


# ---------------------------------------------------------------------------
# 13. the shipped example file declares the new fields
# ---------------------------------------------------------------------------


def test_example_profiles_declare_the_platform_wallet_and_caps() -> None:
    """The shipped example's wallet and platform caps, on the configuration models.

    The document that used to carry them is deleted (the profile set lives in the
    SQLite state store and nothing reads a profiles file), so the values are
    pinned against ``ProfileConfig`` and ``RealtimeConfig`` directly.  The
    *behaviour* these assertions protect is unchanged: the wallet still starts at
    exactly the sum of the allocations, so every profile keeps the share it
    declared.
    """
    profiles = [ProfileConfig.model_validate(payload) for payload in EXAMPLE_PROFILE_PAYLOADS]
    realtime = RealtimeConfig.model_validate(EXAMPLE_REALTIME_SETTINGS)

    assert [profile.allocation for profile in profiles] == [10_000.0, 5_000.0]
    assert [profile.effective_allocation for profile in profiles] == [10_000.0, 5_000.0]
    assert realtime.platform_initial_balance == 15_000.0
    assert realtime.platform_max_total_notional == 12_000.0
    assert realtime.platform_max_daily_loss == 1_000.0
    # The documented behaviour is unchanged: the wallet starts at exactly the sum
    # of the allocations, so every profile keeps the share it declared.
    assert resolve_platform_initial_balance(realtime, profiles) == 15_000.0
    assert sum(profile.effective_allocation for profile in profiles) == 15_000.0
    assert PlatformRiskLimits.from_config(realtime).to_dict() == {
        "max_total_notional": 12_000.0,
        "max_daily_loss": 1_000.0,
    }
