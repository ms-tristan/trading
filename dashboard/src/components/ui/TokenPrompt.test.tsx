import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  OPERATOR_TOKEN_STORAGE_KEY,
  TokenPrompt,
  clearOperatorToken,
  readOperatorToken,
  saveOperatorToken,
} from "./TokenPrompt";

/**
 * Recording double of `localStorage`.
 *
 * The host Node runtime exposes no `localStorage` in the jsdom environment
 * (Node's own experimental implementation is disabled without
 * `--localstorage-file`), so the double both proves that nothing is written
 * there and keeps the assertion readable.
 */
const localStore = vi.hoisted(() => ({ writes: [] as string[], reads: [] as string[] }));

beforeEach(() => {
  window.sessionStorage.clear();
  localStore.writes.length = 0;
  localStore.reads.length = 0;
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => {
      localStore.reads.push(key);
      return null;
    },
    setItem: (key: string, value: string) => {
      localStore.writes.push(`${key}=${value}`);
    },
    removeItem: (key: string) => {
      localStore.writes.push(`remove:${key}`);
    },
    clear: () => {
      localStore.writes.push("clear");
    },
    key: () => null,
    length: 0,
  });
});

afterEach(() => {
  clearOperatorToken();
  vi.unstubAllGlobals();
});

/**
 * Vitest runs with `globals: false`, so React Testing Library cannot register
 * its automatic cleanup: every rendered tree is unmounted here.
 */
afterEach(cleanup);

describe("operator token storage helpers", () => {
  it("stores, reads and forgets a token in sessionStorage", () => {
    expect(readOperatorToken()).toBeNull();

    saveOperatorToken("s3cret");
    expect(readOperatorToken()).toBe("s3cret");
    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBe("s3cret");

    clearOperatorToken();
    expect(readOperatorToken()).toBeNull();
  });

  it("ignores a blank stored token", () => {
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, "   ");
    expect(readOperatorToken()).toBeNull();
  });
});

describe("TokenPrompt", () => {
  it("asks for the token once and keeps it out of the DOM afterwards", () => {
    const onTokenChange = vi.fn();
    render(<TokenPrompt onTokenChange={onTokenChange} />);

    const input = screen.getByLabelText(/Token \(kept in this tab only\)/);
    expect(input).toHaveAttribute("type", "password");

    fireEvent.change(input, { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));

    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBe("s3cret");
    expect(onTokenChange).toHaveBeenLastCalledWith("s3cret");
    expect(screen.queryByLabelText(/Token \(kept in this tab only\)/)).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("s3cret")).not.toBeInTheDocument();
    expect(screen.getByText(/An operator token is held for this tab/)).toBeInTheDocument();
  });

  it("never writes the token to localStorage or to a cookie", () => {
    render(<TokenPrompt />);

    fireEvent.change(screen.getByLabelText(/Token \(kept in this tab only\)/), {
      target: { value: "s3cret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));

    expect(localStore.writes).toEqual([]);
    expect(document.cookie).toBe("");
  });

  it("refuses an empty token", () => {
    const onTokenChange = vi.fn();
    render(<TokenPrompt onTokenChange={onTokenChange} />);

    fireEvent.change(screen.getByLabelText(/Token \(kept in this tab only\)/), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Enter the operator token.");
    expect(window.sessionStorage.length).toBe(0);
    expect(onTokenChange).toHaveBeenLastCalledWith(null);
  });

  it("forgets the token on demand", () => {
    const onTokenChange = vi.fn();
    render(<TokenPrompt onTokenChange={onTokenChange} />);

    fireEvent.change(screen.getByLabelText(/Token \(kept in this tab only\)/), {
      target: { value: "s3cret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save token" }));
    fireEvent.click(screen.getByRole("button", { name: "Forget token" }));

    expect(window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY)).toBeNull();
    expect(onTokenChange).toHaveBeenLastCalledWith(null);
    expect(screen.getByLabelText(/Token \(kept in this tab only\)/)).toBeInTheDocument();
  });

  it("picks up a token already stored for the tab", () => {
    saveOperatorToken("stored");
    const onTokenChange = vi.fn();

    render(<TokenPrompt onTokenChange={onTokenChange} />);

    expect(onTokenChange).toHaveBeenCalledWith("stored");
    expect(screen.getByRole("button", { name: "Forget token" })).toBeInTheDocument();
  });

  it("names the header the token travels in", () => {
    saveOperatorToken("stored");
    const { container } = render(<TokenPrompt />);

    expect(container.querySelector("code")).toHaveTextContent("X-Operator-Token");
  });
});
