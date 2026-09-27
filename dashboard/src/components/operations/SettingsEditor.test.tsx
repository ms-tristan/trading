import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse } from "@/components/profile/fixtures";
import { clearOperatorToken, saveOperatorToken } from "@/components/ui/TokenPrompt";

import { SettingsEditor, parsePositiveInteger } from "./SettingsEditor";

const { refreshMock, routerMock } = vi.hoisted(() => {
  const refresh = vi.fn();
  return { refreshMock: refresh, routerMock: { refresh } };
});

vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
}));

const SETTINGS = {
  refresh_interval_seconds: 15,
  allow_live_trading: false,
  max_running_profiles: 4,
  snapshot_interval_seconds: 60,
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  window.sessionStorage.clear();
  fetchMock = vi.fn(async () =>
    jsonResponse({ ...SETTINGS, max_running_profiles: 6, snapshot_interval_seconds: 30 }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(cleanup);
afterEach(() => {
  clearOperatorToken();
  vi.unstubAllGlobals();
});

/** The labelled input of the editor. */
function field(label: string): HTMLInputElement {
  return screen.getByLabelText(new RegExp(`^${label}`)) as HTMLInputElement;
}

describe("parsePositiveInteger", () => {
  it("accepts a whole number greater than zero only", () => {
    expect(parsePositiveInteger("6")).toBe(6);
    expect(parsePositiveInteger(" 30 ")).toBe(30);
    expect(parsePositiveInteger("")).toBeNull();
    expect(parsePositiveInteger("0")).toBeNull();
    expect(parsePositiveInteger("-2")).toBeNull();
    expect(parsePositiveInteger("2.5")).toBeNull();
    expect(parsePositiveInteger("many")).toBeNull();
  });
});

describe("SettingsEditor", () => {
  it("shows the settings the API published", () => {
    render(<SettingsEditor settings={SETTINGS} token={null} />);

    expect(field("Max running profiles").value).toBe("4");
    expect(field("Snapshot interval").value).toBe("60");
    expect(screen.getByText("refresh every 15 s")).toBeInTheDocument();
    expect(screen.getByText(/Live trading is disabled/)).toBeInTheDocument();
  });

  it("saves the two settings with the operator token of the session", async () => {
    saveOperatorToken("s3cr3t");
    render(<SettingsEditor settings={SETTINGS} token={null} />);

    fireEvent.change(field("Max running profiles"), { target: { value: "6" } });
    fireEvent.change(field("Snapshot interval"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Operator-Token": "s3cr3t" },
      body: JSON.stringify({ max_running_profiles: 6, snapshot_interval_seconds: 30 }),
    });
    expect(
      await screen.findByText("Saved: max running profiles 6, snapshot interval 30 s."),
    ).toBeInTheDocument();
    expect(refreshMock).toHaveBeenCalled();
  });

  it("sends the current value of every filled field", async () => {
    render(<SettingsEditor settings={SETTINGS} token="held-token" />);

    fireEvent.change(field("Max running profiles"), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchMock.mock.calls[0][1]?.body).toBe(
      JSON.stringify({ max_running_profiles: 8, snapshot_interval_seconds: 60 }),
    );
  });

  it("leaves an emptied field out of the request", async () => {
    render(<SettingsEditor settings={SETTINGS} token="held-token" />);

    fireEvent.change(field("Max running profiles"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchMock.mock.calls[0][1]?.body).toBe(
      JSON.stringify({ snapshot_interval_seconds: 60 }),
    );
  });

  it("asks for the token instead of calling the API without one", () => {
    render(<SettingsEditor settings={SETTINGS} token={null} />);

    fireEvent.change(field("Max running profiles"), { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      screen.getByText("Save the operator token of this tab before changing a setting."),
    ).toBeInTheDocument();
  });

  it("refuses an invalid entry before calling the API", () => {
    render(<SettingsEditor settings={SETTINGS} token="held-token" />);

    fireEvent.change(field("Snapshot interval"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Snapshot interval must be a whole number of seconds greater than zero.",
    );
  });

  it("rejects an invalid fleet cap too", () => {
    render(<SettingsEditor settings={SETTINGS} token="held-token" />);

    fireEvent.change(field("Max running profiles"), { target: { value: "2.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Max running profiles must be a whole number greater than zero.",
    );
  });

  it("says so when there is nothing to save", () => {
    render(<SettingsEditor settings={{ refresh_interval_seconds: 15, allow_live_trading: false }} token="held-token" />);

    expect(field("Max running profiles").value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      screen.getByText("Nothing to save: fill in at least one of the two settings."),
    ).toBeInTheDocument();
  });

  it("surfaces the 401 of the API in the banner", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "missing operator token" }, 401));
    render(<SettingsEditor settings={SETTINGS} token="held-token" />);

    fireEvent.change(field("Max running profiles"), { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByText("The engine refused the new settings")).toBeInTheDocument();
    expect(screen.getByText(/HTTP 401/)).toBeInTheDocument();
    expect(refreshMock).not.toHaveBeenCalled();
  });

  it("surfaces the 403 of the API in the banner", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "wrong operator token" }, 403));
    render(<SettingsEditor settings={SETTINGS} token="wrong" />);

    fireEvent.change(field("Max running profiles"), { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    expect(await screen.findByText(/HTTP 403/)).toBeInTheDocument();
  });

  it("never renders the token", () => {
    render(<SettingsEditor settings={SETTINGS} token="held-token" />);

    expect(screen.queryByText("held-token")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("held-token")).not.toBeInTheDocument();
  });
});
