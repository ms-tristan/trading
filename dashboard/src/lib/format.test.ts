import { describe, expect, it } from "vitest";

import {
  directionClass,
  directionGlyph,
  directionLabel,
  formatDuration,
  formatPercent,
  formatRatioPercent,
  formatSignedPercent,
  formatSignedRatioPercent,
  formatSignedUsdt,
  formatTimestamp,
  formatUsdt,
} from "./format";

const EM_DASH = "\u2014";

describe("formatUsdt", () => {
  it("renders thousands, two decimals and the USDT unit", () => {
    expect(formatUsdt(12345.6)).toBe("12,345.60 USDT");
    expect(formatUsdt(0)).toBe("0.00 USDT");
    expect(formatUsdt(-1234.5)).toBe("-1,234.50 USDT");
  });

  it("honours the requested number of decimals", () => {
    expect(formatUsdt(1234.5678, 0)).toBe("1,235 USDT");
    expect(formatUsdt(1234.5678, 4)).toBe("1,234.5678 USDT");
  });

  it("renders an em dash instead of NaN or Infinity", () => {
    expect(formatUsdt(Number.NaN)).toBe(EM_DASH);
    expect(formatUsdt(Number.POSITIVE_INFINITY)).toBe(EM_DASH);
    expect(formatUsdt(Number.NEGATIVE_INFINITY)).toBe(EM_DASH);
  });
});

describe("formatSignedUsdt", () => {
  it("prefixes gains with a plus sign", () => {
    expect(formatSignedUsdt(1234.5)).toBe("+1,234.50 USDT");
  });

  it("keeps the minus sign of a loss and does not sign a zero", () => {
    expect(formatSignedUsdt(-1234.5)).toBe("-1,234.50 USDT");
    expect(formatSignedUsdt(0)).toBe("0.00 USDT");
  });

  it("renders an em dash for a non-finite value", () => {
    expect(formatSignedUsdt(Number.NaN)).toBe(EM_DASH);
  });
});

describe("formatPercent", () => {
  it("appends the percent sign to an already-multiplied value", () => {
    expect(formatPercent(12.345)).toBe("12.35%");
    expect(formatPercent(0)).toBe("0.00%");
  });

  it("renders an em dash for a non-finite value", () => {
    expect(formatPercent(Number.NaN)).toBe(EM_DASH);
  });
});

describe("formatSignedPercent", () => {
  it("signs gains and losses", () => {
    expect(formatSignedPercent(12.345)).toBe("+12.35%");
    expect(formatSignedPercent(-3)).toBe("-3.00%");
    expect(formatSignedPercent(0)).toBe("0.00%");
  });

  it("renders an em dash for a non-finite value", () => {
    expect(formatSignedPercent(Number.POSITIVE_INFINITY)).toBe(EM_DASH);
  });
});

describe("formatRatioPercent", () => {
  it("multiplies a 0..1 ratio by 100", () => {
    expect(formatRatioPercent(0.1234)).toBe("12.34%");
    expect(formatRatioPercent(0)).toBe("0.00%");
    expect(formatRatioPercent(1)).toBe("100.00%");
  });

  it("renders an em dash for a non-finite ratio", () => {
    expect(formatRatioPercent(Number.NaN)).toBe(EM_DASH);
  });
});

describe("formatSignedRatioPercent", () => {
  it("signs a ratio after scaling it", () => {
    expect(formatSignedRatioPercent(0.05)).toBe("+5.00%");
    expect(formatSignedRatioPercent(-0.05)).toBe("-5.00%");
  });

  it("renders an em dash for a non-finite ratio", () => {
    expect(formatSignedRatioPercent(Number.NaN)).toBe(EM_DASH);
  });
});

describe("formatDuration", () => {
  it("renders seconds, minutes, hours and days", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(90)).toBe("1m 30s");
    expect(formatDuration(3661)).toBe("1h 1m");
    expect(formatDuration(90061)).toBe("1d 1h");
  });

  it("renders an em dash for a negative or non-finite duration", () => {
    expect(formatDuration(-1)).toBe(EM_DASH);
    expect(formatDuration(Number.NaN)).toBe(EM_DASH);
  });
});

describe("formatTimestamp", () => {
  it("renders an ISO timestamp in UTC", () => {
    expect(formatTimestamp("2026-09-27T17:32:05Z")).toBe("2026-09-27 17:32:05 UTC");
    expect(formatTimestamp("2026-01-02T03:04:05.123Z")).toBe("2026-01-02 03:04:05 UTC");
  });

  it("renders an em dash for an empty or unparsable timestamp", () => {
    expect(formatTimestamp("")).toBe(EM_DASH);
    expect(formatTimestamp("   ")).toBe(EM_DASH);
    expect(formatTimestamp("not-a-date")).toBe(EM_DASH);
  });
});

describe("direction helpers", () => {
  it("maps a signed value to a text glyph", () => {
    expect(directionGlyph(1)).toBe("\u25B2");
    expect(directionGlyph(-1)).toBe("\u25BC");
    expect(directionGlyph(0)).toBe(EM_DASH);
    expect(directionGlyph(Number.NaN)).toBe(EM_DASH);
  });

  it("maps a signed value to a colour class", () => {
    expect(directionClass(1)).toBe("text-profit");
    expect(directionClass(-1)).toBe("text-loss");
    expect(directionClass(0)).toBe("text-muted-foreground");
    expect(directionClass(Number.NaN)).toBe("text-muted-foreground");
  });

  it("maps a signed value to a spoken direction", () => {
    expect(directionLabel(1)).toBe("up");
    expect(directionLabel(-1)).toBe("down");
    expect(directionLabel(0)).toBe("flat");
    expect(directionLabel(Number.NaN)).toBe("flat");
  });
});
