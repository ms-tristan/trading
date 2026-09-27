import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearOperatorToken, saveOperatorToken } from "@/components/ui/TokenPrompt";

import { actionDisabled, ProfileControls } from "./ProfileControls";
import { jsonResponse } from "./fixtures";

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
    jsonResponse({
      // The wire envelope of `POST /api/profiles/{id}/actions/{action}`: the
      // refreshed profile view, and no message (the control writes its own).
      profile: { id: "alpha", name: "Alpha", state: "running" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(cleanup);
afterEach(() => {
  clearOperatorToken();
  vi.unstubAllGlobals();
});

function clickAction(name: string) {
  fireEvent.click(screen.getByRole("button", { name }));
}

describe("actionDisabled", () => {
  it("greys out an action that cannot change anything", () => {
    expect(actionDisabled("running", "start")).toBe(true);
    expect(actionDisabled("running", "stop")).toBe(false);
    expect(actionDisabled("queued", "stop")).toBe(false);

    for (const state of ["stopped", "error", "blocked"] as const) {
      expect(actionDisabled(state, "stop")).toBe(true);
      expect(actionDisabled(state, "start")).toBe(false);
      expect(actionDisabled(state, "restart")).toBe(false);
    }
  });
});

describe("ProfileControls", () => {
  it("asks for the token before calling the API", () => {
    render(<ProfileControls profileId="alpha" state="stopped" />);

    clickAction("Start");

    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      screen.getByText("Save the operator token of this tab before sending an action."),
    ).toBeInTheDocument();
  });

  it("posts the action with the session token and refreshes the page", async () => {
    saveOperatorToken("s3cr3t");
    render(<ProfileControls profileId="alpha" state="stopped" />);

    clickAction("Start");

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/profiles/alpha/actions/start",
      expect.objectContaining({
        method: "POST",
        headers: { "X-Operator-Token": "s3cr3t" },
      }),
    );
    expect(await screen.findByText("Alpha is starting.")).toBeInTheDocument();
    expect(refreshMock).toHaveBeenCalled();
  });

  it("never renders the token back", () => {
    saveOperatorToken("s3cr3t");
    render(<ProfileControls profileId="alpha" state="running" />);

    expect(screen.queryByDisplayValue("s3cr3t")).not.toBeInTheDocument();
    expect(screen.queryByText("s3cr3t")).not.toBeInTheDocument();
    expect(screen.getByText(/An operator token is held for this tab/)).toBeInTheDocument();
  });

  it("surfaces a wrong token in the banner", async () => {
    saveOperatorToken("wrong");
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "forbidden" }, 403));
    render(<ProfileControls profileId="alpha" state="stopped" />);

    clickAction("Restart");

    expect(await screen.findByText(/HTTP 403/)).toBeInTheDocument();
    expect(screen.getByText(/The engine refused the action/)).toBeInTheDocument();
    expect(refreshMock).not.toHaveBeenCalled();
  });

  it("surfaces a network failure instead of throwing", async () => {
    saveOperatorToken("s3cr3t");
    fetchMock.mockRejectedValueOnce(new Error("connection refused"));
    render(<ProfileControls profileId="alpha" state="queued" />);

    clickAction("Stop");

    expect(await screen.findByText(/connection refused/)).toBeInTheDocument();
  });

  it("greys out the actions the current state makes pointless", () => {
    const { unmount } = render(<ProfileControls profileId="alpha" state="running" />);
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();

    unmount();
    render(<ProfileControls profileId="alpha" state="stopped" />);
    expect(screen.getByRole("button", { name: "Start" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  });
});
