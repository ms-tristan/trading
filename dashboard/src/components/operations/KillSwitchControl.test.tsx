import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse } from "@/components/profile/fixtures";
import { clearOperatorToken, saveOperatorToken } from "@/components/ui/TokenPrompt";

import { KillSwitchControl } from "./KillSwitchControl";

const { refreshMock, routerMock } = vi.hoisted(() => {
  const refresh = vi.fn();
  return { refreshMock: refresh, routerMock: { refresh } };
});

vi.mock("next/navigation", () => ({
  useRouter: () => routerMock,
}));

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  window.sessionStorage.clear();
  fetchMock = vi.fn(async () =>
    // The wire body of `POST /api/kill-switch`: the flag only.
    jsonResponse({ kill_switch_engaged: true }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(cleanup);
afterEach(() => {
  clearOperatorToken();
  vi.unstubAllGlobals();
});

describe("KillSwitchControl", () => {
  it("shows the state the API reports", () => {
    const { unmount } = render(<KillSwitchControl engaged={false} token={null} />);
    expect(screen.getByText("Released")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Engage kill switch" })).toBeInTheDocument();

    unmount();
    render(<KillSwitchControl engaged token={null} />);
    expect(screen.getByText("Engaged")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Release kill switch" })).toBeInTheDocument();
  });

  it("asks for a confirmation and only then sends the engagement", async () => {
    saveOperatorToken("s3cr3t");
    render(<KillSwitchControl engaged={false} token={null} />);

    fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));

    // The first click opens the confirmation only: nothing has been sent yet.
    expect(fetchMock).not.toHaveBeenCalled();
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent("Engage the kill switch?");
    expect(dialog).toHaveTextContent(
      "Every running profile is stopped now, and nothing starts until you release it.",
    );
    expect(screen.getByRole("button", { name: "Confirm engagement" })).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: "Confirm engagement" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/kill-switch", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Operator-Token": "s3cr3t" },
      body: JSON.stringify({ engaged: true }),
    });
    expect(
      await screen.findByText("Kill switch engaged; the engine stopped the running workers."),
    ).toBeInTheDocument();
    expect(refreshMock).toHaveBeenCalled();
  });

  it("releases the switch through the same confirmation", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ kill_switch_engaged: false }));
    render(<KillSwitchControl engaged token="held-token" />);

    fireEvent.click(screen.getByRole("button", { name: "Release kill switch" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("Release the kill switch?");
    expect(fetchMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Confirm release" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchMock.mock.calls[0][1]?.body).toBe(JSON.stringify({ engaged: false }));
    expect(
      await screen.findByText("Kill switch released; the engine may start workers again."),
    ).toBeInTheDocument();
  });

  it("does not invent a count of stopped profiles the API never publishes", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ kill_switch_engaged: true }));
    render(<KillSwitchControl engaged={false} token="held-token" />);

    fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm engagement" }));

    expect(
      await screen.findByText("Kill switch engaged; the engine stopped the running workers."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/profiles were stopped/)).not.toBeInTheDocument();
  });

  it("cancels without calling the API", () => {
    render(<KillSwitchControl engaged={false} token="held-token" />);

    fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Engage kill switch" })).toBeInTheDocument();
  });

  it("asks for the token instead of calling the API without one", () => {
    render(<KillSwitchControl engaged={false} token={null} />);

    fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm engagement" }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      screen.getByText("Save the operator token of this tab before using the kill switch."),
    ).toBeInTheDocument();
  });

  it("surfaces the 403 of the API in the banner", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "wrong operator token" }, 403));
    render(<KillSwitchControl engaged={false} token="wrong" />);

    fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm engagement" }));

    expect(await screen.findByText("The engine refused the kill switch")).toBeInTheDocument();
    expect(screen.getByText(/HTTP 403/)).toBeInTheDocument();
  });

  it("never renders the token", () => {
    render(<KillSwitchControl engaged={false} token="held-token" />);

    expect(screen.queryByText("held-token")).not.toBeInTheDocument();
  });
});
