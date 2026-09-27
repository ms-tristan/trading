import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse } from "@/components/profile/fixtures";
import { clearOperatorToken } from "@/components/ui/TokenPrompt";

import { OperationsConsole } from "./OperationsConsole";

const { routerMock } = vi.hoisted(() => ({ routerMock: { refresh: vi.fn() } }));

vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
}));

const SETTINGS = {
  refresh_interval_seconds: 15,
  allow_live_trading: false,
  max_running_profiles: 4,
  snapshot_interval_seconds: 60,
  // Published by `GET /api/settings`; the console holds it like any other engine
  // setting of the view model even though it renders no control for it.
  worker_start_stagger_seconds: 12,
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  window.sessionStorage.clear();
  fetchMock = vi.fn(async () =>
    jsonResponse({ ...SETTINGS, max_running_profiles: 6, snapshot_interval_seconds: 60 }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(cleanup);
afterEach(() => {
  clearOperatorToken();
  vi.unstubAllGlobals();
});

describe("OperationsConsole", () => {
  it("holds one operator prompt for the mutation controls of the page", () => {
    render(<OperationsConsole settings={SETTINGS} killSwitchEngaged={false} />);

    expect(screen.getAllByText("Operator token")).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 2, name: "Platform settings" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Kill switch" })).toBeInTheDocument();
  });

  it("does not call the API before the token of the prompt is saved", () => {
    render(<OperationsConsole settings={SETTINGS} killSwitchEngaged={false} />);

    fireEvent.change(screen.getByLabelText(/^Max running profiles/), { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      screen.getByText("Save the operator token of this tab before changing a setting."),
    ).toBeInTheDocument();
  });

  it("carries the token saved through the prompt into the settings call", async () => {
    render(<OperationsConsole settings={SETTINGS} killSwitchEngaged={false} />);

    fireEvent.change(screen.getByLabelText(/^Token/), { target: { value: "s3cr3t" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));

    expect(screen.getByText(/An operator token is held for this tab/)).toBeInTheDocument();
    expect(screen.queryByText("s3cr3t")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/^Max running profiles/), { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchMock.mock.calls[0][1]?.headers).toEqual({
      "Content-Type": "application/json",
      "X-Operator-Token": "s3cr3t",
    });
  });

  it("carries the same token into the kill switch confirmation", async () => {
    render(<OperationsConsole settings={SETTINGS} killSwitchEngaged />);

    fireEvent.change(screen.getByLabelText(/^Token/), { target: { value: "s3cr3t" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));

    fireEvent.click(screen.getByRole("button", { name: "Release kill switch" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm release" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/kill-switch");
    expect(fetchMock.mock.calls[0][1]?.body).toBe(JSON.stringify({ engaged: false }));
  });
});
