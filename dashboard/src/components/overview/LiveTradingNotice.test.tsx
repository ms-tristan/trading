import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ProfileView } from "@/lib/types";

import { LiveTradingNotice } from "./LiveTradingNotice";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

function liveProfile(overrides: Partial<ProfileView> & { id: string }): ProfileView {
  return {
    name: overrides.id,
    strategy: "BasicStrategy",
    strategy_title: "Basic",
    timeframe: "5m",
    pairs: ["BTC/USDT"],
    mode: "live",
    state: "blocked",
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

describe("LiveTradingNotice", () => {
  it("states that live trading is disabled and why", () => {
    render(
      <LiveTradingNotice
        allowLiveTrading={false}
        profiles={[
          liveProfile({
            id: "funded",
            name: "Funded BTC",
            state: "blocked",
            state_reason: "live trading is not enabled",
          }),
        ]}
      />,
    );

    expect(screen.getByRole("note")).toHaveTextContent("Live trading is not enabled");
    expect(screen.getByRole("note")).toHaveTextContent(/allow_live_trading = false/);
    expect(screen.getByText("Refused profile")).toBeInTheDocument();
    expect(screen.getByRole("listitem")).toHaveTextContent(
      "Funded BTC — Blocked: live trading is not enabled",
    );
  });

  it("lists every refused profile", () => {
    render(
      <LiveTradingNotice
        allowLiveTrading={false}
        profiles={[
          liveProfile({ id: "one", name: "One", state: "blocked", state_reason: "no exchange key" }),
          liveProfile({ id: "two", name: "Two", state: "error", state_reason: "engine exited" }),
          liveProfile({ id: "three", name: "Three", state: "stopped" }),
        ]}
      />,
    );

    expect(screen.getByText("Refused profiles")).toBeInTheDocument();
    expect(screen.getByText(/no exchange key/)).toBeInTheDocument();
    expect(screen.getByText(/engine exited/)).toBeInTheDocument();
    expect(screen.queryByText(/Three/)).not.toBeInTheDocument();
  });

  it("says when live trading is enabled but nothing is configured", () => {
    render(<LiveTradingNotice allowLiveTrading profiles={[]} />);

    expect(screen.getByRole("note")).toHaveTextContent("Live trading is enabled");
    expect(screen.getByRole("note")).toHaveTextContent(/No live profile is configured yet/);
  });

  it("counts the configured live profiles when the gate is open", () => {
    render(
      <LiveTradingNotice
        allowLiveTrading
        profiles={[
          liveProfile({ id: "one", name: "One", state: "running" }),
          liveProfile({ id: "two", name: "Two", state: "running" }),
        ]}
      />,
    );

    expect(screen.getByText("2 live profiles are configured; the venue is funded with real money.")).toBeInTheDocument();
    expect(screen.queryByText("Refused profiles")).not.toBeInTheDocument();
  });

  it("uses the singular for a single configured live profile", () => {
    render(<LiveTradingNotice allowLiveTrading profiles={[liveProfile({ id: "one" })]} />);

    expect(screen.getByText(/1 live profile is configured/)).toBeInTheDocument();
  });

  it("admits when the settings could not be read instead of guessing", () => {
    render(<LiveTradingNotice allowLiveTrading={null} profiles={[]} />);

    expect(screen.getByRole("note")).toHaveTextContent("Live trading status unknown");
    expect(screen.getByRole("note")).toHaveTextContent(/could not be read/);
  });
});
