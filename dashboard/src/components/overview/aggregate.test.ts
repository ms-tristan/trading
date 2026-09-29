import { describe, expect, it } from "vitest";

import type { ProfileView } from "@/lib/types";

import {
  directionOf,
  formatModeSummary,
  formatProfitFactor,
  rankProfiles,
  summariseProfiles,
} from "./aggregate";

function profile(overrides: Partial<ProfileView> & { id: string }): ProfileView {
  return {
    name: overrides.id,
    strategy: "BasicStrategy",
    strategy_title: "Basic",
    timeframe: "5m",
    pairs: ["BTC/USDT"],
    mode: "paper",
    state: "running",
    state_reason: null,
    portfolio_value: 0,
    initial_capital: 1000,
    profit_usdt: 0,
    profit_pct: 0,
    open_trades: 0,
    closed_trades: 0,
    win_rate: 0,
    profit_factor: 0,
    max_drawdown_pct: 0,
    engine_slot: null,
    api_port: null,
    sparkline: [],
    updated_at: "2026-09-27T00:00:00Z",
    ...overrides,
  };
}

const ALPHA = profile({
  id: "alpha",
  portfolio_value: 1100,
  initial_capital: 1000,
  profit_usdt: 100,
  open_trades: 2,
  closed_trades: 8,
  win_rate: 0.5,
});
const BRAVO = profile({
  id: "bravo",
  portfolio_value: 900,
  initial_capital: 1000,
  profit_usdt: -100,
  state: "stopped",
  state_reason: "not started",
  open_trades: 0,
  closed_trades: 2,
  win_rate: 1,
});
const CHARLIE = profile({
  id: "charlie",
  portfolio_value: 1000,
  initial_capital: 1000,
  state: "blocked",
  closed_trades: 0,
});

describe("summariseProfiles", () => {
  it("sums value, capital, profit, trades and states", () => {
    const summary = summariseProfiles([ALPHA, BRAVO, CHARLIE]);

    expect(summary.profiles_total).toBe(3);
    expect(summary.profiles_running).toBe(1);
    expect(summary.profiles_needing_decision).toBe(1);
    expect(summary.portfolio_value).toBe(3000);
    expect(summary.initial_capital).toBe(3000);
    expect(summary.profit_usdt).toBe(0);
    expect(summary.profit_pct).toBe(0);
    expect(summary.open_trades).toBe(2);
    expect(summary.closed_trades).toBe(10);
  });

  it("weights the win rate by the number of closed trades", () => {
    const summary = summariseProfiles([ALPHA, BRAVO]);

    expect(summary.win_rate).toBeCloseTo((0.5 * 8 + 1 * 2) / 10);
  });

  it("keeps a zero win rate when nothing closed yet", () => {
    expect(summariseProfiles([CHARLIE]).win_rate).toBe(0);
  });

  it("computes the profit percentage from the invested capital", () => {
    const summary = summariseProfiles([ALPHA, BRAVO]);

    expect(summary.profit_usdt).toBe(0);
    expect(summary.profit_pct).toBe(0);

    const gaining = summariseProfiles([ALPHA]);
    expect(gaining.profit_pct).toBeCloseTo(0.1);
  });

  it("ignores non-finite money instead of poisoning the sums", () => {
    const summary = summariseProfiles([
      profile({ id: "broken", portfolio_value: Number.NaN, profit_usdt: Number.POSITIVE_INFINITY }),
    ]);

    expect(summary.portfolio_value).toBe(0);
    expect(summary.profit_usdt).toBe(0);
    expect(summary.profit_pct).toBe(0);
  });

  it("answers a zeroed summary for an empty section", () => {
    expect(summariseProfiles([])).toMatchObject({
      profiles_total: 0,
      profiles_running: 0,
      profiles_needing_decision: 0,
      portfolio_value: 0,
      profit_pct: 0,
    });
  });

  it("counts only error and blocked as a decision, never a stopped profile", () => {
    const errored = summariseProfiles([profile({ id: "errored", state: "error" })]);
    expect(errored.profiles_needing_decision).toBe(1);

    const blocked = summariseProfiles([profile({ id: "blocked", state: "blocked" })]);
    expect(blocked.profiles_needing_decision).toBe(1);

    const idle = summariseProfiles([profile({ id: "stopped", state: "stopped" })]);
    expect(idle.profiles_needing_decision).toBe(0);

    const running = summariseProfiles([profile({ id: "running", state: "running" })]);
    expect(running.profiles_needing_decision).toBe(0);
  });

  it("carries no waiting counter any more", () => {
    expect(Object.keys(summariseProfiles([]))).not.toContain("profiles_waiting");
  });
});

describe("formatModeSummary", () => {
  it("spells out the aggregate line of a section", () => {
    const line = formatModeSummary(summariseProfiles([ALPHA, BRAVO]));

    expect(line).toContain("2 profiles");
    expect(line).toContain("1 running");
    expect(line).toContain("2,000.00 USDT");
    expect(line).toContain("0.00 USDT (0.00%)");
    expect(line).not.toContain("needing attention");
    expect(line).toContain("60.00% win rate");
  });

  it("uses the singular and omits the empty aggregates", () => {
    const line = formatModeSummary(summariseProfiles([profile({ id: "solo", state: "running" })]));

    expect(line).toContain("1 profile");
    expect(line).toContain("1 running");
    expect(line).toContain("0.00 USDT (0.00%)");
    expect(line).not.toContain("needing attention");
    expect(line).not.toContain("1,000.00 USDT");
  });

  it("reads a decision after the running count and before the portfolio value", () => {
    const line = formatModeSummary(summariseProfiles([ALPHA, CHARLIE]));

    expect(line).toContain("2 profiles - 1 running - 1 needing attention - 2,100.00 USDT");
  });

  it("never mentions a queue or a wait", () => {
    const line = formatModeSummary(summariseProfiles([ALPHA, BRAVO, CHARLIE]));

    expect(line).not.toContain("waiting");
    expect(line).not.toContain("queued");
    expect(line).not.toContain("slot");
  });
});

describe("rankProfiles", () => {
  it("numbers the profiles in the order the API returned them", () => {
    const rows = rankProfiles([ALPHA, BRAVO, CHARLIE]);

    expect(rows.map((row) => row.rank)).toEqual([1, 2, 3]);
    expect(rows.map((row) => row.profile.id)).toEqual(["alpha", "bravo", "charlie"]);
  });

  it("answers an empty ranking for an empty section", () => {
    expect(rankProfiles([])).toEqual([]);
  });
});

describe("directionOf", () => {
  it("maps a signed value to a KPI direction", () => {
    expect(directionOf(1)).toBe("up");
    expect(directionOf(-1)).toBe("down");
    expect(directionOf(0)).toBe("flat");
    expect(directionOf(Number.NaN)).toBe("flat");
  });
});

describe("formatProfitFactor", () => {
  it("renders two decimals", () => {
    expect(formatProfitFactor(1.856)).toBe("1.86");
    expect(formatProfitFactor(0)).toBe("0.00");
  });

  it("renders an unbounded factor as infinity", () => {
    expect(formatProfitFactor(Number.POSITIVE_INFINITY)).toBe("\u221E");
  });

  it("renders a missing or NaN factor as an em dash", () => {
    expect(formatProfitFactor(null)).toBe("\u2014");
    expect(formatProfitFactor(undefined)).toBe("\u2014");
    expect(formatProfitFactor(Number.NaN)).toBe("\u2014");
  });
});
