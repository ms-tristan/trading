"""Tests of the realtime safety layer: limits, kill switch and the live gate.

Everything here is offline and deterministic: the clock is a
:class:`~trading_platform.realtime.clock.ManualClock` advanced explicitly, the
meta store is a local in-memory fake (this package never imports
``realtime.store``) and no test touches the network or the wall clock.
"""

from __future__ import annotations

import json
import logging
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from trading_platform.config.models import ProfileConfig, RiskLimitsConfig
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
    RiskDecision,
    RiskLimits,
    RiskManager,
)

LOGGER_NAME = "trading_platform.realtime.risk"


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
) -> RiskDecision:
    """Run one order through a fresh manager and return its verdict."""
    manager = RiskManager(limits, clock=_clock(), kill_switch=kill_switch)
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
