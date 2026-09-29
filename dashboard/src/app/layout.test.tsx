import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetLastKnown } from "@/lib/api";

import RootLayout, { metadata } from "./layout";

const { refreshMock } = vi.hoisted(() => ({ refreshMock: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: refreshMock }),
}));

vi.mock("next/font/google", () => ({
  Inter: () => ({ variable: "--font-inter", className: "font-inter" }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  // the last-known-value cache is module state: it must not leak between tests
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

describe("RootLayout", () => {
  it("renders the shell, the navigation and the children", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ snapshot_interval_seconds: 30, allow_live_trading: false }),
    );

    render(await RootLayout({ children: <p>page content</p> }));

    expect(screen.getByText("Trading platform")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveTextContent("page content");
    expect(fetchMock).toHaveBeenCalledWith("/api/settings", expect.anything());

    // two navigation entries per link: the sidebar (md+) and the compact header
    for (const label of ["Overview", "Strategies", "Operations"]) {
      const links = screen.getAllByRole("link", { name: label });
      expect(links).toHaveLength(2);
      expect(links[0]).toHaveAttribute("href");
    }
  });

  it("refreshes at the documented interval and still reads the engine settings", async () => {
    // `GET /api/settings` publishes the engine settings, not a dashboard
    // refresh interval: the shell keeps the documented 15 s default whatever
    // `snapshot_interval_seconds` says.
    fetchMock.mockResolvedValue(
      jsonResponse({ snapshot_interval_seconds: 45, allow_live_trading: true }),
    );

    render(await RootLayout({ children: <p>page content</p> }));

    expect(screen.getByText("Auto-refresh every 15 s")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/settings", expect.anything());
  });

  it("falls back to the documented 15 s when the settings are unreachable", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));

    render(await RootLayout({ children: <p>page content</p> }));

    expect(screen.getByText("Auto-refresh every 15 s")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveTextContent("page content");
  });

  it("falls back to 15 s when the settings omit the interval", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ allow_live_trading: false }));

    render(await RootLayout({ children: <p>page content</p> }));

    expect(screen.getByText("Auto-refresh every 15 s")).toBeInTheDocument();
  });

  it("offers the operator token prompt in the sidebar", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ snapshot_interval_seconds: 60 }));

    render(await RootLayout({ children: <p>page content</p> }));

    expect(screen.getByText("Operator token")).toBeInTheDocument();
  });

  it("renders the dark theme colour for the browser chrome", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ snapshot_interval_seconds: 60 }));

    render(await RootLayout({ children: <p>page content</p> }));

    const themeColour = document.head.querySelector('meta[name="theme-color"]');
    expect(themeColour).not.toBeNull();
    expect(themeColour).toHaveAttribute("content", "#020617");
  });
});

describe("metadata", () => {
  it("names the application and titles pages through the template", () => {
    expect(metadata.applicationName).toBe("Trading platform");
    expect(metadata.title).toEqual({
      default: "Trading platform",
      template: "%s - Trading platform",
    });
    expect(metadata.description).toBe("Multi-profile paper and live trading monitor.");
  });
});
