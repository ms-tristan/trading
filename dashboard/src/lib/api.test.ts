import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  API_ORIGIN,
  ApiError,
  OPERATOR_TOKEN_HEADER,
  applyCatalogue,
  createProfile,
  deleteProfile,
  fetchJson,
  fetchWithFallback,
  forgetLastKnown,
  getAccount,
  getEvents,
  getHealth,
  getProfile,
  getProfiles,
  getSettings,
  getStrategies,
  lastKnown,
  patchProfile,
  postAction,
  postKillSwitch,
  postSettings,
  resolveRequestUrl,
} from "./api";

/** Response double: the code only reads `ok`, `status` and `text()`. */
function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function emptyResponse(status = 204): Response {
  return { ok: true, status, text: async () => "" } as unknown as Response;
}

function unreadableResponse(): Response {
  return {
    ok: true,
    status: 200,
    text: async () => {
      throw new Error("socket closed");
    },
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  forgetLastKnown();
});

afterEach(() => {
  vi.unstubAllGlobals();
  forgetLastKnown();
});

/** URL of the n-th fetch call (0 by default). */
function calledUrl(index = 0): string {
  return String(fetchMock.mock.calls[index][0]);
}

/** RequestInit of the n-th fetch call (0 by default). */
function calledInit(index = 0): RequestInit {
  return (fetchMock.mock.calls[index][1] ?? {}) as RequestInit;
}

describe("API_ORIGIN", () => {
  it("defaults to the documented loopback origin", () => {
    expect(API_ORIGIN).toBe(process.env.API_ORIGIN ?? "http://127.0.0.1:8080");
  });
});

describe("resolveRequestUrl", () => {
  it("stays same-origin in the browser", () => {
    expect(resolveRequestUrl("/api/health")).toBe("/api/health");
  });

  it("uses the absolute origin on the server", () => {
    vi.stubGlobal("window", undefined);
    expect(resolveRequestUrl("/api/health")).toBe(`${API_ORIGIN}/api/health`);
  });
});

describe("fetchJson", () => {
  it("calls the same-origin path from the browser, with no cache override", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));

    await expect(fetchJson("/api/health")).resolves.toEqual({ status: "ok" });
    expect(calledUrl()).toBe("/api/health");
    expect(calledInit().cache).toBeUndefined();
  });

  it("calls the absolute origin with cache no-store on the server", async () => {
    vi.stubGlobal("window", undefined);
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));

    await expect(fetchJson("/api/health")).resolves.toEqual({ status: "ok" });
    expect(calledUrl()).toBe(`${API_ORIGIN}/api/health`);
    expect(calledInit().cache).toBe("no-store");
  });

  it("throws an ApiError carrying the status and the path on a non-2xx answer", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "nope" }, 404));

    await expect(fetchJson("/api/profiles/missing")).rejects.toBeInstanceOf(ApiError);
    await expect(fetchJson("/api/profiles/missing")).rejects.toMatchObject({
      status: 404,
      path: "/api/profiles/missing",
      name: "ApiError",
    });
  });

  it("throws an ApiError when the body is not JSON", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => "<html>proxy error</html>",
    } as unknown as Response);

    await expect(fetchJson("/api/health")).rejects.toMatchObject({
      status: 200,
      path: "/api/health",
    });
    await expect(fetchJson("/api/health")).rejects.toThrow(/not valid JSON/);
  });

  it("throws an ApiError when the body cannot be read", async () => {
    fetchMock.mockResolvedValue(unreadableResponse());

    await expect(fetchJson("/api/health")).rejects.toThrow(/could not be read/);
  });

  it("throws an ApiError with status 0 when the network fails", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));

    await expect(fetchJson("/api/health")).rejects.toMatchObject({ status: 0, path: "/api/health" });
  });

  it("never resolves to Infinity or NaN", async () => {
    // A hand-written body: JSON.stringify would turn Infinity into null, while
    // JSON.parse("1e999") really does yield Infinity.
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => '{"value": 1e999, "ratio": -1e999, "count": 3}',
    } as unknown as Response);

    const payload = await fetchJson<{ value: number; ratio: number; count: number }>("/api/x");
    expect(payload.value).toBe(0);
    expect(payload.ratio).toBe(0);
    expect(payload.count).toBe(3);
    expect(Number.isFinite(payload.value)).toBe(true);
  });

  it("answers undefined for a 204 without body", async () => {
    fetchMock.mockResolvedValue(emptyResponse());

    await expect(fetchJson("/api/profiles/p1")).resolves.toBeUndefined();
  });

  it("remembers the last successful body of a path", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));

    expect(lastKnown("/api/health")).toBeUndefined();
    await fetchJson("/api/health");
    expect(lastKnown("/api/health")).toEqual({ status: "ok" });
  });
});

describe("fetchWithFallback", () => {
  it("returns the fresh body and no error on success", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ profiles: [] }));

    await expect(fetchWithFallback("/api/profiles", { profiles: ["fallback"] })).resolves.toEqual({
      data: { profiles: [] },
      error: null,
    });
  });

  it("falls back to the last known body on failure", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ profiles: ["known"] }));
    await fetchJson("/api/profiles");

    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "boom" }, 500));
    const result = await fetchWithFallback("/api/profiles", { profiles: ["fallback"] });

    expect(result.data).toEqual({ profiles: ["known"] });
    expect(result.error).toBeInstanceOf(ApiError);
    expect(result.error?.status).toBe(500);
  });

  it("falls back to the caller default when nothing is known", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));

    const result = await fetchWithFallback("/api/account", { portfolio_value: 0 });
    expect(result.data).toEqual({ portfolio_value: 0 });
    expect(result.error).toBeInstanceOf(ApiError);
  });

  it("wraps a non-ApiError throw and still answers the fallback", async () => {
    fetchMock.mockImplementation(() => {
      throw new Error("synchronous explosion");
    });

    const result = await fetchWithFallback("/api/events", { events: [] });
    expect(result.data).toEqual({ events: [] });
    expect(result.error?.message).toContain("synchronous explosion");
    expect(result.error?.path).toBe("/api/events");
  });
});

describe("read endpoints", () => {
  beforeEach(() => {
    fetchMock.mockResolvedValue(jsonResponse({}));
  });

  it("getHealth", async () => {
    await getHealth();
    expect(calledUrl()).toBe("/api/health");
  });

  it("getAccount carries the window", async () => {
    await getAccount("7d");
    expect(calledUrl()).toBe("/api/account?window=7d");
  });

  it("getAccount defaults to 24h", async () => {
    await getAccount();
    expect(calledUrl()).toBe("/api/account?window=24h");
  });

  it("getProfiles forwards the filters and omits empty ones", async () => {
    await getProfiles({ mode: "live", state: "blocked" });
    expect(calledUrl()).toBe("/api/profiles?mode=live&state=blocked");

    await getProfiles();
    expect(calledUrl(1)).toBe("/api/profiles");

    await getProfiles({ mode: undefined, state: undefined });
    expect(calledUrl(2)).toBe("/api/profiles");
  });

  it("getProfile encodes the id and carries the window", async () => {
    await getProfile("profile one", "30d");
    expect(calledUrl()).toBe("/api/profiles/profile%20one?window=30d");
  });

  it("getStrategies", async () => {
    await getStrategies();
    expect(calledUrl()).toBe("/api/strategies");
  });

  it("getEvents carries the limit", async () => {
    await getEvents(25);
    expect(calledUrl()).toBe("/api/events?limit=25");
  });

  it("getEvents defaults to 50", async () => {
    await getEvents();
    expect(calledUrl()).toBe("/api/events?limit=50");
  });

  it("getSettings", async () => {
    await getSettings();
    expect(calledUrl()).toBe("/api/settings");
  });
});

describe("mutating endpoints", () => {
  beforeEach(() => {
    fetchMock.mockResolvedValue(jsonResponse({}));
  });

  it("postAction posts to the action path with the operator token", async () => {
    await postAction("p1", "restart", "s3cret");

    expect(calledUrl()).toBe("/api/profiles/p1/actions/restart");
    expect(calledInit().method).toBe("POST");
    expect((calledInit().headers as Record<string, string>)[OPERATOR_TOKEN_HEADER]).toBe("s3cret");
  });

  it("postAction encodes a profile id", async () => {
    await postAction("a/b", "stop", "t");
    expect(calledUrl()).toBe("/api/profiles/a%2Fb/actions/stop");
  });

  it("postKillSwitch sends the engaged flag as JSON", async () => {
    await postKillSwitch(true, "s3cret");

    expect(calledUrl()).toBe("/api/kill-switch");
    expect(calledInit().method).toBe("POST");
    expect(calledInit().body).toBe(JSON.stringify({ engaged: true }));
    expect((calledInit().headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("patchProfile patches the profile with a JSON body", async () => {
    await patchProfile("p1", { name: "renamed" }, "s3cret");

    expect(calledUrl()).toBe("/api/profiles/p1");
    expect(calledInit().method).toBe("PATCH");
    expect(calledInit().body).toBe(JSON.stringify({ name: "renamed" }));
  });

  it("createProfile posts the body", async () => {
    await createProfile(
      {
        name: "new",
        strategy: "MomentumStrategy",
        timeframe: "5m",
        pairs: ["BTC/USDT"],
        mode: "paper",
        initial_capital: 1000,
      },
      "s3cret",
    );

    expect(calledUrl()).toBe("/api/profiles");
    expect(calledInit().method).toBe("POST");
    expect(JSON.parse(String(calledInit().body))).toMatchObject({ name: "new" });
  });

  it("deleteProfile omits force by default and adds it when asked", async () => {
    await deleteProfile("p1", "s3cret");
    expect(calledUrl()).toBe("/api/profiles/p1");
    expect(calledInit().method).toBe("DELETE");

    await deleteProfile("p1", "s3cret", true);
    expect(calledUrl(1)).toBe("/api/profiles/p1?force=true");
  });

  it("applyCatalogue posts an empty body by default", async () => {
    await applyCatalogue("s3cret");

    expect(calledUrl()).toBe("/api/catalogue/apply");
    expect(calledInit().body).toBe(JSON.stringify({}));

    await applyCatalogue("s3cret", { prune: true });
    expect(calledInit(1).body).toBe(JSON.stringify({ prune: true }));
  });

  it("postSettings posts the settings", async () => {
    await postSettings({ refresh_interval_seconds: 30 }, "s3cret");

    expect(calledUrl()).toBe("/api/settings");
    expect(calledInit().method).toBe("POST");
    expect(calledInit().body).toBe(JSON.stringify({ refresh_interval_seconds: 30 }));
  });
});

describe("ApiError", () => {
  it("is a real Error subclass carrying status and path", () => {
    const error = new ApiError("boom", 503, "/api/health");

    expect(error).toBeInstanceOf(Error);
    expect(error.name).toBe("ApiError");
    expect(error.status).toBe(503);
    expect(error.path).toBe("/api/health");
    expect(error.message).toBe("boom");
  });
});
