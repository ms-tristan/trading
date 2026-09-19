/**
 * Pure helpers of the candlestick chart.
 *
 * Everything here is a plain function over the frozen JSON payloads: no React,
 * no DOM, no clock, no randomness. The component layer only has to render what
 * these helpers return, which keeps the tricky parts (windowing, marker
 * building, price lines, time conversion) unit-testable in isolation.
 *
 * Two invariants hold for every export:
 *
 * * **nothing throws** — a malformed payload degrades to an empty result;
 * * **no `NaN` ever reaches the chart** — an unparsable timestamp or a
 *   non-finite price is dropped instead of being forwarded to the canvas.
 */

import { formatNumber } from './format';
import type { Candle, Position, TradeRecord } from './types';

/**
 * Maximum number of candles handed to the charting library.
 *
 * Lightweight Charts redraws on the main thread; beyond a few hundred bars the
 * live refresh starts to fight the polling cadence. The API caps its own window
 * server-side, this is the rendering cap of the browser side.
 */
export const CANDLE_RENDER_LIMIT = 500;

/** Colour reference of an entry marker (design token, never a raw hex). */
export const MARKER_ENTRY_COLOR = 'var(--color-chart-marker-entry)';

/** Colour reference of an exit marker (design token, never a raw hex). */
export const MARKER_EXIT_COLOR = 'var(--color-chart-marker-exit)';

/** Colour reference of the average price line (design token, never a raw hex). */
export const AVERAGE_LINE_COLOR = 'var(--color-chart-average)';

/** Colour reference of the stop loss line (design token, never a raw hex). */
export const STOP_LINE_COLOR = 'var(--color-chart-stop)';

/**
 * The candles a chart may render: the most recent `limit` ones, in the input
 * order (the API returns them oldest first).
 *
 * An empty input, or an unusable `limit`, yields a bounded result rather than a
 * throw: a negative, zero, fractional or non-finite `limit` falls back to
 * {@link CANDLE_RENDER_LIMIT}. The returned array is always a copy, so a caller
 * can never mutate the payload it was given.
 */
export function visibleCandles(candles: Candle[], limit: number = CANDLE_RENDER_LIMIT): Candle[] {
  if (!Array.isArray(candles) || candles.length === 0) {
    return [];
  }
  const size =
    Number.isFinite(limit) && limit >= 1 ? Math.floor(limit) : CANDLE_RENDER_LIMIT;
  return candles.length <= size ? candles.slice() : candles.slice(candles.length - size);
}

/** One marker drawn on the chart, in the shape the markers plugin expects. */
export interface CandleMarker {
  /** UNIX seconds of the marker (never `NaN`). */
  time: number;
  position: 'aboveBar' | 'belowBar';
  /** CSS colour reference of the design token set. */
  color: string;
  shape: 'arrowUp' | 'arrowDown';
  /** Human-readable label ('Long 20,000.00', 'take_profit 20,500.00', ...). */
  text: string;
}

/** Whether `value` is a usable, finite number. */
function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/**
 * Build the entry and exit markers of every closed trade.
 *
 * A long trade enters with an upward arrow below the bar and exits with a
 * downward arrow above it; a short trade is mirrored. Between the two, position
 * *and* shape carry the direction, so the marker stays readable without colour.
 * A record whose time or price is unusable is skipped entirely rather than
 * rendered at a guessed coordinate.
 */
export function tradeMarkers(trades: TradeRecord[]): CandleMarker[] {
  if (!Array.isArray(trades) || trades.length === 0) {
    return [];
  }
  const markers: CandleMarker[] = [];
  for (const trade of trades) {
    if (trade === null || typeof trade !== 'object') {
      continue;
    }
    const entryTime = candleTime(trade.entry_time);
    const exitTime = candleTime(trade.exit_time);
    if (
      entryTime === null ||
      exitTime === null ||
      !isFiniteNumber(trade.entry_price) ||
      !isFiniteNumber(trade.exit_price)
    ) {
      continue;
    }
    const isLong = trade.direction !== 'short';
    const directionLabel = isLong ? 'Long' : 'Short';
    markers.push({
      time: entryTime,
      position: isLong ? 'belowBar' : 'aboveBar',
      color: MARKER_ENTRY_COLOR,
      shape: isLong ? 'arrowUp' : 'arrowDown',
      text: `${directionLabel} ${formatNumber(trade.entry_price)}`,
    });
    markers.push({
      time: exitTime,
      position: isLong ? 'aboveBar' : 'belowBar',
      color: MARKER_EXIT_COLOR,
      shape: isLong ? 'arrowDown' : 'arrowUp',
      text: `${trade.exit_reason} ${formatNumber(trade.exit_price)}`,
    });
  }
  return markers;
}

/** One horizontal price line of the chart. */
export interface CandlePriceLine {
  price: number;
  /** CSS colour reference of the design token set. */
  color: string;
  /** Axis label of the line ('Average', 'Stop'). */
  title: string;
  lineStyle: 'solid' | 'dashed';
}

/**
 * Price lines of the open positions: the average entry price of each position,
 * and its stop loss when the engine has set one.
 *
 * The stop line is dashed and the average line solid, so the two stay
 * distinguishable on a monochrome screen — a trader must be able to read where
 * the position is protected without relying on the colour of the line.
 */
export function positionPriceLines(positions: Position[]): CandlePriceLine[] {
  if (!Array.isArray(positions) || positions.length === 0) {
    return [];
  }
  const lines: CandlePriceLine[] = [];
  for (const position of positions) {
    if (position === null || typeof position !== 'object') {
      continue;
    }
    if (isFiniteNumber(position.average_price)) {
      lines.push({
        price: position.average_price,
        color: AVERAGE_LINE_COLOR,
        title: 'Average',
        lineStyle: 'solid',
      });
    }
    if (isFiniteNumber(position.stop_price)) {
      lines.push({
        price: position.stop_price,
        color: STOP_LINE_COLOR,
        title: 'Stop',
        lineStyle: 'dashed',
      });
    }
  }
  return lines;
}

/**
 * Convert an ISO-8601 timestamp to the UNIX seconds the chart plots on.
 *
 * Returns `null` for anything unparsable (an empty string, a date without a
 * time, a non-string), so a bad row is skipped instead of producing `NaN`
 * coordinates.
 */
export function candleTime(timestamp: string): number | null {
  if (typeof timestamp !== 'string' || timestamp.trim() === '') {
    return null;
  }
  const parsed = Date.parse(timestamp);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  return Math.floor(parsed / 1000);
}

/**
 * Direction of one candle: the comparison of its close against its open.
 *
 * A doji (equal values) and a candle missing either bound are `'flat'`; the
 * chart paints a flat candle with the bullish (hollow) style, which is the
 * convention of the charting library itself.
 */
export function candleDirection(candle: Candle): 'bull' | 'bear' | 'flat' {
  if (candle === null || typeof candle !== 'object') {
    return 'flat';
  }
  const open = candle.open;
  const close = candle.close;
  if (!isFiniteNumber(open) || !isFiniteNumber(close) || open === close) {
    return 'flat';
  }
  return close > open ? 'bull' : 'bear';
}
