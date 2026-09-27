import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ProfileConfigCard } from "./ProfileConfigCard";
import { config, profile } from "./fixtures";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("ProfileConfigCard", () => {
  it("lists what the engine resolved for the profile", () => {
    render(
      <ProfileConfigCard
        config={config()}
        profile={profile({ id: "alpha", engine_slot: 3, api_port: 8083 })}
        exchange="binance"
        priority={100}
      />,
    );

    expect(
      screen.getByRole("heading", { level: 2, name: "Resolved configuration" }),
    ).toBeInTheDocument();
    expect(screen.getByText("EMA cross baseline")).toBeInTheDocument();
    expect(screen.getByText("basic")).toBeInTheDocument();
    expect(screen.getByText("5m")).toBeInTheDocument();
    expect(screen.getByText("paper")).toBeInTheDocument();
    expect(screen.getByText("dry run")).toBeInTheDocument();
    expect(screen.getByText("binance")).toBeInTheDocument();
    expect(screen.getByText("BTC/USDT")).toBeInTheDocument();
    expect(screen.getByText("1,000.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("100.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("8083")).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument();
  });

  it("falls back to the live profile when the configuration block is empty", () => {
    render(
      <ProfileConfigCard
        config={config({
          strategy: "",
          timeframe: "",
          pairs: [],
          initial_capital: 0,
          max_open_trades: 0,
          stake_amount: Number.NaN,
          startable: false,
        })}
        profile={profile({
          id: "alpha",
          strategy_title: "Momentum breakout",
          timeframe: "1h",
          pairs: ["ETH/USDT"],
          initial_capital: 500,
        })}
        exchange={null}
        priority={null}
      />,
    );

    expect(screen.getByText("Momentum breakout")).toBeInTheDocument();
    expect(screen.getByText("1h")).toBeInTheDocument();
    expect(screen.getByText("ETH/USDT")).toBeInTheDocument();
    expect(screen.getByText("500.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
    expect(screen.getByText(/the engine refuses to start it right now/)).toBeInTheDocument();
    // Exchange, priority, slot, port and the stake amount stay unknown.
    expect(screen.getAllByText("\u2014").length).toBeGreaterThanOrEqual(4);
  });

  it("renders real funds without the dry-run wording", () => {
    render(
      <ProfileConfigCard
        config={config({
          mode: "live",
          dry_run: false,
          stake_amount: 250,
          startable: true,
        })}
        profile={profile({ id: "funded", mode: "live" })}
        exchange="kraken"
        priority={10}
      />,
    );

    expect(screen.getByText("live")).toBeInTheDocument();
    expect(screen.getByText("real funds")).toBeInTheDocument();
    expect(screen.getByText("250.00 USDT")).toBeInTheDocument();
    expect(screen.getByText("kraken")).toBeInTheDocument();
  });
});
