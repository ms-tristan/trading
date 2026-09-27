import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { eventItem } from "@/components/profile/fixtures";
import type { EventItem } from "@/lib/types";

import { EVENTS_LIMIT } from "@/lib/types";

import { EventLevelBadge, EventsTable } from "./EventsTable";

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

const EVENTS: EventItem[] = [
  eventItem({
    id: "12",
    timestamp: "2026-09-27T11:59:00Z",
    level: "info",
    kind: "profile_started",
    profile_id: "alpha",
    message: "Alpha started on an engine slot.",
  }),
  eventItem({
    id: "11",
    timestamp: "2026-09-27T11:58:00Z",
    level: "warning",
    kind: "cap_reached",
    profile_id: null,
    message: "The fleet cap is reached; the profile is queued.",
  }),
  eventItem({
    id: "10",
    timestamp: "2026-09-27T11:57:00Z",
    level: "error",
    kind: "crash",
    profile_id: "bravo",
    message: "The worker of Bravo exited with code 1.",
  }),
];

describe("EventLevelBadge", () => {
  it("writes every level out next to a plain text marker", () => {
    const { unmount } = render(<EventLevelBadge level="info" />);
    expect(screen.getByText("Info")).toBeInTheDocument();
    expect(screen.getByText("i")).toBeInTheDocument();
    expect(screen.getByText("Info").closest("[data-level]")).toHaveAttribute("data-level", "info");

    unmount();
    render(<EventLevelBadge level="warning" />);
    expect(screen.getByText("Warning")).toBeInTheDocument();
    expect(screen.getByText("!")).toBeInTheDocument();

    unmount();
    render(<EventLevelBadge level="error" />);
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText("\u2715")).toBeInTheDocument();
  });

  it("renders a level of a newer API as itself", () => {
    render(<EventLevelBadge level={"critical" as EventItem["level"]} />);

    expect(screen.getByText("critical")).toBeInTheDocument();
    expect(screen.getByText("?")).toBeInTheDocument();
  });
});

describe("EventsTable", () => {
  it("lists the five fields of the journal, newest first", () => {
    render(<EventsTable events={EVENTS} />);

    expect(screen.getByRole("heading", { level: 2, name: "Recent events" })).toBeInTheDocument();
    expect(screen.getByText("3 newest rows of the engine journal")).toBeInTheDocument();
    expect(EVENTS_LIMIT).toBe(50);

    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows).toHaveLength(4);
    expect(rows[1]).toHaveTextContent("2026-09-27 11:59:00 UTC");
    expect(rows[1]).toHaveTextContent("profile_started");
    expect(rows[1]).toHaveTextContent("Alpha started on an engine slot.");
    expect(rows[2]).toHaveTextContent("platform");
    expect(rows[3]).toHaveTextContent("crash");
    expect(screen.getByRole("link", { name: "alpha" })).toHaveAttribute("href", "/profiles/alpha");
  });

  it("marks the level of every row next to its wording", () => {
    render(<EventsTable events={EVENTS} />);

    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows[1]).not.toHaveClass("bg-destructive/10");
    expect(rows[1]).not.toHaveClass("bg-muted/50");
    expect(rows[2]).toHaveClass("bg-muted/50");
    expect(rows[3]).toHaveClass("bg-destructive/10");

    expect(within(rows[1]).getByText("Info")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Warning")).toBeInTheDocument();
    expect(within(rows[3]).getByText("Error")).toBeInTheDocument();
  });

  it("sorts the journal through the aria-sort headers", () => {
    render(<EventsTable events={EVENTS} />);

    const table = screen.getByRole("table");
    const header = within(table).getByRole("button", { name: "Sort by Time" });
    fireEvent.click(header);

    expect(header.closest("th")).toHaveAttribute("aria-sort", "ascending");
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("crash");
    expect(rows[3]).toHaveTextContent("profile_started");
  });

  it("says so when the journal is empty", () => {
    render(<EventsTable events={[]} />);

    expect(screen.getByText("The engine journal is empty.")).toBeInTheDocument();
    expect(screen.getByText("0 newest rows of the engine journal")).toBeInTheDocument();
  });
});
