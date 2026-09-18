/**
 * Design-token bridge of the candlestick chart.
 *
 * The charting library paints on a `<canvas>`, where Tailwind classes do not
 * exist: the colours have to be read back from the document at runtime. This
 * module is the single place that does it, and it does it the strict way —
 *
 * * a token is read from the `@theme` block of `globals.css` through
 *   `getComputedStyle`, never duplicated as a literal;
 * * a token that cannot be resolved yields `undefined`, so the caller keeps the
 *   library's own default instead of inventing a colour;
 * * **nothing here ever throws**: a missing `document`, a missing
 *   `getComputedStyle`, or a throwing implementation all degrade to
 *   `undefined`, which is what lets the chart render in a non-browser
 *   environment (jsdom, a server render) without blowing up the view.
 */

/**
 * Every token the candlestick chart reads.
 *
 * The first ten are the additive chart tokens of `globals.css`; the last two are
 * the axis and grid tokens the equity chart already uses, shared so both charts
 * of the dashboard look like one product.
 */
export const CANDLE_TOKEN_NAMES: readonly string[] = [
  '--color-chart-bull',
  '--color-chart-bear',
  '--color-chart-bull-hollow',
  '--color-chart-wick-bull',
  '--color-chart-wick-bear',
  '--color-chart-crosshair',
  '--color-chart-marker-entry',
  '--color-chart-marker-exit',
  '--color-chart-stop',
  '--color-chart-average',
  '--color-chart-grid',
  '--color-chart-axis',
];

/**
 * Resolve the computed value of one CSS custom property.
 *
 * @returns the trimmed value, or `undefined` when there is no document, when the
 *   property is unset, or when the environment cannot compute styles.
 */
export function chartToken(name: string): string | undefined {
  try {
    if (typeof document === 'undefined' || document === null) {
      return undefined;
    }
    if (typeof getComputedStyle !== 'function') {
      return undefined;
    }
    const value = getComputedStyle(document.documentElement).getPropertyValue(name);
    if (typeof value !== 'string') {
      return undefined;
    }
    const trimmed = value.trim();
    return trimmed === '' ? undefined : trimmed;
  } catch {
    // An environment without a usable style engine renders the chart bare.
    return undefined;
  }
}

/**
 * Resolve every {@link CANDLE_TOKEN_NAMES} entry at once.
 *
 * A missing token keeps an explicit `undefined` entry, so a caller can tell
 * "this token is not defined here" from "this token was never asked for".
 */
export function chartTheme(): Record<string, string | undefined> {
  const theme: Record<string, string | undefined> = {};
  for (const name of CANDLE_TOKEN_NAMES) {
    theme[name] = chartToken(name);
  }
  return theme;
}
