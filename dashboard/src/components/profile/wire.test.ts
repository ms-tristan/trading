import { describe, expect, it } from "vitest";

import {
  EMPTY_PROFILE_DETAIL,
  formatTradeNumber,
  profileRank,
  readDailyBars,
  readNumber,
  readNumberArray,
  readProfileExtras,
  readRecord,
  readStringArray,
  readText,
  readTrade,
  readTradeList,
  resolveCapital,
} from "./wire";
import { config, dailyBar, detail, performance, profile, tradeRow } from "./fixtures";

describe("low-level readers", () => {
  it("reads text and numbers defensively", () => {
    expect(readText("  BTC/USDT ")).toBe("BTC/USDT");
    expect(readText("   ")).toBeNull();
    expect(readText(42)).toBe("42");
    expect(readText(null)).toBeNull();

    expect(readNumber(12.5)).toBe(12.5);
    expect(readNumber("12.5")).toBe(12.5);
    expect(readNumber("")).toBeNull();
    expect(readNumber("not a number")).toBeNull();
    expect(readNumber(Number.POSITIVE_INFINITY)).toBeNull();
    expect(readNumber({})).toBeNull();
  });

  it("never mistakes an array for an object", () => {
    expect(readRecord({ a: 1 })).toEqual({ a: 1 });
    expect(readRecord([1, 2])).toBeNull();
    expect(readRecord(null)).toBeNull();
    expect(readRecord("profit")).toBeNull();
  });

  it("drops the malformed entries of an array", () => {
    expect(readStringArray(["BTC/USDT", "", 7, null])).toEqual(["BTC/USDT", "7"]);
    expect(readStringArray("BTC/USDT")).toEqual([]);
    expect(readNumberArray([1, "2", "x", null])).toEqual([1, 2]);
    expect(readNumberArray(undefined)).toEqual([]);
  });
});

describe("readTrade", () => {
  it("normalises a freqtrade row", () => {
    const row = readTrade(tradeRow(), true);

    expect(row).not.toBeNull();
    expect(row?.tradeId).toBe("12");
    expect(row?.pair).toBe("BTC/USDT");
    expect(row?.openedAt).toBe("2026-09-27T09:00:00Z");
    expect(row?.openRate).toBe(60000);
    expect(row?.currentRate).toBe(60600);
    expect(row?.closeRate).toBeNull();
    expect(row?.amount).toBe(0.0166);
    expect(row?.stakeAmount).toBe(1000);
    expect(row?.profitAbs).toBe(10);
    expect(row?.profitPercent).toBe(1);
    expect(row?.exitReason).toBeNull();
    expect(row?.enterTag).toBe("ema_cross");
    expect(row?.isOpen).toBe(true);
  });

  it("accepts the timestamp aliases of the closed rows", () => {
    const row = readTrade(
      tradeRow({
        open_date: undefined,
        close_date: undefined,
        open_timestamp: "2026-09-27T08:00:00Z",
        close_timestamp: "2026-09-27T10:00:00Z",
        is_open: false,
      }),
      false,
    );

    expect(row?.openedAt).toBe("2026-09-27T08:00:00Z");
    expect(row?.closedAt).toBe("2026-09-27T10:00:00Z");
    expect(row?.isOpen).toBe(false);
  });

  it("turns profit_ratio into percentage points", () => {
    const row = readTrade(tradeRow({ profit_pct: null, profit_ratio: 0.0125 }), true);

    expect(row?.profitPercent).toBeCloseTo(1.25);
  });

  it("derives the percentage from profit_abs and stake_amount", () => {
    const row = readTrade(tradeRow({ profit_pct: null, profit_abs: 25, stake_amount: 1000 }), true);

    expect(row?.profitPercent).toBeCloseTo(2.5);
  });

  it("leaves the percentage unknown when nothing computes it", () => {
    const row = readTrade(tradeRow({ profit_pct: null, profit_abs: null }), true);

    expect(row?.profitPercent).toBeNull();
    expect(row?.profitAbs).toBeNull();
  });

  it("defaults is_open from the list the row belongs to", () => {
    expect(readTrade(tradeRow({ is_open: undefined }), true)?.isOpen).toBe(true);
    expect(readTrade(tradeRow({ is_open: undefined }), false)?.isOpen).toBe(false);
  });

  it("falls back to the open rate when the row carries no current rate", () => {
    const row = readTrade(tradeRow({ current_rate: undefined }), true);

    expect(row?.currentRate).toBe(60000);
    expect(row?.openRate).toBe(60000);
  });

  it("keeps both rates unknown when the row carries neither", () => {
    const row = readTrade(tradeRow({ current_rate: null, open_rate: null }), true);

    expect(row?.currentRate).toBeNull();
    expect(row?.openRate).toBeNull();
  });

  it("keeps a row that carries nothing readable", () => {
    const row = readTrade({ pair: "ETH/USDT" }, false);

    expect(row?.pair).toBe("ETH/USDT");
    expect(row?.tradeId).toBe("");
    expect(row?.openRate).toBeNull();
    expect(row?.isOpen).toBe(false);
  });

  it("drops an entry that is not an object", () => {
    expect(readTrade("trade 12", true)).toBeNull();
    expect(readTradeList([tradeRow({ trade_id: 1 }), null, 7], true)).toHaveLength(1);
    expect(readTradeList({ open_trades: [] }, true)).toEqual([]);
  });
});

describe("readDailyBars", () => {
  it("reads the daily series and drops a bar without a date", () => {
    const bars = readDailyBars([
      dailyBar({ date: "2026-09-26", profit_usdt: 15, trades: 2 }),
      { profit_usdt: 3 },
      null,
    ]);

    expect(bars).toHaveLength(1);
    expect(bars[0]).toEqual({ date: "2026-09-26", profit_usdt: 15, trades: 2 });
  });

  it("reads the state-database keys `abs_profit` and `trade_count` first", () => {
    const bars = readDailyBars([
      { date: "2026-09-26", abs_profit: 15, rel_profit: 0.015, starting_balance: 1000, trade_count: 2 },
    ]);

    expect(bars).toEqual([{ date: "2026-09-26", profit_usdt: 15, trades: 2 }]);
  });

  it("still accepts the older `profit_usdt`/`trades` pair", () => {
    const bars = readDailyBars([{ date: "2026-09-25", profit_usdt: -4, trades: 1 }]);

    expect(bars).toEqual([{ date: "2026-09-25", profit_usdt: -4, trades: 1 }]);
  });

  it("answers an empty series for a non-array", () => {
    expect(readDailyBars(undefined)).toEqual([]);
  });
});

describe("readProfileExtras", () => {
  it("reads the wire fields types.ts does not declare", () => {
    const extras = readProfileExtras(
      detail({
        open_trades: [tradeRow({ trade_id: 1 })],
        recent_trades: [tradeRow({ trade_id: 2, is_open: false })],
        strategy: { id: "basic", title: "EMA cross baseline" },
        config: config({ exchange: "binance", priority: 100 }),
        performance: performance({ cash: 40, positions_value: 1000 }),
        profile: profile({ id: "alpha", rank: 2, uptime_seconds: 600 }),
      }),
    );

    expect(extras.openTrades).toHaveLength(1);
    expect(extras.openTrades[0].isOpen).toBe(true);
    expect(extras.recentTrades).toHaveLength(1);
    expect(extras.recentTrades[0].isOpen).toBe(false);
    expect(extras.strategy).toEqual({ id: "basic", title: "EMA cross baseline" });
    expect(extras.exchange).toBe("binance");
    expect(extras.priority).toBe(100);
    expect(extras.cash).toBe(40);
    expect(extras.positionsValue).toBe(1000);
    expect(extras.rank).toBe(2);
    expect(extras.uptimeSeconds).toBe(600);
  });

  it("accepts the daily and closed_trades aliases", () => {
    const extras = readProfileExtras(
      detail({
        daily_profit: undefined,
        daily: [dailyBar({ date: "2026-09-26", profit_usdt: 5 })],
        closed_trades: [tradeRow({ trade_id: 9, is_open: false })],
      }),
    );

    expect(extras.dailyBars).toHaveLength(1);
    expect(extras.dailyBars[0].profit_usdt).toBe(5);
    expect(extras.recentTrades).toHaveLength(1);
    expect(extras.recentTrades[0].tradeId).toBe("9");
  });

  it("answers neutral values for a payload that carries none of them", () => {
    const extras = readProfileExtras(detail());

    expect(extras.strategy).toBeNull();
    expect(extras.exchange).toBeNull();
    expect(extras.priority).toBeNull();
    expect(extras.cash).toBeNull();
    expect(extras.positionsValue).toBeNull();
    expect(extras.rank).toBeNull();
    expect(extras.uptimeSeconds).toBeNull();
    expect(extras.openTrades).toEqual([]);
    expect(extras.recentTrades).toEqual([]);
  });
});

describe("resolveCapital", () => {
  it("prefers the figures the payload publishes", () => {
    const payload = detail({ performance: performance({ cash: 40, positions_value: 1000 }) });

    expect(resolveCapital(payload, readProfileExtras(payload))).toEqual({
      cash: 40,
      positionsValue: 1000,
    });
  });

  it("sums the open trades and derives the cash", () => {
    const payload = detail({
      performance: performance({ portfolio_value: 1040, open_trades: 2 }),
      open_trades: [
        tradeRow({ trade_id: 1, stake_amount: 400 }),
        tradeRow({ trade_id: 2, stake_amount: 600 }),
      ],
    });

    expect(resolveCapital(payload, readProfileExtras(payload))).toEqual({
      cash: 40,
      positionsValue: 1000,
    });
  });

  it("reports a flat profile without open trades", () => {
    const payload = detail({ performance: performance({ portfolio_value: 1040, open_trades: 0 }) });

    expect(resolveCapital(payload, readProfileExtras(payload))).toEqual({
      cash: 1040,
      positionsValue: 0,
    });
  });

  it("stays unknown when open trades are announced but not published", () => {
    const payload = detail({ performance: performance({ open_trades: 3 }) });

    expect(resolveCapital(payload, readProfileExtras(payload))).toEqual({
      cash: null,
      positionsValue: null,
    });
  });
});

describe("profileRank", () => {
  it("returns the 1-based position of the ranked list", () => {
    const ranked = [profile({ id: "bravo" }), profile({ id: "alpha" })];

    expect(profileRank(ranked, "alpha")).toBe(2);
    expect(profileRank(ranked, "unknown")).toBeNull();
  });
});

describe("formatTradeNumber", () => {
  it("groups a rate without a currency unit", () => {
    expect(formatTradeNumber(60000.123456789)).toBe("60,000.12345679");
    expect(formatTradeNumber(0.0166)).toBe("0.0166");
  });

  it("renders an em dash for an unknown value", () => {
    expect(formatTradeNumber(null)).toBe("\u2014");
    expect(formatTradeNumber(Number.NaN)).toBe("\u2014");
  });
});

describe("EMPTY_PROFILE_DETAIL", () => {
  it("is API-shaped, so a cold failure renders a well-formed page", () => {
    expect(EMPTY_PROFILE_DETAIL.generated_at).toBe("");
    expect(EMPTY_PROFILE_DETAIL.window).toBe("24h");
    expect(EMPTY_PROFILE_DETAIL.profile.id).toBe("");
    expect(EMPTY_PROFILE_DETAIL.config.startable).toBe(false);
    expect(EMPTY_PROFILE_DETAIL.equity_curve).toEqual([]);
    expect(EMPTY_PROFILE_DETAIL.daily_profit).toEqual([]);
  });
});
