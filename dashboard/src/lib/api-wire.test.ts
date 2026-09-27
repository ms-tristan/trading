/**
 * Tests of the wire translation layer.
 *
 * The doubles below are **verbatim copies of payloads the running platform API
 * answered** (`GET /api/health`, `/api/account`, `/api/profiles`,
 * `/api/profiles/{id}`, `/api/strategies`, `/api/events`, `/api/settings` and the
 * mutating envelopes), trimmed to the fields the dashboard reads. They are the
 * regression net of the dashboard/API contract: a rename on the Python side
 * fails here instead of blanking a page.
 */

import { describe, expect, it } from "vitest";

import {
  asNumber,
  asRecord,
  toAccountResponse,
  toCatalogueApplyResponse,
  toDashboardSettings,
  toEventItem,
  toEventsResponse,
  toHealthStatus,
  toKillSwitchResponse,
  toProfileActionResponse,
  toProfileDetail,
  toProfilesResponse,
  toStrategyView,
} from "./api-wire";

/** `GET /api/health` of a running engine. */
const HEALTH = {
  status: "ok",
  version: "1.0.0",
  uptime_seconds: 23.59,
  profiles_total: 22,
  profiles_running: 10,
  profiles_healthy: 10,
  profiles_paper: 20,
  profiles_running_paper: 10,
  profiles_live: 2,
  profiles_running_live: 0,
  profiles_queued: 10,
  engine_slots_used: 10,
  engine_slots_total: 2,
  kill_switch_engaged: false,
  generated_at: "2026-09-27T17:06:09Z",
};

/** One row of `GET /api/profiles`. */
const PROFILE = {
  id: "basic-btc-1h",
  name: "Basic BTC 1h",
  strategy: "basic",
  strategy_title: "EMA cross baseline",
  strategy_category: "baseline",
  timeframe: "1h",
  mode: "paper",
  state: "running",
  state_reason: null,
  exchange: "binance",
  pairs: ["BTC/USDT"],
  initial_capital: 1000.0,
  portfolio_value: 1040.0,
  cash: 40.0,
  positions_value: 1000.0,
  profit_abs: 40.0,
  profit_pct: 0.04,
  realized_profit_abs: 25.0,
  unrealized_profit_abs: 15.0,
  open_trades: 1,
  closed_trades: 6,
  win_rate: 0.5,
  profit_factor: 1.4,
  max_drawdown_pct: 3.0,
  max_open_trades: 2,
  priority: 100,
  rank: 1,
  uptime_seconds: 3600,
  best_pair: "BTC/USDT",
  last_updated: "2026-09-27T17:07:00Z",
};

describe("toProfileView", () => {
  it("maps the wire names of one profile onto the view model", () => {
    const view = toProfilesResponse({ generated_at: "2026-09-27T17:06:09Z", profiles: [PROFILE] })
      .profiles[0];

    expect(view.id).toBe("basic-btc-1h");
    expect(view.strategy_title).toBe("EMA cross baseline");
    expect(view.profit_usdt).toBe(40);
    expect(view.profit_pct).toBeCloseTo(0.04, 6);
    expect(view.updated_at).toBe("2026-09-27T17:07:00Z");
    expect(view.pairs).toEqual(["BTC/USDT"]);
    expect(view.rank).toBe(1);
    expect(view.uptime_seconds).toBe(3600);
    expect(view.cash).toBe(40);
    expect(view.positions_value).toBe(1000);
  });

  it("leaves the fields the API never publishes empty instead of inventing them", () => {
    const [view] = toProfilesResponse({ profiles: [PROFILE] }).profiles;

    expect(view.engine_slot).toBeNull();
    expect(view.api_port).toBeNull();
    expect(view.sparkline).toEqual([]);
  });

  it("narrows an unknown mode and state to the safe documented value", () => {
    const [view] = toProfilesResponse({ profiles: [{ ...PROFILE, mode: "wat", state: "wat" }] })
      .profiles;

    expect(view.mode).toBe("paper");
    expect(view.state).toBe("stopped");
  });

  it("answers an empty ranking for a body that carries no profile list", () => {
    expect(toProfilesResponse({}).profiles).toEqual([]);
    expect(toProfilesResponse(null).profiles).toEqual([]);
  });
});

describe("toHealthStatus", () => {
  it("maps the counters the deploy smoke test asserts on", () => {
    const health = toHealthStatus(HEALTH);

    expect(health.status).toBe("ok");
    expect(health.running_profiles).toBe(10);
    expect(health.max_running_profiles).toBe(2);
    expect(health.kill_switch_engaged).toBe(false);
  });

  it("reports live trading as disabled: the health payload never publishes the gate", () => {
    expect(toHealthStatus(HEALTH).live_trading_enabled).toBe(false);
  });
});

describe("toAccountResponse", () => {
  const ACCOUNT = {
    generated_at: "2026-09-27T17:07:06Z",
    paper: { scope: "paper", portfolio_value: 20000.0, initial_capital: 20000.0 },
    live: { scope: "live", portfolio_value: 500.0, initial_capital: 500.0 },
    combined: {
      scope: "combined",
      portfolio_value: 20500.0,
      initial_capital: 20500.0,
      profit_abs: 0.0,
      profit_pct: 0.0,
      realized_profit_abs: 0.0,
      unrealized_profit_abs: 0.0,
      open_trades: 0,
      closed_trades: 0,
      win_rate: 0.0,
      profit_factor: 0.0,
      max_drawdown_pct: 0.0,
      profiles_total: 22,
      profiles_running: 2,
      equity_curve: [{ t: "2026-09-27T17:07:00Z", value: 20500.0, profit_pct: 0.0 }],
    },
  };

  it("reads the combined scope and its `t`/`value` curve", () => {
    const account = toAccountResponse(ACCOUNT, "24h");

    expect(account.window).toBe("24h");
    expect(account.performance.portfolio_value).toBe(20500);
    expect(account.performance.profiles_total).toBe(22);
    expect(account.equity_curve).toHaveLength(1);
    expect(account.equity_curve[0].timestamp).toBe("2026-09-27T17:07:00Z");
    expect(account.equity_curve[0].portfolio_value).toBe(20500);
  });

  it("derives the absolute profit of a point from the initial capital", () => {
    const account = toAccountResponse(
      {
        combined: {
          initial_capital: 20000,
          equity_curve: [{ t: "2026-09-27T17:07:00Z", value: 20500, profit_pct: 0.025 }],
        },
      },
      "all",
    );

    expect(account.equity_curve[0].profit_usdt).toBe(500);
  });
});

describe("toProfileDetail", () => {
  const DETAIL = {
    profile: PROFILE,
    strategy: {
      id: "basic",
      class_name: "BasicStrategy",
      title: "EMA cross baseline",
      category: "baseline",
      summary: "Minimal EMA crossover used as the platform baseline.",
      description: "A deliberately small trend strategy.",
      indicators: ["EMA(12)", "EMA(26)"],
      timeframes: ["1h", "4h"],
      reference: "Freqtrade strategy customization guide",
      risk_notes: "Whipsaws in ranging markets.",
      profile_count: 2,
      profiles_running: 1,
      portfolio_value: 2000.0,
      profit_abs: 0.0,
      profit_pct: 0.0,
      best_profile_id: "basic-btc-1h",
      win_rate: 0.0,
    },
    equity_curve: [{ t: "2026-09-27T17:07:00Z", value: 1040.0, profit_pct: 0.04 }],
    daily: [],
    open_trades: [{ trade_id: 12, pair: "BTC/USDT", is_open: true }],
    recent_trades: [{ trade_id: 41, pair: "BTC/USDT", is_open: false }],
  };

  it("serves the profile, its curve, its catalogue entry and its trade rows", () => {
    const detail = toProfileDetail(DETAIL, "24h");

    expect(detail.profile.id).toBe("basic-btc-1h");
    expect(detail.window).toBe("24h");
    expect(detail.performance.profit_usdt).toBe(40);
    expect(detail.performance.cash).toBe(40);
    expect(detail.equity_curve).toHaveLength(1);
    expect(detail.config.exchange).toBe("binance");
    expect(detail.config.priority).toBe(100);
    expect(detail.config.max_open_trades).toBe(2);
    expect(detail.strategy?.class_name).toBe("BasicStrategy");
    expect(detail.strategy?.indicators).toEqual(["EMA(12)", "EMA(26)"]);
    expect(detail.open_trades).toHaveLength(1);
    expect(detail.recent_trades).toHaveLength(1);
  });

  it("renders an empty but well-formed detail for an empty body", () => {
    const detail = toProfileDetail({}, "7d");

    expect(detail.profile.id).toBe("");
    expect(detail.equity_curve).toEqual([]);
    expect(detail.daily_profit).toEqual([]);
    expect(detail.open_trades).toEqual([]);
    expect(detail.strategy).toBeUndefined();
  });
});

describe("toStrategyView", () => {
  it("maps the catalogue metadata and keeps an omitted aggregate unknown", () => {
    const view = toStrategyView({
      id: "momentum",
      title: "Momentum breakout",
      timeframes: ["15m", "1h"],
      indicators: ["ROC(12)"],
      profile_count: 3,
      profit_abs: 120.0,
      profit_pct: 0.06,
    });

    expect(view.timeframe).toBe("15m");
    expect(view.timeframes).toEqual(["15m", "1h"]);
    expect(view.profiles_total).toBe(3);
    expect(view.profit_usdt).toBe(120);

    const withoutAggregate = toStrategyView({ id: "donchian", title: "Donchian" });
    expect(Number.isFinite(withoutAggregate.profiles_total)).toBe(false);
  });
});

describe("toEventItem", () => {
  it("carries the journal row of the API onto the view model", () => {
    const event = toEventItem({
      id: 4,
      ts: "2026-09-27T17:06:53Z",
      profile_id: null,
      level: "warning",
      kind: "cap_reached",
      message: "fleet cap reached: 2 profile(s) running, 20 profile(s) queued",
    });

    expect(event.id).toBe("4");
    expect(event.timestamp).toBe("2026-09-27T17:06:53Z");
    expect(event.level).toBe("warning");
    expect(event.kind).toBe("cap_reached");
    expect(event.profile_id).toBeNull();
  });

  it("falls back to `info` for an unknown level and keeps `since`-less lists working", () => {
    expect(toEventItem({ id: 1, level: "critical" }).level).toBe("info");
    expect(toEventsResponse({}).events).toEqual([]);
  });
});

describe("toDashboardSettings", () => {
  it("maps the engine settings the API publishes", () => {
    const settings = toDashboardSettings({
      max_running_profiles: 2,
      snapshot_interval_seconds: 60,
      kill_switch_engaged: false,
      allow_live_trading: false,
      catalogue_profile_count: 22,
      operator_profile_count: 0,
      state_db_path: "data/realtime/ci-state.db",
      version: "1.0.0",
    });

    expect(settings.max_running_profiles).toBe(2);
    expect(settings.snapshot_interval_seconds).toBe(60);
    expect(settings.allow_live_trading).toBe(false);
    // The API publishes no dashboard refresh interval: the documented default.
    expect(settings.refresh_interval_seconds).toBe(15);
  });
});

describe("mutation envelopes", () => {
  it("maps the profile envelope of an action", () => {
    const answer = toProfileActionResponse({ profile: PROFILE });

    expect(answer.profile.id).toBe("basic-btc-1h");
    expect(answer.generated_at).toBe("2026-09-27T17:07:00Z");
  });

  it("maps the kill switch flag", () => {
    expect(toKillSwitchResponse({ kill_switch_engaged: true }).engaged).toBe(true);
    expect(toKillSwitchResponse({ kill_switch_engaged: true }).stopped_profiles).toEqual([]);
  });

  it("maps the catalogue apply counters, `skipped` and `refused_live` included", () => {
    const result = toCatalogueApplyResponse({
      created: ["alpha"],
      updated: [],
      skipped: ["bravo"],
      pruned: ["charlie"],
      refused_live: ["delta"],
    });

    expect(result.created).toEqual(["alpha"]);
    expect(result.unchanged).toEqual(["bravo"]);
    expect(result.pruned).toEqual(["charlie"]);
    expect(result.refused).toEqual(["delta"]);
  });
});

describe("defensive readers", () => {
  it("never emits a non-finite number", () => {
    expect(asNumber(Number.NaN)).toBe(0);
    expect(asNumber(Number.POSITIVE_INFINITY)).toBe(0);
    expect(asNumber("12")).toBe(0);
    expect(asNumber(12)).toBe(12);
    expect(asRecord([])).toEqual({});
    expect(asRecord("nope")).toEqual({});
  });
});
