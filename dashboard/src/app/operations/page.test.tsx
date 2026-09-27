import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiEvent,
  apiEvents,
  apiHealth,
  apiProfile,
  apiProfiles,
  apiSettings,
  jsonResponse,
} from "@/components/profile/fixtures";
import { clearOperatorToken, saveOperatorToken } from "@/components/ui/TokenPrompt";
import { forgetLastKnown } from "@/lib/api";

import OperationsPage from "./page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn() }),
}));

const HEALTH = apiHealth({ kill_switch_engaged: false, running_profiles: 2, max_running_profiles: 4 });

const PROFILES = apiProfiles([
  apiProfile({
    id: "alpha",
    name: "Alpha",
    state: "running",
    portfolio_value: 1040,
    strategy_title: "EMA cross baseline",
  }),
  apiProfile({
    id: "bravo",
    name: "Bravo",
    state: "running",
    portfolio_value: 990,
    strategy_title: "Momentum breakout",
  }),
  apiProfile({
    id: "charlie",
    name: "Charlie",
    state: "queued",
    state_reason: "waiting for an engine slot",
    portfolio_value: 950,
    strategy_title: "Momentum breakout",
  }),
]);

const SETTINGS = apiSettings({ allow_live_trading: false, max_running_profiles: 4 });

const EVENTS = apiEvents([
  apiEvent({
    id: "12",
    level: "info",
    kind: "profile_started",
    profile_id: "alpha",
    message: "Alpha started.",
  }),
  apiEvent({
    id: "11",
    level: "warning",
    kind: "cap_reached",
    profile_id: "charlie",
    message: "Charlie waits for an engine slot.",
  }),
  apiEvent({
    id: "10",
    level: "error",
    kind: "crash",
    profile_id: "bravo",
    message: "Bravo exited with code 1.",
  }),
]);

let fetchMock: ReturnType<typeof vi.fn>;
let killSwitchAnswer: () => Response;

beforeEach(() => {
  window.sessionStorage.clear();
  killSwitchAnswer = () =>
    jsonResponse({ kill_switch_engaged: false });
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input).split("?")[0];
    switch (path) {
      case "/api/health":
        return jsonResponse(HEALTH);
      case "/api/profiles":
        return jsonResponse(PROFILES);
      case "/api/settings":
        return jsonResponse(SETTINGS);
      case "/api/events":
        return jsonResponse(EVENTS);
      case "/api/kill-switch":
        return killSwitchAnswer();
      default:
        return jsonResponse({ detail: `${path} not found` }, 404);
    }
  });
  vi.stubGlobal("fetch", fetchMock);
  forgetLastKnown();
});

afterEach(() => {
  vi.unstubAllGlobals();
  forgetLastKnown();
});

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);
afterEach(clearOperatorToken);

async function renderOperations() {
  return render(await OperationsPage());
}

describe("OperationsPage", () => {
  it("reads the health, the profiles, the settings and the 50 newest events", async () => {
    await renderOperations();

    expect(fetchMock).toHaveBeenCalledWith("/api/health", expect.anything());
    expect(fetchMock).toHaveBeenCalledWith("/api/profiles", expect.anything());
    expect(fetchMock).toHaveBeenCalledWith("/api/settings", expect.anything());
    expect(fetchMock).toHaveBeenCalledWith("/api/events?limit=50", expect.anything());
  });

  it("reports the engine state, the slot usage and the journal", async () => {
    await renderOperations();

    expect(screen.getByRole("heading", { level: 2, name: /Operations/ })).toBeInTheDocument();
    expect(screen.getByText(/engine ok - version 2026\.8 - 2 of 4 engine slots used/))
      .toBeInTheDocument();

    const engine = screen.getByRole("heading", { level: 2, name: "Engine state" }).closest("section");
    expect(engine).not.toBeNull();
    expect(within(engine as HTMLElement).getByText("Released")).toBeInTheDocument();
    expect(within(engine as HTMLElement).getByText("Disabled")).toBeInTheDocument();

    const slots = screen.getByRole("heading", { level: 2, name: /Engine slots/ }).closest("section");
    expect(slots).not.toBeNull();
    const slotRows = within(within(slots as HTMLElement).getByRole("table")).getAllByRole("row");
    expect(slotRows).toHaveLength(4); // header + two running + one queued
    // The API publishes no engine slot index and no worker REST port: the two
    // columns render the em dash rather than a number the engine never sent.
    expect(slotRows[1]).toHaveTextContent("\u2014");
    expect(slotRows[1]).toHaveTextContent("Alpha");
    expect(slotRows[3]).toHaveTextContent("waiting for an engine slot");

    const journal = screen.getByRole("heading", { level: 2, name: "Recent events" }).closest("section");
    expect(journal).not.toBeNull();
    const eventRows = within(within(journal as HTMLElement).getByRole("table")).getAllByRole("row");
    expect(eventRows).toHaveLength(4);
    expect(eventRows[1]).toHaveTextContent("Info");
    expect(eventRows[2]).toHaveTextContent("Warning");
    expect(eventRows[3]).toHaveTextContent("Error");
    expect(eventRows[3]).toHaveClass("bg-destructive/10");
  });

  it("starts the two mutating controls from the values the API published", async () => {
    await renderOperations();

    expect(screen.getByLabelText(/^Max running profiles/)).toHaveValue(4);
    expect(screen.getByLabelText(/^Snapshot interval/)).toHaveValue(60);
    expect(screen.getByText("refresh every 15 s")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Engage kill switch" })).toBeInTheDocument();
  });

  it("reports an engaged kill switch as engaged", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const path = String(input).split("?")[0];
      if (path === "/api/health") {
        return jsonResponse(apiHealth({ kill_switch_engaged: true }));
      }
      if (path === "/api/profiles") {
        return jsonResponse(PROFILES);
      }
      if (path === "/api/settings") {
        return jsonResponse(SETTINGS);
      }
      return jsonResponse(EVENTS);
    });

    await renderOperations();

    expect(screen.getByRole("button", { name: "Release kill switch" })).toBeInTheDocument();
    expect(screen.getByText("no worker may start")).toBeInTheDocument();
  });

  it("surfaces the 403 of the API when the kill switch is confirmed with a wrong token", async () => {
    saveOperatorToken("wrong");
    killSwitchAnswer = () => jsonResponse({ detail: "wrong operator token" }, 403);

    await renderOperations();

    fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm engagement" }));

    expect(await screen.findByText("The engine refused the kill switch")).toBeInTheDocument();
    expect(screen.getByText(/HTTP 403/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/kill-switch",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("posts the engagement and reports the position of the switch", async () => {
    saveOperatorToken("s3cr3t");
    killSwitchAnswer = () => jsonResponse({ kill_switch_engaged: true });

    await renderOperations();

    fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm engagement" }));

    expect(
      await screen.findByText("Kill switch engaged; the engine stopped the running workers."),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/kill-switch", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Operator-Token": "s3cr3t" },
      body: JSON.stringify({ engaged: true }),
    });
  });

  it("surfaces the 401 of the API when a setting is saved without a token", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input).split("?")[0];
      if (path === "/api/settings" && init?.method === "POST") {
        return jsonResponse({ detail: "missing operator token" }, 401);
      }
      switch (path) {
        case "/api/health":
          return jsonResponse(HEALTH);
        case "/api/profiles":
          return jsonResponse(PROFILES);
        case "/api/settings":
          return jsonResponse(SETTINGS);
        default:
          return jsonResponse(EVENTS);
      }
    });
    saveOperatorToken("stale");

    await renderOperations();

    fireEvent.change(screen.getByLabelText(/^Max running profiles/), { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByText("The engine refused the new settings")).toBeInTheDocument();
    expect(screen.getByText(/HTTP 401/)).toBeInTheDocument();
  });

  it("renders the banners plus the last known data when the API goes down", async () => {
    await renderOperations();
    cleanup();

    fetchMock.mockRejectedValue(new Error("connect ECONNREFUSED"));
    await renderOperations();

    expect(screen.getByText("The engine state could not be refreshed")).toBeInTheDocument();
    expect(screen.getByText("The slot usage could not be refreshed")).toBeInTheDocument();
    expect(screen.getByText("The platform settings could not be refreshed")).toBeInTheDocument();
    expect(screen.getByText("The event log could not be refreshed")).toBeInTheDocument();

    expect(screen.getByText(/2 of 4 engine slots used/)).toBeInTheDocument();
    expect(screen.getByText("Alpha started.")).toBeInTheDocument();
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });

  it("renders an empty but well-formed page on a cold failure", async () => {
    fetchMock.mockRejectedValue(new Error("connect ECONNREFUSED"));

    await renderOperations();

    expect(screen.getByText("The engine state could not be refreshed")).toBeInTheDocument();
    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.getByText("No profile holds or waits for an engine slot.")).toBeInTheDocument();
    expect(screen.getByText("The engine journal is empty.")).toBeInTheDocument();
    expect(screen.queryByText("NaN")).not.toBeInTheDocument();
  });
});
