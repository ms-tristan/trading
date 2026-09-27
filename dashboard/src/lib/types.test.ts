import { describe, expect, it } from "vitest";

import {
  DEFAULT_REFRESH_INTERVAL_SECONDS,
  DEFAULT_SETTINGS,
  EMPTY_ACCOUNT,
  EMPTY_EVENTS,
  EMPTY_PROFILES,
  EMPTY_STRATEGIES,
  type HealthStatus,
} from "./types";

/**
 * The fallback payloads are handed to `fetchWithFallback` when the API cannot be
 * reached: they must stay API-shaped, so the page renders an empty dashboard
 * rather than crashing on a missing field.
 */
describe("fallback payloads", () => {
  it("defaults the refresh interval to 15 s and keeps live trading off", () => {
    expect(DEFAULT_REFRESH_INTERVAL_SECONDS).toBe(15);
    expect(DEFAULT_SETTINGS.refresh_interval_seconds).toBe(DEFAULT_REFRESH_INTERVAL_SECONDS);
    expect(DEFAULT_SETTINGS.allow_live_trading).toBe(false);
  });

  it("exposes an empty profile list", () => {
    expect(EMPTY_PROFILES.profiles).toEqual([]);
    expect(EMPTY_PROFILES.generated_at).toBe("");
  });

  it("exposes a complete zeroed account performance", () => {
    expect(EMPTY_ACCOUNT.equity_curve).toEqual([]);
    expect(EMPTY_ACCOUNT.performance.portfolio_value).toBe(0);
    expect(EMPTY_ACCOUNT.performance.profiles_total).toBe(0);
    expect(EMPTY_ACCOUNT.performance.engine_slots_total).toBe(0);
    expect(EMPTY_ACCOUNT.performance.window).toBe(EMPTY_ACCOUNT.window);
  });

  it("exposes empty strategies and events", () => {
    expect(EMPTY_STRATEGIES.strategies).toEqual([]);
    expect(EMPTY_EVENTS.events).toEqual([]);
  });
});

/**
 * The view model of `GET /api/health` mirrors the wire names: the slot pair is
 * `engine_slots_used`/`engine_slots_total` and the fleet size is
 * `profiles_running`. The literal below is typed, so a rename on the wire side
 * breaks this test at compile time instead of blanking a KPI.
 */
describe("HealthStatus", () => {
  const health: HealthStatus = {
    status: "ok",
    version: "2026.8",
    uptime_seconds: 60,
    profiles_running: 2,
    engine_slots_used: 2,
    engine_slots_total: 4,
    live_trading_enabled: false,
    kill_switch_engaged: false,
    generated_at: "2026-09-27T12:00:00Z",
  };

  it("carries the three counters the wire publishes", () => {
    expect(Object.keys(health)).toEqual([
      "status",
      "version",
      "uptime_seconds",
      "profiles_running",
      "engine_slots_used",
      "engine_slots_total",
      "live_trading_enabled",
      "kill_switch_engaged",
      "generated_at",
    ]);
    expect(health.profiles_running).toBe(2);
    expect(health.engine_slots_total).toBe(4);
  });
});
