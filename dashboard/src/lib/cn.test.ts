import { describe, expect, it } from "vitest";

import { cn } from "./cn";

describe("cn", () => {
  it("joins the truthy class names", () => {
    expect(cn("a", "b")).toBe("a b");
  });

  it("drops empty, nullish and false entries", () => {
    expect(cn("a", "", undefined, null, false, "b")).toBe("a b");
  });

  it("returns an empty string without any class", () => {
    expect(cn()).toBe("");
    expect(cn(false, undefined)).toBe("");
  });
});
