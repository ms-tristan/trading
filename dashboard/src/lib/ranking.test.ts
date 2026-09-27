import { describe, expect, it } from "vitest";

import { MODE_ORDER, SORT_KEYS, hasAttentionProfile, isAttentionState, sortProfiles, splitByMode } from "./ranking";
import type { ProfileView } from "./types";

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

const ALPHA = profile({ id: "alpha", name: "Alpha", strategy_title: "Zeta", portfolio_value: 100, profit_usdt: 5 });
const BRAVO = profile({ id: "bravo", name: "Bravo", strategy_title: "Alpha", portfolio_value: 300, profit_usdt: -2 });
const CHARLIE = profile({
  id: "charlie",
  name: "Charlie",
  strategy_title: "Mike",
  portfolio_value: 200,
  profit_usdt: 9,
  mode: "live",
});

describe("sortProfiles", () => {
  it("ranks by portfolio value descending", () => {
    expect(sortProfiles([ALPHA, BRAVO, CHARLIE], "value").map((entry) => entry.id)).toEqual([
      "bravo",
      "charlie",
      "alpha",
    ]);
  });

  it("ranks by profit descending", () => {
    expect(sortProfiles([ALPHA, BRAVO, CHARLIE], "profit").map((entry) => entry.id)).toEqual([
      "charlie",
      "alpha",
      "bravo",
    ]);
  });

  it("orders by profile name and by strategy title, ascending", () => {
    expect(sortProfiles([CHARLIE, ALPHA, BRAVO], "name").map((entry) => entry.id)).toEqual([
      "alpha",
      "bravo",
      "charlie",
    ]);
    expect(sortProfiles([CHARLIE, ALPHA, BRAVO], "strategy").map((entry) => entry.id)).toEqual([
      "bravo",
      "charlie",
      "alpha",
    ]);
  });

  it("keeps the incoming order for equal values", () => {
    const first = profile({ id: "first", portfolio_value: 100 });
    const second = profile({ id: "second", portfolio_value: 100 });
    expect(sortProfiles([first, second], "value").map((entry) => entry.id)).toEqual([
      "first",
      "second",
    ]);
  });

  it("never mutates the input array", () => {
    const input = [ALPHA, BRAVO, CHARLIE];
    sortProfiles(input, "name");
    expect(input.map((entry) => entry.id)).toEqual(["alpha", "bravo", "charlie"]);
  });

  it("treats a non-finite value as zero instead of producing NaN order", () => {
    const broken = profile({ id: "broken", portfolio_value: Number.NaN });
    const ok = profile({ id: "ok", portfolio_value: 10 });
    expect(sortProfiles([broken, ok], "value").map((entry) => entry.id)).toEqual(["ok", "broken"]);
  });
});

describe("splitByMode", () => {
  it("separates the two modes and preserves the API order", () => {
    const input = [CHARLIE, ALPHA, profile({ id: "delta", mode: "live" })];
    const { paper, live } = splitByMode(input);
    expect(paper.map((entry) => entry.id)).toEqual(["alpha"]);
    expect(live.map((entry) => entry.id)).toEqual(["charlie", "delta"]);
  });

  it("answers two empty lists for an empty input", () => {
    expect(splitByMode([])).toEqual({ paper: [], live: [] });
  });
});

describe("ranking vocabulary", () => {
  it("re-exports the attention predicate of the state module", () => {
    expect(isAttentionState("queued")).toBe(true);
    expect(isAttentionState("blocked")).toBe(true);
    expect(isAttentionState("error")).toBe(true);
    expect(isAttentionState("running")).toBe(false);
    expect(isAttentionState("stopped")).toBe(false);
  });

  it("flags a list holding at least one attention state", () => {
    expect(hasAttentionProfile(["running", "stopped"])).toBe(false);
    expect(hasAttentionProfile(["running", "error"])).toBe(true);
  });

  it("publishes the sort keys and the mode order", () => {
    expect(SORT_KEYS).toEqual(["value", "profit", "name", "strategy"]);
    expect(MODE_ORDER).toEqual(["paper", "live"]);
  });
});
