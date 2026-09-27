/**
 * Number, money, percentage, duration and timestamp formatting.
 *
 * House rules:
 * * a non-finite or unparsable input renders as an em dash (`-`), never
 *   `NaN`/`Infinity`;
 * * money is always suffixed with the `USDT` unit, percentages always with `%`;
 * * timestamps are rendered in UTC, so a test and the container agree.
 */

const EM_DASH = "\u2014";

const usdtFormatterCache = new Map<number, Intl.NumberFormat>();

function usdtFormatter(decimals: number): Intl.NumberFormat {
  const cached = usdtFormatterCache.get(decimals);
  if (cached !== undefined) {
    return cached;
  }
  const formatter = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  usdtFormatterCache.set(decimals, formatter);
  return formatter;
}

function isUsable(value: number): boolean {
  return typeof value === "number" && Number.isFinite(value);
}

/** `12345.6` -> `"12,345.60 USDT"`; a non-finite value -> `"-"`. */
export function formatUsdt(value: number, decimals = 2): string {
  if (!isUsable(value)) {
    return EM_DASH;
  }
  return `${usdtFormatter(decimals).format(value)} USDT`;
}

/** `12345.6` -> `"+12,345.60 USDT"`, `-12` -> `"-12.00 USDT"`. */
export function formatSignedUsdt(value: number, decimals = 2): string {
  if (!isUsable(value)) {
    return EM_DASH;
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${usdtFormatter(decimals).format(value)} USDT`;
}

/** `12.345` -> `"12.35%"` (the input is already a percentage). */
export function formatPercent(value: number, decimals = 2): string {
  if (!isUsable(value)) {
    return EM_DASH;
  }
  return `${usdtFormatter(decimals).format(value)}%`;
}

/** `12.345` -> `"+12.35%"`, `-3` -> `"-3.00%"`. */
export function formatSignedPercent(value: number, decimals = 2): string {
  if (!isUsable(value)) {
    return EM_DASH;
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${usdtFormatter(decimals).format(value)}%`;
}

/**
 * `0.1234` -> `"12.34%"`: multiplies a 0..1 ratio by 100.
 *
 * This is the formatter of `profit_pct`, `win_rate` and `max_drawdown_pct`,
 * which the API publishes as ratios.
 */
export function formatRatioPercent(ratio: number, decimals = 2): string {
  if (!isUsable(ratio)) {
    return EM_DASH;
  }
  return formatPercent(ratio * 100, decimals);
}

/** `formatRatioPercent` with an explicit sign: `0.1234` -> `"+12.34%"`. */
export function formatSignedRatioPercent(ratio: number, decimals = 2): string {
  if (!isUsable(ratio)) {
    return EM_DASH;
  }
  return formatSignedPercent(ratio * 100, decimals);
}

/**
 * Compact duration: `45` -> `"45s"`, `3670` -> `"1h 1m"`, `90061` -> `"1d 1h"`.
 *
 * A negative or non-finite input renders as an em dash.
 */
export function formatDuration(seconds: number): string {
  if (!isUsable(seconds) || seconds < 0) {
    return EM_DASH;
  }
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remaining = total % 60;

  if (days > 0) {
    return `${days}d ${hours}h`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  if (minutes > 0) {
    return `${minutes}m ${remaining}s`;
  }
  return `${remaining}s`;
}

/**
 * ISO-8601 timestamp -> `"2026-09-27 17:32:05 UTC"`.
 *
 * An unparsable timestamp renders as an em dash.
 */
export function formatTimestamp(iso: string): string {
  if (typeof iso !== "string" || iso.trim() === "") {
    return EM_DASH;
  }
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return EM_DASH;
  }
  const pad = (value: number) => String(value).padStart(2, "0");
  return (
    `${parsed.getUTCFullYear()}-${pad(parsed.getUTCMonth() + 1)}-${pad(parsed.getUTCDate())} ` +
    `${pad(parsed.getUTCHours())}:${pad(parsed.getUTCMinutes())}:${pad(parsed.getUTCSeconds())} UTC`
  );
}

/**
 * Direction of a signed value, as text: `"▲"`, `"▼"` or `"—"` for flat and
 * non-finite inputs. Plain Unicode text, never an emoji.
 */
export function directionGlyph(value: number): "▲" | "▼" | "—" {
  if (!isUsable(value) || value === 0) {
    return "—";
  }
  return value > 0 ? "▲" : "▼";
}

/** Tailwind text colour class of a signed value (`+` green, `-` red, `0` muted). */
export function directionClass(value: number): string {
  if (!isUsable(value) || value === 0) {
    return "text-muted-foreground";
  }
  return value > 0 ? "text-profit" : "text-loss";
}

/** Accessible wording of a direction, for `aria-label`s and table cells. */
export function directionLabel(value: number): string {
  if (!isUsable(value) || value === 0) {
    return "flat";
  }
  return value > 0 ? "up" : "down";
}
