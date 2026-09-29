import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ProfileHeader } from "./ProfileHeader";
import { profile } from "./fixtures";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("ProfileHeader", () => {
  it("leads with the profile and spells out its venue", () => {
    render(
      <ProfileHeader
        profile={profile({
          id: "alpha",
          name: "Alpha BTC 5m",
          strategy: "basic",
          strategy_title: "EMA cross baseline",
          timeframe: "5m",
          pairs: ["BTC/USDT", "ETH/USDT"],
          portfolio_value: 1040,
          updated_at: "2026-09-27T12:00:00Z",
        })}
      />,
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "Alpha BTC 5m" }),
    ).toBeInTheDocument();
    expect(screen.getByText("EMA cross baseline")).toBeInTheDocument();
    expect(screen.getByText("basic")).toBeInTheDocument();
    expect(screen.getByText("5m")).toBeInTheDocument();
    expect(screen.getByText("paper")).toBeInTheDocument();
    expect(screen.getByText("BTC/USDT, ETH/USDT")).toBeInTheDocument();
    expect(screen.getByText(/1,040\.00 USDT/)).toBeInTheDocument();
    expect(screen.getByText(/2026-09-27 12:00:00 UTC/)).toBeInTheDocument();
  });

  it("shows the state badge with the reason the engine published", () => {
    render(
      <ProfileHeader
        profile={profile({
          id: "blocked-one",
          state: "blocked",
          state_reason: "the live gate is closed",
        })}
      />,
    );

    const badge = screen.getByText("Blocked");
    expect(badge).toBeInTheDocument();
    expect(screen.getByText("the live gate is closed")).toBeInTheDocument();
    expect(badge.closest("[data-state]")).toHaveAttribute("data-attention", "true");
  });

  it("falls back to the requested id when the payload is empty", () => {
    render(<ProfileHeader profile={profile({ id: "", name: "" })} fallbackName="ghost" />);

    expect(screen.getByRole("heading", { level: 1, name: "ghost" })).toBeInTheDocument();
  });

  it("renders the slot and the port of a running profile only", () => {
    const { unmount } = render(
      <ProfileHeader profile={profile({ id: "alpha", engine_slot: 3, api_port: 8083 })} />,
    );
    expect(screen.getByText(/engine slot #3/)).toBeInTheDocument();
    expect(screen.getByText(/freqtrade API port 8083/)).toBeInTheDocument();

    unmount();
    render(<ProfileHeader profile={profile({ id: "alpha" })} />);
    expect(screen.queryByText(/engine slot/)).not.toBeInTheDocument();
  });

  it("renders the controls it is given", () => {
    render(
      <ProfileHeader
        profile={profile({ id: "alpha" })}
        actions={<button type="button">Start</button>}
      />,
    );

    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
  });
});
