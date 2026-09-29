import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { StateBadge } from "./StateBadge";
import type { ProfileState } from "@/lib/types";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("StateBadge", () => {
  it.each([
    ["running", "Running", "--status-running"],
    ["stopped", "Stopped", "--status-stopped"],
    ["error", "Error", "--status-error"],
    ["blocked", "Blocked", "--status-blocked"],
  ] as [ProfileState, string, string][])(
    "renders %s as %s with %s",
    (state, label, colorVar) => {
      const { container } = render(<StateBadge state={state} />);

      expect(screen.getByText(label)).toBeInTheDocument();
      const badge = container.querySelector(`[data-state="${state}"]`);
      expect(badge).not.toBeNull();
      const styled = badge?.querySelector("[title]");
      // the state colour outlines the badge and paints the marker, never the text
      expect(styled?.getAttribute("style")).toContain(`border-color: var(${colorVar})`);
      expect(styled?.querySelector("[aria-hidden='true']")?.getAttribute("style")).toContain(
        `color: var(${colorVar})`,
      );
      expect(styled).toHaveClass("text-card-foreground");
    },
  );

  it("shows the reason as visible text and as the title attribute", () => {
    const { container } = render(
      <StateBadge state="blocked" reason="live trading is disabled" />,
    );

    const reason = screen.getByText("live trading is disabled");
    expect(reason).toHaveAttribute("title", "live trading is disabled");
    expect(container.querySelector("[data-attention='true']")).not.toBeNull();
  });

  it("falls back to the label when there is no reason", () => {
    const { container } = render(<StateBadge state="running" reason={null} />);

    expect(container.querySelector("[title='Running']")).not.toBeNull();
    expect(container.querySelector("[data-attention='false']")).not.toBeNull();
  });

  it("treats a blank reason as no reason", () => {
    render(<StateBadge state="stopped" reason="   " />);

    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.queryByText("   ")).not.toBeInTheDocument();
  });

  it("renders an unknown state without crashing", () => {
    render(<StateBadge state={"legacy" as ProfileState} />);

    expect(screen.getByText("Unknown")).toBeInTheDocument();
  });

  it("degrades a legacy queued state to an unknown badge instead of crashing", () => {
    // An older engine may still publish `"queued"`; the badge is typed on the
    // four documented states, so the defensive fallback answers for it.
    render(<StateBadge state={"queued" as ProfileState} />);

    expect(screen.getByText("Unknown")).toBeInTheDocument();
    expect(screen.queryByText("Queued")).not.toBeInTheDocument();
  });
});
