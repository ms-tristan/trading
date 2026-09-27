import { describe, expect, it } from "vitest";

import { detail, profile, strategyView } from "@/components/profile/fixtures";

import {
  bestProfileFor,
  countProfilesFor,
  profileMatchesStrategy,
  splitReference,
  strategyCardView,
  strategyFromDetail,
  withDerivedProfileCount,
} from "./strategyMeta";

describe("strategyCardView", () => {
  it("reads the catalogue metadata the wire publishes", () => {
    const view = strategyCardView(
      strategyView({
        id: "momentum",
        title: "Momentum breakout",
        category: "trend",
        description: "Buys strength while momentum stays positive.",
        summary: "Buys strength.",
        risk_notes: "Momentum crashes.",
        indicators: ["Momentum(10)", "EMA(50)"],
        timeframes: ["15m", "1h"],
        reference: "Momentum investing - https://www.investopedia.com/terms/m/momentum.asp",
      }),
    );

    expect(view?.id).toBe("momentum");
    expect(view?.title).toBe("Momentum breakout");
    expect(view?.category).toBe("trend");
    expect(view?.description).toBe("Buys strength while momentum stays positive.");
    expect(view?.summary).toBe("Buys strength.");
    expect(view?.riskNotes).toBe("Momentum crashes.");
    expect(view?.indicators).toEqual(["Momentum(10)", "EMA(50)"]);
    expect(view?.timeframes).toEqual(["15m", "1h"]);
    expect(view?.reference).toContain("investopedia");
    expect(view?.profilesTotal).toBe(2);
    expect(view?.profilesRunning).toBe(1);
    expect(view?.portfolioValue).toBe(2000);
    expect(view?.sparkline).toEqual([1000, 1040]);
  });

  it("falls back to the singular timeframe and the class name", () => {
    const view = strategyCardView({ class_name: "MomentumStrategy", timeframe: "1h" });

    expect(view?.id).toBe("MomentumStrategy");
    expect(view?.title).toBe("MomentumStrategy");
    expect(view?.timeframes).toEqual(["1h"]);
    expect(view?.indicators).toEqual([]);
    expect(view?.pairs).toEqual([]);
  });

  it("leaves an absent figure unknown instead of inventing a zero", () => {
    const view = strategyCardView({ id: "basic" });

    expect(Number.isNaN(view?.profilesTotal ?? 0)).toBe(true);
    expect(Number.isNaN(view?.portfolioValue ?? 0)).toBe(true);
    expect(view?.category).toBeNull();
    expect(view?.description).toBeNull();
    expect(view?.reference).toBeNull();
  });

  it("answers null for an entry that is not an object", () => {
    expect(strategyCardView(null)).toBeNull();
    expect(strategyCardView("momentum")).toBeNull();
    expect(strategyFromDetail(detail({ strategy: { id: "basic", title: "Baseline" } }))?.id).toBe(
      "basic",
    );
    expect(strategyFromDetail(detail())).toBeNull();
  });
});

describe("profileMatchesStrategy", () => {
  it("matches the catalogue id and the freqtrade class name", () => {
    expect(profileMatchesStrategy("momentum", "momentum")).toBe(true);
    expect(profileMatchesStrategy("momentum", "MomentumStrategy")).toBe(true);
    expect(profileMatchesStrategy("rsi-reversion", "RsiReversionStrategy")).toBe(true);
    expect(profileMatchesStrategy("MomentumStrategy", "momentum")).toBe(true);
    expect(profileMatchesStrategy("momentum", "basic")).toBe(false);
  });

  it("never matches on an empty identifier", () => {
    expect(profileMatchesStrategy("", "momentum")).toBe(false);
    expect(profileMatchesStrategy("momentum", "")).toBe(false);
  });
});

describe("bestProfileFor and countProfilesFor", () => {
  const ranked = [
    profile({ id: "bravo", strategy: "momentum", portfolio_value: 1300 }),
    profile({ id: "alpha", strategy: "basic", portfolio_value: 1100 }),
    profile({ id: "charlie", strategy: "MomentumStrategy", portfolio_value: 950 }),
  ];

  it("takes the first match of the API ranking", () => {
    expect(bestProfileFor("momentum", ranked)?.id).toBe("bravo");
    expect(bestProfileFor("donchian", ranked)).toBeNull();
  });

  it("counts every profile that holds the strategy", () => {
    expect(countProfilesFor("momentum", ranked)).toBe(2);
    expect(countProfilesFor("basic", ranked)).toBe(1);
    expect(countProfilesFor("donchian", ranked)).toBe(0);
  });
});

describe("withDerivedProfileCount", () => {
  const ranked = [
    profile({ id: "bravo", strategy: "momentum" }),
    profile({ id: "charlie", strategy: "MomentumStrategy" }),
  ];

  it("keeps the count the API aggregated", () => {
    const view = strategyCardView(strategyView({ id: "momentum", profiles_total: 7 }));

    expect(view).not.toBeNull();
    expect(withDerivedProfileCount(view!, ranked).profilesTotal).toBe(7);
  });

  it("derives the count from the ranked list when the API omits it", () => {
    const view = strategyCardView({ id: "momentum" });

    expect(withDerivedProfileCount(view!, ranked).profilesTotal).toBe(2);
  });

  it("stays unknown when the ranked list is empty", () => {
    const view = strategyCardView({ id: "momentum" });

    expect(Number.isNaN(withDerivedProfileCount(view!, []).profilesTotal)).toBe(true);
  });
});

describe("splitReference", () => {
  it("splits a label from its source URL", () => {
    expect(
      splitReference("Freqtrade strategy customization guide - https://www.freqtrade.io/en/stable/"),
    ).toEqual({
      label: "Freqtrade strategy customization guide",
      url: "https://www.freqtrade.io/en/stable/",
    });
  });

  it("keeps a reference without a URL as plain text", () => {
    expect(splitReference("Relative Strength Index, J. Welles Wilder (1978)")).toEqual({
      label: "Relative Strength Index, J. Welles Wilder (1978)",
      url: null,
    });
  });

  it("uses the URL as the label when the reference is only a URL", () => {
    expect(splitReference("https://www.investopedia.com/terms/r/rsi.asp")).toEqual({
      label: "https://www.investopedia.com/terms/r/rsi.asp",
      url: "https://www.investopedia.com/terms/r/rsi.asp",
    });
  });

  it("refuses a link that is not http(s)", () => {
    expect(splitReference("see javascript:alert(1)")).toEqual({
      label: "see javascript:alert(1)",
      url: null,
    });
    expect(splitReference("broken https://")).toEqual({ label: "broken https://", url: null });
  });
});
